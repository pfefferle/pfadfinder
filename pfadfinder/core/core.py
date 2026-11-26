"""The logging core: runs a mode's sources, writes sessions, answers the UI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .. import __version__
from ..cadence import MotionTracker
from ..config import SECRET_FIELDS, Config, ConfigError, ConfigWatcher, Paths, load_config, update_config_file
from ..model import RECEIVER_FIX_KINDS, Device, Fix, Kind, Observation
from ..modes import Mode, load_modes, plan_mode
from ..power import PowerManager
from ..sources.base import Source, SysInfo
from ..sources.gps import FixSelector
from ..sources.registry import all_sources
from ..storage.db import Store
from ..storage.session import Session
from ..storage.throttle import RowThrottle
from ..upload.runner import pending_count, upload_site
from ..upload.sites import SITES, SiteError

log = logging.getLogger("pfadfinder.core")

BASE_POWER_W = 2.0        # board + screen estimate until measured on hardware
STABLE_RUN_S = 60.0       # a run this long resets the restart backoff
STATS_MAX_AGE_S = 600.0   # refresh site stats after 10 minutes
STATS_RETRY_S = 60.0      # earliest retry after any stats attempt, failed or not
PENDING_TTL_S = 15.0      # pending counts scan the session files; not on every redraw
ALERT_S = 120.0           # an error stays on the status screen this long


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _Ctx:
    """SourceContext handed to one source."""

    def __init__(self, core: "Core", name: str) -> None:
        self.core, self.name = core, name

    def emit(self, obs: Observation) -> None:
        self.core._on_observation(obs)

    def fix(self) -> Fix | None:
        return self.core.current_fix()

    def update_fix(self, device: str, fix: Fix) -> None:
        self.core._on_fix(device, fix)

    def scan_interval(self) -> float | None:
        return self.core.scan_interval()

    def report_error(self, source: str, message: str) -> None:
        self.core.report_error(source, message)


def online(route_file: Path) -> bool:
    """A default route is up (Wi-Fi or any other link). Unreadable means unknown: let the request try."""
    try:
        lines = Path(route_file).read_text().splitlines()[1:]
    except OSError:
        return True
    return any(len(c) > 3 and c[1] == "00000000" and int(c[3], 16) & 1 for c in (l.split() for l in lines))


class Core:
    route_file = Path("/proc/net/route")
    def __init__(self, paths: Paths, config: Config, sys: SysInfo | None, power: PowerManager, *,
                 sources: dict[str, type[Source]] | None = None, modes: dict[str, Mode] | None = None,
                 clock: Callable[[], datetime] = _utcnow, version: str = __version__,
                 flush_interval: float = 30.0, restart_delays: tuple[float, ...] = (5, 10, 30, 60)) -> None:
        self.paths, self.config, self.sys, self.power = paths, config, sys, power
        self.sources = sources if sources is not None else all_sources()
        self.modes = modes if modes is not None else load_modes()
        self.clock, self.version = clock, version
        self.flush_interval, self.restart_delays = flush_interval, restart_delays
        self.store = Store(paths.db_path)
        self._config_watch = ConfigWatcher(paths.config_path)
        self.mode: str | None = None
        self.session: Session | None = None
        self.started: datetime | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._source_state: dict[str, dict] = {}
        self._errors: list[str] = []
        self._last_error: tuple[str, float] | None = None
        self._stats_attempt: dict[str, float] = {}
        self._stats_error: dict[str, str] = {}
        self._fixes = FixSelector()
        self._motion = MotionTracker()
        self._stats_tasks: dict[str, asyncio.Task] = {}
        self._upload_task: asyncio.Task | None = None
        self._pending: tuple[float, dict[str, int]] | None = None  # (monotonic time, counts)
        self._stats_locks: dict[str, asyncio.Lock] = {}

    # -- fixes and cadence --------------------------------------------------
    def current_fix(self) -> Fix | None:
        return self._fixes.best()

    def _on_fix(self, device: str, fix: Fix) -> None:
        self._fixes.update(device, fix)
        self._motion.update(self.current_fix(), self.clock())

    def scan_interval(self) -> float | None:
        self._motion.update(self.current_fix(), self.clock())
        return self._motion.interval()

    # -- pipeline -------------------------------------------------------------
    def _on_observation(self, obs: Observation) -> None:
        if self.session is None:
            return
        if obs.kind in RECEIVER_FIX_KINDS:  # aircraft carry their own position
            fix = self.current_fix()
            if fix is None:
                return
            obs.fix = fix
        try:
            self.session.write(obs)  # the CSV is the primary record; only buffers in RAM
        except (OSError, ValueError) as e:
            self.report_error("storage", str(e))
        try:
            self.store.record(obs, self.session.id)
        except sqlite3.Error as e:
            self.report_error("database", str(e))

    def report_error(self, source: str, message: str) -> None:
        line = f"{self.clock().strftime('%H:%M:%S')} {source}: {message}"
        if self._errors and self._errors[-1].split(" ", 1)[1] == f"{source}: {message}":
            self._errors[-1] = line  # repeated error: refresh, do not flood
        else:
            log.warning("%s: %s", source, message)
            self._errors = (self._errors + [line])[-5:]
        self._last_error = (f"{source}: {message}", time.monotonic())

    # -- modes ------------------------------------------------------------------
    async def start_mode(self, name: str) -> dict:
        mode = self.modes.get(name)
        if mode is None:
            return {"ok": False, "error": f"unknown mode: {name}"}
        if self.mode is not None:
            await self.stop()
        if not mode.sources:
            self.mode = name
            return {"ok": True}
        plan = plan_mode(mode, self.sources, self.sys)
        if not plan.available:
            return {"ok": False, "error": plan.reason}
        throttle = RowThrottle(self.config.throttle_seconds, self.config.throttle_meters)
        self.session = Session.create(self.paths.sessions_dir, name, self.clock(), self.version, throttle)
        self.mode, self.started = name, self.clock()
        self._write_state(name)
        cores = 2 if "adsb" in plan.devices else 1  # readsb needs about one core on its own
        self.power.enter_wardrive(cores=cores)
        for src_name, device in plan.devices.items():
            self._tasks[src_name] = asyncio.create_task(self._supervise(src_name, device))
        return {"ok": True, "session": self.session.id}

    async def stop(self) -> dict:
        """A user stop: the mode is not resumed on the next start."""
        await self._halt()
        self._write_state(None)
        return {"ok": True}

    async def stop_for_shutdown(self) -> None:
        """Service shutdown (reboot, shutdown, update): close cleanly, no resume on the next
        start. Only a power loss or crash, which never reaches this, resumes the mode.
        A running upload gets up to 10 s to finish before the store is closed."""
        await self._halt()
        self._write_state(None)
        if self._upload_task is not None and not self._upload_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._upload_task), 10)
            except (asyncio.TimeoutError, Exception):
                pass

    async def _halt(self) -> None:
        tasks = list(self._tasks.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._source_state.clear()
        if self.session is not None:
            try:
                self.session.close(self.clock())
            except OSError as e:
                self.report_error("storage", str(e))
            self.session = None
            self._pending = None
            self.power.leave_wardrive()
        self._commit()
        self.mode, self.started = None, None

    async def _supervise(self, name: str, device: Device) -> None:
        cls = self.sources[name]
        attempt = 0
        while True:
            state = self._source_state[name] = {"device": device.label, "internal": device.internal,
                                                "state": "running", "error": ""}
            began = time.monotonic()
            try:
                await cls(device).run(_Ctx(self, name))
                raise RuntimeError("source stopped")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # any source failure is isolated and retried
                state["state"], state["error"] = "error", str(e)
                self.report_error(name, str(e))
            if time.monotonic() - began > STABLE_RUN_S:
                attempt = 0
            while True:
                await asyncio.sleep(self.restart_delays[min(attempt, len(self.restart_delays) - 1)])
                attempt += 1
                try:
                    devices = cls.detect(self.sys)
                except Exception as e:  # e.g. sysfs entry vanishing during an unplug
                    state["error"] = f"detect failed: {e}"
                    continue
                if devices:
                    device = devices[0]
                    break
                state["error"] = cls.missing_reason(self.sys)

    def _write_state(self, mode: str | None) -> None:
        try:
            self.paths.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.paths.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"running_mode": mode}))
            os.replace(tmp, self.paths.state_path)
        except OSError as e:
            self.report_error("state", str(e))

    def _read_state(self) -> str | None:
        try:
            return json.loads(self.paths.state_path.read_text()).get("running_mode")
        except (OSError, ValueError):
            return None

    def _commit(self) -> None:
        try:
            self.store.commit()
        except sqlite3.Error as e:
            self.report_error("storage", str(e))

    def reload_config(self) -> None:
        """Pick up edits made to config.toml (e.g. over SSH). A broken file keeps the last good config."""
        if not self._config_watch.changed():
            return
        try:
            self.config = load_config(self.paths.config_path)
        except ConfigError as e:
            self.report_error("config", f"{self.paths.config_path.name} not loaded: {e}")

    def flush(self) -> None:
        self.reload_config()
        if self.session is not None:
            try:
                self.session.flush()
            except OSError as e:
                self.report_error("storage", str(e))
        self._commit()

    async def run_forever(self) -> None:
        try:
            await self._startup()
        except Exception as e:  # never let startup problems stop the flush loop
            log.exception("startup failed")
            self.report_error("core", f"startup: {e}")
        while True:
            await asyncio.sleep(self.flush_interval)
            self.flush()

    async def _startup(self) -> None:
        for d in Session.unclosed(self.paths.sessions_dir):
            if self.session is None or d != self.session.dir:
                try:
                    Session.close_stale(d, self.clock())
                except OSError as e:
                    self.report_error("storage", f"cannot close {d.name}: {e}")
        resume = self._read_state()
        if self.mode is None:
            if resume and resume in self.modes and self.modes[resume].sources:
                res = await self.start_mode(resume)
                if not res["ok"]:
                    self.report_error("core", f"could not resume {resume}: {res['error']}")
            elif self.config.start_on_boot and "drive" in self.modes:
                await self.start_mode("drive")

    # -- queries ------------------------------------------------------------------
    def status(self) -> dict:
        fix = self.current_fix()
        interval = self.scan_interval()
        sources = {}
        for name, st in self._source_state.items():
            st = dict(st)
            if st["state"] == "running" and self.sources[name].kinds & RECEIVER_FIX_KINDS and interval is None:
                st["state"] = "paused"
            sources[name] = st
        counts = {}
        if self.session is not None:
            counts = {k.value: list(v) for k, v in self.store.counts(self.session.id).items()}
        battery = self.power.battery()
        est = BASE_POWER_W + sum(self.sources[n].power_w for n in self._source_state)
        return {
            "mode": self.mode,
            "session": self.session.id if self.session else None,
            "running": self.session is not None,
            "started": self.started.isoformat() if self.started else None,
            "fix": None if fix is None else {
                "lat": fix.lat, "lon": fix.lon, "mode": fix.mode, "sats": fix.sats,
                "speed_kmh": None if fix.speed_mps is None else round(fix.speed_mps * 3.6, 1)},
            "counts": counts,
            "sources": sources,
            "battery": None if battery is None else asdict(battery),
            "est_power_w": round(est, 2) if self._source_state else None,
            "errors": list(self._errors),
            "alert": self._last_error[0] if self._last_error and
            time.monotonic() - self._last_error[1] < ALERT_S else None,
        }

    def hardware(self) -> dict:
        out = {}
        for name, cls in self.sources.items():
            try:
                devs = cls.detect(self.sys)
            except Exception as e:  # detection must never break the screen
                self.report_error(name, f"detect failed: {e}")
                devs = []
            out[name] = [{"path": d.path, "label": d.label, "internal": d.internal} for d in devs]
        return out

    def modes_info(self) -> dict:
        out = {}
        for name, mode in self.modes.items():
            plan = plan_mode(mode, self.sources, self.sys) if mode.sources else None
            out[name] = {"title": mode.title, "available": plan is None or plan.available,
                         "reason": "" if plan is None else plan.reason,
                         "warn_external_power": mode.warn_external_power, "description": mode.description,
                         "sources": mode.sources, "optional": mode.optional}
        return out

    def live(self, kind: str, n: int) -> list[dict]:
        return self.store.latest(Kind(kind), n)

    def config_view(self) -> dict:
        view = asdict(self.config)
        for k in SECRET_FIELDS:
            view[k] = "set" if view[k] else ""
        return view

    def set_config(self, key: str, value) -> dict:
        types = {f.name: type(getattr(Config(), f.name)) for f in fields(Config)}
        if key not in types:
            return {"ok": False, "error": f"unknown setting: {key}"}
        want = types[key]
        try:
            if want is bool:
                if not isinstance(value, bool):
                    raise TypeError
            elif want is float:
                value = float(value)
            elif not isinstance(value, str):
                raise TypeError
        except (TypeError, ValueError):
            return {"ok": False, "error": f"bad value for {key}"}
        if key == "screen_mode" and value not in ("pocket", "mount"):
            return {"ok": False, "error": "screen_mode must be pocket or mount"}
        try:
            update_config_file(self.paths.config_path, key, value)
            self.config = load_config(self.paths.config_path)  # includes edits made by hand
        except (OSError, ConfigError) as e:
            return {"ok": False, "error": f"cannot save setting: {e}"}
        self._config_watch.mark_seen()
        return {"ok": True}

    # -- sites ------------------------------------------------------------------
    def _site(self, key: str):
        return next((s for s in SITES if s.key == key), None)

    async def upload(self, key: str) -> dict:
        if self.session is not None:
            title = self.modes[self.mode].title if self.mode in self.modes else "the mode"
            return {"ok": False, "error": f"Stop {title} first."}
        if key == "all":
            targets = [s for s in SITES if s.configured(self.config)]
            if not targets:
                return {"ok": False, "error": "no site has keys yet"}
        else:
            site = self._site(key)
            if site is None:
                return {"ok": False, "error": f"unknown site: {key}"}
            if not site.configured(self.config):
                return {"ok": False, "error": f"{site.title} keys missing"}
            targets = [site]
        if self._upload_task is not None and not self._upload_task.done():
            return {"ok": False, "error": "an upload is already running"}
        if not online(self.route_file):
            return {"ok": False, "error": "No network. Connect Wi-Fi first."}
        cfg = self.config

        async def job() -> None:
            await self._run_uploads(targets, cfg)
            self._pending = None

        self._upload_task = asyncio.create_task(job())
        return {"ok": True}

    async def _run_uploads(self, targets, cfg) -> None:
            for site in targets:
                try:
                    results = await asyncio.to_thread(upload_site, self.store, self.paths.sessions_dir, site, cfg,
                                                      self.clock(), self.paths.state_path.parent)
                except Exception as e:  # one site must not stop the others
                    log.exception("upload to %s failed", site.title)
                    self.report_error(site.key, str(e))
                    continue
                for r in results:
                    if not r.ok:
                        self.report_error(site.key, r.message)
                self._stats_attempt[site.key] = time.monotonic()
                await self._refresh_stats(site)  # the numbers changed

    async def _refresh_stats(self, site) -> None:
        lock = self._stats_locks.setdefault(site.key, asyncio.Lock())
        async with lock:  # the upload job and the screen must not sign in twice at once
            await self._refresh_stats_locked(site)

    async def _refresh_stats_locked(self, site) -> None:
        if not online(self.route_file):
            return  # keep the last numbers; asking now would only fail on DNS
        try:
            data = await asyncio.to_thread(site.stats, self.config, self.paths.state_path.parent,
                                           self.paths.config_path)
        except SiteError as e:
            self._stats_error[site.key] = str(e)
            return
        except Exception as e:  # network trouble and the like
            log.info("stats for %s failed: %s", site.title, type(e).__name__)
            self._stats_error[site.key] = f"no answer from {site.title}"
            return
        self._stats_error.pop(site.key, None)
        if data:
            self.store.set_stats(site.key, data)

    def _pending_counts(self) -> dict[str, int]:
        now = time.monotonic()
        if self._pending is None or now - self._pending[0] > PENDING_TTL_S:
            self._pending = (now, {s.key: pending_count(self.store, self.paths.sessions_dir, s.key, self.clock())
                                   for s in SITES})
        return self._pending[1]

    def sites(self) -> dict:
        out = {}
        pending = self._pending_counts()
        uploading = self._upload_task is not None and not self._upload_task.done()
        for site in SITES:
            configured = site.configured(self.config)
            stats = self.store.get_stats(site.key)
            if configured:
                running = self._stats_tasks.get(site.key)
                fetched = stats.get("fetched_at") if stats else None
                age = (self.clock() - datetime.fromisoformat(fetched)).total_seconds() if fetched else None
                attempt = self._stats_attempt.get(site.key)
                recently_tried = attempt is not None and time.monotonic() - attempt < STATS_RETRY_S
                if (running is None or running.done()) and (age is None or age > STATS_MAX_AGE_S) \
                        and not recently_tried:
                    self._stats_attempt[site.key] = time.monotonic()
                    self._stats_tasks[site.key] = asyncio.create_task(self._refresh_stats(site))
            uploads = self.store.upload_status(site.key)
            out[site.key] = {
                "title": site.title,
                "configured": configured,
                "stats": {k: v for k, v in stats.items() if k != "fetched_at"} if stats else None,
                "stats_error": self._stats_error.get(site.key, ""),
                "pending": pending[site.key],
                "pending_unit": "uploads" if getattr(site, "takes_aircraft", False) else "sessions",
                "uploading": uploading,
                "last_upload": {k: uploads[0][k] for k in ("session", "status", "error", "updated_at")}
                if uploads else None,
            }
        return out

    async def handle(self, req: dict) -> dict:
        cmd = req.get("cmd")
        if cmd == "status":
            return {"ok": True, "status": self.status()}
        if cmd == "hardware":
            return {"ok": True, "hardware": self.hardware()}
        if cmd == "modes":
            return {"ok": True, "modes": self.modes_info()}
        if cmd == "start":
            return await self.start_mode(str(req.get("mode", "")))
        if cmd == "stop":
            return await self.stop()
        if cmd == "live":
            try:
                return {"ok": True, "rows": self.live(str(req.get("kind", "WIFI")), int(req.get("n", 20)))}
            except ValueError as e:
                return {"ok": False, "error": str(e)}
        if cmd == "upload":
            return await self.upload(str(req.get("site", "")))
        if cmd == "sites":
            return {"ok": True, "sites": self.sites()}
        if cmd == "config.get":
            return {"ok": True, "config": self.config_view()}
        if cmd == "config.set":
            return self.set_config(str(req.get("key", "")), req.get("value"))
        return {"ok": False, "error": f"unknown command: {cmd}"}
