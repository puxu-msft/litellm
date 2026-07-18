from __future__ import annotations

import struct
from collections.abc import Mapping
from datetime import datetime
from typing import TypeAlias, assert_never
from uuid import UUID

import orjson
from pydantic import JsonValue, TypeAdapter

from litellm.proxy.observability.terminal.events import (
    SYSTEM_EVENT_TYPES,
    BodyBoundary,
    BodyEventPayload,
    BodyState,
    EventEnvelope,
    EventPayload,
    EventType,
    FrozenJsonObject,
    FrozenJsonValue,
    JsonEntries,
    LifecycleEventPayload,
    LogEventPayload,
    LogSeverity,
    RequestTerminalPayload,
    SystemEventPayload,
    TerminalReason,
)

DEFAULT_MAX_FRAME_BYTES = 8 * 1024 * 1024
_HEADER_SIZE = 4
_SUPPORTED_SCHEMA_VERSION = 1
_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])
_STRING_LIST_ADAPTER = TypeAdapter(list[str])

MutableJsonValue: TypeAlias = JsonValue


class EventCodecError(ValueError):
    """Base class for event framing and schema failures."""


class FrameTooLarge(EventCodecError):
    def __init__(self, size: int, limit: int) -> None:
        super().__init__(f"event frame size {size} exceeds limit {limit}")
        self.size = size
        self.limit = limit


class TruncatedFrame(EventCodecError):
    pass


class UnsupportedSchema(EventCodecError):
    def __init__(self, schema_version: int, raw_event: FrozenJsonObject) -> None:
        super().__init__(f"unsupported event schema_version={schema_version}")
        self.schema_version = schema_version
        self.raw_event = raw_event


class InvalidEvent(EventCodecError):
    pass


class DecoderFailed(EventCodecError):
    pass


def encode_event_frame(
    event: EventEnvelope,
    *,
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
) -> bytes:
    if max_frame_bytes < 1:
        raise ValueError("max_frame_bytes must be positive")
    payload = orjson.dumps(_event_to_json(event))
    if len(payload) > max_frame_bytes:
        raise FrameTooLarge(len(payload), max_frame_bytes)
    return struct.pack("!I", len(payload)) + payload


class EventFrameDecoder:
    def __init__(self, *, max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES) -> None:
        if max_frame_bytes < 1:
            raise ValueError("max_frame_bytes must be positive")
        self._max_frame_bytes = max_frame_bytes
        self._buffer = b""
        self._failed = False

    def feed(self, chunk: bytes) -> tuple[EventEnvelope, ...]:
        if self._failed:
            raise DecoderFailed("decoder cannot be reused after a frame error")
        buffer = self._buffer + chunk
        decoded: tuple[EventEnvelope, ...] = ()
        try:
            while len(buffer) >= _HEADER_SIZE:
                size = struct.unpack("!I", buffer[:_HEADER_SIZE])[0]
                if size > self._max_frame_bytes:
                    raise FrameTooLarge(size, self._max_frame_bytes)
                frame_end = _HEADER_SIZE + size
                if len(buffer) < frame_end:
                    break
                decoded = (*decoded, _decode_event(buffer[_HEADER_SIZE:frame_end]))
                buffer = buffer[frame_end:]
        except EventCodecError:
            self._failed = True
            raise
        self._buffer = buffer
        return decoded

    def finish(self) -> None:
        if self._failed:
            raise DecoderFailed("decoder cannot be reused after a frame error")
        if self._buffer:
            raise TruncatedFrame(f"event stream ended with {len(self._buffer)} buffered bytes")


def _event_to_json(event: EventEnvelope) -> dict[str, MutableJsonValue]:
    known: dict[str, MutableJsonValue] = {
        "schema_version": event.schema_version,
        "event_id": str(event.event_id),
        "event_type": event.event_type.value,
        "worker_instance_id": str(event.worker_instance_id),
        "worker_sequence": event.worker_sequence,
        "occurred_at_utc": event.occurred_at_utc.isoformat().replace("+00:00", "Z"),
        "payload": _payload_to_json(event.payload),
        "blob_digests": list(event.blob_digests),
    }
    optional: tuple[tuple[str, MutableJsonValue | None], ...] = (
        ("request_id", str(event.request_id) if event.request_id is not None else None),
        ("session_hash", event.session_hash),
        ("monotonic_offset_ns", event.monotonic_offset_ns),
        ("severity", event.severity.value if event.severity is not None else None),
    )
    for key, value in optional:
        if value is not None:
            known[key] = value
    return _merge_extensions(known, event.extensions)


