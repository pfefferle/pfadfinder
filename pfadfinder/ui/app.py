"""The on-device app: five screens, pages, keys, and the screen-off policy."""

from __future__ import annotations

from datetime import datetime

from ..config import SECRET_FIELDS
from . import views

IDLE_S = 10.0
AWAKE_REDRAW_S = 1.0
MOUNT_REDRAW_S = 5.0
DIM_LEVEL = 0.3
CONFIG_REFRESH_S = 10.0  # config.toml can change behind our back (SSH edits)
LIST_HEIGHT = 4
SITES = ["wigle", "wdgwars", "wardrift"]
# CardputerZero arrows are printed on F (up), X (down), Z (left), C (right). With fn the
# keyboard driver sends real arrow codes; outside text entry the plain letters work too.
NAV = {"UP": "UP", "f": "UP", "DOWN": "DOWN", "x": "DOWN", "LEFT": "LEFT", "z": "LEFT", "RIGHT": "RIGHT", "c": "RIGHT"}
PAGES = {1: views.STATUS_PAGES, 3: len(SITES)}


class App:
    def __init__(self, client, fb, backlight, keys, clock) -> None:
        self.client, self.fb, self.backlight, self.keys, self.clock = client, fb, backlight, keys, clock
        self.screen = 1
        self.page = {1: 0, 3: 0}
        self.offset = 0
        self.selected = 0
        self.editing: str | None = None
        self.alert = ""
        self.running = True
        self.asleep = False
        self.dirty = True
        self.last_key = clock()
        self.last_draw: datetime | None = None
        self.last_render: dict = {}
        self.cfg = self._config()
        self._cfg_at = clock()
        self._status: dict | None = None
        self._modes: dict = {}

    def _config(self) -> dict:
        res = self.client.call("config.get")
        return res.get("config", {}) if res.get("ok") else {}

    # -- input ----------------------------------------------------------------
    def tick(self) -> None:
        keys = self.keys.read(0.2 if not self.asleep else 1.0)
        now = self.clock()
        if keys:
            self.last_key = now
            if self.asleep:
                self.asleep = False
                self.backlight.set(1)
            else:
                for k in keys:
                    self.on_key(k)
            self.dirty = True
        if (now - self._cfg_at).total_seconds() >= CONFIG_REFRESH_S:
            self.cfg, self._cfg_at = self._config() or self.cfg, now
        pocket = self.cfg.get("screen_mode", "pocket") != "mount"
        if not self.asleep and (now - self.last_key).total_seconds() >= IDLE_S:
            self.asleep = True
            self.backlight.set(0 if pocket else DIM_LEVEL)
        if self.asleep and pocket:
            return
        every = MOUNT_REDRAW_S if self.asleep else AWAKE_REDRAW_S
        if self.dirty or self.last_draw is None or (now - self.last_draw).total_seconds() >= every:
            self.draw(now)

    def on_key(self, key: str) -> None:
        if self.editing is not None:
            self._edit_key(key)
            return
        self.alert = ""
        if len(key) == 1 and key in "12345":
            self.screen, self.selected, self.offset = int(key), 0, 0
            return
        if key == "ESC":
            if self.screen == 1:
                self.running = False
            self.screen, self.selected, self.offset = 1, 0, 0
            return
        nav = NAV.get(key)
        if nav in ("LEFT", "RIGHT") and self.screen in PAGES:
            step = 1 if nav == "RIGHT" else -1
            self.page[self.screen] = (self.page[self.screen] + step) % PAGES[self.screen]
            self.offset = 0
        elif nav in ("UP", "DOWN"):
            step = 1 if nav == "DOWN" else -1
            if self.screen == 1 and self.page[1] > 0:
                self.offset = max(0, self.offset + step)
            else:
                self.selected = max(0, self.selected + step)
        elif key == "ENTER" and self.screen == 1:
            running = (self._status or {}).get("running")
            mode = {3: "sky", 4: "mesh"}.get(self.page[1], "drive")  # the Aircraft and Mesh pages start their mode
            self._result(self.client.call("stop") if running else self.client.call("start", mode=mode))
        elif key == "ENTER" and self.screen == 2:
            self._toggle_mode()
        elif key == "ENTER" and self.screen == 5:
            self._settings_enter()
        elif key == "u" and self.screen == 3:
            self._result(self.client.call("upload", site=SITES[self.page[3]]))
        elif key == "a" and self.screen == 3:
            self._result(self.client.call("upload", site="all"))

    def _result(self, res: dict) -> None:
        self.alert = "" if res.get("ok") else res.get("error", "")

    def _toggle_mode(self) -> None:
        modes = self._modes or self.client.call("modes").get("modes", {})
        names = list(modes)
        if not names:
            return
        name = names[min(self.selected, len(names) - 1)]
        running = (self._status or {}).get("mode")
        self._result(self.client.call("stop") if name == running else self.client.call("start", mode=name))

    def _settings_enter(self) -> None:
        key, _ = views.SETTINGS[min(self.selected, len(views.SETTINGS) - 1)]
        if key == "screen_mode":
            self._set(key, "mount" if self.cfg.get(key) == "pocket" else "pocket")
        elif key == "start_on_boot":
            self._set(key, not self.cfg.get(key))
        else:
            self.editing = "" if key in SECRET_FIELDS else str(self.cfg.get(key) or "")

    def _edit_key(self, key: str) -> None:
        if key == "ESC":
            self.editing = None
        elif key == "ENTER":
            field, _ = views.SETTINGS[min(self.selected, len(views.SETTINGS) - 1)]
            self._set(field, self.editing)
            self.editing = None
        elif key == "BACKSPACE":
            self.editing = self.editing[:-1]
        elif len(key) == 1:
            self.editing += key

    def _set(self, key: str, value) -> None:
        self._result(self.client.call("config.set", key=key, value=value))
        self.cfg = self._config()

    # -- output -------------------------------------------------------------------
    def screen_lines(self, now: datetime) -> tuple[list[str], str, int | None, bool]:
        """Body, help line, highlighted body line, whether the first line is a header."""
        status = self._status
        if self.screen == 1:
            page = self.page[1]
            if page == 0:
                body, help_ = views.overview(status, now)
                return body, help_, None, True
            if page == 3:
                rows = self.client.call("live", kind="AIRCRAFT", n=50).get("rows", [])
                seen = ((status.get("counts") or {}).get("AIRCRAFT") or [0, 0])[0]
                self.offset = min(self.offset, max(0, len(rows) - LIST_HEIGHT))
                body, help_ = views.aircraft_list(rows, seen, "adsb" in (status.get("sources") or {}),
                                                  self.offset, LIST_HEIGHT)
                return body, help_, None, True
            if page == 4:
                rows = self.client.call("live", kind="MESH_NODE", n=50).get("rows", [])
                seen = ((status.get("counts") or {}).get("MESH_NODE") or [0, 0])[0]
                self.offset = min(self.offset, max(0, len(rows) - LIST_HEIGHT))
                body, help_ = views.mesh_list(rows, seen, bool(status.get("running")),
                                              "mesh-usb" in (status.get("sources") or {}), self.offset, LIST_HEIGHT)
                return body, help_, None, True
            kinds = ["WIFI"] if page == 1 else ["BLE", "BT"]
            rows = []
            for kind in kinds:
                rows += self.client.call("live", kind=kind, n=50).get("rows", [])
            rows.sort(key=lambda r: r.get("last_seen") or "", reverse=True)
            counts = status.get("counts") or {}
            seen = sum((counts.get(k) or [0, 0])[0] for k in kinds)
            self.offset = min(self.offset, max(0, len(rows) - LIST_HEIGHT))
            body, help_ = views.network_list("Wi-Fi" if page == 1 else "Bluetooth", rows, seen, page,
                                             bool(status.get("running")), self.offset, LIST_HEIGHT)
            return body, help_, None, True
        if self.screen == 2:
            self._modes = self.client.call("modes").get("modes", {})
            self.selected = min(self.selected, max(0, len(self._modes) - 1))
            body, help_ = views.mode_lines(self._modes, self.selected, status.get("mode"))
            return body, help_, self.selected, False
        if self.screen == 3:
            site = SITES[self.page[3]]
            info = self.client.call("sites").get("sites", {}).get(site, {})
            body, help_ = views.upload_page(site, info, self.page[3], len(SITES))
            return body, help_, None, True
        if self.screen == 4:
            body, help_ = views.hw_lines(self.client.call("hardware").get("hardware", {}), status)
            return body, help_, None, False
        self.selected = min(self.selected, len(views.SETTINGS) - 1)
        body, help_ = views.settings_lines(self.cfg, self.selected, self.editing)
        return body, help_, self.selected, False

    def draw(self, now: datetime) -> None:
        res = self.client.call("status")
        self._status = res.get("status") if res.get("ok") else None
        if self._status is None:
            img = views.render(views.not_reachable())
            self.last_render = {"current": None}
        else:
            body, help_, highlight, header = self.screen_lines(now)
            alert = self.alert or self._status.get("alert") or ""
            page, pages = self.page.get(self.screen, 0), PAGES.get(self.screen, 1)
            img = views.render(views.compose(body, help_, alert, page, pages), highlight,
                               current=self.screen, header=header)
            self.last_render = {"current": self.screen}
        self.fb.show(img)
        self.last_draw, self.dirty = now, False

    def close(self) -> None:
        self.backlight.set(1)
