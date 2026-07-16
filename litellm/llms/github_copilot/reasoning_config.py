"""Resolved reasoning config for the GitHub Copilot gpt reasoning<->thinking bridge.

Deployment config lives in ``model_info.github_copilot_reasoning: {carrier, summary}``
(metadata, never forwarded to the provider request body). This module resolves it
into a frozen, typed ``ResolvedReasoningConfig`` and fails loud (as a value) on
unknown values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Union, cast

Carrier = Literal["signature", "redacted_thinking"]
Summary = Literal["off", "auto", "concise", "detailed"]

_DEFAULT_CARRIER: Carrier = "signature"
_DEFAULT_SUMMARY: Summary = "auto"

_VALID_CARRIERS = ("signature", "redacted_thinking")
_VALID_SUMMARIES = ("off", "auto", "concise", "detailed")


@dataclass(frozen=True, slots=True)
class ResolvedReasoningConfig:
    carrier: Carrier = _DEFAULT_CARRIER
    summary: Summary = _DEFAULT_SUMMARY


@dataclass(frozen=True, slots=True)
class InvalidReasoningConfig:
    reason: str


ResolveResult = Union[ResolvedReasoningConfig, InvalidReasoningConfig]


def resolve_reasoning_config(model_info: Union[Mapping[str, object], None]) -> ResolveResult:
    """Resolve deployment ``model_info`` into a reasoning config.

    Missing/absent config -> defaults (carrier=signature, summary=auto). Unknown
    ``carrier``/``summary`` values -> ``InvalidReasoningConfig`` (fail loud). A
    non-mapping ``github_copilot_reasoning`` -> ``InvalidReasoningConfig``.
    """
    if not model_info:
        return ResolvedReasoningConfig()
    raw = model_info.get("github_copilot_reasoning")
    if raw is None:
        return ResolvedReasoningConfig()
    if not isinstance(raw, Mapping):
        return InvalidReasoningConfig("github_copilot_reasoning-not-a-mapping")
    raw_map = cast("Mapping[str, object]", raw)  # cast-ok: config mapping narrowing
    carrier = raw_map.get("carrier", _DEFAULT_CARRIER)
    summary = raw_map.get("summary", _DEFAULT_SUMMARY)
    if carrier not in _VALID_CARRIERS:
        return InvalidReasoningConfig(f"unknown-carrier:{carrier!r}")
    if summary not in _VALID_SUMMARIES:
        return InvalidReasoningConfig(f"unknown-summary:{summary!r}")
    return ResolvedReasoningConfig(carrier=carrier, summary=summary)


def summary_wire_value(summary: Summary) -> Union[str, None]:
    """Map a deployment ``summary`` to the Responses ``reasoning.summary`` wire value.

    ``off`` -> None (omit the field); ``auto``/``concise``/``detailed`` -> as-is.
    """
    return None if summary == "off" else summary


def reasoning_bridge_enabled() -> bool:
    """Whether the gpt reasoning<->thinking carrier bridge is active.

    On by default (full fidelity is the goal); set ``GHC_REASONING_DISABLE=1`` as a
    kill switch. Per-deployment carrier/summary selection is separate
    (``resolve_reasoning_config``).
    """
    import os

    return os.environ.get("GHC_REASONING_DISABLE") != "1"