def _payload_to_json(payload: EventPayload) -> dict[str, MutableJsonValue]:
    match payload:
        case LifecycleEventPayload(stage=stage, extensions=extensions):
            return _merge_extensions({"stage": stage}, extensions)
        case RequestTerminalPayload(
            reason=reason,
            http_status=http_status,
            detail=detail,
            extensions=extensions,
        ):
            values: dict[str, MutableJsonValue] = {"reason": reason.value}
            if http_status is not None:
                values["http_status"] = http_status
            if detail is not None:
                values["detail"] = detail
            return _merge_extensions(values, extensions)
        case BodyEventPayload(
            boundary=boundary,
            sequence=sequence,
            byte_count=byte_count,
            blob_digest=blob_digest,
            state=state,
            extensions=extensions,
        ):
            values = {
                "boundary": boundary.value,
                "sequence": sequence,
                "byte_count": byte_count,
                "state": state.value,
            }
            if blob_digest is not None:
                values["blob_digest"] = blob_digest
            return _merge_extensions(values, extensions)
        case LogEventPayload(logger_name=logger_name, message=message, extensions=extensions):
            return _merge_extensions({"logger_name": logger_name, "message": message}, extensions)
        case SystemEventPayload(code=code, detail=detail, extensions=extensions):
            return _merge_extensions({"code": code, "detail": detail}, extensions)
        case _:
            assert_never(payload)


def _frozen_json_to_mutable(value: FrozenJsonValue) -> MutableJsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, tuple):
        return [_frozen_json_to_mutable(item) for item in value]
    return {key: _frozen_json_to_mutable(item) for key, item in value.entries}


def _merge_extensions(
    known: dict[str, MutableJsonValue],
    extensions: JsonEntries,
) -> dict[str, MutableJsonValue]:
    extension_keys = frozenset(key for key, _value in extensions)
    collisions = extension_keys.intersection(known)
    if collisions:
        raise ValueError(f"extension fields collide with known fields: {sorted(collisions)}")
    return {**known, **{key: _frozen_json_to_mutable(value) for key, value in extensions}}


def _decode_event(payload: bytes) -> EventEnvelope:
    try:
        raw = _JSON_OBJECT_ADAPTER.validate_python(orjson.loads(payload))
    except Exception as exception:
        raise InvalidEvent(f"invalid event JSON: {exception}") from exception
    frozen_raw = _freeze_object(raw)
    schema_version = _required_int(raw, "schema_version")
    if schema_version != _SUPPORTED_SCHEMA_VERSION:
        raise UnsupportedSchema(schema_version, frozen_raw)
    try:
        event_type = EventType(_required_str(raw, "event_type"))
        known_keys = frozenset(
            {
                "schema_version",
                "event_id",
                "event_type",
                "worker_instance_id",
                "worker_sequence",
                "request_id",
                "session_hash",
                "occurred_at_utc",
                "monotonic_offset_ns",
                "severity",
                "payload",
                "blob_digests",
            }
        )
        return EventEnvelope(
            schema_version=schema_version,
            event_id=UUID(_required_str(raw, "event_id")),
            event_type=event_type,
            worker_instance_id=UUID(_required_str(raw, "worker_instance_id")),
            worker_sequence=_required_int(raw, "worker_sequence"),
            request_id=_optional_uuid(raw, "request_id"),
            session_hash=_optional_str(raw, "session_hash"),
            occurred_at_utc=_parse_utc(_required_str(raw, "occurred_at_utc")),
            monotonic_offset_ns=_optional_int(raw, "monotonic_offset_ns"),
            severity=_optional_severity(raw),
            payload=_decode_payload(event_type, _required_object(raw, "payload")),
            blob_digests=_string_tuple(raw, "blob_digests"),
            extensions=_unknown_entries(raw, known_keys),
        )
    except (TypeError, ValueError, KeyError) as exception:
        raise InvalidEvent(f"invalid event fields: {exception}") from exception


