"""Render the Status screen with sample data: python3 -m pfadfinder.ui.screenshot --out FILE"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from . import views

SAMPLE = {
    "mode": "drive", "running": True, "started": "2026-11-19T12:00:05+00:00",
    "fix": {"lat": 52.52, "lon": 13.405, "mode": 3, "sats": 9, "speed_kmh": 48.0},
    "counts": {"WIFI": [1284, 312], "BLE": [400, 70], "BT": [106, 18]},
    "sources": {"wifi": {"device": "USB dongle", "internal": False, "state": "running", "error": ""},
                "ble": {"device": "internal (hci0)", "internal": True, "state": "running", "error": ""}},
    "battery": {"percent": 72, "voltage_v": 3.85, "power_w": 3.1}, "est_power_w": 3.2, "errors": [],
}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    args = p.parse_args()
    now = datetime(2026, 11, 19, 12, 42, 15, tzinfo=timezone.utc)
    views.render(views.compose(*views.overview(SAMPLE, now)), current=1, header=True).save(args.out)


if __name__ == "__main__":
    main()
