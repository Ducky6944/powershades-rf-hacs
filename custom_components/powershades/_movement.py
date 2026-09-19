"""Shared "move to a position" logic (local gateway only).

The gateway exposes only up / down / stop — no "set to N%". We approximate an
absolute position by *timing* a move for a fraction of a full 0↔100% sweep
(``travel_time``). Two modes:

* **Known start** (``from_position`` given): move the *shortest* direction for
  just the needed distance — no full-travel leg, so it's fast and gentle.
* **Unknown start** (``from_position`` is None, e.g. after a restart): calibrate
  to fully open (drive up a full sweep → known 100%), then drive down by the
  remaining distance.

Accuracy depends on ``travel_time`` being close to reality. ``reset_open``
re-syncs a drifted shade by driving it fully open (generous fixed time) and
locking it at 100%. Best-effort: timeouts / HTTP errors are logged, not raised.
"""

from __future__ import annotations

import asyncio
import logging

from .client import PowerShadesClient

_LOGGER = logging.getLogger(__name__)


async def stop(client: PowerShadesClient, channel: int) -> None:
    """Idempotent stop on a channel (swallows errors, logs)."""
    try:
        await client.gateway_stop(channel)
    except Exception as err:
        _LOGGER.debug("stop ch%s: %s", channel, err)


async def open_channel(client: PowerShadesClient, channel: int) -> None:
    """Drive to fully open (up) until the shade's own end-stop halts it."""
    try:
        await client.gateway_up(channel)
    except Exception as err:
        _LOGGER.warning("open ch%s failed: %s", channel, err)


async def close_channel(client: PowerShadesClient, channel: int) -> None:
    """Drive to fully closed (down) until the shade's own end-stop halts it."""
    try:
        await client.gateway_down(channel)
    except Exception as err:
        _LOGGER.warning("close ch%s failed: %s", channel, err)


async def _drive(client: PowerShadesClient, channel: int, up: bool, seconds: float) -> bool:
    """Drive a channel up/down for ``seconds`` then stop. True on success."""
    try:
        if up:
            await client.gateway_up(channel)
        else:
            await client.gateway_down(channel)
    except Exception as err:
        _LOGGER.warning("drive ch%s %s failed: %s", channel, "up" if up else "down", err)
        return False
    await asyncio.sleep(max(0.2, seconds))
    await stop(client, channel)
    return True


async def set_position(
    client: PowerShadesClient,
    channel: int,
    target: int,
    travel_time_s: float,
    from_position: int | None = None,
) -> None:
    """Approximate "set channel to target%" (0=closed, 100=open), timed.

    If ``from_position`` (our last known/recorded position) is provided, move the
    *shortest* direction for just the needed distance — no full-travel calibration
    needed. If it's ``None`` (unknown — e.g. after a restart), fall back to the
    safe path: full up (known 100% reference) then down by the remaining distance.
    """
    target = max(0, min(100, int(target)))
    travel_time_s = max(0.5, float(travel_time_s))

    if from_position is not None and 0 <= int(from_position) <= 100:
        dist = abs(target - int(from_position))
        if dist < 1:
            await stop(client, channel)
            return
        up = target > int(from_position)
        await _drive(client, channel, up, travel_time_s * (dist / 100.0))
        return

    await stop(client, channel)
    # Calibrate to fully open (known 100% reference), then drive down.
    try:
        await client.gateway_up(channel)
    except Exception as err:
        _LOGGER.warning("set_position ch%s open leg failed: %s", channel, err)
        return
    await asyncio.sleep(travel_time_s)
    await stop(client, channel)
    if target < 100:
        await _drive(client, channel, up=False, seconds=travel_time_s * ((100 - target) / 100.0))


async def reset_open(client: PowerShadesClient, channel: int, seconds: float) -> None:
    """Drive a channel to fully open and (physically) park it there.

    Sends ``up`` for a *generous* fixed duration (longer than any realistic full
    travel, so the shade reaches its top end-stop and stops on its own), then a
    hard ``stop``. Used to re-sync a shade whose recorded position has drifted:
    after a reset the shade *is* at 100%, and the caller locks the estimate to
    100 so it can no longer be out of sync.
    """
    up_s = max(5, float(seconds))
    try:
        await client.gateway_up(channel)
    except Exception as err:
        _LOGGER.warning("reset ch%s open leg failed: %s", channel, err)
        return
    await asyncio.sleep(up_s)
    await stop(client, channel)
