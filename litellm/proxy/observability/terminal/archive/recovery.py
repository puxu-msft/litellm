from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from pydantic import TypeAdapter

from litellm.proxy.observability.terminal.archive.catalog import Catalog, SegmentState
from litellm.proxy.observability.terminal.events import TerminalReason

_TABLE_NAME_ROWS = TypeAdapter(list[tuple[str]])


@dataclass(frozen=True, slots=True)
class RecoveredRequest:
    request_id: UUID
    segment_id: UUID
    reason: TerminalReason


@dataclass(frozen=True, slots=True)
class RecoveryCompleted:
    recovered_requests: tuple[RecoveredRequest, ...]
    published_segments: tuple[UUID, ...]
    orphan_blobs_deleted: int
    missing_segment_files: tuple[UUID, ...] = ()


def reconcile_archive(root: Path, catalog: Catalog, *, live_workers: frozenset[UUID]) -> RecoveryCompleted:
    recovered: tuple[RecoveredRequest, ...] = ()
    published: tuple[UUID, ...] = ()
    orphan_count = 0
    missing_files: tuple[UUID, ...] = ()
    for segment in catalog.segments():
        for request_id, worker_id in catalog.open_requests(segment.segment_id):
            if worker_id not in live_workers:
                catalog.mark_request_terminal(request_id, TerminalReason.SHUTDOWN_DROPPED.value)
                recovered = (
                    *recovered,
                    RecoveredRequest(request_id, segment.segment_id, TerminalReason.SHUTDOWN_DROPPED),
                )
        segment_path = root / segment.path
        if segment.state is SegmentState.PUBLISHING:
            old_path = root / "segments" / f"{segment.segment_id}.active.sqlite"
            if not segment_path.exists() and old_path.exists():
                old_path.rename(segment_path)
            if segment_path.exists():
                catalog.transition_segment(segment.segment_id, SegmentState.PUBLISHED)
                published = (*published, segment.segment_id)
                segment = catalog.get_segment(segment.segment_id)
            else:
                missing_files = (*missing_files, segment.segment_id)
        if segment_path.exists():
            with sqlite3.connect(segment_path) as connection:
                tables = frozenset(
                    row[0]
                    for row in _TABLE_NAME_ROWS.validate_python(
                        connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                    )
                )
                can_gc = segment.state is SegmentState.PUBLISHED or (
                    segment.state is SegmentState.DRAINING and not catalog.open_requests(segment.segment_id)
                )
                if can_gc and {"content_blobs", "body_chunk_refs"}.issubset(tables):
                    cursor = connection.execute(
                        "DELETE FROM content_blobs WHERE digest NOT IN (SELECT blob_digest FROM body_chunk_refs)"
                    )
                    orphan_count += cursor.rowcount
    return RecoveryCompleted(recovered, published, orphan_count, missing_files)
