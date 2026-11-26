import gzip
import json
from datetime import timedelta

from pfadfinder.config import Config
from pfadfinder.storage.db import Store
from pfadfinder.storage.session import Session
from pfadfinder.storage.throttle import RowThrottle
from pfadfinder.upload.runner import pending_count, upload_site
from pfadfinder.upload.sites import SiteResult
from tests.conftest import make_fix, make_obs

NOW = make_obs().time + timedelta(hours=1)


class FakeSite:
    def __init__(self, key, results=None):
        self.key, self.title = key, key
        self.results = list(results or [])
        self.sent = []

    def configured(self, cfg):
        return True

    def upload(self, data, filename, cfg):
        self.sent.append((filename, gzip.decompress(data).decode()))
        return self.results.pop(0) if self.results else SiteResult(True, "ok")


def closed_session(tmp_path, rows=1, ended=None, second=0):
    t = make_obs().time.replace(second=second)
    s = Session.create(tmp_path / "sessions", "drive", t, "0.1.0", RowThrottle())
    for i in range(rows):
        s.write(make_obs(id=f"00:00:00:00:00:{i:02x}"))
    s.close(ended or t)
    return s


def test_wigle_sends_each_closed_session_once(tmp_path):
    store = Store(tmp_path / "db")
    good = closed_session(tmp_path, second=1)
    closed_session(tmp_path, rows=0, second=2)
    site = FakeSite("wigle")
    upload_site(store, tmp_path / "sessions", site, Config(), NOW)
    assert [f for f, _ in site.sent] == [f"pfadfinder-{good.id}.wiglecsv.gz"]
    assert upload_site(store, tmp_path / "sessions", site, Config(), NOW) == []
    assert pending_count(store, tmp_path / "sessions", "wigle", NOW) == 0


def test_wardrift_skips_sessions_older_than_72h(tmp_path):
    store = Store(tmp_path / "db")
    old = closed_session(tmp_path, second=1, ended=NOW - timedelta(hours=73))
    import os
    stamp = (NOW - timedelta(hours=73)).timestamp()  # the age comes from the last data write
    os.utime(old.dir / "wifi-bt.wiglecsv.gz", (stamp, stamp))
    new = closed_session(tmp_path, second=2, ended=NOW - timedelta(hours=1))
    site = FakeSite("wardrift")
    assert pending_count(store, tmp_path / "sessions", "wardrift", NOW) == 1
    upload_site(store, tmp_path / "sessions", site, Config(), NOW)
    assert [f for f, _ in site.sent] == [f"pfadfinder-{new.id}.wiglecsv.gz"]
    skipped = [r for r in store.upload_status("wardrift") if r["session"] == old.id]
    assert skipped[0]["status"] == "skipped" and skipped[0]["error"] == "older than 72 h"


def test_wdgwars_sends_latest_sighting_once(tmp_path):
    store = Store(tmp_path / "db")
    t = make_obs().time
    store.record(make_obs(id="aa", rssi=-40, fix=make_fix(lat=1.0, time=t)), "s1")
    store.record(make_obs(id="aa", rssi=-60, time=t + timedelta(seconds=30),
                          fix=make_fix(lat=2.0, time=t + timedelta(seconds=30))), "s1")
    store.commit()
    site = FakeSite("wdgwars")
    assert pending_count(store, tmp_path / "sessions", "wdgwars", NOW) == 1
    upload_site(store, tmp_path / "sessions", site, Config(), NOW)
    name, csv = site.sent[0]
    rows = csv.splitlines()
    assert name.startswith("pfadfinder-networks-") and rows[0].startswith("WigleWifi-1.6") and len(rows) == 3
    assert rows[2].startswith("aa,MyNet,") and ",-60,2.0000000," in rows[2]
    upload_site(store, tmp_path / "sessions", site, Config(), NOW)
    assert len(site.sent) == 1


