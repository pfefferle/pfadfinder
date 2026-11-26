"""What each site gets, and the upload ledger.

- WiGLE: every closed session with rows, once.
- Wardrift: the same, but only sessions that ended within the last 72 hours.
- WDGWars: one generated file with the newest sighting of every network that
  was seen again since the last WDGWars upload.
- Aircraft and mesh files: once per session to every site that takes them
  (WDGWars and Wardrift), Wardrift again only within 72 hours.

The first failure stops a site's run; the next upload tries again.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta
from pathlib import Path

from .. import __version__
from ..model import Fix, Kind, Observation
from ..storage import wiglecsv
from ..storage.db import Store
from ..storage.session import AIRCRAFT_FILE, MESH_FILE, META_FILE, WIFI_BT_FILE
from .sites import SiteResult, site as site_by_key

WARDRIFT_MAX_AGE = timedelta(hours=72)
MAX_REJECTS = 3
# failures that say nothing about the file itself: stop the site and try again later
_SITE_PROBLEMS = ("key", "token", "api name", "did not accept", "sign in", "could not reach", "timed out",
                  "timeout", "connection", "network", "unreachable", "http 5", "throttled", "429",
                  # the Wardrift mesh ledger keeps the nodes itself until it can send them
                  "retry is pending", "remain queued", "mesh state unavailable")


def _site_problem(message: str) -> bool:
    m = message.lower()
    return any(s in m for s in _SITE_PROBLEMS)


def _meta(session_dir: Path) -> dict:
    try:
        return json.loads((session_dir / META_FILE).read_text())
    except (OSError, ValueError):
        return {}


def _has_rows(path: Path) -> bool:
    """True when the file holds at least one row after the two header lines."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            fh.readline(); fh.readline()
            return bool(fh.readline())
    except (OSError, EOFError):
        return False


def _has_lines(path: Path) -> bool:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return bool(fh.readline())
    except (OSError, EOFError):
        return False


def _one_member(path: Path) -> bytes:
    """The session file as one gzip member. It is written as many (one per flush); WiGLE reads that,
    Wardrift checks the length in the last trailer against the whole body and refuses it."""
    return gzip.compress(gzip.decompress(path.read_bytes()))


def _session_files(store: Store, sessions_dir: Path, key: str, now: datetime,
                   name: str = WIFI_BT_FILE) -> tuple[list[Path], list[Path]]:
    """(to send, too old for this site) for one kind of session file."""
    has_content = _has_rows if name == WIFI_BT_FILE else _has_lines
    settled = {(r["session"], r["file"]) for r in store.upload_status(key) if r["status"] in ("done", "skipped")}
    send, old = [], []
    for d in sorted(Path(sessions_dir).iterdir()) if Path(sessions_dir).is_dir() else []:
        f = d / name
        meta = _meta(d)
        if (d.name, name) in settled or not f.exists() or not meta.get("ended") or not has_content(f):
            continue
        # age from the last data write: a session closed on the next boot after a power loss
        # would look fresh by its "ended" stamp
        written = datetime.fromtimestamp(f.stat().st_mtime, now.tzinfo)
        if key == "wardrift" and now - written > WARDRIFT_MAX_AGE:
            old.append(f)
        else:
            send.append(f)
    return send, old


def _row_observation(row: dict) -> Observation:
    when = datetime.fromisoformat(row["last_time"])
    fix = Fix(row["last_lat"], row["last_lon"], row["last_alt"], row["last_acc"] or 0.0, None, when, 3)
    return Observation(Kind(row["kind"]), row["id"], row["name"] or "", row["last_rssi"], when,
                       channel=row["channel"] or 0, frequency=row["frequency"] or 0, auth=row["auth"] or "",
                       mfgr_id=row["mfgr_id"], fix=fix)


# session files beside the Wi-Fi/Bluetooth CSV: (file, the site's flag, the site's upload method)
SESSION_FILES = [(AIRCRAFT_FILE, "takes_aircraft", "upload_aircraft"), (MESH_FILE, "takes_mesh", "upload_mesh")]


