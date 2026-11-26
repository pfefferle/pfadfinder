"""Shared data types. All datetimes are timezone-aware UTC."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Kind(str, Enum):
    """Observation kind. Values match the WiGLE CSV ``Type`` column."""

    WIFI = "WIFI"
    BLE = "BLE"
    BT = "BT"
    AIRCRAFT = "AIRCRAFT"
    MESH_NODE = "MESH_NODE"


# kinds whose position is the receiver's own GPS fix; aircraft and mesh nodes report their own
RECEIVER_FIX_KINDS = frozenset({Kind.WIFI, Kind.BLE, Kind.BT})


@dataclass(frozen=True)
class Fix:
    lat: float
    lon: float
    alt_m: float | None
    accuracy_m: float
    speed_mps: float | None
    time: datetime
    mode: int  # gpsd mode: 2 = 2D, 3 = 3D
    sats: int | None = None


@dataclass
class Observation:
    kind: Kind
    id: str
    name: str
    rssi: int | None
    time: datetime
    channel: int = 0
    frequency: int = 0
    auth: str = ""
    mfgr_id: int | None = None
    extra: dict = field(default_factory=dict)
    fix: Fix | None = None


@dataclass(frozen=True)
class Claim:
    """A hardware resource a source needs. Exclusive claims cannot be shared."""

    resource: str
    exclusive: bool = True


@dataclass(frozen=True)
class Device:
    source: str
    path: str
    label: str
    internal: bool
    claims: tuple[Claim, ...]
