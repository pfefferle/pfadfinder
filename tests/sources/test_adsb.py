import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pfadfinder.model import Claim, Device, Kind
from pfadfinder.sources.adsb import AdsbSource, parse_aircraft_json
from pfadfinder.sources.base import SourceGone, SysInfo
from pfadfinder.sources.registry import all_sources

DATA = json.loads((Path(__file__).parent.parent / "fixtures/aircraft.json").read_text())
NOW = datetime.fromtimestamp(DATA["now"], timezone.utc)


def test_parse_aircraft_json():
    obs = {o.id: o for o in parse_aircraft_json(DATA)}
    assert set(obs) == {"3C6DD4", "4CA1E2", "3C4B26"}  # TIS-B track files with "~" are skipped
    a = obs["3C6DD4"]
    assert (a.kind, a.name, a.rssi) == (Kind.AIRCRAFT, "DLH4AB", -12)
    assert (a.fix.lat, a.fix.lon) == (52.512345, 13.401234) and abs(a.fix.alt_m - 37000 * 0.3048) < 0.01
    assert a.extra["obs_ts"] == pytest.approx(DATA["now"] - 0.4)
    assert a.extra["pos_ts"] == pytest.approx(DATA["now"] - 0.9)
    assert a.extra["aircraft"]["hex"] == "3c6dd4" and a.time == datetime.fromtimestamp(DATA["now"] - 0.4, timezone.utc)
    assert obs["4CA1E2"].fix is None and obs["4CA1E2"].extra["pos_ts"] is None  # Mode S only
    assert obs["3C4B26"].fix.alt_m is None and obs["3C4B26"].name == ""      # on the ground


def test_registered():
    assert all_sources()["adsb"] is AdsbSource


def fake_usb(root, busid, vid, pid, serial=None):
    d = root / "sys/bus/usb/devices" / busid
    d.mkdir(parents=True)
    (d / "idVendor").write_text(vid + "\n")
    (d / "idProduct").write_text(pid + "\n")
    if serial:
        (d / "serial").write_text(serial + "\n")


def test_detect_rtlsdr_ids(tmp_path, monkeypatch):
    from pfadfinder.sources import adsb
    monkeypatch.setattr(adsb, "find_decoder", lambda path=None: "/usr/local/bin/dump1090")
    fake_usb(tmp_path, "1-1.3", "0bda", "2838", serial="00000001")
    fake_usb(tmp_path, "1-1.4", "0bda", "2832")
    fake_usb(tmp_path, "1-1.2", "2357", "010c")  # the Wi-Fi dongle
    devs = AdsbSource.detect(SysInfo(tmp_path))
    assert [(d.path, d.label) for d in devs] == [("1-1.3", "RTL-SDR (1-1.3)"), ("1-1.4", "RTL-SDR (1-1.4)")]
    assert devs[0].claims == (Claim("usb:1-1.3"),) and devs[0].internal is False


def fake_readsb(tmp_path, body):
    script = tmp_path / "readsb"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    return str(script)


class Ctx:
    def __init__(self):
        self.emitted = []

    def fix(self):
        return None

    def emit(self, obs):
        self.emitted.append(obs)


def source(tmp_path, readsb, present=True):
    if present:
        fake_usb(tmp_path / "root", "1-1.3", "0bda", "2838")
    src = AdsbSource(Device("adsb", "1-1.3", "RTL-SDR (1-1.3)", False, (Claim("usb:1-1.3"),)))
    src.decoder, src.sysroot, src.workdir, src.poll_s = readsb, tmp_path / "root", tmp_path / "work", 0.05
    return src


WRITES_JSON = """
dir=""
while [ $# -gt 0 ]; do [ "$1" = "--write-json" ] && dir="$2"; shift; done
mkdir -p "$dir"
cp "{fixture}" "$dir/aircraft.json"
echo $$ > "{pidfile}"
exec sleep 30
"""


