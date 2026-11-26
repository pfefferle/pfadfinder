import gzip
import json

import pytest

from pfadfinder.model import Kind
from pfadfinder.storage.session import WRITER_KINDS, Session
from pfadfinder.storage.throttle import RowThrottle
from tests.conftest import make_obs


def new(tmp_path):
    return Session.create(tmp_path, "drive", make_obs().time, "0.1.0", RowThrottle())


def test_writer_kinds():
    assert WRITER_KINDS == frozenset({Kind.WIFI, Kind.BLE, Kind.BT, Kind.AIRCRAFT, Kind.MESH_NODE})


def test_create_writes_header_and_meta(tmp_path):
    s = new(tmp_path)
    assert s.id == "20261119T120005Z" and s.dir == tmp_path / s.id
    meta = json.loads((s.dir / "session.json").read_text())
    assert meta == {"id": s.id, "mode": "drive", "started": "2026-11-19T12:00:05+00:00", "ended": None}
    text = gzip.open(s.dir / "wifi-bt.wiglecsv.gz", "rt").read()
    assert text.startswith("WigleWifi-1.6,") and len(text.splitlines()) == 2


def test_write_and_close(tmp_path):
    s = new(tmp_path)
    assert s.write(make_obs()) is True
    assert s.write(make_obs()) is False  # throttled
    s.close(make_obs().time)
    lines = gzip.open(s.dir / "wifi-bt.wiglecsv.gz", "rt").read().splitlines()
    assert len(lines) == 3 and lines[2].startswith("aa:bb:cc:dd:ee:ff,MyNet")
    assert json.loads((s.dir / "session.json").read_text())["ended"] == "2026-11-19T12:00:05+00:00"


def test_unclosed_detected_and_closed(tmp_path):
    s = new(tmp_path)
    s.write(make_obs()); s.flush()
    assert Session.unclosed(tmp_path) == [s.dir]
    Session.close_stale(s.dir, make_obs().time)
    assert Session.unclosed(tmp_path) == []


def test_obs_without_fix_rejected(tmp_path):
    with pytest.raises(ValueError):
        new(tmp_path).write(make_obs(fix=None))


def test_close_stale_repairs_torn_file(tmp_path):
    s = new(tmp_path)
    s.write(make_obs()); s.flush()
    f = s.dir / "wifi-bt.wiglecsv.gz"
    with f.open("ab") as fh:
        fh.write(gzip.compress(b"half a member")[:7])
    Session.close_stale(s.dir, make_obs().time)
    assert len(gzip.open(f, "rt").read().splitlines()) == 3


def aircraft_obs(hex="3c6dd4", lat=52.5, lon=13.4, t=None, flight="DLH4AB  "):
    from pfadfinder.model import Fix, Observation
    t = t or make_obs().time
    ac = {"hex": hex, "flight": flight, "lat": lat, "lon": lon, "alt_baro": 37000, "gs": 450.2, "track": 92.1,
          "rssi": -12.3, "seen": 0.4, "seen_pos": 0.9}
    fix = None if lat is None else Fix(lat, lon, 11277.6, 0.0, None, t, 3)
    return Observation(Kind.AIRCRAFT, hex.upper(), flight.strip(), -12, t, fix=fix,
                       extra={"aircraft": ac, "obs_ts": t.timestamp() - 0.4, "pos_ts": t.timestamp() - 0.9})


def test_aircraft_written_as_jsonl(tmp_path):
    s = new(tmp_path)
    assert s.write(aircraft_obs()) is True
    s.close(make_obs().time)
    lines = gzip.open(s.dir / "aircraft.jsonl.gz", "rt").read().splitlines()
    rec = json.loads(lines[0])
    assert len(lines) == 1 and rec["hex"] == "3c6dd4" and rec["alt_baro"] == 37000
    assert rec["_obs_ts"] == make_obs().time.timestamp() - 0.4 and rec["_pos_ts"] == make_obs().time.timestamp() - 0.9


def test_aircraft_throttle_30s_or_1km(tmp_path):
    from datetime import timedelta
    s = new(tmp_path)
    t = make_obs().time
    assert s.write(aircraft_obs(t=t))
    assert not s.write(aircraft_obs(t=t + timedelta(seconds=10), lat=52.5005))   # 10 s, ~55 m
    assert s.write(aircraft_obs(t=t + timedelta(seconds=15), lat=52.52))         # moved ~2.2 km
    assert s.write(aircraft_obs(t=t + timedelta(seconds=46), lat=52.52))         # 31 s later


