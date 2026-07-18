from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from litellm.proxy.middleware.in_flight_registry import RegistryEvent, RequestStage
from litellm.proxy.observability.terminal.collector.runtime import ShadowRuntime
from litellm.proxy.observability.terminal.events import (
    EventEnvelope,
    EventType,
    LifecycleEventPayload,
    LogEventPayload,
    RequestTerminalPayload,
    TerminalReason,
)


@dataclass(frozen=True, slots=True)
class RegistryEventAdapter:
    worker_instance_id: UUID
    event_id_source: Callable[[], UUID] = uuid4
    wall_clock: Callable[[], float] = time.time
    monotonic_clock: Callable[[], float] = time.monotonic

    def convert(self, event: RegistryEvent) -> EventEnvelope:
        event_type, payload = _payload(event)
        return EventEnvelope(
            schema_version=1,
            event_id=self.event_id_source(),
            event_type=event_type,
            worker_instance_id=self.worker_instance_id,
            worker_sequence=event.sequence,
            request_id=event.record.id,
            session_hash=event.record.session_hash,
            occurred_at_utc=datetime.fromtimestamp(self.wall_clock(), timezone.utc),
            monotonic_offset_ns=max(
                0,
                round((self.monotonic_clock() - event.record.started_at_monotonic) * 1_000_000_000),
            ),
            severity=None,
            payload=payload,
        )

    def subscriber(self, runtime: ShadowRuntime) -> Callable[[RegistryEvent], None]:
        def commit(event: RegistryEvent) -> None:
            runtime.commit(self.convert(event))

        return commit


def _payload(
    event: RegistryEvent,
) -> tuple[EventType, LifecycleEventPayload | LogEventPayload | RequestTerminalPayload]:
    if event.terminal_reason is not None:
        reason = TerminalReason(event.terminal_reason.value)
        return _terminal_type(reason), RequestTerminalPayload(reason)
    event_type = {
        RequestStage.RECEIVED: EventType.REQUEST_ACCEPTED,
        RequestStage.AUTH: EventType.REQUEST_ROUTED,
        RequestStage.UPSTREAM: EventType.UPSTREAM_STARTED,
        RequestStage.STREAMING: EventType.REQUEST_STREAMING,
        RequestStage.ACCOUNTING: EventType.LOG_RECORD,
    }[event.record.stage]
    if event.record.stage is RequestStage.ACCOUNTING:
        return event_type, LogEventPayload("InFlightRegistry", "accounting")
    return event_type, LifecycleEventPayload(stage=event.record.stage.name.lower())


def _terminal_type(reason: TerminalReason) -> EventType:
    return {
        TerminalReason.COMPLETED: EventType.REQUEST_COMPLETED,
        TerminalReason.FAILED: EventType.REQUEST_FAILED,
        TerminalReason.CANCELLED: EventType.REQUEST_CANCELLED,
        TerminalReason.TIMED_OUT: EventType.REQUEST_TIMED_OUT,
        TerminalReason.SHUTDOWN_DROPPED: EventType.REQUEST_SHUTDOWN_DROPPED,
    }[reason]
