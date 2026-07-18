from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TypeAlias
from uuid import UUID

from litellm.proxy.observability.terminal.events import EventEnvelope, RequestTerminalPayload


@dataclass(frozen=True, slots=True)
class ProjectedRequest:
    request_id: UUID
    worker_instance_id: UUID
    session_hash: str | None
    stage: str
    last_sequence: int


@dataclass(frozen=True, slots=True)
class ProjectionState:
    requests: tuple[ProjectedRequest, ...] = ()
    seen_event_ids: frozenset[UUID] = frozenset()
    anomalies: tuple[str, ...] = ()
    terminated_request_ids: frozenset[UUID] = frozenset()


@dataclass(frozen=True, slots=True)
class ProjectionApplied:
    state: ProjectionState


@dataclass(frozen=True, slots=True)
class ProjectionDuplicate:
    state: ProjectionState


ProjectionResult: TypeAlias = ProjectionApplied | ProjectionDuplicate


def apply_event(state: ProjectionState, event: EventEnvelope) -> ProjectionResult:
    if event.event_id in state.seen_event_ids:
        return ProjectionDuplicate(state)
    seen = state.seen_event_ids | {event.event_id}
    if event.request_id is None:
        return ProjectionApplied(replace(state, seen_event_ids=seen))
    if event.request_id in state.terminated_request_ids:
        return ProjectionApplied(
            replace(
                state,
                seen_event_ids=seen,
                anomalies=(*state.anomalies, f"lifecycle_after_terminal:{event.request_id}"),
            )
        )
    existing = next((item for item in state.requests if item.request_id == event.request_id), None)
    terminal = isinstance(event.payload, RequestTerminalPayload)
    if terminal:
        if existing is None:
            return ProjectionApplied(
                replace(
                    state,
                    seen_event_ids=seen,
                    anomalies=(*state.anomalies, f"terminal_without_request:{event.request_id}"),
                )
            )
        return ProjectionApplied(
            replace(
                state,
                seen_event_ids=seen,
                requests=tuple(item for item in state.requests if item.request_id != event.request_id),
                terminated_request_ids=state.terminated_request_ids | {event.request_id},
            )
        )
    stage = event.event_type.value
    projected = ProjectedRequest(
        event.request_id,
        event.worker_instance_id,
        event.session_hash,
        stage,
        event.worker_sequence,
    )
    if existing is None:
        return ProjectionApplied(replace(state, seen_event_ids=seen, requests=(*state.requests, projected)))
    if event.worker_sequence <= existing.last_sequence:
        return ProjectionApplied(
            replace(
                state,
                seen_event_ids=seen,
                anomalies=(*state.anomalies, f"non_monotonic:{event.request_id}"),
            )
        )
    return ProjectionApplied(
        replace(
            state,
            seen_event_ids=seen,
            requests=tuple(projected if item.request_id == event.request_id else item for item in state.requests),
        )
    )
