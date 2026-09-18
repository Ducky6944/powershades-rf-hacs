"""Config + options flow for PowerShades (local RF gateway only).

Setup is a small, guided flow:

1. **gateway + base travel time** — the gateway address, and a *base* full-
   0→100% sweep time (used to emulate "set to N%", which the gateway can't do
   on its own). Each shade can override this in the next step.
2. **identify each channel** — for every linked channel the user picks
   *up* or *down* to nudge that physical shade and spot it. They can do this as
   many times as needed (nothing advances until they tick "move on"), then
   optionally name the shade and set its own travel time.
3. **groups (optional)** — one per line in a textarea, ``Name: 1,2,3``.
4. **done**.

No cloud. No email / password / API key. The gateway is the integration.
"""

from __future__ import annotations

import json
import logging
import re

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithConfigEntry,
)
from homeassistant.helpers import config_validation as cv, selector

# Reuse the parser + client from the runtime so both sides agree on shapes.
from .client import PowerShadesClient, PowerShadesUnavailable, _parse_gateway
from .const import (
    CONF_AVAILABLE,
    CONF_CHANNEL_NAMES,
    CONF_GATEWAY,
    CONF_GROUPS,
    CONF_TRAVEL_TIME,
    CONF_TRAVEL_TIMES,
    DOMAIN,
    GW_AJAX_PATH,
)

_LOGGER = logging.getLogger(__name__)

# Variables we ask the gateway to report (same as runtime coordinator).
PROBE_VARS = ("percent", "battery", "rx", "rfdevs")

DEFAULT_TRAVEL_TIME = 20.0


# -- Schemas ---------------------------------------------------------------

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_GATEWAY): vol.Coerce(str),
        vol.Optional(CONF_TRAVEL_TIME, default=DEFAULT_TRAVEL_TIME): vol.Coerce(float),
    }
)

# Per-channel identify step. ``action`` nudge (up/down) fires on submit (blank =
# no movement, just save the name/travel). ``advance`` must be ticked to move
# on, so the user can nudge as many times as needed before committing.
CHANNEL_SCHEMA = vol.Schema(
    {
        vol.Optional("action"): vol.In(["up", "down"]),
        vol.Optional("name", default=""): vol.Coerce(str),
        vol.Optional("travel_time", default=""): vol.Coerce(str),
        vol.Required("advance", default=False): cv.boolean,
    }
)

# Groups as a multiline textarea (one ``Name: 1,2,3`` per line).
GROUPS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_GROUPS, default=""): selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
    }
)


def _parse_groups(text: str) -> dict[str, list[int]]:
    """Parse ``"Name: 1,2,3; Other: 1,4,5"`` (or newline-separated) into a dict."""
    out: dict[str, list[int]] = {}
    for part in re.split(r"[;\n]+", text or ""):
        part = (part or "").strip()
        if not part or ":" not in part:
            continue
        name, _, chans = part.partition(":")
        name = name.strip()
        if not name:
            continue
        channels: list[int] = []
        for tok in re.split(r"[\s,]+", chans):
            tok = tok.strip()
            if tok.isdigit():
                channels.append(int(tok))
        if channels:
            out[name] = channels
    return out


def _parse_travel_times(text: str) -> dict[int, float]:
    """Parse per-channel travel-time overrides (``"12=25``" lines) into
    ``{channel: seconds}``. Malformed lines are ignored."""
    out: dict[int, float] = {}
    for part in re.split(r"[;\n]+", text or ""):
        part = (part or "").strip()
        if not part or "=" not in part:
            continue
        ch, _, secs = part.partition("=")
        try:
            ch_i = int(ch.strip())
            s = float(secs.strip())
        except ValueError:
            continue
        if ch_i > 0 and s > 0:
            out[ch_i] = s
    return out


def _normalize_gateway(value: str) -> str | None:
    value = (value or "").strip().rstrip("/")
    if not value:
        return None
    return value if "://" in value else f"http://{value}"


def _gateway_key(gateway: str) -> str:
    hostport = gateway.split("://", 1)[-1].split("/", 1)[0]
    return hostport.lower()


def _title_for(gateway: str | None) -> str:
    host = (gateway or "").split("://", 1)[-1].split("/", 1)[0]
    return f"PowerShades {host}" if host else "PowerShades"


