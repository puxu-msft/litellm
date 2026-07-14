"""Tests for the GitHub Copilot reasoning config resolver."""
from __future__ import annotations

from litellm.llms.github_copilot.reasoning_config import (
    ResolvedReasoningConfig,
    InvalidReasoningConfig,
    resolve_reasoning_config,
    summary_wire_value,
)


def test_no_model_info_defaults():
    r = resolve_reasoning_config(None)
    assert r == ResolvedReasoningConfig(carrier="signature", summary="auto")


def test_missing_key_defaults():
    r = resolve_reasoning_config({"mode": "responses"})
    assert isinstance(r, ResolvedReasoningConfig)
    assert (r.carrier, r.summary) == ("signature", "auto")


def test_valid_config_resolved():
    r = resolve_reasoning_config({"github_copilot_reasoning": {"carrier": "redacted_thinking", "summary": "detailed"}})
    assert r == ResolvedReasoningConfig(carrier="redacted_thinking", summary="detailed")


def test_partial_config_fills_defaults():
    r = resolve_reasoning_config({"github_copilot_reasoning": {"summary": "off"}})
    assert r == ResolvedReasoningConfig(carrier="signature", summary="off")


def test_unknown_carrier_fails_loud():
    r = resolve_reasoning_config({"github_copilot_reasoning": {"carrier": "typo"}})
    assert isinstance(r, InvalidReasoningConfig)
    assert "carrier" in r.reason


def test_unknown_summary_fails_loud():
    r = resolve_reasoning_config({"github_copilot_reasoning": {"summary": "verbose"}})
    assert isinstance(r, InvalidReasoningConfig)
    assert "summary" in r.reason


def test_non_mapping_raw_invalid():
    r = resolve_reasoning_config({"github_copilot_reasoning": "signature"})
    assert isinstance(r, InvalidReasoningConfig)


def test_summary_wire_value_off_is_omitted():
    assert summary_wire_value("off") is None
    assert summary_wire_value("auto") == "auto"
    assert summary_wire_value("concise") == "concise"
    assert summary_wire_value("detailed") == "detailed"
