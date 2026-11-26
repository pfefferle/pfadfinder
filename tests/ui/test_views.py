from datetime import datetime, timezone

from pfadfinder.ui import views
from tests.ui.fixtures import STATUS_DRIVE

NOW = datetime(2026, 11, 19, 12, 42, 15, tzinfo=timezone.utc)
IDLE = {**STATUS_DRIVE, "mode": None, "running": False, "session": None, "started": None,
        "counts": {}, "sources": {}, "est_power_w": None, "fix": None}
MENU = "1 Status 2 Mode 3 Upload 4 HW 5 Set"


def test_menu_line():
    assert views.MENU_LINE == MENU
    assert [n for n, _ in views.SCREENS] == ["Status", "Mode", "Upload", "HW", "Set"]


# -- 1 Status: overview ---------------------------------------------------------
def test_overview_running():
    lines, help_ = views.overview(STATUS_DRIVE, NOW)
    assert lines[0].startswith("DRIVE ● REC") and "GPS 3D 9sat 48km/h" in lines[0]
    assert lines[0].endswith("72%") and "●" not in lines[0].replace("● REC", "")
    assert lines[1].startswith("WiFi") and "1 284" in lines[1] and "(+312 new)" in lines[1]
    assert lines[2].startswith("BLE") and "506" in lines[2] and "(+88 new)" in lines[2]
    assert "~3.2 W" in lines[3] and "~1h27 left" in lines[3] and "session 00:42:10" in lines[3]
    assert not any(l.startswith(("Mesh", "Air")) for l in lines)
    assert help_ == "enter stop Drive · ◀ ▶ pages"


def test_overview_waiting_for_gps():
    lines, _ = views.overview({**STATUS_DRIVE, "fix": None}, NOW)
    assert "waiting for GPS" in lines[0]
    assert "Scanners wait for a GPS fix." in lines


def test_overview_idle():
    lines, help_ = views.overview(IDLE, NOW)
    assert lines[0].startswith("IDLE") and "no GPS fix" in lines[0] and lines[0].endswith("72%")
    assert lines[1:4] == ["Pfadfinder logs Wi-Fi, Bluetooth, aircraft", "and mesh nodes for WiGLE, WDGWars, Wardrift.",
                          "Pick a mode on 2 Mode, or enter for Drive."]
    assert help_ == "enter start Drive · ◀ ▶ pages"


# -- 1 Status: lists --------------------------------------------------------------
ROWS = [{"id": "aa", "name": "FRITZ!Box", "rssi": -48}, {"id": "bb", "name": "", "rssi": -70}]


def test_network_list_running():
    lines, help_ = views.network_list("Wi-Fi", ROWS, seen=1284, page=1, running=True, offset=0, height=4)
    assert lines[0] == "Wi-Fi  1 284 seen"
    assert lines[1] == "▂▄▆█  -48  FRITZ!Box" and lines[2] == "▂▄    -70  (hidden)"
    assert help_ == "▲ ▼ scroll · ◀ ▶ pages · enter stop"


def test_network_list_idle_hint():
    lines, help_ = views.network_list("Bluetooth", [], seen=0, page=2, running=False, offset=0, height=4)
    assert lines[0] == "Bluetooth  0 seen" and "Start Drive to see networks here." in lines
    assert help_ == "◀ ▶ pages · enter start Drive"


def test_network_list_scroll():
    rows = [{"id": str(i), "name": f"n{i}", "rssi": -50} for i in range(10)]
    lines, _ = views.network_list("Wi-Fi", rows, seen=10, page=1, running=True, offset=3, height=4)
    assert [l.split()[-1] for l in lines[1:]] == ["n3", "n4", "n5", "n6"]


# -- 2 Mode -----------------------------------------------------------------------
MODES = {"drive": {"title": "Drive", "available": True, "reason": "", "sources": ["gps", "wifi", "ble"],
                   "optional": ["mesh-usb"]},
         "sky": {"title": "Sky", "available": False, "reason": "adsb: no RTL-SDR found", "sources": ["adsb"],
                 "optional": ["gps", "mesh-usb"]},
         "mesh": {"title": "Mesh", "available": True, "reason": "", "sources": ["mesh-usb"], "optional": ["gps"]}}


