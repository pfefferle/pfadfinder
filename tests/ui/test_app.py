from datetime import datetime, timedelta, timezone

from pfadfinder.ui.app import App
from tests.ui.fixtures import STATUS_DRIVE

T0 = datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, s):
        self.now += timedelta(seconds=s)


class FakeClient:
    def __init__(self, screen_mode="pocket", running=True):
        self.calls = []
        self.running = running
        self.cfg = {"wigle_api_name": "", "wigle_api_token": "", "screen_mode": screen_mode, "start_on_boot": False}

    def call(self, cmd, **args):
        self.calls.append((cmd, args))
        if cmd == "status":
            return {"ok": True, "status": {**STATUS_DRIVE, "running": self.running,
                                           "mode": "drive" if self.running else None}}
        if cmd == "config.get":
            return {"ok": True, "config": dict(self.cfg)}
        if cmd == "config.set":
            self.cfg[args["key"]] = args["value"]
            return {"ok": True}
        if cmd == "modes":
            return {"ok": True, "modes": {"drive": {"title": "Drive", "available": True, "reason": ""}}}
        if cmd == "live":
            return {"ok": True, "rows": [{"id": str(i), "name": f"n{i}", "rssi": -50} for i in range(20)]}
        if cmd == "upload":
            return {"ok": False, "error": "Stop Drive first."} if self.running else {"ok": True}
        if cmd == "start":
            return {"ok": False, "error": "wifi: no Wi-Fi interface found"}
        return {"ok": True}


class FakeFb:
    def __init__(self):
        self.shown = 0

    def show(self, img):
        self.shown += 1


class FakeBacklight:
    level = 1.0

    def set(self, level):
        self.level = level


class FakeKeys:
    def __init__(self):
        self.queue = []

    def read(self, timeout):
        q, self.queue = self.queue, []
        return q


def make_app(screen_mode="pocket", running=True):
    clock = Clock()
    app = App(FakeClient(screen_mode, running), FakeFb(), FakeBacklight(), FakeKeys(), clock)
    return app, clock


def press(app, *keys):
    app.keys.queue = list(keys)
    app.tick()


def test_pocket_timeout_turns_off_and_stops_drawing():
    app, clock = make_app()
    app.tick()
    clock.advance(11); app.tick()
    assert app.backlight.level == 0
    shown = app.fb.shown
    for _ in range(5):
        clock.advance(5); app.tick()
    assert app.fb.shown == shown


def test_wake_key_swallowed():
    app, clock = make_app()
    app.tick(); clock.advance(11); app.tick()
    press(app, "2")
    assert app.backlight.level == 1 and app.screen == 1
    press(app, "2")
    assert app.screen == 2


def test_mount_dims_and_redraws_every_5s():
    app, clock = make_app("mount")
    app.tick(); clock.advance(11); app.tick()
    assert app.backlight.level == 0.3
    shown = app.fb.shown
    clock.advance(2); app.tick()
    assert app.fb.shown == shown
    clock.advance(4); app.tick()
    assert app.fb.shown == shown + 1


def test_five_screens_only():
    app, _ = make_app()
    app.tick()
    for key, screen in (("5", 5), ("3", 3), ("6", 3), ("1", 1)):
        press(app, key)
        assert app.screen == screen


def test_status_pages_with_z_and_c_and_fn_arrows():
    app, _ = make_app()
    app.tick()
    press(app, "c"); assert app.page[1] == 1
    press(app, "RIGHT"); assert app.page[1] == 2
    press(app, "c"); assert app.page[1] == 3  # Aircraft
    press(app, "c"); assert app.page[1] == 4  # Mesh
    press(app, "c"); assert app.page[1] == 0  # wraps
    press(app, "z"); assert app.page[1] == 4


def test_list_scrolls_with_f_and_x():
    app, _ = make_app()
    app.tick()
    press(app, "c")
    press(app, "x", "x", "x"); assert app.offset == 3
    press(app, "f"); assert app.offset == 2
    press(app, "z"); assert app.offset == 0  # page change resets scroll


