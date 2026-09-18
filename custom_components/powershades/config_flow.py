"""Config + options flow for PowerShades (local-first).

Primary input is the **local RF gateway** address (the state + control plane).
Because the gateway reports channels positionally (no name / device-id bridge to
the cloud), each linked channel is optionally **named by the user** during setup
(again via the options flow). Cloud credentials (API key or e-mail+password) are
optional and only add **group** covers (absolute-position moves); they do *not*
auto-name the per-channel covers.
"""

from __future__ import annotations

import json
import logging

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryOptionsFlow,
    ConfigFlow,
    ConfigFlowResult,
)

from .client import (
    PowerShadesClient,
    PowerShadesError,
    PowerShadesUnavailable,
    _parse_gateway,
)
from .const import (
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_CHANNEL_NAMES,
    CONF_EMAIL,
    CONF_GATEWAY,
    CONF_PASSWORD,
    DEFAULT_BASE_URL,
    DOMAIN,
    GW_AJAX_PATH,
)

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_GATEWAY): str,
        vol.Optional(CONF_BASE_URL, default=DEFAULT_BASE_URL): str,
        vol.Optional(CONF_API_KEY): str,
        vol.Optional(CONF_EMAIL): str,
        vol.Optional(CONF_PASSWORD): str,
    }
)

GATEWAY_VARS = ("percent", "battery", "rx", "rfdevs")


class PowerShadesConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the PowerShades config flow."""

    VERSION = 1

    _gateway: str | None = None
    _linked: list[int] = []
    _base_url: str = DEFAULT_BASE_URL
    _credentials: dict[str, str] = {}

    # -- step 1: gateway (+ optional cloud credentials) -------------------

    async def async_step_user(self, user_input: dict[str, str] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            gateway = _normalize_gateway(user_input.get(CONF_GATEWAY) or "")
            self._base_url = (user_input.get(CONF_BASE_URL) or DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
            self._gateway = gateway
            api_key = (user_input.get(CONF_API_KEY) or "").strip()
            email = (user_input.get(CONF_EMAIL) or "").strip()
            password = user_input.get(CONF_PASSWORD) or ""

            self._credentials = _credential_fields(api_key, email, password)

            errors: dict[str, str] = {}
            if api_key and (email or password):
                errors["base"] = "credential_conflict"
                return self._show_user(errors)

            if gateway:
                await self.async_set_unique_id(_gateway_key(gateway))
                self._abort_if_unique_id_configured()

                async with aiohttp.ClientSession() as session:
                    try:
                        self._linked = await _probe_gateway(session, gateway)
                        if self._credentials:
                            await _validate_cloud(session, api_key or None, email or None, password, self._base_url)
                    except PowerShadesUnavailable as err:
                        _LOGGER.debug("Gateway unreachable: %s", err)
                        errors["base"] = "gateway_unreachable"
                    except PowerShadesError as err:
                        _LOGGER.debug("Cloud credentials invalid: %s", err)
                        errors["base"] = "invalid_auth"
                    except Exception:
                        _LOGGER.exception("Unexpected config-flow error")
                        errors["base"] = "unknown"
            else:
                errors["base"] = "required"

            if errors:
                return self._show_user(errors)

            # Always let the user name the linked channels (there is no local
            # name source); this is the only way to get readable entity names.
            return await self.async_step_name(None)

        return self._show_user({})

    def _show_user(self, errors):
        return self.async_show_form(step_id="user", data_schema=USER_SCHEMA, errors=errors)

    # -- step 2: name each linked channel ---------------------------------

    async def async_step_name(self, user_input: dict[str, str] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            names = {
                int(key[2:]): str(value).strip() for key, value in user_input.items() if key.startswith("ch") and value
            }
            data: dict = {
                CONF_GATEWAY: self._gateway or "",
                CONF_BASE_URL: self._base_url,
                **self._credentials,
            }
            if names:
                data[CONF_CHANNEL_NAMES] = {str(k): v for k, v in names.items()}
            return self.async_create_entry(title=_title_for(self._gateway), data=data)

        schema = {vol.Optional(f"ch{n}"): vol.Coerce(str) for n in self._linked}
        return self.async_show_form(
            step_id="name",
            data_schema=vol.Schema(schema),
            description_placeholders={"count": len(self._linked), "gateway": self._gateway or ""},
        )

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> PowerShadesOptionsFlow:
        return PowerShadesOptionsFlow(config_entry)


class PowerShadesOptionsFlow(ConfigEntryOptionsFlow, domain=DOMAIN):
    """Rename linked channels (and clear) without re-adding the entry."""

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            names = {
                int(key[2:]): str(value).strip() for key, value in user_input.items() if key.startswith("ch") and value
            }
            data = dict(self.config_entry.data)
            if names:
                data[CONF_CHANNEL_NAMES] = {str(k): v for k, v in names.items()}
            else:
                data.pop(CONF_CHANNEL_NAMES, None)
            await self.hass.config_entries.async_update_entry(self.config_entry, data=data)
            return self.async_create_entry(title="", data=user_input)

        coordinator = self.config_entry.runtime_data
        linked = [c.channel for c in coordinator.data.gateway if c.linked]
        schema = {vol.Optional(f"ch{n}"): vol.Coerce(str) for n in linked}
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema),
            description_placeholders={"count": len(linked)},
        )


def _normalize_gateway(value: str) -> str | None:
    value = (value or "").strip().rstrip("/")
    if not value:
        return None
    return value if "://" in value else f"http://{value}"


def _gateway_key(gateway: str) -> str:
    hostport = gateway.split("://", 1)[-1].split("/", 1)[0]
    return hostport.lower()


def _credential_fields(api_key: str, email: str, password: str) -> dict[str, str]:
    if api_key:
        return {CONF_API_KEY: api_key}
    if email and password:
        return {CONF_EMAIL: email, CONF_PASSWORD: password}
    return {}


def _title_for(gateway: str | None) -> str:
    host = (gateway or "").split("://", 1)[-1].split("/", 1)[0]
    return f"PowerShades {host}" if host else "PowerShades"


async def _probe_gateway(session: aiohttp.ClientSession, gateway: str) -> list[int]:
    """Reach the gateway and return linked channel numbers (best-effort)."""
    url = f"{gateway}{GW_AJAX_PATH}?var=" + ",".join(GATEWAY_VARS)
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
        if not (200 <= resp.status < 300):
            raise PowerShadesUnavailable(f"gateway HTTP {resp.status}")
        text = await resp.text()
    payload = json.loads(text)
    channels = _parse_gateway(payload) or []
    return [c.channel for c in channels if c.linked]


async def _validate_cloud(session, api_key, email, password, base_url) -> None:
    client = PowerShadesClient(
        session,
        gateway=None,
        email=email or None,
        password=password or None,
        api_key=api_key or None,
        base_url=base_url,
    )
    await client.async_validate()
