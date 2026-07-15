"""Group 1: the real anthropic SDK drives the GHC gpt reasoning bridge through the proxy.

Confirms our proxy's response is SDK-parseable and that gpt reasoning surfaces as a
thinking block carrying the ``ghc-rsn`` reasoning carrier (with real encrypted_content),
both non-streaming and streaming. Marked ``anthropic_sdk`` (billed: hits copilot).
"""
from __future__ import annotations

import pytest

from litellm.llms.github_copilot.reasoning_carrier import DecodedCarrier, decode_carrier

_PROMPT = "A bat and a ball cost $1.10 total; the bat costs $1.00 more than the ball. How much is the ball? Reason step by step."
_BETA = "interleaved-thinking-2025-05-14"


def _carrier_signatures(content) -> list:
    return [
        b.signature
        for b in content
        if getattr(b, "type", None) == "thinking" and (getattr(b, "signature", "") or "").startswith("ghc-rsn:v1:")
    ]


@pytest.mark.anthropic_sdk
class TestAnthropicSdkReasoning:
    def test_nonstream_thinking_carries_reasoning_carrier(self, anthropic_client, gpt_model):
        msg = anthropic_client.messages.create(
            model=gpt_model,
            max_tokens=2048,
            thinking={"type": "enabled", "budget_tokens": 1500},
            messages=[{"role": "user", "content": _PROMPT}],
            extra_headers={"anthropic-beta": _BETA},
        )
        sigs = _carrier_signatures(msg.content)
        assert sigs, "gpt reasoning should surface as a thinking block with a ghc-rsn carrier"
        res = decode_carrier({"type": "thinking", "thinking": "", "signature": sigs[0]})
        assert isinstance(res, DecodedCarrier)
        assert len(res.envelope.encrypted_content) > 100, "carrier should hold real encrypted reasoning"

    @pytest.mark.xfail(
        reason="double message_start (block 2 protocol-envelope defect) breaks the strict "
        "anthropic SDK stream's thinking_delta accumulation; Claude Code (lenient) handles it "
        "-- visible reasoning for the real client is covered by the claude_cli group. xpass "
        "here signals block 2 fixed the double message_start.",
        strict=False,
    )
    def test_streaming_reasoning_is_visible_summary(self, anthropic_client, gpt_model):
        # Visible reasoning (summary=auto) is streamed as thinking_delta. Accumulate the raw
        # deltas the SDK surfaces (currently suppressed by the double message_start, see xfail).
        thinking_text = ""
        with anthropic_client.messages.stream(
            model=gpt_model,
            max_tokens=2048,
            thinking={"type": "enabled", "budget_tokens": 1500},
            messages=[{"role": "user", "content": _PROMPT}],
            extra_headers={"anthropic-beta": _BETA},
        ) as stream:
            for event in stream:
                delta = getattr(event, "delta", None)
                if getattr(event, "type", None) == "content_block_delta" and getattr(delta, "type", None) == "thinking_delta":
                    thinking_text += getattr(delta, "thinking", "") or ""
        assert thinking_text.strip(), "summary=auto default should stream visible reasoning as thinking_delta"

    def test_streaming_final_message_carries_carrier(self, anthropic_client, gpt_model):
        with anthropic_client.messages.stream(
            model=gpt_model,
            max_tokens=2048,
            thinking={"type": "enabled", "budget_tokens": 1500},
            messages=[{"role": "user", "content": _PROMPT}],
            extra_headers={"anthropic-beta": _BETA},
        ) as stream:
            final = stream.get_final_message()
        sigs = _carrier_signatures(final.content)
        assert sigs, "streamed final message should reassemble a thinking block with the ghc-rsn carrier"
        assert isinstance(decode_carrier({"type": "thinking", "thinking": "", "signature": sigs[0]}), DecodedCarrier)