def test_mode_lines_list_the_parts_of_each_mode():
    lines, help_ = views.mode_lines(MODES, 0, "drive")
    assert lines[:3] == ["▶ Drive  GPS Wi-Fi BT (+Mesh)  ● REC", "  Sky    SDR (+GPS +Mesh)  n/a",
                         "  Mesh   Mesh (+GPS)"]
    assert help_ == "▲ ▼ select · enter start/stop"


def test_mode_lines_explain_why_the_selected_mode_is_unavailable():
    lines, _ = views.mode_lines(MODES, 1, None)
    assert lines[3:5] == ["", "! adsb: no RTL-SDR found"]
    assert "! " not in "".join(views.mode_lines(MODES, 0, None)[0])


def test_every_source_has_a_mode_label():
    from pfadfinder.sources.registry import all_sources
    assert set(all_sources()) <= set(views.PARTS)


def test_shipped_modes_fit_the_mode_screen():
    from pfadfinder.modes import load_modes
    modes = {n: {"title": m.title, "available": False, "reason": "x" * 60, "sources": m.sources,
                 "optional": m.optional, "description": m.description} for n, m in load_modes().items()}
    for i, name in enumerate(modes):
        lines = views.compose(*views.mode_lines(modes, i, name))
        assert len(lines) <= 10, lines  # 17 px rows, the 14 px font
        assert all(len(l) <= views.COLS for l in lines), lines


def test_mode_lines_describe_the_selected_mode():
    modes = {"drive": {"title": "Drive", "available": True, "reason": "",
                       "description": "Wi-Fi and Bluetooth with GPS,\non foot or in the car."},
             "sky": {"title": "Sky", "available": False, "reason": "adsb: no RTL-SDR found",
                     "description": "Aircraft with an RTL-SDR stick,\nno GPS fix needed."}}
    lines, _ = views.mode_lines(modes, 1, None)
    assert lines[2] == "" and lines[4:] == ["Aircraft with an RTL-SDR stick,", "no GPS fix needed."]
    lines, _ = views.mode_lines(modes, 0, None)
    assert lines[3:] == ["Wi-Fi and Bluetooth with GPS,", "on foot or in the car."]


# -- 3 Upload ---------------------------------------------------------------------
WIGLE = {"title": "WiGLE", "configured": True,
         "stats": {"user": "matthias", "lines": ["rank 1 234   month 56", "WiFi 10 432   BT 2 101   Cell 3"]},
         "stats_error": "", "pending": 2, "pending_unit": "sessions", "uploading": False,
         "last_upload": {"session": "s", "status": "done", "error": "", "updated_at": "2026-11-19T12:40:00+00:00"}}


def test_upload_single_site():
    lines, help_ = views.upload_page("wigle", WIGLE, page=0, pages=1)
    assert lines[0] == "WiGLE  matthias"  # no dots with one site
    assert "rank 1 234   month 56" in lines and "WiFi 10 432   BT 2 101   Cell 3" in lines
    assert lines[3].startswith("pending 2 sessions") and "last upload 12:40" in lines[3]
    assert help_ == "u upload"


def test_upload_several_sites_dots_and_keys():
    lines, help_ = views.upload_page("wigle", WIGLE, page=0, pages=3)
    assert lines[0] == "WiGLE  matthias"
    assert help_ == "u upload · a all sites · ◀ ▶ sites"


def test_upload_keys_missing():
    lines, _ = views.upload_page("wigle", {"title": "WiGLE", "configured": False}, 0, 1)
    joined = "\n".join(lines)
    assert "WiGLE keys missing" in joined and "5 Set" in joined and "~/.config/pfadfinder/config.toml" in joined
    lines, _ = views.upload_page("wdgwars", {"title": "WDGWars", "configured": False}, 1, 3)
    joined = "\n".join(lines)
    assert "WDGWars keys missing" in joined and "[wdgwars]" in joined and "5 Set" not in joined


def test_upload_pending_networks_and_stats_error():
    info = {**WIGLE, "title": "WDGWars", "stats": None, "stats_error": "no answer", "pending": 7,
            "pending_unit": "networks"}
    lines, _ = views.upload_page("wdgwars", info, 1, 3)
    assert lines[0] == "WDGWars" and "! no answer" in lines and lines[-1].startswith("pending 7 networks")


