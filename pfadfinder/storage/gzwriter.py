"""Append-only gzip file written as independent members.

Each flush() compresses the buffered text as one complete gzip member and
appends it. Concatenated members are a valid gzip stream, so a power loss can
only lose the unflushed buffer, never earlier data. A failed write is rolled
back to the previous member boundary, and repair() cuts a torn tail left by a
power loss.
"""

from __future__ import annotations

import gzip
import os
import zlib
from pathlib import Path

_write = os.write  # indirection so tests can simulate a partial write


class GzipMemberWriter:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._buf: list[str] = []

    def write(self, text: str) -> None:
        self._buf.append(text)

    def flush(self) -> None:
        if not self._buf:
            return
        data = gzip.compress("".join(self._buf).encode("utf-8"), compresslevel=6)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            size = os.fstat(fd).st_size
            try:
                view = memoryview(data)
                while view:
                    view = view[_write(fd, view):]
                os.fsync(fd)
            except OSError:
                try:
                    os.ftruncate(fd, size)  # keep the file at a member boundary
                except OSError:
                    pass
                raise
        finally:
            os.close(fd)
        self._buf.clear()

    def close(self) -> None:
        self.flush()


def repair(path: Path) -> None:
    """Truncate a gzip file to its last complete member."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return
    good = pos = 0
    while pos < len(data):
        d = zlib.decompressobj(wbits=31)
        try:
            d.decompress(data[pos:])
        except zlib.error:
            break
        if not d.eof:
            break
        pos = len(data) - len(d.unused_data)
        good = pos
    if good < len(data):
        with path.open("r+b") as fh:
            fh.truncate(good)
            fh.flush()
            os.fsync(fh.fileno())
