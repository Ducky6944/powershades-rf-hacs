"""Config flow for PowerShades (local-first).

Primary input is the **local RF gateway** address (the state + control plane).
Cloud credentials are *optional* and used only to resolve shade names and drive
absolute-position moves; the user supplies either an **account API key** or
**email + password**. If neither is supplied, the flow offers a manual
per-channel naming step so unlinked channels still get usable names.
"""

from __future__ import annotations

import json
import logging

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

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

    # -- step 1: gateway (+ optional cloud credentials) -------------------

    async def async_step_user(self, user_input: dict[str, str] | None = None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            gateway = _normalize_gateway(user_input.get(CONF_GATEWAY) or "")
            self._base_url = (user_input.get(CONF_BASE_URL) or DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
            self._gateway = gateway
            api_key = (user_input.get(CONF_API_KEY) or "").strip()
            email = (user_input.get(CONF_EMAIL) or "").strip()
            password = user_input.get(CONF_PASSWORD) or ""

            creds = bool(api_key) or (bool(email) and bool(password))
            if api_key and (email or password):
                errors["base"] = "credential_conflict"
                return self._show_user(errors)

            if gateway:
                await self.async_set_unique_id(_gateway_key(gateway))
                self._abort_if_unique_id_configured()

                async with aiohttp.ClientSession() as session:
                    try:
                        self._linked = await _probe_gateway(session, gateway)
                        if creds:
                            await _validate_cloud(session, api_key, email, password, self._base_url)
                    except PowerShadesUnavailable as err:
                        _LOGGER.debug("Gateway unreachable: %s", err)
                        errors["base"] = "gateway_unreachable"
                    except PowerShadesError as err:
                        _LOGGER.debug("Cloud credentials invalid: %s", err)
                        errors["base"] = "invalid_auth"
                    except Exception:
                        _LOGGER.exception("Unexpected config-flow error")
                        errors["base"] = "unknown"

            if not gateway:
                errors["base"] = "required"
            if errors:
                return self._show_user(errors)

            if creds:
                return self.async_create_entry(
                    title=_title_for(gateway),
                    data={
                        CONF_GATEWAY: gateway,
                        CONF_BASE_URL: self._base_url,
                        **_credential_fields(api_key, email, password),
                    },
                )
            return await self.async_step_name(None)

        return self._show_user(errors)

    def _show_user(self, errors):
        return self.async_show_form(step_id="user", data_schema=USER_SCHEMA, errors=errors)

    # -- step 2: manual channel naming (when no cloud credentials) -------

    async def async_step_name(self, user_input: dict[str, str] | None = None) -> ConfigFlowResult:
        """Optionally name each linked channel (no names available locally)."""
        if user_input is not None:
            names = {
                int(key[2:]): str(value).strip() for key, value in user_input.items() if key.startswith("ch") and value
            }
            data: dict = {CONF_GATEWAY: self._gateway or "", CONF_BASE_URL: self._base_url}
            if names:
                data[CONF_CHANNEL_NAMES] = {str(k): v for k, v in names.items()}
            return self.async_create_entry(title=_title_for(self._gateway), data=data)

        schema = {vol.Optional(f"ch{n}"): vol.Coerce(str) for n in self._linked}
        return self.async_show_form(
            step_id="name",
            data_schema=vol.Schema(schema),
            description_placeholders={"count": len(self._linked), "gateway": self._gateway or ""},
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
    return {CONF_EMAIL: email, CONF_PASSWORD: password}


def _title_for(gateway: str | None) -> str:
    if not gateway:
        return "PowerShades"
    return gateway.split("://", 1)[-1].split("/", 1)[0].rstrip(":")


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
