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

from dataclasses import dataclass

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
