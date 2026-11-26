import asyncio
import json
import stat

from pfadfinder.core.api import serve
from tests.core.test_core import make_core


async def test_api_status_roundtrip(tmp_path):
    core = make_core(tmp_path)
    path = tmp_path / "core.sock"
    server = await serve(core, path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o660
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(b'{"cmd": "status"}\n')
    writer.write(b'not json\n')
    await writer.drain()
    first = json.loads(await reader.readline())
    second = json.loads(await reader.readline())
    assert first["ok"] and first["status"]["running"] is False
    assert second == {"ok": False, "error": "bad request"}
    writer.close()
    await writer.wait_closed()
    for _ in range(5):  # let the handler see EOF and finish
        await asyncio.sleep(0)
    server.close()
    await server.wait_closed()
