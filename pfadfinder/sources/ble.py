"""Bluetooth LE and classic discovery through BlueZ (D-Bus).

A USB adapter is preferred; the internal UART adapter is the fallback. The
adapter claim is shared, so a BLE link to a mesh node can use it at the same
time. Discovery is duty-cycled with the scan cadence.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, ClassVar

from ..model import Claim, Device, Kind, Observation
from .base import Source, SourceContext, SourceGone, SysInfo
from .registry import register

log = logging.getLogger("pfadfinder.ble")

MAX_WINDOW_S = 10.0
DEVICE_IFACE = "org.bluez.Device1"
ADAPTER_IFACE = "org.bluez.Adapter1"


def _address_from_path(path: str) -> str:
    tail = path.rsplit("/", 1)[-1]
    return tail[4:].replace("_", ":") if tail.startswith("dev_") else ""


def props_to_observation(path: str, props: dict, now: datetime) -> Observation | None:
    rssi = props.get("RSSI")
    if rssi is None:
        return None
    address = (props.get("Address") or _address_from_path(path)).lower()
    name = props.get("Name") or props.get("Alias") or ""
    if name.replace("-", ":").lower() == address:
        name = ""  # BlueZ uses the address as alias when there is no name
    mfgr = props.get("ManufacturerData") or {}
    mfgr_id = min(mfgr) if mfgr else None
    if props.get("Class") is not None:
        kind, auth, freq = Kind.BT, "Misc [BT]", int(props["Class"])
    else:
        kind, auth, freq = Kind.BLE, "Misc [LE]", int(props.get("Appearance") or 0)
    return Observation(kind, address, name, int(rssi), now, channel=0, frequency=freq, auth=auth, mfgr_id=mfgr_id)


class DeviceCache:
    """BlueZ device properties for one adapter, kept in sync from D-Bus signals.

    Seeded from GetManagedObjects so devices BlueZ already knows keep their
    Class/Name/Address; entries are dropped on InterfacesRemoved so rotating
    random addresses do not grow memory. `gone` turns True when the adapter
    itself disappears (unplugged dongle, bluetoothd restart).
    """

    def __init__(self, adapter_path: str, on_device: Callable[[str, dict], None]) -> None:
        self.adapter_path = adapter_path
        self.prefix = adapter_path + "/"
        self.on_device = on_device
        self.props: dict[str, dict] = {}
        self.gone = False

    def seed(self, objects: dict) -> None:
        for path, ifaces in objects.items():
            if path.startswith(self.prefix) and DEVICE_IFACE in ifaces:
                self.props[path] = dict(ifaces[DEVICE_IFACE])

    def added(self, path: str, ifaces: dict) -> None:
        if path.startswith(self.prefix) and DEVICE_IFACE in ifaces:
            self.props[path] = dict(ifaces[DEVICE_IFACE])
            if "RSSI" in self.props[path]:
                self.on_device(path, self.props[path])

    def changed(self, path: str, changed: dict) -> None:
        if not path.startswith(self.prefix):
            return
        cur = self.props.setdefault(path, {})
        cur.update(changed)
        if "RSSI" in changed:
            self.on_device(path, cur)

    def removed(self, path: str, ifaces: list) -> None:
        if path == self.adapter_path and ADAPTER_IFACE in ifaces:
            self.gone = True
        elif DEVICE_IFACE in ifaces:
            self.props.pop(path, None)


class BluezAdapter:
    """Thin D-Bus wrapper around one org.bluez.Adapter1. Verified on hardware only."""

    def __init__(self, hci: str) -> None:
        self.hci = hci
        self.path = f"/org/bluez/{hci}"
        self._bus = None
        self._adapter = None
        self.cache: DeviceCache | None = None

    @property
    def gone(self) -> bool:
        return self.cache is not None and self.cache.gone

    async def open(self, on_device: Callable[[str, dict], None]) -> None:
        from dbus_fast import BusType, Message, MessageType, Variant, unpack_variants
        from dbus_fast.aio import MessageBus

        self.cache = cache = DeviceCache(self.path, on_device)
        self._bus = bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

        def handler(msg: Message):
            if msg.message_type != MessageType.SIGNAL:
                return None
            if msg.member == "InterfacesAdded":
                path, ifaces = msg.body
                cache.added(path, unpack_variants(ifaces))
            elif msg.member == "InterfacesRemoved":
                path, ifaces = msg.body
                cache.removed(path, list(ifaces))
            elif msg.member == "PropertiesChanged" and msg.path:
                iface, changed, _ = msg.body
                if iface == DEVICE_IFACE:
                    cache.changed(msg.path, unpack_variants(changed))
            elif msg.member == "NameOwnerChanged" and msg.body and msg.body[0] == "org.bluez":
                cache.gone = True  # bluetoothd restarted or stopped
            return None

        bus.add_message_handler(handler)
        for rule in ("type='signal',sender='org.bluez',interface='org.freedesktop.DBus.ObjectManager'",
                     "type='signal',sender='org.bluez',interface='org.freedesktop.DBus.Properties',"
                     "member='PropertiesChanged',arg0='org.bluez.Device1'",
                     "type='signal',sender='org.freedesktop.DBus',member='NameOwnerChanged',arg0='org.bluez'"):
            await bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                                   interface="org.freedesktop.DBus", member="AddMatch",
                                   signature="s", body=[rule]))

        intro = await bus.introspect("org.bluez", self.path)
        obj = bus.get_proxy_object("org.bluez", self.path, intro)
        self._adapter = obj.get_interface(ADAPTER_IFACE)
        props = obj.get_interface("org.freedesktop.DBus.Properties")
        if not (await props.call_get(ADAPTER_IFACE, "Powered")).value:
            await props.call_set(ADAPTER_IFACE, "Powered", Variant("b", True))
        await self._adapter.call_set_discovery_filter(
            {"Transport": Variant("s", "auto"), "DuplicateData": Variant("b", True)})

        root_intro = await bus.introspect("org.bluez", "/")
        manager = bus.get_proxy_object("org.bluez", "/", root_intro).get_interface(
            "org.freedesktop.DBus.ObjectManager")
        cache.seed(unpack_variants(await manager.call_get_managed_objects()))

    async def start(self) -> None:
        from dbus_fast import DBusError
        try:
            await self._adapter.call_start_discovery()
        except DBusError as e:
            if "InProgress" not in str(e.type):
                raise

    async def stop(self) -> None:
        from dbus_fast import DBusError
        try:
            await self._adapter.call_stop_discovery()
        except DBusError as e:
            if "NotReady" not in str(e.type) and "Failed" not in str(e.type):
                raise

    async def close(self) -> None:
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None


@register
class BleSource(Source):
    name: ClassVar[str] = "ble"
    kinds: ClassVar[frozenset[Kind]] = frozenset({Kind.BLE, Kind.BT})
    power_w: ClassVar[float] = 0.3
    missing_hint: ClassVar[str] = "no Bluetooth adapter found"
    pause_poll = 2.0

    def __init__(self, device: Device) -> None:
        super().__init__(device)
        self.adapter_factory: Callable[[str], BluezAdapter] = BluezAdapter
        self.sleep = asyncio.sleep

    @classmethod
    def detect(cls, sys: SysInfo | None) -> list[Device]:
        if sys is None:
            return []
        hcis = sys.hci_adapters()
        order = [h for h in hcis if h.bus == "usb"] + [h for h in hcis if h.bus != "usb"]
        return [Device("ble", h.name, f"USB dongle ({h.name})" if h.bus == "usb" else f"internal ({h.name})",
                       h.bus != "usb", (Claim(f"hci:{h.name}", exclusive=False),)) for h in order]

    async def run(self, ctx: SourceContext) -> None:
        adapter = self.adapter_factory(self.device.path)

        def on_device(path: str, props: dict) -> None:
            obs = props_to_observation(path, props, datetime.now(timezone.utc))
            if obs is not None:
                ctx.emit(obs)

        discovering = False
        try:
            await adapter.open(on_device)
            while True:
                if getattr(adapter, "gone", False):
                    raise SourceGone(f"{self.device.path}: Bluetooth adapter disappeared")
                interval = ctx.scan_interval()
                if interval is None:
                    if discovering:
                        await adapter.stop()
                        discovering = False
                    await self.sleep(self.pause_poll)
                    continue
                window = min(interval, MAX_WINDOW_S)
                if not discovering:
                    await adapter.start()
                    discovering = True
                await self.sleep(window)
                if getattr(adapter, "gone", False):
                    continue
                rest = interval - window
                if rest > 0:
                    await adapter.stop()
                    discovering = False
                    await self.sleep(rest)
        finally:
            await adapter.close()
