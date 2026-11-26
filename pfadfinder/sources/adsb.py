"""Aircraft (ADS-B, 1090 MHz) with an RTL-SDR.

The source starts an ADS-B decoder with RTL-SDR support (dump1090-fa, or a
readsb built with RTL-SDR) for the stick it was given, reads the decoder's
aircraft.json every few seconds and emits one observation per aircraft. Each
aircraft carries its own position; the receiver's GPS fix is only passed to
the decoder as its location when there is one. Debian's readsb package is
built without RTL-SDR support, so the decoder is found at runtime.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

from ..model import Claim, Device, Fix, Kind, Observation
from .base import Source, SourceContext, SourceGone, SysInfo
from .registry import register

log = logging.getLogger("pfadfinder.adsb")

RTLSDR_IDS = {("0bda", "2838"), ("0bda", "2832")}
FEET = 0.3048
KNOTS = 0.514444
BUSY_MARKERS = ("usb_claim_interface error -6", "resource busy", "Device or resource busy")


DECODERS = ("dump1090-fa", "dump1090", "readsb")
_supports_rtlsdr_cache: dict[tuple[str, int], bool] = {}


def _supports_rtlsdr(path: str) -> bool:
    try:
        out = subprocess.run([path, "--device-type", "rtlsdr", "--help"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    text = out.stdout + out.stderr
    return "not recognized" not in text and "Unknown device type" not in text and \
        ("ENABLE_RTLSDR" in text or "rtlsdr" in text.lower())


def find_decoder(path: str | None = None) -> str | None:
    """The first decoder on PATH (or `path`) that can read an RTL-SDR."""
    for name in DECODERS:
        exe = shutil.which(name, path=path)
        if not exe:
            continue
        try:
            key = (exe, os.stat(exe).st_mtime_ns)
        except OSError:
            continue
        if key not in _supports_rtlsdr_cache:  # asking the binary starts a process; once per version
            _supports_rtlsdr_cache[key] = _supports_rtlsdr(exe)
        if _supports_rtlsdr_cache[key]:
            return exe
    return None


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):  # None or "ground"
        return None


def parse_aircraft_json(data: dict) -> list[Observation]:
    now = float(data.get("now") or 0)
    out = []
    for a in data.get("aircraft") or []:
        hex_ = str(a.get("hex") or "").strip()
        if not hex_ or hex_.startswith("~"):  # "~" marks TIS-B track files without a real ICAO address
            continue
        seen = _num(a.get("seen")) or 0.0
        seen_pos = _num(a.get("seen_pos"))
        obs_ts = now - seen
        when = datetime.fromtimestamp(obs_ts, timezone.utc)
        lat, lon = _num(a.get("lat")), _num(a.get("lon"))
        fix = None
        if lat is not None and lon is not None:
            alt = _num(a.get("alt_baro"))
            gs = _num(a.get("gs"))
            fix = Fix(lat, lon, None if alt is None else alt * FEET, 0.0,
                      None if gs is None else gs * KNOTS, when, 3)
        rssi = _num(a.get("rssi"))
        out.append(Observation(
            Kind.AIRCRAFT, hex_.upper(), str(a.get("flight") or "").strip(),
            None if rssi is None else int(rssi), when, fix=fix,
            extra={"aircraft": a, "obs_ts": obs_ts,
                   "pos_ts": None if fix is None or seen_pos is None else now - seen_pos}))
    return out


@register
class AdsbSource(Source):
    name: ClassVar[str] = "adsb"
    kinds: ClassVar[frozenset[Kind]] = frozenset({Kind.AIRCRAFT})
    power_w: ClassVar[float] = 1.5
    missing_hint: ClassVar[str] = "no RTL-SDR found"
    decoder: str | None = None  # found at run time unless set
    poll_s = 5.0
    sysroot = Path("/")
    workdir: Path | None = None

    @classmethod
    def _sticks(cls, sys: SysInfo | None):
        return [] if sys is None else [d for d in sys.usb_devices() if (d.vendor, d.product) in RTLSDR_IDS]

    @classmethod
    def detect(cls, sys: SysInfo | None) -> list[Device]:
        sticks = cls._sticks(sys)
        if not sticks or find_decoder() is None:
            return []
        return [Device("adsb", d.busid, f"RTL-SDR ({d.busid})", False, (Claim(f"usb:{d.busid}"),)) for d in sticks]

    @classmethod
    def missing_reason(cls, sys: SysInfo | None) -> str:
        if not cls._sticks(sys):
            return cls.missing_hint
        return "no ADS-B decoder with RTL-SDR support"

    def _present(self) -> bool:
        return any(d.busid == self.device.path for d in SysInfo(self.sysroot).usb_devices())

    def _serial(self) -> str:
        dev = next((d for d in SysInfo(self.sysroot).usb_devices() if d.busid == self.device.path), None)
        return dev.serial if dev and dev.serial else "0"

    async def run(self, ctx: SourceContext) -> None:
        base = self.workdir or Path(os.environ.get("RUNTIME_DIRECTORY") or tempfile.gettempdir())
        jsondir = Path(base) / "readsb"
        jsondir.mkdir(parents=True, exist_ok=True)
        decoder = self.decoder or find_decoder()
        if decoder is None:
            raise RuntimeError("no ADS-B decoder with RTL-SDR support")
        args = [decoder, "--device-type", "rtlsdr", "--device", self._serial(), "--write-json", str(jsondir),
                "--write-json-every", "1", "--quiet"]
        fix = ctx.fix()
        if fix is not None:
            args += ["--lat", f"{fix.lat:.5f}", "--lon", f"{fix.lon:.5f}"]
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.PIPE, start_new_session=True)
        # read stderr all the time: a full pipe would block the decoder silently
        self._stderr: collections.deque[str] = collections.deque(maxlen=20)
        reader = asyncio.create_task(self._drain(proc))
        try:
            while True:
                if proc.returncode is not None:
                    await self._exited(proc)
                try:
                    data = json.loads((jsondir / "aircraft.json").read_text())
                except (OSError, ValueError):
                    data = None
                if data:
                    for obs in parse_aircraft_json(data):
                        ctx.emit(obs)
                try:
                    await asyncio.wait_for(proc.wait(), self.poll_s)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self._stop(proc)
            reader.cancel()
            shutil.rmtree(jsondir, ignore_errors=True)

    async def _drain(self, proc) -> None:
        while line := await proc.stderr.readline():
            self._stderr.append(line.decode(errors="replace").rstrip())

    async def _exited(self, proc) -> None:
        await asyncio.sleep(0.05)  # let the reader take the last lines
        err = "\n".join(self._stderr).strip()
        last = err.splitlines()[-1] if err else f"exit code {proc.returncode}"
        if any(m in err for m in BUSY_MARKERS):
            raise RuntimeError("RTL-SDR is in use by another program")
        if not self._present():
            raise SourceGone(f"{self.device.path}: RTL-SDR unplugged")
        raise RuntimeError(f"decoder stopped: {last}")

    @staticmethod
    async def _stop(proc) -> None:
        if proc.returncode is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            await asyncio.wait_for(proc.wait(), 3)
        except asyncio.TimeoutError:
            os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
        except ProcessLookupError:
            pass
