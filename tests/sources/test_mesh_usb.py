import asyncio
import os
import threading
import time

import pytest

from pfadfinder.model import Claim, Device, Kind
from pfadfinder.sources import mesh_usb
from pfadfinder.sources.base import SourceGone, SysInfo
from pfadfinder.sources.mesh_usb import MeshUsbSource
from pfadfinder.sources.registry import all_sources
from tests.conftest import make_fix


def fake_tty(root, name, busid, vid, pid, serial="", devnum="5", product="Heltec"):
    usb = root / "sys/bus/usb/devices" / busid
    (usb / f"{busid}:1.0").mkdir(parents=True, exist_ok=True)
    for f, v in (("idVendor", vid), ("idProduct", pid), ("devnum", devnum), ("product", product)):
        (usb / f).write_text(v + "\n")
    if serial:
        (usb / "serial").write_text(serial + "\n")
    tty = root / "sys/class/tty" / name
    tty.mkdir(parents=True, exist_ok=True)
    link = tty / "device"
    if link.is_symlink():
        link.unlink()
    os.symlink(os.path.relpath(usb / f"{busid}:1.0", tty), link)


def unplug(root, name, busid):
    import shutil
    shutil.rmtree(root / "sys/class/tty" / name)
    shutil.rmtree(root / "sys/bus/usb/devices" / busid)


def test_registered():
    assert all_sources()["mesh-usb"] is MeshUsbSource


def test_usb_serial_ports(tmp_path):
    fake_tty(tmp_path, "ttyACM0", "1-1.2", "303a", "1001", serial="AB12")
    (tmp_path / "sys/class/tty/ttyS0").mkdir(parents=True)  # no USB device behind it
    [p] = SysInfo(tmp_path).usb_serial_ports()
    assert (p.dev, p.name, p.busid, p.vendor, p.product, p.serial, p.devnum, p.product_name) == \
        ("/dev/ttyACM0", "ttyACM0", "1-1.2", "303a", "1001", "AB12", "5", "Heltec")


def test_detect_mesh_usb_ids(tmp_path):
    fake_tty(tmp_path, "ttyUSB0", "1-1.1", "10c4", "ea60")           # CP210x, Heltec V3
    fake_tty(tmp_path, "ttyACM0", "1-1.2", "1a86", "55d4")           # CH9102, T-Beam
    fake_tty(tmp_path, "ttyACM1", "1-1.3", "303a", "1001")           # ESP32-S3 native
    fake_tty(tmp_path, "ttyACM2", "1-1.4", "239a", "8029")           # RAK4631
    fake_tty(tmp_path, "ttyACM3", "1-1.5", "1546", "01a7")           # u-blox GPS
    devs = MeshUsbSource.detect(SysInfo(tmp_path))
    assert [d.path for d in devs] == ["/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyACM2", "/dev/ttyUSB0"]
    assert devs[0].claims == (Claim("usb:1-1.2"),) and devs[0].label == "Heltec (ttyACM0)"
    assert devs[0].internal is False and devs[0].source == "mesh-usb"


class FakeWorker:
    """Stands in for the reference MeshCoreWorker/MeshtasticWorker (threads that fill a MeshStore)."""

    def __init__(self, network, port, store, *, answers=True, nodes=(), hang=False, slow=False):
        self.network, self.port, self.store = network, port, store
        self.answers, self.nodes, self.hang, self.slow = answers, list(nodes), hang, slow
        self.connected, self.error = False, ""
        self.started = self.stopped = False
        self._alive = False

    def start(self):
        self.started = self._alive = True
        if self.slow:
            return  # still connecting
        if self.answers:
            self.connected = True
            for n in self.nodes:
                self.store.put(dict(n))
        else:
            self.error = "could not connect"

    def stop(self):
        self.stopped = True
        if not self.hang:
            self._alive = False

    def join(self, timeout=None):
        if self.hang:
            time.sleep(min(timeout or 0, 0.2))

    def is_alive(self):
        return self._alive


