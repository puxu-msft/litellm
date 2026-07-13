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
        "",
        "sig",
        "EqoBabc==",
        base64.urlsafe_b64encode(b"{}").decode(),
        "ghc-rsn",
        "ghc-rsn:",
        "ghc-rsn:v1",
        "ghc-rsn:v1:",
        "ghc-rsn:vX:YWJj",
        "ghc-rsn:v1:" + base64.urlsafe_b64encode(b"[1,2,3]").decode(),
    ]
    rng = random.Random(1234)
    corpus = seeds + ["".join(rng.choice(string.printable) for _ in range(rng.randint(0, 40))) for _ in range(200)]
    for s in corpus:
        for block in (
            {"type": "thinking", "thinking": "", "signature": s},
            {"type": "redacted_thinking", "data": s},
        ):
            res = decode_carrier(block)  # must not raise
            assert not isinstance(res, DecodedCarrier) or s.startswith("ghc-rsn:v1:")


# ---- strict decode-boundary tests (adversarial code review findings) ----


def _tok(payload: dict) -> str:
    import base64
    import json

    b = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    return f"ghc-rsn:v1:{b}"


def _sig(payload: dict) -> dict:
    return {"type": "thinking", "thinking": "", "signature": _tok(payload)}


def test_sp_non_string_elements_rejected():
    assert isinstance(decode_carrier(_sig({"id": "a", "ec": "e", "sp": [1, 2]})), InvalidCarrier)


def test_sp_as_string_rejected_not_split_into_chars():
    assert isinstance(decode_carrier(_sig({"id": "a", "ec": "e", "sp": "abc"})), InvalidCarrier)


def test_origin_model_non_string_rejected():
    assert isinstance(decode_carrier(_sig({"id": "a", "ec": "e", "sp": [], "om": 123})), InvalidCarrier)


def test_missing_sp_rejected():
    assert isinstance(decode_carrier(_sig({"id": "a", "ec": "e"})), InvalidCarrier)


def test_empty_id_or_ec_rejected():
    assert isinstance(decode_carrier(_sig({"id": "", "ec": "e", "sp": []})), InvalidCarrier)
    assert isinstance(decode_carrier(_sig({"id": "a", "ec": "", "sp": []})), InvalidCarrier)


def test_extra_keys_rejected():
    assert isinstance(decode_carrier(_sig({"id": "a", "ec": "e", "sp": [], "x": 1})), InvalidCarrier)


def test_invalid_char_injection_head_mid_tail_all_invalid():
    env = ReasoningReplayEnvelope("rs", "ENC==", ("s",), None)
    (block,) = encode_carrier(env, "signature")
    good = block["signature"]
    ns, ver, body = good.split(":", 2)
    for tampered_body in ("%" + body, body[:8] + "%" + body[8:], body + "%"):
        tampered = {"type": "thinking", "thinking": "", "signature": f"{ns}:{ver}:{tampered_body}"}
        assert isinstance(decode_carrier(tampered), InvalidCarrier), tampered_body


def test_unicode_and_special_chars_roundtrip():
    env = ReasoningReplayEnvelope(
        reasoning_item_id="rs_✓_中文",
        encrypted_content="a:b+c/d=eq\nNUL\x00tail",
        summary_parts=("推理一：先设 x", "line\nbreak", "plus+slash/eq="),
        origin_model="gpt-5.6-sol",
    )
    (block,) = encode_carrier(env, "signature")
    res = decode_carrier(block)
    assert isinstance(res, DecodedCarrier)
    assert res.envelope == env


def test_long_encrypted_content_roundtrip():
    env = ReasoningReplayEnvelope("rs", "E" * 100_000, (), None)
    (block,) = encode_carrier(env, "signature")
    res = decode_carrier(block)
    assert isinstance(res, DecodedCarrier)
    assert res.envelope.encrypted_content == "E" * 100_000


def test_encode_invalid_carrier_selector_fails_loud():
    env = ReasoningReplayEnvelope("rs", "e", (), None)
    import pytest

    with pytest.raises((AssertionError, Exception)):
        encode_carrier(env, "typo")  # type: ignore[arg-type]


def test_prefixed_but_corrupt_is_invalid_not_notourcarrier():
    # a value with our namespace prefix but broken structure is InvalidCarrier, never DecodedCarrier
    for sig in ("ghc-rsn:v1:not@@base64", "ghc-rsn:vX:YWJj", "ghc-rsn:onlytwo"):
        res = decode_carrier({"type": "thinking", "thinking": "", "signature": sig})
        assert not isinstance(res, DecodedCarrier)
        assert not isinstance(res, NotOurCarrier)
