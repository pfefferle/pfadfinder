"""Screen content as text lines, and rendering to a 320x170 image.

Every screen is body lines plus a help line; compose() adds the menu line.
"""

from __future__ import annotations

import re
import textwrap
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..config import SECRET_FIELDS

W, H = 320, 170
COLS = 44
SCREENS = [("Status", "1"), ("Mode", "2"), ("Upload", "3"), ("HW", "4"), ("Set", "5")]
MENU_LINE = " ".join(f"{key} {name}" for name, key in SCREENS)
INTRO = ["Pfadfinder logs Wi-Fi, Bluetooth, aircraft", "and mesh nodes for WiGLE, WDGWars, Wardrift."]
BATTERY_WH = 6.5
FONT_PATH = str(Path(__file__).with_name("fonts") / "TerminusTTF-4.49.3.ttf")  # bitmap strikes at 12 and 14 px
# amber terminal: text on near-black, the header and the current tab as filled bars
FG, BG, ACCENT, DIM, ALERT = (255, 176, 0), (12, 8, 0), (255, 176, 0), (130, 88, 0), (255, 70, 50)
SELECT = (80, 52, 0)
ROW = 17  # line height for the 14 px font
SITE_NAMES = {"wigle": "WiGLE", "wdgwars": "WDGWars", "wardrift": "Wardrift"}


def _n(value) -> str:
    return "—" if value is None else f"{value:,}".replace(",", " ")


def dots(page: int, pages: int) -> str:
    return "".join("●" if i == page else "○" for i in range(pages)) if pages > 1 else ""


