from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from itertools import takewhile
from pathlib import Path
from typing import TypeAlias
from uuid import UUID

from pydantic import TypeAdapter

from litellm.proxy.observability.terminal.archive.schema import SPOOL_SCHEMA_SQL, SPOOL_SCHEMA_VERSION
from litellm.proxy.observability.terminal.codec import EventCodecError, EventFrameDecoder, encode_event_frame
from litellm.proxy.observability.terminal.events import EventEnvelope

ConnectionFactory: TypeAlias = Callable[[Path], sqlite3.Connection]
OptionalMetadataRow: TypeAlias = tuple[int, str] | None
OptionalExistingEventRow: TypeAlias = tuple[str, bytes] | None
_OPTIONAL_METADATA_ROW: TypeAdapter[OptionalMetadataRow] = TypeAdapter(OptionalMetadataRow)
_OPTIONAL_INT_ROW = TypeAdapter(tuple[int | None])
_OPTIONAL_EXISTING_EVENT_ROW: TypeAdapter[OptionalExistingEventRow] = TypeAdapter(OptionalExistingEventRow)
_EVENT_ROWS = TypeAdapter(list[tuple[int, bytes]])
_COUNT_ROW = TypeAdapter(tuple[int])


class SpoolFailureCode(StrEnum):
    CLOSED = "closed"
    CORRUPT_FRAME = "corrupt_frame"
    EMPTY_BATCH = "empty_batch"
    IDENTITY_CONFLICT = "identity_conflict"
    NON_CONTIGUOUS_BATCH = "non_contiguous_batch"
    RANGE_MISSING = "range_missing"
    SEQUENCE_GAP = "sequence_gap"
    SCHEMA_MISMATCH = "schema_mismatch"
    SQLITE_ERROR = "sqlite_error"
    WORKER_MISMATCH = "worker_mismatch"


@dataclass(frozen=True, slots=True)
class SequenceRange:
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError("sequence range start must be non-negative")
        if self.end < self.start:
            raise ValueError("sequence range end must not precede start")

    @property
    def count(self) -> int:
        return self.end - self.start + 1


@dataclass(frozen=True, slots=True)
class StoreCommitted:
    sequence_range: SequenceRange
    inserted_count: int


@dataclass(frozen=True, slots=True)
class StoreFailed:
    code: SpoolFailureCode
    detail: str


StoreResult: TypeAlias = StoreCommitted | StoreFailed


@dataclass(frozen=True, slots=True)
class AckCommitted:
    sequence_range: SequenceRange
    updated_count: int


@dataclass(frozen=True, slots=True)
class AckFailed:
    code: SpoolFailureCode
    detail: str


AckResult: TypeAlias = AckCommitted | AckFailed


@dataclass(frozen=True, slots=True)
class CompactCommitted:
    deleted_count: int
    through_sequence: int | None


@dataclass(frozen=True, slots=True)
class CompactFailed:
    code: SpoolFailureCode
    detail: str


CompactResult: TypeAlias = CompactCommitted | CompactFailed


@dataclass(frozen=True, slots=True)
class LoadCommitted:
    sequence_range: SequenceRange | None
    events: tuple[EventEnvelope, ...]


@dataclass(frozen=True, slots=True)
class LoadFailed:
    code: SpoolFailureCode
    detail: str


LoadResult: TypeAlias = LoadCommitted | LoadFailed


@dataclass(frozen=True, slots=True)
class Opened:
    spool: WorkerSpool


@dataclass(frozen=True, slots=True)
class OpenFailed:
    code: SpoolFailureCode
    detail: str


OpenResult: TypeAlias = Opened | OpenFailed


