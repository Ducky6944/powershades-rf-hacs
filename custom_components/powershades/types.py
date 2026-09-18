"""Shared data models for PowerShades.

Kept in their own module so both ``client.py`` and ``coordinator.py`` can import
the types without creating an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GatewayChannel:
    """One RF channel on the local gateway (the primary, live state plane).

    ``percent`` is the position the gateway reports (0 = fully closed/down,
    100 = fully open/up); ``None`` = not linked / not reporting.
    """

    channel: int
    name: str | None = None
    device_id: str | None = None  # gateway RF device id, hex (e.g. "b65e5183...")
    percent: int | None = None  # 0..100; None = not reporting
    battery_v: float | None = None  # volts; None = unknown
    rx_db: int | None = None  # dB; None = unknown
    rf_type: str = "RF"

    @property
    def linked(self) -> bool:
        """True if a shade/device is reporting on this channel."""
        return self.device_id is not None or self.percent is not None


@dataclass(frozen=True)
class GroupInfo:
    """A user-defined local group: a set of channels moved together."""

    id: int
    name: str
    shades: tuple[int, ...]


@dataclass(frozen=True)
class PowerShadesData:
    """Snapshot: local gateway channels (the only state plane)."""

    gateway: list[GatewayChannel] = field(default_factory=list)

    def channel(self, number: int) -> GatewayChannel | None:
        return next((c for c in self.gateway if c.channel == number), None)
