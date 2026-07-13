"""Downstream SSE keepalive: frame type + two-phase keepalive strategies.

Keepalive frames are injected during upstream idle to reset the downstream
client's inactivity/read timer. The baseline frame is an SSE comment
(``: ping``), ignored by every compliant SSE parser (Anthropic & OpenAI SDKs
included) and safe at any point in the stream. For Anthropic once
``message_start`` has been seen we additionally emit a native ``event: ping``
(protocol-level defense-in-depth; Anthropic's own streams ping after
message_start).

State (whether ``message_start`` has been seen) is threaded through the
combinator loop as a local, not stored as mutable strategy state — the
strategy objects are frozen and pure.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from enum import Enum
from typing import AsyncGenerator, Optional

from typing_extensions import assert_never

SSEFrame = str | bytes

KEEPALIVE_COMMENT = ": ping\n\n"
ANTHROPIC_PING_EVENT = 'event: ping\ndata: {"type": "ping"}\n\n'


def _as_text(frame: SSEFrame) -> str:
    if isinstance(frame, (bytes, bytearray)):
        return bytes(frame).decode("utf-8", errors="replace")
    return frame


def frame_is_anthropic_message_start(frame: SSEFrame) -> bool:
    """True iff the frame carries an ``event: message_start`` line.

    Matches the SSE ``event:`` field only, so ``message_start`` appearing
    inside a ``data:`` JSON payload does not false-positive.
    """
    text = _as_text(frame)
    return any(line.strip() == "event: message_start" for line in text.split("\n"))


@dataclass(frozen=True, slots=True)
class KeepaliveStrategy:
    """Comment-only baseline; subclasses may add protocol-native idle frames."""

    def idle_frames(self, seen_message_start: bool) -> tuple[str, ...]:
        return (KEEPALIVE_COMMENT,)

    def observe_advances_to_phase2(self, frame: SSEFrame) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class CommentOnlyKeepaliveStrategy(KeepaliveStrategy):
    """OpenAI chat / responses: no native heartbeat event, comment only."""


@dataclass(frozen=True, slots=True)
class AnthropicKeepaliveStrategy(KeepaliveStrategy):
    """Comment before message_start; comment + native ping after."""

    def idle_frames(self, seen_message_start: bool) -> tuple[str, ...]:
        if seen_message_start:
            return (KEEPALIVE_COMMENT, ANTHROPIC_PING_EVENT)
        return (KEEPALIVE_COMMENT,)

    def observe_advances_to_phase2(self, frame: SSEFrame) -> bool:
        return frame_is_anthropic_message_start(frame)


class StreamLease:
    """Sole, idempotent response-level owner of the streaming resources.

    Holds the pending ``__anext__`` task (if any) and the innermost producer
    generator. A single ``close()`` — safe to call concurrently and more than
    once — cancels the pending task, awaits it (shielded) so the cancellation
    reaches the producer's disconnect/refund path exactly once, then closes the
    producer. The mandatory order (cancel -> await -> aclose) avoids
    ``RuntimeError: aclose(): asynchronous generator is already running`` that
    would arise from closing a producer whose ``__anext__`` is still in flight.
    """

    def __init__(
        self,
        inner: "AsyncGenerator[SSEFrame, None]",
        pending_task: "Optional[asyncio.Task[SSEFrame]]" = None,
    ) -> None:
        self._inner = inner
        self._pending_task = pending_task
        self._closed = False

    def set_pending_task(self, task: "asyncio.Task[SSEFrame]") -> None:
        self._pending_task = task

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True  # set before any await so a concurrent close() short-circuits
        task = self._pending_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.shield(task)
            except BaseException:  # noqa: BLE001 - swallow cancellation/errors; goal is delivery, not result
                pass
        try:
            await self._inner.aclose()
        except BaseException:  # noqa: BLE001
            pass


async def sse_keepalive(
    real_frames: "AsyncGenerator[SSEFrame, None]",
    strategy: KeepaliveStrategy,
    interval: float,
    lease: StreamLease,
) -> "AsyncGenerator[SSEFrame, None]":
    """Forward ``real_frames``, injecting keepalive frames during idle gaps.

    Each ``__anext__`` runs as a persistent task (handed to ``lease`` so a
    downstream disconnect can cancel it). While that task is pending we race it
    against ``interval`` with ``asyncio.wait`` — never ``wait_for``, which would
    cancel the in-flight ``__anext__`` and tear down the upstream read. On idle
    we emit ``strategy.idle_frames`` and keep waiting on the *same* task; a new
    task is only created once the current one resolves. Same-tick priority:
    when the task is already done we consume the real frame rather than inject a
    ping. Anthropic phase advances only after a ``message_start`` frame is
    forwarded (the frame itself carries no synchronous ping).
    """
    seen_message_start = False
    while True:
        task: "asyncio.Task[SSEFrame]" = asyncio.ensure_future(real_frames.__anext__())
        lease.set_pending_task(task)
        while True:
            done, _pending = await asyncio.wait({task}, timeout=interval)
            if done:
                break
            for frame in strategy.idle_frames(seen_message_start):
                yield frame
        try:
            frame = task.result()
        except StopAsyncIteration:
            return
        yield frame
        if not seen_message_start and strategy.observe_advances_to_phase2(frame):
            seen_message_start = True


class DownstreamSSESurface(str, Enum):
    """Which downstream SSE wire format a streaming response speaks."""

    ANTHROPIC = "anthropic"
    OPENAI_CHAT = "openai_chat"
    OPENAI_RESPONSES = "openai_responses"


def strategy_for(surface: DownstreamSSESurface) -> KeepaliveStrategy:
    match surface:
        case DownstreamSSESurface.ANTHROPIC:
            return AnthropicKeepaliveStrategy()
        case DownstreamSSESurface.OPENAI_CHAT | DownstreamSSESurface.OPENAI_RESPONSES:
            return CommentOnlyKeepaliveStrategy()
    assert_never(surface)


def needs_frame_normalizer(surface: DownstreamSSESurface) -> bool:
    """Only the Anthropic native passthrough forwards raw bytes needing framing."""
    return surface is DownstreamSSESurface.ANTHROPIC


def committed_error_frame(surface: DownstreamSSESurface, error_obj: dict) -> str:
    """Serialize an error as a client-recognizable SSE frame for a committed stream.

    Once the 200 + headers have been sent (slow-commit / mid-stream), an error
    can no longer become a JSON response — it must ride the SSE channel in a form
    the downstream SDK actually surfaces. The Anthropic SDK only raises on
    ``event: error`` frames whose payload ``type == "error"``; a bare
    ``data: {"error": ...}`` frame is silently ignored. OpenAI chat / responses
    SDKs recognize the bare ``data: {"error": ...}`` form.
    """
    match surface:
        case DownstreamSSESurface.ANTHROPIC:
            payload = json.dumps({"type": "error", "error": error_obj})
            return f"event: error\ndata: {payload}\n\n"
        case DownstreamSSESurface.OPENAI_CHAT | DownstreamSSESurface.OPENAI_RESPONSES:
            return f'data: {json.dumps({"error": error_obj})}\n\n'
    assert_never(surface)