def _decode_payload(event_type: EventType, raw: dict[str, JsonValue]) -> EventPayload:
    if event_type in {
        EventType.REQUEST_COMPLETED,
        EventType.REQUEST_FAILED,
        EventType.REQUEST_CANCELLED,
        EventType.REQUEST_TIMED_OUT,
        EventType.REQUEST_SHUTDOWN_DROPPED,
    }:
        keys = frozenset({"reason", "http_status", "detail"})
        return RequestTerminalPayload(
            reason=TerminalReason(_required_str(raw, "reason")),
            http_status=_optional_int(raw, "http_status"),
            detail=_optional_str(raw, "detail"),
            extensions=_unknown_entries(raw, keys),
        )
    if event_type in {
        EventType.HTTP_BODY_STARTED,
        EventType.HTTP_BODY_CHUNK,
        EventType.HTTP_BODY_COMPLETED,
        EventType.HTTP_BODY_INCOMPLETE,
    }:
        keys = frozenset({"boundary", "sequence", "byte_count", "blob_digest", "state"})
        return BodyEventPayload(
            boundary=BodyBoundary(_required_str(raw, "boundary")),
            sequence=_required_int(raw, "sequence"),
            byte_count=_required_int(raw, "byte_count"),
            blob_digest=_optional_str(raw, "blob_digest"),
            state=BodyState(_required_str(raw, "state")),
            extensions=_unknown_entries(raw, keys),
        )
    if event_type is EventType.LOG_RECORD:
        keys = frozenset({"logger_name", "message"})
        return LogEventPayload(
            logger_name=_required_str(raw, "logger_name"),
            message=_required_str(raw, "message"),
            extensions=_unknown_entries(raw, keys),
        )
    if event_type in SYSTEM_EVENT_TYPES:
        keys = frozenset({"code", "detail"})
        return SystemEventPayload(
            code=_required_str(raw, "code"),
            detail=_required_str(raw, "detail"),
            extensions=_unknown_entries(raw, keys),
        )
    return LifecycleEventPayload(
        stage=_required_str(raw, "stage"),
        extensions=_unknown_entries(raw, frozenset({"stage"})),
    )


def _freeze_object(raw: Mapping[str, JsonValue]) -> FrozenJsonObject:
    return FrozenJsonObject(tuple((key, _freeze_json(value)) for key, value in raw.items()))


def _freeze_json(value: JsonValue) -> FrozenJsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return _freeze_object(value)


def _unknown_entries(raw: Mapping[str, JsonValue], known: frozenset[str]) -> JsonEntries:
    return tuple((key, _freeze_json(value)) for key, value in raw.items() if key not in known)


def _required_str(raw: Mapping[str, JsonValue], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value


def _optional_str(raw: Mapping[str, JsonValue], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string or null")
    return value


def _required_int(raw: Mapping[str, JsonValue], key: str) -> int:
    value = raw[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{key} must be an integer")
    return value


def _optional_int(raw: Mapping[str, JsonValue], key: str) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{key} must be an integer or null")
    return value


def _required_object(raw: Mapping[str, JsonValue], key: str) -> dict[str, JsonValue]:
    return _JSON_OBJECT_ADAPTER.validate_python(raw[key])


def _optional_uuid(raw: Mapping[str, JsonValue], key: str) -> UUID | None:
    value = _optional_str(raw, key)
    return UUID(value) if value is not None else None


def _optional_severity(raw: Mapping[str, JsonValue]) -> LogSeverity | None:
    value = _optional_str(raw, "severity")
    return LogSeverity(value) if value is not None else None


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed


def _string_tuple(raw: Mapping[str, JsonValue], key: str) -> tuple[str, ...]:
    return tuple(_STRING_LIST_ADAPTER.validate_python(raw.get(key, [])))
