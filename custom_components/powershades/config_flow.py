"""Config flow for the PowerShades integration."""

from __future__ import annotations

import logging

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers import config_validation as cv

from .client import PowerShadesAuthError, PowerShadesClient, PowerShadesUnavailable
from .const import CONF_BASE_URL, CONF_EMAIL, CONF_GATEWAY, CONF_PASSWORD, DEFAULT_BASE_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_BASE_URL, default=DEFAULT_BASE_URL): cv.string,
        vol.Optional(CONF_GATEWAY): cv.string,
    }
)


class PowerShadesConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the PowerShades config flow."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, str] | None = None) -> ConfigFlowResult:
        """Handle the initial step (email + password)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD]
            base_url = (user_input.get(CONF_BASE_URL) or DEFAULT_BASE_URL).strip()
            gateway_raw = (user_input.get(CONF_GATEWAY) or "").strip()

            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_configured()

            gateway = _normalize_gateway(gateway_raw)

            try:
                conn = aiohttp.TCPConnector(ssl=True)
                timeout = aiohttp.ClientTimeout(total=15)
                async with aiohttp.ClientSession(connector=conn, timeout=timeout) as session:
                    client = PowerShadesClient(session, email, password, base_url)
                    await client.async_validate()
            except PowerShadesAuthError as err:
                _LOGGER.debug("Authentication failed: %s", err)
                return self.async_abort(reason="invalid_auth")
            except PowerShadesUnavailable as err:
                _LOGGER.debug("Could not reach PowerShades API: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during config flow")
                errors["base"] = "unknown"

            if not errors:
                return self.async_create_entry(
                    title=email,
                    data={
                        CONF_EMAIL: email,
                        CONF_PASSWORD: password,
                        CONF_BASE_URL: base_url,
                        CONF_GATEWAY: gateway,
                    },
                )

        return self.async_show_form(initial_values=user_input, schema=USER_SCHEMA, errors=errors)


def _normalize_gateway(value: str) -> str | None:
    """Turn a user-entered gateway address into an absolut HTTP URL."""
    if not value:
        return None
    value = value.strip().rstrip("/")
    if "://" in value:
        return value
    return f"http://{value}"
