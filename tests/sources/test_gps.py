import asyncio
import json
from datetime import timedelta
from pathlib import Path

from pfadfinder.sources.gps import FixSelector, GpsSource, parse_tpv
from pfadfinder.sources.registry import all_sources
from tests.conftest import make_fix

STREAM = [json.loads(l) for l in (Path(__file__).parent.parent / "fixtures/gpsd_stream.jsonl").read_text().splitlines()]
TPV3D = STREAM[4]


def test_parse_tpv_3d():
    fix = parse_tpv(TPV3D)
    assert (fix.lat, fix.lon, fix.alt_m, fix.accuracy_m, fix.speed_mps, fix.mode) == (52.52, 13.405, 34.2, 4.5, 13.4, 3)
    assert fix.time.isoformat() == "2026-11-19T12:00:05+00:00"


def test_parse_tpv_no_fix_none():
    assert parse_tpv(STREAM[5]) is None
    assert parse_tpv(STREAM[3]) is None


def test_parse_tpv_accuracy_fallbacks():
    msg = {k: v for k, v in TPV3D.items() if k not in ("eph", "altHAE")}
    fix = parse_tpv(msg)
    assert fix.accuracy_m == 4.0 and fix.alt_m == 31.0
    bare = {k: v for k, v in msg.items() if k not in ("epx", "epy")}
    assert parse_tpv(bare).accuracy_m == 0.0


class Mono:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_selector_prefers_3d_then_accuracy():
    sel = FixSelector(Mono())
    sel.update("usb", make_fix(mode=2, accuracy_m=1.0))
    sel.update("uart", make_fix(mode=3, accuracy_m=9.0))
    sel.update("other", make_fix(mode=3, accuracy_m=3.0, lat=1.0))
    assert sel.best().lat == 1.0


def test_selector_drops_stale_by_receipt_time():
    mono = Mono()
    sel = FixSelector(mono)
    sel.update("usb", make_fix())
    mono.t += 4
    assert sel.best() is not None
    mono.t += 2
    assert sel.best() is None


def test_selector_ignores_system_clock_offset():
    # GPS time far from the system clock must not make a fresh fix stale (or a stale one fresh)
    mono = Mono()
    sel = FixSelector(mono)
    sel.update("usb", make_fix(time=make_fix().time - timedelta(hours=3)))
    assert sel.best() is not None


def test_registered():
    assert all_sources()["gps"] is GpsSource


class Ctx:
    def __init__(self):
        self.fixes, self.errors = [], []

    def update_fix(self, device, fix):
        self.fixes.append((device, fix))

    def report_error(self, source, message):
        self.errors.append((source, message))


async def test_run_reconnects():
    connections = 0

    async def handle(reader, writer):
        nonlocal connections
        connections += 1
        await reader.readline()  # ?WATCH
        tpvs = 2 if connections == 1 else 1
        for _ in range(tpvs):
            writer.write((json.dumps(TPV3D) + "\n").encode())
        await writer.drain()
        if connections == 1:
            writer.close()
        else:
            await reader.read()  # until the client disconnects

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    src = GpsSource(GpsSource.detect(None)[0])
    src.device = src.device.__class__(**{**src.device.__dict__, "path": f"gpsd://127.0.0.1:{port}"})
    src.reconnect_delay = 0.05
    ctx = Ctx()
    task = asyncio.create_task(src.run(ctx))
    for _ in range(100):
        if len(ctx.fixes) >= 3:
            break
        await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    server.close()
    await server.wait_closed()
    assert len(ctx.fixes) == 3 and ctx.fixes[0][0] == "/dev/ttyACM0"
    assert len(ctx.errors) == 1 and ctx.errors[0][0] == "gps"


async def test_sats_from_sky():
    async def handle(reader, writer):
        await reader.readline()
        writer.write((json.dumps(STREAM[3]) + "\n" + json.dumps(TPV3D) + "\n").encode())
        await writer.drain()
        await reader.read()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    src = GpsSource(GpsSource.detect(None)[0])
    src.device = src.device.__class__(**{**src.device.__dict__, "path": f"gpsd://127.0.0.1:{port}"})
    ctx = Ctx()
    task = asyncio.create_task(src.run(ctx))
    for _ in range(100):
        if ctx.fixes:
            break
        await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    server.close()
    await server.wait_closed()
    assert ctx.fixes[0][1].sats == 9


def test_detect_label_shows_what_gpsd_reads(tmp_path):
    from pfadfinder.sources.base import SysInfo
    (tmp_path / "etc/default").mkdir(parents=True)
    (tmp_path / "etc/default/gpsd").write_text('DEVICES="/dev/serial0"\nGPSD_OPTIONS="-s 115200"\nUSBAUTO="true"\n')
    assert GpsSource.detect(SysInfo(tmp_path))[0].label == "gpsd: /dev/serial0, USB auto"


def test_detect_label_usb_only_and_missing_file(tmp_path):
    from pfadfinder.sources.base import SysInfo
    assert GpsSource.detect(SysInfo(tmp_path))[0].label == "gpsd"
    (tmp_path / "etc/default").mkdir(parents=True)
    (tmp_path / "etc/default/gpsd").write_text('DEVICES=""\nUSBAUTO="true"\n')
    assert GpsSource.detect(SysInfo(tmp_path))[0].label == "gpsd: USB auto"
