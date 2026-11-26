"""SQLite store: unique sightings, per-session counts, upload ledger, site stats.

Writes are batched: nothing is durable until commit(), which the core calls on
its 30 s flush tick.
"""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..model import Kind, Observation

SCHEMA = """
CREATE TABLE IF NOT EXISTS networks (
    kind TEXT NOT NULL, id TEXT NOT NULL, name TEXT,
    first_seen TEXT, last_seen TEXT,
    best_rssi INTEGER, best_lat REAL, best_lon REAL,
    first_session TEXT, wdgwars_uploaded_at TEXT,
    auth TEXT, channel INTEGER, frequency INTEGER, mfgr_id INTEGER,
    last_rssi INTEGER, last_lat REAL, last_lon REAL, last_alt REAL, last_acc REAL, last_time TEXT,
    PRIMARY KEY (kind, id)
);
CREATE INDEX IF NOT EXISTS networks_last_seen ON networks (kind, last_seen);
CREATE TABLE IF NOT EXISTS session_sightings (
    session TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, is_new INTEGER NOT NULL,
    PRIMARY KEY (session, kind, id)
);
CREATE TABLE IF NOT EXISTS uploads (
    site TEXT NOT NULL, session TEXT NOT NULL, file TEXT NOT NULL,
    status TEXT NOT NULL, receipt TEXT, detail TEXT, error TEXT, updated_at TEXT,
    PRIMARY KEY (site, session, file)
);
CREATE TABLE IF NOT EXISTS site_stats (
    site TEXT PRIMARY KEY, json TEXT NOT NULL, fetched_at TEXT NOT NULL
);
"""


# columns added after the first release; older databases get them on open
NEW_NETWORK_COLUMNS = {"auth": "TEXT", "channel": "INTEGER", "frequency": "INTEGER", "mfgr_id": "INTEGER",
                       "last_rssi": "INTEGER", "last_lat": "REAL", "last_lon": "REAL", "last_alt": "REAL",
                       "last_acc": "REAL", "last_time": "TEXT"}


