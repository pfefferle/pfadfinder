"""RGB565 framebuffer output and backlight control."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
from PIL import Image


def to_rgb565(img: Image.Image) -> bytes:
    a = np.asarray(img.convert("RGB"), dtype=np.uint16)
    v = ((a[..., 0] & 0xF8) << 8) | ((a[..., 1] & 0xFC) << 3) | (a[..., 2] >> 3)
    return v.astype("<u2").tobytes()


def _stride_from_sysfs(dev: Path, width: int) -> int:
    try:
        return int(Path(f"/sys/class/graphics/{Path(dev).name}/stride").read_text())
    except (OSError, ValueError):
        return width * 2


class Framebuffer:
    def __init__(self, path: Path | str = "/dev/fb0", size: tuple[int, int] = (320, 170),
                 stride: int | None = None) -> None:
        self.path = Path(path)
        self.size = size
        self.stride = stride or _stride_from_sysfs(self.path, size[0])

    def show(self, img: Image.Image) -> None:
        w, h = self.size
        data = to_rgb565(img.resize(self.size) if img.size != self.size else img)
        row = w * 2
        fd = os.open(self.path, os.O_WRONLY)
        try:
            if self.stride == row:
                os.pwrite(fd, data, 0)
            else:
                for y in range(h):
                    os.pwrite(fd, data[y * row:(y + 1) * row], y * self.stride)
        finally:
            os.close(fd)


class Backlight:
    """`set(1)` restores the brightness the user had when the app started."""

    def __init__(self, sysroot: Path = Path("/")) -> None:
        base = Path(sysroot) / "sys/class/backlight"
        dirs = sorted(base.iterdir()) if base.is_dir() else []
        self.dir = dirs[0] if dirs else None
        self.full = 0
        if self.dir is not None:
            try:
                self.max = int((self.dir / "max_brightness").read_text())
                self.full = int((self.dir / "brightness").read_text()) or self.max
            except (OSError, ValueError):
                self.dir = None

    def set(self, level: float) -> None:
        if self.dir is None:
            return
        value = self.full if level >= 1 else round(self.full * level)
        try:
            (self.dir / "brightness").write_text(f"{value}\n")
        except OSError:
            pass
