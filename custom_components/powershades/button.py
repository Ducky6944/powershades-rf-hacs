"""Button platform for PowerShades (scenes)."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
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
    """Set up buttons for PowerShades scenes."""
    coordinator: PowerShadesCoordinator = entry.runtime_data
    data = coordinator.data
    async_add_entities(PowerShadesSceneButton(coordinator, scene.id, scene.name) for scene in data.scenes)


class PowerShadesSceneButton(CoordinatorEntity[PowerShadesCoordinator], ButtonEntity):
    """A button that activates a PowerShades scene."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PowerShadesCoordinator,
        scene_id: int,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._scene_id = scene_id
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{coordinator.entry.data[CONF_EMAIL]}_scene_{scene_id}"
        self._attr_device_info = coordinator.device_info

    async def async_press(self) -> None:
        scene = next((s for s in self.coordinator.data.scenes if s.id == self._scene_id), None)
        if scene is None:
            _LOGGER.warning("Scene %s not found; skipping press", self._scene_id)
            return
        await self.coordinator.client.activate_scene(scene.name)
