"""Async client for PowerShades (local RF gateway only).

The client wraps a caller-owned ``aiohttp.ClientSession`` and holds no long-
lived state of its own. All I/O is over HTTP to the local RF gateway:
- read per-channel state (percent, battery, rx, rfdevs, chnames)
- send up / down / stop to a channel
"""

from __future__ import annotations

import json
from typing import Any

import aiohttp

from .const import GW_AJAX_PATH, GW_CMD_QUERY

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=5)


class PowerShadesError(Exception):
    """Base error for the PowerShades client."""


class PowerShadesUnavailable(PowerShadesError):
    """The gateway cannot be reached or returned a non-2xx response."""


class PowerShadesClient:
    """Local RF gateway client.

    Transport-agnostic: wraps a caller-owned ``aiohttp.ClientSession``. All
    methods are async; any network failure raises :class:`PowerShadesUnavailable`
    or :class:`PowerShadesError` (not swallowed here).
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        gateway: str | None = None,
    ) -> None:
        self._session = session
        self._gateway = (gateway or "").rstrip("/") or None

    @property
    def gateway_configured(self) -> bool:
        return self._gateway is not None

    # -- low level ---------------------------------------------------------

    async def _get(self, url: str) -> str:
        try:
            async with self._session.get(url, timeout=DEFAULT_TIMEOUT) as resp:
                if not (200 <= resp.status < 300):
                    raise PowerShadesUnavailable(f"HTTP {resp.status} for {url}")
                return await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise PowerShadesUnavailable(str(err)) from err

    # -- gateway read ------------------------------------------------------

    async def fetch_gateway(self, variables: tuple[str, ...]) -> list[dict[str, Any]] | None:
        """Read the gateway's per-channel state; return ``None`` if unreachable."""
        if not self._gateway:
            return None
        url = f"{self._gateway}{GW_AJAX_PATH}?var=" + ",".join(variables)
        try:
            text = await self._get(url)
            return self._parse_gateway(json.loads(text))
        except (aiohttp.ClientError, ValueError, TimeoutError) as err:
            raise PowerShadesUnavailable(str(err)) from err

    # -- gateway control ---------------------------------------------------

    def _cmd_url(self, param: str, channel: int) -> str:
        if not self._gateway:
            raise PowerShadesUnavailable("No gateway configured")
        return f"{self._gateway}/{GW_CMD_QUERY}?{param}={int(channel)}"

    async def _gateway_cmd(self, param: str, channel: int) -> None:
        await self._get(self._cmd_url(param, channel))

    async def gateway_up(self, channel: int) -> None:
        from .const import GW_CMD_UP

        await self._gateway_cmd(GW_CMD_UP, channel)

    async def gateway_down(self, channel: int) -> None:
        from .const import GW_CMD_DOWN

        await self._gateway_cmd(GW_CMD_DOWN, channel)

    async def gateway_stop(self, channel: int) -> None:
        from .const import GW_CMD_STOP

        await self._gateway_cmd(GW_CMD_STOP, channel)


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


def _parse_gateway(payload: object) -> list[dict[str, Any]] | None:
    """Turn a raw ``ajax.shtml`` response into a list of per-channel dicts.

    The gateway returns a JSON **array** whose elements align with the order of
    the requested ``var`` names: ``percent, battery, rx, rfdevs, chnames1-3``.
    Returns ``None`` if the shape is unexpected.
    """
    if not isinstance(payload, list) or len(payload) < 4:
        return None
    percent = _cells(payload[0]) if isinstance(payload[0], str) else []
    battery = _cells(payload[1]) if isinstance(payload[1], str) else []
    rx = _cells(payload[2]) if isinstance(payload[2], str) else []
    rfdevs = _cells(payload[3]) if isinstance(payload[3], str) else []
    chnames = _flatten_names(payload[4:])
    count = max(len(percent), 30)
    out: list[dict[str, Any]] = []
    for ch in range(1, count + 1):
        i = ch - 1
        out.append(
            {
                "channel": ch,
                "name": (chnames[i] if i < len(chnames) and chnames[i] else None),
                "device_id": _optdev(rfdevs, i),
                "percent": _opt(percent, i),
                "battery_v": _optb(battery, i),
                "rx_db": _opt(rx, i),
            }
        )
    return out