def test_wdgwars_resends_after_new_sighting(tmp_path):
    store = Store(tmp_path / "db")
    t = make_obs().time
    store.record(make_obs(id="aa", fix=make_fix(time=t)), "s1")
    site = FakeSite("wdgwars")
    upload_site(store, tmp_path / "sessions", site, Config(), NOW)
    store.record(make_obs(id="aa", time=NOW + timedelta(minutes=1), fix=make_fix(lat=5.0, time=NOW)), "s2")
    upload_site(store, tmp_path / "sessions", site, Config(), NOW + timedelta(minutes=2))
    assert len(site.sent) == 2 and ",5.0000000," in site.sent[1][1]


def test_wdgwars_failure_keeps_networks_pending(tmp_path):
    store = Store(tmp_path / "db")
    store.record(make_obs(id="aa"), "s1")
    site = FakeSite("wdgwars", [SiteResult(False, "could not reach WDGWars")])
    upload_site(store, tmp_path / "sessions", site, Config(), NOW)
    assert pending_count(store, tmp_path / "sessions", "wdgwars", NOW) == 1


def test_first_failure_stops_site(tmp_path):
    store = Store(tmp_path / "db")
    for second in (1, 2, 3):
        closed_session(tmp_path, second=second)
    site = FakeSite("wigle", [SiteResult(False, "WiGLE did not accept that API Name and Token")])
    res = upload_site(store, tmp_path / "sessions", site, Config(), NOW)
    assert len(site.sent) == 1 and len(res) == 1 and not res[0].ok
    statuses = [r["status"] for r in store.upload_status("wigle")]
    assert statuses == ["failed"]
    assert pending_count(store, tmp_path / "sessions", "wigle", NOW) == 3


def test_other_sites_unaffected_by_one_failing(tmp_path):
    store = Store(tmp_path / "db")
    closed_session(tmp_path, second=1, ended=NOW)
    bad, good = FakeSite("wigle", [SiteResult(False, "down")]), FakeSite("wardrift")
    upload_site(store, tmp_path / "sessions", bad, Config(), NOW)
    upload_site(store, tmp_path / "sessions", good, Config(), NOW)
    assert len(good.sent) == 1
    assert [r["status"] for r in store.upload_status("wardrift")] == ["done"]


class AircraftSite(FakeSite):
    takes_aircraft = True

    def __init__(self, key, results=None):
        super().__init__(key, results)
        self.aircraft = []

    def upload_aircraft(self, records, cfg):
        self.aircraft.append(records)
        return SiteResult(True, "ok")


def session_with_aircraft(tmp_path, wifi_rows=0, ended=None, second=0):
    from pfadfinder.model import Fix, Kind, Observation
    t = make_obs().time.replace(second=second)
    s = Session.create(tmp_path / "sessions", "sky", t, "0.1.0", RowThrottle())
    for i in range(wifi_rows):
        s.write(make_obs(id=f"00:00:00:00:00:{i:02x}"))
    s.write(Observation(Kind.AIRCRAFT, "3C6DD4", "DLH4AB", -12, t, fix=Fix(52.5, 13.4, 11000.0, 0.0, None, t, 3),
                        extra={"aircraft": {"hex": "3c6dd4", "lat": 52.5, "lon": 13.4},
                               "obs_ts": t.timestamp(), "pos_ts": t.timestamp()}))
    s.close(ended or t)
    return s


def test_aircraft_go_to_wdgwars_and_wardrift_once(tmp_path):
    store = Store(tmp_path / "db")
    session_with_aircraft(tmp_path, ended=NOW)
    for key in ("wdgwars", "wardrift"):
        site = AircraftSite(key)
        assert pending_count(store, tmp_path / "sessions", key, NOW) >= 1
        upload_site(store, tmp_path / "sessions", site, Config(), NOW)
        assert len(site.aircraft) == 1 and site.aircraft[0][0]["hex"] == "3c6dd4"
        assert "_obs_ts" in site.aircraft[0][0]
        upload_site(store, tmp_path / "sessions", site, Config(), NOW)
        assert len(site.aircraft) == 1


def test_wigle_never_gets_aircraft(tmp_path):
    store = Store(tmp_path / "db")
    session_with_aircraft(tmp_path, wifi_rows=0, ended=NOW)
    site = FakeSite("wigle")
    site.takes_aircraft = False
    assert pending_count(store, tmp_path / "sessions", "wigle", NOW) == 0
    assert upload_site(store, tmp_path / "sessions", site, Config(), NOW) == []


