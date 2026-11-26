from pathlib import Path

from pfadfinder.modes import load_modes

SHIPPED = Path(__file__).resolve().parent.parent / "pfadfinder" / "modes.toml"


def test_shipped_modes():
    modes = load_modes(SHIPPED)
    assert modes["drive"].sources == ["gps", "wifi", "ble"]
    assert modes["drive"].title == "Drive"
    assert list(modes) == ["drive", "sky", "mesh"]  # uploading lives on the Upload screen, not in a mode
    assert modes["sky"].title == "Sky" and modes["sky"].sources == ["adsb"]
    # a mesh node on USB joins both modes when it is plugged in
    assert modes["drive"].optional == ["mesh-usb"] and modes["sky"].optional == ["gps", "mesh-usb"]
    # mesh alone: the nodes send their own position, so GPS is optional
    assert modes["mesh"].title == "Mesh" and modes["mesh"].sources == ["mesh-usb"]
    assert modes["mesh"].optional == ["gps"]


def test_load_defaults(tmp_path):
    p = tmp_path / "m.toml"
    p.write_text('[x]\ntitle = "X"\nsources = ["a"]\n')
    m = load_modes(p)["x"]
    assert m.optional == [] and m.warn_external_power is False and m.name == "x"


def test_shipped_modes_have_descriptions():
    modes = load_modes(SHIPPED)
    assert modes["drive"].description == "Wi-Fi and Bluetooth with GPS,\non foot or in the car.\nA mesh node on USB is used too."
    assert modes["sky"].description == "Aircraft with an RTL-SDR stick,\nno GPS fix needed.\nA mesh node on USB is used too."
    assert modes["mesh"].description == ("Only a Meshtastic or MeshCore node on USB.\n"
                                         "Nodes send their own position,\nso GPS is optional.")
    from pfadfinder.ui.views import COLS
    for m in modes.values():
        assert all(len(line) <= COLS for line in m.description.splitlines()), m.name
