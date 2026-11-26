import asyncio
import os
import json
from datetime import datetime, timedelta, timezone
from typing import ClassVar

import pytest

from pfadfinder.config import Config, Paths
from pfadfinder.core.core import Core
from pfadfinder.model import Claim, Device, Fix, Kind, Observation
from pfadfinder.modes import Mode
from pfadfinder.power import PowerManager
from pfadfinder.sources.base import Source, SourceGone
from pfadfinder.storage.session import Session
from pfadfinder.storage.throttle import RowThrottle


def now():
    return datetime.now(timezone.utc)


class FakeGps(Source):
    name: ClassVar[str] = "gps"
    kinds: ClassVar[frozenset] = frozenset()
    power_w: ClassVar[float] = 0.15
    have_fix = True

    @classmethod
    def detect(cls, sys):
        return [Device("gps", "fake", "gpsd", True, ())]

    async def run(self, ctx):
        while True:
            if FakeGps.have_fix:
                ctx.update_fix("fake", Fix(52.52, 13.405, 30.0, 3.0, 2.0, now(), 3, 9))
            await asyncio.sleep(0.01)


class FakeWifi(Source):
    name: ClassVar[str] = "wifi"
    kinds: ClassVar[frozenset] = frozenset({Kind.WIFI})
    power_w: ClassVar[float] = 0.6
    devices: ClassVar[list] = []
    fail_paths: ClassVar[set] = set()

    @classmethod
    def detect(cls, sys):
        return list(cls.devices)

    async def run(self, ctx):
        if self.device.path in FakeWifi.fail_paths:
            raise SourceGone(f"{self.device.path}: No such device")
        n = 0
        while True:
            n += 1
            ctx.emit(Observation(Kind.WIFI, f"00:00:00:00:00:{n % 256:02x}", f"net{n}", -50, now()))
            await asyncio.sleep(0.01)


USB = Device("wifi", "wlan1", "USB dongle (wlan1, rtl8xxxu)", False, (Claim("wlan:wlan1"),))
INTERNAL = Device("wifi", "wlan0", "internal (wlan0)", True, (Claim("wlan:wlan0"),))
MODES = {"drive": Mode("drive", "Drive", ["gps", "wifi"], [], False), "upload": Mode("upload", "Upload", [], [], False)}


@pytest.fixture(autouse=True)
def reset_fakes():
    FakeGps.have_fix = True
    FakeWifi.devices = [INTERNAL]
    FakeWifi.fail_paths = set()


def make_core(tmp_path, config=None):
    env = {"HOME": str(tmp_path / "home"), "PFADFINDER_SOCKET": str(tmp_path / "core.sock")}
    return Core(Paths.from_env(env), config or Config(), None, PowerManager(tmp_path / "root"),
                sources={"gps": FakeGps, "wifi": FakeWifi}, modes=MODES,
                flush_interval=0.05, restart_delays=(0.02, 0.02, 0.02, 0.02))


