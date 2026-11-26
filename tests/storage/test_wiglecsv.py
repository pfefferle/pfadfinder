import csv
import io

from pfadfinder.model import Kind
from pfadfinder.storage.wiglecsv import header, row
from tests.conftest import make_fix, make_obs


def test_header():
    lines = header("0.1.0").splitlines()
    assert lines[0] == ("WigleWifi-1.6,appRelease=0.1.0,model=CardputerZero,release=0.1.0,"
                        "device=pfadfinder,display=pfadfinder,board=CM0,brand=M5Stack,star=Sol,body=3,subBody=0")
    assert lines[1] == ("MAC,SSID,AuthMode,FirstSeen,Channel,Frequency,RSSI,CurrentLatitude,"
                        "CurrentLongitude,AltitudeMeters,AccuracyMeters,RCOIs,MfgrId,Type")
    assert header("0.1.0").endswith("\n")


def test_wifi_row():
    obs = make_obs(auth="[WPA2-PSK-CCMP][ESS]", channel=6, frequency=2437)
    assert row(obs) == ("aa:bb:cc:dd:ee:ff,MyNet,[WPA2-PSK-CCMP][ESS],2026-11-19 12:00:05,6,2437,-55,"
                        "52.5200000,13.4050000,34.2,4.5,,,WIFI\n")


def test_ssid_escaping():
    obs = make_obs(name='a,"b"\nc', auth="[ESS]")
    line = row(obs)
    assert '"a,""b"" c"' in line
    assert line.count("\n") == 1
    parsed = next(csv.reader(io.StringIO(line)))
    assert parsed[1] == 'a,"b" c' and len(parsed) == 14


def test_ble_row():
    obs = make_obs(kind=Kind.BLE, id="11:22:33:44:55:66", name="", rssi=-70, auth="Misc [LE]", mfgr_id=76)
    assert row(obs) == ("11:22:33:44:55:66,,Misc [LE],2026-11-19 12:00:05,0,0,-70,"
                        "52.5200000,13.4050000,34.2,4.5,,76,BLE\n")


def test_missing_altitude_writes_empty_field():
    obs = make_obs(fix=make_fix(alt_m=None))
    assert ",13.4050000,,4.5,,,WIFI\n" in row(obs)
