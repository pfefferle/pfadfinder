import json
import logging
import stat

import pytest
from wdgwars_uploader import uploader, wardrift, wigle

from pfadfinder.config import Config, ensure_config, load_config
from pfadfinder.upload import sites
from pfadfinder.upload.sites import SITES, SiteResult, site


def test_sites_in_fixed_order():
    assert [s.key for s in SITES] == ["wigle", "wdgwars", "wardrift"]
    assert [s.title for s in SITES] == ["WiGLE", "WDGWars", "Wardrift"]


def test_configured():
    assert not site("wigle").configured(Config(wigle_api_name="a"))
    assert site("wigle").configured(Config(wigle_api_name="a", wigle_api_token="b"))
    assert site("wdgwars").configured(Config(wdgwars_api_key="k"))
    assert site("wardrift").configured(Config(wardrift_api_key="k"))


# -- WiGLE ---------------------------------------------------------------------
def test_wigle_upload(monkeypatch):
    seen = {}

    def fake(name, token, data, filename):
        seen.update(name=name, token=token, data=data, filename=filename)
        return True, "uploaded as m (tx1)"

    monkeypatch.setattr(wigle, "upload", fake)
    res = site("wigle").upload(b"gz", "pfadfinder-s1.wiglecsv.gz", Config(wigle_api_name="AID", wigle_api_token="t"))
    assert res == SiteResult(True, "uploaded as m (tx1)")
    assert seen == {"name": "AID", "token": "t", "data": b"gz", "filename": "pfadfinder-s1.wiglecsv.gz"}


def test_wigle_stats(monkeypatch, tmp_path):
    monkeypatch.setattr(wigle, "user_stats", lambda n, t: {"ok": True, "name": "matthias", "rank": 1234,
                                                          "month_rank": 56, "wifi_gps": 10432, "bt": 2101, "cell": 3})
    st = site("wigle").stats(Config(wigle_api_name="a", wigle_api_token="b"), tmp_path)
    assert st == {"user": "matthias", "lines": ["rank 1 234   month 56", "WiFi 10 432   BT 2 101   Cell 3"]}


def test_wigle_stats_failure_is_none(monkeypatch, tmp_path):
    monkeypatch.setattr(wigle, "user_stats", lambda n, t: {"ok": False, "error": "nope"})
    assert site("wigle").stats(Config(wigle_api_name="a", wigle_api_token="b"), tmp_path) is None


# -- WDGWars -------------------------------------------------------------------
class FakeWdg:
    def __init__(self, api_key, **kw):
        self.api_key = api_key

    def upload_wardrive_csv(self, gz, name):
        return True, "imported=3"

    def get_stats(self):
        return {"ok": True, "username": "pfefferle", "wifi": 1500, "ble": 300, "aircraft": 69, "mesh": 41,
                "total": 1910}  # field names as returned by /api/me on the device; there is no rank


def test_wdgwars_upload_and_stats(monkeypatch, tmp_path):
    monkeypatch.setattr(uploader, "WDGWarsClient", FakeWdg)
    cfg = Config(wdgwars_api_key="k" * 64)
    assert site("wdgwars").upload(b"gz", "f.gz", cfg) == SiteResult(True, "imported=3")
    assert site("wdgwars").stats(cfg, tmp_path) == {"user": "pfefferle",
                                                    "lines": ["WiFi 1 500   BLE 300",
                                                              "aircraft 69   mesh 41   total 1 910"]}


def test_wdgwars_stats_missing_fields(monkeypatch, tmp_path):
    class Sparse(FakeWdg):
        def get_stats(self):
            return {"wifi": 5}

    monkeypatch.setattr(uploader, "WDGWarsClient", Sparse)
    assert site("wdgwars").stats(Config(wdgwars_api_key="k"), tmp_path) == \
        {"user": "", "lines": ["WiFi 5   BLE —", "aircraft —   mesh —   total —"]}
    monkeypatch.setattr(uploader, "WDGWarsClient", type("Empty", (FakeWdg,), {"get_stats": lambda self: {}}))
    assert site("wdgwars").stats(Config(wdgwars_api_key="k"), tmp_path) is None


# -- Wardrift ------------------------------------------------------------------
class FakeWardrift:
    def __init__(self, api_key, **kw):
        self.api_key = api_key

    def upload_csv(self, gz, name):
        return True, "12 accepted, +30 XP"