# -- 4 HW -------------------------------------------------------------------------
HW = {"gps": [{"label": "gpsd: /dev/serial0, USB auto", "internal": True, "path": "gpsd"}],
      "wifi": [{"label": "USB dongle (wlan1, rtl8xxxu)", "internal": False, "path": "wlan1"},
               {"label": "internal (wlan0)", "internal": True, "path": "wlan0"}],
      "ble": [{"label": "internal (hci0)", "internal": True, "path": "hci0"}]}


def test_hw_only_used_device_and_gps_state():
    lines, help_ = views.hw_lines(HW, STATUS_DRIVE)
    assert lines == ["Wi-Fi      USB dongle (wlan1)", "Bluetooth  internal (hci0)", "SDR        none found",
                     "Mesh       none found", "GPS        3D fix, 9 sats", "           /dev/serial0, USB auto"]
    assert help_ == ""


def test_hw_running_uses_actual_device_and_shows_errors():
    st = {**STATUS_DRIVE, "fix": None, "sources": {
        "wifi": {"device": "internal (wlan0)", "internal": True, "state": "error", "error": "wlan1: gone"},
        "ble": {"device": "internal (hci0)", "internal": True, "state": "running", "error": ""}}}
    lines, _ = views.hw_lines(HW, st)
    assert lines[0] == "Wi-Fi      error: wlan1: gone"
    assert lines[4] == "GPS        no fix"


def test_hw_none_found():
    lines, _ = views.hw_lines({"gps": HW["gps"], "wifi": [], "ble": HW["ble"], "adsb": []}, IDLE)
    assert lines[0] == "Wi-Fi      none found"


# -- 5 Set ------------------------------------------------------------------------
def test_settings():
    cfg = {"wigle_api_name": "AID1", "wigle_api_token": "set", "screen_mode": "pocket", "start_on_boot": False}
    lines, help_ = views.settings_lines(cfg, selected=0, editing=None)
    assert lines[0].startswith("▶ WiGLE API name") and "AID1" in lines[0] and "••••" in lines[1]
    assert help_ == "▲ ▼ select · enter edit"
    assert views.settings_lines(cfg, 6, None)[1] == "▲ ▼ select · enter switch"
    lines, help_ = views.settings_lines(cfg, 1, "abc")
    assert "***_" in lines[1] and "abc" not in "\n".join(lines) and help_ == "type · enter save · esc cancel"


def test_settings_cover_every_config_field():
    from dataclasses import fields
    from pfadfinder.config import Config
    not_on_screen = {"throttle_seconds", "throttle_meters"}  # tuning values, config.toml only
    assert {f.name for f in fields(Config)} - not_on_screen == {key for key, _ in views.SETTINGS}


def test_every_secret_is_masked():
    from pfadfinder.config import SECRET_FIELDS
    cfg = {k: "set" for k in SECRET_FIELDS}  # what the core sends for a secret that is set
    lines, _ = views.settings_lines(cfg, 0, None)
    for i, (key, label) in enumerate(views.SETTINGS):
        if key in SECRET_FIELDS:
            assert lines[i].endswith("••••") and "set" not in lines[i].replace(label, ""), lines[i]
            assert "abc" not in views.settings_lines(cfg, i, "abc")[0][i]


# -- service not reachable --------------------------------------------------------
def test_not_reachable():
    assert views.not_reachable() == ["Starting the background service…", "It keeps recording while this app is",
                                     "closed. This screen updates on its own.", "If it stays: sudo systemctl restart",
                                     "pfadfinder-core", "esc quit"]


# -- render ------------------------------------------------------------------------
def test_compose_adds_help_and_menu_and_alert_replaces_help():
    assert views.compose(["a"], "help") == ["a", "help", MENU]
    assert views.compose(["a"], "help", alert="disk full") == ["a", "! disk full", MENU]


def test_compose_puts_page_dots_at_the_end_of_the_help_line():
    line = views.compose(["a"], "enter stop Drive · ◀ ▶ pages", page=1, pages=3)[1]
    assert line.startswith("enter stop Drive · ◀ ▶ pages") and line.endswith(" ○●○") and len(line) == views.COLS


