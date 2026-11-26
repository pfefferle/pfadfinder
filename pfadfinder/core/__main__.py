"""Entry point of the pfadfinder-core service: python3 -m pfadfinder.core"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from ..config import Config, ConfigError, Paths, ensure_config, load_config
from ..power import PowerManager
from ..sources.base import SysInfo
from .api import serve
from .core import Core


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    paths = Paths.from_env(os.environ)
    error = None
    try:
        ensure_config(paths.config_path)
        config = load_config(paths.config_path)
    except (OSError, ConfigError) as e:
        config, error = Config(), e
    core = Core(paths, config, SysInfo(), PowerManager())
    if error is not None:
        core.report_error("config", f"{paths.config_path.name} not loaded: {error}")
    server = await serve(core, paths.socket_path)
    runner = asyncio.create_task(core.run_forever())
    stop = asyncio.Event()
    failed: list[BaseException] = []

    def runner_done(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            logging.getLogger("pfadfinder.core").error("core loop died", exc_info=task.exception())
            failed.append(task.exception())
            stop.set()

    runner.add_done_callback(runner_done)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    runner.cancel()
    await core.stop_for_shutdown()
    server.close()
    core.store.close()
    if failed:
        raise SystemExit(1)  # systemd restarts the service


if __name__ == "__main__":
    asyncio.run(main())
