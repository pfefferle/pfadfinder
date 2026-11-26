"""GPS through gpsd. gpsd owns every receiver (USB, Grove UART, cap GNSS)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import replace
from datetime import datetime
from typing import Callable, ClassVar
from urllib.parse import urlparse

from ..model import Device, Fix, Kind
from .base import Source, SourceContext, SysInfo
from .registry import register

log = logging.getLogger("pfadfinder.gps")

FIX_MAX_AGE_S = 5.0
WATCH = b'?WATCH={"enable":true,"json":true};\n'


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_tpv(msg: dict) -> Fix | None:
    if msg.get("class") != "TPV" or (msg.get("mode") or 0) < 2:
        return None
    if msg.get("lat") is None or msg.get("lon") is None or not msg.get("time"):
        return None
    alt = msg.get("altHAE", msg.get("alt"))
    if msg.get("eph") is not None:
        acc = float(msg["eph"])
    elif msg.get("epx") is not None or msg.get("epy") is not None:
        acc = float(max(msg.get("epx") or 0.0, msg.get("epy") or 0.0))
    else:
        acc = 0.0
    speed = msg.get("speed")
    return Fix(float(msg["lat"]), float(msg["lon"]), None if alt is None else float(alt), acc,
               None if speed is None else float(speed), _time(msg["time"]), int(msg["mode"]))


class FixSelector:
    """Keeps the latest fix per receiver; best() picks 3D over 2D, then accuracy.

    Freshness is measured from when a fix arrived (monotonic clock), not from
    GPS time against the system clock, which may be off until synced.
    """

    def __init__(self, mono: Callable[[], float] = time.monotonic) -> None:
        self._mono = mono
        self._fixes: dict[str, tuple[Fix, float]] = {}

    def update(self, device: str, fix: Fix) -> None:
        self._fixes[device] = (fix, self._mono())

    def best(self) -> Fix | None:
        now = self._mono()
        fresh = [f for f, at in self._fixes.values() if now - at <= FIX_MAX_AGE_S]
        if not fresh:
            return None
        return min(fresh, key=lambda f: (-f.mode, f.accuracy_m))


def _gpsd_label(sys: SysInfo | None) -> str:
    """What gpsd reads, from /etc/default/gpsd: e.g. "gpsd: /dev/serial0, USB auto"."""
    if sys is None:
        return "gpsd"
    try:
        text = (sys.root / "etc/default/gpsd").read_text()
    except OSError:
        return "gpsd"
    values = dict(re.findall(r'^([A-Z_]+)="?([^"\n]*)"?', text, re.M))
    parts = values.get("DEVICES", "").split()
    if values.get("USBAUTO", "").lower() == "true":
        parts.append("USB auto")
    return "gpsd: " + ", ".join(parts) if parts else "gpsd"


@register
class GpsSource(Source):
    name: ClassVar[str] = "gps"
    kinds: ClassVar[frozenset[Kind]] = frozenset()
    power_w: ClassVar[float] = 0.15
    missing_hint: ClassVar[str] = "gpsd not available"
    reconnect_delay = 5.0

    @classmethod
    def detect(cls, sys: SysInfo | None) -> list[Device]:
        return [Device("gps", "gpsd://127.0.0.1:2947", _gpsd_label(sys), True, ())]

    async def run(self, ctx: SourceContext) -> None:
        url = urlparse(self.device.path)
        host, port = url.hostname or "127.0.0.1", url.port or 2947
        outage_reported = False
        while True:
            try:
                reader, writer = await asyncio.open_connection(host, port)
            except OSError as e:
                if not outage_reported:
                    ctx.report_error("gps", f"gpsd not reachable: {e}")
                    outage_reported = True
                await asyncio.sleep(self.reconnect_delay)
                continue
            outage_reported = False
            sats: dict[str, int] = {}
            try:
                writer.write(WATCH)
                await writer.drain()
                while line := await reader.readline():
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    device = msg.get("device", "?")
                    if msg.get("class") == "SKY" and msg.get("uSat") is not None:
                        sats[device] = int(msg["uSat"])
                    fix = parse_tpv(msg)
                    if fix is not None:
                        ctx.update_fix(device, replace(fix, sats=sats.get(device)))
            finally:
                writer.close()
            ctx.report_error("gps", "gpsd connection lost")
            outage_reported = True
            await asyncio.sleep(self.reconnect_delay)
