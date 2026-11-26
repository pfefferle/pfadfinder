"""One small adapter per upload site, on top of RocketGod's site clients (vendor/wdgwars_uploader).

Each site uploads a gzipped WiGLE CSV and reports the account numbers for the
Upload screen as {"user": str, "lines": [str, str]}. Secrets are never logged.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from wdgwars_uploader import uploader, wardrift, wigle

from ..config import Config, update_config_file


@dataclass(frozen=True)
class SiteResult:
    ok: bool
    message: str


class SiteError(Exception):
    """Numbers could not be fetched; the message is shown on the site's page."""


def _n(value) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def _first(d: dict, *keys: str):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


# the fields WDGWars takes for a mesh node (reference state.py, _MESH_UPLOAD_FIELDS)
MESH_UPLOAD_FIELDS = ("node_id", "node_type", "name", "lat", "lon", "rssi", "first_seen", "type", "network", "public_key")


def newest_per_node(records: list[dict]) -> list[dict]:
    """A session file has a line per sighting; the sites want each node once, as last heard."""
    out: dict[tuple, dict] = {}
    for r in records:
        k = (r.get("network"), r.get("node_id"))
        if k not in out or (r.get("heard_ts") or 0) >= (out[k].get("heard_ts") or 0):
            out[k] = r
    return list(out.values())


class Wigle:
    key, title = "wigle", "WiGLE"
    takes_aircraft = False
    takes_mesh = False

    def configured(self, cfg: Config) -> bool:
        return bool(cfg.wigle_api_name and cfg.wigle_api_token)

    def upload(self, data: bytes, filename: str, cfg: Config) -> SiteResult:
        ok, msg = wigle.upload(cfg.wigle_api_name, cfg.wigle_api_token, data, filename)
        return SiteResult(ok, msg)

    def stats(self, cfg: Config, state_dir: Path, config_path: Path | None = None) -> dict | None:
        me = wigle.user_stats(cfg.wigle_api_name, cfg.wigle_api_token)
        if not me.get("ok"):
            return None
        return {"user": me.get("name") or "",
                "lines": [f"rank {_n(me.get('rank'))}   month {_n(me.get('month_rank'))}",
                          f"WiFi {_n(me.get('wifi_gps'))}   BT {_n(me.get('bt'))}   Cell {_n(me.get('cell'))}"]}


class Wdgwars:
    key, title = "wdgwars", "WDGWars"
    takes_aircraft = True
    takes_mesh = True  # MeshCore and Meshtastic nodes with a position, in one list

    def configured(self, cfg: Config) -> bool:
        return bool(cfg.wdgwars_api_key)

    def upload(self, data: bytes, filename: str, cfg: Config) -> SiteResult:
        ok, msg = uploader.WDGWarsClient(cfg.wdgwars_api_key).upload_wardrive_csv(data, filename)
        return SiteResult(ok, msg)

    def upload_aircraft(self, records: list[dict], cfg: Config) -> SiteResult:
        res = uploader.WDGWarsClient(cfg.wdgwars_api_key).upload_aircraft_chunked(records)
        return SiteResult(res.ok, res.summary())

    def upload_mesh(self, records: list[dict], cfg: Config, state_dir: Path) -> SiteResult:
        nodes = [{k: n[k] for k in MESH_UPLOAD_FIELDS if k in n} for n in newest_per_node(records)
                 if n.get("uploadable")
                 and n.get("lat") is not None and n.get("lon") is not None]
        if not nodes:
            return SiteResult(True, "no mesh nodes with a position")
        res = uploader.WDGWarsClient(cfg.wdgwars_api_key).upload_json_hmac(
            {"networks": [], "aircraft": [], "meshcore_nodes": nodes})
        if not res.ok:
            return SiteResult(False, res.error)
        d = res.detail.get("result", res.detail)
        return SiteResult(True, f"{len(nodes)} nodes: imported {_n(d.get('meshcore_imported'))}, "
                                f"seen {_n(d.get('meshcore_already_seen'))}")

    def stats(self, cfg: Config, state_dir: Path, config_path: Path | None = None) -> dict | None:
        me = uploader.WDGWarsClient(cfg.wdgwars_api_key).get_stats()
        if not me:
            return None
        return {"user": str(_first(me, "username", "name", "nick", "user") or ""),
                "lines": [f"WiFi {_n(me.get('wifi'))}   BLE {_n(me.get('ble'))}",
                          f"aircraft {_n(me.get('aircraft'))}   mesh {_n(me.get('mesh'))}   total {_n(me.get('total'))}"]}