def node(node_id="a1b2c3d4e5f60718", network="meshcore", lat=52.5, lon=13.4, name="Kiez", heard=1_763_553_600.0):
    return {"node_id": node_id, "node_type": "REPEATER", "name": name, "lat": lat, "lon": lon, "rssi": None,
            "first_seen": "2025-11-19 12:00:00", "heard_ts": heard, "type": "MESHCORE", "network": network,
            "uploadable": lat is not None}


class Ctx:
    def __init__(self, fix=None):
        self.emitted, self.errors, self._fix = [], [], fix

    def fix(self):
        return self._fix

    def emit(self, obs):
        self.emitted.append(obs)

    def report_error(self, source, message):
        self.errors.append(message)


@pytest.fixture(autouse=True)
def fresh_memo():
    mesh_usb.MeshUsbSource._firmware.clear()
    mesh_usb.MeshUsbSource._no_firmware.clear()
    mesh_usb.MeshUsbSource._stuck.clear()


def make_source(root, factory, port="ttyACM0", busid="1-1.2", serial="AB12"):
    fake_tty(root, port, busid, "303a", "1001", serial=serial)
    src = MeshUsbSource(Device("mesh-usb", f"/dev/{port}", f"Heltec ({port})", False, (Claim(f"usb:{busid}"),)))
    src.sysroot, src.worker_factory, src.poll_s = root, factory, 0.02
    src.firmware_probe = staticmethod(lambda path: False)
    src.probe_s = {"meshcore": 0.2, "meshtastic": 0.2}
    return src


async def run_for(src, ctx, seconds):
    task = asyncio.create_task(src.run(ctx))
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return task


def factory_for(answering, made, nodes=(), hang=False, slow=False):
    def factory(network, port, store):
        w = FakeWorker(network, port, store, answers=network == answering, nodes=nodes, hang=hang, slow=slow)
        made.append(w)
        return w
    return factory


def test_meshcore_first_then_meshtastic(tmp_path):
    made = []
    src = make_source(tmp_path, factory_for("meshtastic", made))
    asyncio.run(run_for(src, Ctx(), 0.5))
    assert [w.network for w in made] == ["meshcore", "meshtastic"]
    assert made[0].stopped and made[1].port == "/dev/ttyACM0"


def test_firmware_remembered_per_serial(tmp_path):
    made = []
    src = make_source(tmp_path, factory_for("meshtastic", made))
    asyncio.run(run_for(src, Ctx(), 0.5))
    made.clear()
    asyncio.run(run_for(src, Ctx(), 0.2))
    assert [w.network for w in made] == ["meshtastic"]


def test_emits_changed_nodes_only(tmp_path):
    made = []
    src = make_source(tmp_path, factory_for("meshcore", made, nodes=[node()]))
    ctx = Ctx()
    asyncio.run(run_for(src, ctx, 0.3))
    assert len(ctx.emitted) == 1  # polled several times, the node did not change
    o = ctx.emitted[0]
    assert (o.kind, o.name, o.rssi) == (Kind.MESH_NODE, "Kiez", None)
    assert (o.fix.lat, o.fix.lon) == (52.5, 13.4) and o.extra["node"]["node_id"] == "a1b2c3d4e5f60718"
    assert o.time.timestamp() == 1_763_553_600.0


def test_node_without_position_has_no_fix(tmp_path):
    made = []
    src = make_source(tmp_path, factory_for("meshcore", made, nodes=[node(lat=None, lon=None)]))
    ctx = Ctx()
    asyncio.run(run_for(src, ctx, 0.1))
    assert ctx.emitted[0].fix is None


def test_node_ids_prefixed_with_network(tmp_path):
    made = []
    nodes = [node(), node(node_id="deadbeef", network="meshtastic")]
    src = make_source(tmp_path, factory_for("meshcore", made, nodes=nodes))
    ctx = Ctx()
    asyncio.run(run_for(src, ctx, 0.1))
    assert sorted(o.id for o in ctx.emitted) == ["meshcore:a1b2c3d4e5f60718", "meshtastic:deadbeef"]


