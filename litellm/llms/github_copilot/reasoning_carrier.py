"""GitHub Copilot reasoning carrier codec.

gpt-* Copilot models run via the Responses API; their reasoning state is an
opaque ``encrypted_content`` plus an optional summary. Claude Code only speaks
Anthropic Messages, so to round-trip that reasoning across turns we serialize a
``ReasoningReplayEnvelope`` (item id + encrypted_content + summary) into an
Anthropic thinking/redacted_thinking carrier block and decode it back on the
next request.

This module is pure: no litellm request/response machinery. Failures are modeled
as values via the ``DecodeResult`` tagged union rather than raised.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Literal, Union

_NS = "ghc-rsn"
_VERSION = 1

_Carrier = Literal["signature", "redacted_thinking"]


@dataclass(frozen=True, slots=True)
class ReasoningReplayEnvelope:
    reasoning_item_id: str
    encrypted_content: str
    summary_parts: tuple[str, ...]
    origin_model: Union[str, None] = None
    version: int = _VERSION


@dataclass(frozen=True, slots=True)
class DecodedCarrier:
    envelope: ReasoningReplayEnvelope


@dataclass(frozen=True, slots=True)
class NotOurCarrier:
    pass


@dataclass(frozen=True, slots=True)
class InvalidCarrier:
    reason: str


@dataclass(frozen=True, slots=True)
class UnsupportedCarrierVersion:
    version: int


DecodeResult = Union[DecodedCarrier, NotOurCarrier, InvalidCarrier, UnsupportedCarrierVersion]


def _serialize(env: ReasoningReplayEnvelope) -> str:
    payload = {
        "id": env.reasoning_item_id,
        "ec": env.encrypted_content,
        "sp": list(env.summary_parts),
        "om": env.origin_model,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).decode("ascii")
    return f"{_NS}:v{env.version}:{b64}"


def _summary_text(env: ReasoningReplayEnvelope) -> str:
    return " ".join(env.summary_parts).strip()


def encode_carrier(env: ReasoningReplayEnvelope, carrier: _Carrier) -> tuple[dict, ...]:
    """Encode a reasoning envelope into Anthropic carrier block(s).

    ``signature`` -> a single ``thinking`` block whose ``signature`` carries the
    serialized envelope. ``redacted_thinking`` -> a ``redacted_thinking`` block
    carrying the envelope in ``data``, preceded by a display ``thinking`` block
    when a summary is present (two independent content blocks).
    """
    token = _serialize(env)
    summary = _summary_text(env)
    if carrier == "signature":
        return ({"type": "thinking", "thinking": summary, "signature": token},)
    summary_block = ({"type": "thinking", "thinking": summary},) if summary else ()
    return (*summary_block, {"type": "redacted_thinking", "data": token})
