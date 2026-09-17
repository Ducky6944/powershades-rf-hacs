"""Async client for PowerShades.

Local-first: the **local RF gateway** is the primary state + control plane
(position read-back, up/down/stop). The **cloud API** is an optional, secure
fallback used to *resolve shade names* and to drive absolute-position moves,
and is only used when the user supplies credentials (e-mail/password) or an
account API key.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp

from .const import (
    AUTH_JWT,
    AUTH_JWT_REFRESH,
    DEFAULT_BASE_URL,
    GROUPS,
    GROUPS_MOVE,
    SCENES,
    SCENES_MOVE,
    SHADE_ATTRIBUTES,
    SHADES,
    SHADES_MOVE,
)
from .types import GatewayChannel


class PowerShadesError(Exception):
    """Base error for the PowerShades client."""


class PowerShadesAuthError(PowerShadesError):
    """Cloud credentials are invalid or the session cannot be refreshed."""


class PowerShadesUnavailable(PowerShadesError):
    """The remote (or gateway) cannot be reached."""


class PowerShadesClient:
    """Local gateway + optional cloud API client.

    The client is intentionally transport-agnostic: it wraps a caller-owned
    ``aiohttp.ClientSession`` and holds no long-lived state of its own.

    Cloud auth uses **one of** two modes, chosen by the caller:

    * ``email`` + ``password``  -> JWT (login, refresh on 401, re-login)
    * ``api_key``               -> plain ``Authorization: Bearer <key>`` (no refresh)

    A config entry should set *either* the email/password pair *or* the api key,
    not both — presence of ``api_key`` takes precedence.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        gateway: str | None = None,
        email: str | None = None,
        password: str | None = None,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        self._session = session
        # Local gateway (primary plane).
        self._gateway = (gateway or "").rstrip("/") or None
        # Cloud (optional enrichment).
        self._email = email
        self._password = password
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")

        if bool(api_key) == bool(email and password):
            raise ValueError("Provide either an api_key OR email+password, not both")

        if api_key:
            self._mode = "api_key"
            self._api_key = api_key
            self._access: str | None = api_key
            self._refresh: str | None = None
        else:
            self._mode = "email"
            self._api_key = None
            self._access = None
            self._refresh = None
        self._auth_lock = asyncio.Lock()

    # -- properties ---------------------------------------------------------

    @property
    def cloud_configured(self) -> bool:
        """True when cloud credentials were provided (email/pw or api key)."""
        return True  # always; the mode flag below tells which

    @property
    def gateway_configured(self) -> bool:
        return self._gateway is not None

    # -- auth (email/password only) ----------------------------------------

    async def _login(self) -> None:
        resp = await self._post_json(AUTH_JWT, {"email": self._email, "password": self._password})
        self._access = resp["access"]
        self._refresh = resp.get("refresh")

    async def _refresh_token(self) -> None:
        if not self._refresh:
            await self._login()
            return
        resp = await self._post_json(AUTH_JWT_REFRESH, {"refresh": self._refresh})
        self._access = resp["access"]
        if resp.get("refresh"):
            self._refresh = resp["refresh"]

    async def ensure_authenticated(self) -> None:
        async with self._auth_lock:
            if self._mode == "email" and self._access is None:
                await self._login()

    # -- low level (cloud) --------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "Authorization": f"Bearer {self._access}"}

    async def _request(self, method: str, path: str, *, payload: dict[str, Any] | None = None) -> Any:
        url = f"{self._base_url}{path}"
        try:
            async with self._session.request(method, url, json=payload, headers=self._headers()) as resp:
                return await self._read(resp, path)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise PowerShadesUnavailable(str(err)) from err

    @staticmethod
    async def _read(resp: aiohttp.ClientResponse, path: str) -> Any:
        text = await resp.text()
        if 200 <= resp.status < 300:
            return json.loads(text) if text else None
        body = text[:200]
        if resp.status in (401, 403):
            raise PowerShadesAuthError(f"HTTP {resp.status} for {path}: {body}")
        if resp.status == 404:
            raise PowerShadesUnavailable(f"HTTP 404 for {path}: {body}")
        raise PowerShadesError(f"HTTP {resp.status} for {path}: {body}")

    async def _post_json(self, path: str, payload: dict[str, Any]) -> Any:
        url = f"{self._base_url}{path}"
        try:
            async with self._session.post(url, json=payload, headers={"Accept": "application/json"}) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    return json.loads(text) if text else None
                raise PowerShadesAuthError(f"HTTP {resp.status} for {path}: {(await resp.text())[:200]}")
        except (aiohttp.ClientError, TimeoutError) as err:
            raise PowerShadesUnavailable(str(err)) from err
        except PowerShadesError:
            raise

    async def _authed(self, method: str, path: str, *, payload: dict[str, Any] | None = None) -> Any:
        await self.ensure_authenticated()
        try:
            return await self._request(method, path, payload=payload)
        except PowerShadesAuthError:
            if self._mode == "api_key":
                raise  # api key doesn't refresh; surface the error
            await self._refresh_token()
            return await self._request(method, path, payload=payload)

    # -- cloud reads --------------------------------------------------------

    async def _list(self, path: str) -> list[Any]:
        data = await self._authed("GET", path)
        if data is None:
            return []
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            return data["results"]
        if isinstance(data, list):
            return data
        return []

    async def fetch_shades(self) -> list[dict[str, Any]]:
        return await self._list(SHADES)

    async def fetch_groups(self) -> list[dict[str, Any]]:
        return await self._list(GROUPS)

    async def fetch_scenes(self) -> list[dict[str, Any]]:
        return await self._list(SCENES)

    async def fetch_shade_attributes(self) -> list[dict[str, Any]]:
        return await self._list(SHADE_ATTRIBUTES)

    async def async_validate(self) -> list[dict[str, Any]]:
        """Prove cloud credentials work and return the shade list (config flow)."""
        self._access = None if self._mode == "email" else self._api_key
        self._refresh = None
        return await self.fetch_shades()

    # -- cloud commands -----------------------------------------------------

    async def move_shade(self, shade_name: str, percentage: int) -> None:
        await self._authed("POST", SHADES_MOVE, payload={"shade_name": shade_name, "percentage": int(percentage)})

    async def move_group(self, group_name: str, percentage: int) -> None:
        await self._authed("POST", GROUPS_MOVE, payload={"group_name": group_name, "percentage": int(percentage)})

    async def activate_scene(self, scene_name: str) -> None:
        await self._authed("POST", SCENES_MOVE, payload={"scene_name": scene_name})

    # -- local gateway (primary plane) -------------------------------------

    async def fetch_gateway(self, variables: tuple[str, ...]) -> list[GatewayChannel] | None:
        """Read the gateway's per-channel state; return ``None`` if unreachable."""
        from .const import GW_AJAX_PATH, GW_VARIABLES

        url = f"{self._gateway}{GW_AJAX_PATH}?var=" + ",".join(variables if variables else GW_VARIABLES)
        try:
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                text = await resp.text()
                payload = json.loads(text)
        except (aiohttp.ClientError, ValueError, TimeoutError):
            return None
        return _parse_gateway(payload)

    async def _gateway_cmd(self, param: str, channel: int) -> None:
        if not self._gateway:
            raise PowerShadesUnavailable("No gateway configured")
        url = f"{self._gateway}/{self._gateway_param() or 'ajax.shtml'}?{param}={int(channel)}"
        try:
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if not (200 <= resp.status < 300):
                    raise PowerShadesUnavailable(f"gateway {param} ch{channel}: HTTP {resp.status}")
        except (aiohttp.ClientError, TimeoutError) as err:
            raise PowerShadesUnavailable(str(err)) from err

    def _gateway_param(self) -> str:
        from .const import GW_CMD_QUERY

        return GW_CMD_QUERY

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


def _parse_gateway(payload: object) -> list[GatewayChannel] | None:
    """Turn a raw ``ajax.shtml`` response into a list of :class:`GatewayChannel`.

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
    channels: list[GatewayChannel] = []
    for ch in range(1, count + 1):
        i = ch - 1
        channels.append(
            GatewayChannel(
                channel=ch,
                name=(chnames[i] if i < len(chnames) and chnames[i] else None),
                device_id=_optdev(rfdevs, i),
                percent=_opt(percent, i),
                battery_v=_optb(battery, i),
                rx_db=_opt(rx, i),
            )
        )
    return channels
