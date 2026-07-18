from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import IntEnum, StrEnum
from uuid import UUID, uuid4


class RequestStage(IntEnum):
    RECEIVED = 1
    AUTH = 2
    UPSTREAM = 3
    STREAMING = 4
    ACCOUNTING = 5


class RequestTerminalReason(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    SHUTDOWN_DROPPED = "shutdown_dropped"


@dataclass(frozen=True, slots=True)
class RequestRecord:
    id: UUID
    method: str
    path: str
    client_ip: str | None
    started_at_monotonic: float
    started_at_wall: float
    model: str | None = None
    call_type: str | None = None
    provider: str | None = None
    streaming: bool | None = None
    stage: RequestStage = RequestStage.RECEIVED
    version: int = 1


@dataclass(frozen=True, slots=True)
class RegistryEvent:
    sequence: int
    record: RequestRecord
    terminal_reason: RequestTerminalReason | None


RegistrySubscriber = Callable[[RegistryEvent], None]


class InFlightRegistry:
    def __init__(self) -> None:
        self._records: tuple[RequestRecord, ...] = ()
        self._sequence = 0
        self._subscribers: tuple[RegistrySubscriber, ...] = ()

    def subscribe(self, subscriber: RegistrySubscriber) -> Callable[[], None]:
        self._subscribers = (*self._subscribers, subscriber)

        def unsubscribe() -> None:
            self._subscribers = tuple(item for item in self._subscribers if item is not subscriber)

        return unsubscribe

    def register(
        self,
        *,
        method: str,
        path: str,
        client_ip: str | None,
        id_source: Callable[[], UUID] = uuid4,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> RequestRecord:
        record = RequestRecord(id_source(), method, path, client_ip, monotonic_clock(), wall_clock())
        self._records = (*self._records, record)
        self._publish(record, None)
        return record

    def advance_stage(self, request_id: UUID, stage: RequestStage) -> RequestRecord:
        record = self._required(request_id)
        updated = replace(record, stage=max(record.stage, stage), version=record.version + 1)
        self._replace(updated)
        self._publish(updated, None)
        return updated

    def set_llm_context(
        self,
        request_id: UUID,
        *,
        model: str | None,
        call_type: str | None,
        provider: str | None,
        streaming: bool | None,
    ) -> RequestRecord:
        record = self._required(request_id)
        updated = replace(
            record,
            model=model,
            call_type=call_type,
            provider=provider,
            streaming=streaming,
            version=record.version + 1,
        )
        self._replace(updated)
        self._publish(updated, None)
        return updated

    def finish(self, request_id: UUID, reason: RequestTerminalReason) -> RequestRecord:
        record = self._required(request_id)
        self._records = tuple(item for item in self._records if item.id != request_id)
        updated = replace(record, version=record.version + 1)
        self._publish(updated, reason)
        return updated

    def snapshot(self) -> tuple[RequestRecord, ...]:
        return self._records

    def _required(self, request_id: UUID) -> RequestRecord:
        record = next((item for item in self._records if item.id == request_id), None)
        if record is None:
            raise KeyError(request_id)
        return record

    def _replace(self, record: RequestRecord) -> None:
        self._records = tuple(record if item.id == record.id else item for item in self._records)

    def _publish(self, record: RequestRecord, terminal_reason: RequestTerminalReason | None) -> None:
        self._sequence += 1
        event = RegistryEvent(self._sequence, record, terminal_reason)
        for subscriber in self._subscribers:
            subscriber(event)


GLOBAL_IN_FLIGHT_REGISTRY = InFlightRegistry()