def test_enter_on_status_starts_drive_and_shows_alert():
    app, _ = make_app(running=False)
    app.tick()
    press(app, "ENTER")
    assert ("start", {"mode": "drive"}) in app.client.calls
    assert app.alert == "wifi: no Wi-Fi interface found"
    press(app, "c")  # any key clears the message
    assert app.alert == ""


def test_enter_on_status_stops_running_drive():
    app, _ = make_app(running=True)
    app.tick()
    press(app, "ENTER")
    assert ("stop", {}) in app.client.calls


def test_upload_u_and_message_while_driving():
    app, _ = make_app(running=True)
    app.tick()
    press(app, "3")
    press(app, "u")
    assert ("upload", {"site": "wigle"}) in app.client.calls and app.alert == "Stop Drive first."


def test_upload_a_uploads_all_sites():
    app, _ = make_app(running=False)
    app.tick()
    press(app, "3")
    press(app, "a")
    assert ("upload", {"site": "all"}) in app.client.calls


def test_upload_pages_switch_sites():
    app, _ = make_app(running=False)
    app.tick()
    press(app, "3")
    press(app, "c"); press(app, "u")
    assert ("upload", {"site": "wdgwars"}) in app.client.calls
    press(app, "c"); press(app, "c")
    assert app.page[3] == 0


def test_esc_back_then_quit():
    app, _ = make_app()
    app.tick()
    press(app, "4")
    press(app, "ESC"); assert app.screen == 1 and app.running
    press(app, "ESC"); assert app.running is False
    app.close()
    assert app.backlight.level == 1


def test_settings_edit_token():
    app, _ = make_app()
    app.tick()
    press(app, "5")
    press(app, "x", "ENTER", "s", "e", "c", "BACKSPACE", "c", "ENTER")
    assert app.client.cfg["wigle_api_token"] == "sec"


def test_every_secret_edit_starts_empty():
    from pfadfinder.config import SECRET_FIELDS
    from pfadfinder.ui import views
    for i, (key, _) in enumerate(views.SETTINGS):
        if key not in SECRET_FIELDS:
            continue
        app, _ = make_app()
        app.client.cfg[key] = "set"  # the core never sends a secret, only that it is set
        app.tick()
        press(app, "5", *["x"] * i, "ENTER", "k", "ENTER")
        assert app.client.cfg[key] == "k", key


def test_letters_are_text_while_editing():
    app, _ = make_app()
    app.tick()
    press(app, "5")
    press(app, "ENTER", "f", "x", "z", "c", "1", "ENTER")
    assert app.client.cfg["wigle_api_name"] == "fxzc1"


def test_mode_enter_toggles():
    app, _ = make_app()
    app.tick()
    press(app, "2")
    press(app, "ENTER")
    assert ("stop", {}) in app.client.calls


def test_config_refreshed_from_core():
    app, clock = make_app("pocket")
    app.tick()
    app.client.cfg["screen_mode"] = "mount"
    clock.advance(11); app.tick()
    assert app.backlight.level == 0.3


def test_menu_highlights_current_screen():
    app, _ = make_app()
    app.tick()
    press(app, "4")
    assert app.last_render == {"current": 4}


def test_enter_on_aircraft_page_starts_sky():
    app, _ = make_app(running=False)
    app.tick()
    press(app, "z", "z")  # Mesh, then Aircraft
    press(app, "ENTER")
    assert ("start", {"mode": "sky"}) in app.client.calls


def test_mesh_page_lists_nodes_and_enter_starts_mesh():
    from datetime import datetime, timezone
    app, _ = make_app(running=False)
    app.tick()
    press(app, "z")  # Mesh page
    assert app.page[1] == 4
    body, help_, _, _ = app.screen_lines(datetime(2026, 11, 20, tzinfo=timezone.utc))
    assert body[0].startswith("Mesh") and ("live", {"kind": "MESH_NODE", "n": 50}) in app.client.calls
    press(app, "ENTER")
    assert ("start", {"mode": "mesh"}) in app.client.calls
