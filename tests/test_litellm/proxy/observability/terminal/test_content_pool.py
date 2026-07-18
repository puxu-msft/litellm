from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from random import Random

import blake3
import zstandard

from litellm.proxy.observability.terminal.archive.content_pool import (
    BlobCorrupt,
    BlobFound,
    BlobMissing,
    ContentPool,
    StoreBlobCommitted,
    StoreBlobFailed,
)
from litellm.proxy.observability.terminal.capture.body import (
    BodyCompleteness,
    BodyManifest,
    CapturedChunk,
    ManifestValid,
    validate_manifest,
)
from litellm.proxy.observability.terminal.events import BodyBoundary


def _pool() -> tuple[sqlite3.Connection, ContentPool]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    return connection, ContentPool.initialize(connection)


def test_store_stream_uses_blake3_and_zstd_with_direct_oracles() -> None:
    connection, pool = _pool()
    chunks = (b"hello ", "世界".encode(), b"!" * 1024)
    expected = b"".join(chunks)
    stored = pool.store_stream(iter(chunks))
    connection.close()

    assert isinstance(stored, StoreBlobCommitted)
    assert stored.digest == f"b3:{blake3.blake3(expected).hexdigest()}"
    assert stored.byte_count == len(expected)
    assert zstandard.ZstdDecompressor().decompress(stored.compressed, max_output_size=len(expected)) == expected


def test_duplicate_blob_is_stored_once() -> None:
    connection, pool = _pool()
    first = pool.store_stream(iter((b"same",)))
    second = pool.store_stream(iter((b"same",)))
    assert isinstance(first, StoreBlobCommitted)
    assert isinstance(second, StoreBlobCommitted)
    assert first.inserted is True
    assert second.inserted is False
    assert connection.execute("SELECT COUNT(*) FROM content_blobs").fetchone() == (1,)
    connection.close()


def test_load_round_trip_and_missing_value() -> None:
    connection, pool = _pool()
    stored = pool.store_stream(iter((b"first", b"second")))
    assert isinstance(stored, StoreBlobCommitted)
    assert pool.load(stored.digest) == BlobFound(stored.digest, b"firstsecond")
    assert pool.load("b3:" + "0" * 64) == BlobMissing("b3:" + "0" * 64)
    connection.close()


def test_corrupt_compressed_blob_is_a_value() -> None:
    connection, pool = _pool()
    stored = pool.store_stream(iter((b"payload",)))
    assert isinstance(stored, StoreBlobCommitted)
    connection.execute("UPDATE content_blobs SET compressed=? WHERE digest=?", (b"corrupt", stored.digest))
    loaded = pool.load(stored.digest)
    assert isinstance(loaded, BlobCorrupt)
    connection.close()


def test_digest_mismatch_is_corrupt() -> None:
    connection, pool = _pool()
    stored = pool.store_stream(iter((b"payload",)))
    replacement = zstandard.ZstdCompressor().compress(b"changed")
    assert isinstance(stored, StoreBlobCommitted)
    connection.execute(
        "UPDATE content_blobs SET compressed=?, byte_count=? WHERE digest=?",
        (replacement, len(b"changed"), stored.digest),
    )
    assert isinstance(pool.load(stored.digest), BlobCorrupt)
    connection.close()


def test_empty_stream_is_valid_blob() -> None:
    connection, pool = _pool()
    stored = pool.store_stream(iter(()))
    assert isinstance(stored, StoreBlobCommitted)
    assert stored.byte_count == 0
    assert pool.load(stored.digest) == BlobFound(stored.digest, b"")
    connection.close()


def test_stream_is_consumed_incrementally() -> None:
    connection, pool = _pool()
    pulls = 0

    def source():
        nonlocal pulls
        for _index in range(256):
            pulls += 1
            yield b"x" * 4096

    stored = pool.store_stream(source())
    assert isinstance(stored, StoreBlobCommitted)
    assert pulls == 256
    assert stored.byte_count == 256 * 4096
    assert len(stored.compressed) < stored.byte_count
    connection.close()


def test_store_failure_is_a_value() -> None:
    connection, pool = _pool()
    connection.close()
    result = pool.store_stream(iter((b"payload",)))
    assert isinstance(result, StoreBlobFailed)


def test_random_chunk_cuts_reconstruct_original_payload() -> None:
    random = Random(20260718)
    payload = random.randbytes(64 * 1024)
    cuts = tuple(sorted(random.sample(range(1, len(payload)), 127)))
    boundaries = (0, *cuts, len(payload))
    chunks = tuple(payload[start:end] for start, end in zip(boundaries, boundaries[1:]))
    connection, pool = _pool()
    stored = tuple(pool.store_stream(iter((chunk,))) for chunk in chunks)
    assert all(isinstance(item, StoreBlobCommitted) for item in stored)
    committed = tuple(item for item in stored if isinstance(item, StoreBlobCommitted))
    start_time = datetime(2026, 7, 18, tzinfo=timezone.utc)
    manifest = BodyManifest(
        boundary=BodyBoundary.UPSTREAM_RESPONSE,
        chunks=tuple(
            CapturedChunk(
                sequence=index,
                blob_digest=item.digest,
                byte_count=item.byte_count,
                occurred_at_utc=start_time + timedelta(microseconds=index),
                monotonic_offset_ns=index,
            )
            for index, item in enumerate(committed)
        ),
        completeness=BodyCompleteness.complete(),
    )
    assert validate_manifest(manifest, frozenset(item.digest for item in committed)) == ManifestValid()
    loaded = tuple(pool.load(chunk.blob_digest) for chunk in manifest.chunks)
    assert all(isinstance(item, BlobFound) for item in loaded)
    assert b"".join(item.content for item in loaded if isinstance(item, BlobFound)) == payload
    connection.close()
