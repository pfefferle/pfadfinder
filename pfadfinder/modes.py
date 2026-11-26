"""Modes: data-defined radio combinations, checked against present hardware."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .model import Device
from .sources.base import Source, SysInfo

DEFAULT_MODES = Path(__file__).with_name("modes.toml")


@dataclass
class Mode:
    name: str
    title: str
    sources: list[str]
    optional: list[str]
    warn_external_power: bool
    description: str = ""


@dataclass
class ModePlan:
    available: bool
    reason: str = ""
    devices: dict[str, Device] = field(default_factory=dict)


def load_modes(path: Path = DEFAULT_MODES) -> dict[str, Mode]:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    return {
        name: Mode(name, m.get("title", name.title()), list(m.get("sources", [])),
                   list(m.get("optional", [])), bool(m.get("warn_external_power", False)),
                   str(m.get("description", "")))
        for name, m in data.items()
    }


def plan_mode(mode: Mode, sources: dict[str, type[Source]], sys: SysInfo | None) -> ModePlan:
    taken: dict[str, str] = {}  # exclusive resource -> source name
    plan = ModePlan(True)
    for name in mode.sources + mode.optional:
        required = name in mode.sources
        cls = sources.get(name)
        if cls is None:
            if required:
                return ModePlan(False, f"{name}: unknown source")
            continue
        devices = cls.detect(sys)
        conflict = ""
        chosen = None
        for dev in devices:
            clash = next((c.resource for c in dev.claims if c.exclusive and c.resource in taken), None)
            if clash is None:
                chosen = dev
                break
            conflict = conflict or f"{taken[clash]} and {name} both need {clash}"
        if chosen is None:
            if required:
                return ModePlan(False, conflict or f"{name}: {cls.missing_reason(sys)}")
            continue
        for c in chosen.claims:
            if c.exclusive:
                taken[c.resource] = name
        plan.devices[name] = chosen
    return plan
