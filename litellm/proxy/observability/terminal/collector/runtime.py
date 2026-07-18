from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeAlias
from uuid import UUID

from pydantic import TypeAdapter

from litellm.proxy.observability.terminal.archive.catalog import CatalogOpened, open_catalog
from litellm.proxy.observability.terminal.archive.content_pool import ContentPool, StoreBlobCommitted
from litellm.proxy.observability.terminal.archive.segment import SegmentManagerOpened, open_segment_manager
from litellm.proxy.observability.terminal.codec import EventFrameDecoder, encode_event_frame
from litellm.proxy.observability.terminal.collector.projection import ProjectionState, apply_event
from litellm.proxy.observability.terminal.config import TerminalLoggingConfig
from litellm.proxy.observability.terminal.events import BodyBoundary, EventEnvelope, EventType, RequestTerminalPayload
from litellm.proxy.observability.terminal.identity import SessionAlias, SessionDigest, resolve_session_aliases

_OPTIONAL_SEQUENCE = TypeAdapter(tuple[int | None])
_BYTE_TOTAL_ROW = TypeAdapter(tuple[int | None])

_FRAME_ROWS = TypeAdapter(list[tuple[bytes]])
Clock = Callable[[], int]


class EventSink(Protocol):
    def emit(self, event: EventEnvelope) -> None: ...


@dataclass(frozen=True, slots=True)
class ShadowCommitted:
    inserted: bool
    state: ProjectionState


@dataclass(frozen=True, slots=True)
class ShadowFailed:
    detail: str


ShadowResult: TypeAlias = ShadowCommitted | ShadowFailed


@dataclass(frozen=True, slots=True)
class ShadowRuntimeOpened:
    runtime: ShadowRuntime


@dataclass(frozen=True, slots=True)
class ShadowRuntimeOpenFailed:
    detail: str


ShadowRuntimeOpenResult: TypeAlias = ShadowRuntimeOpened | ShadowRuntimeOpenFailed


def open_shadow_runtime(
    config: TerminalLoggingConfig,
    *,
    sink: EventSink | None = None,
    clock: Clock = time.monotonic_ns,
) -> ShadowRuntimeOpenResult:
    try:
        return ShadowRuntimeOpened(ShadowRuntime(config, sink=sink, clock=clock))
    except (OSError, sqlite3.Error, ValueError) as exception:
        return ShadowRuntimeOpenFailed(str(exception))


