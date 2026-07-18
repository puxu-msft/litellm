from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from litellm.proxy.observability.terminal.events import (
    ALL_EVENT_TYPES,
    BodyBoundary,
    BodyEventPayload,
    BodyState,
    EventEnvelope,
    EventType,
    FrozenJsonObject,
    LifecycleEventPayload,
    LogEventPayload,
    LogSeverity,
    RequestTerminalPayload,
    SystemEventPayload,
    TerminalMarker,
    TerminalReason,
    event_type_for_terminal_reason,
    marker_for_terminal_reason,
)


EVENT_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKER_ID = UUID("00000000-0000-4000-8000-000000000002")
REQUEST_ID = UUID("00000000-0000-4000-8000-000000000003")
OCCURRED_AT = datetime(2026, 7, 18, 12, 34, 56, tzinfo=timezone.utc)


def _payload(event_type: EventType):
    if event_type in {
        EventType.REQUEST_COMPLETED,
        EventType.REQUEST_FAILED,
        EventType.REQUEST_CANCELLED,
        EventType.REQUEST_TIMED_OUT,
        EventType.REQUEST_SHUTDOWN_DROPPED,
    }:
        reason = {
            EventType.REQUEST_COMPLETED: TerminalReason.COMPLETED,
            EventType.REQUEST_FAILED: TerminalReason.FAILED,
            EventType.REQUEST_CANCELLED: TerminalReason.CANCELLED,
            EventType.REQUEST_TIMED_OUT: TerminalReason.TIMED_OUT,
            EventType.REQUEST_SHUTDOWN_DROPPED: TerminalReason.SHUTDOWN_DROPPED,
        }[event_type]
        return RequestTerminalPayload(
            reason=reason,
            http_status=200,
            detail="terminal detail",
            extensions=(("terminal_future", "kept"),),
        )
    if event_type.value.startswith("http.body_"):
        state = {
            EventType.HTTP_BODY_STARTED: BodyState.STARTED,
            EventType.HTTP_BODY_CHUNK: BodyState.CHUNK,
            EventType.HTTP_BODY_COMPLETED: BodyState.COMPLETE,
            EventType.HTTP_BODY_INCOMPLETE: BodyState.INCOMPLETE,
        }[event_type]
        return BodyEventPayload(
            boundary=BodyBoundary.UPSTREAM_RESPONSE,
            sequence=4,
            byte_count=12,
            blob_digest="b3:abcd",
            state=state,
            extensions=(("body_future", "kept"),),
        )
    if event_type is EventType.LOG_RECORD:
        return LogEventPayload(
            logger_name="LiteLLM",
            message="hello",
            extensions=(("log_future", "kept"),),
        )
    if event_type.value.startswith(("renderer.", "ipc.", "spool.", "segment.")):
        return SystemEventPayload(
            code="phase0",
            detail="observed",
            extensions=(("system_future", "kept"),),
        )
    return LifecycleEventPayload(stage=event_type.value, extensions=(("payload_future", "kept"),))


def envelope(event_type: EventType) -> EventEnvelope:
    return EventEnvelope(
        schema_version=1,
        event_id=EVENT_ID,
        event_type=event_type,
        worker_instance_id=WORKER_ID,
        worker_sequence=7,
        request_id=REQUEST_ID,
        session_hash="7K3M",
        occurred_at_utc=OCCURRED_AT,
        monotonic_offset_ns=123_456,
        severity=LogSeverity.INFO,
        payload=_payload(event_type),
        blob_digests=("b3:abcd",),
        extensions=(("future_field", "kept"),),
    )


def test_all_frozen_event_types_construct() -> None:
    assert frozenset(EventType) == ALL_EVENT_TYPES
    assert len(ALL_EVENT_TYPES) == 26
    for event_type in ALL_EVENT_TYPES:
        assert envelope(event_type).event_type is event_type


@pytest.mark.parametrize(
    ("reason", "event_type", "marker"),
    (
        (TerminalReason.COMPLETED, EventType.REQUEST_COMPLETED, TerminalMarker.OK),
        (TerminalReason.FAILED, EventType.REQUEST_FAILED, TerminalMarker.FAIL),
        (TerminalReason.CANCELLED, EventType.REQUEST_CANCELLED, TerminalMarker.CANCELLED),
        (TerminalReason.TIMED_OUT, EventType.REQUEST_TIMED_OUT, TerminalMarker.TIMEOUT),
        (
            TerminalReason.SHUTDOWN_DROPPED,
            EventType.REQUEST_SHUTDOWN_DROPPED,
            TerminalMarker.CANCELLED,
        ),
    ),
)
def test_terminal_mapping_is_exhaustive(
    reason: TerminalReason,
    event_type: EventType,
    marker: TerminalMarker,
) -> None:
    assert event_type_for_terminal_reason(reason) is event_type
    assert marker_for_terminal_reason(reason) is marker


def test_event_envelope_accepts_zero_sequence_and_offset() -> None:
    event = EventEnvelope(
        schema_version=1,
        event_id=EVENT_ID,
        event_type=EventType.REQUEST_ACCEPTED,
        worker_instance_id=WORKER_ID,
        worker_sequence=0,
        occurred_at_utc=OCCURRED_AT,
        monotonic_offset_ns=0,
        payload=LifecycleEventPayload(stage="accepted"),
    )
    assert event.worker_sequence == 0
    assert event.monotonic_offset_ns == 0


def test_event_envelope_requires_utc_timestamp() -> None:
    with pytest.raises(ValueError, match="UTC"):
        EventEnvelope(
            schema_version=1,
            event_id=EVENT_ID,
            event_type=EventType.REQUEST_ACCEPTED,
            worker_instance_id=WORKER_ID,
            worker_sequence=0,
            occurred_at_utc=datetime(2026, 7, 18, 12, 34, 56),
            payload=LifecycleEventPayload(stage="accepted"),
        )


def test_frozen_json_object_rejects_duplicate_keys() -> None:
    with pytest.raises(ValueError, match="unique"):
        FrozenJsonObject((("duplicate", 1), ("duplicate", 2)))
