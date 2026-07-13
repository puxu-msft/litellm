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

from dataclasses import dataclass
from typing import Union

_NS = "ghc-rsn"
_VERSION = 1


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