def _hms(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _gps(fix: dict | None, running: bool) -> str:
    if not fix:
        return "waiting for GPS" if running else "no GPS fix"
    parts = [f"GPS {fix['mode']}D"]
    if fix.get("sats") is not None:
        parts.append(f"{fix['sats']}sat")
    if fix.get("speed_kmh") is not None:
        parts.append(f"{fix['speed_kmh']:.0f}km/h")
    return " ".join(parts)


def _battery(b: dict | None) -> str:
    return "" if not b or b.get("percent") is None else f"{b['percent']}%"


def _count(label: str, counts: list[int]) -> str:
    seen, new = counts
    return f"{label:<5}{_n(seen):>7}  (+{new} new)"


STATUS_PAGES = 5  # Overview, Wi-Fi, Bluetooth, Aircraft, Mesh


# -- 1 Status ----------------------------------------------------------------------
def overview(status: dict, now: datetime) -> tuple[list[str], str]:
    running = bool(status.get("running"))
    mode = (status.get("mode") or "idle").upper()
    rec = " ● REC" if running else ""
    top = f"{mode}{rec}  {_gps(status.get('fix'), running)}  {_battery(status.get('battery'))}".rstrip()[:COLS]
    if not running:
        return [top, *INTRO, "Pick a mode on 2 Mode, or enter for Drive."], "enter start Drive · ◀ ▶ pages"
    counts = status.get("counts") or {}
    sources = status.get("sources") or {}
    body = []
    if "wifi" in sources or "WIFI" in counts:
        body.append(_count("WiFi", counts.get("WIFI") or [0, 0]))
    if "ble" in sources or "BLE" in counts or "BT" in counts:
        body.append(_count("BLE", [sum(counts.get(k, [0, 0])[i] for k in ("BLE", "BT")) for i in (0, 1)]))
    if "adsb" in sources or "AIRCRAFT" in counts:
        body.append(_count("Air", counts.get("AIRCRAFT") or [0, 0]))
    if "mesh-usb" in sources or "MESH_NODE" in counts:
        body.append(_count("Mesh", counts.get("MESH_NODE") or [0, 0]))
    needs_fix = "wifi" in sources or "ble" in sources
    if status.get("fix") or not needs_fix:
        info = ""
        est = status.get("est_power_w")
        if est:
            info = f"~{est:.1f} W"
            b = status.get("battery")
            if b and b.get("percent") is not None:
                hours = BATTERY_WH * b["percent"] / 100 / est
                info += f"   ~{int(hours)}h{int(hours * 60) % 60:02d} left"
        if status.get("started"):
            info += f"   session {_hms((now - datetime.fromisoformat(status['started'])).total_seconds())}"
    else:
        info = "Scanners wait for a GPS fix."
    return [top, *body, info.strip()[:COLS]], f"enter stop {mode.title()} · ◀ ▶ pages"


def signal_bars(rssi: int | None) -> str:
    if rssi is None:
        return "    "
    level = 4 if rssi > -55 else 3 if rssi > -67 else 2 if rssi > -75 else 1
    return "▂▄▆█"[:level].ljust(4)


def network_list(title: str, rows: list[dict], seen: int, page: int, running: bool,
                 offset: int, height: int) -> tuple[list[str], str]:
    head = f"{title}  {_n(seen)} seen"
    if not running:
        return [head, "Start Drive to see networks here."], "◀ ▶ pages · enter start Drive"
    body = []
    for r in rows[offset:offset + height]:
        rssi = "    " if r.get("rssi") is None else f"{r['rssi']:>4}"
        body.append(f"{signal_bars(r.get('rssi'))} {rssi}  {r.get('name') or '(hidden)'}"[:COLS])
    return [head, *body], "▲ ▼ scroll · ◀ ▶ pages · enter stop"


def aircraft_list(rows: list[dict], seen: int, running: bool, offset: int, height: int) -> tuple[list[str], str]:
    head = f"Aircraft  {_n(seen)} seen"
    if not running:
        return [head, "Start Sky to see aircraft here."], "◀ ▶ pages · enter start Sky"
    body = []
    for r in rows[offset:offset + height]:
        alt = r.get("alt_m")
        alt_text = "—" if alt is None else f"{_n(int(round(alt / 0.3048, -2)))} ft"
        body.append(f"{(r.get('name') or r['id']):<10} {alt_text}"[:COLS])
    return [head, *body], "▲ ▼ scroll · ◀ ▶ pages · enter stop"


NETWORK_TAGS = {"meshcore": "MC", "meshtastic": "MT"}


def mesh_list(rows: list[dict], seen: int, running: bool, node: bool, offset: int,
              height: int) -> tuple[list[str], str]:
    head = f"Mesh  {_n(seen)} seen"
    if not running:
        return [head, "Plug a Meshtastic or MeshCore node into", "USB, then press enter for Mesh."], \
            "◀ ▶ pages · enter start Mesh"
    if not node and not rows:
        return [head, "No mesh node was plugged in at the start."], "◀ ▶ pages · enter stop"
    body = []
    for r in rows[offset:offset + height]:
        network, _, node_id = r["id"].partition(":")
        body.append(f"{(r.get('name') or node_id)[:20]:<22}{NETWORK_TAGS.get(network, '')}"[:COLS])
    return [head, *body], "▲ ▼ scroll · ◀ ▶ pages · enter stop"


# -- 2 Mode ------------------------------------------------------------------------
PARTS = {"gps": "GPS", "wifi": "Wi-Fi", "ble": "BT", "adsb": "SDR", "mesh-usb": "Mesh"}  # short, for the mode list


def _parts(m: dict) -> str:
    """'GPS Wi-Fi BT (+Mesh)': what the mode needs, then in brackets what joins when it is there."""
    needed = " ".join(PARTS.get(s, s) for s in m.get("sources", []))
    extra = " ".join(f"+{PARTS.get(s, s)}" for s in m.get("optional", []))
    return f"{needed} ({extra})" if extra else needed


def mode_lines(modes: dict, selected: int, running: str | None) -> tuple[list[str], str]:
    lines = []
    for i, (name, m) in enumerate(modes.items()):
        mark = "▶" if i == selected else " "
        state = "  ● REC" if name == running else ("" if m["available"] else "  n/a")
        lines.append(f"{mark} {m['title']:<6} {_parts(m)}{state}"[:COLS].rstrip())
    names = list(modes)
    if names:
        name = names[min(selected, len(names) - 1)]
        m = modes[name]
        missing = f"! {m['reason']}"[:COLS] if not m["available"] and name != running else ""
        description = m.get("description", "")
        if missing or description:
            lines.append("")
        if missing:
            lines.append(missing)
        if description:
            for part in description.splitlines():  # the text's own line breaks, then the width
                lines += textwrap.wrap(part, COLS)
    return lines, "▲ ▼ select · enter start/stop"


# -- 3 Upload ----------------------------------------------------------------------
def upload_page(site: str, info: dict, page: int, pages: int) -> tuple[list[str], str]:
    name = info.get("title") or SITE_NAMES.get(site, site)
    help_ = "u upload · a all sites · ◀ ▶ sites" if pages > 1 else "u upload"
    if not info.get("configured"):
        if site == "wigle":
            hint = [f"{name} keys missing. Add them in 5 Set", "or in ~/.config/pfadfinder/config.toml"]
        else:
            hint = [f"{name} keys missing: api_key in [{site}]", "in ~/.config/pfadfinder/config.toml"]
        return [name, *hint], help_
    s = info.get("stats")
    lines = [f"{name}  {s.get('user') or ''}".rstrip() if s else name]
    if s:
        lines += [line[:COLS] for line in s.get("lines", [])]
    if info.get("stats_error"):
        lines.append(f"! {info['stats_error']}"[:COLS])
    last = info.get("last_upload")
    if info.get("uploading"):
        state = "uploading…"
    elif last and last["status"] == "done":
        state = f"last upload {last['updated_at'][11:16]}"
    elif last:
        state = f"last upload {last['status']}"
    else:
        state = ""
    unit = info.get("pending_unit", "sessions")
    lines.append(f"pending {info.get('pending', 0)} {unit}   {state}".rstrip()[:COLS])
    if last and last.get("error"):
        lines.append(f"! {last['error']}"[:COLS])
    return lines, help_


# -- 4 HW --------------------------------------------------------------------------
ROLES = [("wifi", "Wi-Fi"), ("ble", "Bluetooth"), ("adsb", "SDR"), ("mesh-usb", "Mesh"), ("gps", "GPS")]


def _short(label: str) -> str:
    """'USB dongle (wlan1, rtl8xxxu)' -> 'USB dongle (wlan1)'; 'gpsd: /dev/serial0' -> '/dev/serial0'."""
    if label.startswith("gpsd: "):
        return label[6:]
    if "(" in label and "," in label:
        return label.split(",", 1)[0] + ")"
    return label


def hw_lines(hw: dict, status: dict | None) -> tuple[list[str], str]:
    sources = (status or {}).get("sources") or {}
    lines = []
    for key, role in ROLES:
        devs = hw.get(key) or []
        running = sources.get(key)
        if running and running.get("state") == "error":
            value = f"error: {running.get('error') or 'unknown'}"
        elif key == "gps" and devs:
            value = _short(devs[0]["label"])  # one gpsd; its detected label lists what it reads
        elif running:
            value = _short(running["device"])
        elif devs:
            value = _short(devs[0]["label"])
        else:
            value = "none found"
        if key == "gps" and devs:
            fix = (status or {}).get("fix")
            state = "no fix" if not fix else f"{fix['mode']}D fix" + (
                f", {fix['sats']} sats" if fix.get("sats") is not None else "")
            lines.append(f"{role:<11}{state}")
            lines.append(f"{'':<11}{value}"[:COLS])
        else:
            lines.append(f"{role:<11}{value}"[:COLS])
    return lines, ""


# -- 5 Set -------------------------------------------------------------------------
SETTINGS = [("wigle_api_name", "WiGLE API name"), ("wigle_api_token", "WiGLE API token"),
            ("wdgwars_api_key", "WDGWars API key"), ("wardrift_api_key", "Wardrift API key"),
            ("wardrift_username", "Wardrift user"), ("wardrift_password", "Wardrift password"),
            ("screen_mode", "Screen"), ("start_on_boot", "Drive on boot")]
TOGGLES = {"screen_mode", "start_on_boot"}


def settings_lines(cfg: dict, selected: int, editing: str | None) -> tuple[list[str], str]:
    lines = []
    for i, (key, label) in enumerate(SETTINGS):
        if i == selected and editing is not None:
            value = ("*" * len(editing) if key in SECRET_FIELDS else editing)[-20:] + "_"
        elif key in SECRET_FIELDS:
            value = "••••" if cfg.get(key) else "not set"
        elif key == "start_on_boot":
            value = "on" if cfg.get(key) else "off"
        else:
            value = str(cfg.get(key) or "not set")
        lines.append(f"{'▶' if i == selected else ' '} {label:<17} {value}"[:COLS])
    if editing is not None:
        return lines, "type · enter save · esc cancel"
    if SETTINGS[min(selected, len(SETTINGS) - 1)][0] in TOGGLES:
        return lines, "▲ ▼ select · enter switch"
    return lines, "▲ ▼ select · enter edit"


# -- not reachable ---------------------------------------------------------------
def not_reachable() -> list[str]:
    return ["Starting the background service…", "It keeps recording while this app is",
            "closed. This screen updates on its own.", "If it stays: sudo systemctl restart",
            "pfadfinder-core", "esc quit"]


# -- composition and rendering --------------------------------------------------
def compose(body: list[str], help_: str, alert: str = "", page: int = 0, pages: int = 1) -> list[str]:
    """Body, then the help line (or the alert in its place) with the page dots
    right-aligned, then the menu line."""
    line = f"! {alert}" if alert else help_
    d = dots(page, pages)
    if d:
        room = COLS - len(d) - 1
        line = f"{line[:room]:<{room}} {d}"
    return [*body, line[:COLS], MENU_LINE]


@lru_cache(maxsize=4)
def _font(size: int):
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except OSError:
        return ImageFont.load_default()


def font_for(n_lines: int):
    """44 columns of the 7x14 font fit 320 px; the 6x12 one when the lines do not fit 17 px rows."""
    return _font(14 if H // max(n_lines, 7) >= ROW else 12)


def _battery_icon(d: ImageDraw.ImageDraw, x: int, y: int, percent: int) -> None:
    d.rectangle((x, y, x + 15, y + 7), outline=BG)
    d.rectangle((x + 16, y + 2, x + 17, y + 5), fill=BG)
    d.rectangle((x + 2, y + 2, x + 2 + round(11 * min(max(percent, 0), 100) / 100), y + 5), fill=BG)


def _header(d: ImageDraw.ImageDraw, font, text: str, row: int, pad: int) -> None:
    """A filled bar: the first part on the left (the REC dot in red), the rest right-aligned,
    a battery icon after a trailing percentage."""
    d.rectangle((0, 0, W, row - 1), fill=ACCENT)
    left, _, right = text.partition("  ")
    x = 4
    for part in re.split("(●)", left):
        d.text((x, pad), part, font=font, fill=ALERT if part == "●" else BG)
        x += font.getlength(part)
    right = right.strip()
    xr = W - 4
    m = re.search(r"(\d+)%$", right)
    if m:
        xr -= 18
        _battery_icon(d, xr, (row - 8) // 2, int(m.group(1)))
        xr -= 4
    d.text((xr - font.getlength(right), pad), right, font=font, fill=BG)


def _tabs(d: ImageDraw.ImageDraw, font, current: int, y: int, row: int, pad: int) -> None:
    d.line((0, y - 1, W, y - 1), fill=DIM)
    x = 2
    for idx, (name, key) in enumerate(SCREENS, start=1):
        part = f"{key} {name}"
        width = font.getlength(part) + 8
        if idx == current:
            d.rectangle((x, y, x + width, H - 1), fill=ACCENT)
        d.text((x + 4, y + pad), part, font=font, fill=BG if idx == current else DIM)
        x += width + 2


def render(lines: list[str], highlight: int | None = None, current: int | None = None,
           header: bool = False) -> Image.Image:
    """`current` (1-based screen) marks the last line as the menu; the line before it is the help line.
    Both sit at the bottom, the other lines start at the top."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    font = font_for(len(lines))
    row = min(ROW, H // max(len(lines), 1))
    pad = max(0, (row - getattr(font, "size", 11)) // 2)
    menu_at = len(lines) - 1 if current is not None else None
    for i, text in enumerate(lines):
        y = H - row * (len(lines) - i) if menu_at is not None and i >= menu_at - 1 else i * row
        if i == highlight:
            d.rectangle((0, y, W, y + row - 1), fill=SELECT)
        if i == menu_at:
            _tabs(d, font, current, y, row, pad)
            continue
        if i == 0 and header:
            _header(d, font, text, row, pad)
            continue
        if text.startswith("! "):
            color = ALERT
        elif menu_at is not None and i == menu_at - 1:
            color = DIM
        else:
            color = FG
        d.text((4, y + pad), text, font=font, fill=color)
    return img
