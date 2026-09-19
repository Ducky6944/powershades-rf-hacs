"""Number platform for PowerShades — one "travel time" entity per shade.

The per-shade full 0↔100% sweep time (seconds) that drives the timed
"set to N%" math. Editing it here updates the config entry immediately, so the
next move uses the new value. (You can also set it during setup / options flow.)
"""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_TRAVEL_TIMES, DOMAIN
from .coordinator import PowerShadesCoordinator
from .types import GatewayChannel

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PowerShadesCoordinator = entry.runtime_data
    entities = [PowerShadesTravelTime(coordinator, ch.channel) for ch in coordinator.data.gateway if ch.linked]
    async_add_entities(entities)


class PowerShadesTravelTime(CoordinatorEntity[PowerShadesCoordinator], NumberEntity):
    """Seconds a shade takes for a full 0↔100% travel (per-channel)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Travel time"
    _attr_native_min_value = 1
    _attr_native_max_value = 600
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_icon = "mdi:timer-outline"

    def __init__(self, coordinator: PowerShadesCoordinator, channel: int) -> None:
        super().__init__(coordinator)
        self._channel = channel
        self._attr_unique_id = f"{DOMAIN}_{coordinator.config_entry.entry_id}_ch{channel}_travel"
        ch = coordinator.data.channel(channel) or GatewayChannel(channel=channel)
        self._attr_device_info = coordinator.channel_device_info(ch)

    @property
    def native_value(self) -> float:
        return float(self.coordinator.travel_time_for(self._channel))

    @property
    def available(self) -> bool:
        ch = self.coordinator.data.channel(self._channel)
        return ch is not None and ch.linked

    async def async_set_native_value(self, value: float) -> None:
        seconds = max(1, min(600, float(value)))
        data = dict(self.coordinator.config_entry.data)
        overrides = {str(k): v for k, v in (data.get(CONF_TRAVEL_TIMES) or {}).items()}
        overrides[str(self._channel)] = seconds
        data[CONF_TRAVEL_TIMES] = overrides
        await self.hass.config_entries.async_update_entry(self.coordinator.config_entry, data=data)
        _LOGGER.info("ch%s travel time set to %ss", self._channel, seconds)
