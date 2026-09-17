"""Shared data models for the PowerShades integration.

Kept in their own module so both ``client.py`` and ``coordinator.py`` can import
the types without creating an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GatewayChannel:
    """One RF channel on the local gateway (the primary, live state plane).

    ``percent`` is the position the gateway reports (its own convention: a value
    where fully-open and fully-closed are opposite ends); ``None`` = not
    reporting / not linked.
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
class ShadeInfo:
    """A cloud shade plus its static metadata (name-resolution only)."""

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
class PowerShadesData:
    """Snapshot: local gateway channels (primary) + optional cloud name lists."""

    gateway: list[GatewayChannel] = field(default_factory=list)
    shades: list[ShadeInfo] = field(default_factory=list)  # cloud, optional
    groups: list[GroupInfo] = field(default_factory=list)  # cloud, optional
    scenes: list[SceneInfo] = field(default_factory=list)  # cloud, optional

    def channel(self, number: int) -> GatewayChannel | None:
        return next((c for c in self.gateway if c.channel == number), None)
