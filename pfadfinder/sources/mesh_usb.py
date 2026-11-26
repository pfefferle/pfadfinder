"""A Meshtastic or MeshCore node on USB serial.

RocketGod's capture workers (vendor/wdgwars_uploader/mesh_capture.py) talk to
the node in a thread and keep every node it hears in a MeshStore. This source
runs one of them for the port it was given, reads the store every few seconds
and emits one observation per node that changed. Which firmware the node runs
is only known after asking it: a Meshtastic node answers a raw config request
at once, so that is asked first; otherwise MeshCore, which answers or refuses
quickly, is tried before Meshtastic. The workers run with adverts and hop
changes switched off, so nothing is transmitted.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, ClassVar

from ..model import Claim, Device, Fix, Kind, Observation
from .base import SerialPort, Source, SourceContext, SourceGone, SysInfo
from .registry import register

log = logging.getLogger("pfadfinder.mesh")

# boards that carry Meshtastic or MeshCore: ESP32-S3/S2 native USB, nRF52 (any 239a id),
# CP210x, CH9102 and CH340 bridges
MESH_USB_IDS = {("303a", "1001"), ("303a", "4001"), ("10c4", "ea60"), ("1a86", "55d4"), ("1a86", "7523")}
MESH_USB_VENDORS = {"239a"}
NETWORKS = ("meshcore", "meshtastic")
# ToRadio{want_config_id: 1} in a Meshtastic serial frame, after the wake-up bytes the library sends too
WANT_CONFIG = b"\xc3" * 32 + bytes([0x94, 0xC3, 0x00, 0x02, 0x18, 0x01])


def _candidate(p: SerialPort) -> bool:
    return (p.vendor, p.product) in MESH_USB_IDS or p.vendor in MESH_USB_VENDORS


def default_worker(network: str, port: str, store):
    from wdgwars_uploader import mesh_capture
    # The reference opens ESP32 ports with DTR/RTS held low, against Windows toggling them. On Linux
    # pyserial drops DTR before RTS, which is the ESP32 reset signal: the board reboots mid-handshake
    # and Meshtastic waits out its whole connect timer. The libraries handle the lines themselves.
    mesh_capture._needs_no_reset = lambda port: False
    if network == "meshcore":
        return mesh_capture.MeshCoreWorker(store, port=port, transport="serial", advert_minutes=0)
    return mesh_capture.MeshtasticWorker(port, store, hop_limit=0, advert_minutes=0, transport="serial")


def answers_meshtastic(path: str, serial_cls=None, wait_s: float = 3.0) -> bool:
    """True when the node answers a Meshtastic config request with Meshtastic frames.

    Asked before any library touches the port: the MeshCore library tries several DTR settings,
    and on ESP32 native USB (M5Stack C6L) that can leave the board in its download mode, silent
    until it is replugged. DTR high and RTS low is the line state that does not reset an ESP32."""
    try:
        if serial_cls is None:
            import serial
            serial_cls = serial.Serial
        s = serial_cls()
        s.port, s.baudrate, s.timeout = path, 115200, 0.2
        s.dtr, s.rts = True, False
        s.open()
    except Exception:  # noqa: BLE001 - busy or gone: the libraries report it properly
        return False
    try:
        s.write(WANT_CONFIG)
        got, end = b"", time.monotonic() + wait_s
        while time.monotonic() < end:
            got += s.read(4096)
            if b"\x94\xc3" in got:
                return True
        return False
    except Exception:  # noqa: BLE001
        return False
    finally:
        s.close()


def _new_store():
    from wdgwars_uploader import mesh_capture
    return mesh_capture.MeshStore()


def node_observation(n: dict, receiver: Fix | None) -> Observation | None:
    network, node_id = str(n.get("network") or ""), str(n.get("node_id") or "")
    if network not in NETWORKS or not node_id:
        return None
    heard = n.get("heard_ts")
    when = datetime.fromtimestamp(heard, timezone.utc) if isinstance(heard, (int, float)) \
        else datetime.now(timezone.utc)
    lat, lon = n.get("lat"), n.get("lon")
    fix = None if lat is None or lon is None else Fix(lat, lon, None, 0.0, None, when, 2)
    return Observation(Kind.MESH_NODE, f"{network}:{node_id}", str(n.get("name") or ""), None, when, fix=fix,
                       extra={"node": n, "receiver": receiver})


@register
class MeshUsbSource(Source):
    name: ClassVar[str] = "mesh-usb"
    kinds: ClassVar[frozenset[Kind]] = frozenset({Kind.MESH_NODE})
    power_w: ClassVar[float] = 0.5  # the node draws from the Cardputer's battery; placeholder until measured
    missing_hint: ClassVar[str] = "no mesh node on USB"
    # firmware per USB serial for the life of the process; ports that answered neither, per plug-in
    _firmware: ClassVar[dict[str, str]] = {}
    _no_firmware: ClassVar[dict[tuple[str, str], str]] = {}
    _stuck: ClassVar[dict[tuple[str, str], str]] = {}  # a hung library still holds the port, per plug-in

    sysroot = Path("/")
    poll_s = 5.0
    join_s = 5.0
    probe_s = {"meshcore": 30.0, "meshtastic": 210.0}  # the reference waits up to 210 s for a node DB
    worker_factory: Callable = staticmethod(default_worker)
    firmware_probe: Callable = staticmethod(answers_meshtastic)

    @classmethod
    def _ports(cls, sys: SysInfo | None) -> list[SerialPort]:
        return [] if sys is None else [p for p in sys.usb_serial_ports() if _candidate(p)]

    @classmethod
    def detect(cls, sys: SysInfo | None) -> list[Device]:
        return [Device("mesh-usb", p.dev, f"{p.product_name or 'USB serial'} ({p.name})", False,
                       (Claim(f"usb:{p.busid}"),))
                for p in sorted(cls._ports(sys), key=lambda p: p.name)
                if (p.busid, p.devnum) not in cls._no_firmware and (p.busid, p.devnum) not in cls._stuck]

    @classmethod
    def missing_reason(cls, sys: SysInfo | None) -> str:
        for p in cls._ports(sys):
            if (p.busid, p.devnum) in cls._stuck:
                return f"{p.name} is stuck, replug the node"
            if (p.busid, p.devnum) in cls._no_firmware:
                return f"no Meshtastic or MeshCore node on {p.name}"
        return cls.missing_hint

    def _port(self) -> SerialPort | None:
        return next((p for p in SysInfo(self.sysroot).usb_serial_ports() if p.dev == self.device.path), None)

    async def _stop(self, worker, ctx: SourceContext) -> str:
        """Stop the worker; returns an error when it does not let go of the port."""
        worker.stop()
        await asyncio.to_thread(worker.join, self.join_s)
        if not worker.is_alive():
            return ""
        # the Meshtastic library can hang in its constructor and keep the port open
        msg = f"{Path(self.device.path).name} is stuck, replug the node"
        port = self._port()
        if port is not None:
            self._stuck[(port.busid, port.devnum)] = msg
        ctx.report_error(self.name, msg)
        return msg

    async def _probe(self, network: str, store, ctx: SourceContext):
        worker = self.worker_factory(network, self.device.path, store)
        worker.start()
        end = time.monotonic() + self.probe_s[network]
        try:
            while time.monotonic() < end:
                if worker.connected:
                    return worker
                if worker.error or self._port() is None:
                    break
                await asyncio.sleep(min(0.5, self.poll_s))
        except asyncio.CancelledError:  # mode stopped while the node was still connecting
            await asyncio.shield(self._stop(worker, ctx))
            raise
        stuck = await self._stop(worker, ctx)
        if stuck:
            raise RuntimeError(stuck)  # no second probe next to a hung reader
        return None

    async def run(self, ctx: SourceContext) -> None:
        port = self._port()
        if port is None:
            raise SourceGone(f"{self.device.path}: unplugged")
        key = port.serial or port.busid
        known = self._firmware.get(key)
        if known is None and await asyncio.to_thread(self.firmware_probe, self.device.path):
            known = "meshtastic"
        store = _new_store()
        worker = None
        try:
            # the remembered firmware first; serials repeat (or are missing), so the other one is tried too
            for network in ([known] if known else []) + [n for n in NETWORKS if n != known]:
                worker = await self._probe(network, store, ctx)
                if worker is not None:
                    self._firmware[key] = network
                    break
                if self._port() is None:
                    raise SourceGone(f"{self.device.path}: unplugged")
            if worker is None:
                self._firmware.pop(key, None)
                self._no_firmware[(port.busid, port.devnum)] = port.name
                raise RuntimeError(f"no Meshtastic or MeshCore node on {port.name}")
            seen: dict[str, tuple] = {}
            while True:
                if self._port() is None:
                    raise SourceGone(f"{self.device.path}: unplugged")
                receiver = ctx.fix()
                for n in store.snapshot():
                    obs = node_observation(n, receiver)
                    if obs is None:
                        continue
                    state = (n.get("heard_ts"), n.get("lat"), n.get("lon"), n.get("name"))
                    if seen.get(obs.id) != state:
                        seen[obs.id] = state
                        ctx.emit(obs)
                await asyncio.sleep(self.poll_s)
        finally:
            if worker is not None:
                await asyncio.shield(self._stop(worker, ctx))