def test_page_dots_stay_next_to_an_alert():
    line = views.compose(["a"], "help", alert="x" * 60, page=0, pages=3)[1]
    assert line.startswith("! xxx") and line.endswith(" ●○○") and len(line) == views.COLS


def test_no_dots_for_a_single_page():
    assert views.compose(["a"], "u upload", page=0, pages=1)[1] == "u upload"


def test_signal_bars():
    assert [views.signal_bars(r) for r in (-40, -55, -60, -67, -70, -75, -90, None)] == \
        ["▂▄▆█", "▂▄▆ ", "▂▄▆ ", "▂▄  ", "▂▄  ", "▂   ", "▂   ", "    "]


def test_font_is_bundled_terminus():
    from pathlib import Path
    assert Path(views.FONT_PATH).is_file() and "Terminus" in Path(views.FONT_PATH).name
    assert (Path(views.FONT_PATH).parent / "OFL.txt").is_file()
    font = views.font_for(7)
    assert font.getlength("M") == 7 and font.getlength("M" * views.COLS) <= views.W - 6


def test_header_is_a_filled_bar_with_battery():
    img = views.render(views.compose(*views.overview(STATUS_DRIVE, NOW)), current=1, header=True)
    assert img.getpixel((1, 1)) == views.ACCENT and img.getpixel((views.W - 1, 1)) == views.ACCENT
    assert img.getpixel((1, 40)) == views.BG
    plain = views.render(views.compose(*views.overview(STATUS_DRIVE, NOW)), current=1)
    assert plain.getpixel((1, 1)) == views.BG


def test_current_tab_is_filled():
    lines = views.compose(["a"], "help")
    font = views.font_for(len(lines))
    x2 = 2 + font.getlength("1 Status") + 8 + 2  # start of the second tab
    img = views.render(lines, current=2)
    assert img.getpixel((int(x2) + 1, views.H - 2)) == views.ACCENT
    assert img.getpixel((3, views.H - 2)) == views.BG


def test_render_size():
    img = views.render(views.compose(*views.overview(STATUS_DRIVE, NOW)), current=1)
    assert img.size == (320, 170) and img.mode == "RGB"


def test_every_screen_fits_width():
    cfg = {"wigle_api_name": "AID1234567890", "wigle_api_token": "set", "screen_mode": "mount", "start_on_boot": True}
    screens = [
        views.overview(STATUS_DRIVE, NOW), views.overview(IDLE, NOW), views.overview({**STATUS_DRIVE, "fix": None}, NOW),
        views.network_list("Bluetooth", ROWS, 1284, 2, True, 0, 4),
        views.mode_lines({"drive": {"title": "Drive", "available": False, "reason": "wifi: no Wi-Fi interface found"}},
                         0, None),
        views.upload_page("wigle", WIGLE, 0, 3),
        views.upload_page("wardrift", {"title": "Wardrift", "configured": False}, 2, 3),
        views.hw_lines(HW, STATUS_DRIVE), views.settings_lines(cfg, 1, None), views.settings_lines(cfg, 0, "x" * 40),
        views.overview(SKY, NOW), views.aircraft_list([{"id": "3C6DD4", "name": "DLH4AB", "alt_m": 11277.6}], 23, True, 0, 4),
        views.overview(MESH, NOW), views.mesh_list(MESH_ROWS, 2, True, True, 0, 4),
        views.mesh_list([], 0, False, False, 0, 4), views.mesh_list([], 0, True, False, 0, 4),
        views.hw_lines({**HW, "mesh-usb": [{"label": "Heltec Wireless Stick Lite (ttyACM0)", "internal": False,
                                            "path": "/dev/ttyACM0"}]}, MESH),
    ]
    for body, help_ in screens:
        lines = views.compose(body, help_, alert="storage: No space left on device and more text here",
                              page=0, pages=3)
        font = views.font_for(len(lines))
        for line in lines:
            assert font.getlength(line) <= views.W - 6, line


SKY = {**STATUS_DRIVE, "mode": "sky", "counts": {"AIRCRAFT": [23, 5]},
       "sources": {"adsb": {"device": "RTL-SDR (1-1.3)", "internal": False, "state": "running", "error": ""}}}


