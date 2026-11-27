"""Power knobs in sysfs (CPU governor, cores, Ethernet chip) and battery readout.

Write access for the non-root service comes from the tmpfiles.d snippet
installed by the package; the Ethernet chip has none, as the store rejects
udev RUN rules. Any write that fails is logged once and
skipped: power saving is best effort, never a reason to stop logging.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("pfadfinder.power")

ETHERNET_USB_ID = ("0fe6", "9900")  # SR9900A on the CardputerZero hub
GOVERNOR = "sys/devices/system/cpu/cpufreq/policy0/scaling_governor"


@dataclass(frozen=True)
class BatteryInfo:
    percent: int | None
    voltage_v: float | None
    power_w: float | None


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _int(path: Path) -> int | None:
    v = _read(path)
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


class PowerManager:
    def __init__(self, sysroot: Path = Path("/")) -> None:
        self.root = Path(sysroot)
        self._saved: list[tuple[Path, str]] = []
        self._warned: set[str] = set()

    def _set(self, path: Path, value: str) -> None:
        old = _read(path)
        if old is None or old == value:
            return
        try:
            path.write_text(value + "\n")
        except OSError as e:
            if str(path) not in self._warned:
                self._warned.add(str(path))
                log.warning("cannot write %s: %s", path, e)
            return
        self._saved.append((path, old))

    def enter_wardrive(self, cores: int) -> None:
        self._set(self.root / GOVERNOR, "powersave")
        for n in range(max(1, cores), 4):
            self._set(self.root / f"sys/devices/system/cpu/cpu{n}/online", "0")
        usb = self.root / "sys/bus/usb/devices"
        for dev in sorted(usb.iterdir()) if usb.is_dir() else []:
            if (_read(dev / "idVendor"), _read(dev / "idProduct")) == ETHERNET_USB_ID:
                self._set(dev / "authorized", "0")

    def leave_wardrive(self) -> None:
        while self._saved:
            path, old = self._saved.pop()
            try:
                path.write_text(old + "\n")
            except OSError as e:
                log.warning("cannot restore %s: %s", path, e)

    def battery(self) -> BatteryInfo | None:
        base = self.root / "sys/class/power_supply"
        for d in sorted(base.iterdir()) if base.is_dir() else []:
            if _read(d / "type") != "Battery":
                continue
            volt_uv = _int(d / "voltage_now")
            power_uw = _int(d / "power_now")
            current_ua = _int(d / "current_now")
            if power_uw is not None:
                power = abs(power_uw) / 1e6
            elif current_ua is not None and volt_uv is not None:
                power = abs(current_ua) * volt_uv / 1e12
            else:
                power = None
            return BatteryInfo(_int(d / "capacity"), None if volt_uv is None else volt_uv / 1e6, power)
        return None
