"""Data coordinator for the PowerShades integration (one per account)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import PowerShadesClient, PowerShadesError, PowerShadesUnavailable
from .const import CONF_BASE_URL, CONF_EMAIL, CONF_GATEWAY, CONF_PASSWORD, DEFAULT_BASE_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)

PowerShadesConfigEntry = ConfigEntry["PowerShadesCoordinator"]


def _now() -> float:
    """Monotonic clock, split out so it can be patched in tests."""
    return time.monotonic()


@dataclass(frozen=True)
class ShadeInfo:
    """A shade plus its static metadata."""

    id: int
    name: str
    device_id: int
    property_id: int
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class GroupInfo:
    id: int
    name: str
    shades: tuple[int, ...]


@dataclass(frozen=True)
class SceneInfo:
    id: int
    name: str


@dataclass(frozen=True)
class GatewayChannel:
    """One local RF gateway channel."""

    channel: int
    percent: int | None = None  # 0..100; None = not reporting
    battery_v: float | None = None
    rx_db: int | None = None
    device_id: str | None = None  # RF device id, may be hex (e.g. "aabbccdd")
    name: str | None = None


@dataclass(frozen=True)
class PowerShadesData:
    """Snapshot of the whole PowerShades account."""

    shades: list[ShadeInfo]
    groups: list[GroupInfo]
    scenes: list[SceneInfo]
    gateway: list[GatewayChannel] = field(default_factory=list)


class PowerShadesCoordinator(DataUpdateCoordinator[PowerShadesData]):
    """Fetch shades, groups, scenes, and (optionally) local gateway state."""

    config_entry: PowerShadesConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: PowerShadesConfigEntry,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"PowerShades {entry.data[CONF_EMAIL]}",
            update_interval=timedelta(seconds=10),
            config_entry=entry,
        )
        self._session = aiohttp_client.async_create_clientsession(hass)
        self._client = PowerShadesClient(
            self._session,
            entry.data[CONF_EMAIL],
            entry.data[CONF_PASSWORD],
            entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL),
        )
        self._gateway_url: str | None = entry.data.get(CONF_GATEWAY)
        # Cloud lists (shades/groups/scenes/attrs) change rarely, so we cache
        # them and only re-fetch on a slow clock. The local gateway is the live
        # state plane and is re-fetched every poll.
        self._cloud: list | None = None
        self._cloud_at: float | None = None
        self._CLOUD_REFRESH_SECONDS = 300.0  # re-fetch cloud lists at most every 5 min

    @property
    def client(self) -> PowerShadesClient:
        """The underlying cloud API client."""
        return self._client

    @property
    def entry(self) -> PowerShadesConfigEntry:
        """The owning config entry (alias for ``config_entry``)."""
        return self.config_entry

    @property
    def device_info(self) -> dict:
        """Shared device registry info for the account."""
        return {
            "identifiers": {(DOMAIN, self.config_entry.entry_id)},
            "name": "PowerShades",
            "manufacturer": "PowerShades",
            "model": "Cloud",
        }

    async def _fetch_gateway(self) -> list[GatewayChannel]:
        from .const import GW_AJAX_PATH, GW_VARIABLES

        url = f"{self._gateway_url}{GW_AJAX_PATH}?var=" + ",".join(GW_VARIABLES)
        try:
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                text = await resp.text()
                payload = json.loads(text)
        except (aiohttp.ClientError, ValueError, TimeoutError) as err:
            _LOGGER.debug("Gateway read failed: %s", err)
            return []
        if not isinstance(payload, list) or len(payload) < 4:
            return []
        try:
            percent = _cells(payload[0]) if isinstance(payload[0], str) else []
            battery = _cells(payload[1]) if isinstance(payload[1], str) else []
            rx = _cells(payload[2]) if isinstance(payload[2], str) else []
            rfdevs = _cells(payload[3]) if isinstance(payload[3], str) else []
            chnames = _flatten_names(payload[4:])
        except (IndexError, AttributeError):
            return []
        channels: list[GatewayChannel] = []
        count = max(len(percent), 30)
        for ch in range(1, count + 1):
            i = ch - 1
            channels.append(
                GatewayChannel(
                    channel=ch,
                    percent=_opt(percent, i),
                    battery_v=_optb(battery, i),
                    rx_db=_opt(rx, i),
                    device_id=_optdev(rfdevs, i),
                    name=(chnames[i] if i < len(chnames) and chnames[i] else None),
                )
            )
        return channels

    async def _cloud_lists(self) -> tuple:
        """Fetch (or return cached) static cloud lists.

        The cloud is hit at most every ``_CLOUD_REFRESH_SECONDS``; on a
        transient cloud error we fall back to the last good cache rather than
        failing the whole update.
        """
        now = _now()
        if (
            self._cloud is not None
            and self._cloud_at is not None
            and (now - self._cloud_at) < self._CLOUD_REFRESH_SECONDS
        ):
            return self._cloud
        try:
            cloud = await asyncio.gather(
                self._client.fetch_shades(),
                self._client.fetch_groups(),
                self._client.fetch_scenes(),
                self._client.fetch_shade_attributes(),
            )
            self._cloud = cloud
            self._cloud_at = now
            return cloud
        except (PowerShadesError, TimeoutError) as err:
            if self._cloud is not None:
                _LOGGER.debug("Cloud refresh failed; using last good cache: %s", err)
                return self._cloud
            raise

    async def _async_update_data(self) -> PowerShadesData:
        try:
            shades_raw, groups_raw, scenes_raw, attrs_raw = await self._cloud_lists()
            gateway = await self._optional_gateway()
        except PowerShadesUnavailable as err:
            raise UpdateFailed(f"Unable to reach PowerShades API: {err}") from err
        except PowerShadesError as err:
            raise UpdateFailed(f"PowerShades API error: {err}") from err
        return _build_data(shades_raw, groups_raw, scenes_raw, attrs_raw, gateway)

    async def _optional_gateway(self) -> list[GatewayChannel]:
        if not self._gateway_url:
            return []
        try:
            return await self._fetch_gateway()
        except Exception:  # gateway is optional; never fail the update
            _LOGGER.debug("Gateway read failed; continuing without it", exc_info=True)
            return []


def _cells(value: object) -> list[str]:
    if not isinstance(value, str):
        return []
    return value.split(":")


def _flatten_names(rows: object) -> list[str]:
    if not isinstance(rows, list):
        return []
    out: list[str] = []
    for row in rows:
        if isinstance(row, str):
            out.extend(row.split(":"))
        elif isinstance(row, list):
            out.extend(str(x) for x in row)
    return out


def _opt(values: list[str], i: int) -> int | None:
    if i < len(values):
        try:
            n = int(values[i])
        except ValueError:
            return None
        return None if n < 0 else n
    return None


def _optb(values: list[str], i: int) -> float | None:
    if i < len(values):
        try:
            mv = int(values[i])
        except ValueError:
            return None
        if mv > 0:
            return round(mv / 1000.0, 2)
    return None


def _optdev(values: list[str], i: int) -> str | None:
    if i < len(values):
        v = values[i].strip()
        if v and v != "0":
            return v
    return None


def _build_data(
    shades_raw: list[dict],
    groups_raw: list[dict],
    scenes_raw: list[dict],
    attrs_raw: list[dict],
    gateway: list[GatewayChannel],
) -> PowerShadesData:
    attrs_by_shade: dict[int, dict[str, str]] = {}
    for a in attrs_raw or []:
        attrs_by_shade.setdefault(int(a["shade_id"]), {})[a["attribute"]] = a["value"]

    shades = [
        ShadeInfo(
            id=int(s["id"]),
            name=s["name"],
            device_id=int(s.get("device_id", 0)),
            property_id=int(s.get("property_id", 0)),
            attributes=dict(attrs_by_shade.get(int(s["id"]), {})),
        )
        for s in shades_raw or []
    ]
    groups = [
        GroupInfo(id=int(g["id"]), name=g["name"], shades=tuple(int(x) for x in g.get("shades", [])))
        for g in groups_raw or []
    ]
    scenes = [SceneInfo(id=int(sc["id"]), name=sc["name"]) for sc in scenes_raw or []]
    return PowerShadesData(shades=shades, groups=groups, scenes=scenes, gateway=gateway or [])
