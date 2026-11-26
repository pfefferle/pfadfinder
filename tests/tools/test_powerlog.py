import csv
from datetime import datetime, timezone

from pfadfinder.power import BatteryInfo
from pfadfinder.tools.powerlog import HEADER, sample_row, summarize


def test_summarize(tmp_path):
    p = tmp_path / "log.csv"
    with p.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        for watts in (3.0, 3.2, 3.4):
            w.writerow(["2026-11-19T12:00:00+00:00", 70, 3.8, watts, "drive", "USB dongle", "internal", "off"])
        w.writerow(["2026-11-19T12:00:30+00:00", 70, 3.8, "", "drive", "", "", "off"])  # no reading
    s = summarize(p)
    assert s["samples"] == 3 and abs(s["avg_power_w"] - 3.2) < 1e-9
    assert abs(s["est_runtime_h"] - 6.5 / 3.2) < 1e-9


def test_summarize_empty(tmp_path):
    p = tmp_path / "log.csv"
    p.write_text(",".join(HEADER) + "\n")
    assert summarize(p) == {"samples": 0, "avg_power_w": None, "est_runtime_h": None}


class FakePower:
    def battery(self):
        return BatteryInfo(72, 3.85, 3.1)


class FakeClient:
    def call(self, cmd, **args):
        return {"ok": True, "status": {"mode": "drive", "sources": {
            "wifi": {"device": "USB dongle (wlan1, rtl8xxxu)", "internal": False, "state": "running", "error": ""},
            "ble": {"device": "internal (hci0)", "internal": True, "state": "running", "error": ""}}}}


def test_logger_row_format():
    now = datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc)
    row = sample_row(FakePower(), FakeClient(), now, screen="off")
    assert HEADER == ["time", "percent", "voltage_v", "power_w", "mode", "wifi_device", "ble_device", "screen"]
    assert row == ["2026-11-19T12:00:00+00:00", 72, 3.85, 3.1, "drive",
                   "USB dongle (wlan1, rtl8xxxu)", "internal (hci0)", "off"]


def test_logger_row_without_core_or_battery():
    class NoPower:
        def battery(self):
            return None

    class NoCore:
        def call(self, cmd, **args):
            return {"ok": False, "error": "core not running"}

    now = datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc)
    assert sample_row(NoPower(), NoCore(), now, screen="on") == ["2026-11-19T12:00:00+00:00", "", "", "", "", "", "", "on"]