def pending_count(store: Store, sessions_dir: Path, key: str, now: datetime) -> int:
    """Uploads waiting for this site. For WDGWars all pending networks are one upload."""
    if key == "wdgwars":
        count = 1 if store.wdgwars_pending_count() else 0
    else:
        count = len(_session_files(store, sessions_dir, key, now)[0])
    site = site_by_key(key)
    for name, flag, _ in SESSION_FILES:
        if getattr(site, flag):
            count += len(_session_files(store, sessions_dir, key, now, name)[0])
    return count


def _skip_old(store: Store, key: str, old: list[Path]) -> None:
    for f in old:
        store.mark_upload(key, f.parent.name, f.name, "skipped", error="older than 72 h")
    store.commit()


def _send(store: Store, key: str, f: Path, send) -> SiteResult:
    """Send one file. A file the site rejects MAX_REJECTS times is skipped for good."""
    session = f.parent.name
    prev = next((r for r in store.upload_status(key) if (r["session"], r["file"]) == (session, f.name)), None)
    rejects = json.loads(prev["detail"] or "{}").get("rejects", 0) if prev else 0
    store.mark_upload(key, session, f.name, "pending", detail={"rejects": rejects})
    store.commit()
    res = send()
    if res.ok:
        store.mark_upload(key, session, f.name, "done", detail={"message": res.message})
    elif _site_problem(res.message):
        store.mark_upload(key, session, f.name, "failed", detail={"rejects": rejects}, error=res.message)
    else:
        rejects += 1
        status = "skipped" if rejects >= MAX_REJECTS else "failed"
        error = f"rejected {rejects} times: {res.message}" if status == "skipped" else res.message
        store.mark_upload(key, session, f.name, status, detail={"rejects": rejects}, error=error)
    store.commit()
    return res


def upload_site(store: Store, sessions_dir: Path, site, cfg, now: datetime,
                state_dir: Path | None = None) -> list[SiteResult]:
    """Wi-Fi/Bluetooth first, then aircraft and mesh. The first site problem stops the site; the next upload tries again."""
    if site.key == "wdgwars":
        results = _upload_networks(store, site, cfg, now)
    else:
        results = []
        send, old = _session_files(store, sessions_dir, site.key, now)
        _skip_old(store, site.key, old)
        for f in send:
            res = _send(store, site.key, f,
                        lambda f=f: site.upload(_one_member(f), f"pfadfinder-{f.parent.name}.wiglecsv.gz", cfg))
            results.append(res)
            if not res.ok and _site_problem(res.message):
                return results  # key or network: the next upload tries again
    for name, flag, method in SESSION_FILES:
        if any(not r.ok and _site_problem(r.message) for r in results):
            break
        if not getattr(site, flag, False):
            continue
        send, old = _session_files(store, sessions_dir, site.key, now, name)
        _skip_old(store, site.key, old)
        upload = getattr(site, method)
        for f in send:
            records = [json.loads(line) for line in gzip.open(f, "rt", encoding="utf-8") if line.strip()]
            args = (records, cfg) if name == AIRCRAFT_FILE else (records, cfg, state_dir)
            res = _send(store, site.key, f, lambda args=args: upload(*args))
            results.append(res)
            if not res.ok and _site_problem(res.message):
                break
    return results


def _upload_networks(store: Store, site, cfg, now: datetime) -> list[SiteResult]:
    rows = store.wdgwars_pending()
    if not rows:
        return []
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    text = wiglecsv.header(__version__) + "".join(wiglecsv.row(_row_observation(r)) for r in rows)
    file = f"{stamp}.wiglecsv.gz"
    store.mark_upload(site.key, "networks", file, "pending")
    store.commit()
    res = site.upload(gzip.compress(text.encode("utf-8")), f"pfadfinder-networks-{file}", cfg)
    store.mark_upload(site.key, "networks", file, "done" if res.ok else "failed",
                      detail={"message": res.message, "networks": len(rows)}, error="" if res.ok else res.message)
    if res.ok:
        store.mark_wdgwars_sent([(r["kind"], r["id"]) for r in rows], now.isoformat(timespec="seconds"))
    store.commit()
    return [res]
