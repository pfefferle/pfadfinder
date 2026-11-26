from datetime import timedelta

from pfadfinder.cadence import MotionTracker
from tests.conftest import make_fix

T0 = make_fix().time


def track(*points):
    """points: (seconds, speed_mps or None for no fix)"""
    m = MotionTracker()
    for sec, speed in points:
        now = T0 + timedelta(seconds=sec)
        m.update(None if speed is None else make_fix(speed_mps=speed, time=now), now)
    return m


def test_no_fix_paused():
    assert track((0, None)).interval() is None


def test_driving():
    assert track((0, 2.0)).interval() == 3


def test_walking():
    assert track((0, 1.0)).interval() == 10


def test_stationary_long():
    assert track((0, 0.1), (61, 0.1)).interval() == 60


def test_stationary_short():
    assert track((0, 0.1), (30, 0.1)).interval() == 10


def test_moving_resets_stationary():
    assert track((0, 0.1), (61, 2.0), (70, 0.1)).interval() == 10


def test_gps_time_offset_does_not_pause():
    # freshness is decided by FixSelector (receipt time); the tracker trusts the fix it gets
    m = MotionTracker()
    m.update(make_fix(speed_mps=2.0, time=T0 - timedelta(hours=3)), T0)
    assert m.interval() == 3
