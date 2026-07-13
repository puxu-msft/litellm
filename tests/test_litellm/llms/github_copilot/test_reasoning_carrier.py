"""Tests for the GitHub Copilot reasoning carrier codec.

Pure round-trip / classification tests for encoding a Responses reasoning item
into an Anthropic thinking/redacted_thinking carrier block and decoding it back.
No litellm request/response machinery involved.
"""
from __future__ import annotations

import dataclasses

from litellm.llms.github_copilot.reasoning_carrier import (
    ReasoningReplayEnvelope,
    DecodedCarrier,
    NotOurCarrier,
    InvalidCarrier,
    UnsupportedCarrierVersion,
)


def test_envelope_is_frozen_and_holds_fields():
    env = ReasoningReplayEnvelope(
        reasoning_item_id="rs_abc",
        encrypted_content="ENC==",
        summary_parts=("step one", "step two"),
        origin_model="gpt-5.6-sol",
    )
    assert env.reasoning_item_id == "rs_abc"
    assert env.encrypted_content == "ENC=="
    assert env.summary_parts == ("step one", "step two")
    assert env.version == 1
    try:
        env.encrypted_content = "x"  # type: ignore[misc]
        assert False, "should be frozen"
    except dataclasses.FrozenInstanceError:
        pass
