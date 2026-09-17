"""Cover platform for PowerShades (shades + groups)."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
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
    """Set up cover entities for shades and groups."""
    coordinator: PowerShadesCoordinator = entry.runtime_data
    data = coordinator.data

    entities: list[PowerShadesCover] = []
    for shade in data.shades:
        entities.append(PowerShadesCover(coordinator, "shade", shade.id, shade.name))
    for group in data.groups:
        entities.append(PowerShadesCover(coordinator, "group", group.id, group.name))

    async_add_entities(entities)


class PowerShadesCover(CoordinatorEntity[PowerShadesCoordinator], CoverEntity):
    """A single PowerShades shade or group."""

    _attr_has_entity_name = True
    _attr_device_class = CoverDeviceClass.SHADE
    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.SET_POSITION

    def __init__(
        self,
        coordinator: PowerShadesCoordinator,
        kind: str,
        target_id: int,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._kind = kind
        self._target_id = target_id
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{coordinator.entry.data[CONF_EMAIL]}_{kind}_{target_id}"
        self._attr_device_info = coordinator.device_info
        self._attr_extra_state_attributes = {}

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self._kind == "shade":
            shade = next((s for s in self.coordinator.data.shades if s.id == self._target_id), None)
            if shade:
                attrs: dict[str, Any] = {}
                for attr, value in shade.attributes.items():
                    key = attr.replace(" ", "_").lower()
                    attrs[f"shade_{key}"] = value
                return attrs
        return {}

    @property
    def current_cover_position(self) -> int | None:
        # We don't track live position from the cloud API alone.
        # Position is returned from the local gateway (if available).
        # Without that, we report None (= unknown).
        return None

    @property
    def is_closed(self) -> bool | None:
        return None

    @property
    def is_opening(self) -> bool:
        return False

    @property
    def is_closing(self) -> bool:
        return False

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._move(0)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._move(100)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        await self._move(kwargs[ATTR_POSITION])

    async def _move(self, percentage: int) -> None:
        if self._kind == "shade":
            shade = next((s for s in self.coordinator.data.shades if s.id == self._target_id), None)
            if shade is None:
                _LOGGER.warning("Shade %s not found in coordinator data", self._target_id)
                return
            await self.coordinator.client.move_shade(shade.name, 100 - percentage)
        else:
            group = next((g for g in self.coordinator.data.groups if g.id == self._target_id), None)
            if group is None:
                _LOGGER.warning("Group %s not found in coordinator data", self._target_id)
                return
            await self.coordinator.client.move_group(group.name, 100 - percentage)
        # Trigger a coordinator refresh so UI updates
        await self.coordinator.async_request_refresh()
