"""Async client for the PowerShades cloud API."""

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


class PowerShadesError(Exception):
    """Base error for the PowerShades client."""


class PowerShadesAuthError(PowerShadesError):
    """Raised when credentials are invalid or the session cannot be refreshed."""


class PowerShadesUnavailable(PowerShadesError):
    """Raised when the API cannot be reached."""


_LIST_PATHS: dict[str, str] = {
    "shades": SHADES,
    "groups": GROUPS,
    "scenes": SCENES,
    "shade_attributes": SHADE_ATTRIBUTES,
}


class PowerShadesClient:
    """JWT-authenticated async client for the PowerShades cloud API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._access: str | None = None
        self._refresh: str | None = None
        self._auth_lock = asyncio.Lock()

    # -- auth ---------------------------------------------------------------

    async def _login(self) -> None:
        """Log in with email + password, storing access + refresh tokens."""
        resp = await self._post_json(AUTH_JWT, {"email": self._email, "password": self._password})
        self._access = resp["access"]
        self._refresh = resp.get("refresh")

    async def _refresh_token(self) -> None:
        if not self._refresh:
            raise PowerShadesAuthError("No refresh token available")
        resp = await self._post_json(AUTH_JWT_REFRESH, {"refresh": self._refresh})
        self._access = resp["access"]
        if resp.get("refresh"):
            self._refresh = resp["refresh"]

    async def ensure_authenticated(self) -> None:
        """Make sure we hold a valid access token (log in if needed)."""
        async with self._auth_lock:
            if self._access is None:
                await self._login()

    # -- low level ---------------------------------------------------------

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
            await self._refresh_token()
            return await self._request(method, path, payload=payload)

    # -- reads -------------------------------------------------------------

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
        """Prove the credentials work and return the shade list (config flow)."""
        self._access = None
        self._refresh = None
        return await self.fetch_shades()

    # -- commands ----------------------------------------------------------

    async def move_shade(self, shade_name: str, percentage: int) -> None:
        await self._authed("POST", SHADES_MOVE, payload={"shade_name": shade_name, "percentage": int(percentage)})

    async def move_group(self, group_name: str, percentage: int) -> None:
        await self._authed("POST", GROUPS_MOVE, payload={"group_name": group_name, "percentage": int(percentage)})

    async def activate_scene(self, scene_name: str) -> None:
        await self._authed("POST", SCENES_MOVE, payload={"scene_name": scene_name})
