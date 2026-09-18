"""Sensor platform for PowerShades — per-channel diagnostics.

Every sensor here is tagged ``DIAGNOSTIC`` because these are *read-back /
reference* values, not the thing you control. Two categories:

* **Gateway truth** — what the gateway actually reports (percent, battery, rx,
  device_id). Only created if the user's gateway has that metric.
* **Our estimate** — (estimated position) what we *intended* after the last
  command, useful when the gateway's own read is flaky or absent.

A metric sensor is only created if the corresponding key is in the user's
``available`` list (populated during setup based on what the first gateway read
returned, or that the user chose).
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
ESTIMATE_KEY = "estimated"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create per-channel diagnostics for each linked channel, gated by availability."""
    coordinator: PowerShadesCoordinator = entry.runtime_data
    linked = [ch for ch in coordinator.data.gateway if ch.linked]
    if not linked:
        return

    available = coordinator.available_metrics
    entities: list[SensorEntity] = []
    for ch in linked:
        for key, unit in SENSORS:
            if key in available:
                entities.append(_ChannelSensor(coordinator, ch.channel, key, unit))
        # Estimated position is always available (it's derived from our own commands).
        entities.append(_EstimateSensor(coordinator, ch.channel))
    async_add_entities(entities)


class _ChannelSensor(CoordinatorEntity[PowerShadesCoordinator], SensorEntity):
    """A diagnostic reading straight from the gateway (flaky, best-effort)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: PowerShadesCoordinator, channel: int, key: str, unit: str | None) -> None:
        super().__init__(coordinator)
        self._channel = channel
        self._key = key
        human = key.replace("_", " ")
        self._attr_name = human.capitalize()
        self._attr_unique_id = f"{DOMAIN}_{coordinator.config_entry.entry_id}_gw_ch{channel}_{key}"
        ch = coordinator.data.channel(channel) or GatewayChannel(channel=channel)
        self._attr_device_info = coordinator.channel_device_info(ch)
        if unit:
            self._attr_unit_of_measurement = unit

    @property
    def native_value(self):
        ch = self.coordinator.data.channel(self._channel)
        if ch is None:
            return None
        return {
            "percent": ch.percent,
            "battery": ch.battery_v,
            "rx": ch.rx_db,
            "device_id": ch.device_id,
        }.get(self._key)


class _EstimateSensor(CoordinatorEntity[PowerShadesCoordinator], SensorEntity):
    """A diagnostic sensor for the position we *intended* after our last command."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Estimated position"
    _attr_native_unit_of_measurement = "%"

    def __init__(self, coordinator: PowerShadesCoordinator, channel: int) -> None:
        super().__init__(coordinator)
        self._channel = channel
        self._attr_unique_id = f"{DOMAIN}_{coordinator.config_entry.entry_id}_gw_ch{channel}_{ESTIMATE_KEY}"
        ch = coordinator.data.channel(channel) or GatewayChannel(channel=channel)
        self._attr_device_info = coordinator.channel_device_info(ch)

    @property
    def native_value(self):
        return self.coordinator.estimate(self._channel)