async def until(pred, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


async def test_start_drive_writes_rows_only_with_fix(tmp_path):
    FakeGps.have_fix = False
    core = make_core(tmp_path)
    assert (await core.start_mode("drive"))["ok"]
    await asyncio.sleep(0.1)
    assert core.status()["counts"] == {}
    FakeGps.have_fix = True
    assert await until(lambda: core.status()["counts"].get("WIFI", [0])[0] > 0)
    st = core.status()
    assert st["mode"] == "drive" and st["running"] and st["fix"]["mode"] == 3 and st["fix"]["sats"] == 9
    await core.stop()
    assert core.status()["running"] is False


async def test_unavailable_mode_reason(tmp_path):
    FakeWifi.devices = []
    core = make_core(tmp_path)
    res = await core.start_mode("drive")
    assert res == {"ok": False, "error": "wifi: no device found"}


async def test_source_failure_restarts_with_redetect(tmp_path):
    FakeWifi.devices = [USB, INTERNAL]
    FakeWifi.fail_paths = {"wlan1"}
    core = make_core(tmp_path)
    await core.start_mode("drive")
    await until(lambda: any("wlan1" in e for e in core.status()["errors"]))
    FakeWifi.devices = [INTERNAL]  # dongle unplugged
    assert await until(lambda: core.status()["sources"]["wifi"]["device"] == "internal (wlan0)"
                       and core.status()["sources"]["wifi"]["state"] == "running")
    assert core.status()["sources"]["gps"]["state"] == "running"
    await core.stop()


async def test_paused_state_without_fix(tmp_path):
    FakeGps.have_fix = False
    core = make_core(tmp_path)
    await core.start_mode("drive")
    await asyncio.sleep(0.05)
    assert core.status()["sources"]["wifi"]["state"] == "paused"
    await core.stop()


async def test_resume_after_power_loss(tmp_path):
    core = make_core(tmp_path)
    paths = core.paths
    old = Session.create(paths.sessions_dir, "drive", now() - timedelta(hours=1), "0.1.0", RowThrottle())
    paths.state_path.parent.mkdir(parents=True, exist_ok=True)
    paths.state_path.write_text(json.dumps({"running_mode": "drive"}))
    task = asyncio.create_task(core.run_forever())
    assert await until(lambda: core.status()["running"])
    meta = json.loads((old.dir / "session.json").read_text())
    assert meta["ended"] is not None
    assert core.status()["session"] != old.id
    task.cancel()
    await core.stop()


async def test_start_on_boot(tmp_path):
    core = make_core(tmp_path, Config(start_on_boot=True))
    task = asyncio.create_task(core.run_forever())
    assert await until(lambda: core.status()["mode"] == "drive" and core.status()["running"])
    task.cancel()
    await core.stop()


async def test_clean_stop_does_not_resume(tmp_path):
    core = make_core(tmp_path)
    await core.start_mode("drive")
    await core.stop()
    core2 = make_core(tmp_path)
    task = asyncio.create_task(core2.run_forever())
    await asyncio.sleep(0.1)
    assert core2.status()["running"] is False
    task.cancel()


async def test_disk_error_surfaces(tmp_path):
    core = make_core(tmp_path)
    await core.start_mode("drive")
    calls = {"n": 0}
    real_flush = core.session.flush

    def broken_flush():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(28, "No space left on device")
        real_flush()

    core.session.flush = broken_flush
    task = asyncio.create_task(core.run_forever())
    assert await until(lambda: calls["n"] >= 2)
    assert any("No space left" in e for e in core.status()["errors"])
    assert core.status()["running"]
    task.cancel()
    await core.stop()


async def test_config_set_hides_token(tmp_path):
    core = make_core(tmp_path)
    res = await core.handle({"cmd": "config.set", "key": "wigle_api_token", "value": "s3cret"})
    assert res["ok"]
    got = await core.handle({"cmd": "config.get"})
    assert got["config"]["wigle_api_token"] == "set"
    assert "s3cret" not in json.dumps(got)
    bad = await core.handle({"cmd": "config.set", "key": "nope", "value": 1})
    assert not bad["ok"]


async def test_hardware_and_modes(tmp_path):
    FakeWifi.devices = [USB, INTERNAL]
    core = make_core(tmp_path)
    hw = await core.handle({"cmd": "hardware"})
    assert [d["label"] for d in hw["hardware"]["wifi"]] == [USB.label, INTERNAL.label]
    modes = await core.handle({"cmd": "modes"})
    assert modes["modes"]["drive"]["available"] is True


async def test_unknown_command(tmp_path):
    res = await make_core(tmp_path).handle({"cmd": "explode"})
    assert res == {"ok": False, "error": "unknown command: explode"}


async def test_clean_shutdown_does_not_resume(tmp_path):
    # reboot, shutdown or update: the service is stopped cleanly, the mode does not come back
    core = make_core(tmp_path)
    await core.start_mode("drive")
    sid = core.status()["session"]
    await core.stop_for_shutdown()
    meta = json.loads((core.paths.sessions_dir / sid / "session.json").read_text())
    assert meta["ended"] is not None
    assert json.loads(core.paths.state_path.read_text()) == {"running_mode": None}
    core2 = make_core(tmp_path)
    task = asyncio.create_task(core2.run_forever())
    await asyncio.sleep(0.1)
    assert core2.status()["running"] is False
    task.cancel()


async def test_power_loss_still_resumes(tmp_path):
    # no clean stop (power loss, crash): state still says "drive"
    core = make_core(tmp_path)
    await core.start_mode("drive")
    for t in list(core._tasks.values()):
        t.cancel()  # the process dies without stop()
    core2 = make_core(tmp_path)
    task = asyncio.create_task(core2.run_forever())
    assert await until(lambda: core2.status()["running"] and core2.status()["mode"] == "drive")
    task.cancel()
    await core2.stop()


async def test_upload_refused_during_drive(tmp_path):
    core = make_core(tmp_path)
    await core.start_mode("drive")
    res = await core.handle({"cmd": "upload", "site": "wigle"})
    assert res == {"ok": False, "error": "Stop Drive first."}
    await core.stop()


async def test_startup_error_does_not_kill_flush_loop(tmp_path, monkeypatch):
    core = make_core(tmp_path)
    old = Session.create(core.paths.sessions_dir, "drive", now() - timedelta(hours=1), "0.1.0", RowThrottle())

    def boom(*a, **k):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(Session, "close_stale", staticmethod(boom))
    flushed = []
    monkeypatch.setattr(core, "flush", lambda: flushed.append(1))
    task = asyncio.create_task(core.run_forever())
    assert await until(lambda: len(flushed) >= 2)
    assert any("Read-only" in e for e in core.status()["errors"])
    task.cancel()


async def test_db_error_keeps_csv_row(tmp_path, monkeypatch):
    import gzip as _gzip
    import sqlite3
    core = make_core(tmp_path)
    await core.start_mode("drive")

    def broken(*a, **k):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(core.store, "record", broken)
    await until(lambda: any("disk is full" in e for e in core.status()["errors"]))
    sid = core.status()["session"]
    await core.stop()
    rows = _gzip.open(core.paths.sessions_dir / sid / "wifi-bt.wiglecsv.gz", "rt").read().splitlines()[2:]
    assert rows


async def test_detect_exception_during_restart_recovers(tmp_path):
    FakeWifi.devices = [USB]
    FakeWifi.fail_paths = {"wlan1"}
    core = make_core(tmp_path)
    await core.start_mode("drive")
    calls = {"n": 0}
    real = FakeWifi.detect.__func__

    def flaky(cls, sys):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(2, "sysfs entry vanished")
        return [INTERNAL]

    FakeWifi.detect = classmethod(flaky)
    try:
        assert await until(lambda: core.status()["sources"].get("wifi", {}).get("device") == INTERNAL.label
                           and core.status()["sources"]["wifi"]["state"] == "running")
    finally:
        FakeWifi.detect = classmethod(real)
        await core.stop()


async def test_status_alert_recent_error(tmp_path):
    core = make_core(tmp_path)
    assert core.status()["alert"] is None
    core.report_error("storage", "No space left on device")
    assert core.status()["alert"] == "storage: No space left on device"


async def test_config_file_edit_picked_up_on_flush(tmp_path):
    from pfadfinder.config import update_config_file
    core = make_core(tmp_path)
    update_config_file(core.paths.config_path, "screen_mode", "mount")
    core.flush()
    assert core.config_view()["screen_mode"] == "mount"


async def test_broken_config_keeps_last_good_and_alerts(tmp_path):
    from pfadfinder.config import update_config_file
    core = make_core(tmp_path)
    update_config_file(core.paths.config_path, "screen_mode", "mount")
    core.flush()
    core.paths.config_path.write_text('[ui]\nscreen_mode = "pocket\n')
    os.utime(core.paths.config_path, ns=(1, 2))
    core.flush()
    assert core.config_view()["screen_mode"] == "mount"
    assert core.status()["alert"].startswith("config: ")


async def test_set_config_keeps_manual_edit_and_comments(tmp_path):
    from pfadfinder.config import ensure_config
    core = make_core(tmp_path)
    path = core.paths.config_path
    ensure_config(path)
    path.write_text(path.read_text().replace('api_name = ""', 'api_name = "manual"  # typed over ssh'))
    res = await core.handle({"cmd": "config.set", "key": "screen_mode", "value": "mount"})
    assert res["ok"]
    text = path.read_text()
    assert 'api_name = "manual"  # typed over ssh' in text and 'screen_mode = "mount"' in text
    assert core.config_view()["wigle_api_name"] == "manual"


async def test_set_config_validates_before_writing(tmp_path):
    core = make_core(tmp_path)
    res = await core.handle({"cmd": "config.set", "key": "screen_mode", "value": "sideways"})
    assert not res["ok"]
    assert not core.paths.config_path.exists() or "sideways" not in core.paths.config_path.read_text()


async def test_config_view_masks_all_secrets(tmp_path):
    core = make_core(tmp_path, Config(wigle_api_token="t", wdgwars_api_key="k", wardrift_api_key="w",
                                      wardrift_password="p", wardrift_username="u"))
    view = core.config_view()
    assert [view[k] for k in ("wigle_api_token", "wdgwars_api_key", "wardrift_api_key", "wardrift_password")] == \
        ["set"] * 4
    assert view["wardrift_username"] == "u"



class FakeSite:
    def __init__(self, key, title, configured=True, stats=None):
        self.key, self.title, self._configured, self._stats = key, title, configured, stats
        self.uploads, self.stats_calls = [], 0

    def configured(self, cfg):
        return self._configured

    def upload(self, data, filename, cfg):
        self.uploads.append(filename)
        from pfadfinder.upload.sites import SiteResult
        return SiteResult(True, "ok")

    def stats(self, cfg, state_dir, config_path=None):
        self.stats_calls += 1
        return self._stats


def fake_sites(monkeypatch, **configured):
    from pfadfinder.core import core as core_mod
    sites = [FakeSite("wigle", "WiGLE", configured.get("wigle", True), {"user": "m", "lines": ["a", "b"]}),
             FakeSite("wdgwars", "WDGWars", configured.get("wdgwars", True), None),
             FakeSite("wardrift", "Wardrift", configured.get("wardrift", False), None)]
    sites[1].takes_aircraft = sites[2].takes_aircraft = True
    monkeypatch.setattr(core_mod, "SITES", sites)
    return sites


async def test_sites_lists_three_in_order(tmp_path, monkeypatch):
    fake_sites(monkeypatch)
    core = make_core(tmp_path)
    sites = (await core.handle({"cmd": "sites"}))["sites"]
    assert list(sites) == ["wigle", "wdgwars", "wardrift"]
    assert sites["wardrift"]["configured"] is False and sites["wigle"]["title"] == "WiGLE"
    assert sites["wigle"]["pending_unit"] == "sessions"
    assert sites["wdgwars"]["pending_unit"] == sites["wardrift"]["pending_unit"] == "uploads"


async def test_stats_cache_per_site(tmp_path, monkeypatch):
    sites = fake_sites(monkeypatch)
    core = make_core(tmp_path)
    for _ in range(5):
        core.sites()
        await asyncio.sleep(0.01)
    assert [s.stats_calls for s in sites] == [1, 1, 0]  # wardrift not configured
    assert core.sites()["wigle"]["stats"] == {"user": "m", "lines": ["a", "b"]}


async def test_stats_error_shown_and_not_retried(tmp_path, monkeypatch):
    from pfadfinder.upload.sites import SiteError
    sites = fake_sites(monkeypatch, wardrift=True)

    def boom(cfg, state_dir, config_path=None):
        sites[2].stats_calls += 1
        raise SiteError("Wardrift did not accept that username and password")

    sites[2].stats = boom
    core = make_core(tmp_path)
    for _ in range(3):
        core.sites()
        await asyncio.sleep(0.01)
    assert sites[2].stats_calls == 1
    assert core.sites()["wardrift"]["stats_error"] == "Wardrift did not accept that username and password"


async def test_upload_all_runs_each_configured_site(tmp_path, monkeypatch):
    sites = fake_sites(monkeypatch)
    core = make_core(tmp_path)
    s = Session.create(core.paths.sessions_dir, "drive", now(), "0.1.0", RowThrottle())
    s.write(Observation(Kind.WIFI, "aa", "n", -50, now(), fix=Fix(52.5, 13.4, 30.0, 3.0, 0.0, now(), 3)))
    s.close(now())
    core.store.record(Observation(Kind.WIFI, "aa", "n", -50, now(), fix=Fix(52.5, 13.4, 30.0, 3.0, 0.0, now(), 3)),
                      s.id)
    core.store.commit()
    assert (await core.handle({"cmd": "upload", "site": "all"}))["ok"]
    await until(lambda: not core.sites()["wigle"]["uploading"])
    assert len(sites[0].uploads) == 1 and len(sites[1].uploads) == 1 and sites[2].uploads == []


async def test_offline_refuses_upload_and_skips_stats(tmp_path, monkeypatch):
    sites = fake_sites(monkeypatch)
    core = make_core(tmp_path)
    core.route_file = tmp_path / "route"
    core.route_file.write_text("Iface\tDestination\tGateway \tFlags\n"
                               "wlan0\t00B2A8C0\t00000000\t0001\n")  # only the local net, no default route
    assert await core.handle({"cmd": "upload", "site": "all"}) == {"ok": False,
                                                                  "error": "No network. Connect Wi-Fi first."}
    core.sites()
    await asyncio.sleep(0.05)
    assert [s.stats_calls for s in sites] == [0, 0, 0] and core.sites()["wigle"]["stats_error"] == ""


async def test_default_route_counts_as_online(tmp_path, monkeypatch):
    fake_sites(monkeypatch)
    core = make_core(tmp_path)
    core.route_file = tmp_path / "route"
    core.route_file.write_text("Iface\tDestination\tGateway \tFlags\n"
                               "wlan0\t00000000\t01B2A8C0\t0003\n")
    assert (await core.handle({"cmd": "upload", "site": "all"}))["ok"]


async def test_upload_site_without_keys(tmp_path, monkeypatch):
    fake_sites(monkeypatch)
    res = await make_core(tmp_path).handle({"cmd": "upload", "site": "wardrift"})
    assert res == {"ok": False, "error": "Wardrift keys missing"}


async def test_shutdown_waits_for_upload(tmp_path, monkeypatch):
    sites = fake_sites(monkeypatch)
    import threading
    gate = threading.Event()
    finished = []

    def slow_upload(data, filename, cfg):
        gate.wait(2)
        finished.append(filename)
        from pfadfinder.upload.sites import SiteResult
        return SiteResult(True, "ok")

    sites[0].upload = slow_upload
    core = make_core(tmp_path)
    s = Session.create(core.paths.sessions_dir, "drive", now(), "0.1.0", RowThrottle())
    s.write(Observation(Kind.WIFI, "aa", "n", -50, now(), fix=Fix(52.5, 13.4, 30.0, 3.0, 0.0, now(), 3)))
    s.close(now())
    await core.handle({"cmd": "upload", "site": "wigle"})
    asyncio.get_running_loop().call_later(0.1, gate.set)
    await core.stop_for_shutdown()
    assert finished == [f"pfadfinder-{s.id}.wiglecsv.gz"]


class FakeAdsb(Source):
    name: ClassVar[str] = "adsb"
    kinds: ClassVar[frozenset] = frozenset({Kind.AIRCRAFT})
    power_w: ClassVar[float] = 1.5

    @classmethod
    def detect(cls, sys):
        return [Device("adsb", "1-1.3", "RTL-SDR (1-1.3)", False, (Claim("usb:1-1.3"),))]

    async def run(self, ctx):
        while True:
            t = now()
            ctx.emit(Observation(Kind.AIRCRAFT, "3C6DD4", "DLH4AB", -12, t, fix=Fix(52.5, 13.4, 11000.0, 0.0, None, t, 3),
                                 extra={"aircraft": {"hex": "3c6dd4", "lat": 52.5, "lon": 13.4},
                                        "obs_ts": t.timestamp(), "pos_ts": t.timestamp()}))
            await asyncio.sleep(0.01)


async def test_aircraft_logged_without_receiver_fix(tmp_path):
    FakeGps.have_fix = False
    env = {"HOME": str(tmp_path / "home"), "PFADFINDER_SOCKET": str(tmp_path / "core.sock")}
    core = Core(Paths.from_env(env), Config(), None, PowerManager(tmp_path / "root"),
                sources={"gps": FakeGps, "adsb": FakeAdsb},
                modes={"sky": Mode("sky", "Sky", ["adsb"], ["gps"], False)}, flush_interval=0.05)
    await core.start_mode("sky")
    assert await until(lambda: core.status()["counts"].get("AIRCRAFT", [0])[0] == 1)
    sid = core.status()["session"]
    await core.stop()
    import gzip as _gzip
    rec = json.loads(_gzip.open(core.paths.sessions_dir / sid / "aircraft.jsonl.gz", "rt").readline())
    assert rec["hex"] == "3c6dd4"


class FakeMesh(Source):
    name: ClassVar[str] = "mesh-usb"
    kinds: ClassVar[frozenset] = frozenset({Kind.MESH_NODE})
    power_w: ClassVar[float] = 0.5
    present: ClassVar[bool] = True

    @classmethod
    def detect(cls, sys):
        return [Device("mesh-usb", "/dev/ttyACM0", "Heltec (ttyACM0)", False, (Claim("usb:1-1.2"),))] \
            if cls.present else []

    async def run(self, ctx):
        while True:
            t = now()
            node = {"node_id": "a1b2c3d4e5f60718", "network": "meshcore", "name": "Kiez", "lat": 52.5, "lon": 13.4}
            ctx.emit(Observation(Kind.MESH_NODE, "meshcore:a1b2c3d4e5f60718", "Kiez", None, t,
                                 fix=Fix(52.5, 13.4, None, 0.0, None, t, 2),
                                 extra={"node": node, "receiver": ctx.fix()}))
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("present", [True, False])
async def test_drive_runs_with_and_without_mesh_node(tmp_path, present):
    FakeMesh.present = present
    FakeWifi.devices = [Device("wifi", "wlan0", "wlan0", True, (Claim("wlan0"),))]
    env = {"HOME": str(tmp_path / "home"), "PFADFINDER_SOCKET": str(tmp_path / "core.sock")}
    core = Core(Paths.from_env(env), Config(), None, PowerManager(tmp_path / "root"),
                sources={"gps": FakeGps, "wifi": FakeWifi, "mesh-usb": FakeMesh},
                modes={"drive": Mode("drive", "Drive", ["gps", "wifi"], ["mesh-usb"], False)}, flush_interval=0.05)
    try:
        assert core.modes_info()["drive"]["available"]
        assert (await core.start_mode("drive"))["ok"]
        assert await until(lambda: "wifi" in core.status()["sources"])
        assert ("mesh-usb" in core.status()["sources"]) is present
    finally:
        await core.stop()
        FakeMesh.present = True


async def test_mesh_logged_without_receiver_fix(tmp_path):
    FakeGps.have_fix = False
    env = {"HOME": str(tmp_path / "home"), "PFADFINDER_SOCKET": str(tmp_path / "core.sock")}
    core = Core(Paths.from_env(env), Config(), None, PowerManager(tmp_path / "root"),
                sources={"gps": FakeGps, "wifi": FakeWifi, "mesh-usb": FakeMesh},
                modes={"drive": Mode("drive", "Drive", ["gps", "wifi"], ["mesh-usb"], False)}, flush_interval=0.05)
    FakeWifi.devices = [Device("wifi", "wlan0", "wlan0", True, (Claim("wlan0"),))]
    await core.start_mode("drive")
    assert await until(lambda: core.status()["counts"].get("MESH_NODE", [0])[0] == 1)
    assert core.status()["sources"]["mesh-usb"]["state"] == "running"  # not paused without a fix
    sid = core.status()["session"]
    await core.stop()
    import gzip as _gzip
    rec = json.loads(_gzip.open(core.paths.sessions_dir / sid / "mesh.jsonl.gz", "rt").readline())
    assert rec["node_id"] == "a1b2c3d4e5f60718" and rec["_receiver"] is None


class NoAdsb(FakeAdsb):
    @classmethod
    def detect(cls, sys):
        return []


async def test_sky_unavailable_without_rtlsdr(tmp_path):
    from pfadfinder.modes import load_modes
    core = Core(Paths.from_env({"HOME": str(tmp_path / "home")}), Config(), None, PowerManager(tmp_path / "root"),
                sources={"gps": FakeGps, "adsb": NoAdsb, "wifi": FakeWifi, "ble": FakeWifi}, modes=load_modes())
    NoAdsb.missing_hint = "no RTL-SDR found"
    info = core.modes_info()["sky"]
    assert (info["title"], info["available"], info["reason"]) == ("Sky", False, "adsb: no RTL-SDR found")
    assert info["description"].startswith("Aircraft with an RTL-SDR stick,\nno GPS fix needed.")
    assert (info["sources"], info["optional"]) == (["adsb"], ["gps", "mesh-usb"])


async def test_sky_starts_without_gps_and_keeps_two_cores(tmp_path, monkeypatch):
    from pfadfinder.modes import load_modes

    class NoGps(FakeGps):
        @classmethod
        def detect(cls, sys):
            return []

    core = Core(Paths.from_env({"HOME": str(tmp_path / "home")}), Config(), None, PowerManager(tmp_path / "root"),
                sources={"gps": NoGps, "adsb": FakeAdsb}, modes=load_modes())
    recorded = []
    monkeypatch.setattr(core.power, "enter_wardrive", lambda cores: recorded.append(cores))
    assert (await core.start_mode("sky"))["ok"]
    await core.stop()
    assert recorded == [2]


async def test_upload_refused_names_running_mode(tmp_path):
    core = Core(Paths.from_env({"HOME": str(tmp_path / "home")}), Config(), None, PowerManager(tmp_path / "root"),
                sources={"gps": FakeGps, "adsb": FakeAdsb},
                modes={"sky": Mode("sky", "Sky", ["adsb"], ["gps"], False)}, flush_interval=0.05)
    await core.start_mode("sky")
    assert await core.handle({"cmd": "upload", "site": "wigle"}) == {"ok": False, "error": "Stop Sky first."}
    await core.stop()


async def test_pending_counts_cached(tmp_path, monkeypatch):
    fake_sites(monkeypatch)
    from pfadfinder.core import core as core_mod
    calls = []
    real = core_mod.pending_count
    monkeypatch.setattr(core_mod, "pending_count", lambda *a: calls.append(a[2]) or real(*a))
    core = make_core(tmp_path)
    for _ in range(5):
        core.sites()
    assert sorted(calls) == ["wardrift", "wdgwars", "wigle"]


async def test_supervisor_uses_missing_reason(tmp_path):
    class Gone(FakeWifi):
        @classmethod
        def missing_reason(cls, sys):
            return "no ADS-B decoder with RTL-SDR support"

    FakeWifi.devices = [USB]
    FakeWifi.fail_paths = {"wlan1"}
    core = make_core(tmp_path)
    core.sources["wifi"] = Gone
    await core.start_mode("drive")
    FakeWifi.devices = []
    assert await until(lambda: core.status()["sources"].get("wifi", {}).get("error")
                       == "no ADS-B decoder with RTL-SDR support")
    await core.stop()


async def test_adsb_not_shown_paused_without_fix(tmp_path):
    FakeGps.have_fix = False
    core = Core(Paths.from_env({"HOME": str(tmp_path / "home")}), Config(), None, PowerManager(tmp_path / "root"),
                sources={"gps": FakeGps, "adsb": FakeAdsb},
                modes={"sky": Mode("sky", "Sky", ["adsb"], ["gps"], False)}, flush_interval=0.05)
    await core.start_mode("sky")
    await asyncio.sleep(0.05)
    assert core.status()["sources"]["adsb"]["state"] == "running"  # aircraft need no receiver fix
    await core.stop()
