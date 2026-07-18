from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from litellm.proxy.observability.terminal.archive.spool import (
    AckCommitted,
    CompactCommitted,
    LoadCommitted,
    WorkerSpool,
)
from litellm.proxy.observability.terminal.collector.protocol import (
    RangeNotice,
    decode_ack,
    encode_notice,
    read_frame,
)


@dataclass(frozen=True, slots=True)
class ReplayCompleted:
    acknowledged_count: int
    compacted_count: int


@dataclass(frozen=True, slots=True)
class ReplayDeferred:
    detail: str


async def replay_pending(spool: WorkerSpool, socket_path: Path, *, limit: int = 256):
    loaded = spool.load_pending(limit=limit)
    if not isinstance(loaded, LoadCommitted):
        return ReplayDeferred(loaded.detail)
    if loaded.sequence_range is None:
        return ReplayCompleted(0, 0)
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path)
        notice = RangeNotice(loaded.events[0].worker_instance_id, loaded.sequence_range)
        writer.write(encode_notice(notice))
        await writer.drain()
        ack = decode_ack(await read_frame(reader))
        writer.close()
        await writer.wait_closed()
    except (OSError, asyncio.IncompleteReadError, ValueError) as exception:
        return ReplayDeferred(str(exception))
    if ack.worker_instance_id != notice.worker_instance_id or ack.sequence_range != notice.sequence_range:
        return ReplayDeferred("collector ack does not match notice")
    acknowledged = spool.acknowledge(ack.sequence_range)
    if not isinstance(acknowledged, AckCommitted):
        return ReplayDeferred(acknowledged.detail)
    compacted = spool.compact_acknowledged_prefix()
    if not isinstance(compacted, CompactCommitted):
        return ReplayDeferred(compacted.detail)
    return ReplayCompleted(acknowledged.updated_count, compacted.deleted_count)