class _StoreAbort(Exception):
    def __init__(self, code: SpoolFailureCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _default_connection_factory(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path, isolation_level=None)


def open_worker_spool(
    path: Path,
    worker_instance_id: UUID,
    *,
    connection_factory: ConnectionFactory = _default_connection_factory,
) -> OpenResult:
    connection: sqlite3.Connection | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        connection = connection_factory(path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.executescript(SPOOL_SCHEMA_SQL)
        connection.execute(f"PRAGMA user_version={SPOOL_SCHEMA_VERSION}")
        metadata = _OPTIONAL_METADATA_ROW.validate_python(
            connection.execute(
                "SELECT schema_version, worker_instance_id FROM spool_metadata WHERE singleton=1"
            ).fetchone()
        )
        if metadata is None:
            connection.execute(
                "INSERT INTO spool_metadata(singleton, schema_version, worker_instance_id) VALUES (1, ?, ?)",
                (SPOOL_SCHEMA_VERSION, str(worker_instance_id)),
            )
        elif metadata[0] != SPOOL_SCHEMA_VERSION:
            connection.close()
            return OpenFailed(
                SpoolFailureCode.SCHEMA_MISMATCH,
                f"spool schema version {metadata[0]} does not match {SPOOL_SCHEMA_VERSION}",
            )
        elif metadata[1] != str(worker_instance_id):
            connection.close()
            return OpenFailed(
                SpoolFailureCode.WORKER_MISMATCH,
                f"spool metadata does not match worker {worker_instance_id}",
            )
        return Opened(WorkerSpool(connection, worker_instance_id))
    except (OSError, sqlite3.Error) as exception:
        if connection is not None:
            connection.close()
        return OpenFailed(SpoolFailureCode.SQLITE_ERROR, str(exception))


class WorkerSpool:
    def __init__(self, connection: sqlite3.Connection, worker_instance_id: UUID) -> None:
        self._connection = connection
        self._worker_instance_id = worker_instance_id
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._connection.close()
        self._closed = True

    def store(self, events: tuple[EventEnvelope, ...]) -> StoreResult:
        if self._closed:
            return StoreFailed(SpoolFailureCode.CLOSED, "worker spool is closed")
        invalid = self._validate_batch(events)
        if invalid is not None:
            return invalid
        frames = tuple(encode_event_frame(event) for event in events)
        sequence_range = SequenceRange(events[0].worker_sequence, events[-1].worker_sequence)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            inserted_count = self._store_transaction(events, frames)
            self._connection.execute("COMMIT")
            return StoreCommitted(sequence_range, inserted_count)
        except _StoreAbort as exception:
            self._rollback()
            return StoreFailed(exception.code, exception.detail)
        except sqlite3.Error as exception:
            self._rollback()
            return StoreFailed(SpoolFailureCode.SQLITE_ERROR, str(exception))

    def _validate_batch(self, events: tuple[EventEnvelope, ...]) -> StoreFailed | None:
        if not events:
            return StoreFailed(SpoolFailureCode.EMPTY_BATCH, "event batch must not be empty")
        if any(event.worker_instance_id != self._worker_instance_id for event in events):
            return StoreFailed(SpoolFailureCode.WORKER_MISMATCH, "event worker does not match spool worker")
        expected = tuple(range(events[0].worker_sequence, events[0].worker_sequence + len(events)))
        actual = tuple(event.worker_sequence for event in events)
        if actual != expected:
            return StoreFailed(SpoolFailureCode.NON_CONTIGUOUS_BATCH, "event batch sequence is not contiguous")
        return None

    def _store_transaction(self, events: tuple[EventEnvelope, ...], frames: tuple[bytes, ...]) -> int:
        maximum_row = _OPTIONAL_INT_ROW.validate_python(
            self._connection.execute("SELECT MAX(worker_sequence) FROM spool_events").fetchone()
        )
        maximum = maximum_row[0] if maximum_row[0] is not None else -1
        inserted_count = 0
        for event, frame in zip(events, frames):
            existing = _OPTIONAL_EXISTING_EVENT_ROW.validate_python(
                self._connection.execute(
                    "SELECT event_id, frame FROM spool_events WHERE worker_sequence=?",
                    (event.worker_sequence,),
                ).fetchone()
            )
            if existing is not None:
                if existing != (str(event.event_id), frame):
                    raise _StoreAbort(
                        SpoolFailureCode.IDENTITY_CONFLICT,
                        f"sequence {event.worker_sequence} already belongs to a different event",
                    )
                continue
            if event.worker_sequence != maximum + 1:
                raise _StoreAbort(
                    SpoolFailureCode.SEQUENCE_GAP,
                    f"expected sequence {maximum + 1}, got {event.worker_sequence}",
                )
            self._connection.execute(
                "INSERT INTO spool_events(worker_sequence, event_id, frame) VALUES (?, ?, ?)",
                (event.worker_sequence, str(event.event_id), frame),
            )
            maximum = event.worker_sequence
            inserted_count += 1
        return inserted_count

    def load_pending(self, *, limit: int) -> LoadResult:
        if limit < 1:
            raise ValueError("limit must be positive")
        if self._closed:
            return LoadFailed(SpoolFailureCode.CLOSED, "worker spool is closed")
        try:
            rows = _EVENT_ROWS.validate_python(
                self._connection.execute(
                    "SELECT worker_sequence, frame FROM spool_events "
                    "WHERE acknowledged=0 ORDER BY worker_sequence LIMIT ?",
                    (limit,),
                ).fetchall()
            )
            if not rows:
                return LoadCommitted(None, ())
            start = rows[0][0]
            contiguous = tuple(
                takewhile(
                    lambda indexed: indexed[1][0] == start + indexed[0],
                    enumerate(rows),
                )
            )
            events = tuple(_decode_frame(indexed[1][1]) for indexed in contiguous)
            return LoadCommitted(SequenceRange(start, start + len(events) - 1), events)
        except sqlite3.Error as exception:
            return LoadFailed(SpoolFailureCode.SQLITE_ERROR, str(exception))
        except (EventCodecError, ValueError) as exception:
            return LoadFailed(SpoolFailureCode.CORRUPT_FRAME, str(exception))

    def acknowledge(self, sequence_range: SequenceRange) -> AckResult:
        if self._closed:
            return AckFailed(SpoolFailureCode.CLOSED, "worker spool is closed")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            count = _COUNT_ROW.validate_python(
                self._connection.execute(
                    "SELECT COUNT(*) FROM spool_events WHERE worker_sequence BETWEEN ? AND ?",
                    (sequence_range.start, sequence_range.end),
                ).fetchone()
            )[0]
            if count != sequence_range.count:
                self._rollback()
                return AckFailed(SpoolFailureCode.RANGE_MISSING, "ack range contains missing events")
            cursor = self._connection.execute(
                "UPDATE spool_events SET acknowledged=1 WHERE acknowledged=0 AND worker_sequence BETWEEN ? AND ?",
                (sequence_range.start, sequence_range.end),
            )
            self._connection.execute("COMMIT")
            return AckCommitted(sequence_range, cursor.rowcount)
        except sqlite3.Error as exception:
            self._rollback()
            return AckFailed(SpoolFailureCode.SQLITE_ERROR, str(exception))

    def compact_acknowledged_prefix(self) -> CompactResult:
        if self._closed:
            return CompactFailed(SpoolFailureCode.CLOSED, "worker spool is closed")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            first_unacknowledged = _OPTIONAL_INT_ROW.validate_python(
                self._connection.execute(
                    "SELECT MIN(worker_sequence) FROM spool_events WHERE acknowledged=0"
                ).fetchone()
            )[0]
            boundary = (
                first_unacknowledged
                if first_unacknowledged is not None
                else _OPTIONAL_INT_ROW.validate_python(
                    self._connection.execute("SELECT MAX(worker_sequence) + 1 FROM spool_events").fetchone()
                )[0]
            )
            if boundary is None:
                boundary = 0
            through = _OPTIONAL_INT_ROW.validate_python(
                self._connection.execute(
                    "SELECT MAX(worker_sequence) FROM spool_events WHERE acknowledged=1 AND worker_sequence < ?",
                    (boundary,),
                ).fetchone()
            )[0]
            cursor = self._connection.execute(
                "DELETE FROM spool_events WHERE acknowledged=1 AND worker_sequence < ?",
                (boundary,),
            )
            self._connection.execute("COMMIT")
            return CompactCommitted(cursor.rowcount, through)
        except sqlite3.Error as exception:
            self._rollback()
            return CompactFailed(SpoolFailureCode.SQLITE_ERROR, str(exception))

    def _rollback(self) -> None:
        try:
            self._connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass


def _decode_frame(frame: bytes) -> EventEnvelope:
    decoder = EventFrameDecoder()
    events = decoder.feed(frame)
    decoder.finish()
    if len(events) != 1:
        raise ValueError("stored event frame must contain exactly one event")
    return events[0]
