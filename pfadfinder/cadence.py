"""Scan cadence from GPS speed: scan fast while moving, slowly when parked."""

from __future__ import annotations

from datetime import datetime

from .model import Fix

DRIVING_MPS = 5 / 3.6       # > 5 km/h
STATIONARY_MPS = 1 / 3.6    # < 1 km/h
PARKED_AFTER_S = 60.0


class MotionTracker:
    def __init__(self) -> None:
        self._fix: Fix | None = None
        self._now: datetime | None = None
        self._stationary_since: datetime | None = None

    def update(self, fix: Fix | None, now: datetime) -> None:
        self._now = now
        self._fix = fix  # already fresh: FixSelector drops stale fixes
        if fix is None:
            return
        speed = fix.speed_mps
        if speed is not None and speed < STATIONARY_MPS:
            if self._stationary_since is None:
                self._stationary_since = now
        else:
            self._stationary_since = None

    def has_fix(self) -> bool:
        return self._fix is not None

    def interval(self) -> float | None:
        fix = self._fix
        if fix is None or self._now is None:
            return None
        if fix.speed_mps is not None and fix.speed_mps > DRIVING_MPS:
            return 3
        if self._stationary_since is not None and \
                (self._now - self._stationary_since).total_seconds() > PARKED_AFTER_S:
            return 60
        return 10
