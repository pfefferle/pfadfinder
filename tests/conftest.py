from datetime import datetime, timezone

import pytest

from pfadfinder.model import Fix, Kind, Observation

UTC = timezone.utc


def make_fix(lat=52.52, lon=13.405, alt_m=34.2, accuracy_m=4.5, speed_mps=0.0,
             time=datetime(2026, 11, 19, 12, 0, 5, tzinfo=UTC), mode=3):
    return Fix(lat, lon, alt_m, accuracy_m, speed_mps, time, mode)


def make_obs(kind=Kind.WIFI, id="aa:bb:cc:dd:ee:ff", name="MyNet", rssi=-55,
             time=datetime(2026, 11, 19, 12, 0, 5, tzinfo=UTC), fix="default", **kw):
    if fix == "default":
        fix = make_fix(time=time)
    return Observation(kind, id, name, rssi, time, fix=fix, **kw)


@pytest.fixture
def obs_factory():
    return make_obs


@pytest.fixture
def fix_factory():
    return make_fix


def build_fake_sys(root, ifaces=(), hcis=()):
    """Fake sysfs. ifaces: (name, driver, bus) with bus in usb|sdio|other; hcis: (name, bus)."""
    from pathlib import Path

    from pfadfinder.sources.base import SysInfo

    root = Path(root)
    bus_dirs = {"usb": "devices/platform/soc/usb1/1-1/1-1.2/1-1.2:1.0",
                "sdio": "devices/platform/soc/mmc_host/mmc1/mmc1:0001/mmc1:0001:1",
                "other": "devices/platform/soc/serial0/serial0-0",
                "uart": "devices/platform/soc/serial0/serial0-0"}
    for i, (name, driver, bus) in enumerate(ifaces):
        dev = root / "sys" / bus_dirs[bus] / f"net{i}"
        dev.mkdir(parents=True, exist_ok=True)
        drv = root / "sys/bus/drivers" / driver
        drv.mkdir(parents=True, exist_ok=True)
        (dev / "driver").symlink_to(drv)
        cls = root / "sys/class/net" / name
        cls.mkdir(parents=True)
        (cls / "device").symlink_to(dev)
        (cls / "wireless").mkdir()
    for i, (name, bus) in enumerate(hcis):
        dev = root / "sys" / bus_dirs[bus] / f"bt{i}"
        dev.mkdir(parents=True, exist_ok=True)
        cls = root / "sys/class/bluetooth" / name
        cls.mkdir(parents=True)
        (cls / "device").symlink_to(dev)
    return SysInfo(root)


@pytest.fixture
def fake_sys(tmp_path):
    return lambda ifaces=(), hcis=(): build_fake_sys(tmp_path / "root", ifaces, hcis)