class Wardrift:
    key, title = "wardrift", "Wardrift"
    takes_aircraft = True
    takes_mesh = True
    MESH_STATE = "wardrift_mesh.json"  # the reference's durable delivery ledger
    MESH_DEFINITIVE = (400, 413, 422)  # what the reference treats as a final rejection
    TOKEN_FILE = "wardrift.json"
    _failed_login: tuple[str, str, str] | None = None  # (username, password, message): not retried
    COUNT_KEYS = ("signals", "networks", "total", "count")

    def configured(self, cfg: Config) -> bool:
        return bool(cfg.wardrift_api_key)

    def upload(self, data: bytes, filename: str, cfg: Config) -> SiteResult:
        ok, msg = wardrift.WardriftClient(cfg.wardrift_api_key).upload_csv(data, filename)
        return SiteResult(ok, msg)

    def upload_aircraft(self, records: list[dict], cfg: Config) -> SiteResult:
        res = wardrift.WardriftClient(cfg.wardrift_api_key).upload_aircraft(records)
        return SiteResult(res.ok and not res.error, res.error if res.error else res.summary())

    def upload_mesh(self, records: list[dict], cfg: Config, state_dir: Path) -> SiteResult:
        reporter = wardrift.MeshReporter(Path(state_dir) / self.MESH_STATE)
        res = wardrift.WardriftClient(cfg.wardrift_api_key, timeout=wardrift.MESH_TIMEOUT).upload_mesh(
            newest_per_node(records), [], reporter)
        if res.ok and not res.error:
            return SiteResult(True, res.summary())
        # The ledger keeps every batch and retries it itself; it often hands back a held batch's stored
        # error before this file's nodes are even staged. Only a definitive rejection is about the file.
        if res.responses and res.responses[-1][0] in self.MESH_DEFINITIVE:
            return SiteResult(False, res.error)
        return SiteResult(False, res.error if "retry is pending" in res.error else f"{res.error} (retry is pending)")

    def _token(self, cfg: Config, state_dir: Path, config_path: Path | None) -> str | None:
        path = Path(state_dir) / self.TOKEN_FILE
        try:
            return json.loads(path.read_text())["token"]
        except (OSError, ValueError, KeyError):
            pass
        if not (cfg.wardrift_username and cfg.wardrift_password):
            return None
        failed = Wardrift._failed_login
        if failed and failed[:2] == (cfg.wardrift_username, cfg.wardrift_password):
            raise SiteError(failed[2])  # same credentials were rejected: wait for a change
        try:
            token = wardrift.sign_in(cfg.wardrift_username, cfg.wardrift_password)["token"]
        except ValueError as e:
            Wardrift._failed_login = (cfg.wardrift_username, cfg.wardrift_password, str(e))
            raise SiteError(str(e)) from None
        Wardrift._failed_login = None
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"token": token}, fh)
        if config_path is not None:
            update_config_file(config_path, "wardrift_password", "")  # only the session is kept
        return token

    def stats(self, cfg: Config, state_dir: Path, config_path: Path | None = None) -> dict | None:
        token = self._token(cfg, state_dir, config_path)
        if token is None:
            return None
        try:
            acct = wardrift.account(token)
        except wardrift.SessionExpired:
            (Path(state_dir) / self.TOKEN_FILE).unlink(missing_ok=True)
            raise SiteError("Wardrift session expired, sign in again (Wardrift password in Set)") from None
        ch = acct.get("character") or {}
        wd = acct.get("wardrive") or {}
        life, week = wd.get("lifetime") or {}, wd.get("this_week") or {}
        label = next((k for k in self.COUNT_KEYS if k in life), "total")
        return {"user": ch.get("name") or "",
                "lines": [f"level {_n(ch.get('level'))}   {_n(ch.get('exp'))} XP",
                          f"{label} {_n(life.get(label))}   week {_n(week.get(label))}"]}


SITES = [Wigle(), Wdgwars(), Wardrift()]


def site(key: str):
    return next(s for s in SITES if s.key == key)
