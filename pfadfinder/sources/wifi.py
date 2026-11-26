"""Wi-Fi via nl80211 managed-mode scans (`iw dev <if> scan`).

A USB dongle is preferred; the internal Broadcom radio (brcmfmac) is the
fallback. Triggering a scan needs CAP_NET_ADMIN, which the systemd unit grants
as an ambient capability (inherited by `iw`).
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import ClassVar

from ..geo import freq_to_channel
from ..model import Claim, Device, Kind, Observation
from .base import Source, SourceContext, SourceGone, SysInfo
from .registry import register

log = logging.getLogger("pfadfinder.wifi")

INTERNAL_DRIVERS = {"brcmfmac"}
# `iw scan` also dumps the cfg80211 cache; older entries would get the current position
MAX_CACHE_AGE_MS = 6000
_BSS = re.compile(r"^BSS ([0-9a-fA-F:]{17})")
_HEX = re.compile(r"\\x([0-9a-fA-F]{2})")

_RSN_SUITES = {
    "PSK": "WPA2-PSK", "PSK/SHA-256": "WPA2-PSK", "FT/PSK": "WPA2-PSK",
    "SAE": "WPA3-SAE", "FT/SAE": "WPA3-SAE",
    "IEEE802.1X": "WPA2-EAP", "IEEE802.1X/SHA-256": "WPA2-EAP", "FT/IEEE802.1X": "WPA2-EAP",
    "802.1X/SUITE-B": "WPA3-EAP", "802.1X/SUITE-B-192": "WPA3-EAP",
    "OWE": "OWE",
}
_WPA_SUITES = {"PSK": "WPA-PSK", "IEEE802.1X": "WPA-EAP"}


def decode_iw_ssid(raw: str) -> str:
    data = bytearray()
    i = 0
    while i < len(raw):
        m = _HEX.match(raw, i)
        if m:
            data.append(int(m.group(1), 16))
            i = m.end()
        else:
            data.extend(raw[i].encode("utf-8"))
            i += 1
    if data and not any(data):
        return ""  # hidden network advertised as NUL bytes
    return data.decode("utf-8", errors="replace")


def _suites(text: str) -> list[str]:
    return text.replace("IEEE 802.1X", "IEEE802.1X").split()


def auth_mode(rsn_suites: list[str] | None, rsn_pairwise: list[str], wpa_suites: list[str] | None,
              wpa_pairwise: list[str], privacy: bool, ess: bool) -> str:
    parts: list[str] = []
    if wpa_suites is not None:
        ciphers = "+".join(wpa_pairwise) or "TKIP"
        for s in dict.fromkeys(_WPA_SUITES.get(x, "WPA") for x in wpa_suites):
            parts.append(f"[{s}-{ciphers}]")
    if rsn_suites is not None:
        ciphers = "+".join(rsn_pairwise) or "CCMP"
        for s in dict.fromkeys(_RSN_SUITES.get(x, "WPA2") for x in rsn_suites):
            parts.append(f"[{s}-{ciphers}]")
    if not parts and privacy:
        parts.append("[WEP]")
    if ess:
        parts.append("[ESS]")
    return "".join(parts)


def parse_iw_scan(text: str, now: datetime) -> list[Observation]:
    out: list[Observation] = []
    bss: dict | None = None
    section: str | None = None

    def finish() -> None:
        if bss is None or bss.get("age_ms", 0) > MAX_CACHE_AGE_MS:
            return
        freq = bss.get("freq", 0.0)
        out.append(Observation(
            Kind.WIFI, bss["mac"], bss.get("ssid", ""), bss.get("rssi"), now,
            channel=freq_to_channel(freq), frequency=int(round(freq)),
            auth=auth_mode(bss.get("RSN_suites"), bss.get("RSN_pairwise", []),
                           bss.get("WPA_suites"), bss.get("WPA_pairwise", []),
                           bss.get("privacy", False), bss.get("ess", False))))

    for line in text.splitlines():
        m = _BSS.match(line)
        if m:
            finish()
            bss, section = {"mac": m.group(1).lower()}, None
            continue
        if bss is None:
            continue
        if line.startswith("\t\t"):
            item = line.strip().lstrip("* ").strip()
        elif line.startswith("\t"):
            section = None
            item = line.strip()
            sec = re.match(r"^(RSN|WPA):\s*(.*)$", item)
            if sec:
                section = sec.group(1)
                bss[f"{section}_suites"] = []
                item = sec.group(2).lstrip("* ").strip()
            elif item.startswith("freq:"):
                bss["freq"] = float(item.split(":", 1)[1])
                continue
            elif item.startswith("last seen:") and item.endswith("ms ago"):
                bss["age_ms"] = int(item.split(":", 1)[1].split()[0])
                continue
            elif item.startswith("signal:"):
                bss["rssi"] = int(round(float(item.split(":", 1)[1].split()[0])))
                continue
            elif item.startswith("SSID:"):
                bss["ssid"] = decode_iw_ssid(line.split("SSID:", 1)[1][1:])
                continue
            elif item.startswith("capability:"):
                caps = item.split(":", 1)[1].split()
                bss["ess"] = "ESS" in caps
                bss["privacy"] = "Privacy" in caps
                continue
        else:
            continue
        if section and ":" in item:
            key, value = (s.strip() for s in item.split(":", 1))
            if key == "Pairwise ciphers":
                bss[f"{section}_pairwise"] = value.split()
            elif key == "Authentication suites":
                bss[f"{section}_suites"] = _suites(value)
    finish()
    return out


@register
class WifiSource(Source):
    name: ClassVar[str] = "wifi"
    kinds: ClassVar[frozenset[Kind]] = frozenset({Kind.WIFI})
    power_w: ClassVar[float] = 0.6
    missing_hint: ClassVar[str] = "no Wi-Fi interface found"
    iw = "iw"
    pause_poll = 2.0
    scan_timeout = 15.0

    @classmethod
    def detect(cls, sys: SysInfo | None) -> list[Device]:
        if sys is None:
            return []
        ifaces = sys.net_ifaces()
        usb = [i for i in ifaces if i.bus == "usb" and i.driver not in INTERNAL_DRIVERS]
        internal = [i for i in ifaces if i.driver in INTERNAL_DRIVERS]
        devs = [Device("wifi", i.name, f"USB dongle ({i.name}, {i.driver})", False, (Claim(f"wlan:{i.name}"),))
                for i in usb]
        devs += [Device("wifi", i.name, f"internal ({i.name})", True, (Claim(f"wlan:{i.name}"),))
                 for i in internal]
        return devs

    async def scan_once(self) -> list[Observation]:
        proc = await asyncio.create_subprocess_exec(
            self.iw, "dev", self.device.path, "scan",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), self.scan_timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            log.warning("iw scan on %s timed out", self.device.path)
            return []
        if proc.returncode != 0:
            msg = err.decode(errors="replace").strip()
            if "No such device" in msg:
                raise SourceGone(f"{self.device.path}: {msg}")
            if "busy" in msg.lower():
                return []
            raise RuntimeError(f"iw scan failed on {self.device.path}: {msg}")
        return parse_iw_scan(out.decode("ascii", errors="replace"), datetime.now(timezone.utc))

    async def run(self, ctx: SourceContext) -> None:
        while True:
            interval = ctx.scan_interval()
            if interval is None:
                await asyncio.sleep(self.pause_poll)
                continue
            for obs in await self.scan_once():
                ctx.emit(obs)
            await asyncio.sleep(interval)
