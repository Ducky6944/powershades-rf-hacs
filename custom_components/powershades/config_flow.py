"""Config + options flow for PowerShades (local RF gateway only).

Setup is a small, guided flow you can move through in either direction:

1. **gateway + base travel time** — the gateway address, and a *base* full-
   0→100% sweep time (used to emulate "set to N%", which the gateway can't do
   on its own).
2. **identify each channel** — for every linked channel the user picks *up* /
   *down* to nudge that physical shade and spot it, then sets its name, travel
   time, and *position source* (estimate vs. gateway reading). Re-issuable —
   nothing advances until you tap **Next**.
3. **groups (optional)** — one per line in a textarea, ``Name: 1,2,3``.
4. **done**.

No cloud. No email / password / API key. The gateway is the integration.

Navigation: the HA frontend already tracks the order of steps you've visited and
shows its own **Back** button. Every step below is *idempotent* — re-showing it
re-seeds from the values already captured — so Back / Forward round-trips
losslessly without re-adding the integration.
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
    CONF_POSITION_SOURCE,
    CONF_TRAVEL_TIME,
    CONF_TRAVEL_TIMES,
    DOMAIN,
    GW_AJAX_PATH,
    POSITION_SOURCE_ESTIMATE,
    POSITION_SOURCE_GATEWAY,
)

_LOGGER = logging.getLogger(__name__)

# Variables we ask the gateway to report (same as runtime coordinator).
PROBE_VARS = ("percent", "battery", "rx", "rfdevs")

DEFAULT_TRAVEL_TIME = 20.0

_OPTIONS_MULTILINE = selector.TextSelector(selector.TextSelectorConfig(multiline=True))
_POSITION_SOURCE_OPTIONS = [POSITION_SOURCE_ESTIMATE, POSITION_SOURCE_GATEWAY]


# -- Schemas ---------------------------------------------------------------

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_GATEWAY): vol.Coerce(str),
        vol.Optional(CONF_TRAVEL_TIME, default=DEFAULT_TRAVEL_TIME): vol.Coerce(str),
    }
)

# Groups as a multiline textarea (one ``Name: 1,2,3`` per line).
GROUPS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_GROUPS, default=""): _OPTIONS_MULTILINE,
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
    """Parse per-channel travel-time overrides (``"12=25"`` lines) into
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


