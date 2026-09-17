"""Data coordinator for PowerShades (one per config entry).

Local-first: the **local RF gateway** is the live *state plane* (position,
battery, RF signal) and is re-fetched every poll. The **cloud API** is optional
*enrichment* (shade names, groups, scenes) fetched on a slow clock and cached;
a cloud outage never breaks local control.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .client import PowerShadesClient, PowerShadesError
from .const import (
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_EMAIL,
    CONF_GATEWAY,
    CONF_PASSWORD,
    DEFAULT_BASE_URL,
    DOMAIN,
    GW_VARIABLES,
)
from .types import GatewayChannel, GroupInfo, PowerShadesData, SceneInfo, ShadeInfo

_LOGGER = logging.getLogger(__name__)

PowerShadesConfigEntry = ConfigEntry["PowerShadesCoordinator"]

# Re-fetch cloud lists at most every N seconds; the gateway polls every N seconds.
CLOUD_REFRESH_SECONDS = 300.0
GATEWAY_POLL_SECONDS = 10


def _now() -> float:
    """Monotonic clock (split out so it can be patched in tests)."""
    return time.monotonic()


class PowerShadesCoordinator(DataUpdateCoordinator[PowerShadesData]):
    """Local gateway state (primary) + optional cloud names/groups/scenes."""

    config_entry: PowerShadesConfigEntry

    def __init__(self, hass: HomeAssistant, entry: PowerShadesConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"PowerShades {entry.title}",
            update_interval=timedelta(seconds=GATEWAY_POLL_SECONDS),
            config_entry=entry,
        )
        self._session = aiohttp_client.async_create_clientsession(hass)
        data = entry.data
        self._cloud_configured = bool(data.get(CONF_EMAIL) or data.get(CONF_API_KEY))
        self._client = PowerShadesClient(
            self._session,
            gateway=data.get(CONF_GATEWAY),
            email=data.get(CONF_EMAIL),
            password=data.get(CONF_PASSWORD),
            api_key=data.get(CONF_API_KEY),
            base_url=data.get(CONF_BASE_URL, DEFAULT_BASE_URL),
        )
        self._gateway_url: str | None = data.get(CONF_GATEWAY)
        # Cloud-side cache (names/groups/scenes change rarely).
        self._cloud: list | None = None
        self._cloud_at: float | None = None
        # Last good gateway read, so a gateway hiccup doesn't blank the UI.
        self._last_gateway: list[GatewayChannel] | None = None

    @property
    def client(self) -> PowerShadesClient:
        return self._client

    @property
    def entry(self) -> PowerShadesConfigEntry:
        """Alias for ``config_entry`` (entry.data holds secrets)."""
        return self.config_entry

    @property
    def cloud_configured(self) -> bool:
        """True if the user supplied cloud credentials (email/pw or api key)."""
        return self._cloud_configured

    @property
    def channel_names(self) -> dict[int, str]:
        """Optional user-supplied channel -> name overrides."""
        raw = self.config_entry.data.get("channel_names") or {}
        return {int(k): str(v) for k, v in raw.items() if str(v)}

    @property
    def device_info(self) -> dict:
        """Shared device-registry group for the account (cloud-only entities)."""
        return {
            "identifiers": {(DOMAIN, self.config_entry.entry_id)},
            "name": "PowerShades Account",
            "manufacturer": "PowerShades",
            "model": "Cloud",
        }

    def channel_device_info(self, ch: GatewayChannel) -> dict:
        """Device-registry entry for one RF gateway channel (the live plane)."""
        ident = (DOMAIN, f"{self.config_entry.entry_id}:gw:{ch.channel}")
        name = f"PowerShades Gateway Ch {ch.channel}"
        if ch.name:
            name = f"{ch.name} (gateway ch {ch.channel})"
        info: dict = {
            "identifiers": {ident},
            "name": name,
            "manufacturer": "PowerShades",
            "model": "RF Gateway",
        }
        if ch.device_id:
            info["model"] = f"RF Gateway (device {ch.device_id})"
        return info

    # -- gateway (primary plane) -------------------------------------------

    async def _fetch_gateway(self) -> list[GatewayChannel] | None:
        if not self._gateway_url:
            return None
        try:
            return await self._client.fetch_gateway(GW_VARIABLES)
        except Exception:  # gateway is best-effort: never raise here
            _LOGGER.debug("Gateway read failed", exc_info=True)
            return None

    # -- cloud (optional enrichment) ---------------------------------------

    async def _cloud_lists(self) -> tuple | None:
        """Fetch (or reuse cache of) static cloud lists; degrade gracefully."""
        now = _now()
        if self._cloud is not None and self._cloud_at is not None and (now - self._cloud_at) < CLOUD_REFRESH_SECONDS:
            return self._cloud
        if not self._cloud_configured:
            return self._cloud  # may be None -> no names
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
            _LOGGER.debug("Cloud read failed: %s", err)
            return self._cloud  # None -> no names, but local still works

    # -- update -------------------------------------------------------------

    async def _async_update_data(self) -> PowerShadesData:
        gateway = await self._fetch_gateway()
        if gateway:
            self._last_gateway = gateway
        elif self._last_gateway is not None:
            gateway = self._last_gateway
        else:
            gateway = []

        cloud = await self._cloud_lists()
        shades = groups = scenes = []
        if cloud is not None:
            shades_raw, groups_raw, scenes_raw, attrs_raw = cloud
            shades, groups, scenes = _build_cloud(shades_raw, groups_raw, scenes_raw, attrs_raw)

        return PowerShadesData(gateway=gateway, shades=shades, groups=groups, scenes=scenes)


def _attrs_by_shade(attrs_raw: list[dict]) -> dict[int, dict[str, str]]:
    by_id: dict[int, dict[str, str]] = {}
    for a in attrs_raw or []:
        try:
            sid = int(a["shade_id"])
        except (KeyError, ValueError):
            continue
        by_id.setdefault(sid, {})[a["attribute"]] = a["value"]
    return by_id


def _build_cloud(
    shades_raw: list[dict],
    groups_raw: list[dict],
    scenes_raw: list[dict],
    attrs_raw: list[dict],
) -> tuple[list[ShadeInfo], list[GroupInfo], list[SceneInfo]]:
    attrs = _attrs_by_shade(attrs_raw)
    shades = [
        ShadeInfo(
            id=int(s["id"]),
            name=s["name"],
            device_id=int(s.get("device_id", 0) or 0),
            property_id=int(s.get("property_id", 0) or 0),
            attributes=dict(attrs.get(int(s["id"]), {})),
        )
        for s in shades_raw or []
    ]
    groups = [
        GroupInfo(id=int(g["id"]), name=g["name"], shades=tuple(int(x) for x in g.get("shades", [])))
        for g in groups_raw or []
    ]
    scenes = [SceneInfo(id=int(sc["id"]), name=sc["name"]) for sc in scenes_raw or []]
    return shades, groups, scenes
