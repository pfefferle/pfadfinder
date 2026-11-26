"""Launcher entry: python3 -m pfadfinder.ui"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from ..config import Paths
from .app import App
from .client import CoreClient
from .fb import Backlight, Framebuffer
from .keys import KeyReader


def main() -> None:
    paths = Paths.from_env(os.environ)
    app = App(CoreClient(paths.socket_path), Framebuffer(), Backlight(), KeyReader(),
              lambda: datetime.now(timezone.utc))
    try:
        while app.running:
            app.tick()
    except KeyboardInterrupt:
        pass
    finally:
        app.close()


if __name__ == "__main__":
    main()
