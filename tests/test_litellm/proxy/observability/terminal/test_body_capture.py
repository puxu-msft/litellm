from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from litellm.proxy.observability.terminal.capture.body import (
    BodyCompleteness,
    BodyIncompleteReason,
    BodyManifest,
    CapturedChunk,
    ManifestInvalid,
    ManifestValid,
    validate_manifest,
)
from litellm.proxy.observability.terminal.events import BodyBoundary


START = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


def _chunk(sequence: int, digest: str | None = None) -> CapturedChunk:
    return CapturedChunk(
        sequence=sequence,
        blob_digest=digest or f"b3:{sequence:064x}",
        byte_count=sequence + 1,
        occurred_at_utc=START + timedelta(milliseconds=sequence),
        monotonic_offset_ns=sequence * 1_000_000,
    )


def test_complete_manifest_is_valid_when_all_blobs_exist() -> None:
    chunks = (_chunk(0), _chunk(1), _chunk(2))
    manifest = BodyManifest(
        boundary=BodyBoundary.UPSTREAM_RESPONSE,
        chunks=chunks,
        completeness=BodyCompleteness.complete(),
    )
    assert manifest.byte_count == 6
    assert validate_manifest(manifest, frozenset(chunk.blob_digest for chunk in chunks)) == ManifestValid()


@pytest.mark.parametrize(
    "reason",
    (
        BodyIncompleteReason.OVERFLOW,
        BodyIncompleteReason.CRASH_TAIL,
        BodyIncompleteReason.SPOOL_FAILURE,
    ),
)
def test_incomplete_reasons_are_explicit(reason: BodyIncompleteReason) -> None:
    manifest = BodyManifest(
        boundary=BodyBoundary.CLIENT_RESPONSE,
        chunks=(_chunk(0),),
        completeness=BodyCompleteness.incomplete(reason),
    )
    assert manifest.completeness.reason is reason


def test_completeness_rejects_inconsistent_reason() -> None:
    with pytest.raises(ValueError, match="reason"):
        BodyCompleteness(True, BodyIncompleteReason.OVERFLOW)
    with pytest.raises(ValueError, match="reason"):
        BodyCompleteness(False, None)


def test_manifest_rejects_non_contiguous_sequence() -> None:
    with pytest.raises(ValueError, match="contiguous"):
        BodyManifest(
            boundary=BodyBoundary.UPSTREAM_RESPONSE,
            chunks=(_chunk(0), _chunk(2)),
            completeness=BodyCompleteness.complete(),
        )


def test_manifest_rejects_non_monotonic_time() -> None:
    later = _chunk(1)
    earlier = CapturedChunk(
        sequence=2,
        blob_digest=f"b3:{2:064x}",
        byte_count=3,
        occurred_at_utc=START,
        monotonic_offset_ns=0,
    )
    with pytest.raises(ValueError, match="monotonic"):
        BodyManifest(
            boundary=BodyBoundary.UPSTREAM_RESPONSE,
            chunks=(_chunk(0), later, earlier),
            completeness=BodyCompleteness.complete(),
        )


def test_manifest_allows_wall_clock_rollback_when_monotonic_offset_increases() -> None:
    first = _chunk(0)
    second = CapturedChunk(
        sequence=1,
        blob_digest=f"b3:{1:064x}",
        byte_count=2,
        occurred_at_utc=START - timedelta(seconds=1),
        monotonic_offset_ns=1,
    )
    manifest = BodyManifest(
        boundary=BodyBoundary.UPSTREAM_RESPONSE,
        chunks=(first, second),
        completeness=BodyCompleteness.complete(),
    )
    assert manifest.chunks == (first, second)


def test_complete_manifest_with_missing_blob_is_invalid() -> None:
    chunks = (_chunk(0), _chunk(1))
    manifest = BodyManifest(
        boundary=BodyBoundary.UPSTREAM_RESPONSE,
        chunks=chunks,
        completeness=BodyCompleteness.complete(),
    )
    result = validate_manifest(manifest, frozenset({chunks[0].blob_digest}))
    assert result == ManifestInvalid(missing_digests=(chunks[1].blob_digest,))


def test_chunk_requires_utc_and_non_negative_values() -> None:
    with pytest.raises(ValueError, match="UTC"):
        CapturedChunk(0, "b3:" + "0" * 64, 1, datetime(2026, 7, 18), 0)
    with pytest.raises(ValueError, match="non-negative"):
        CapturedChunk(-1, "b3:" + "0" * 64, 1, START, 0)
    with pytest.raises(ValueError, match="non-negative"):
        CapturedChunk(0, "b3:" + "0" * 64, -1, START, 0)
