"""The PowerShades integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, PLATFORMS
from .coordinator import PowerShadesConfigEntry, PowerShadesCoordinator

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup_entry(hass: HomeAssistant, entry: PowerShadesConfigEntry) -> bool:
    """Set up PowerShades from a config entry."""
    coordinator = PowerShadesCoordinator(hass, entry)
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady as err:
        _LOGGER.debug("Could not contact PowerShades yet: %s", err)
        raise

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PowerShadesConfigEntry) -> bool:
    """Unload a PowerShades config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
