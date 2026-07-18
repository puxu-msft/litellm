from __future__ import annotations

from dataclasses import dataclass

from litellm.proxy.observability.terminal.archive.content_pool import BlobFound, ContentPool
from litellm.proxy.observability.terminal.capture.body import BodyManifest


@dataclass(frozen=True, slots=True)
class BodyReconstructed:
    content: bytes


@dataclass(frozen=True, slots=True)
class BodyReconstructionFailed:
    digest: str


def reconstruct_body(pool: ContentPool, manifest: BodyManifest) -> BodyReconstructed | BodyReconstructionFailed:
    chunks: tuple[bytes, ...] = ()
    for chunk in manifest.chunks:
        loaded = pool.load(chunk.blob_digest)
        if not isinstance(loaded, BlobFound):
            return BodyReconstructionFailed(chunk.blob_digest)
        chunks = (*chunks, loaded.content)
    return BodyReconstructed(b"".join(chunks))
