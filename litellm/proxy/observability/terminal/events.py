from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TypeAlias, assert_never
from uuid import UUID


class EventType(StrEnum):
    REQUEST_ACCEPTED = "request.accepted"
    REQUEST_ROUTED = "request.routed"
    UPSTREAM_STARTED = "upstream.started"
    UPSTREAM_FIRST_BYTE = "upstream.first_byte"
    DOWNSTREAM_FIRST_BYTE = "downstream.first_byte"
    REQUEST_STREAMING = "request.streaming"
    REQUEST_RETRYING = "request.retrying"
    REQUEST_COMPLETED = "request.completed"
    REQUEST_FAILED = "request.failed"
    REQUEST_CANCELLED = "request.cancelled"
    REQUEST_TIMED_OUT = "request.timed_out"
    REQUEST_SHUTDOWN_DROPPED = "request.shutdown_dropped"
    REQUEST_STALE_DETECTED = "request.stale_detected"
    HTTP_BODY_STARTED = "http.body_started"
    HTTP_BODY_CHUNK = "http.body_chunk"
    HTTP_BODY_COMPLETED = "http.body_completed"
    HTTP_BODY_INCOMPLETE = "http.body_incomplete"
    LOG_RECORD = "log.record"
    RENDERER_FAILED = "renderer.failed"
    RENDERER_RECOVERED = "renderer.recovered"
    RENDERER_DEGRADED = "renderer.degraded"
    IPC_DISCONNECTED = "ipc.disconnected"
    IPC_RECONNECTED = "ipc.reconnected"
    SPOOL_REPLAYED = "spool.replayed"
    SEGMENT_ROTATED = "segment.rotated"
    SEGMENT_ARCHIVE_PENDING = "segment.archive_pending"


ALL_EVENT_TYPES = frozenset(EventType)


