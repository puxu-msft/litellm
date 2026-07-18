from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from litellm.proxy.observability.terminal.collector.protocol import (
    RangeAck,
    RangeNotice,
    decode_notice,
    encode_ack,
    read_frame,
)

RangeCommitter = Callable[[RangeNotice], Awaitable[bool]]


class CollectorIPCServer:
    def __init__(self, path: Path, commit: RangeCommitter) -> None:
        self._path = path
        self._commit = commit
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._path.unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(self._handle, path=self._path)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._path.unlink(missing_ok=True)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            notice = decode_notice(await read_frame(reader))
            if await self._commit(notice):
                writer.write(encode_ack(RangeAck(notice.worker_instance_id, notice.sequence_range)))
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
