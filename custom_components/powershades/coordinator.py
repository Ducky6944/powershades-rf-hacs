"""Data coordinator for PowerShades (local RF gateway only).

Polls the local gateway every ``GATEWAY_POLL_SECONDS``. The gateway is the only
state plane — no cloud dependency. A transient failure falls back to the last
good read so the UI doesn't go blank.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .client import PowerShadesClient, PowerShadesError
from .const import (
    CONF_CHANNEL_NAMES,
    CONF_GATEWAY,
    CONF_POSITION_SOURCE,
    CONF_TRAVEL_TIME,
    CONF_TRAVEL_TIMES,
    DOMAIN,
    GW_VARIABLES,
    POSITION_SOURCE_ESTIMATE,
    POSITION_SOURCE_GATEWAY,
)
from .types import GatewayChannel, GroupInfo, PowerShadesData

_LOGGER = logging.getLogger(__name__)

PowerShadesConfigEntry = ConfigEntry["PowerShadesCoordinator"]

GATEWAY_POLL_SECONDS = 3
DEFAULT_TRAVEL_TIME = 20.0


def _to_channel(raw: dict, user_names: dict[str, str] | None) -> GatewayChannel:
    """Build a :class:`GatewayChannel`, preferring the user's name over the
    gateway's own ``chnames`` (which we don't control and may be blank)."""
    channel = int(raw.get("channel", 0))
    user_name = (user_names or {}).get(str(channel)) if channel else None
    return GatewayChannel(
        channel=channel,
        name=user_name or raw.get("name"),
        device_id=raw.get("device_id"),
        percent=raw.get("percent"),
        battery_v=raw.get("battery_v"),
        rx_db=raw.get("rx_db"),
    )


class PowerShadesCoordinator(DataUpdateCoordinator[PowerShadesData]):
    """Local gateway state (the only state plane)."""

    config_entry: PowerShadesConfigEntry

    def __init__(self, hass: HomeAssistant, entry: PowerShadesConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"PowerShades {entry.title}",
            config_entry=entry,
            update_interval=timedelta(seconds=GATEWAY_POLL_SECONDS),
        )
        self._session = aiohttp_client.async_create_clientsession(hass)
        data = entry.data
        self._client = PowerShadesClient(self._session, gateway=data.get(CONF_GATEWAY))
        self._last_gateway: list[GatewayChannel] | None = None
        # Per-channel "estimated position" — what we think the shade is at
        # after our last command; overrides only when the gateway reports.
        # 0..100, or absent = unknown.
        self._estimates: dict[int, int] = {}

    @property
    def client(self) -> PowerShadesClient:
        return self._client

    @property
    def entry(self) -> PowerShadesConfigEntry:
        """Alias for ``config_entry`` (entry.data holds secrets)."""
        return self.config_entry

    @property
    def travel_time(self) -> float:
        """Base seconds a shade takes for a full 0->100% sweep (user-tunable)."""
        return float(self.config_entry.data.get(CONF_TRAVEL_TIME) or DEFAULT_TRAVEL_TIME)

    def travel_time_for(self, channel: int) -> float:
        """Travel time for one channel — per-shade override when set, else base.

        Different shades move at different speeds (height / load differ), so
        ``set to N%`` accuracy depends on each shade's own sweep time.
        """
        overrides = self.config_entry.data.get(CONF_TRAVEL_TIMES) or {}
        try:
            val = overrides.get(str(int(channel)))
        except (TypeError, ValueError):
            val = None
        try:
            if val:
                return float(val)
        except (TypeError, ValueError):
            pass
        return self.travel_time

    def record_estimate(self, channel: int, position: int) -> None:
        """Record our best-guess position for a channel (from a command).

        Diagnostic / reference only — the gateway read is authoritative when
        present, but it is flaky, so we also surface what we *intended* last.
        """
        self._estimates[int(channel)] = max(0, min(100, int(position)))

    def estimate(self, channel: int) -> int | None:
        return self._estimates.get(int(channel))

    def resolve_position(self, channel: int) -> int | None:
        """The cover's "current position" for a channel.

        The **estimate** (what we last commanded, tracked by our own timer math)
        is the source of truth and is returned independent of what the gateway
        reports — a channel dropping out of a transient/read-missing gateway
        read must NOT blank a position we already know. ``None`` = we have no
        estimate for the channel (never commanded, or cleared by a stop).

        ``position_source = gateway`` opts in to showing the live gateway read
        *when it has one*; if that read is blank we fall back to the estimate so
        the cover never flips to "unknown" just because the gateway is quiet.
        Shared by the single-shade cover and the group cover so both agree.
        """
        est = self.estimate(channel)
        if self.position_source_for(channel) == POSITION_SOURCE_GATEWAY:
            data = self.data
            ch = data.channel(int(channel)) if data else None
            if ch is not None and ch.percent is not None:
                return int(ch.percent)
        return int(est) if est is not None else None

    def clear_estimate(self, channel: int) -> None:
        """Drop a channel's recorded position (e.g. after a stop whose position
        we can't read) so the next set_position re-calibrates from open."""
        self._estimates.pop(int(channel), None)

    @property
    def channel_names(self) -> dict[str, str]:
        """User-supplied channel -> name map (keys are strings of the channel).
        Absent / missing key = no custom name for that channel."""
        raw = self.config_entry.data.get(CONF_CHANNEL_NAMES) or {}
        out: dict[str, str] = {}
        for k, v in raw.items():
            try:
                ch = int(str(k))
            except (TypeError, ValueError):
                continue
            if ch > 0 and v:
                out[str(ch)] = str(v)
        return out

    def position_source_for(self, channel: int) -> str:
        """Position source for one channel: ``"estimate"`` or ``"gateway"``.

        Defaults to ``estimate`` (our own time-based record) because the
        gateway's live read can be stale / mid-travel. A per-shade override
        comes from ``position_source`` in the entry data.
        """
        overrides = self.config_entry.data.get(CONF_POSITION_SOURCE) or {}
        try:
            val = overrides.get(str(int(channel)))
        except (TypeError, ValueError):
            val = None
        if val in (POSITION_SOURCE_ESTIMATE, POSITION_SOURCE_GATEWAY):
            return str(val)
        return POSITION_SOURCE_ESTIMATE

    @property
    def user_groups(self) -> list[GroupInfo]:
        """User-defined local groups: ``{"<name>": [ch1, ch2, ...], ...}``."""
        raw = self.config_entry.data.get("groups") or {}
        groups: list[GroupInfo] = []
        for idx, (name, channels) in enumerate(raw.items()):
            try:
                chans = tuple(int(x) for x in channels)
            except (TypeError, ValueError):
                continue
            if chans:
                groups.append(GroupInfo(id=idx, name=str(name), shades=chans))
        return groups

    @property
    def available_metrics(self) -> set[str]:
        """Keys of diagnostic metrics the user confirmed are present.

        If the user didn't choose in setup, default to all known ones (the user
        opted in by configuring this integration).
        """
        raw = self.config_entry.data.get("available")
        if raw is None:
            return {"percent", "battery", "rx", "device_id"}
        return {str(k) for k in raw}

    def channel_device_info(self, ch: GatewayChannel) -> dict:
        """Device-registry entry for one RF gateway channel (the live plane)."""
        # Fall back to channel number if no name is set (defensive).
        name = ch.name or f"Channel {ch.channel}"
        info: dict = {
            "identifiers": {(DOMAIN, f"{self.config_entry.entry_id}:gw:{ch.channel}")},
            "name": f"PowerShades {name}",
            "manufacturer": "PowerShades",
            "model": "RF Gateway",
            "sw_version": "1.0",
        }
        if ch.device_id:
            info["model"] = f"RF Gateway (device {ch.device_id})"
        return info

    def user_group_device_info(self, group: GroupInfo) -> dict:
        """Device-registry entry for a user group (one per group, local plane)."""
        return {
            "identifiers": {(DOMAIN, f"{self.config_entry.entry_id}:usergroup:{group.id}")},
            "name": f"PowerShades {group.name}",
            "manufacturer": "PowerShades",
            "model": f"RF Group ({len(group.shades)})",
            "sw_version": "1.0",
        }

    async def _async_update_data(self) -> PowerShadesData:
        gateway = None
        try:
            gateway = await self._client.fetch_gateway(GW_VARIABLES)
        except PowerShadesError as err:
            _LOGGER.debug("Gateway read failed (will use last good): %s", err)
        if gateway:
            self._last_gateway = [_to_channel(g, self.channel_names) for g in gateway]
        elif self._last_gateway is None:
            self._last_gateway = []
        return PowerShadesData(gateway=self._last_gateway)
