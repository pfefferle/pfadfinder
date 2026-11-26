"""Source registry: built-in sources plus the `pfadfinder.sources` entry-point group."""

from __future__ import annotations

import importlib
import logging
from importlib.metadata import entry_points

from .base import Source

log = logging.getLogger("pfadfinder.sources")

BUILTIN_MODULES: tuple[str, ...] = (
    "pfadfinder.sources.gps",
    "pfadfinder.sources.wifi",
    "pfadfinder.sources.ble",
    "pfadfinder.sources.adsb",
    "pfadfinder.sources.mesh_usb",
)
_REGISTRY: dict[str, type[Source]] = {}


def register(cls: type[Source]) -> type[Source]:
    _REGISTRY[cls.name] = cls
    return cls


def all_sources() -> dict[str, type[Source]]:
    for mod in BUILTIN_MODULES:
        importlib.import_module(mod)
    for ep in entry_points(group="pfadfinder.sources"):
        try:
            register(ep.load())
        except Exception:  # a broken plug-in must not take the core down
            log.exception("could not load source plug-in %s", ep.name)
    return dict(_REGISTRY)
