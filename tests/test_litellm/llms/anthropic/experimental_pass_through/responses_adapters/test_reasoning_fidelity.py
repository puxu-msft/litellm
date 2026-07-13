"""Phase 2 PoC: streaming reasoning carrier emission.

Drives ``AnthropicResponsesStreamWrapper._process_event`` with constructed
Responses events and asserts that, behind the ``GHC_REASONING_POC`` flag, a
``signature_delta`` carrying the reasoning replay envelope is emitted right
before ``content_block_stop`` on ``response.output_item.done`` for a reasoning
item. This is the streaming path a real Claude Code gpt session hits.
"""
from __future__ import annotations

from litellm.llms.anthropic.experimental_pass_through.responses_adapters.streaming_iterator import (
    AnthropicResponsesStreamWrapper,
)
from litellm.llms.github_copilot.reasoning_carrier import decode_carrier, DecodedCarrier


def _wrapper() -> AnthropicResponsesStreamWrapper:
    return AnthropicResponsesStreamWrapper(responses_stream=iter(()), model="gpt-5.6-sol")


def _drive_reasoning(w: AnthropicResponsesStreamWrapper) -> list:
    w._process_event({"type": "response.output_item.added", "item": {"type": "reasoning", "id": "rs_s"}})
    w._process_event(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "reasoning",
                "id": "rs_s",
                "encrypted_content": "ENC-STREAM==",
                "summary": [{"text": "sa"}, {"text": "sb"}],
            },
        }
    )
    return list(w._chunk_queue)


def test_poc_streaming_reasoning_done_emits_signature_delta(monkeypatch):
    monkeypatch.setenv("GHC_REASONING_POC", "1")
    chunks = _drive_reasoning(_wrapper())

    sig = [c for c in chunks if c.get("delta", {}).get("type") == "signature_delta"]
    assert sig, f"expected a signature_delta, got {[c.get('type') for c in chunks]}"

    stops = [i for i, c in enumerate(chunks) if c.get("type") == "content_block_stop"]
    assert stops, "expected a content_block_stop"
    assert chunks.index(sig[0]) < stops[-1], "signature_delta must precede content_block_stop"

    res = decode_carrier({"type": "thinking", "thinking": "", "signature": sig[0]["delta"]["signature"]})
    assert isinstance(res, DecodedCarrier)
    assert res.envelope.reasoning_item_id == "rs_s"
    assert res.envelope.encrypted_content == "ENC-STREAM=="
    assert res.envelope.summary_parts == ("sa", "sb")


def test_poc_off_by_default_no_signature_delta(monkeypatch):
    monkeypatch.delenv("GHC_REASONING_POC", raising=False)
    chunks = _drive_reasoning(_wrapper())
    assert not any(c.get("delta", {}).get("type") == "signature_delta" for c in chunks)
