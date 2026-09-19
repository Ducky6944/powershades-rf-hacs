"""Button platform for PowerShades — one "Reset (open fully)" button per shade.

Pressing it drives the channel up for a generous fixed duration so it reaches its
top end-stop, then locks the recorded position at 100%. Use it to re-sync a shade
whose *estimated* position has drifted (or after a mid-travel stop), so the
subsequent "set to N%" starts from a known reference.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ._movement import reset_open
from .const import DOMAIN, RESET_UP_SECONDS
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
    entities = [PowerShadesResetButton(coordinator, ch.channel) for ch in coordinator.data.gateway if ch.linked]
    async_add_entities(entities)


class PowerShadesResetButton(CoordinatorEntity[PowerShadesCoordinator], ButtonEntity):
    """Drive one shade fully open and lock its recorded position at 100%."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Reset (open fully)"
    _attr_icon = "mdi:arrow-up-bold-circle-outline"

    def __init__(self, coordinator: PowerShadesCoordinator, channel: int) -> None:
        super().__init__(coordinator)
        self._channel = channel
        self._attr_unique_id = f"{DOMAIN}_{coordinator.config_entry.entry_id}_ch{channel}_reset"
        ch = coordinator.data.channel(channel) or GatewayChannel(channel=channel)
        self._attr_device_info = coordinator.channel_device_info(ch)

    @property
    def available(self) -> bool:
        ch = self.coordinator.data.channel(self._channel)
        return ch is not None and ch.linked

    async def async_press(self) -> None:
        try:
            await reset_open(self.coordinator.client, self._channel, RESET_UP_SECONDS)
        except Exception as err:
            _LOGGER.warning("reset ch%s failed: %s", self._channel, err)
            return
        # After a full up, the shade is (by construction) at 100% — lock it there.
        self.coordinator.record_estimate(self._channel, 100)
