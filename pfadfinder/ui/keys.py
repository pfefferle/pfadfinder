"""Keyboard input from evdev (the CardputerZero's TCA8418 matrix)."""

from __future__ import annotations

import glob
import os
import select
import struct

EV_FORMAT = "llHHi"
EV_SIZE = struct.calcsize(EV_FORMAT)
EV_KEY = 1
SHIFT_CODES = {42, 54}

_PLAIN = {
    **{c: ch for c, ch in zip(range(2, 12), "1234567890")},
    **{c: ch for c, ch in zip(range(16, 26), "qwertyuiop")},
    **{c: ch for c, ch in zip(range(30, 39), "asdfghjkl")},
    **{c: ch for c, ch in zip(range(44, 51), "zxcvbnm")},
    12: "-", 13: "=", 26: "[", 27: "]", 39: ";", 40: "'", 41: "`", 43: "\\", 51: ",", 52: ".", 53: "/", 57: " ",
}
_SHIFTED = {
    **{c: ch for c, ch in zip(range(2, 12), "!@#$%^&*()")},
    12: "_", 13: "+", 26: "{", 27: "}", 39: ":", 40: '"', 41: "~", 43: "|", 51: "<", 52: ">", 53: "?", 57: " ",
}
_NAMED = {1: "ESC", 14: "BACKSPACE", 15: "TAB", 28: "ENTER", 103: "UP", 108: "DOWN", 105: "LEFT", 106: "RIGHT"}


def decode(code: int, shift: bool) -> str | None:
    if code in _NAMED:
        return _NAMED[code]
    if shift:
        if code in _SHIFTED:
            return _SHIFTED[code]
        ch = _PLAIN.get(code)
        return ch.upper() if ch else None
    return _PLAIN.get(code)


class KeyReader:
    def __init__(self, fds: list[int] | None = None) -> None:
        if fds is None:
            fds = []
            for path in sorted(glob.glob("/dev/input/event*")):
                try:
                    fds.append(os.open(path, os.O_RDONLY | os.O_NONBLOCK))
                except OSError:
                    continue
        self.fds = fds
        self.shift = False
        self._pending: dict[int, bytes] = {}

    def read(self, timeout: float) -> list[str]:
        if not self.fds:
            return []
        ready, _, _ = select.select(self.fds, [], [], timeout)
        out: list[str] = []
        for fd in ready:
            try:
                data = self._pending.pop(fd, b"") + os.read(fd, EV_SIZE * 64)
            except (BlockingIOError, OSError):
                continue
            usable = len(data) - len(data) % EV_SIZE
            if usable < len(data):
                self._pending[fd] = data[usable:]
            for i in range(0, usable, EV_SIZE):
                _, _, type_, code, value = struct.unpack_from(EV_FORMAT, data, i)
                if type_ != EV_KEY:
                    continue
                if code in SHIFT_CODES:
                    self.shift = value != 0
                    continue
                if value in (1, 2):  # press and auto-repeat
                    key = decode(code, self.shift)
                    if key is not None:
                        out.append(key)
        return out

    def close(self) -> None:
        for fd in self.fds:
            os.close(fd)
