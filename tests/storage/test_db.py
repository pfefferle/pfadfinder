from datetime import timedelta

from pfadfinder.model import Kind
from pfadfinder.storage.db import Store
from tests.conftest import make_fix, make_obs


def test_record_new_then_known(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    assert s.record(make_obs(), "s1") is True
    assert s.record(make_obs(), "s1") is False


def test_counts_per_session(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    s.record(make_obs(), "s1")
    s.record(make_obs(id="11:11:11:11:11:11"), "s1")
    s.record(make_obs(), "s2")
    s.record(make_obs(kind=Kind.BLE, id="22:22:22:22:22:22"), "s2")
    assert s.counts("s1") == {Kind.WIFI: (2, 2)}
    assert s.counts("s2") == {Kind.WIFI: (1, 0), Kind.BLE: (1, 1)}


def test_best_rssi_location_kept(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    s.record(make_obs(rssi=-60, fix=make_fix(lat=1.0)), "s1")
    s.record(make_obs(rssi=-40, fix=make_fix(lat=2.0)), "s1")
    s.record(make_obs(rssi=-80, fix=make_fix(lat=3.0)), "s1")
    row = s.network(Kind.WIFI, "aa:bb:cc:dd:ee:ff")
    assert row["best_rssi"] == -40 and row["best_lat"] == 2.0


def test_latest_newest_first(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    t = make_obs().time
    s.record(make_obs(id="a", time=t), "s1")
    s.record(make_obs(id="b", time=t + timedelta(seconds=5)), "s1")
    assert [r["id"] for r in s.latest(Kind.WIFI, 10)] == ["b", "a"]
    assert set(s.latest(Kind.WIFI, 1)[0]) == {"id", "name", "rssi", "last_seen", "alt_m"}


def test_upload_ledger_upsert(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    s.mark_upload("wigle", "s1", "wifi-bt.wiglecsv.gz", "pending")
    s.mark_upload("wigle", "s1", "wifi-bt.wiglecsv.gz", "done", receipt="tx1", detail={"n": 3})
    rows = s.upload_status("wigle")
    assert len(rows) == 1 and rows[0]["status"] == "done" and rows[0]["session"] == "s1"


def test_stats_roundtrip(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    assert s.get_stats("wigle") is None
    s.set_stats("wigle", {"rank": 5})
    got = s.get_stats("wigle")
    assert got["rank"] == 5 and "fetched_at" in got


def test_uncommitted_lost_on_reopen(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    s.record(make_obs(id="a"), "s1"); s.commit()
    s.record(make_obs(id="b"), "s1")
    s._conn.close()  # crash without commit
    s2 = Store(tmp_path / "db.sqlite")
    assert [r["id"] for r in s2.latest(Kind.WIFI, 10)] == ["a"]


def test_concurrent_threads_do_not_break_transactions(tmp_path):
    import threading
    s = Store(tmp_path / "db.sqlite")
    errors = []

    def writer(prefix):
        try:
            for i in range(300):
                s.record(make_obs(id=f"{prefix}{i}"), "s1")
                if i % 7 == 0:
                    s.commit()
                s.mark_upload("wigle", prefix, f"f{i}", "pending")
                s.counts("s1")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(p,)) for p in ("a", "b", "c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    s.commit()
    assert errors == []
    assert s.counts("s1") == {Kind.WIFI: (900, 900)}


def test_commit_survives_sqlite_auto_rollback(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    s.record(make_obs(id="a"), "s1")
    s._conn.execute("ROLLBACK")  # what SQLite does on its own after SQLITE_FULL
    s.commit()  # must not raise
    s.record(make_obs(id="b"), "s1")
    s.commit()
    s2 = Store(tmp_path / "db.sqlite")
    assert [r["id"] for r in s2.latest(Kind.WIFI, 10)] == ["b"]


def test_latest_sighting_kept(tmp_path):
    from datetime import timedelta
    s = Store(tmp_path / "db.sqlite")
    t = make_obs().time
    s.record(make_obs(rssi=-40, auth="[WPA2-PSK-CCMP][ESS]", channel=6, frequency=2437,
                      fix=make_fix(lat=1.0, time=t)), "s1")
    s.record(make_obs(rssi=-70, auth="[WPA3-SAE-CCMP][ESS]", channel=11, frequency=2462, time=t + timedelta(seconds=30),
                      fix=make_fix(lat=2.0, alt_m=50.0, accuracy_m=3.0, time=t + timedelta(seconds=30))), "s1")
    row = s.network(Kind.WIFI, "aa:bb:cc:dd:ee:ff")
    assert (row["best_rssi"], row["best_lat"]) == (-40, 1.0)
    assert (row["last_rssi"], row["last_lat"], row["last_alt"], row["last_acc"]) == (-70, 2.0, 50.0, 3.0)
    assert (row["auth"], row["channel"], row["frequency"]) == ("[WPA3-SAE-CCMP][ESS]", 11, 2462)


def test_wdgwars_pending_and_sent(tmp_path):
    from datetime import timedelta
    s = Store(tmp_path / "db.sqlite")
    t = make_obs().time
    s.record(make_obs(id="a", time=t), "s1")
    s.record(make_obs(id="b", time=t), "s1")
    assert {r["id"] for r in s.wdgwars_pending()} == {"a", "b"}
    s.mark_wdgwars_sent([("WIFI", "a"), ("WIFI", "b")], (t + timedelta(seconds=1)).isoformat(timespec="seconds"))
    assert s.wdgwars_pending() == []
    s.record(make_obs(id="a", time=t + timedelta(minutes=5), fix=make_fix(lat=3.0, time=t + timedelta(minutes=5))), "s2")
    assert [(r["id"], r["last_lat"]) for r in s.wdgwars_pending()] == [("a", 3.0)]


def test_existing_db_gets_new_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite"
    c = sqlite3.connect(path)
    c.executescript("""CREATE TABLE networks (kind TEXT NOT NULL, id TEXT NOT NULL, name TEXT, first_seen TEXT,
        last_seen TEXT, best_rssi INTEGER, best_lat REAL, best_lon REAL, first_session TEXT,
        wdgwars_uploaded_at TEXT, PRIMARY KEY (kind, id));
        INSERT INTO networks (kind, id, name, last_seen) VALUES ('WIFI', 'x', 'old', '2026-01-01T00:00:00+00:00');""")
    c.commit(); c.close()
    s = Store(path)
    assert s.network(Kind.WIFI, "x")["last_lat"] is None
    s.record(make_obs(id="y"), "s1")
    assert s.network(Kind.WIFI, "y")["last_lat"] == 52.52


def test_wdgwars_pending_excludes_aircraft(tmp_path):
    from pfadfinder.model import Fix, Observation
    s = Store(tmp_path / "db.sqlite")
    t = make_obs().time
    s.record(Observation(Kind.AIRCRAFT, "3C6DD4", "DLH4AB", -12, t, fix=Fix(52.5, 13.4, 11000.0, 0.0, None, t, 3)), "s1")
    s.record(make_obs(id="aa"), "s1")
    assert [r["id"] for r in s.wdgwars_pending()] == ["aa"]
    assert s.counts("s1")[Kind.AIRCRAFT] == (1, 1)


def test_mesh_record_without_fix(tmp_path):
    from pfadfinder.model import Observation
    s = Store(tmp_path / "db.sqlite")
    t = make_obs().time
    assert s.record(Observation(Kind.MESH_NODE, "meshcore:a1b2c3d4e5f60718", "Kiez", None, t), "s1") is True
    row = s.network(Kind.MESH_NODE, "meshcore:a1b2c3d4e5f60718")
    assert row["last_lat"] is None and row["name"] == "Kiez"
    assert s.counts("s1")[Kind.MESH_NODE] == (1, 1)


def test_wdgwars_pending_excludes_mesh(tmp_path):
    from pfadfinder.model import Fix, Observation
    s = Store(tmp_path / "db.sqlite")
    t = make_obs().time
    s.record(Observation(Kind.MESH_NODE, "meshcore:a1b2c3d4e5f60718", "Kiez", None, t,
                         fix=Fix(52.5, 13.4, None, 0.0, None, t, 2)), "s1")
    s.record(make_obs(id="aa"), "s1")
    assert [r["id"] for r in s.wdgwars_pending()] == ["aa"]
