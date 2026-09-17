"""Sensor platform for PowerShades (per-RF-channel state + RF diagnostics).

These report the **local gateway** live plane per channel: shade position,
battery voltage, and RF signal strength. They are diagnostic-grade.
"""

from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfElectricPotential
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PowerShadesCoordinator
from .types import GatewayChannel

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create live-channel + diagnostic sensors for each linked gateway channel."""
    coordinator: PowerShadesCoordinator = entry.runtime_data
    linked = [ch for ch in coordinator.data.gateway if ch.linked]
    if not linked:
        return

    entities: list[PowerShadesChannelSensor] = []
    for ch in linked:
        entities.append(
            PowerShadesChannelSensor(coordinator, ch.channel, "type", None, None, EntityCategory.DIAGNOSTIC)
        )
        entities.append(
            PowerShadesChannelSensor(coordinator, ch.channel, "position", None, PERCENTAGE, EntityCategory.DIAGNOSTIC)
        )
        entities.append(
            PowerShadesChannelSensor(
                coordinator,
                ch.channel,
                "battery",
                SensorDeviceClass.BATTERY,
                UnitOfElectricPotential.VOLT,
                EntityCategory.DIAGNOSTIC,
            )
        )
        entities.append(PowerShadesChannelSensor(coordinator, ch.channel, "rx", None, "dB", EntityCategory.DIAGNOSTIC))
    async_add_entities(entities)


class PowerShadesChannelSensor(CoordinatorEntity[PowerShadesCoordinator], SensorEntity):
    """One live/diagnostic reading from an RF gateway channel."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PowerShadesCoordinator,
        channel: int,
        key: str,
        device_class,
        unit,
        category,
    ) -> None:
        super().__init__(coordinator)
        self._channel = channel
        self._key = key
        self._attr_name = key.capitalize()
        self._attr_unique_id = f"{DOMAIN}_{coordinator.config_entry.entry_id}_gw_ch{channel}_{key}"
        ch = coordinator.data.channel(channel)
        self._attr_device_info = coordinator.channel_device_info(ch or GatewayChannel(channel=channel))
        self._attr_device_class = device_class
        self._attr_unit_of_measurement = unit
        self._attr_entity_category = category

    def _channel_data(self):
        return self.coordinator.data.channel(self._channel)

    @property
    def native_value(self):
        ch = self._channel_data()
        if ch is None:
            return None
        if self._key == "position":
            return ch.percent
        if self._key == "battery":
            return ch.battery_v
        if self._key == "rx":
            return ch.rx_db
        if self._key == "type":
            return ch.rf_type
        return None