def test_wardrift_skips_old_aircraft(tmp_path):
    store = Store(tmp_path / "db")
    s = session_with_aircraft(tmp_path, ended=NOW - timedelta(hours=80))
    import os
    stamp = (NOW - timedelta(hours=80)).timestamp()
    os.utime(s.dir / "aircraft.jsonl.gz", (stamp, stamp))
    site = AircraftSite("wardrift")
    upload_site(store, tmp_path / "sessions", site, Config(), NOW)
    assert site.aircraft == []


def test_rejected_file_does_not_block_the_site(tmp_path):
    store = Store(tmp_path / "db")
    for second in (1, 2):
        closed_session(tmp_path, second=second, ended=NOW)
    site = FakeSite("wigle")
    site.results = [SiteResult(False, "WiGLE did not take the file: malformed"), SiteResult(True, "ok")]
    upload_site(store, tmp_path / "sessions", site, Config(), NOW)
    assert len(site.sent) == 2  # moved on to the next file
    assert sorted(r["status"] for r in store.upload_status("wigle")) == ["done", "failed"]


def test_file_rejected_three_times_is_skipped(tmp_path):
    store = Store(tmp_path / "db")
    closed_session(tmp_path, second=1, ended=NOW)
    for _ in range(3):
        site = FakeSite("wigle", [SiteResult(False, "WiGLE did not take the file: malformed")])
        upload_site(store, tmp_path / "sessions", site, Config(), NOW)
    row = store.upload_status("wigle")[0]
    assert row["status"] == "skipped" and "rejected 3 times" in row["error"]
    assert pending_count(store, tmp_path / "sessions", "wigle", NOW) == 0


def test_network_or_key_error_stops_the_site(tmp_path):
    store = Store(tmp_path / "db")
    for second in (1, 2):
        closed_session(tmp_path, second=second, ended=NOW)
    for msg in ("WiGLE did not accept that API Name and Token", "WiGLE upload failed: Connection refused"):
        site = FakeSite("wigle", [SiteResult(False, msg)])
        upload_site(store, tmp_path / "sessions", site, Config(), NOW)
        assert len(site.sent) == 1


def test_wardrift_age_from_data_not_recovery_time(tmp_path):
    import os
    store = Store(tmp_path / "db")
    s = closed_session(tmp_path, second=1, ended=NOW)  # e.g. closed on the next boot after a power loss
    old = (NOW - timedelta(hours=100)).timestamp()
    os.utime(s.dir / "wifi-bt.wiglecsv.gz", (old, old))
    assert pending_count(store, tmp_path / "sessions", "wardrift", NOW) == 0


def test_wdgwars_pending_is_one_upload_for_all_networks(tmp_path):
    store = Store(tmp_path / "db")
    for i in range(5):
        store.record(make_obs(id=f"n{i}"), "s1")
    assert pending_count(store, tmp_path / "sessions", "wdgwars", NOW) == 1


class MeshSite(AircraftSite):
    takes_mesh = True

    def __init__(self, key, results=None, mesh_results=None):
        super().__init__(key, results)
        self.mesh, self.state_dirs = [], []
        self.mesh_results = list(mesh_results or [])

    def upload_mesh(self, records, cfg, state_dir):
        self.mesh.append(records)
        self.state_dirs.append(state_dir)
        return self.mesh_results.pop(0) if self.mesh_results else SiteResult(True, "ok")


def session_with_mesh(tmp_path, ended=None, second=0):
    from pfadfinder.model import Kind, Observation
    t = make_obs().time.replace(second=second)
    s = Session.create(tmp_path / "sessions", "drive", t, "0.1.0", RowThrottle())
    node = {"node_id": "a1b2c3d4e5f60718", "network": "meshcore", "name": "Kiez", "lat": None, "lon": None,
            "heard_ts": t.timestamp()}
    s.write(Observation(Kind.MESH_NODE, "meshcore:a1b2c3d4e5f60718", "Kiez", None, t,
                        extra={"node": node, "receiver": None}))
    s.close(ended or t)
    return s


