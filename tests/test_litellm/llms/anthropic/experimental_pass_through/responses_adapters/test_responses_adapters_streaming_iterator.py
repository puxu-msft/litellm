"""
Tests for AnthropicResponsesStreamWrapper
(litellm/llms/anthropic/experimental_pass_through/responses_adapters/streaming_iterator.py)
"""

import os
import sys

import pytest

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../../.."))
)

from litellm.llms.anthropic.experimental_pass_through.responses_adapters.streaming_iterator import (
    AnthropicResponsesStreamWrapper,
)


def _process_all(events: list) -> list:
    wrapper = AnthropicResponsesStreamWrapper(responses_stream=None, model="m")
    for event in events:
        wrapper._process_event(event)
    return list(wrapper._chunk_queue)


class TestProcessEventTextDeltaWithoutOutputItemAdded:
    """Streams that skip response.output_item.added (e.g. LMStudio) must still
    open a text block before any delta and never emit index -1."""

    def test_process_event_synthesizes_content_block_start_before_delta(self):
        chunks = _process_all(
            [
                {"type": "response.output_text.delta", "item_id": "i1", "delta": "Hel"},
                {"type": "response.output_text.delta", "item_id": "i1", "delta": "lo"},
            ]
        )
        assert [c["type"] for c in chunks] == [
            "content_block_start",
            "content_block_delta",
            "content_block_delta",
        ]
        assert chunks[0]["content_block"] == {"type": "text", "text": ""}
        assert [c["index"] for c in chunks] == [0, 0, 0]
        assert chunks[1]["delta"] == {"type": "text_delta", "text": "Hel"}

    def test_process_event_delta_without_item_id_never_yields_negative_index(self):
        chunks = _process_all([{"type": "response.output_text.delta", "delta": "Hi"}])
        assert [(c["type"], c["index"]) for c in chunks] == [
            ("content_block_start", 0),
            ("content_block_delta", 0),
        ]

    def test_process_event_unregistered_item_id_opens_new_text_block(self):
        chunks = _process_all(
            [
                {
                    "type": "response.output_item.added",
                    "item": {"type": "reasoning", "id": "rs_1"},
                },
                {"type": "response.output_text.delta", "item_id": "m1", "delta": "Hi"},
            ]
        )
        assert chunks[1]["type"] == "content_block_start"
        assert chunks[1]["content_block"] == {"type": "text", "text": ""}
        assert [c["index"] for c in chunks[1:]] == [1, 1]

    def test_process_event_registered_item_id_does_not_synthesize_start(self):
        chunks = _process_all(
            [
                {
                    "type": "response.output_item.added",
                    "item": {"type": "message", "id": "m1"},
                },
                {"type": "response.output_text.delta", "item_id": "m1", "delta": "Hi"},
            ]
        )
        assert [(c["type"], c["index"]) for c in chunks] == [
            ("content_block_start", 0),
            ("content_block_delta", 0),
        ]


def test_reasoning_delta_without_output_item_added_opens_thinking_block():
    chunks = _process_all(
        [
            {
                "type": "response.reasoning_summary_text.delta",
                "item_id": "reasoning_1",
                "delta": "step",
            }
        ]
    )

    assert [(chunk["type"], chunk["index"]) for chunk in chunks] == [
        ("content_block_start", 0),
        ("content_block_delta", 0),
    ]
    assert chunks[0]["content_block"] == {"type": "thinking", "thinking": ""}


def test_function_arguments_without_output_item_added_are_replayed_after_done():
    chunks = _process_all(
        [
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "function_1",
                "delta": '{"city":',
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "function_1",
                "delta": '"Paris"}',
            },
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "function_1",
                    "call_id": "call_1",
                    "name": "weather",
                    "arguments": '{"city":"Paris"}',
                },
            },
        ]
    )

    assert [(chunk["type"], chunk["index"]) for chunk in chunks] == [
        ("content_block_start", 0),
        ("content_block_delta", 0),
        ("content_block_delta", 0),
        ("content_block_stop", 0),
    ]
    assert chunks[0]["content_block"] == {
        "type": "tool_use",
        "id": "call_1",
        "name": "weather",
        "input": {},
    }
    assert [chunk["delta"]["partial_json"] for chunk in chunks[1:3]] == [
        '{"city":',
        '"Paris"}',
    ]


