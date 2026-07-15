"""Group 2: real-backend (billed) differential + continuity for the GHC reasoning bridge.

- A valid carrier replayed as history is accepted (backend verifies the encrypted_content).
- A structurally-valid carrier whose encrypted_content was tampered is rejected -> proves
  the proxy really reconstructs the Responses reasoning item and the backend validates it
  (not merely tolerating an opaque thinking block).
- Two dependent reasoning turns both succeed (cross-turn continuity).

Marked ``billed``; hits copilot and consumes quota. Opt-in via LITELLM_RUN_BILLED=1.
"""
from __future__ import annotations

import pytest

from litellm.llms.github_copilot.reasoning_carrier import (
    ReasoningReplayEnvelope,
    DecodedCarrier,
    decode_carrier,
    serialize_envelope,
)

_PROMPT = "A bat and a ball cost $1.10 total; the bat costs $1.00 more than the ball. How much is the ball? Reason step by step."
_BETA = "interleaved-thinking-2025-05-14"


def _first_carrier_block(content) -> dict:
    for b in content:
        sig = getattr(b, "signature", "") or ""
        if getattr(b, "type", None) == "thinking" and sig.startswith("ghc-rsn:v1:"):
            return {"type": "thinking", "thinking": getattr(b, "thinking", "") or "", "signature": sig}
    raise AssertionError("no ghc-rsn carrier thinking block in response")


def _reason(client, model: str) -> object:
    return client.messages.create(
        model=model,
        max_tokens=2048,
        thinking={"type": "enabled", "budget_tokens": 1500},
        messages=[{"role": "user", "content": _PROMPT}],
        extra_headers={"anthropic-beta": _BETA},
    )


def _tamper_encrypted_content(carrier_block: dict) -> dict:
    res = decode_carrier(carrier_block)
    assert isinstance(res, DecodedCarrier)
    env = res.envelope
    bad = ReasoningReplayEnvelope(
        reasoning_item_id=env.reasoning_item_id,
        encrypted_content="TAMPERED_UNVERIFIABLE_ENCRYPTED_CONTENT" * 8,
        summary_parts=env.summary_parts,
        origin_model=env.origin_model,
    )
    return {**carrier_block, "signature": serialize_envelope(bad)}


@pytest.mark.billed
class TestReasoningBilled:
    def test_valid_carrier_replay_is_accepted(self, anthropic_client, gpt_model):
        carrier = _first_carrier_block(_reason(anthropic_client, gpt_model).content)
        followup = anthropic_client.messages.create(
            model=gpt_model,
            max_tokens=512,
            messages=[
                {"role": "user", "content": _PROMPT},
                {"role": "assistant", "content": [carrier, {"type": "text", "text": "The ball is $0.05."}]},
                {"role": "user", "content": "Are you sure? Recheck briefly."},
            ],
            thinking={"type": "enabled", "budget_tokens": 1024},
            extra_headers={"anthropic-beta": _BETA},
        )
        assert followup.content, "a valid replayed carrier must be accepted by the backend"

    def test_tampered_carrier_is_rejected(self, anthropic_client, gpt_model):
        import anthropic

        carrier = _first_carrier_block(_reason(anthropic_client, gpt_model).content)
        tampered = _tamper_encrypted_content(carrier)
        with pytest.raises(anthropic.APIStatusError):
            anthropic_client.messages.create(
                model=gpt_model,
                max_tokens=512,
                messages=[
                    {"role": "user", "content": _PROMPT},
                    {"role": "assistant", "content": [tampered, {"type": "text", "text": "The ball is $0.05."}]},
                    {"role": "user", "content": "Recheck."},
                ],
                thinking={"type": "enabled", "budget_tokens": 1024},
                extra_headers={"anthropic-beta": _BETA},
            )

    def test_cross_turn_reasoning_continuity(self, anthropic_client, gpt_model):
        first = _reason(anthropic_client, gpt_model)
        carrier = _first_carrier_block(first.content)
        answer_text = "".join(getattr(b, "text", "") or "" for b in first.content if getattr(b, "type", None) == "text")
        second = anthropic_client.messages.create(
            model=gpt_model,
            max_tokens=1024,
            messages=[
                {"role": "user", "content": _PROMPT},
                {"role": "assistant", "content": [carrier, {"type": "text", "text": answer_text or "The ball is $0.05."}]},
                {"role": "user", "content": "Now add a 20% tip split evenly; how much per person if 2 people?"},
            ],
            thinking={"type": "enabled", "budget_tokens": 1500},
            extra_headers={"anthropic-beta": _BETA},
        )
        assert second.content, "the dependent second turn should succeed with the carrier replayed"
