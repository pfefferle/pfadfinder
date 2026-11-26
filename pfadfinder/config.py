"""User configuration (TOML) and well-known paths."""

from __future__ import annotations

import os
import re
import tempfile
import threading
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Mapping

# (section, key) for every Config field
_LAYOUT: dict[str, tuple[str, str]] = {
    "wigle_api_name": ("wigle", "api_name"),
    "wigle_api_token": ("wigle", "api_token"),
    "wdgwars_api_key": ("wdgwars", "api_key"),
    "wardrift_api_key": ("wardrift", "api_key"),
    "wardrift_username": ("wardrift", "username"),
    "wardrift_password": ("wardrift", "password"),
    "screen_mode": ("ui", "screen_mode"),
    "start_on_boot": ("drive", "start_on_boot"),
    "throttle_seconds": ("drive", "throttle_seconds"),
    "throttle_meters": ("drive", "throttle_meters"),
}
# never sent to the UI, which only learns whether they are set
SECRET_FIELDS = frozenset({"wigle_api_token", "wdgwars_api_key", "wardrift_api_key", "wardrift_password"})


@dataclass
class Config:
    wigle_api_name: str = ""
    wigle_api_token: str = ""
    wdgwars_api_key: str = ""
    wardrift_api_key: str = ""
    wardrift_username: str = ""
    wardrift_password: str = ""
    screen_mode: str = "pocket"  # "pocket" | "mount"
    start_on_boot: bool = False
    throttle_seconds: float = 10.0
    throttle_meters: float = 25.0


@dataclass(frozen=True)
class Paths:
    data_dir: Path
    sessions_dir: Path
    db_path: Path
    state_path: Path
    config_path: Path
    socket_path: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Paths":
        home = Path(env.get("HOME", "/home/pi"))
        data = home / ".local/share/pfadfinder"
        return cls(
            data_dir=data,
            sessions_dir=data / "sessions",
            db_path=data / "pfadfinder.db",
            state_path=home / ".local/state/pfadfinder/state.json",
            config_path=home / ".config/pfadfinder/config.toml",
            socket_path=Path(env.get("PFADFINDER_SOCKET", "/run/pfadfinder/core.sock")),
        )


TEMPLATE = """# Pfadfinder configuration
#
# Edit this file over SSH or on the device. Changes are picked up within
# 30 seconds, no restart needed. Settings changed on the device replace only
# their own line here, so your comments and order stay.

[wigle]
# API name and API token from https://wigle.net/account
api_name = ""
api_token = ""

[wdgwars]
# API key from https://wdgwars.pl/profile (API Keys)
api_key = ""

[wardrift]
# API key from the Wardrift dashboard (API keys), used for uploads
api_key = ""
# username and password only for your numbers on the Upload screen;
# the password is removed after the first sign-in, only a session is kept
username = ""
password = ""

[ui]
# "pocket": screen off 10 s after the last key
# "mount":  screen dimmed after 10 s, updates every 5 s
screen_mode = "pocket"

[drive]
# start Drive mode when the device boots
start_on_boot = false
# write a network again after this many seconds, if you moved this many
# meters or the signal got 3 dB stronger
throttle_seconds = 10.0
throttle_meters = 25.0
"""


class ConfigError(ValueError):
    """The config file is not valid TOML."""


def load_config(path: Path) -> Config:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Config()
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(str(e)) from e
    cfg = Config()
    for f in fields(Config):
        section, key = _LAYOUT[f.name]
        value = data.get(section, {}).get(key)
        if value is None:
            continue
        default = getattr(cfg, f.name)
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif isinstance(default, float):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            value = float(value) if ok else value
        else:
            ok = isinstance(value, str)
        if ok:
            setattr(cfg, f.name, value)
    return cfg


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(float(value))
    out = []
    for ch in str(value):
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


_SECTION = re.compile(r"^\s*\[([^\[\]]+)\]\s*(#.*)?$")


def _template_sections() -> dict[str, str]:
    """Each section of the template with its comments: {"wigle": "[wigle]\n# ...\napi_name = ...\n"}."""
    blocks: dict[str, str] = {}
    current = None
    for line in TEMPLATE.splitlines(keepends=True):
        m = _SECTION.match(line)
        if m:
            current = m.group(1).strip()
            blocks[current] = ""
        if current is not None:
            blocks[current] += line
    return {name: block.rstrip("\n") + "\n" for name, block in blocks.items()}


def ensure_config(path: Path) -> None:
    """Write the commented template if there is no config file yet; otherwise append
    the template's sections the file does not have, leaving every existing line alone."""
    if not path.exists():
        _write_private(path, TEMPLATE)
        return
    text = path.read_text(encoding="utf-8")
    have = {m.group(1).strip() for m in map(_SECTION.match, text.splitlines()) if m}
    missing = [block for name, block in _template_sections().items() if name not in have]
    if not missing:
        return
    if not text.endswith("\n"):
        text += "\n"
    _write_private(path, text + "".join("\n" + block for block in missing))



_WRITE_LOCK = threading.Lock()  # the core and the Wardrift sign-in thread both write the file


def update_config_file(path: Path, field: str, value: object) -> None:
    with _WRITE_LOCK:
        _update_config_file(path, field, value)


def _update_config_file(path: Path, field: str, value: object) -> None:
    """Set one setting by replacing only its `key = value` line.

    Comments, order and every other line stay as they are. A missing key is
    added at the end of its section, a missing section at the end of the file.
    """
    section, key = _LAYOUT[field]
    if not path.exists():
        _write_private(path, TEMPLATE)  # only the template; missing sections are added at service start
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    new_line = f"{key} = {_toml_value(value)}"
    key_re = re.compile(rf"^(\s*){re.escape(key)}\s*=")
    current, last_in_section, done = None, None, False
    for i, line in enumerate(lines):
        m = _SECTION.match(line)
        if m:
            current = m.group(1).strip()
            if current == section:
                last_in_section = i
            continue
        if current == section:
            km = key_re.match(line)
            if km:
                ending = "\n" if line.endswith("\n") else ""
                lines[i] = km.group(1) + new_line + ending
                done = True
                break
            if line.strip() and not line.lstrip().startswith("#"):
                last_in_section = i
    if not done:
        if last_in_section is not None:
            if not lines[last_in_section].endswith("\n"):
                lines[last_in_section] += "\n"
            lines.insert(last_in_section + 1, new_line + "\n")
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(f"\n[{section}]\n{new_line}\n")
    _write_private(path, "".join(lines))


class ConfigWatcher:
    """Tells whether the file changed since the last check (mtime and size)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._seen = self._stamp()

    def _stamp(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def changed(self) -> bool:
        stamp = self._stamp()
        if stamp == self._seen:
            return False
        self._seen = stamp
        return True

    def mark_seen(self) -> None:
        self._seen = self._stamp()
