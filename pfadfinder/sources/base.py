"""Source adapter interface and hardware discovery."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Protocol

from ..model import Device, Fix, Kind, Observation


class SourceGone(Exception):
    """The source's device disappeared (unplugged); the core re-detects."""


class SourceContext(Protocol):
    def emit(self, obs: Observation) -> None: ...
    def fix(self) -> Fix | None: ...
    def update_fix(self, device: str, fix: Fix) -> None: ...
    def scan_interval(self) -> float | None: ...
    def report_error(self, source: str, message: str) -> None: ...


@dataclass(frozen=True)
class NetIface:
    name: str
    driver: str
    bus: str  # "usb" | "sdio" | "other"


@dataclass(frozen=True)
class HciAdapter:
    name: str
    bus: str  # "usb" | "other"


def _bus_of(device_path: str) -> str:
    if "/usb" in device_path:
        return "usb"
    if "/mmc" in device_path:
        return "sdio"
    return "other"


@dataclass(frozen=True)
class UsbDevice:
    busid: str
    vendor: str
    product: str
    serial: str


@dataclass(frozen=True)
class SerialPort:
    dev: str           # /dev/ttyACM0
    name: str          # ttyACM0
    busid: str         # USB device, e.g. 1-1.2
    vendor: str
    product: str
    serial: str
    devnum: str        # changes on every re-plug
    product_name: str


class SysInfo:
    """Read-only view of sysfs, rooted at `root` so tests can fake it."""

    def __init__(self, root: Path = Path("/")) -> None:
        self.root = Path(root)

    def _p(self, rel: str) -> Path:
        return self.root / rel.lstrip("/")

    def exists(self, rel: str) -> bool:
        return self._p(rel).exists()

    def net_ifaces(self) -> list[NetIface]:
        out = []
        base = self._p("sys/class/net")
        for d in sorted(base.iterdir()) if base.is_dir() else []:
            if not ((d / "wireless").exists() or (d / "phy80211").exists()):
                continue
            dev = d / "device"
            driver = ""
            if (dev / "driver").exists():
                driver = os.path.basename(os.path.realpath(dev / "driver"))
            out.append(NetIface(d.name, driver, _bus_of(os.path.realpath(dev)) if dev.exists() else "other"))
        return out

    def usb_devices(self) -> list[UsbDevice]:
        out = []
        base = self._p("sys/bus/usb/devices")
        for d in sorted(base.iterdir()) if base.is_dir() else []:
            try:
                vid = (d / "idVendor").read_text().strip()
                pid = (d / "idProduct").read_text().strip()
            except OSError:
                continue
            try:
                serial = (d / "serial").read_text().strip()
            except OSError:
                serial = ""
            out.append(UsbDevice(d.name, vid, pid, serial))
        return out

    def usb_serial_ports(self) -> list[SerialPort]:
        """Serial ports (ttyACM*, ttyUSB*) backed by a USB device."""
        out = []
        base = self._p("sys/class/tty")
        root = os.path.realpath(self.root)
        for d in sorted(base.iterdir()) if base.is_dir() else []:
            if not (d / "device").exists():
                continue
            p = Path(os.path.realpath(d / "device"))
            while str(p).startswith(root) and p != p.parent and not (p / "idVendor").exists():
                p = p.parent
            if not (p / "idVendor").exists():
                continue

            def read(f: str) -> str:
                try:
                    return (p / f).read_text().strip()
                except OSError:
                    return ""
            out.append(SerialPort(f"/dev/{d.name}", d.name, p.name, read("idVendor"), read("idProduct"),
                                  read("serial"), read("devnum"), read("product")))
        return out

    def hci_adapters(self) -> list[HciAdapter]:
        out = []
        base = self._p("sys/class/bluetooth")
        for d in sorted(base.iterdir()) if base.is_dir() else []:
            if not d.name.startswith("hci") or ":" in d.name:
                continue
            dev = d / "device"
            bus = _bus_of(os.path.realpath(dev)) if dev.exists() else "other"
            out.append(HciAdapter(d.name, "usb" if bus == "usb" else "other"))
        return out


class Source(ABC):
    name: ClassVar[str]
    kinds: ClassVar[frozenset[Kind]]
    power_w: ClassVar[float]
    missing_hint: ClassVar[str] = "no device found"

    def __init__(self, device: Device) -> None:
        self.device = device

    @classmethod
    def missing_reason(cls, sys: SysInfo | None) -> str:
        """Why detect() found nothing; sources with more than one cause override this."""
        return cls.missing_hint

    @classmethod
    @abstractmethod
    def detect(cls, sys: SysInfo) -> list[Device]:
        """Present devices for this source, best first. No side effects."""

    @abstractmethod
    async def run(self, ctx: SourceContext) -> None:
        """Run until cancelled. Raising means the source failed."""