def test_aircraft_without_position_not_written(tmp_path):
    s = new(tmp_path)
    assert s.write(aircraft_obs(lat=None)) is False
    s.close(make_obs().time)
    f = s.dir / "aircraft.jsonl.gz"
    assert not f.exists() or gzip.open(f, "rt").read() == ""


def test_wifi_file_has_no_aircraft(tmp_path):
    s = new(tmp_path)
    s.write(aircraft_obs()); s.write(make_obs())
    s.close(make_obs().time)
    rows = gzip.open(s.dir / "wifi-bt.wiglecsv.gz", "rt").read().splitlines()
    assert len(rows) == 3 and "AIRCRAFT" not in rows[2]


def mesh_obs(node_id="a1b2c3d4e5f60718", lat=52.5, lon=13.4, t=None, name="Kiez-Rptr", receiver="default"):
    from pfadfinder.model import Fix, Observation
    t = t or make_obs().time
    node = {"node_id": node_id, "node_type": "REPEATER", "name": name, "lat": lat, "lon": lon, "rssi": None,
            "first_seen": "2026-11-19 12:00:00", "heard_ts": t.timestamp(), "type": "MESHCORE",
            "network": "meshcore", "uploadable": lat is not None, "public_key": node_id + "0" * 48}
    fix = None if lat is None else Fix(lat, lon, None, 0.0, None, t, 2)
    rx = make_obs().fix if receiver == "default" else receiver
    return Observation(Kind.MESH_NODE, f"meshcore:{node_id}", name, None, t, fix=fix,
                       extra={"node": node, "receiver": rx})


def _mesh_lines(s):
    f = s.dir / "mesh.jsonl.gz"
    return [json.loads(x) for x in gzip.open(f, "rt").read().splitlines()] if f.exists() else []


def test_mesh_written_as_jsonl(tmp_path):
    s = new(tmp_path)
    assert s.write(mesh_obs()) is True
    s.close(make_obs().time)
    [rec] = _mesh_lines(s)
    assert rec["node_id"] == "a1b2c3d4e5f60718" and rec["network"] == "meshcore" and rec["lat"] == 52.5
    assert rec["_obs_ts"] == make_obs().time.timestamp()
    fix = make_obs().fix
    assert rec["_receiver"] == {"lat": fix.lat, "lon": fix.lon, "acc": fix.accuracy_m, "time": fix.time.timestamp()}


def test_mesh_without_position_written(tmp_path):
    s = new(tmp_path)
    assert s.write(mesh_obs(lat=None, lon=None, receiver=None)) is True
    s.close(make_obs().time)
    [rec] = _mesh_lines(s)
    assert rec["lat"] is None and rec["uploadable"] is False and rec["_receiver"] is None


def test_mesh_throttle_60s_100m_or_new_position(tmp_path):
    from datetime import timedelta
    s = new(tmp_path)
    t = make_obs().time
    assert s.write(mesh_obs(lat=None, lon=None, t=t))
    assert s.write(mesh_obs(t=t + timedelta(seconds=5)))                       # position appeared
    assert not s.write(mesh_obs(t=t + timedelta(seconds=10), lat=52.5005))     # ~55 m
    assert s.write(mesh_obs(t=t + timedelta(seconds=15), lat=52.502))          # ~220 m
    assert s.write(mesh_obs(t=t + timedelta(seconds=20), lat=52.502, name="Kiez-2"))  # new name
    assert not s.write(mesh_obs(t=t + timedelta(seconds=40), lat=52.502, name="Kiez-2"))
    assert s.write(mesh_obs(t=t + timedelta(seconds=81), lat=52.502, name="Kiez-2"))  # 61 s later


def test_mesh_stale_session_repaired(tmp_path):
    s = new(tmp_path)
    s.write(mesh_obs()); s.flush()
    f = s.dir / "mesh.jsonl.gz"
    with f.open("ab") as fh:
        fh.write(gzip.compress(b"half a member")[:7])
    Session.close_stale(s.dir, make_obs().time)
    assert len(gzip.open(f, "rt").read().splitlines()) == 1


def test_wifi_file_has_no_mesh(tmp_path):
    s = new(tmp_path)
    s.write(mesh_obs()); s.write(make_obs())
    s.close(make_obs().time)
    rows = gzip.open(s.dir / "wifi-bt.wiglecsv.gz", "rt").read().splitlines()
    assert len(rows) == 3 and "MESH" not in rows[2]