def _parse_names(text: str) -> dict[int, str]:
    """Parse per-channel names (``"12=Living Room"`` lines) into
    ``{channel: name}``. Malformed lines are ignored."""
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
    """Gateway → (nudge + name + travel + position-source) per channel → groups."""

    VERSION = 1

    _gateway: str | None = None
    _travel_time: float = DEFAULT_TRAVEL_TIME
    _linked: list[int] = []
    _names: dict[int, str] = {}
    _travel_times: dict[int, float] = {}
    _position_source: dict[int, str] = {}
    _available: set[str] = set()
    _i: int = 0  # 1-based index into self._linked

    # -- Step 1: gateway + travel time ------------------------------------

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is None:
            # Re-showing (Back from the identify step) — re-seed from captured values.
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_GATEWAY, default=self._gateway or ""): vol.Coerce(str),
                        vol.Optional(
                            CONF_TRAVEL_TIME,
                            default=str(self._travel_time or DEFAULT_TRAVEL_TIME),
                        ): vol.Coerce(str),
                    }
                ),
            )

        gateway = _normalize_gateway(user_input.get(CONF_GATEWAY) or "")
        self._travel_time = self._travel_time_from_input(user_input)
        self._gateway = gateway
        self._linked = []
        self._names = {}
        self._travel_times = {}
        self._position_source = {}
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
            return self.async_show_form(step_id="user", data_schema=USER_SCHEMA, errors={"base": "gateway_unreachable"})
        except Exception:
            _LOGGER.exception("unexpected probe failure")
            return self.async_show_form(step_id="user", data_schema=USER_SCHEMA, errors={"base": "unknown"})

        if not self._linked:
            return self.async_show_form(step_id="user", data_schema=USER_SCHEMA, errors={"base": "no_shades"})

        self._i = 1
        return self.async_show_channel()

    def _travel_time_from_input(self, user_input: dict) -> float:
        raw = user_input.get(CONF_TRAVEL_TIME)
        try:
            tt = float(raw)
        except (TypeError, ValueError):
            return self._travel_time or DEFAULT_TRAVEL_TIME
        return tt if tt > 0 else (self._travel_time or DEFAULT_TRAVEL_TIME)

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

            # 2. Capture the (optional) name, travel-time override, and position source.
            name = str(user_input.get("name") or "").strip()
            if name:
                self._names[n] = name
            raw_tt = user_input.get("travel_time")
            try:
                tt = float(raw_tt)
                if tt > 0:
                    self._travel_times[n] = tt
            except (TypeError, ValueError):
                pass
            source = user_input.get(CONF_POSITION_SOURCE)
            if source in (POSITION_SOURCE_ESTIMATE, POSITION_SOURCE_GATEWAY):
                self._position_source[n] = source

            # 3. Advance only when the user is done matching this shade.
            if user_input.get("advance"):
                self._i += 1
                if self._i <= len(self._linked):
                    return self.async_show_channel()
                return await self.async_step_groups()

        return self.async_show_channel()

    def async_show_channel(self) -> ConfigFlowResult:
        """Render the identify form for the current channel.

        Idempotent: if the user has already named this shade / set its travel
        time, re-showing re-seeds those values so Back / Forward never blanks them.
        """
        n = self._linked[self._i - 1]
        existing_name = self._names.get(n, "")
        existing_tt = self._travel_times.get(n)
        existing_source = self._position_source.get(n, POSITION_SOURCE_ESTIMATE)
        schema = vol.Schema(
            {
                vol.Optional("action"): vol.In(["up", "down"]),
                vol.Optional("name", default=existing_name): vol.Coerce(str),
                vol.Optional("travel_time", default=str(existing_tt) if existing_tt else ""): vol.Coerce(str),
                vol.Required(CONF_POSITION_SOURCE, default=existing_source): vol.In(_POSITION_SOURCE_OPTIONS),
                vol.Required("advance", default=False): cv.boolean,
            }
        )
        return self.async_show_form(
            step_id="channel",
            data_schema=schema,
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
                CONF_POSITION_SOURCE: {str(k): v for k, v in self._position_source.items()},
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
    """Edit base travel time, per-channel travel times, names, groups, and each
    channel's position source — without re-adding the entry. Optional + blank = unchanged.
    """

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
                names = _parse_names(user_input.get("channel_names") or "")
                if names:
                    data[CONF_CHANNEL_NAMES] = {str(k): v for k, v in names.items()}
                else:
                    data.pop(CONF_CHANNEL_NAMES, None)

            if user_input.get("position_source") is not None:
                sources = self._parse_position_source(user_input.get("position_source") or "")
                if sources:
                    data[CONF_POSITION_SOURCE] = {str(k): v for k, v in sources.items()}
                else:
                    data.pop(CONF_POSITION_SOURCE, None)

            await self.hass.config_entries.async_update_entry(self.config_entry, data=data)
            return self.async_create_entry(title="", data={})

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_TRAVEL_TIME,
                    default=str(float(current.get(CONF_TRAVEL_TIME) or DEFAULT_TRAVEL_TIME)),
                ): vol.Coerce(str),
                vol.Optional(CONF_GROUPS, default=_current_groups(current)): _OPTIONS_MULTILINE,
                vol.Optional(CONF_TRAVEL_TIMES, default=_current_travel_times(current)): _OPTIONS_MULTILINE,
                vol.Optional("channel_names", default=_current_names(current)): _OPTIONS_MULTILINE,
                vol.Optional(
                    "position_source",
                    default=self._current_position_source(current),
                ): _OPTIONS_MULTILINE,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    @staticmethod
    def _parse_position_source(text: str) -> dict[int, str]:
        out: dict[int, str] = {}
        for part in re.split(r"[;\n]+", text or ""):
            part = (part or "").strip()
            if not part or "=" not in part:
                continue
            ch, _, val = part.partition("=")
            val = val.strip()
            try:
                ch_i = int(ch.strip())
            except ValueError:
                continue
            if ch_i > 0 and val in (POSITION_SOURCE_ESTIMATE, POSITION_SOURCE_GATEWAY):
                out[ch_i] = val
        return out

    @staticmethod
    def _current_position_source(data: dict) -> str:
        sources = data.get(CONF_POSITION_SOURCE) or {}
        lines = [f"{ch}={src}" for ch, src in sorted((int(k), v) for k, v in sources.items())]
        return "\n".join(lines)
