"""Cover platform for PowerShades (local RF gateway only).

Two kinds of covers:

* **Channel covers** — one per linked RF channel. Open / close / stop send the
  gateway's up / down / stop. **Set to N%** is emulated via the timed routine:
  up (full travel) → down (``(100-N) * travel / 100`` s) → stop, which lands on
  N% regardless of where the shade started.
* **User-group covers** — one per user-defined group. Open / close / stop fan
  out to every member channel. No set_position on groups (members can drift,
  so a single target on the group is ambiguous; use the channel covers for that).

All covers set ``assumed_state=True`` so the UI keeps up / down / stop enabled
regardless of what the (flaky) gateway reports for position.
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
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ._movement import close_channel, open_channel, set_position as move_to_position
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
    coordinator: PowerShadesCoordinator = entry.runtime_data
    data = coordinator.data

    entities: list[CoverEntity] = []
    for ch in data.gateway:
        if ch.linked:
            entities.append(PowerShadesChannelCover(coordinator, ch))
    for group in coordinator.user_groups:
        entities.append(PowerShadesLocalGroupCover(coordinator, group))
    async_add_entities(entities)


class _AssumedStateCover(CoordinatorEntity[PowerShadesCoordinator], CoverEntity):
    """Cover whose up / down / stop are always enabled.

    The gateway's position read-back is best-effort and can be stale, edge
    (0 / 100), or absent. The HA cover UI disables *Open* when it sees
    ``state == "open"`` and *Close* when it sees ``state == "closed"``; setting
    ``assumed_state`` keeps both buttons (and Stop) enabled no matter what the
    position reads — which is what a shade control wants.
    """

    _attr_has_entity_name = True
    _attr_device_class = CoverDeviceClass.SHADE
    _attr_assumed_state = True


class PowerShadesChannelCover(_AssumedStateCover):
    """A single shade / channel exposed via the local gateway."""

    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP | CoverEntityFeature.SET_POSITION
    )

    def __init__(self, coordinator: PowerShadesCoordinator, ch: GatewayChannel) -> None:
        super().__init__(coordinator)
        self._channel = ch.channel
        self._attr_unique_id = f"{DOMAIN}_{coordinator.config_entry.entry_id}_ch{ch.channel}"
        self._attr_device_info = coordinator.channel_device_info(ch)

    def _ch(self) -> GatewayChannel | None:
        return self.coordinator.data.channel(self._channel)

    @property
    def name(self) -> str | None:
        ch = self._ch()
        if ch is not None and ch.name:
            return ch.name
        return f"Channel {self._channel}"

    @property
    def is_closed(self) -> bool | None:
        ch = self._ch()
        if ch is None or ch.percent is None:
            return None
        return ch.percent < 2

    @property
    def is_open(self) -> bool | None:
        ch = self._ch()
        if ch is None or ch.percent is None:
            return None
        return ch.percent > 98

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
        return int(ch.percent)

    @property
    def available(self) -> bool:
        ch = self._ch()
        return ch is not None and ch.linked

    async def async_open_cover(self, **kwargs: Any) -> None:
        await open_channel(self.coordinator.client, self._channel)
        self.coordinator.record_estimate(self._channel, 100)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await close_channel(self.coordinator.client, self._channel)
        self.coordinator.record_estimate(self._channel, 0)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self.coordinator.client.gateway_stop(self._channel)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        target = int(kwargs[ATTR_POSITION])
        await move_to_position(self.coordinator.client, self._channel, target, self.coordinator.travel_time)
        self.coordinator.record_estimate(self._channel, target)


class PowerShadesLocalGroupCover(_AssumedStateCover):
    """A user-defined group of local channels; open/close/stop fan out to each."""

    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP

    def __init__(self, coordinator: PowerShadesCoordinator, group: GroupInfo) -> None:
        super().__init__(coordinator)
        self._name = group.name
        self._channels = list(group.shades)
        self._attr_unique_id = f"{DOMAIN}_{coordinator.config_entry.entry_id}_usergroup_{group.id}"
        self._attr_device_info = coordinator.user_group_device_info(group)

    @property
    def name(self) -> str | None:
        return self._name

    @property
    def current_cover_position(self) -> int | None:
        return None

    @property
    def is_closed(self) -> bool | None:
        return None

    @property
    def is_open(self) -> bool | None:
        return None

    async def _fan(self, kind: str) -> None:
        client = self.coordinator.client
        for channel in self._channels:
            try:
                if kind == "up":
                    await client.gateway_up(channel)
                elif kind == "down":
                    await client.gateway_down(channel)
                else:
                    await client.gateway_stop(channel)
            except Exception as err:
                _LOGGER.warning("Group '%s' %s on ch%s failed: %s", self._name, kind, channel, err)

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._fan("up")

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._fan("down")

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self._fan("stop")