ACCOUNT = {"character": {"name": "Pfadfinder", "level": 7, "exp": 1234},
           "wardrive": {"lifetime": {"signals": 5000}, "this_week": {"signals": 120}, "streak_days": 3}}


def wardrift_cfg(tmp_path, password="s3cr3t"):
    path = tmp_path / "config.toml"
    ensure_config(path)
    from pfadfinder.config import update_config_file
    update_config_file(path, "wardrift_api_key", "wk")
    update_config_file(path, "wardrift_username", "pfefferle")
    update_config_file(path, "wardrift_password", password)
    return path, load_config(path)


def test_wardrift_upload(monkeypatch):
    monkeypatch.setattr(wardrift, "WardriftClient", FakeWardrift)
    assert site("wardrift").upload(b"gz", "f.gz", Config(wardrift_api_key="wk")) == \
        SiteResult(True, "12 accepted, +30 XP")


def test_wardrift_sign_in_stores_token_and_clears_password(monkeypatch, tmp_path):
    path, cfg = wardrift_cfg(tmp_path)
    calls = []
    monkeypatch.setattr(wardrift, "sign_in", lambda u, p: calls.append((u, p)) or {"token": "TOKEN"})
    monkeypatch.setattr(wardrift, "account", lambda token: ACCOUNT if token == "TOKEN" else {})
    st = site("wardrift").stats(cfg, tmp_path / "state", config_path=path)
    assert st == {"user": "Pfadfinder", "lines": ["level 7   1 234 XP", "signals 5 000   week 120"]}
    assert calls == [("pfefferle", "s3cr3t")]
    token_file = tmp_path / "state" / "wardrift.json"
    assert json.loads(token_file.read_text()) == {"token": "TOKEN"}
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert load_config(path).wardrift_password == ""
    # second call uses the stored token, no new sign-in
    site("wardrift").stats(load_config(path), tmp_path / "state", config_path=path)
    assert len(calls) == 1


def test_wardrift_bad_password_keeps_password(monkeypatch, tmp_path):
    path, cfg = wardrift_cfg(tmp_path)

    def bad(u, p):
        raise ValueError("Wardrift did not accept that username and password")

    monkeypatch.setattr(wardrift, "sign_in", bad)
    with pytest.raises(sites.SiteError, match="did not accept"):
        site("wardrift").stats(cfg, tmp_path / "state", config_path=path)
    assert load_config(path).wardrift_password == "s3cr3t"


def test_wardrift_expired_token_dropped(monkeypatch, tmp_path):
    path, cfg = wardrift_cfg(tmp_path, password="")
    state = tmp_path / "state"
    state.mkdir()
    (state / "wardrift.json").write_text('{"token": "OLD"}')

    def expired(token):
        raise wardrift.SessionExpired()

    monkeypatch.setattr(wardrift, "account", expired)
    with pytest.raises(sites.SiteError, match="sign in again"):
        site("wardrift").stats(cfg, state, config_path=path)
    assert not (state / "wardrift.json").exists()


def test_wardrift_without_login_has_no_stats(tmp_path):
    assert site("wardrift").stats(Config(wardrift_api_key="wk"), tmp_path) is None


