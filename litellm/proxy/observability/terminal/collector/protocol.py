from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass
from uuid import UUID

import orjson
from pydantic import BaseModel, ConfigDict, TypeAdapter

from litellm.proxy.observability.terminal.archive.spool import SequenceRange


@dataclass(frozen=True, slots=True)
class RangeNotice:
    worker_instance_id: UUID
    sequence_range: SequenceRange


@dataclass(frozen=True, slots=True)
class RangeAck:
    worker_instance_id: UUID
    sequence_range: SequenceRange


class _WireRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    worker_instance_id: UUID
    start: int
    end: int


_WIRE = TypeAdapter(_WireRange)


def encode_notice(notice: RangeNotice) -> bytes:
    return _frame("notice", notice.worker_instance_id, notice.sequence_range)


def encode_ack(ack: RangeAck) -> bytes:
    return _frame("ack", ack.worker_instance_id, ack.sequence_range)


def decode_notice(payload: bytes) -> RangeNotice:
    wire = _WIRE.validate_json(payload)
    if wire.kind != "notice":
        raise ValueError("expected range notice")
    return RangeNotice(wire.worker_instance_id, SequenceRange(wire.start, wire.end))


def decode_ack(payload: bytes) -> RangeAck:
    wire = _WIRE.validate_json(payload)
    if wire.kind != "ack":
        raise ValueError("expected range ack")
    return RangeAck(wire.worker_instance_id, SequenceRange(wire.start, wire.end))


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    header = await reader.readexactly(4)
    size = int.from_bytes(header, "big")
    if size > 1024 * 1024:
        raise ValueError("IPC frame too large")
    return await reader.readexactly(size)


def _frame(kind: str, worker: UUID, sequence_range: SequenceRange) -> bytes:
    payload = orjson.dumps(
        {"kind": kind, "worker_instance_id": str(worker), "start": sequence_range.start, "end": sequence_range.end}
    )
    return struct.pack("!I", len(payload)) + payload