def test_every_session_file_has_a_decision_per_site():
    from pfadfinder.storage.session import DATA_FILES, WIFI_BT_FILE
    from pfadfinder.upload.runner import SESSION_FILES
    from pfadfinder.upload.sites import SITES
    assert set(DATA_FILES) == {WIFI_BT_FILE} | {f for f, _, _ in SESSION_FILES}
    for site in SITES:
        for f, flag, method in SESSION_FILES:
            assert isinstance(getattr(site, flag, None), bool), f"{site.key} must decide {flag}"
            assert not getattr(site, flag) or callable(getattr(site, method, None)), f"{site.key}.{method}"


def test_mesh_goes_to_wdgwars_and_wardrift_once(tmp_path):
    store = Store(tmp_path / "db")
    session_with_mesh(tmp_path, ended=NOW)
    for key in ("wdgwars", "wardrift"):
        site = MeshSite(key)
        assert pending_count(store, tmp_path / "sessions", key, NOW) == 1
        upload_site(store, tmp_path / "sessions", site, Config(), NOW, tmp_path / "state")
        assert len(site.mesh) == 1 and site.mesh[0][0]["node_id"] == "a1b2c3d4e5f60718"
        assert site.state_dirs == [tmp_path / "state"] and "_receiver" in site.mesh[0][0]
        upload_site(store, tmp_path / "sessions", site, Config(), NOW, tmp_path / "state")
        assert len(site.mesh) == 1 and pending_count(store, tmp_path / "sessions", key, NOW) == 0


def test_wigle_never_gets_mesh(tmp_path):
    store = Store(tmp_path / "db")
    session_with_mesh(tmp_path, ended=NOW)
    site = FakeSite("wigle")
    site.takes_aircraft = site.takes_mesh = False
    assert pending_count(store, tmp_path / "sessions", "wigle", NOW) == 0
    assert upload_site(store, tmp_path / "sessions", site, Config(), NOW, tmp_path / "state") == []


def test_wardrift_mesh_skips_older_than_72h(tmp_path):
    import os
    store = Store(tmp_path / "db")
    s = session_with_mesh(tmp_path, ended=NOW - timedelta(hours=80))
    stamp = (NOW - timedelta(hours=80)).timestamp()
    os.utime(s.dir / "mesh.jsonl.gz", (stamp, stamp))
    site = MeshSite("wardrift")
    upload_site(store, tmp_path / "sessions", site, Config(), NOW, tmp_path / "state")
    assert site.mesh == []


def test_mesh_ledger_busy_is_site_problem(tmp_path):
    store = Store(tmp_path / "db")
    session_with_mesh(tmp_path, ended=NOW)
    for msg in ("Mesh upload retry is pending", "Wardrift key changed; measurements remain queued",
                "Mesh state unavailable; preserve wardrift_mesh.json and repair/restore it: bad"):
        site = MeshSite("wardrift", mesh_results=[SiteResult(False, msg)] * 4)
        for _ in range(4):
            upload_site(store, tmp_path / "sessions", site, Config(), NOW, tmp_path / "state")
        [row] = [r for r in store.upload_status("wardrift") if r["file"] == "mesh.jsonl.gz"]
        assert row["status"] == "failed", msg  # never counted toward the 3 rejects


def test_session_file_sent_as_one_gzip_member(tmp_path):
    """The session file is many appended members; Wardrift checks the length trailer against the whole body."""
    import struct
    import zlib
    store = Store(tmp_path / "db")
    s = closed_session(tmp_path, rows=2)
    f = s.dir / "wifi-bt.wiglecsv.gz"
    f.write_bytes(f.read_bytes() + gzip.compress(b""))  # one more member, as every flush adds
    site, raw = FakeSite("wardrift"), []
    upload = site.upload
    site.upload = lambda data, name, cfg: (raw.append(data), upload(data, name, cfg))[1]
    upload_site(store, tmp_path / "sessions", site, Config(), NOW)
    [data] = raw
    d = zlib.decompressobj(31)
    text = d.decompress(data)
    assert d.unused_data == b"" and struct.unpack("<I", data[-4:])[0] == len(text)
    assert text == gzip.decompress(f.read_bytes())