def test_secrets_never_logged(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(wigle, "upload", lambda *a: (False, "WiGLE did not accept that API Name and Token"))
    monkeypatch.setattr(uploader, "WDGWarsClient", FakeWdg)
    monkeypatch.setattr(wardrift, "WardriftClient", FakeWardrift)
    path, _ = wardrift_cfg(tmp_path)
    monkeypatch.setattr(wardrift, "sign_in", lambda u, p: {"token": "TOKEN-SECRET"})
    monkeypatch.setattr(wardrift, "account", lambda t: ACCOUNT)
    cfg = Config(wigle_api_name="AID", wigle_api_token="WIGLE-SECRET", wdgwars_api_key="WDG-SECRET",
                 wardrift_api_key="WD-SECRET", wardrift_username="u", wardrift_password="PW-SECRET")
    for s in SITES:
        s.upload(b"gz", "f.gz", cfg)
    site("wardrift").stats(cfg, tmp_path / "state", config_path=path)
    for secret in ("WIGLE-SECRET", "WDG-SECRET", "WD-SECRET", "PW-SECRET", "TOKEN-SECRET"):
        assert secret not in caplog.text


RECORDS = [{"hex": "3c6dd4", "flight": "DLH4AB", "lat": 52.5, "lon": 13.4, "alt_baro": 37000,
            "_obs_ts": 1795100000.0, "_pos_ts": 1795099999.5}]


def test_takes_aircraft_flags():
    assert [s.takes_aircraft for s in SITES] == [False, True, True]


def test_wdgwars_aircraft_upload(monkeypatch):
    got = {}

    class Wdg(FakeWdg):
        def upload_aircraft_chunked(self, aircraft, chunk=None):
            got["aircraft"] = aircraft
            return uploader.UploadResult(True, {"result": {"aircraft_imported": 1}})

    monkeypatch.setattr(uploader, "WDGWarsClient", Wdg)
    res = site("wdgwars").upload_aircraft(RECORDS, Config(wdgwars_api_key="k"))
    assert res.ok and got["aircraft"] == RECORDS and "aircraft_imported=1" in res.message


def test_wardrift_aircraft_upload_keeps_timestamps(monkeypatch):
    got = {}

    class Wd(FakeWardrift):
        def upload_aircraft(self, records):
            got["records"] = records
            return wardrift.WardriftResult(ok=True, sent=list(records), credited=1, exp=5)

    monkeypatch.setattr(wardrift, "WardriftClient", Wd)
    res = site("wardrift").upload_aircraft(RECORDS, Config(wardrift_api_key="k"))
    assert res.ok and got["records"][0]["_obs_ts"] == 1795100000.0 and "1 sent" in res.message


def test_wardrift_aircraft_failure(monkeypatch):
    class Wd(FakeWardrift):
        def upload_aircraft(self, records):
            return wardrift.WardriftResult(ok=False, error="HTTP 503")

    monkeypatch.setattr(wardrift, "WardriftClient", Wd)
    assert site("wardrift").upload_aircraft(RECORDS, Config(wardrift_api_key="k")) == SiteResult(False, "HTTP 503")


def test_wardrift_wrong_password_not_retried(monkeypatch, tmp_path):
    path, cfg = wardrift_cfg(tmp_path)
    calls = []

    def bad(u, p):
        calls.append(p)
        raise ValueError("Wardrift did not accept that username and password")

    monkeypatch.setattr(wardrift, "sign_in", bad)
    monkeypatch.setattr(sites.Wardrift, "_failed_login", None, raising=False)
    for _ in range(3):
        with pytest.raises(sites.SiteError, match="did not accept"):
            site("wardrift").stats(cfg, tmp_path / "state", config_path=path)
    assert calls == ["s3cr3t"]
    from dataclasses import replace
    with pytest.raises(sites.SiteError):
        site("wardrift").stats(replace(cfg, wardrift_password="fixed"), tmp_path / "state", config_path=path)
    assert calls == ["s3cr3t", "fixed"]  # a new password is tried


# -- mesh nodes ------------------------------------------------------------------
def mesh_line(node_id="a1b2c3d4e5f60718", network="meshcore", lat=52.5, lon=13.4, heard=1795100000.0, name="Kiez"):
    line = {"node_id": node_id, "node_type": "REPEATER", "name": name, "lat": lat, "lon": lon, "rssi": None,
            "first_seen": "2026-11-19 12:00:00", "heard_ts": heard, "type": "MESHCORE", "network": network,
            "uploadable": lat is not None, "tele": {"snr": 7.5}, "_obs_ts": heard, "_receiver": None}
    if network == "meshcore":
        line["public_key"] = node_id + "0" * 48
    return line


def test_takes_mesh_flags():
    assert [s.takes_mesh for s in SITES] == [False, True, True]


def test_wdgwars_mesh_both_networks_with_position_only(monkeypatch, tmp_path):
    got = {}

    class Wdg(FakeWdg):
        def upload_json_hmac(self, payload):
            got["payload"] = payload
            return uploader.UploadResult(True, {"meshcore_imported": 1, "meshcore_already_seen": 0})

    monkeypatch.setattr(uploader, "WDGWarsClient", Wdg)
    records = [mesh_line(), mesh_line(node_id="b" * 16, lat=None, lon=None), mesh_line(node_id="deadbeef", network="meshtastic")]
    res = site("wdgwars").upload_mesh(records, Config(wdgwars_api_key="k"), tmp_path)
    p = got["payload"]
    assert res.ok and "imported 1" in res.message
    assert p["networks"] == [] and p["aircraft"] == []
    assert [n["node_id"] for n in p["meshcore_nodes"]] == ["a1b2c3d4e5f60718", "deadbeef"]  # the server sorts by network
    assert [n["network"] for n in p["meshcore_nodes"]] == ["meshcore", "meshtastic"]


def test_wdgwars_mesh_fields_whitelisted(monkeypatch, tmp_path):
    got = {}

    class Wdg(FakeWdg):
        def upload_json_hmac(self, payload):
            got["payload"] = payload
            return uploader.UploadResult(True, {})

    monkeypatch.setattr(uploader, "WDGWarsClient", Wdg)
    site("wdgwars").upload_mesh([mesh_line()], Config(wdgwars_api_key="k"), tmp_path)
    assert set(got["payload"]["meshcore_nodes"][0]) == {
        "node_id", "node_type", "name", "lat", "lon", "rssi", "first_seen", "type", "network", "public_key"}


def test_wdgwars_mesh_nothing_to_send(monkeypatch, tmp_path):
    class Wdg(FakeWdg):
        def upload_json_hmac(self, payload):
            raise AssertionError("nothing should be sent")

    monkeypatch.setattr(uploader, "WDGWarsClient", Wdg)
    res = site("wdgwars").upload_mesh([mesh_line(network="meshtastic", node_id="deadbeef", lat=None, lon=None)], Config(wdgwars_api_key="k"),
                                      tmp_path)
    assert res == SiteResult(True, "no mesh nodes with a position")


def test_mesh_newest_line_per_node(monkeypatch, tmp_path):
    got = {}

    class Wdg(FakeWdg):
        def upload_json_hmac(self, payload):
            got["payload"] = payload
            return uploader.UploadResult(True, {})

    monkeypatch.setattr(uploader, "WDGWarsClient", Wdg)
    records = [mesh_line(heard=100.0, lat=52.5), mesh_line(heard=300.0, lat=52.6), mesh_line(heard=200.0, lat=52.7)]
    site("wdgwars").upload_mesh(records, Config(wdgwars_api_key="k"), tmp_path)
    [n] = got["payload"]["meshcore_nodes"]
    assert n["lat"] == 52.6


def test_wardrift_mesh_uses_reporter_in_state_dir(monkeypatch, tmp_path):
    got = {}

    class Wd(FakeWardrift):
        def upload_mesh(self, nodes, radios, reporter, **kw):
            got.update(nodes=nodes, radios=radios, path=reporter.path)
            return wardrift.MeshResult(ok=True, nodes=len(nodes))

    monkeypatch.setattr(wardrift, "WardriftClient", Wd)
    records = [mesh_line(), mesh_line(heard=1795100100.0), mesh_line(node_id="deadbeef", network="meshtastic", lat=None)]
    res = site("wardrift").upload_mesh(records, Config(wardrift_api_key="k"), tmp_path)
    assert res.ok and "2 nodes acknowledged" in res.message
    assert got["path"] == tmp_path / "wardrift_mesh.json" and got["radios"] == []
    assert sorted((n["network"], n["heard_ts"]) for n in got["nodes"]) == [
        ("meshcore", 1795100100.0), ("meshtastic", 1795100000.0)]


def test_wardrift_mesh_failure(monkeypatch, tmp_path):
    class Wd(FakeWardrift):
        def upload_mesh(self, nodes, radios, reporter, **kw):
            return wardrift.MeshResult(ok=False, error="Mesh upload retry is pending")

    monkeypatch.setattr(wardrift, "WardriftClient", Wd)
    res = site("wardrift").upload_mesh([mesh_line()], Config(wardrift_api_key="k"), tmp_path)
    assert res == SiteResult(False, "Mesh upload retry is pending")


@pytest.mark.parametrize("status,error,file_problem", [
    (422, "HTTP 422 invalid node", True), (400, "HTTP 400", True), (413, "HTTP 413", True),
    (408, "HTTP 408", False), (200, "HTTP 200 batch not accepted", False), (None, "Mesh account verification failed: SSLError", False),
    (None, "Mesh delivery queue full; try later", False)])
def test_wardrift_mesh_only_definitive_rejects_count_against_the_file(monkeypatch, tmp_path, status, error, file_problem):
    from pfadfinder.upload.runner import _site_problem

    class Wd(FakeWardrift):
        def upload_mesh(self, nodes, radios, reporter, **kw):
            return wardrift.MeshResult(ok=False, error=error, responses=[(status, "{}")] if status else [])

    monkeypatch.setattr(wardrift, "WardriftClient", Wd)
    res = site("wardrift").upload_mesh([mesh_line()], Config(wardrift_api_key="k"), tmp_path)
    assert not res.ok and error in res.message
    assert _site_problem(res.message) is not file_problem
