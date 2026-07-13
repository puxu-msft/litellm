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
    encode_carrier,
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


def _env(summary=("s1", "s2")):
    return ReasoningReplayEnvelope("rs_1", "ENC==", summary, "gpt-5.6-sol")


def test_encode_signature_single_block():
    blocks = encode_carrier(_env(), "signature")
    assert len(blocks) == 1
    b = blocks[0]
    assert b["type"] == "thinking"
    assert b["thinking"] == "s1 s2"
    assert b["signature"].startswith("ghc-rsn:v1:")


def test_encode_redacted_with_summary_two_blocks_in_order():
    blocks = encode_carrier(_env(), "redacted_thinking")
    assert [b["type"] for b in blocks] == ["thinking", "redacted_thinking"]
    assert blocks[0]["thinking"] == "s1 s2"
    assert blocks[1]["data"].startswith("ghc-rsn:v1:")
    assert "signature" not in blocks[1]


def test_encode_redacted_without_summary_single_redacted_block():
    blocks = encode_carrier(_env(summary=()), "redacted_thinking")
    assert [b["type"] for b in blocks] == ["redacted_thinking"]
    assert blocks[0]["data"].startswith("ghc-rsn:v1:")
