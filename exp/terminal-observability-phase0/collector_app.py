from __future__ import annotations

import asyncio
import json
import os
import struct
from typing import Any


async def _send_event(event: str, **fields: Any) -> None:
    socket_path = os.environ["TERMINAL_OBSERVABILITY_COLLECTOR_SOCKET"]
    reader, writer = await asyncio.open_unix_connection(socket_path)
    del reader
    payload = json.dumps({"event": event, "pid": os.getpid(), **fields}, sort_keys=True).encode()
    writer.write(struct.pack("!I", len(payload)) + payload)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await _send_event("worker_started")
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await _send_event("worker_stopped")
                await send({"type": "lifespan.shutdown.complete"})
                return

    if scope["type"] != "http":
        return

    path = scope["path"]
    if path == "/pid":
        await _send_event("request", path=path)
        body = str(os.getpid()).encode()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body})
        return
    if path == "/crash":
        await _send_event("worker_crashing", path=path)
        os._exit(23)

    await send({"type": "http.response.start", "status": 404, "headers": []})
    await send({"type": "http.response.body", "body": b"not found"})