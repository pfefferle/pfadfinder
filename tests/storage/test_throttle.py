from datetime import timedelta

from pfadfinder.model import Kind
from pfadfinder.storage.throttle import RowThrottle
from tests.conftest import make_fix, make_obs

T0 = make_obs().time


def at(seconds, lat=52.52, rssi=-60, kind=Kind.WIFI):
    t = T0 + timedelta(seconds=seconds)
    return make_obs(kind=kind, rssi=rssi, time=t, fix=make_fix(lat=lat, time=t))


def test_first_sighting_written():
    assert RowThrottle().should_write(at(0))


def test_within_10s_suppressed():
    th = RowThrottle(); th.should_write(at(0))
    assert not th.should_write(at(5, rssi=-50))


def test_after_10s_not_moved_same_rssi_suppressed():
    th = RowThrottle(); th.should_write(at(0))
    assert not th.should_write(at(11))


def test_after_10s_moved_30m_written():
    th = RowThrottle(); th.should_write(at(0))
    assert th.should_write(at(11, lat=52.52 + 30 / 111_195))


def test_after_10s_rssi_plus_3_written():
    th = RowThrottle(); th.should_write(at(0))
    assert th.should_write(at(11, rssi=-57))


def test_kind_is_part_of_key():
    th = RowThrottle()
    assert th.should_write(at(0, kind=Kind.WIFI))
    assert th.should_write(at(0, kind=Kind.BLE))


def test_state_only_updates_when_written():
    th = RowThrottle(); th.should_write(at(0))
    assert not th.should_write(at(5, lat=52.53))
    # 11 s after the first row, still 1.1 km from it: written
    assert th.should_write(at(11, lat=52.53))
