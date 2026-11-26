from typing import ClassVar

from pfadfinder.model import Claim, Device, Kind
from pfadfinder.modes import Mode, plan_mode
from pfadfinder.sources.base import Source
from pfadfinder.sources.registry import all_sources
from pfadfinder.storage.session import WRITER_KINDS


def test_every_registered_source_is_complete():
    for name, cls in all_sources().items():
        assert cls.name == name
        assert isinstance(cls.kinds, frozenset)
        assert cls.power_w > 0
        for k in cls.kinds:
            assert k in WRITER_KINDS, f"{name} emits {k} but no session writer handles it"


def fake_source(name, claims, devices=1):
    class Fake(Source):
        kinds: ClassVar[frozenset] = frozenset({Kind.WIFI})
        power_w: ClassVar[float] = 0.1

        @classmethod
        def detect(cls, sys):
            return [Device(name, f"/dev/{name}{i}", f"{name}{i}", True, tuple(claims)) for i in range(devices)]

        async def run(self, ctx):
            pass

    Fake.name = name
    return Fake


def mode(*sources, optional=()):
    return Mode("m", "M", list(sources), list(optional), False)


def test_conflicting_exclusive_claims_unavailable():
    srcs = {"a": fake_source("a", [Claim("uart0")]), "b": fake_source("b", [Claim("uart0")])}
    plan = plan_mode(mode("a", "b"), srcs, None)
    assert not plan.available
    assert plan.reason == "a and b both need uart0"


def test_shared_claims_coexist():
    srcs = {"a": fake_source("a", [Claim("hci0", exclusive=False)]),
            "b": fake_source("b", [Claim("hci0", exclusive=False)])}
    plan = plan_mode(mode("a", "b"), srcs, None)
    assert plan.available and set(plan.devices) == {"a", "b"}


def test_missing_required_device_reason():
    srcs = {"wifi": fake_source("wifi", [], devices=0)}
    srcs["wifi"].missing_hint = "no Wi-Fi interface found"
    plan = plan_mode(mode("wifi"), srcs, None)
    assert not plan.available and plan.reason == "wifi: no Wi-Fi interface found"


def test_missing_optional_is_fine():
    srcs = {"a": fake_source("a", []), "b": fake_source("b", [], devices=0)}
    plan = plan_mode(mode("a", optional=["b"]), srcs, None)
    assert plan.available and set(plan.devices) == {"a"}


def test_unknown_source_reason():
    plan = plan_mode(mode("nope"), {}, None)
    assert not plan.available and plan.reason == "nope: unknown source"


def test_sysinfo_wifi_and_bus(fake_sys):
    sys = fake_sys(ifaces=[("wlan0", "brcmfmac", "sdio"), ("wlan1", "rtl8xxxu", "usb")],
                   hcis=[("hci0", "uart"), ("hci1", "usb")])
    by_name = {i.name: i for i in sys.net_ifaces()}
    assert by_name["wlan0"].driver == "brcmfmac" and by_name["wlan0"].bus == "sdio"
    assert by_name["wlan1"].driver == "rtl8xxxu" and by_name["wlan1"].bus == "usb"
    assert {(h.name, h.bus) for h in sys.hci_adapters()} == {("hci0", "other"), ("hci1", "usb")}