def _locked(method):
    """Serialize access: the core loop and the upload thread share one connection."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(networks)")}
        for col, typ in NEW_NETWORK_COLUMNS.items():
            if col not in have:
                self._conn.execute(f"ALTER TABLE networks ADD COLUMN {col} {typ}")
        self._lock = threading.RLock()

    def _begin(self) -> None:
        # SQLite may roll back on its own (e.g. SQLITE_FULL), so ask it, not a shadow flag
        if not self._conn.in_transaction:
            self._conn.execute("BEGIN")

    @_locked
    def commit(self) -> None:
        if self._conn.in_transaction:
            self._conn.execute("COMMIT")

    @_locked
    def close(self) -> None:
        self.commit()
        self._conn.close()

    # -- sightings ------------------------------------------------------------
    @_locked
    def record(self, obs: Observation, session_id: str) -> bool:
        self._begin()
        kind, seen = obs.kind.value, obs.time.isoformat(timespec="seconds")
        fix = obs.fix
        lat, lon = (fix.lat, fix.lon) if fix else (None, None)
        alt, acc = (fix.alt_m, fix.accuracy_m) if fix else (None, None)
        existing = self._conn.execute(
            "SELECT best_rssi FROM networks WHERE kind=? AND id=?", (kind, obs.id)).fetchone()
        is_new = existing is None
        if is_new:
            self._conn.execute(
                "INSERT INTO networks (kind,id,name,first_seen,last_seen,best_rssi,best_lat,best_lon,first_session,"
                " auth,channel,frequency,mfgr_id,last_rssi,last_lat,last_lon,last_alt,last_acc,last_time)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (kind, obs.id, obs.name, seen, seen, obs.rssi, lat, lon, session_id,
                 obs.auth, obs.channel, obs.frequency, obs.mfgr_id, obs.rssi, lat, lon, alt, acc, seen))
        else:
            better = obs.rssi is not None and (existing["best_rssi"] is None or obs.rssi > existing["best_rssi"])
            if better:
                self._conn.execute("UPDATE networks SET best_rssi=?, best_lat=?, best_lon=? WHERE kind=? AND id=?",
                                   (obs.rssi, lat, lon, kind, obs.id))
            self._conn.execute(
                "UPDATE networks SET name=COALESCE(NULLIF(?,''),name), last_seen=?, auth=COALESCE(NULLIF(?,''),auth),"
                " channel=?, frequency=?, mfgr_id=COALESCE(?,mfgr_id), last_rssi=?, last_lat=?, last_lon=?,"
                " last_alt=?, last_acc=?, last_time=? WHERE kind=? AND id=?",
                (obs.name, seen, obs.auth, obs.channel, obs.frequency, obs.mfgr_id, obs.rssi, lat, lon, alt, acc,
                 seen, kind, obs.id))
        self._conn.execute(
            "INSERT OR IGNORE INTO session_sightings (session,kind,id,is_new) VALUES (?,?,?,?)",
            (session_id, kind, obs.id, int(is_new)))
        return is_new

    @_locked
    def wdgwars_pending(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM networks WHERE kind IN ('WIFI', 'BLE', 'BT') AND last_lat IS NOT NULL"
            " AND last_time > COALESCE(wdgwars_uploaded_at, '') ORDER BY kind, id").fetchall()
        return [dict(r) for r in rows]

    @_locked
    def wdgwars_pending_count(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM networks WHERE kind IN ('WIFI', 'BLE', 'BT') AND last_lat IS NOT NULL"
            " AND last_time > COALESCE(wdgwars_uploaded_at, '')").fetchone()[0]

    @_locked
    def mark_wdgwars_sent(self, keys: list[tuple[str, str]], at: str) -> None:
        self._begin()
        self._conn.executemany("UPDATE networks SET wdgwars_uploaded_at=? WHERE kind=? AND id=?",
                               [(at, kind, id) for kind, id in keys])
        self.commit()

    @_locked
    def counts(self, session_id: str) -> dict[Kind, tuple[int, int]]:
        rows = self._conn.execute(
            "SELECT kind, COUNT(*) AS seen, SUM(is_new) AS new FROM session_sightings WHERE session=? GROUP BY kind",
            (session_id,)).fetchall()
        return {Kind(r["kind"]): (r["seen"], r["new"]) for r in rows}

    @_locked
    def network(self, kind: Kind, id: str) -> dict | None:
        r = self._conn.execute("SELECT * FROM networks WHERE kind=? AND id=?", (kind.value, id)).fetchone()
        return dict(r) if r else None

    @_locked
    def latest(self, kind: Kind, n: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, name, best_rssi AS rssi, last_seen, last_alt AS alt_m FROM networks WHERE kind=?"
            " ORDER BY last_seen DESC LIMIT ?", (kind.value, n)).fetchall()
        return [dict(r) for r in rows]

    # -- uploads ----------------------------------------------------------------
    @_locked
    def mark_upload(self, site: str, session_id: str, file: str, status: str, *,
                    receipt: str = "", detail: dict | None = None, error: str = "") -> None:
        if status not in ("pending", "done", "failed", "skipped"):
            raise ValueError(f"bad upload status {status!r}")
        self._conn.execute(
            "INSERT INTO uploads (site,session,file,status,receipt,detail,error,updated_at) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(site,session,file) DO UPDATE SET status=excluded.status, receipt=excluded.receipt,"
            " detail=excluded.detail, error=excluded.error, updated_at=excluded.updated_at",
            (site, session_id, file, status, receipt, json.dumps(detail or {}), error, _now()))

    @_locked
    def upload_status(self, site: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT session, file, status, error, updated_at, receipt, detail FROM uploads WHERE site=?"
            " ORDER BY updated_at DESC", (site,)).fetchall()
        return [dict(r) for r in rows]

    # -- stats ------------------------------------------------------------------
    @_locked
    def set_stats(self, site: str, data: dict) -> None:
        self._conn.execute(
            "INSERT INTO site_stats (site,json,fetched_at) VALUES (?,?,?)"
            " ON CONFLICT(site) DO UPDATE SET json=excluded.json, fetched_at=excluded.fetched_at",
            (site, json.dumps(data), _now()))

    @_locked
    def get_stats(self, site: str) -> dict | None:
        r = self._conn.execute("SELECT json, fetched_at FROM site_stats WHERE site=?", (site,)).fetchone()
        if r is None:
            return None
        return {**json.loads(r["json"]), "fetched_at": r["fetched_at"]}
