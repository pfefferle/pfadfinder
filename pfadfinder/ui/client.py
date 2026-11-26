"""Blocking JSON-lines client for the core's control socket."""

from __future__ import annotations

import json
import socket
from pathlib import Path


class CoreClient:
    def __init__(self, path: Path, timeout: float = 2.0) -> None:
        self.path = Path(path)
        self.timeout = timeout

    def call(self, cmd: str, **args) -> dict:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(self.timeout)
                s.connect(str(self.path))
                s.sendall((json.dumps({"cmd": cmd, **args}) + "\n").encode())
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
            return json.loads(buf)
        except (OSError, ValueError):
            return {"ok": False, "error": "core not running"}
