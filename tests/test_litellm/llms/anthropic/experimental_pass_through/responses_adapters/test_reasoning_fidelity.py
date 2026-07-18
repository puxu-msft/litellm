"""Phase 2 PoC: streaming reasoning carrier emission.

Drives ``AnthropicResponsesStreamWrapper._process_event`` with constructed
Responses events and asserts that, on by default (kill switch ``GHC_REASONING_DISABLE``), a
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
    monkeypatch.delenv("GHC_REASONING_DISABLE", raising=False)
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
    monkeypatch.setenv("GHC_REASONING_DISABLE", "1")
    chunks = _drive_reasoning(_wrapper())
    assert not any(c.get("delta", {}).get("type") == "signature_delta" for c in chunks)


def _sig_deltas(chunks):
    return [c for c in chunks if c.get("delta", {}).get("type") == "signature_delta"]


def test_no_carrier_when_encrypted_content_empty(monkeypatch):
    monkeypatch.delenv("GHC_REASONING_DISABLE", raising=False)
    w = _wrapper()
    w._process_event({"type": "response.output_item.added", "item": {"type": "reasoning", "id": "rs_e"}})
    w._process_event(
        {
            "type": "response.output_item.done",
            "item": {"type": "reasoning", "id": "rs_e", "encrypted_content": "", "summary": []},
        }
    )
    assert not _sig_deltas(list(w._chunk_queue)), "empty encrypted_content must not emit a carrier"


def test_no_carrier_when_id_missing(monkeypatch):
    monkeypatch.delenv("GHC_REASONING_DISABLE", raising=False)
    w = _wrapper()
    w._process_event({"type": "response.output_item.added", "item": {"type": "reasoning", "id": "rs_x"}})
    w._process_event(
        {"type": "response.output_item.done", "item": {"type": "reasoning", "encrypted_content": "E", "summary": []}}
    )
    assert not _sig_deltas(list(w._chunk_queue))


def test_signature_delta_index_equals_reasoning_block_start(monkeypatch):
    monkeypatch.delenv("GHC_REASONING_DISABLE", raising=False)
    chunks = _drive_reasoning(_wrapper())
    start = next(
        c
        for c in chunks
        if c.get("type") == "content_block_start" and c.get("content_block", {}).get("type") == "thinking"
    )
    sig = _sig_deltas(chunks)[0]
    assert sig["index"] == start["index"], "signature_delta must ride the reasoning block, not another"


def test_unmapped_done_id_uses_new_reasoning_block_for_carrier(monkeypatch):
    monkeypatch.delenv("GHC_REASONING_DISABLE", raising=False)
    w = _wrapper()
    # reasoning block opened for rs_s, then a text/message block opened
    w._process_event({"type": "response.output_item.added", "item": {"type": "reasoning", "id": "rs_s"}})
    w._process_event({"type": "response.output_item.added", "item": {"type": "message", "id": "m1"}})
    # A done event with an unmapped reasoning id opens its own block. The
    # carrier must use that block rather than either previously open block.
    w._process_event(
        {
            "type": "response.output_item.done",
            "item": {"type": "reasoning", "id": "other", "encrypted_content": "E", "summary": []},
        }
    )
    chunks = list(w._chunk_queue)
    thinking_starts = [
        chunk
        for chunk in chunks
        if chunk.get("type") == "content_block_start"
        and chunk.get("content_block", {}).get("type") == "thinking"
    ]
    assert [chunk["index"] for chunk in thinking_starts] == [0, 2]
    signature_delta = _sig_deltas(chunks)[0]
    assert signature_delta["index"] == 2


def test_malformed_summary_does_not_crash_and_still_carries(monkeypatch):
    monkeypatch.delenv("GHC_REASONING_DISABLE", raising=False)
    w = _wrapper()
    w._process_event({"type": "response.output_item.added", "item": {"type": "reasoning", "id": "rs_m"}})
    w._process_event(
        {
            "type": "response.output_item.done",
            "item": {"type": "reasoning", "id": "rs_m", "encrypted_content": "E", "summary": 123},
        }
    )
    sig = _sig_deltas(list(w._chunk_queue))
    assert sig, "malformed summary must not suppress the carrier when id+ec are present"
    res = decode_carrier({"type": "thinking", "thinking": "", "signature": sig[0]["delta"]["signature"]})
    assert isinstance(res, DecodedCarrier)
    assert res.envelope.summary_parts == ()


# ---- Phase 4: request-side carrier -> Responses reasoning item ----
import json  # noqa: E402

from litellm.llms.anthropic.experimental_pass_through.responses_adapters.transformation import (  # noqa: E402
    LiteLLMAnthropicToResponsesAPIAdapter,
)
from litellm.llms.github_copilot.reasoning_carrier import (  # noqa: E402
    ReasoningReplayEnvelope,
    encode_carrier,
)

_ADAPTER = LiteLLMAnthropicToResponsesAPIAdapter()


def _carrier_block(item_id="rs_r", ec="ENC-REPLAY==", summary=("think a",)):
    env = ReasoningReplayEnvelope(item_id, ec, summary, "gpt-5.6-sol")
    (block,) = encode_carrier(env, "signature")
    return block, env


def test_request_side_carrier_becomes_reasoning_item(monkeypatch):
    monkeypatch.delenv("GHC_REASONING_DISABLE", raising=False)
    block, env = _carrier_block()
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [block, {"type": "text", "text": "answer"}]},
        {"role": "user", "content": "follow up"},
    ]
    items = _ADAPTER.translate_messages_to_responses_input(msgs)
    reasoning = [it for it in items if it.get("type") == "reasoning"]
    assert len(reasoning) == 1
    r = reasoning[0]
    assert r["id"] == env.reasoning_item_id
    assert r["encrypted_content"] == env.encrypted_content
    assert r["summary"] == [{"type": "summary_text", "text": "think a"}]
    # the reconstructed reasoning item must precede the assistant answer message
    types = [it.get("type") for it in items]
    assert types.index("reasoning") < types.index("message", types.index("reasoning"))
    # the real answer survives
    assert "answer" in json.dumps(items)


def test_request_side_non_carrier_thinking_kept_as_output_text(monkeypatch):
    # non-carrier (real Claude) thinking on a gpt request keeps litellm's default
    # behavior: preserved as visible output_text, not reconstructed as a reasoning item
    monkeypatch.delenv("GHC_REASONING_DISABLE", raising=False)
    claude_block = {"type": "thinking", "thinking": "claude internal reasoning", "signature": "EqoBrealclaudesig=="}
    msgs = [{"role": "assistant", "content": [claude_block, {"type": "text", "text": "hi"}]}]
    items = _ADAPTER.translate_messages_to_responses_input(msgs)
    assert not any(it.get("type") == "reasoning" for it in items)  # not our carrier -> no reasoning item
    joined = json.dumps(items)
    assert "claude internal reasoning" in joined  # preserved as output_text (litellm default)
    assert "hi" in joined


def test_request_side_disabled_no_reasoning_item(monkeypatch):
    monkeypatch.setenv("GHC_REASONING_DISABLE", "1")
    block, _ = _carrier_block()
    msgs = [{"role": "assistant", "content": [block]}]
    items = _ADAPTER.translate_messages_to_responses_input(msgs)
    assert not any(it.get("type") == "reasoning" for it in items)


# ---- summary follows litellm default (visible summary is a config opt-in, not forced) ----


def test_summary_not_forced_by_default(monkeypatch):
    monkeypatch.delenv("GHC_REASONING_DISABLE", raising=False)
    monkeypatch.setattr(
        "litellm.llms.anthropic.experimental_pass_through.responses_adapters.transformation.is_reasoning_auto_summary_enabled",
        lambda: False,
    )
    r = LiteLLMAnthropicToResponsesAPIAdapter.translate_thinking_to_reasoning(
        {"type": "enabled", "budget_tokens": 1024}
    )
    assert r is not None
    assert "summary" not in r


def test_translate_thinking_reasoning_summary_param(monkeypatch):
    monkeypatch.setattr(
        "litellm.llms.anthropic.experimental_pass_through.responses_adapters.transformation.is_reasoning_auto_summary_enabled",
        lambda: False,
    )
    # explicit resolved summary -> requested
    r = LiteLLMAnthropicToResponsesAPIAdapter.translate_thinking_to_reasoning(
        {"type": "enabled", "budget_tokens": 1024}, reasoning_summary="auto"
    )
    assert r["summary"] == "auto"
    # None -> defer to litellm default (no summary when global auto is off)
    r2 = LiteLLMAnthropicToResponsesAPIAdapter.translate_thinking_to_reasoning(
        {"type": "enabled", "budget_tokens": 1024}, reasoning_summary=None
    )
    assert "summary" not in r2


def test_resolve_reasoning_summary_default_auto_when_enabled(monkeypatch):
    from litellm.llms.anthropic.experimental_pass_through.responses_adapters.handler import _resolve_reasoning_summary

    monkeypatch.delenv("GHC_REASONING_DISABLE", raising=False)
    # no model_info config -> default summary "auto" (visible reasoning)
    assert _resolve_reasoning_summary({"custom_llm_provider": "github_copilot"}) == "auto"
    # explicit off -> omit
    assert (
        _resolve_reasoning_summary(
            {"custom_llm_provider": "github_copilot", "model_info": {"github_copilot_reasoning": {"summary": "off"}}}
        )
        is None
    )
    # detailed
    assert (
        _resolve_reasoning_summary(
            {
                "custom_llm_provider": "github_copilot",
                "model_info": {"github_copilot_reasoning": {"summary": "detailed"}},
            }
        )
        == "detailed"
    )


def test_resolve_reasoning_summary_none_when_disabled(monkeypatch):
    from litellm.llms.anthropic.experimental_pass_through.responses_adapters.handler import _resolve_reasoning_summary

    monkeypatch.setenv("GHC_REASONING_DISABLE", "1")
    assert _resolve_reasoning_summary({"custom_llm_provider": "github_copilot"}) is None


# ---- B carrier (redacted_thinking) streaming: two independent blocks ----


def _wrapper_b() -> AnthropicResponsesStreamWrapper:
    return AnthropicResponsesStreamWrapper(
        responses_stream=iter(()), model="gpt-5.6-sol", reasoning_carrier="redacted_thinking"
    )


def test_b_carrier_emits_separate_redacted_block(monkeypatch):
    monkeypatch.delenv("GHC_REASONING_DISABLE", raising=False)
    chunks = _drive_reasoning(_wrapper_b())
    # B uses a redacted_thinking block, not a signature_delta
    assert not _sig_deltas(chunks)
    red = [
        c
        for c in chunks
        if c.get("type") == "content_block_start" and c.get("content_block", {}).get("type") == "redacted_thinking"
    ]
    assert len(red) == 1, f"expected one redacted_thinking block, got {[c.get('type') for c in chunks]}"
    data = red[0]["content_block"]["data"]
    res = decode_carrier({"type": "redacted_thinking", "data": data})
    assert isinstance(res, DecodedCarrier)
    assert res.envelope.encrypted_content == "ENC-STREAM=="
    # the redacted carrier block is a separate content block from the summary thinking block
    thinking_start = next(
        c
        for c in chunks
        if c.get("type") == "content_block_start" and c.get("content_block", {}).get("type") == "thinking"
    )
    assert red[0]["index"] != thinking_start["index"]
    # both blocks get their own content_block_stop
    stops = {c["index"] for c in chunks if c.get("type") == "content_block_stop"}
    assert thinking_start["index"] in stops and red[0]["index"] in stops


def test_b_carrier_duplicate_done_is_idempotent(monkeypatch):
    monkeypatch.delenv("GHC_REASONING_DISABLE", raising=False)
    wrapper = _wrapper_b()
    wrapper._process_event(
        {
            "type": "response.output_item.added",
            "item": {"type": "reasoning", "id": "rs_duplicate"},
        }
    )
    done_event = {
        "type": "response.output_item.done",
        "item": {
            "type": "reasoning",
            "id": "rs_duplicate",
            "encrypted_content": "ENC-DUPLICATE==",
            "summary": [],
        },
    }

    wrapper._process_event(done_event)
    wrapper._process_event(done_event)

    redacted_starts = [
        chunk
        for chunk in wrapper._chunk_queue
        if chunk.get("type") == "content_block_start"
        and chunk.get("content_block", {}).get("type") == "redacted_thinking"
    ]
    assert len(redacted_starts) == 1


def test_a_carrier_still_default_signature(monkeypatch):
    monkeypatch.delenv("GHC_REASONING_DISABLE", raising=False)
    chunks = _drive_reasoning(_wrapper())  # default carrier = signature
    assert _sig_deltas(chunks)
    assert not any(
        c.get("type") == "content_block_start" and c.get("content_block", {}).get("type") == "redacted_thinking"
        for c in chunks
    )


def test_resolve_reasoning_carrier_from_config(monkeypatch):
    from litellm.llms.anthropic.experimental_pass_through.responses_adapters.handler import _resolve_reasoning_carrier

    monkeypatch.delenv("GHC_REASONING_DISABLE", raising=False)
    assert _resolve_reasoning_carrier({"custom_llm_provider": "github_copilot"}) == "signature"  # default
    assert (
        _resolve_reasoning_carrier(
            {
                "custom_llm_provider": "github_copilot",
                "model_info": {"github_copilot_reasoning": {"carrier": "redacted_thinking"}},
            }
        )
        == "redacted_thinking"
    )
    monkeypatch.setenv("GHC_REASONING_DISABLE", "1")
    assert (
        _resolve_reasoning_carrier(
            {
                "custom_llm_provider": "github_copilot",
                "model_info": {"github_copilot_reasoning": {"carrier": "redacted_thinking"}},
            }
        )
        == "off"
    )


def test_resolve_reasoning_off_for_non_copilot_provider(monkeypatch):
    from litellm.llms.anthropic.experimental_pass_through.responses_adapters.handler import (
        _resolve_reasoning_carrier,
        _resolve_reasoning_summary,
    )

    monkeypatch.delenv("GHC_REASONING_DISABLE", raising=False)
    # non github_copilot Responses provider (e.g. openai) -> bridge does not apply
    assert _resolve_reasoning_summary({"custom_llm_provider": "openai"}) is None
    assert _resolve_reasoning_carrier({"custom_llm_provider": "openai"}) == "off"


# ---- non-stream response carrier + full round-trip regression ----
from unittest.mock import MagicMock  # noqa: E402


def _reasoning_response(item_id="rs_rt", ec="ENC-RT-1234567890", summaries=("step alpha", "step beta")):
    from openai.types.responses import ResponseReasoningItem

    item = MagicMock(spec=ResponseReasoningItem)
    item.id = item_id
    item.encrypted_content = ec
    item.summary = [type("S", (), {"text": t})() for t in summaries]
    resp = MagicMock()
    resp.output = [item]
    resp.status = "completed"
    resp.usage = None
    resp.id = "resp_x"
    resp.model = "gpt-5.6-sol"
    return resp


def test_nonstream_response_emits_signature_carrier(monkeypatch):
    monkeypatch.delenv("GHC_REASONING_DISABLE", raising=False)
    out = _ADAPTER.translate_response(_reasoning_response(), reasoning_carrier="signature")
    thinking = [b for b in out["content"] if b.get("type") == "thinking" and b.get("signature")]
    assert thinking, f"expected a carrier thinking block, got {[b.get('type') for b in out['content']]}"
    res = decode_carrier(thinking[0])
    assert isinstance(res, DecodedCarrier)
    assert res.envelope.encrypted_content == "ENC-RT-1234567890"
    assert thinking[0]["thinking"] == "step alpha step beta"  # visible summary


def test_nonstream_response_emits_redacted_carrier_when_configured(monkeypatch):
    monkeypatch.delenv("GHC_REASONING_DISABLE", raising=False)
    out = _ADAPTER.translate_response(_reasoning_response(), reasoning_carrier="redacted_thinking")
    red = [b for b in out["content"] if b.get("type") == "redacted_thinking"]
    assert len(red) == 1
    assert isinstance(decode_carrier(red[0]), DecodedCarrier)


def test_nonstream_response_disabled_falls_back_to_summary_only(monkeypatch):
    monkeypatch.setenv("GHC_REASONING_DISABLE", "1")
    out = _ADAPTER.translate_response(_reasoning_response(), reasoning_carrier="signature")
    thinking = [b for b in out["content"] if b.get("type") == "thinking"]
    assert thinking and not thinking[0].get("signature")  # plain summary, no carrier


def test_full_roundtrip_response_emit_then_request_reconstruct(monkeypatch):
    """The core invariant, end-to-end without a live backend: a carrier emitted on the
    response, stored+replayed verbatim, reconstructs to a reasoning item with the exact
    original id + encrypted_content."""
    monkeypatch.delenv("GHC_REASONING_DISABLE", raising=False)
    out = _ADAPTER.translate_response(_reasoning_response(), reasoning_carrier="signature")
    replayed = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": out["content"]},
        {"role": "user", "content": "follow up"},
    ]
    items = _ADAPTER.translate_messages_to_responses_input(replayed)
    reasoning = [it for it in items if it.get("type") == "reasoning"]
    assert len(reasoning) == 1
    assert reasoning[0]["id"] == "rs_rt"
    assert reasoning[0]["encrypted_content"] == "ENC-RT-1234567890"


def test_roundtrip_tampered_carrier_does_not_reconstruct(monkeypatch):
    """A tampered carrier must NOT reconstruct a (backend-rejectable) reasoning item."""
    monkeypatch.delenv("GHC_REASONING_DISABLE", raising=False)
    out = _ADAPTER.translate_response(_reasoning_response(), reasoning_carrier="signature")
    block = next(b for b in out["content"] if b.get("type") == "thinking" and b.get("signature"))
    ns, ver, body = block["signature"].split(":", 2)
    block["signature"] = f"{ns}:{ver}:%{body}"  # inject an invalid base64 char
    replayed = [{"role": "assistant", "content": [block]}]
    items = _ADAPTER.translate_messages_to_responses_input(replayed)
    assert not any(it.get("type") == "reasoning" for it in items)


def test_build_responses_kwargs_carries_reconstructed_reasoning_to_backend(monkeypatch):
    """The outgoing Responses request (what hits copilot) contains the reconstructed
    reasoning item with the exact encrypted_content -- the 'reaches the backend' invariant."""
    from litellm.llms.anthropic.experimental_pass_through.responses_adapters.handler import _build_responses_kwargs

    monkeypatch.delenv("GHC_REASONING_DISABLE", raising=False)
    block, _ = _carrier_block(item_id="rs_backend", ec="ENC-BACKEND==")
    kwargs = _build_responses_kwargs(
        max_tokens=100,
        model="gpt",
        messages=[
            {"role": "assistant", "content": [block, {"type": "text", "text": "prev"}]},
            {"role": "user", "content": "continue"},
        ],
    )
    reasoning = [it for it in kwargs["input"] if isinstance(it, dict) and it.get("type") == "reasoning"]
    assert len(reasoning) == 1
    assert reasoning[0]["id"] == "rs_backend"
    assert reasoning[0]["encrypted_content"] == "ENC-BACKEND=="
