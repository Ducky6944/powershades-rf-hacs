"""Config + options flow for PowerShades (local RF gateway only).

Setup is a small, guided flow:

1. **gateway + travel time** — the gateway address, and how long a shade takes
   for a full 0→100% sweep (used to emulate "set to N%", which the gateway
   itself can't do).
2. **identify each channel** — for every linked channel the user sees a name
   field plus a "test" dropdown (idle / open / close). Tapping "open" or
   "close" physically moves that shade so the user can match it to a real
   window, name it, and continue to the next channel.
3. **groups (optional)** — one line each, ``Name: 1,2,3``.
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

# Reuse the parser + client from the runtime so both sides agree on shapes.
from .client import PowerShadesClient, PowerShadesUnavailable, _parse_gateway
from .const import (
    CONF_AVAILABLE,
    CONF_CHANNEL_NAMES,
    CONF_GATEWAY,
    CONF_GROUPS,
    CONF_TRAVEL_TIME,
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

CHANNEL_SCHEMA = vol.Schema(
    {
        vol.Required("action", default="idle"): vol.In(["idle", "up", "down"]),
        vol.Optional("name", default=""): vol.Coerce(str),
    }
)

GROUPS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_GROUPS, default=""): vol.Coerce(str),
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
            return self.async_show_form(
                step_id="channel",
                data_schema=CHANNEL_SCHEMA,
                description_placeholders={"n": self._i, "m": len(self._linked)},
            )

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

    # -- Step N: per-channel name + test ----------------------------------

    async def async_step_channel(self, user_input: dict | None = None) -> ConfigFlowResult:
        n = self._linked[self._i - 1] if 1 <= self._i <= len(self._linked) else None
        if n is None:
            return self.async_abort(reason="no_shades")

        if user_input is not None:
            action = user_input.get("action", "idle")
            name = str(user_input.get("name") or "").strip()
            if name:
                self._names[n] = name

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

            self._i += 1
            if self._i <= len(self._linked):
                return self.async_show_form(
                    step_id="channel",
                    data_schema=CHANNEL_SCHEMA,
                    description_placeholders={"n": self._i, "m": len(self._linked)},
                )
            return await self.async_step_groups()

        return self.async_show_form(
            step_id="channel",
            data_schema=CHANNEL_SCHEMA,
            description_placeholders={"n": max(self._i, 1), "m": len(self._linked)},
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


class PowerShadesOptionsFlow(OptionsFlowWithConfigEntry):
    """Edit names / groups / travel time without re-adding the entry."""

    async def async_step_init(self, user_input: dict | None = None) -> ConfigFlowResult:
        current = self.config_entry.data
        if user_input is not None:
            data = dict(current)
            tt = user_input.get(CONF_TRAVEL_TIME)
            if tt:
                data[CONF_TRAVEL_TIME] = float(tt)
            groups = _parse_groups(user_input.get(CONF_GROUPS) or "")
            if groups:
                data[CONF_GROUPS] = groups
            else:
                data.pop(CONF_GROUPS, None)
            await self.hass.config_entries.async_update_entry(self.config_entry, data=data)
            return self.async_create_entry(title="", data={})

        schema_opts = {
            vol.Required(
                CONF_TRAVEL_TIME,
                default=float(current.get(CONF_TRAVEL_TIME) or DEFAULT_TRAVEL_TIME),
            ): vol.Coerce(float),
            vol.Optional(CONF_GROUPS, default=""): vol.Coerce(str),
        }
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema_opts))
