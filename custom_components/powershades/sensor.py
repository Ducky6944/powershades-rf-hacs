"""Sensor platform for PowerShades (per-channel RF diagnostics).

Reports the **local gateway** live plane per linked channel: position (percent),
battery voltage, RF signal strength, and the linked RF device id. All are tagged
``DIAGNOSTIC`` on purpose — they are read-back/diagnostic values, not the thing
you control. Position on the cover is independent and always usable regardless
of what these read.
"""

from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfElectricPotential
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PowerShadesCoordinator
from .types import GatewayChannel

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# (key, unit) — per-channel diagnostics worth surfacing. ``None`` = no unit.
SENSORS = (
    ("percent", None),
    ("battery", UnitOfElectricPotential.VOLT),
    ("rx", "dB"),
    ("device_id", None),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create per-channel diagnostic sensors (battery + rx) for each linked channel."""
    coordinator: PowerShadesCoordinator = entry.runtime_data
    linked = [ch for ch in coordinator.data.gateway if ch.linked]
    if not linked:
        return

    entities: list[PowerShadesChannelSensor] = [
        PowerShadesChannelSensor(coordinator, ch.channel, key, unit) for ch in linked for key, unit in SENSORS
    ]
    async_add_entities(entities)


class PowerShadesChannelSensor(CoordinatorEntity[PowerShadesCoordinator], SensorEntity):
    """One diagnostic reading from an RF gateway channel."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: PowerShadesCoordinator,
        channel: int,
        key: str,
        unit: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._channel = channel
        self._key = key
        self._attr_name = key.capitalize()
        self._attr_unique_id = f"{DOMAIN}_{coordinator.config_entry.entry_id}_gw_ch{channel}_{key}"
        ch = coordinator.data.channel(channel)
        self._attr_device_info = coordinator.channel_device_info(ch or GatewayChannel(channel=channel))
        self._attr_unit_of_measurement = unit

    def _channel_data(self):
        return self.coordinator.data.channel(self._channel)

    @property
    def native_value(self):
        ch = self._channel_data()
        if ch is None:
            return None
        if self._key == "percent":
            return ch.percent
        if self._key == "battery":
            return ch.battery_v
        if self._key == "rx":
            return ch.rx_db
        if self._key == "device_id":
            return ch.device_id
        return None
