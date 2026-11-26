"""One wardrive run: a directory with session.json and the data files."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from ..geo import haversine_m
from ..model import Kind, Observation
from . import wiglecsv
from .gzwriter import GzipMemberWriter, repair
from .throttle import RowThrottle

WRITER_KINDS: frozenset[Kind] = frozenset({Kind.WIFI, Kind.BLE, Kind.BT, Kind.AIRCRAFT, Kind.MESH_NODE})
WIFI_BT_FILE = "wifi-bt.wiglecsv.gz"
AIRCRAFT_FILE = "aircraft.jsonl.gz"
MESH_FILE = "mesh.jsonl.gz"
DATA_FILES = (WIFI_BT_FILE, AIRCRAFT_FILE, MESH_FILE)
META_FILE = "session.json"


def _write_meta(path: Path, meta: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta), encoding="utf-8")
    os.replace(tmp, path)


class AircraftThrottle:
    """One line per aircraft every 30 s, or sooner when it moved 1 km."""

    SECONDS, METERS = 30.0, 1000.0

    def __init__(self) -> None:
        self._last: dict[str, tuple[datetime, float, float]] = {}

    def should_write(self, obs: Observation) -> bool:
        fix = obs.fix
        last = self._last.get(obs.id)
        if last is not None:
            t, lat, lon = last
            if (obs.time - t).total_seconds() < self.SECONDS and \
                    haversine_m(lat, lon, fix.lat, fix.lon) < self.METERS:
                return False
        self._last[obs.id] = (obs.time, fix.lat, fix.lon)
        return True


class MeshThrottle:
    """One line per node every 60 s, or sooner when it moved 100 m or got a position or a new name."""

    SECONDS, METERS = 60.0, 100.0

    def __init__(self) -> None:
        self._last: dict[str, tuple[datetime, float | None, float | None, str]] = {}

    def should_write(self, obs: Observation) -> bool:
        lat, lon = (obs.fix.lat, obs.fix.lon) if obs.fix else (None, None)
        last = self._last.get(obs.id)
        if last is not None:
            t, llat, llon, name = last
            changed = name != obs.name or (lat is not None and (
                llat is None or haversine_m(llat, llon, lat, lon) >= self.METERS))
            if not changed and (obs.time - t).total_seconds() < self.SECONDS:
                return False
        self._last[obs.id] = (obs.time, lat, lon, obs.name)
        return True


def _receiver(fix) -> dict | None:
    if fix is None:
        return None
    return {"lat": fix.lat, "lon": fix.lon, "acc": fix.accuracy_m, "time": fix.time.timestamp()}


class Session:
    def __init__(self, dir: Path, meta: dict, throttle: RowThrottle) -> None:
        self.dir = dir
        self.id: str = meta["id"]
        self.mode: str = meta["mode"]
        self._meta = meta
        self._throttle = throttle
        self._wifi_bt = GzipMemberWriter(dir / WIFI_BT_FILE)
        self._aircraft = GzipMemberWriter(dir / AIRCRAFT_FILE)
        self._aircraft_throttle = AircraftThrottle()
        self._mesh = GzipMemberWriter(dir / MESH_FILE)
        self._mesh_throttle = MeshThrottle()

    @classmethod
    def create(cls, sessions_dir: Path, mode: str, now: datetime, version: str,
               throttle: RowThrottle) -> "Session":
        sid = now.strftime("%Y%m%dT%H%M%SZ")
        d = Path(sessions_dir) / sid
        n = 1
        while d.exists():  # two sessions in the same second (resume right after stop)
            n += 1
            d = Path(sessions_dir) / f"{sid}-{n}"
        d.mkdir(parents=True)
        meta = {"id": d.name, "mode": mode, "started": now.isoformat(), "ended": None}
        _write_meta(d / META_FILE, meta)
        s = cls(d, meta, throttle)
        s._wifi_bt.write(wiglecsv.header(version))
        s._wifi_bt.flush()
        return s

    def write(self, obs: Observation) -> bool:
        if obs.kind not in WRITER_KINDS:
            raise ValueError(f"no writer for {obs.kind}")
        if obs.kind == Kind.AIRCRAFT:
            if obs.fix is None:
                return False  # no position: counted, not stored (no site takes it)
            if not self._aircraft_throttle.should_write(obs):
                return False
            record = {**obs.extra.get("aircraft", {}), "_obs_ts": obs.extra.get("obs_ts"),
                      "_pos_ts": obs.extra.get("pos_ts")}
            self._aircraft.write(json.dumps(record, separators=(",", ":")) + "\n")
            return True
        if obs.kind == Kind.MESH_NODE:
            # kept without a position too: Wardrift takes positionless nodes
            if not self._mesh_throttle.should_write(obs):
                return False
            record = {**obs.extra.get("node", {}), "_obs_ts": obs.time.timestamp(),
                      "_receiver": _receiver(obs.extra.get("receiver"))}
            self._mesh.write(json.dumps(record, separators=(",", ":")) + "\n")
            return True
        if obs.fix is None:
            raise ValueError("observation has no GPS fix")
        if not self._throttle.should_write(obs):
            return False
        self._wifi_bt.write(wiglecsv.row(obs))
        return True

    def flush(self) -> None:
        self._wifi_bt.flush()
        self._aircraft.flush()
        self._mesh.flush()

    def close(self, now: datetime) -> None:
        self.flush()
        self._meta["ended"] = now.isoformat()
        _write_meta(self.dir / META_FILE, self._meta)

    @staticmethod
    def unclosed(sessions_dir: Path) -> list[Path]:
        out = []
        for meta_path in sorted(Path(sessions_dir).glob(f"*/{META_FILE}")):
            try:
                if json.loads(meta_path.read_text())["ended"] is None:
                    out.append(meta_path.parent)
            except (OSError, ValueError, KeyError):
                continue
        return out

    @staticmethod
    def close_stale(session_dir: Path, now: datetime) -> None:
        for name in DATA_FILES:
            repair(Path(session_dir) / name)
        path = Path(session_dir) / META_FILE
        meta = json.loads(path.read_text())
        meta["ended"] = now.isoformat()
        meta["recovered"] = True
        _write_meta(path, meta)
