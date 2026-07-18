from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias
from uuid import UUID, uuid4

from litellm.proxy.observability.terminal.archive.catalog import Catalog, SegmentRecord, SegmentState
from litellm.proxy.observability.terminal.archive.content_pool import BlobCorrupt, BlobMissing, ContentPool

from pydantic import TypeAdapter

SEGMENT_SCHEMA_VERSION = 1
Clock = Callable[[], int]
UUIDSource = Callable[[], UUID]


@dataclass(frozen=True, slots=True)
class SegmentManagerOpened:
    manager: SegmentManager


@dataclass(frozen=True, slots=True)
class SegmentManagerOpenFailed:
    detail: str


SegmentManagerOpenResult: TypeAlias = SegmentManagerOpened | SegmentManagerOpenFailed


@dataclass(frozen=True, slots=True)
class SegmentNotSealable:
    open_request_count: int


@dataclass(frozen=True, slots=True)
class SegmentSealed:
    segment_id: UUID
    path: Path


@dataclass(frozen=True, slots=True)
class SegmentIncomplete:
    missing_digests: tuple[str, ...]
    corrupt_digests: tuple[str, ...]


SealResult: TypeAlias = SegmentNotSealable | SegmentIncomplete | SegmentSealed
_DIGEST_ROWS = TypeAdapter(list[tuple[str]])


def open_segment_manager(
    root: Path,
    catalog: Catalog,
    *,
    clock: Clock,
    max_age_ns: int,
    max_bytes: int,
    uuid_source: UUIDSource = uuid4,
) -> SegmentManagerOpenResult:
    try:
        if max_age_ns < 1 or max_bytes < 1:
            raise ValueError("segment thresholds must be positive")
        root.mkdir(parents=True, exist_ok=True)
        (root / "segments").mkdir(exist_ok=True)
        active = tuple(record for record in catalog.segments() if record.state is SegmentState.ACTIVE)
        if len(active) > 1:
            raise ValueError("catalog contains multiple active segments")
        return SegmentManagerOpened(
            SegmentManager.create(
                root,
                catalog,
                clock,
                max_age_ns,
                max_bytes,
                uuid_source,
                active[0].segment_id if active else None,
            )
        )
    except (OSError, sqlite3.Error, ValueError) as exception:
        return SegmentManagerOpenFailed(str(exception))


class SegmentManager:
    def __init__(
        self,
        root: Path,
        catalog: Catalog,
        clock: Clock,
        max_age_ns: int,
        max_bytes: int,
        uuid_source: UUIDSource,
    ) -> None:
        self._root = root
        self._catalog = catalog
        self._clock = clock
        self._max_age_ns = max_age_ns
        self._max_bytes = max_bytes
        self._uuid_source = uuid_source
        self._active_segment_id = UUID(int=0)

    @classmethod
    def create(
        cls,
        root: Path,
        catalog: Catalog,
        clock: Clock,
        max_age_ns: int,
        max_bytes: int,
        uuid_source: UUIDSource,
        active_segment_id: UUID | None,
    ) -> SegmentManager:
        manager = cls(root, catalog, clock, max_age_ns, max_bytes, uuid_source)
        manager._active_segment_id = (
            active_segment_id if active_segment_id is not None else manager._create_active_segment()
        )
        return manager

    @property
    def active_segment_id(self) -> UUID:
        return self._active_segment_id

    def close(self) -> None:
        pass

    def _create_active_segment(self) -> UUID:
        segment_id = self._uuid_source()
        path = self._root / "segments" / f"{segment_id}.active.sqlite"
        descriptor = os.open(path, os.O_CREAT | os.O_CLOEXEC, 0o600)
        os.close(descriptor)
        with sqlite3.connect(path) as connection:
            ContentPool.initialize(connection)
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS body_chunk_refs("
                "blob_digest TEXT NOT NULL REFERENCES content_blobs(digest));"
                "CREATE TABLE IF NOT EXISTS terminal_events("
                "event_id TEXT PRIMARY KEY,event_type TEXT NOT NULL,frame BLOB NOT NULL);"
            )
        self._catalog.register_segment(
            SegmentRecord(
                segment_id,
                SegmentState.ACTIVE,
                str(path.relative_to(self._root)),
                SEGMENT_SCHEMA_VERSION,
                self._clock(),
            )
        )
        return segment_id

    def bind_request(self, request_id: UUID, worker_id: UUID) -> None:
        if self._catalog.request_owner(request_id) is not None:
            raise ValueError(f"request {request_id} is already bound")
        if not self._catalog.bind_request(request_id, self._active_segment_id, worker_id):
            raise ValueError(f"request {request_id} is already bound")

    def owner_segment(self, request_id: UUID) -> UUID | None:
        return self._catalog.request_owner(request_id)

    def mark_request_terminal(self, request_id: UUID, reason: str) -> None:
        self._catalog.mark_request_terminal(request_id, reason)

    def segment_state(self, segment_id: UUID) -> SegmentState:
        return self._catalog.get_segment(segment_id).state

    def segment_path(self, segment_id: UUID) -> Path:
        return self._root / self._catalog.get_segment(segment_id).path

    def rotate_if_needed(self) -> UUID | None:
        record = self._catalog.get_segment(self._active_segment_id)
        path = self.segment_path(self._active_segment_id)
        age = self._clock() - record.created_at_ns
        if age < self._max_age_ns and path.stat().st_size < self._max_bytes:
            return None
        return self.rotate_force()

    def rotate_force(self) -> UUID:
        old = self._active_segment_id
        self._catalog.transition_segment(old, SegmentState.DRAINING)
        self._active_segment_id = self._create_active_segment()
        return old

    def seal(self, segment_id: UUID) -> SealResult:
        open_requests = self._catalog.open_requests(segment_id)
        if open_requests:
            return SegmentNotSealable(len(open_requests))
        record = self._catalog.get_segment(segment_id)
        if record.state is not SegmentState.DRAINING:
            raise ValueError("only draining segments can be sealed")
        active_path = self._root / record.path
        closure = _validate_blob_closure(active_path)
        if closure is not None:
            return closure
        final_path = active_path.with_name(f"{segment_id}.segment.sqlite")
        relative_final = str(final_path.relative_to(self._root))
        self._catalog.transition_segment(segment_id, SegmentState.PUBLISHING, path=relative_final)
        with sqlite3.connect(active_path) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        active_path.rename(final_path)
        self._catalog.transition_segment(segment_id, SegmentState.PUBLISHED)
        return SegmentSealed(segment_id, final_path)


def _validate_blob_closure(path: Path) -> SegmentIncomplete | None:
    with sqlite3.connect(path, isolation_level=None) as connection:
        digests = tuple(
            row[0]
            for row in _DIGEST_ROWS.validate_python(
                connection.execute("SELECT DISTINCT blob_digest FROM body_chunk_refs ORDER BY blob_digest").fetchall()
            )
        )
        pool = ContentPool.initialize(connection)
        loaded = tuple((digest, pool.load(digest)) for digest in digests)
    missing = tuple(digest for digest, result in loaded if isinstance(result, BlobMissing))
    corrupt = tuple(digest for digest, result in loaded if isinstance(result, BlobCorrupt))
    return SegmentIncomplete(missing, corrupt) if missing or corrupt else None
