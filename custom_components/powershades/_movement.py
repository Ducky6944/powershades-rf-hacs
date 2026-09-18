"""Shared "move to a position" logic (local gateway only).

The gateway exposes only up / down / stop — no "set to N%". We approximate an
absolute position deterministically:

1. ``stop`` (idempotent, clears any in-flight motion).
2. Drive ``up`` for a full sweep (``travel_time``) so we *know* the shade is
   fully open (pos 100).
3. Drive ``down`` for ``(100 - target)%`` of a full sweep, then ``stop``.

That lands on ``target``% regardless of where the shade started. It is a
*timed* approximation, so accuracy depends on the user keeping ``travel_time``
close to reality. Best-effort: timeouts / HTTP errors are logged, not raised.
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


async def set_position(
    client: PowerShadesClient,
    channel: int,
    target: int,
    travel_time_s: float,
) -> None:
    """Approximate "set channel to target%" (0=closed, 100=open) by timed moves.

    Calibrates to fully open first (see module docstring), so the result does
    not depend on the shade's current, possibly-unknown, position.
    """
    target = max(0, min(100, int(target)))
    travel_time_s = max(0.5, float(travel_time_s))

    await stop(client, channel)

    # Step 2: fully open (known reference = 100%).
    try:
        await client.gateway_up(channel)
    except Exception as err:
        _LOGGER.warning("set_position ch%s open leg failed: %s", channel, err)
        return
    await asyncio.sleep(travel_time_s)
    await stop(client, channel)

    # Step 3: move down to target.
    down_seconds = travel_time_s * ((100 - target) / 100.0)
    if down_seconds > 0:
        try:
            await client.gateway_down(channel)
        except Exception as err:
            _LOGGER.warning("set_position ch%s down leg failed: %s", channel, err)
            return
        await asyncio.sleep(down_seconds)
        await stop(client, channel)
