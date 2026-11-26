import asyncio
from pathlib import Path

import pytest

from pfadfinder.model import Kind
from pfadfinder.sources.base import SourceGone
from pfadfinder.sources.registry import all_sources
from pfadfinder.sources.wifi import WifiSource, auth_mode, decode_iw_ssid, parse_iw_scan
from tests.conftest import make_obs

NOW = make_obs().time
SCAN = (Path(__file__).parent.parent / "fixtures/iw_scan.txt").read_text()


def by_mac():
    return {o.id: o for o in parse_iw_scan(SCAN, NOW)}


def test_parse_counts_and_fields():
    obs = by_mac()
    assert len(obs) == 8
    o = obs["00:11:22:33:44:01"]
    assert (o.kind, o.name, o.channel, o.frequency, o.rssi, o.time) == (Kind.WIFI, "MyNet", 6, 2437, -55, NOW)
    assert obs["00:11:22:33:44:02"].channel == 36 and obs["00:11:22:33:44:02"].frequency == 5180


def test_auth_wpa2_psk():
    assert by_mac()["00:11:22:33:44:01"].auth == "[WPA2-PSK-CCMP][ESS]"


def test_auth_mixed_wpa2_wpa3():
    assert by_mac()["00:11:22:33:44:02"].auth == "[WPA2-PSK-CCMP][WPA3-SAE-CCMP][ESS]"


def test_auth_wpa1_tkip():
    assert by_mac()["00:11:22:33:44:03"].auth == "[WPA-PSK-TKIP][ESS]"


def test_auth_wep():
    assert by_mac()["00:11:22:33:44:04"].auth == "[WEP][ESS]"


def test_auth_open():
    assert by_mac()["00:11:22:33:44:05"].auth == "[ESS]"


def test_auth_eap_multi_cipher():
    assert by_mac()["00:11:22:33:44:06"].auth == "[WPA2-EAP-CCMP+TKIP][ESS]"


def test_auth_wpa3_only():
    assert by_mac()["00:11:22:33:44:07"].auth == "[WPA3-SAE-CCMP][ESS]"


def test_auth_mode_direct():
    assert auth_mode(None, [], None, [], privacy=False, ess=False) == ""


def test_hidden_ssid_empty():
    assert by_mac()["00:11:22:33:44:06"].name == ""


def test_utf8_and_comma_ssid():
    assert by_mac()["00:11:22:33:44:05"].name == "Café,1"


def test_invalid_utf8_replaced():
    assert decode_iw_ssid(r"bad\xff\xfebytes") == "bad��bytes"


def test_registered():
    assert all_sources()["wifi"] is WifiSource


def test_detect_order(fake_sys):
    sys = fake_sys(ifaces=[("wlan0", "brcmfmac", "sdio"), ("wlan1", "rtl8xxxu", "usb")])
    devs = WifiSource.detect(sys)
    assert [d.path for d in devs] == ["wlan1", "wlan0"]
    assert devs[0].internal is False and devs[1].internal is True
    assert devs[0].claims[0].resource == "wlan:wlan1" and devs[0].claims[0].exclusive


class Ctx:
    def __init__(self, interval):
        self._interval = interval
        self.emitted = []

    def scan_interval(self):
        return self._interval

    def emit(self, obs):
        self.emitted.append(obs)


def fake_iw(tmp_path, stdout, code=0, stderr=""):
    script = tmp_path / "iw"
    script.write_text(f"#!/bin/sh\necho called >> {tmp_path}/calls\ncat <<'X'\n{stdout}X\n"
                      f"echo '{stderr}' >&2\nexit {code}\n")
    script.chmod(0o755)
    return str(script)


async def test_run_paused_without_fix(tmp_path):
    src = WifiSource(_dev())
    src.iw = fake_iw(tmp_path, SCAN)
    src.pause_poll = 0.01
    task = asyncio.create_task(src.run(Ctx(None)))
    await asyncio.sleep(0.1)
    task.cancel()
    assert not (tmp_path / "calls").exists()


async def test_run_emits(tmp_path):
    src = WifiSource(_dev())
    src.iw = fake_iw(tmp_path, SCAN)
    ctx = Ctx(0.01)
    task = asyncio.create_task(src.run(ctx))
    for _ in range(100):
        if ctx.emitted:
            break
        await asyncio.sleep(0.02)
    task.cancel()
    assert len(ctx.emitted) >= 8


async def test_no_such_device_raises_source_gone(tmp_path):
    src = WifiSource(_dev())
    src.iw = fake_iw(tmp_path, "", code=237, stderr="command failed: No such device (-19)")
    with pytest.raises(SourceGone):
        await src.run(Ctx(0.01))


def _dev():
    from pfadfinder.model import Claim, Device
    return Device("wifi", "wlan9", "wlan9", False, (Claim("wlan:wlan9"),))


def test_stale_cached_entries_dropped():
    stale = (Path(__file__).parent.parent / "fixtures/iw_scan_stale.txt").read_text()
    macs = {o.id for o in parse_iw_scan(SCAN + stale, NOW)}
    assert "00:11:22:33:44:99" not in macs and "00:11:22:33:44:01" in macs
