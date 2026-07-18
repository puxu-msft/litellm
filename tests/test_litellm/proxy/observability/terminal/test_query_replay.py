from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import duckdb

from litellm.proxy.observability.terminal.archive.catalog import SegmentRecord, SegmentState
from litellm.proxy.observability.terminal.archive.content_pool import ContentPool, StoreBlobCommitted
from litellm.proxy.observability.terminal.capture.body import BodyCompleteness, BodyManifest, CapturedChunk
from litellm.proxy.observability.terminal.events import BodyBoundary
from litellm.proxy.observability.terminal.query.coordinator import QueryCoordinator, QueryRejected, QueryRows
from litellm.proxy.observability.terminal.replay.offline import BodyReconstructed, reconstruct_body


def _prepare_extension(root: Path) -> Path:
    extension_dir = root / "extensions"
    extension_dir.mkdir()
    connection = duckdb.connect()
    connection.execute("SET extension_directory=?", [str(extension_dir)])
    connection.execute("INSTALL sqlite")
    connection.close()
    return extension_dir


def test_duckdb_queries_multiple_sqlite_segments_and_rejects_write(tmp_path: Path) -> None:
    segments: list[SegmentRecord] = []
    for index in range(2):
        path = tmp_path / f"segment-{index}.sqlite"
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE terminal_events(event_id TEXT,event_type TEXT,frame BLOB)")
            connection.execute(
                "CREATE TABLE captured_chunks(request_id TEXT,boundary TEXT,sequence INTEGER,blob_digest TEXT,byte_count INTEGER)"
            )
            connection.execute("CREATE TABLE content_blobs(digest TEXT,byte_count INTEGER,compressed BLOB)")
            connection.execute("INSERT INTO terminal_events VALUES (?,?,?)", (f"e{index}", "request.accepted", b"x"))
            connection.execute(
                "INSERT INTO captured_chunks VALUES (?,?,?,?,?)",
                (f"r{index}", "client.request", 0, f"b{index}", index + 1),
            )
        segments.append(SegmentRecord(UUID(int=index + 1), SegmentState.PUBLISHED, path.name, 1, index))
    coordinator = QueryCoordinator(tmp_path, tuple(segments), _prepare_extension(tmp_path))
    result = coordinator.query("SELECT event_id FROM terminal_events ORDER BY event_id")
    rejected = coordinator.query("DELETE FROM terminal_events")
    chunks = coordinator.query("SELECT request_id,byte_count FROM captured_chunks ORDER BY request_id")
    coordinator.close()
    assert result == QueryRows(("event_id",), (("e0",), ("e1",)))
    assert chunks == QueryRows(("request_id", "byte_count"), (("r0", 1), ("r1", 2)))
    assert isinstance(rejected, QueryRejected)


def test_offline_reconstruct_joins_manifest_chunks() -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    pool = ContentPool.initialize(connection)
    stored = tuple(pool.store_stream(iter((chunk,))) for chunk in (b"first", b"second"))
    committed = tuple(item for item in stored if isinstance(item, StoreBlobCommitted))
    manifest = BodyManifest(
        BodyBoundary.UPSTREAM_RESPONSE,
        tuple(
            CapturedChunk(index, item.digest, item.byte_count, datetime.now(timezone.utc), index)
            for index, item in enumerate(committed)
        ),
        BodyCompleteness.complete(),
    )
    assert reconstruct_body(pool, manifest) == BodyReconstructed(b"firstsecond")
    connection.close()
