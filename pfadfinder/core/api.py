"""JSON-lines control socket for the UI (one request per line, one reply per line)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("pfadfinder.api")


async def serve(core, path: Path) -> asyncio.AbstractServer:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)

    async def client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                try:
                    req = json.loads(line)
                    if not isinstance(req, dict):
                        raise ValueError
                except ValueError:
                    reply = {"ok": False, "error": "bad request"}
                else:
                    try:
                        reply = await core.handle(req)
                    except Exception as e:  # never let one request kill the server
                        log.exception("request failed")
                        reply = {"ok": False, "error": str(e)}
                writer.write((json.dumps(reply) + "\n").encode())
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_unix_server(client, str(path))
    os.chmod(path, 0o660)
    return server
