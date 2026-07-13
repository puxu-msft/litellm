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
    decode_carrier,
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


def test_roundtrip_signature():
    env = ReasoningReplayEnvelope("rs_1", "ENC==", ("s1", "s2"), "gpt-5.6-sol")
    (block,) = encode_carrier(env, "signature")
    res = decode_carrier(block)
    assert isinstance(res, DecodedCarrier)
    assert res.envelope == env


def test_roundtrip_redacted():
    env = ReasoningReplayEnvelope("rs_2", "ENC2", (), None)
    blocks = encode_carrier(env, "redacted_thinking")
    res = decode_carrier(blocks[-1])
    assert isinstance(res, DecodedCarrier)
    assert res.envelope == env


def test_real_claude_signature_is_not_our_carrier():
    block = {"type": "thinking", "thinking": "x", "signature": "EqoBCkYIB..."}
    assert isinstance(decode_carrier(block), NotOurCarrier)


def test_random_string_not_our_carrier():
    assert isinstance(decode_carrier({"type": "redacted_thinking", "data": "just-random"}), NotOurCarrier)


def test_corrupt_base64_is_invalid():
    block = {"type": "thinking", "thinking": "", "signature": "ghc-rsn:v1:!!!notb64!!!"}
    assert isinstance(decode_carrier(block), InvalidCarrier)


def test_unknown_version():
    block = {"type": "thinking", "thinking": "", "signature": "ghc-rsn:v9:YWJj"}
    assert isinstance(decode_carrier(block), UnsupportedCarrierVersion)


def test_missing_required_field_is_invalid():
    import base64
    import json

    b64 = base64.urlsafe_b64encode(json.dumps({"ec": "E", "sp": []}).encode()).decode()
    block = {"type": "thinking", "thinking": "", "signature": f"ghc-rsn:v1:{b64}"}
    assert isinstance(decode_carrier(block), InvalidCarrier)


def test_never_decodes_non_envelope_and_never_raises():
    import base64
    import random
    import string

    seeds = [
        "", "sig", "EqoBabc==", base64.urlsafe_b64encode(b"{}").decode(),
        "ghc-rsn", "ghc-rsn:", "ghc-rsn:v1", "ghc-rsn:v1:", "ghc-rsn:vX:YWJj",
        "ghc-rsn:v1:" + base64.urlsafe_b64encode(b"[1,2,3]").decode(),
    ]
    rng = random.Random(1234)
    corpus = seeds + [
        "".join(rng.choice(string.printable) for _ in range(rng.randint(0, 40)))
        for _ in range(200)
    ]
    for s in corpus:
        for block in (
            {"type": "thinking", "thinking": "", "signature": s},
            {"type": "redacted_thinking", "data": s},
        ):
            res = decode_carrier(block)  # must not raise
            assert not isinstance(res, DecodedCarrier) or s.startswith("ghc-rsn:v1:")
