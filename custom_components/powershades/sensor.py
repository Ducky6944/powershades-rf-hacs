"""Sensor platform for PowerShades (local gateway state per channel)."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfElectricPotential
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_EMAIL, DOMAIN
from .coordinator import PowerShadesCoordinator

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create live-state sensors for each local gateway channel.

    Note: the gateway is the feedback/state plane and is organized by RF
    channel; the cloud API is the control plane and is organized by shade
    name. We intentionally do not try to map channels to shade names here.
    """
    coordinator: PowerShadesCoordinator = entry.runtime_data
    linked = [ch for ch in coordinator.data.gateway if ch.device_id is not None or ch.percent is not None]
    if not linked:
        return

    entities: list[PowerShadesChannelSensor] = []
    for ch in linked:
        label = f"Channel {ch.channel}" + (f" ({ch.name})" if ch.name else "")
        entities.append(
            PowerShadesChannelSensor(
                coordinator, ch.channel, "position", f"{label} position", None, PERCENTAGE, EntityCategory.DIAGNOSTIC
            )
        )
        entities.append(
            PowerShadesChannelSensor(
                coordinator,
                ch.channel,
                "battery",
                f"{label} battery",
                SensorDeviceClass.BATTERY,
                UnitOfElectricPotential.VOLT,
                EntityCategory.DIAGNOSTIC,
            )
        )
        entities.append(
            PowerShadesChannelSensor(
                coordinator, ch.channel, "rx", f"{label} RF level", None, "dB", EntityCategory.DIAGNOSTIC
            )
        )
    async_add_entities(entities)


class PowerShadesChannelSensor(CoordinatorEntity[PowerShadesCoordinator], SensorEntity):
    """A per-channel diagnostic sensor reading live gateway state."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PowerShadesCoordinator,
        channel: int,
        key: str,
        name: str,
        device_class,
        unit,
        category,
    ) -> None:
        super().__init__(coordinator)
        self._channel = channel
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{coordinator.entry.data[CONF_EMAIL]}_gw_ch{channel}_{key}"
        self._attr_device_info = coordinator.device_info
        self._attr_device_class = device_class
        self._attr_unit_of_measurement = unit
        self._attr_entity_category = category

    def _channel_data(self):
        return next((c for c in self.coordinator.data.gateway if c.channel == self._channel), None)

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
        return None
