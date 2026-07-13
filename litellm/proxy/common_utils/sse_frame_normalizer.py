"""Bytes-safe SSE frame normalizer for the Anthropic passthrough path.

The Anthropic ``/v1/messages`` github_copilot native path forwards
``httpx.Response.aiter_bytes()`` verbatim — arbitrary byte chunks that may split
mid-line, mid-JSON, or mid-UTF-8. The downstream keepalive layer must only
inject frames at SSE frame boundaries, so this normalizer accumulates raw bytes
and yields one complete frame (up to and including its delimiter) at a time.

It operates purely on ``bytes`` (finding ASCII delimiters), never decoding
per-chunk — a per-chunk ``decode(errors="replace")`` would corrupt a multi-byte
UTF-8 character split across two chunks into replacement characters, breaking
byte-for-byte fidelity with the upstream.

Mirrors the delimiter semantics of ``proxy_server._pop_complete_sse_frame`` (its
str-based sibling used by the OpenAI cost-injection path): pick the delimiter
with the smallest start position, then split after its full length.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

verbose_proxy_logger = logging.getLogger("litellm.proxy")

SSE_FRAME_DELIMITERS: tuple[bytes, ...] = (b"\r\n\r\n", b"\n\n", b"\r\r")
DEFAULT_MAX_UNTERMINATED_BYTES = 8 * 1024 * 1024


def find_frame_delimiter(buf: bytes) -> int:
    """Return the exclusive end index of the first complete frame, or -1.

    Chooses the delimiter whose start position is smallest, then returns
    ``start + len(delimiter)`` so the returned boundary never falls inside a
    multi-byte delimiter (e.g. ``\\r\\n\\r\\n``).
    """
    candidates = tuple(
        (idx, len(delimiter)) for delimiter in SSE_FRAME_DELIMITERS if (idx := buf.find(delimiter)) != -1
    )
    if not candidates:
        return -1
    start, delimiter_len = min(candidates, key=lambda item: item[0])
    return start + delimiter_len


async def normalize_anthropic_sse_frames(
    byte_iter: AsyncIterator[bytes],
    max_unterminated_bytes: int = DEFAULT_MAX_UNTERMINATED_BYTES,
) -> AsyncIterator[bytes]:
    """Yield complete SSE frames from a raw byte stream.

    A non-empty unterminated remainder at EOF is yielded as-is (preserving the
    proxy's historical verbatim forwarding) and logged at debug. Exceeding
    ``max_unterminated_bytes`` without a delimiter raises ``ValueError``.
    """
    buffer = b""
    async for chunk in byte_iter:
        buffer += chunk
        end = find_frame_delimiter(buffer)
        while end != -1:
            yield buffer[:end]
            buffer = buffer[end:]
            end = find_frame_delimiter(buffer)
        if len(buffer) > max_unterminated_bytes:
            raise ValueError(f"SSE frame exceeded {max_unterminated_bytes} bytes without a delimiter")
    if buffer:
        verbose_proxy_logger.debug(
            "normalize_anthropic_sse_frames: flushing %d trailing bytes without delimiter at EOF",
            len(buffer),
        )
        yield buffer