def test_overview_sky_shows_air_line():
    lines, help_ = views.overview(SKY, NOW)
    assert lines[0].startswith("SKY ● REC")
    assert any(l.startswith("Air") and "23" in l and "(+5 new)" in l for l in lines)
    assert help_ == "enter stop Sky · ◀ ▶ pages"


def test_overview_drive_hides_air_line():
    lines, _ = views.overview(STATUS_DRIVE, NOW)
    assert not any(l.startswith("Air") for l in lines)


def test_aircraft_list():
    rows = [{"id": "3C6DD4", "name": "DLH4AB", "alt_m": 11277.6}, {"id": "4CA1E2", "name": "", "alt_m": None}]
    lines, help_ = views.aircraft_list(rows, seen=23, running=True, offset=0, height=4)
    assert lines == ["Aircraft  23 seen", "DLH4AB     37 000 ft", "4CA1E2     —"]
    assert help_ == "▲ ▼ scroll · ◀ ▶ pages · enter stop"
    lines, help_ = views.aircraft_list([], seen=0, running=False, offset=0, height=4)
    assert "Start Sky to see aircraft here." in lines and help_ == "◀ ▶ pages · enter start Sky"


def test_hw_sdr_line():
    hw = {**HW, "adsb": [{"label": "RTL-SDR (1-1.3)", "internal": False, "path": "1-1.3"}]}
    lines, _ = views.hw_lines(hw, STATUS_DRIVE)
    assert "SDR        RTL-SDR (1-1.3)" in lines
    lines, _ = views.hw_lines({**HW, "adsb": []}, STATUS_DRIVE)
    assert "SDR        none found" in lines


MESH = {**STATUS_DRIVE, "counts": {**STATUS_DRIVE["counts"], "MESH_NODE": [23, 5]},
        "sources": {**STATUS_DRIVE["sources"],
                    "mesh-usb": {"device": "Heltec (ttyACM0)", "internal": False, "state": "running", "error": ""}}}
MESH_ROWS = [{"id": "meshcore:a1b2c3d4e5f60718", "name": "Kiez-Rptr"},
             {"id": "meshtastic:deadbeef", "name": ""}]


def test_overview_mesh_line():
    lines, _ = views.overview(MESH, NOW)
    assert any(l.startswith("Mesh") and "23" in l and "(+5 new)" in l for l in lines)
    lines, _ = views.overview(STATUS_DRIVE, NOW)
    assert not any(l.startswith("Mesh") for l in lines)


def test_mesh_list():
    lines, help_ = views.mesh_list(MESH_ROWS, seen=2, running=True, node=True, offset=0, height=4)
    assert lines == ["Mesh  2 seen", "Kiez-Rptr             MC", "deadbeef              MT"]
    assert help_ == "▲ ▼ scroll · ◀ ▶ pages · enter stop"
    lines, help_ = views.mesh_list([], seen=0, running=False, node=False, offset=0, height=4)
    assert lines[1:] == ["Plug a Meshtastic or MeshCore node into", "USB, then press enter for Mesh."]
    assert help_ == "◀ ▶ pages · enter start Mesh"
    lines, _ = views.mesh_list([], seen=0, running=True, node=False, offset=0, height=4)
    assert lines[1:] == ["No mesh node was plugged in at the start."]


def test_hw_mesh_line():
    hw = {**HW, "mesh-usb": [{"label": "Heltec (ttyACM0)", "internal": False, "path": "/dev/ttyACM0"}]}
    lines, _ = views.hw_lines(hw, STATUS_DRIVE)
    assert "Mesh       Heltec (ttyACM0)" in lines


def test_intro_mentions_mesh():
    assert views.INTRO == ["Pfadfinder logs Wi-Fi, Bluetooth, aircraft", "and mesh nodes for WiGLE, WDGWars, Wardrift."]
    assert all(len(l) <= views.COLS for l in views.INTRO)


def test_help_lines_show_the_arrows_not_the_letters():
    from pathlib import Path
    source = Path(views.__file__).read_text()
    assert "z/c" not in source and "f/x" not in source
