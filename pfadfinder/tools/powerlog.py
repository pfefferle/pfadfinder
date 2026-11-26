"""Log battery draw while a mode runs, for runtime measurements.

    python3 -m pfadfinder.tools.powerlog --out drive.csv [--interval 10]
    python3 -m pfadfinder.tools.powerlog --summary drive.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from ..config import Paths
from ..power import PowerManager
from ..ui.client import CoreClient

HEADER = ["time", "percent", "voltage_v", "power_w", "mode", "wifi_device", "ble_device", "screen"]
BATTERY_WH = 6.5  # 3.7 V x 1750 mAh


def screen_state(sysroot: Path = Path("/")) -> str:
    base = Path(sysroot) / "sys/class/backlight"
    for d in sorted(base.iterdir()) if base.is_dir() else []:
        try:
            return "off" if int((d / "brightness").read_text()) == 0 else "on"
        except (OSError, ValueError):
            break
    return ""


def sample_row(power, client, now: datetime, screen: str) -> list:
    b = power.battery()
    res = client.call("status")
    st = (res.get("status") or {}) if res.get("ok") else {}
    sources = st.get("sources") or {}

    def dev(name: str) -> str:
        return (sources.get(name) or {}).get("device", "")

    return [now.isoformat(),
            "" if b is None or b.percent is None else b.percent,
            "" if b is None or b.voltage_v is None else b.voltage_v,
            "" if b is None or b.power_w is None else b.power_w,
            st.get("mode") or "", dev("wifi"), dev("ble"), screen]


def summarize(path: Path) -> dict:
    watts = []
    with Path(path).open(newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                watts.append(float(row["power_w"]))
            except (TypeError, ValueError):
                continue
    if not watts:
        return {"samples": 0, "avg_power_w": None, "est_runtime_h": None}
    avg = sum(watts) / len(watts)
    return {"samples": len(watts), "avg_power_w": avg, "est_runtime_h": BATTERY_WH / avg}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--out", type=Path)
    g.add_argument("--summary", type=Path)
    p.add_argument("--interval", type=float, default=10.0)
    args = p.parse_args()
    if args.summary:
        s = summarize(args.summary)
        if not s["samples"]:
            print("no power readings in", args.summary)
            return
        print(f"{s['samples']} samples, average {s['avg_power_w']:.2f} W, "
              f"estimated runtime {s['est_runtime_h']:.2f} h on {BATTERY_WH} Wh")
        return
    power, client = PowerManager(), CoreClient(Paths.from_env(os.environ).socket_path)
    new = not args.out.exists()
    with args.out.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(HEADER)
        try:
            while True:
                w.writerow(sample_row(power, client, datetime.now(timezone.utc), screen_state()))
                fh.flush()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            pass
    print("summary:", summarize(args.out))


if __name__ == "__main__":
    main()
