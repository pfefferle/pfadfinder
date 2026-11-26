import asyncio

from pfadfinder.model import Claim, Device, Kind
from pfadfinder.sources.ble import BleSource, props_to_observation
from pfadfinder.sources.registry import all_sources
from tests.conftest import make_obs

NOW = make_obs().time
P = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"


def test_le_random_device():
    o = props_to_observation(P, {"Address": "AA:BB:CC:DD:EE:FF", "AddressType": "random",
                                 "Name": "Tile", "RSSI": -70, "Appearance": 512}, NOW)
    assert (o.kind, o.id, o.name, o.rssi, o.auth, o.channel, o.frequency) == \
        (Kind.BLE, "aa:bb:cc:dd:ee:ff", "Tile", -70, "Misc [LE]", 0, 512)


def test_classic_device_with_class():
    o = props_to_observation(P, {"Address": "AA:BB:CC:DD:EE:FF", "AddressType": "public",
                                 "Alias": "Car Kit", "RSSI": -60, "Class": 0x240404}, NOW)
    assert (o.kind, o.name, o.auth, o.frequency) == (Kind.BT, "Car Kit", "Misc [BT]", 0x240404)


def test_no_rssi_none():
    assert props_to_observation(P, {"Address": "AA:BB:CC:DD:EE:FF"}, NOW) is None


def test_mfgr_smallest_key():
    o = props_to_observation(P, {"Address": "AA:BB:CC:DD:EE:FF", "RSSI": -50,
                                 "ManufacturerData": {76: b"\x01", 6: b"\x02"}}, NOW)
    assert o.mfgr_id == 6 and o.kind == Kind.BLE


def test_address_from_path_when_missing():
    o = props_to_observation(P, {"RSSI": -50}, NOW)
    assert o.id == "aa:bb:cc:dd:ee:ff" and o.name == ""


def test_alias_equal_to_address_is_not_a_name():
    o = props_to_observation(P, {"Address": "AA:BB:CC:DD:EE:FF", "Alias": "AA-BB-CC-DD-EE-FF", "RSSI": -50}, NOW)
    assert o.name == ""


def test_registered():
    assert all_sources()["ble"] is BleSource


def test_detect_usb_first(fake_sys):
    sys = fake_sys(hcis=[("hci0", "uart"), ("hci1", "usb")])
    devs = BleSource.detect(sys)
    assert [d.path for d in devs] == ["hci1", "hci0"]
    assert devs[0].internal is False and devs[1].internal is True
    assert devs[0].claims == (Claim("hci:hci1", exclusive=False),)


class FakeAdapter:
    def __init__(self, log):
        self.log = log

    async def open(self, on_device):
        self.on_device = on_device
        self.log.append("open")

    async def start(self):
        self.log.append("start")

    async def stop(self):
        self.log.append("stop")

    async def close(self):
        self.log.append("close")


class Ctx:
    def __init__(self, intervals):
        self.intervals = list(intervals)
        self.emitted = []

    def scan_interval(self):
        return self.intervals.pop(0) if len(self.intervals) > 1 else self.intervals[0]

    def emit(self, obs):
        self.emitted.append(obs)


async def drive(intervals, sleeps):
    log = []
    src = BleSource(Device("ble", "hci0", "internal (hci0)", True, ()))
    src.adapter_factory = lambda hci: FakeAdapter(log)

    async def fake_sleep(s):
        log.append(("sleep", s))
        if sum(1 for e in log if isinstance(e, tuple)) >= sleeps:
            raise asyncio.CancelledError

    src.sleep = fake_sleep
    try:
        await src.run(Ctx(intervals))
    except asyncio.CancelledError:
        pass
    return log


async def test_duty_cycle_fast():
    log = await drive([3], 3)
    assert log == ["open", "start", ("sleep", 3), ("sleep", 3), ("sleep", 3), "close"]


async def test_duty_cycle_parked():
    log = await drive([60], 4)
    assert log == ["open", "start", ("sleep", 10), "stop", ("sleep", 50), "start", ("sleep", 10), "stop",
                   ("sleep", 50), "close"]


async def test_paused_without_fix_stops_discovery():
    log = await drive([3, None], 3)
    assert log[:4] == ["open", "start", ("sleep", 3), "stop"]


async def test_emits_from_adapter_callback():
    log = []
    src = BleSource(Device("ble", "hci0", "internal (hci0)", True, ()))
    adapter = FakeAdapter(log)
    src.adapter_factory = lambda hci: adapter
    ctx = Ctx([3])
    task = asyncio.create_task(src.run(ctx))
    await asyncio.sleep(0.01)
    adapter.on_device(P, {"Address": "AA:BB:CC:DD:EE:FF", "RSSI": -40})
    adapter.on_device(P, {"Address": "AA:BB:CC:DD:EE:FF"})  # no RSSI: ignored
    task.cancel()
    assert [o.rssi for o in ctx.emitted] == [-40]


async def test_adapter_gone_raises_source_gone():
    import pytest
    from pfadfinder.sources.base import SourceGone
    log = []
    src = BleSource(Device("ble", "hci1", "USB dongle (hci1)", False, ()))
    adapter = FakeAdapter(log)
    adapter.gone = False
    src.adapter_factory = lambda hci: adapter

    sleeps = {"n": 0}

    async def fake_sleep(s):
        adapter.gone = True
        sleeps["n"] += 1
        if sleeps["n"] > 5:
            raise AssertionError("adapter loss was not noticed")

    src.sleep = fake_sleep
    with pytest.raises(SourceGone):
        await src.run(Ctx([3]))
    assert log[-1] == "close"


async def test_failed_open_still_closes():
    import pytest
    log = []

    class Broken(FakeAdapter):
        async def open(self, on_device):
            raise RuntimeError("no adapter")

    src = BleSource(Device("ble", "hci0", "internal (hci0)", True, ()))
    src.adapter_factory = lambda hci: Broken(log)
    with pytest.raises(RuntimeError):
        await src.run(Ctx([3]))
    assert log == ["close"]


def test_device_cache_seed_add_change_remove():
    from pfadfinder.sources.ble import DeviceCache
    seen = []
    c = DeviceCache("/org/bluez/hci0", lambda path, props: seen.append((path, dict(props))))
    known = "/org/bluez/hci0/dev_11_22_33_44_55_66"
    c.seed({known: {"org.bluez.Device1": {"Address": "11:22:33:44:55:66", "Class": 0x240404, "Name": "Car"}},
            "/org/bluez/hci1/dev_X": {"org.bluez.Device1": {"Address": "x"}}})
    c.changed(known, {"RSSI": -60})
    assert seen[-1][1]["Class"] == 0x240404 and seen[-1][1]["RSSI"] == -60
    c.changed(known, {"Name": "Car 2"})  # no RSSI: no emission
    assert len(seen) == 1
    other = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
    c.added(other, {"org.bluez.Device1": {"Address": "AA:BB:CC:DD:EE:FF", "RSSI": -70}})
    assert seen[-1][0] == other
    c.removed(other, ["org.bluez.Device1"])
    assert other not in c.props and "/org/bluez/hci1/dev_X" not in c.props
    assert c.gone is False
    c.removed("/org/bluez/hci0", ["org.bluez.Adapter1"])
    assert c.gone is True