def test_receiver_fix_attached_when_present(tmp_path):
    made = []
    src = make_source(tmp_path, factory_for("meshcore", made, nodes=[node()]))
    fix = make_fix()
    ctx = Ctx(fix)
    asyncio.run(run_for(src, ctx, 0.1))
    assert ctx.emitted[0].extra["receiver"] == fix


def test_unplug_raises_source_gone(tmp_path):
    made = []
    src = make_source(tmp_path, factory_for("meshcore", made, nodes=[node()]))

    async def go():
        task = asyncio.create_task(src.run(Ctx()))
        await asyncio.sleep(0.1)
        unplug(tmp_path, "ttyACM0", "1-1.2")
        with pytest.raises(SourceGone):
            await asyncio.wait_for(task, 1)

    asyncio.run(go())
    assert made[-1].stopped


def test_stop_returns_when_worker_hangs(tmp_path):
    made = []
    src = make_source(tmp_path, factory_for("meshcore", made, nodes=[node()], hang=True))
    src.join_s = 0.2
    ctx = Ctx()
    began = time.monotonic()
    asyncio.run(run_for(src, ctx, 0.1))
    assert time.monotonic() - began < 2
    assert any("replug" in e and "ttyACM0" in e for e in ctx.errors)


def test_stop_during_probe_stops_worker(tmp_path):
    made = []
    src = make_source(tmp_path, factory_for("meshtastic", made, slow=True))
    src.probe_s = {"meshcore": 60.0, "meshtastic": 60.0}
    asyncio.run(run_for(src, Ctx(), 0.2))
    assert len(made) == 1 and made[0].stopped


def test_no_firmware_reports_and_is_not_probed_again(tmp_path):
    made = []
    src = make_source(tmp_path, factory_for("none", made))
    with pytest.raises(RuntimeError, match="no Meshtastic or MeshCore node on ttyACM0"):
        asyncio.run(src.run(Ctx()))
    sys = SysInfo(tmp_path)
    assert MeshUsbSource.detect(sys) == []
    assert MeshUsbSource.missing_reason(sys) == "no Meshtastic or MeshCore node on ttyACM0"
    fake_tty(tmp_path, "ttyACM0", "1-1.2", "303a", "1001", serial="AB12", devnum="9")  # replugged
    assert [d.path for d in MeshUsbSource.detect(sys)] == ["/dev/ttyACM0"]


def test_workers_never_transmit():
    from wdgwars_uploader import mesh_capture
    store = mesh_capture.MeshStore()
    mc = mesh_usb.default_worker("meshcore", "/dev/ttyACM0", store)
    mt = mesh_usb.default_worker("meshtastic", "/dev/ttyACM0", store)
    assert isinstance(mc, mesh_capture.MeshCoreWorker) and isinstance(mt, mesh_capture.MeshtasticWorker)
    assert mc.advert_minutes == 0 and mt.advert_minutes == 0 and mt.hop_limit == 0
    assert mc.transport == "serial" and mt.transport == "serial" and mc.port == mt.port == "/dev/ttyACM0"


def test_other_firmware_tried_when_remembered_one_fails(tmp_path):
    made = []
    MeshUsbSource._firmware["AB12"] = "meshtastic"  # same serial, now a MeshCore board
    src = make_source(tmp_path, factory_for("meshcore", made, nodes=[node()]))
    ctx = Ctx()
    asyncio.run(run_for(src, ctx, 0.5))
    assert [w.network for w in made] == ["meshtastic", "meshcore"] and len(ctx.emitted) == 1
    assert MeshUsbSource._firmware["AB12"] == "meshcore"


