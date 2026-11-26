"""Per-network row throttle for session files."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..geo import haversine_m
from ..model import Kind, Observation


@dataclass
class _Last:
    time: datetime
    lat: float
    lon: float
    rssi: int | None


class RowThrottle:
    """Write a row on first sighting, or once `seconds` have passed and the
    receiver moved `meters` or the RSSI improved by 3 dB since the last row."""

    RSSI_GAIN_DB = 3

    def __init__(self, seconds: float = 10.0, meters: float = 25.0) -> None:
        self.seconds = seconds
        self.meters = meters
        self._last: dict[tuple[Kind, str], _Last] = {}

    def should_write(self, obs: Observation) -> bool:
        fix = obs.fix
        if fix is None:
            return False
        key = (obs.kind, obs.id)
        last = self._last.get(key)
        if last is not None:
            if (obs.time - last.time).total_seconds() < self.seconds:
                return False
            moved = haversine_m(last.lat, last.lon, fix.lat, fix.lon) >= self.meters
            stronger = (obs.rssi is not None and last.rssi is not None
                        and obs.rssi >= last.rssi + self.RSSI_GAIN_DB)
            if not (moved or stronger):
                return False
        self._last[key] = _Last(obs.time, fix.lat, fix.lon, obs.rssi)
        return True