class TerminalReason(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    SHUTDOWN_DROPPED = "shutdown_dropped"


class TerminalMarker(StrEnum):
    OK = "[ OK ]"
    FAIL = "[FAIL]"
    CANCELLED = "[CANC]"
    TIMEOUT = "[TIME]"


class LogSeverity(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class BodyBoundary(StrEnum):
    CLIENT_REQUEST = "client.request"
    UPSTREAM_REQUEST = "upstream.request"
    UPSTREAM_RESPONSE = "upstream.response"
    CLIENT_RESPONSE = "client.response"


class BodyState(StrEnum):
    STARTED = "started"
    CHUNK = "chunk"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


JsonScalar: TypeAlias = None | bool | int | float | str


@dataclass(frozen=True, slots=True)
class FrozenJsonObject:
    entries: tuple[tuple[str, "FrozenJsonValue"], ...] = ()

    def __post_init__(self) -> None:
        keys = tuple(key for key, _value in self.entries)
        if len(frozenset(keys)) != len(keys):
            raise ValueError("FrozenJsonObject keys must be unique")

    def __getitem__(self, key: str) -> "FrozenJsonValue":
        for entry_key, value in self.entries:
            if entry_key == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _value in self.entries)

    def __len__(self) -> int:
        return len(self.entries)


FrozenJsonValue: TypeAlias = JsonScalar | tuple["FrozenJsonValue", ...] | FrozenJsonObject
JsonEntries: TypeAlias = tuple[tuple[str, FrozenJsonValue], ...]


@dataclass(frozen=True, slots=True)
class LifecycleEventPayload:
    stage: str
    extensions: JsonEntries = ()


@dataclass(frozen=True, slots=True)
class RequestTerminalPayload:
    reason: TerminalReason
    http_status: int | None = None
    detail: str | None = None
    extensions: JsonEntries = ()


@dataclass(frozen=True, slots=True)
class BodyEventPayload:
    boundary: BodyBoundary
    sequence: int
    byte_count: int
    blob_digest: str | None
    state: BodyState
    extensions: JsonEntries = ()


@dataclass(frozen=True, slots=True)
class LogEventPayload:
    logger_name: str
    message: str
    extensions: JsonEntries = ()


@dataclass(frozen=True, slots=True)
class SystemEventPayload:
    code: str
    detail: str
    extensions: JsonEntries = ()


EventPayload: TypeAlias = (
    LifecycleEventPayload | RequestTerminalPayload | BodyEventPayload | LogEventPayload | SystemEventPayload
)


_TERMINAL_EVENT_TYPES = frozenset(
    {
        EventType.REQUEST_COMPLETED,
        EventType.REQUEST_FAILED,
        EventType.REQUEST_CANCELLED,
        EventType.REQUEST_TIMED_OUT,
        EventType.REQUEST_SHUTDOWN_DROPPED,
    }
)
_BODY_EVENT_TYPES = frozenset(
    {
        EventType.HTTP_BODY_STARTED,
        EventType.HTTP_BODY_CHUNK,
        EventType.HTTP_BODY_COMPLETED,
        EventType.HTTP_BODY_INCOMPLETE,
    }
)
SYSTEM_EVENT_TYPES = frozenset(
    {
        EventType.RENDERER_FAILED,
        EventType.RENDERER_RECOVERED,
        EventType.RENDERER_DEGRADED,
        EventType.IPC_DISCONNECTED,
        EventType.IPC_RECONNECTED,
        EventType.SPOOL_REPLAYED,
        EventType.SEGMENT_ROTATED,
        EventType.SEGMENT_ARCHIVE_PENDING,
    }
)
_BODY_STATES = {
    EventType.HTTP_BODY_STARTED: BodyState.STARTED,
    EventType.HTTP_BODY_CHUNK: BodyState.CHUNK,
    EventType.HTTP_BODY_COMPLETED: BodyState.COMPLETE,
    EventType.HTTP_BODY_INCOMPLETE: BodyState.INCOMPLETE,
}


def event_type_for_terminal_reason(reason: TerminalReason) -> EventType:
    match reason:
        case TerminalReason.COMPLETED:
            return EventType.REQUEST_COMPLETED
        case TerminalReason.FAILED:
            return EventType.REQUEST_FAILED
        case TerminalReason.CANCELLED:
            return EventType.REQUEST_CANCELLED
        case TerminalReason.TIMED_OUT:
            return EventType.REQUEST_TIMED_OUT
        case TerminalReason.SHUTDOWN_DROPPED:
            return EventType.REQUEST_SHUTDOWN_DROPPED
        case _:
            assert_never(reason)


def marker_for_terminal_reason(reason: TerminalReason) -> TerminalMarker:
    match reason:
        case TerminalReason.COMPLETED:
            return TerminalMarker.OK
        case TerminalReason.FAILED:
            return TerminalMarker.FAIL
        case TerminalReason.CANCELLED | TerminalReason.SHUTDOWN_DROPPED:
            return TerminalMarker.CANCELLED
        case TerminalReason.TIMED_OUT:
            return TerminalMarker.TIMEOUT
        case _:
            assert_never(reason)


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    schema_version: int
    event_id: UUID
    event_type: EventType
    worker_instance_id: UUID
    worker_sequence: int
    occurred_at_utc: datetime
    payload: EventPayload
    request_id: UUID | None = None
    session_hash: str | None = None
    monotonic_offset_ns: int | None = None
    severity: LogSeverity | None = None
    blob_digests: tuple[str, ...] = ()
    extensions: JsonEntries = ()

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("schema_version must be 1")
        if self.worker_sequence < 0:
            raise ValueError("worker_sequence must be non-negative")
        if self.monotonic_offset_ns is not None and self.monotonic_offset_ns < 0:
            raise ValueError("monotonic_offset_ns must be non-negative")
        if self.occurred_at_utc.utcoffset() != timedelta(0):
            raise ValueError("occurred_at_utc must use UTC")
        _validate_payload(self.event_type, self.payload)


def _validate_payload(event_type: EventType, payload: EventPayload) -> None:
    if event_type in _TERMINAL_EVENT_TYPES:
        _validate_terminal_payload(event_type, payload)
        return
    if event_type in _BODY_EVENT_TYPES:
        _validate_body_payload(event_type, payload)
        return
    if event_type is EventType.LOG_RECORD:
        if not isinstance(payload, LogEventPayload):
            raise TypeError("log.record requires LogEventPayload")
        return
    if event_type in SYSTEM_EVENT_TYPES:
        if not isinstance(payload, SystemEventPayload):
            raise TypeError(f"{event_type.value} requires SystemEventPayload")
        return
    if not isinstance(payload, LifecycleEventPayload):
        raise TypeError(f"{event_type.value} requires LifecycleEventPayload")


def _validate_terminal_payload(event_type: EventType, payload: EventPayload) -> None:
    if not isinstance(payload, RequestTerminalPayload):
        raise TypeError(f"{event_type.value} requires RequestTerminalPayload")
    if event_type_for_terminal_reason(payload.reason) is not event_type:
        raise ValueError(f"terminal reason {payload.reason.value} does not match {event_type.value}")


def _validate_body_payload(event_type: EventType, payload: EventPayload) -> None:
    if not isinstance(payload, BodyEventPayload):
        raise TypeError(f"{event_type.value} requires BodyEventPayload")
    if _BODY_STATES[event_type] is not payload.state:
        raise ValueError(f"body state {payload.state.value} does not match {event_type.value}")
