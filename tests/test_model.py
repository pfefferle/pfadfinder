from datetime import datetime, timezone

from pfadfinder.model import Claim, Kind, Observation


def test_observation_defaults():
    o = Observation(Kind.WIFI, "aa:bb:cc:dd:ee:ff", "x", -50, datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert o.fix is None and o.extra == {}
    assert o.channel == 0 and o.mfgr_id is None


def test_claim_default_exclusive():
    assert Claim("uart0").exclusive is True


def test_kind_values_are_wigle_types():
    assert [k.value for k in (Kind.WIFI, Kind.BLE, Kind.BT)] == ["WIFI", "BLE", "BT"]
