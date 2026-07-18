from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TypeAlias
from uuid import UUID

from pydantic import TypeAdapter

from litellm.proxy.observability.terminal.identity import SessionAlias, SessionDigest

CATALOG_SCHEMA_VERSION = 1


class SegmentState(StrEnum):
    ACTIVE = "active"
    DRAINING = "draining"
    PUBLISHING = "publishing"
    PUBLISHED = "published"


_ALLOWED_TRANSITIONS = {
    SegmentState.ACTIVE: SegmentState.DRAINING,
    SegmentState.DRAINING: SegmentState.PUBLISHING,
    SegmentState.PUBLISHING: SegmentState.PUBLISHED,
}


@dataclass(frozen=True, slots=True)
class SegmentRecord:
    segment_id: UUID
    state: SegmentState
    path: str
    schema_version: int
    created_at_ns: int


@dataclass(frozen=True, slots=True)
class CatalogOpened:
    catalog: Catalog


@dataclass(frozen=True, slots=True)
class CatalogOpenFailed:
    detail: str


CatalogOpenResult: TypeAlias = CatalogOpened | CatalogOpenFailed
_OPTIONAL_SEGMENT_ROW: TypeAdapter[tuple[str, str, str, int, int] | None] = TypeAdapter(
    tuple[str, str, str, int, int] | None
)
_SEGMENT_ROWS = TypeAdapter(list[tuple[str, str, str, int, int]])
_REQUEST_ROWS = TypeAdapter(list[tuple[str, str]])
_OPTIONAL_REQUEST_OWNER_ROW: TypeAdapter[tuple[str] | None] = TypeAdapter(tuple[str] | None)
_ALIAS_ROWS = TypeAdapter(list[tuple[bytes, int]])


def open_catalog(path: Path) -> CatalogOpenResult:
    connection: sqlite3.Connection | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_CLOEXEC, 0o600)
        os.close(descriptor)
        connection = sqlite3.connect(path, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS segments(
                segment_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                path TEXT NOT NULL UNIQUE,
                schema_version INTEGER NOT NULL,
                created_at_ns INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS request_owners(
                request_id TEXT PRIMARY KEY,
                segment_id TEXT NOT NULL REFERENCES segments(segment_id),
                worker_instance_id TEXT NOT NULL,
                terminal_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS session_aliases(
                digest BLOB PRIMARY KEY,
                prefix_length INTEGER NOT NULL,
                first_seen_ns INTEGER NOT NULL,
                last_seen_ns INTEGER NOT NULL
            );
            """
        )
        connection.execute(f"PRAGMA user_version={CATALOG_SCHEMA_VERSION}")
        return CatalogOpened(Catalog(connection))
    except (OSError, sqlite3.Error) as exception:
        if connection is not None:
            connection.close()
        return CatalogOpenFailed(str(exception))


class Catalog:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def close(self) -> None:
        self._connection.close()

    def register_segment(self, record: SegmentRecord) -> None:
        self._connection.execute(
            "INSERT INTO segments(segment_id,state,path,schema_version,created_at_ns) VALUES (?,?,?,?,?)",
            (
                str(record.segment_id),
                record.state.value,
                record.path,
                record.schema_version,
                record.created_at_ns,
            ),
        )

    def transition_segment(self, segment_id: UUID, state: SegmentState, *, path: str | None = None) -> None:
        current = self.get_segment(segment_id).state
        expected = _ALLOWED_TRANSITIONS.get(current)
        if expected is not state:
            raise ValueError(f"illegal segment transition {current.value} -> {state.value}")
        cursor = self._connection.execute(
            "UPDATE segments SET state=?, path=COALESCE(?,path) WHERE segment_id=? AND state=?",
            (state.value, path, str(segment_id), current.value),
        )
        if cursor.rowcount != 1:
            raise KeyError(segment_id)

    def get_segment(self, segment_id: UUID) -> SegmentRecord:
        row = _OPTIONAL_SEGMENT_ROW.validate_python(
            self._connection.execute(
                "SELECT segment_id,state,path,schema_version,created_at_ns FROM segments WHERE segment_id=?",
                (str(segment_id),),
            ).fetchone()
        )
        if row is None:
            raise KeyError(segment_id)
        return _segment_record(row)

    def segments(self) -> tuple[SegmentRecord, ...]:
        rows = _SEGMENT_ROWS.validate_python(
            self._connection.execute(
                "SELECT segment_id,state,path,schema_version,created_at_ns FROM segments ORDER BY created_at_ns"
            ).fetchall()
        )
        return tuple(_segment_record(row) for row in rows)

    def bind_request(self, request_id: UUID, segment_id: UUID, worker_id: UUID) -> bool:
        cursor = self._connection.execute(
            "INSERT OR IGNORE INTO request_owners(request_id,segment_id,worker_instance_id) VALUES (?,?,?)",
            (str(request_id), str(segment_id), str(worker_id)),
        )
        return cursor.rowcount == 1

    def request_owner(self, request_id: UUID) -> UUID | None:
        row = _OPTIONAL_REQUEST_OWNER_ROW.validate_python(
            self._connection.execute(
                "SELECT segment_id FROM request_owners WHERE request_id=?", (str(request_id),)
            ).fetchone()
        )
        return UUID(str(row[0])) if row is not None else None

    def open_requests(self, segment_id: UUID) -> tuple[tuple[UUID, UUID], ...]:
        rows = _REQUEST_ROWS.validate_python(
            self._connection.execute(
                "SELECT request_id,worker_instance_id FROM request_owners "
                "WHERE segment_id=? AND terminal_reason IS NULL ORDER BY request_id",
                (str(segment_id),),
            ).fetchall()
        )
        return tuple((UUID(request_id), UUID(worker_id)) for request_id, worker_id in rows)

    def mark_request_terminal(self, request_id: UUID, reason: str) -> None:
        cursor = self._connection.execute(
            "UPDATE request_owners SET terminal_reason=? WHERE request_id=? AND terminal_reason IS NULL",
            (reason, str(request_id)),
        )
        if cursor.rowcount != 1:
            raise KeyError(request_id)

    def upsert_alias(self, alias: SessionAlias, *, first_seen_ns: int, last_seen_ns: int) -> None:
        self._connection.execute(
            "INSERT INTO session_aliases(digest,prefix_length,first_seen_ns,last_seen_ns) VALUES (?,?,?,?) "
            "ON CONFLICT(digest) DO UPDATE SET prefix_length=excluded.prefix_length, "
            "first_seen_ns=MIN(first_seen_ns,excluded.first_seen_ns), "
            "last_seen_ns=MAX(last_seen_ns,excluded.last_seen_ns)",
            (alias.digest.raw, len(alias.display_hash), first_seen_ns, last_seen_ns),
        )

    def load_aliases(self) -> tuple[SessionAlias, ...]:
        rows = _ALIAS_ROWS.validate_python(
            self._connection.execute("SELECT digest,prefix_length FROM session_aliases ORDER BY digest").fetchall()
        )
        return tuple(SessionAlias(SessionDigest(raw), SessionDigest(raw).display(length)) for raw, length in rows)


def _segment_record(row: tuple[str, str, str, int, int]) -> SegmentRecord:
    segment_id, state, path, schema_version, created_at_ns = row
    return SegmentRecord(UUID(segment_id), SegmentState(state), path, schema_version, created_at_ns)
