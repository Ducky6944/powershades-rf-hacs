"""Cover platform for PowerShades (local RF gateway only).

Two kinds of covers:

* **Channel covers** — one per linked RF channel. Open / close / stop send the
  gateway's up / down / stop. **Set to N%** is emulated via the timed routine:
  up (full travel) → down (``(100-N) * travel / 100`` s) → stop, which lands on
  N% regardless of where the shade started.
* **User-group covers** — one per user-defined group. Open / close / stop /
  **set-to-N%** all fan out to every member channel, one at a time (the gateway
  has a single RF transmitter, so back-to-back commands to different channels
  are serialized to avoid frame collisions). The group position is shown only
  when all members agree; a mixed group reports **no** position (slider
  "unknown") and flags ``position_mixed`` in its extra attributes.

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
        # State must track the estimate (what we last commanded), not the flaky
        # live gateway read — otherwise a "set to N%" shows the right slider but
        # the state still reads Unknown when the gateway isn't reporting.
        pos = self.coordinator.resolve_position(self._channel)
        if pos is None:
            return None
        return pos == 0

    @property
    def is_open(self) -> bool | None:
        pos = self.coordinator.resolve_position(self._channel)
        if pos is None:
            return None
        return pos > 0

    @property
    def is_opening(self) -> bool:
        return False

    @property
    def is_closing(self) -> bool:
        return False

    @property
    def current_cover_position(self) -> int | None:
        return self.coordinator.resolve_position(self._channel)

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
        # Stopping mid-travel leaves the shade somewhere we don't time-tracked.
        # Best-effort: if the gateway has a live reading, re-anchor to it.
        ch = self._ch()
        if ch is not None and ch.percent is not None:
            self.coordinator.record_estimate(self._channel, int(ch.percent))
        else:
            self.coordinator.clear_estimate(self._channel)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        target = int(kwargs[ATTR_POSITION])
        # Read our last known position *before* we update it, so we can move the
        # shortest way to the target (no full-travel calibration leg). None =
        # unknown → set_position falls back to calibrating from fully open.
        from_pos = self.coordinator.estimate(self._channel)
        # Record the target up front (optimistic) so the UI shows it for the
        # whole move — consistent with open/close, which also set instantly.
        self.coordinator.record_estimate(self._channel, target)
        travel = self.coordinator.travel_time_for(self._channel)
        await move_to_position(self.coordinator.client, self._channel, target, travel, from_pos)


class PowerShadesLocalGroupCover(_AssumedStateCover):
    """A user-defined group of local channels.

    Open / close / stop / **set-to-N%** all fan out to every member channel,
    one at a time (serialized to avoid RF frame collisions on the shared
    transmitter), each using its own travel time and its own last position. The group
    position is only shown when *all* members agree; when they are spread out the
    cover reports **no** position (the HA slider then shows "unknown") and sets
    ``position_mixed`` (with ``position_min`` / ``position_max``) in the extra
    attributes — this is the "Mixed / do nothing" behavior.
    """

    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP | CoverEntityFeature.SET_POSITION
    )

    def __init__(self, coordinator: PowerShadesCoordinator, group: GroupInfo) -> None:
        super().__init__(coordinator)
        self._name = group.name
        self._channels = list(group.shades)
        self._attr_unique_id = f"{DOMAIN}_{coordinator.config_entry.entry_id}_usergroup_{group.id}"
        self._attr_device_info = coordinator.user_group_device_info(group)

    @property
    def name(self) -> str | None:
        return self._name

    def _common_position(self) -> int | None:
        """The group's single position: the shared value when *every* member is
        known and agrees, else ``None`` (unknown — mixed or a member unreported).

        ``current_cover_position``, ``is_open`` and ``is_closed`` all derive from
        this, so the slider and the state always tell the same story.
        """
        resolved = [self.coordinator.resolve_position(ch) for ch in self._channels]
        if any(p is None for p in resolved):
            return None
        return resolved[0] if len(set(resolved)) == 1 else None

    @property
    def current_cover_position(self) -> int | None:
        """Shared position when all members agree, else None (slider "unknown")."""
        return self._common_position()

    @property
    def is_closed(self) -> bool | None:
        p = self._common_position()
        if p is None:
            return None  # mixed / unknown → state "unknown"
        return p == 0

    @property
    def is_open(self) -> bool | None:
        p = self._common_position()
        if p is None:
            return None
        return p > 0

    def _positions(self) -> list[int]:
        """Resolved positions of known members (for min/max / mixed detail)."""
        return [p for p in (self.coordinator.resolve_position(ch) for ch in self._channels) if p is not None]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        positions = self._positions()
        if not positions:
            return {}
        return {
            "position_mixed": len(set(positions)) > 1,
            "position_min": min(positions),
            "position_max": max(positions),
            "members": len(self._channels),
        }

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
        # All members target fully open; record up front (optimistic) like a single shade.
        for channel in self._channels:
            self.coordinator.record_estimate(channel, 100)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._fan("down")
        for channel in self._channels:
            self.coordinator.record_estimate(channel, 0)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self._fan("stop")
        # Re-anchor each member to a live gateway read if we have one, else drop
        # the estimate so its next set_position re-calibrates — mirroring a single shade.
        data = self.coordinator.data
        for channel in self._channels:
            ch = data.channel(channel) if data else None
            if ch is not None and ch.percent is not None:
                self.coordinator.record_estimate(channel, int(ch.percent))
            else:
                self.coordinator.clear_estimate(channel)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        target = int(kwargs[ATTR_POSITION])

        # Move every member to the target, each taking the shortest way from its
        # own last position. Run members ONE AT A TIME (serialized): the gateway
        # has a single RF transmitter, so parallel members' up/down/stop frames can
        # collide and a shade may miss its stop and run to its own end-stop (e.g.
        # 0%) instead of the target. Total time is now the sum of the members'
        # (shortest-path) travels, which is fine for reliability.
        for channel in self._channels:
            from_pos = self.coordinator.estimate(channel)
            self.coordinator.record_estimate(channel, target)
            travel = self.coordinator.travel_time_for(channel)
            try:
                await move_to_position(self.coordinator.client, channel, target, travel, from_pos)
            except Exception as err:
                _LOGGER.warning("Group '%s' set %d%% on ch%s failed: %s", self._name, target, channel, err)