def test_stuck_port_remembered_until_replugged(tmp_path):
    made = []
    src = make_source(tmp_path, factory_for("none", made, slow=True, hang=True))
    src.join_s = 0.1
    with pytest.raises(RuntimeError, match="ttyACM0 is stuck, replug the node"):
        asyncio.run(src.run(Ctx()))
    assert len(made) == 1  # not probed further next to a hung reader
    sys = SysInfo(tmp_path)
    assert MeshUsbSource.detect(sys) == []
    assert MeshUsbSource.missing_reason(sys) == "ttyACM0 is stuck, replug the node"
    fake_tty(tmp_path, "ttyACM0", "1-1.2", "303a", "1001", serial="AB12", devnum="9")  # replugged
    assert [d.path for d in MeshUsbSource.detect(sys)] == ["/dev/ttyACM0"]


def test_serial_lines_left_to_the_libraries(monkeypatch):
    """The reference holds DTR/RTS low on ESP32 ports for Windows. On Linux that resets the board on
    open, and Meshtastic then waits out its whole connect timer. Every port in its list is affected."""
    from types import SimpleNamespace
    from serial.tools import list_ports
    from wdgwars_uploader import mesh_capture
    for vid, pid in mesh_capture._NO_RESET_IDS:
        monkeypatch.setattr(list_ports, "comports",
                            lambda vid=vid, pid=pid: [SimpleNamespace(device="/dev/ttyACM0", vid=vid, pid=pid)])
        mesh_usb.default_worker("meshtastic", "/dev/ttyACM0", mesh_capture.MeshStore())
        assert not mesh_capture._needs_no_reset("/dev/ttyACM0"), (hex(vid), hex(pid))


def test_meshtastic_answer_skips_meshcore(tmp_path):
    """A node that answers the raw Meshtastic request never meets the MeshCore library and its DTR tries."""
    made = []
    src = make_source(tmp_path, factory_for("meshtastic", made, nodes=[node(network="meshtastic")]))
    asked = []
    src.firmware_probe = lambda path: asked.append(path) or True
    asyncio.run(run_for(src, Ctx(), 0.3))
    assert asked == ["/dev/ttyACM0"] and [w.network for w in made] == ["meshtastic"]


def test_remembered_firmware_is_not_asked_again(tmp_path):
    made = []
    MeshUsbSource._firmware["AB12"] = "meshcore"
    src = make_source(tmp_path, factory_for("meshcore", made, nodes=[node()]))
    src.firmware_probe = lambda path: pytest.fail("asked again")
    asyncio.run(run_for(src, Ctx(), 0.2))
    assert [w.network for w in made] == ["meshcore"]


class FakeSerial:
    reply = b""
    fail_open = False

    def __init__(self):
        self.written, self.lines_at_open, self.closed = b"", None, False

    def open(self):
        if self.fail_open:
            raise OSError("busy")
        self.lines_at_open = (self.dtr, self.rts)

    def write(self, data):
        self.written += data

    def read(self, n):
        out, type(self).reply = type(self).reply, b""
        return out

    def close(self):
        self.closed = True


def test_answers_meshtastic(monkeypatch):
    made = []

    def cls():
        made.append(FakeSerial())
        return made[-1]

    FakeSerial.reply = b"\x1b[32mINFO | log\r\n\x94\xc3\x00\x05abcde"
    assert mesh_usb.answers_meshtastic("/dev/ttyACM0", cls, wait_s=0.5)
    s = made[-1]
    assert s.lines_at_open == (True, False)  # DTR high, RTS low: no ESP32 reset
    assert s.written.endswith(bytes([0x94, 0xC3, 0x00, 0x02, 0x18, 0x01])) and s.closed
    FakeSerial.reply = b"some log text without frames"
    assert not mesh_usb.answers_meshtastic("/dev/ttyACM0", cls, wait_s=0.3)
    FakeSerial.fail_open = True
    try:
        assert not mesh_usb.answers_meshtastic("/dev/ttyACM0", cls, wait_s=0.3)
    finally:
        FakeSerial.fail_open = False