async def test_run_emits_and_terminates_readsb_on_cancel(tmp_path):
    fixture = Path(__file__).parent.parent / "fixtures/aircraft.json"
    src = source(tmp_path, fake_readsb(tmp_path, WRITES_JSON.format(fixture=fixture, pidfile=tmp_path / "pid")))
    ctx = Ctx()
    task = asyncio.create_task(src.run(ctx))
    for _ in range(100):
        if ctx.emitted:
            break
        await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert {o.id for o in ctx.emitted} >= {"3C6DD4"}
    pid = int((tmp_path / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # readsb is gone
    assert not (tmp_path / "work").exists() or not any((tmp_path / "work").iterdir())


async def test_readsb_exit_unplugged_raises_source_gone(tmp_path):
    src = source(tmp_path, fake_readsb(tmp_path, "echo 'rtlsdr: no supported devices found' >&2\nexit 1\n"),
                 present=False)
    with pytest.raises(SourceGone):
        await src.run(Ctx())


async def test_busy_device_message(tmp_path):
    src = source(tmp_path, fake_readsb(tmp_path, "echo 'usb_claim_interface error -6' >&2\nexit 1\n"))
    with pytest.raises(RuntimeError, match="RTL-SDR is in use by another program"):
        await src.run(Ctx())


async def test_other_exit_reports_last_line(tmp_path):
    src = source(tmp_path, fake_readsb(tmp_path, "echo 'something odd' >&2\nexit 3\n"))
    with pytest.raises(RuntimeError, match="decoder stopped: something odd"):
        await src.run(Ctx())


def fake_bin(dirpath, name, help_text, code=0):
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / name
    p.write_text(f"#!/bin/sh\ncat <<'X'\n{help_text}\nX\nexit {code}\n")
    p.chmod(0o755)
    return p


DUMP1090_HELP = "| dump1090 ModeS Receiver   dump1090-fa unknown |\n| build options: ENABLE_RTLSDR |\n--device-type <type>"
READSB_NO_RTL = "SDR type 'rtlsdr' not recognized; supported SDR types are:\n  modesbeast\nERROR: Unknown device type:rtlsdr"
READSB_RTL = "readsb version: 3.14\n  --device=<index|serial>   rtlsdr options"


def test_find_decoder_prefers_dump1090_with_rtlsdr(tmp_path):
    from pfadfinder.sources.adsb import find_decoder
    from pfadfinder.sources import adsb as _a
    _a._supports_rtlsdr_cache.clear()
    fake_bin(tmp_path, "dump1090", DUMP1090_HELP)
    fake_bin(tmp_path, "readsb", READSB_RTL)
    assert find_decoder(str(tmp_path)) == str(tmp_path / "dump1090")


def test_find_decoder_skips_readsb_without_rtlsdr(tmp_path):
    from pfadfinder.sources.adsb import find_decoder
    from pfadfinder.sources import adsb as _a
    _a._supports_rtlsdr_cache.clear()
    fake_bin(tmp_path, "readsb", READSB_NO_RTL, code=1)
    assert find_decoder(str(tmp_path)) is None


def test_find_decoder_accepts_readsb_with_rtlsdr(tmp_path):
    from pfadfinder.sources.adsb import find_decoder
    from pfadfinder.sources import adsb as _a
    _a._supports_rtlsdr_cache.clear()
    fake_bin(tmp_path, "readsb", READSB_RTL)
    assert find_decoder(str(tmp_path)) == str(tmp_path / "readsb")


def test_missing_reason(tmp_path, monkeypatch):
    from pfadfinder.sources import adsb
    sys_ = SysInfo(tmp_path / "root")
    assert AdsbSource.missing_reason(sys_) == "no RTL-SDR found"
    fake_usb(tmp_path / "root", "1-1.3", "0bda", "2838")
    monkeypatch.setattr(adsb, "find_decoder", lambda path=None: None)
    assert AdsbSource.detect(sys_) == []
    assert AdsbSource.missing_reason(sys_) == "no ADS-B decoder with RTL-SDR support"
    monkeypatch.setattr(adsb, "find_decoder", lambda path=None: "/usr/local/bin/dump1090")
    assert [d.path for d in AdsbSource.detect(sys_)] == ["1-1.3"]


ARGS_ECHO = """
echo "$@" > "{argsfile}"
exec sleep 30
"""


async def test_decoder_args_without_gain(tmp_path):
    src = source(tmp_path, fake_readsb(tmp_path, ARGS_ECHO.format(argsfile=tmp_path / "args")))
    task = asyncio.create_task(src.run(Ctx()))
    for _ in range(100):
        if (tmp_path / "args").exists():
            break
        await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    args = (tmp_path / "args").read_text().split()
    assert args[:4] == ["--device-type", "rtlsdr", "--device", "0"] and "--gain" not in args
    assert "--write-json" in args and "--write-json-every" in args and "--quiet" in args


def test_find_decoder_knows_flightaware_name(tmp_path):
    from pfadfinder.sources.adsb import find_decoder
    fake_bin(tmp_path, "dump1090-fa", DUMP1090_HELP)
    assert find_decoder(str(tmp_path)) == str(tmp_path / "dump1090-fa")


def test_find_decoder_is_cached(tmp_path):
    from pfadfinder.sources import adsb
    fake_bin(tmp_path, "readsb", READSB_NO_RTL, code=1)
    calls = []
    real = adsb._supports_rtlsdr
    adsb._supports_rtlsdr_cache.clear()

    def counting(path):
        calls.append(path)
        return real(path)

    adsb._supports_rtlsdr = counting
    try:
        for _ in range(5):
            adsb.find_decoder(str(tmp_path))
    finally:
        adsb._supports_rtlsdr = real
    assert len(calls) == 1


NOISY = """
dir=""
while [ $# -gt 0 ]; do [ "$1" = "--write-json" ] && dir="$2"; shift; done
mkdir -p "$dir"
i=0; while [ $i -lt 3000 ]; do echo "warning: a long noisy decoder line number $i padding padding padding" >&2; i=$((i+1)); done
cp "{fixture}" "$dir/aircraft.json"
exec sleep 30
"""


async def test_noisy_decoder_does_not_block(tmp_path):
    fixture = Path(__file__).parent.parent / "fixtures/aircraft.json"
    src = source(tmp_path, fake_readsb(tmp_path, NOISY.format(fixture=fixture)))
    ctx = Ctx()
    task = asyncio.create_task(src.run(ctx))
    for _ in range(100):
        if ctx.emitted:
            break
        await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert ctx.emitted  # more than 64 KB of stderr did not stall the decoder