class ShadowRuntime:
    def __init__(self, config: TerminalLoggingConfig, sink: EventSink | None, clock: Clock) -> None:
        root = config.static.central_path
        catalog_result = open_catalog(root / "catalog.sqlite")
        if not isinstance(catalog_result, CatalogOpened):
            raise ValueError(catalog_result.detail)
        manager_result = open_segment_manager(
            root,
            catalog_result.catalog,
            clock=clock,
            max_age_ns=config.static.segment_max_age_seconds * 1_000_000_000,
            max_bytes=config.static.segment_max_bytes,
        )
        if not isinstance(manager_result, SegmentManagerOpened):
            catalog_result.catalog.close()
            raise ValueError(manager_result.detail)
        self._catalog = catalog_result.catalog
        self._manager = manager_result.manager
        self._sink = sink
        self._state = ProjectionState()
        self._connections: tuple[tuple[UUID, sqlite3.Connection], ...] = ()
        self._restore_projection()

    @property
    def state(self) -> ProjectionState:
        return self._state

    def owner_segment(self, request_id: UUID) -> UUID | None:
        return self._catalog.request_owner(request_id)

    def segment_path(self, segment_id: UUID) -> Path:
        return self._manager.segment_path(segment_id)

    def captured_byte_total(self, request_id: UUID, boundary: BodyBoundary) -> int | None:
        owner = self._catalog.request_owner(request_id)
        if owner is None:
            return None
        connection = self._connection_for(owner)
        return _BYTE_TOTAL_ROW.validate_python(
            connection.execute(
                "SELECT SUM(byte_count) FROM captured_chunks WHERE request_id=? AND boundary=?",
                (str(request_id), boundary.value),
            ).fetchone()
        )[0]

    def resolve_session_alias(self, digest: SessionDigest, *, seen_at_ns: int) -> SessionAlias:
        existing = self._catalog.load_aliases()
        aliases = resolve_session_aliases((*tuple(alias.digest for alias in existing), digest))
        for alias in aliases:
            self._catalog.upsert_alias(alias, first_seen_ns=seen_at_ns, last_seen_ns=seen_at_ns)
        return next(alias for alias in aliases if alias.digest == digest)

    def observe_chunk(self, request_id: UUID, boundary: BodyBoundary, chunk: bytes) -> bool:
        try:
            owner = self._catalog.request_owner(request_id)
            segment_id = owner if owner is not None else self._manager.active_segment_id
            connection = self._connection_for(segment_id)
            self._ensure_capture_tables(connection)
            stored = ContentPool.initialize(connection).store_stream(iter((chunk,)))
            if not isinstance(stored, StoreBlobCommitted):
                return False
            sequence_row = _OPTIONAL_SEQUENCE.validate_python(
                connection.execute(
                    "SELECT MAX(sequence) FROM captured_chunks WHERE request_id=? AND boundary=?",
                    (str(request_id), boundary.value),
                ).fetchone()
            )
            sequence = 0 if sequence_row[0] is None else sequence_row[0] + 1
            connection.execute(
                "INSERT INTO captured_chunks(request_id,boundary,sequence,blob_digest,byte_count) VALUES (?,?,?,?,?)",
                (str(request_id), boundary.value, sequence, stored.digest, stored.byte_count),
            )
            connection.execute("INSERT INTO body_chunk_refs(blob_digest) VALUES (?)", (stored.digest,))
            return True
        except (OSError, sqlite3.Error, ValueError):
            return False

    def commit(self, event: EventEnvelope) -> ShadowResult:
        try:
            if event.event_type is EventType.REQUEST_ACCEPTED:
                self._manager.rotate_if_needed()
            owner = self._catalog.request_owner(event.request_id) if event.request_id is not None else None
            segment_id = owner if owner is not None else self._manager.active_segment_id
            connection = self._connection_for(segment_id)
            frame = encode_event_frame(event)
            cursor = connection.execute(
                "INSERT OR IGNORE INTO terminal_events(event_id,event_type,frame) VALUES (?,?,?)",
                (str(event.event_id), event.event_type.value, frame),
            )
            if cursor.rowcount == 0:
                return ShadowCommitted(False, self._state)
            if event.request_id is not None and event.event_type is EventType.REQUEST_ACCEPTED:
                owner = self._catalog.request_owner(event.request_id)
                if owner is None:
                    self._manager.bind_request(event.request_id, event.worker_instance_id)
            if event.request_id is not None and isinstance(event.payload, RequestTerminalPayload):
                self._manager.mark_request_terminal(event.request_id, event.payload.reason.value)
            projection = apply_event(self._state, event)
            self._state = projection.state
            if self._sink is not None:
                self._sink.emit(event)
            return ShadowCommitted(True, self._state)
        except (OSError, sqlite3.Error, ValueError) as exception:
            return ShadowFailed(str(exception))

    def close(self) -> None:
        for _segment_id, connection in self._connections:
            try:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                connection.close()
        self._connections = ()
        self._manager.close()
        self._catalog.close()

    def _connection_for(self, segment_id: UUID) -> sqlite3.Connection:
        existing = next((connection for key, connection in self._connections if key == segment_id), None)
        if existing is not None:
            return existing
        connection = sqlite3.connect(self._manager.segment_path(segment_id), isolation_level=None)
        self._connections = (*self._connections, (segment_id, connection))
        self._ensure_capture_tables(connection)
        return connection

    @staticmethod
    def _ensure_capture_tables(connection: sqlite3.Connection) -> None:
        connection.executescript(
            "CREATE TABLE IF NOT EXISTS terminal_events("
            "event_id TEXT PRIMARY KEY,event_type TEXT NOT NULL,frame BLOB NOT NULL);"
            "CREATE TABLE IF NOT EXISTS content_blobs("
            "digest TEXT PRIMARY KEY,byte_count INTEGER NOT NULL,compressed BLOB NOT NULL);"
            "CREATE TABLE IF NOT EXISTS body_chunk_refs(blob_digest TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS captured_chunks("
            "request_id TEXT NOT NULL,boundary TEXT NOT NULL,sequence INTEGER NOT NULL,"
            "blob_digest TEXT NOT NULL,byte_count INTEGER NOT NULL,PRIMARY KEY(request_id,boundary,sequence));"
        )

    def _restore_projection(self) -> None:
        for record in self._catalog.segments():
            path = self._manager.segment_path(record.segment_id)
            connection = sqlite3.connect(path)
            try:
                frames = _FRAME_ROWS.validate_python(
                    connection.execute("SELECT frame FROM terminal_events ORDER BY rowid").fetchall()
                )
            finally:
                connection.close()
            for (raw_frame,) in frames:
                decoder = EventFrameDecoder()
                event = decoder.feed(raw_frame)[0]
                decoder.finish()
                if event.request_id is not None and event.event_type is EventType.REQUEST_ACCEPTED:
                    if self._catalog.request_owner(event.request_id) is None:
                        self._catalog.bind_request(
                            event.request_id,
                            record.segment_id,
                            event.worker_instance_id,
                        )
                if event.request_id is not None and isinstance(event.payload, RequestTerminalPayload):
                    if self._catalog.request_owner(event.request_id) is not None:
                        try:
                            self._catalog.mark_request_terminal(event.request_id, event.payload.reason.value)
                        except KeyError:
                            pass
                self._state = apply_event(self._state, event).state
