from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TypeAlias

from litellm.proxy.observability.terminal.events import BodyBoundary


class BodyIncompleteReason(StrEnum):
    OVERFLOW = "overflow"
    CRASH_TAIL = "crash_tail"
    SPOOL_FAILURE = "spool_failure"


@dataclass(frozen=True, slots=True)
class BodyCompleteness:
    is_complete: bool
    reason: BodyIncompleteReason | None

    def __post_init__(self) -> None:
        if self.is_complete == (self.reason is not None):
            raise ValueError("complete bodies have no reason; incomplete bodies require one")

    @classmethod
    def complete(cls) -> BodyCompleteness:
        return cls(True, None)

    @classmethod
    def incomplete(cls, reason: BodyIncompleteReason) -> BodyCompleteness:
        return cls(False, reason)


@dataclass(frozen=True, slots=True)
class CapturedChunk:
    sequence: int
    blob_digest: str
    byte_count: int
    occurred_at_utc: datetime
    monotonic_offset_ns: int

    def __post_init__(self) -> None:
        if self.sequence < 0 or self.byte_count < 0 or self.monotonic_offset_ns < 0:
            raise ValueError("chunk sequence, byte count and monotonic offset must be non-negative")
        if self.occurred_at_utc.utcoffset() != timedelta(0):
            raise ValueError("occurred_at_utc must use UTC")
        if not self.blob_digest.startswith("b3:"):
            raise ValueError("blob_digest must use the b3 namespace")


@dataclass(frozen=True, slots=True)
class BodyManifest:
    boundary: BodyBoundary
    chunks: tuple[CapturedChunk, ...]
    completeness: BodyCompleteness

    def __post_init__(self) -> None:
        sequences = tuple(chunk.sequence for chunk in self.chunks)
        if sequences != tuple(range(len(self.chunks))):
            raise ValueError("body chunk sequence must be contiguous from zero")
        offsets = tuple(chunk.monotonic_offset_ns for chunk in self.chunks)
        if offsets != tuple(sorted(offsets)):
            raise ValueError("body chunk monotonic offsets must not decrease")

    @property
    def byte_count(self) -> int:
        return sum(chunk.byte_count for chunk in self.chunks)


@dataclass(frozen=True, slots=True)
class ManifestValid:
    pass


@dataclass(frozen=True, slots=True)
class ManifestInvalid:
    missing_digests: tuple[str, ...]


ManifestValidation: TypeAlias = ManifestValid | ManifestInvalid


def validate_manifest(manifest: BodyManifest, available_digests: frozenset[str]) -> ManifestValidation:
    missing = tuple(chunk.blob_digest for chunk in manifest.chunks if chunk.blob_digest not in available_digests)
    return ManifestValid() if not missing else ManifestInvalid(missing)
