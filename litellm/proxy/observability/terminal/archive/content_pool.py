from __future__ import annotations

import io
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TypeAlias

import blake3
import zstandard
from pydantic import TypeAdapter

_BLOB_ROW: TypeAdapter[tuple[bytes, int] | None] = TypeAdapter(tuple[bytes, int] | None)


@dataclass(frozen=True, slots=True)
class StoreBlobCommitted:
    digest: str
    byte_count: int
    compressed: bytes
    inserted: bool


@dataclass(frozen=True, slots=True)
class StoreBlobFailed:
    detail: str


StoreBlobResult: TypeAlias = StoreBlobCommitted | StoreBlobFailed


@dataclass(frozen=True, slots=True)
class BlobFound:
    digest: str
    content: bytes


@dataclass(frozen=True, slots=True)
class BlobMissing:
    digest: str


@dataclass(frozen=True, slots=True)
class BlobCorrupt:
    digest: str
    detail: str


BlobLoadResult: TypeAlias = BlobFound | BlobMissing | BlobCorrupt


class ContentPool:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @classmethod
    def initialize(cls, connection: sqlite3.Connection) -> ContentPool:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS content_blobs ("
            "digest TEXT PRIMARY KEY, byte_count INTEGER NOT NULL CHECK(byte_count >= 0), "
            "compressed BLOB NOT NULL)"
        )
        return cls(connection)

    def store_stream(self, chunks: Iterable[bytes]) -> StoreBlobResult:
        hasher = blake3.blake3()
        output = io.BytesIO()
        byte_count = 0
        try:
            with zstandard.ZstdCompressor().stream_writer(output, closefd=False) as writer:
                for chunk in chunks:
                    hasher.update(chunk)
                    writer.write(chunk)
                    byte_count += len(chunk)
            compressed = output.getvalue()
            digest = f"b3:{hasher.hexdigest()}"
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO content_blobs(digest, byte_count, compressed) VALUES (?, ?, ?)",
                (digest, byte_count, compressed),
            )
            return StoreBlobCommitted(digest, byte_count, compressed, cursor.rowcount == 1)
        except (OSError, sqlite3.Error, zstandard.ZstdError) as exception:
            return StoreBlobFailed(str(exception))

    def load(self, digest: str) -> BlobLoadResult:
        try:
            row = _BLOB_ROW.validate_python(
                self._connection.execute(
                    "SELECT compressed, byte_count FROM content_blobs WHERE digest=?", (digest,)
                ).fetchone()
            )
            if row is None:
                return BlobMissing(digest)
            compressed, byte_count = row
            content = zstandard.ZstdDecompressor().decompress(compressed, max_output_size=byte_count)
            actual_digest = f"b3:{blake3.blake3(content).hexdigest()}"
            if len(content) != byte_count or actual_digest != digest:
                return BlobCorrupt(digest, "decompressed content does not match stored metadata")
            return BlobFound(digest, content)
        except (sqlite3.Error, zstandard.ZstdError) as exception:
            return BlobCorrupt(digest, str(exception))
