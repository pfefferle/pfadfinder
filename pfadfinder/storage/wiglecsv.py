"""WiGLE CSV 1.6 (accepted by WiGLE, WDGWars and Wardrift)."""

from __future__ import annotations

import csv
import io
import re

from ..model import Observation

COLUMNS = ("MAC,SSID,AuthMode,FirstSeen,Channel,Frequency,RSSI,CurrentLatitude,CurrentLongitude,"
           "AltitudeMeters,AccuracyMeters,RCOIs,MfgrId,Type")

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def header(version: str) -> str:
    pre = (f"WigleWifi-1.6,appRelease={version},model=CardputerZero,release={version},"
           "device=pfadfinder,display=pfadfinder,board=CM0,brand=M5Stack,star=Sol,body=3,subBody=0")
    return f"{pre}\n{COLUMNS}\n"


def row(obs: Observation) -> str:
    fix = obs.fix
    if fix is None:
        raise ValueError("observation has no GPS fix")
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerow([
        obs.id,
        _CONTROL.sub(" ", obs.name),
        obs.auth,
        obs.time.strftime("%Y-%m-%d %H:%M:%S"),
        obs.channel,
        obs.frequency,
        "" if obs.rssi is None else obs.rssi,
        f"{fix.lat:.7f}",
        f"{fix.lon:.7f}",
        "" if fix.alt_m is None else f"{fix.alt_m:.1f}",
        f"{fix.accuracy_m:.1f}",
        "",
        "" if obs.mfgr_id is None else obs.mfgr_id,
        obs.kind.value,
    ])
    return out.getvalue()