class PowerShadesConfigFlow(ConfigFlow, domain=DOMAIN):
    """Gateway → (name + test) per channel → groups → done."""

    VERSION = 1

    _gateway: str | None = None
    _travel_time: float = DEFAULT_TRAVEL_TIME
    _linked: list[int] = []
    _names: dict[int, str] = {}
    _travel_times: dict[int, float] = {}
    _available: set[str] = set()
    _i: int = 0  # 1-based index into self._linked

    # -- Step 1: gateway + travel time ------------------------------------

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            gateway = _normalize_gateway(user_input.get(CONF_GATEWAY) or "")
            self._travel_time = float(user_input.get(CONF_TRAVEL_TIME) or DEFAULT_TRAVEL_TIME)
            self._gateway = gateway
            self._linked = []
            self._names = {}
            self._travel_times = {}
            self._available = set()
            self._i = 0

            if not gateway:
                return self.async_show_form(step_id="user", data_schema=USER_SCHEMA, errors={"base": "required"})

            await self.async_set_unique_id(_gateway_key(gateway))
            self._abort_if_unique_id_configured()

            try:
                await self._probe(gateway)
            except PowerShadesUnavailable as err:
                _LOGGER.debug("gateway unreachable: %s", err)
                return self.async_show_form(
                    step_id="user", data_schema=USER_SCHEMA, errors={"base": "gateway_unreachable"}
                )
            except Exception:
                _LOGGER.exception("unexpected probe failure")
                return self.async_show_form(step_id="user", data_schema=USER_SCHEMA, errors={"base": "unknown"})

            if not self._linked:
                return self.async_show_form(step_id="user", data_schema=USER_SCHEMA, errors={"base": "no_shades"})

            self._i = 1
            return self._show_channel()

        return self.async_show_form(step_id="user", data_schema=USER_SCHEMA)

    async def _probe(self, gateway: str) -> None:
        """Hit the gateway's ajax endpoint and learn what's actually reporting."""
        url = f"{gateway}{GW_AJAX_PATH}?var=" + ",".join(PROBE_VARS)
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if not (200 <= resp.status < 300):
                        raise PowerShadesUnavailable(f"HTTP {resp.status}")
                    payload = json.loads(await resp.text())
            except (aiohttp.ClientError, TimeoutError) as err:
                raise PowerShadesUnavailable(str(err)) from err

        channels = _parse_gateway(payload) or []
        for c in channels:
            if c.get("device_id") or c.get("percent"):
                self._linked.append(int(c["channel"]))
                if c.get("percent") is not None:
                    self._available.add("percent")
                if c.get("battery_v") is not None:
                    self._available.add("battery")
                if c.get("rx_db") is not None:
                    self._available.add("rx")
                if c.get("device_id"):
                    self._available.add("device_id")
        self._linked.sort()

    # -- Step N: per-channel identify ---------------------------------------

    async def async_step_channel(self, user_input: dict | None = None) -> ConfigFlowResult:
        n = self._linked[self._i - 1] if 1 <= self._i <= len(self._linked) else None
        if n is None:
            return self.async_abort(reason="no_shades")

        if user_input is not None:
            # 1. Fire the nudge (up/down) if the user picked one. Blank = no movement.
            action = user_input.get("action")
            if self._gateway and action in ("up", "down"):
                async with aiohttp.ClientSession() as session:
                    client = PowerShadesClient(session, gateway=self._gateway)
                    try:
                        if action == "up":
                            await client.gateway_up(n)
                        else:
                            await client.gateway_down(n)
                    except Exception as err:
                        _LOGGER.warning("Test %s on ch%s failed: %s", action, n, err)

            # 2. Capture the (optional) name and (optional) travel-time override.
            name = str(user_input.get("name") or "").strip()
            if name:
                self._names[n] = name
            raw_tt = user_input.get("travel_time")
            try:
                tt = float(raw_tt)
            except (TypeError, ValueError):
                tt = None
            if tt and tt > 0:
                self._travel_times[n] = tt

            # 3. Advance only when the user says they're done matching this shade.
            if user_input.get("advance"):
                self._i += 1
                if self._i <= len(self._linked):
                    return self._show_channel()
                return await self.async_step_groups()

            # Otherwise stay on this channel (let them keep nudging to spot it).
            return self._show_channel()

        return self._show_channel()

    def _show_channel(self) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="channel",
            data_schema=CHANNEL_SCHEMA,
            description_placeholders={"n": self._i, "m": len(self._linked)},
        )

    # -- Final step: optional groups ---------------------------------------

    async def async_step_groups(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            groups = _parse_groups(user_input.get(CONF_GROUPS) or "")
            data: dict = {
                CONF_GATEWAY: self._gateway or "",
                CONF_TRAVEL_TIME: self._travel_time,
                CONF_CHANNEL_NAMES: {str(k): v for k, v in self._names.items()},
                CONF_AVAILABLE: sorted(self._available),
            }
            if self._travel_times:
                data[CONF_TRAVEL_TIMES] = {str(k): v for k, v in self._travel_times.items()}
            if groups:
                data[CONF_GROUPS] = groups
            return self.async_create_entry(title=_title_for(self._gateway), data=data)

        return self.async_show_form(
            step_id="groups",
            data_schema=GROUPS_SCHEMA,
            description_placeholders={"n": len(self._linked)},
        )

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> PowerShadesOptionsFlow:
        return PowerShadesOptionsFlow(config_entry)


OPTIONS_MULTILINE = selector.TextSelector(selector.TextSelectorConfig(multiline=True))


def _current_names(data: dict) -> str:
    names = data.get(CONF_CHANNEL_NAMES) or {}
    return "\n".join(f"{ch}={name}" for ch, name in sorted((int(k), v) for k, v in names.items()))


def _current_travel_times(data: dict) -> str:
    tts = data.get(CONF_TRAVEL_TIMES) or {}
    return "\n".join(f"{ch}={tt}" for ch, tt in sorted((int(k), v) for k, v in tts.items()))


def _current_groups(data: dict) -> str:
    groups = data.get(CONF_GROUPS) or {}
    return "\n".join(f"{name}: {','.join(str(c) for c in chans)}" for name, chans in groups.items())


class PowerShadesOptionsFlow(OptionsFlowWithConfigEntry):
    """Edit base travel time, per-channel travel times, names, and groups —
    all without re-adding the entry. Every field is optional; blank = unchanged."""

    async def async_step_init(self, user_input: dict | None = None) -> ConfigFlowResult:
        current = self.config_entry.data
        if user_input is not None:
            data = dict(current)
            base = user_input.get(CONF_TRAVEL_TIME)
            try:
                if float(base) > 0:
                    data[CONF_TRAVEL_TIME] = float(base)
            except (TypeError, ValueError):
                pass

            groups = _parse_groups(user_input.get(CONF_GROUPS) or "")
            if groups:
                data[CONF_GROUPS] = groups
            else:
                data.pop(CONF_GROUPS, None)

            if user_input.get(CONF_TRAVEL_TIMES) is not None:
                tts = _parse_travel_times(user_input.get(CONF_TRAVEL_TIMES) or "")
                if tts:
                    data[CONF_TRAVEL_TIMES] = {str(k): v for k, v in tts.items()}
                else:
                    data.pop(CONF_TRAVEL_TIMES, None)

            if user_input.get("channel_names") is not None:
                names = self._parse_names(user_input.get("channel_names") or "")
                if names:
                    data[CONF_CHANNEL_NAMES] = {str(k): v for k, v in names.items()}
                else:
                    data.pop(CONF_CHANNEL_NAMES, None)

            await self.hass.config_entries.async_update_entry(self.config_entry, data=data)
            return self.async_create_entry(title="", data={})

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_TRAVEL_TIME,
                    default=str(float(current.get(CONF_TRAVEL_TIME) or DEFAULT_TRAVEL_TIME)),
                ): vol.Coerce(str),
                vol.Optional(CONF_GROUPS, default=_current_groups(current)): OPTIONS_MULTILINE,
                vol.Optional(CONF_TRAVEL_TIMES, default=_current_travel_times(current)): OPTIONS_MULTILINE,
                vol.Optional("channel_names", default=_current_names(current)): OPTIONS_MULTILINE,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    @staticmethod
    def _parse_names(text: str) -> dict[int, str]:
        out: dict[int, str] = {}
        for part in re.split(r"[;\n]+", text or ""):
            part = (part or "").strip()
            if not part or "=" not in part:
                continue
            ch, _, name = part.partition("=")
            name = name.strip()
            try:
                ch_i = int(ch.strip())
            except ValueError:
                continue
            if ch_i > 0 and name:
                out[ch_i] = name
        return out
