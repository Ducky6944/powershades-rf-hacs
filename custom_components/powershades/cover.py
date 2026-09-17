"""Cover platform for PowerShades.

Local-first: one **cover per live RF gateway channel** — the primary control +
state plane (up/down/stop, live position/battery via sensors). When the user
supplies cloud credentials *and* names are known, **group** covers are added so
absolute-position moves (which only exist on the cloud) are available for groups.
"""

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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PowerShadesCoordinator
from .types import GatewayChannel, GroupInfo

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up per-channel covers (primary) and optional cloud group covers."""
    coordinator: PowerShadesCoordinator = entry.runtime_data
    data = coordinator.data

    entities: list[CoverEntity] = []
    for ch in data.gateway:
        if ch.linked:
            entities.append(PowerShadesChannelCover(coordinator, ch))
    if coordinator.cloud_configured:
        for group in data.groups:
            entities.append(PowerShadesGroupCover(coordinator, group))

    async_add_entities(entities)


class PowerShadesChannelCover(CoordinatorEntity[PowerShadesCoordinator], CoverEntity):
    """A single shade exposed through its RF gateway channel."""

    _attr_has_entity_name = True
    _attr_device_class = CoverDeviceClass.SHADE
    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP

    def __init__(self, coordinator: PowerShadesCoordinator, ch: GatewayChannel) -> None:
        super().__init__(coordinator)
        self._channel = ch.channel
        self._attr_unique_id = f"{DOMAIN}_{coordinator.config_entry.entry_id}_ch{ch.channel}"
        # Device registry: one device per RF channel (the live plane).
        self._attr_device_info = coordinator.channel_device_info(ch)

    # -- name (resolved local->cloud->manual->fallback) --------------------

    def _ch(self) -> GatewayChannel | None:
        return self.coordinator.data.channel(self._channel)

    def _resolved_name(self) -> str:
        ch = self._ch()
        base = ch.name if ch else None
        if not base:
            base = self.coordinator.channel_names.get(self._channel)
        if not base:
            base = f"Channel {self._channel}"
        return base

    @property
    def name(self) -> str | None:
        return self._resolved_name()

    # -- state (from the live gateway plane) --------------------------------
    # The base CoverEntity.state resolves from is_opening/is_closing/is_closed,
    # so provide those (current_cover_position is an optional extra alongside).

    @property
    def is_closed(self) -> bool | None:
        ch = self._ch()
        if ch is None or ch.percent is None:
            return None
        # gateway/cloud percent: 0 = fully open, 100 = fully closed.
        return ch.percent >= 99

    @property
    def is_open(self) -> bool | None:
        closed = self.is_closed
        return None if closed is None else not closed

    @property
    def is_opening(self) -> bool:
        return False

    @property
    def is_closing(self) -> bool:
        return False

    @property
    def current_cover_position(self) -> int | None:
        ch = self._ch()
        if ch is None or ch.percent is None:
            return None
        # gateway percent: 0=open, 100=closed. HA position: 0=closed, 100=open.
        return 100 - ch.percent

    @property
    def available(self) -> bool:
        ch = self._ch()
        return ch is not None and ch.linked

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        ch = self._ch()
        if ch is None:
            return {}
        attrs: dict[str, Any] = {}
        if ch.device_id:
            attrs["rf_device_id"] = ch.device_id
        return attrs

    # -- commands (local gateway up/down/stop only) -------------------------

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._gateway("up")

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._gateway("down")

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self._gateway("stop")

    async def _gateway(self, cmd: str) -> None:
        """Send up/down/stop over the local gateway (best-effort)."""
        try:
            if cmd == "up":
                await self.coordinator.client.gateway_up(self._channel)
            elif cmd == "down":
                await self.coordinator.client.gateway_down(self._channel)
            else:
                await self.coordinator.client.gateway_stop(self._channel)
        except Exception as err:  # don't let a control error kill the refresh
            _LOGGER.warning("Gateway command '%s' ch%s failed: %s", cmd, self._channel, err)


class PowerShadesGroupCover(CoordinatorEntity[PowerShadesCoordinator], CoverEntity):
    """A cloud group (absolute position via the cloud API)."""

    _attr_has_entity_name = True
    _attr_device_class = CoverDeviceClass.SHADE
    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.SET_POSITION

    def __init__(self, coordinator: PowerShadesCoordinator, group: GroupInfo) -> None:
        super().__init__(coordinator)
        self._group_id = group.id
        self._attr_unique_id = f"{DOMAIN}_{coordinator.config_entry.entry_id}_group_{group.id}"
        self._attr_device_info = coordinator.device_info

    def _group(self) -> GroupInfo | None:
        return next((g for g in self.coordinator.data.groups if g.id == self._group_id), None)

    @property
    def name(self) -> str | None:
        g = self._group()
        return g.name if g else None

    @property
    def current_cover_position(self) -> int | None:
        # Groups have no live position plane on the gateway; report unknown.
        return None

    @property
    def is_closed(self) -> bool | None:
        return None

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._move(0)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._move(100)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        await self._move(kwargs[ATTR_POSITION])

    async def _move(self, ha_position: int) -> None:
        name = self.name
        if not name:
            raise HomeAssistantError("Group name unavailable")
        await self.coordinator.client.move_group(name, 100 - ha_position)
        await self.coordinator.async_request_refresh()