def test_streamed_function_arguments_are_not_repeated_from_done_item():
    chunks = _process_all(
        [
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "function_call",
                    "id": "function_1",
                    "call_id": "call_1",
                    "name": "weather",
                },
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "function_1",
                "delta": '{"city":"Paris"}',
            },
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "function_1",
                    "call_id": "call_1",
                    "name": "weather",
                    "arguments": '{"city":"Paris"}',
                },
            },
        ]
    )

    argument_deltas = [
        chunk["delta"]["partial_json"]
        for chunk in chunks
        if chunk.get("type") == "content_block_delta"
    ]
    assert argument_deltas == ['{"city":"Paris"}']


def test_orphan_function_arguments_are_drained_before_later_streamed_deltas():
    chunks = _process_all(
        [
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "function_1",
                "delta": '{"city":',
            },
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "function_call",
                    "id": "function_1",
                    "call_id": "call_1",
                    "name": "weather",
                },
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "function_1",
                "delta": '"Paris"}',
            },
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "function_1",
                    "call_id": "call_1",
                    "name": "weather",
                    "arguments": '{"city":"Paris"}',
                },
            },
        ]
    )

    argument_deltas = [
        chunk["delta"]["partial_json"]
        for chunk in chunks
        if chunk.get("type") == "content_block_delta"
    ]
    assert argument_deltas == ['{"city":', '"Paris"}']


def test_events_after_message_stop_are_ignored():
    chunks = _process_all(
        [
            {"type": "response.completed"},
            {
                "type": "response.output_text.delta",
                "item_id": "late_message",
                "delta": "late",
            },
            {
                "type": "response.output_item.added",
                "item": {"type": "message", "id": "late_message"},
            },
        ]
    )

    assert [chunk["type"] for chunk in chunks] == [
        "message_delta",
        "message_stop",
    ]


def test_done_without_output_item_added_synthesizes_block_lifecycle():
    chunks = _process_all(
        [
            {
                "type": "response.output_item.done",
                "item": {"type": "message", "id": "message_1"},
            }
        ]
    )

    assert [(chunk["type"], chunk["index"]) for chunk in chunks] == [
        ("content_block_start", 0),
        ("content_block_stop", 0),
    ]


def test_duplicate_output_item_added_does_not_open_second_block():
    chunks = _process_all(
        [
            {
                "type": "response.output_item.added",
                "item": {"type": "message", "id": "message_1"},
            },
            {
                "type": "response.output_item.added",
                "item": {"type": "message", "id": "message_1"},
            },
        ]
    )

    assert [(chunk["type"], chunk["index"]) for chunk in chunks] == [
        ("content_block_start", 0),
    ]


@pytest.mark.asyncio
async def test_response_created_after_fallback_does_not_duplicate_message_start():
    async def responses_stream():
        yield {"type": "response.created"}
        yield {"type": "response.completed"}

    wrapper = AnthropicResponsesStreamWrapper(
        responses_stream=responses_stream(), model="gpt-5"
    )

    chunks = [chunk async for chunk in wrapper]

    assert [chunk["type"] for chunk in chunks] == [
        "message_start",
        "message_delta",
        "message_stop",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_stream_eof_closes_open_block_and_message(fail: bool):
    async def responses_stream():
        yield {
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "msg_item_1"},
        }
        yield {
            "type": "response.output_text.delta",
            "item_id": "msg_item_1",
            "delta": "partial",
        }
        if fail:
            raise RuntimeError("upstream disconnected")

    wrapper = AnthropicResponsesStreamWrapper(
        responses_stream=responses_stream(), model="gpt-5"
    )

    chunks = [chunk async for chunk in wrapper]

    assert [chunk["type"] for chunk in chunks] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert chunks[-2]["delta"]["stop_reason"] == "end_turn"
