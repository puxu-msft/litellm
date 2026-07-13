"""GitHub Copilot reasoning carrier codec.

gpt-* Copilot models run via the Responses API; their reasoning state is an
opaque ``encrypted_content`` plus an optional summary. Claude Code only speaks
Anthropic Messages, so to round-trip that reasoning across turns we serialize a
``ReasoningReplayEnvelope`` (item id + encrypted_content + summary) into an
Anthropic thinking/redacted_thinking carrier block and decode it back on the
next request.

This module is pure: no litellm request/response machinery. Failures are modeled
as values via the ``DecodeResult`` tagged union rather than raised. The decode
boundary validates the wire payload with a strict Pydantic model, so a
``DecodedCarrier`` is a trusted, fully-typed envelope.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Annotated, Literal, Mapping, Union

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError
from typing_extensions import assert_never

_NS = "ghc-rsn"
_VERSION = 1

_Carrier = Literal["signature", "redacted_thinking"]

# non-empty strict string: rejects non-str and empty at the decode boundary
_NonEmptyStr = Annotated[StrictStr, Field(min_length=1)]


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


class _CarrierPayload(BaseModel):
    """Strict wire payload validated at the decode boundary.

    ``extra="forbid"`` rejects unknown keys; ``StrictStr`` rejects non-string
    values (so ``sp=[1]`` / ``om=123`` fail); ``sp`` is required (missing fails)
    but may be an empty list; ``id``/``ec`` must be non-empty.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: _NonEmptyStr
    ec: _NonEmptyStr
    sp: list[StrictStr]
    om: Union[StrictStr, None] = None


def serialize_envelope(env: ReasoningReplayEnvelope) -> str:
    """Serialize an envelope into the ``ghc-rsn:v<N>:<b64url(json)>`` carrier token."""
    payload = {
        "id": env.reasoning_item_id,
        "ec": env.encrypted_content,
        "sp": list(env.summary_parts),
        "om": env.origin_model,
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).decode("ascii")
    return f"{_NS}:v{env.version}:{b64}"


def _summary_text(env: ReasoningReplayEnvelope) -> str:
    return " ".join(env.summary_parts).strip()


def encode_carrier(env: ReasoningReplayEnvelope, carrier: _Carrier) -> tuple[dict[str, object], ...]:
    """Encode a reasoning envelope into Anthropic carrier block(s).

    ``signature`` -> a single ``thinking`` block whose ``signature`` carries the
    serialized envelope. ``redacted_thinking`` -> a ``redacted_thinking`` block
    carrying the envelope in ``data``, preceded by a display ``thinking`` block
    when a summary is present (two independent content blocks).
    """
    token = serialize_envelope(env)
    summary = _summary_text(env)
    match carrier:
        case "signature":
            return ({"type": "thinking", "thinking": summary, "signature": token},)
        case "redacted_thinking":
            summary_block: tuple[dict[str, object], ...] = (
                ({"type": "thinking", "thinking": summary},) if summary else ()
            )
            return (*summary_block, {"type": "redacted_thinking", "data": token})
        case _:
            assert_never(carrier)


def _carrier_field(block: Mapping[str, object]) -> Union[str, None]:
    block_type = block.get("type")
    if block_type == "thinking":
        sig = block.get("signature")
        return sig if isinstance(sig, str) else None
    if block_type == "redacted_thinking":
        data = block.get("data")
        return data if isinstance(data, str) else None
    return None


def decode_carrier(block: Mapping[str, object]) -> DecodeResult:
    """Decode an Anthropic carrier block back into a reasoning envelope.

    Returns a tagged union: ``NotOurCarrier`` for anything without our namespace
    (including genuine Claude signatures), ``UnsupportedCarrierVersion`` for a
    known-namespace but future version, ``InvalidCarrier`` for corruption or a
    payload that violates the strict schema, and ``DecodedCarrier`` on success.
    Never raises.
    """
    field = _carrier_field(block)
    if field is None or not field.startswith(f"{_NS}:"):
        return NotOurCarrier()
    parts = field.split(":", 2)
    if len(parts) != 3 or not parts[1].startswith("v"):
        return InvalidCarrier("malformed-structure")
    try:
        version = int(parts[1][1:])
    except ValueError:
        return InvalidCarrier("malformed-version")
    if version != _VERSION:
        return UnsupportedCarrierVersion(version)
    # strict base64url: reject any non-alphabet byte (validate=True) so a tampered
    # token cannot decode unchanged.
    try:
        raw = base64.b64decode(parts[2].encode("ascii"), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        return InvalidCarrier("malformed-base64")
    # parse + strict-validate in one step (malformed JSON and schema violations both
    # surface as ValidationError), avoiding an untyped json.loads hop.
    try:
        payload = _CarrierPayload.model_validate_json(raw)
    except ValidationError:
        return InvalidCarrier("schema-invalid")
    return DecodedCarrier(
        ReasoningReplayEnvelope(
            reasoning_item_id=payload.id,
            encrypted_content=payload.ec,
            summary_parts=tuple(payload.sp),
            origin_model=payload.om,
            version=version,
        )
    )
