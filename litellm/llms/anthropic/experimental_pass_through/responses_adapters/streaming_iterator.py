# What is this?
## Translates OpenAI call to Anthropic `/v1/messages` format
import json
import traceback
from collections import deque
from typing import Any, AsyncIterator, Dict, Union

from litellm import verbose_logger
from litellm._uuid import uuid


def _reasoning_carrier_token(item: object) -> Union[str, None]:
    """Build the ``ghc-rsn`` carrier token for a completed reasoning item.

    Returns None when the bridge is disabled, the item is not a reasoning item, or
    the reasoning state is incomplete (missing/empty id or encrypted_content) -- so
    we never emit a valid-looking but unreplayable carrier. On by default; kill
    switch ``GHC_REASONING_DISABLE``.
    """
    from litellm.llms.github_copilot.reasoning_config import reasoning_bridge_enabled

    if not reasoning_bridge_enabled() or item is None:
        return None

    def _get(obj: object, key: str) -> object:
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

    if _get(item, "type") != "reasoning":
        return None

    item_id = _get(item, "id")
    encrypted = _get(item, "encrypted_content")
    if not (isinstance(item_id, str) and item_id and isinstance(encrypted, str) and encrypted):
        return None

    from litellm.llms.github_copilot.reasoning_carrier import (
        ReasoningReplayEnvelope,
        serialize_envelope,
    )

    summary_raw = _get(item, "summary")
    summary_seq = summary_raw if isinstance(summary_raw, (list, tuple)) else ()
    summary_parts = tuple(t for t in (_get(s, "text") for s in summary_seq) if isinstance(t, str) and t)
    env = ReasoningReplayEnvelope(
        reasoning_item_id=item_id,
        encrypted_content=encrypted,
        summary_parts=summary_parts,
        origin_model=None,
    )
    return serialize_envelope(env)


class AnthropicResponsesStreamWrapper:
    """
    Wraps a Responses API streaming iterator and re-emits events in Anthropic SSE format.

    Responses API event flow (relevant subset):
      response.created                   -> message_start
      response.output_item.added         -> content_block_start (if message/function_call)
      response.output_text.delta         -> content_block_delta (text_delta)
      response.reasoning_summary_text.delta -> content_block_delta (thinking_delta)
      response.function_call_arguments.delta -> content_block_delta (input_json_delta)
      response.output_item.done          -> content_block_stop
      response.completed                 -> message_delta + message_stop
    """

    def __init__(
        self,
        responses_stream: Any,
        model: str,
        reasoning_carrier: str = "signature",
    ) -> None:
        self.responses_stream = responses_stream
        self.reasoning_carrier = reasoning_carrier
        self.model = model
        self._message_id: str = f"msg_{uuid.uuid4()}"
        self._current_block_index: int = -1
        # Map item_id -> content_block_index so we can stop the right block later
        self._item_id_to_block_index: Dict[str, int] = {}
        # Track open function_call items by item_id so we can emit tool_use start
        self._pending_tool_ids: Dict[str, str] = {}  # item_id -> call_id / name accumulator
        self._orphan_function_argument_deltas: Dict[str, tuple[str, ...]] = {}
        self._streamed_function_argument_item_ids: set[str] = set()
        self._completed_item_ids: set[str] = set()
        self._open_block_indices: set[int] = set()
        self._sent_message_start = False
        self._sent_message_stop = False
        self._chunk_queue: deque = deque()

    def _make_message_start(self) -> Dict[str, Any]:
        return {
            "type": "message_start",
            "message": {
                "id": self._message_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": self.model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        }

    def _next_block_index(self) -> int:
        self._current_block_index += 1
        return self._current_block_index

    def _queue_content_block_start(self, block_idx: int, content_block: Dict[str, Any]) -> None:
        self._open_block_indices.add(block_idx)
        self._chunk_queue.append(
            {
                "type": "content_block_start",
                "index": block_idx,
                "content_block": content_block,
            }
        )

    def _queue_content_block_stop(self, block_idx: int) -> None:
        if block_idx not in self._open_block_indices:
            return
        self._open_block_indices.remove(block_idx)
        self._chunk_queue.append({"type": "content_block_stop", "index": block_idx})

    def _queue_function_argument_delta(self, item_id: Union[str, None], block_idx: int, delta: str) -> None:
        if item_id:
            self._streamed_function_argument_item_ids.add(item_id)
        self._chunk_queue.append(
            {
                "type": "content_block_delta",
                "index": block_idx,
                "delta": {"type": "input_json_delta", "partial_json": delta},
            }
        )

    @staticmethod
    def _item_value(item: object, key: str) -> object:
        return item.get(key) if isinstance(item, dict) else getattr(item, key, None)

    def _ensure_item_block(self, item: object) -> Union[int, None]:
        item_type = self._item_value(item, "type")
        item_id_value = self._item_value(item, "id")
        item_id = item_id_value if isinstance(item_id_value, str) and item_id_value else None
        if item_id is not None and item_id in self._item_id_to_block_index:
            return self._item_id_to_block_index[item_id]

        block_idx = self._next_block_index()
        if item_id is not None:
            self._item_id_to_block_index[item_id] = block_idx

        if item_type == "message":
            self._queue_content_block_start(block_idx, {"type": "text", "text": ""})
            return block_idx
        if item_type == "reasoning":
            self._queue_content_block_start(block_idx, {"type": "thinking", "thinking": ""})
            return block_idx
        if item_type == "function_call":
            call_id_value = self._item_value(item, "call_id")
            name_value = self._item_value(item, "name")
            call_id = call_id_value if isinstance(call_id_value, str) else ""
            name = name_value if isinstance(name_value, str) else ""
            if item_id is not None:
                self._pending_tool_ids[item_id] = call_id
            self._queue_content_block_start(
                block_idx,
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": name,
                    "input": {},
                },
            )
            return block_idx

        if item_id is not None:
            self._item_id_to_block_index.pop(item_id, None)
        self._current_block_index -= 1
        return None

    def _queue_terminal_events(
        self,
        *,
        stop_reason: str = "end_turn",
        usage: Union[Dict[str, Any], None] = None,
    ) -> None:
        if self._sent_message_stop:
            return
        for block_idx in sorted(self._open_block_indices):
            self._queue_content_block_stop(block_idx)
        self._chunk_queue.append(
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": usage or {"input_tokens": 0, "output_tokens": 0},
            }
        )
        self._chunk_queue.append({"type": "message_stop"})
        self._sent_message_stop = True

    def _process_event(self, event: Any) -> None:
        """Convert one Responses API event into zero or more Anthropic chunks queued for emission."""
        event_type = getattr(event, "type", None)
        if event_type is None and isinstance(event, dict):
            event_type = event.get("type")

        if event_type is None:
            return
        if self._sent_message_stop:
            return

        # ---- message_start ----
        if event_type == "response.created":
            if self._sent_message_start:
                return
            self._sent_message_start = True
            self._chunk_queue.append(self._make_message_start())
            return

        # ---- content_block_start for a new output message item ----
        if event_type == "response.output_item.added":
            item = getattr(event, "item", None) or (event.get("item") if isinstance(event, dict) else None)
            if item is None:
                return
            block_idx = self._ensure_item_block(item)
            item_type = self._item_value(item, "type")
            item_id_value = self._item_value(item, "id")
            item_id = item_id_value if isinstance(item_id_value, str) and item_id_value else None
            if item_type == "function_call" and item_id is not None and block_idx is not None:
                for buffered_delta in self._orphan_function_argument_deltas.pop(item_id, ()):
                    self._queue_function_argument_delta(item_id, block_idx, buffered_delta)
            return

        # ---- text delta ----
        if event_type == "response.output_text.delta":
            item_id = getattr(event, "item_id", None) or (event.get("item_id") if isinstance(event, dict) else None)
            delta = getattr(event, "delta", "") or (event.get("delta", "") if isinstance(event, dict) else "")
            block_idx = self._item_id_to_block_index.get(item_id, -1) if item_id else self._current_block_index
            if block_idx < 0:
                # Some providers (e.g. LMStudio) skip response.output_item.added,
                # so no text block is open yet; synthesize content_block_start
                # instead of emitting a delta with index -1
                block_idx = self._next_block_index()
                if item_id:
                    self._item_id_to_block_index[item_id] = block_idx
                self._queue_content_block_start(block_idx, {"type": "text", "text": ""})
            self._chunk_queue.append(
                {
                    "type": "content_block_delta",
                    "index": block_idx,
                    "delta": {"type": "text_delta", "text": delta},
                }
            )
            return

        # ---- reasoning summary text delta ----
        if event_type == "response.reasoning_summary_text.delta":
            item_id = getattr(event, "item_id", None) or (event.get("item_id") if isinstance(event, dict) else None)
            delta = getattr(event, "delta", "") or (event.get("delta", "") if isinstance(event, dict) else "")
            block_idx = self._item_id_to_block_index.get(item_id) if item_id else None
            if block_idx is None:
                block_idx = self._ensure_item_block({"type": "reasoning", "id": item_id})
            if block_idx is None:
                return
            self._chunk_queue.append(
                {
                    "type": "content_block_delta",
                    "index": block_idx,
                    "delta": {"type": "thinking_delta", "thinking": delta},
                }
            )
            return

        # ---- function call arguments delta ----
        if event_type == "response.function_call_arguments.delta":
            item_id = getattr(event, "item_id", None) or (event.get("item_id") if isinstance(event, dict) else None)
            delta = getattr(event, "delta", "") or (event.get("delta", "") if isinstance(event, dict) else "")
            block_idx = self._item_id_to_block_index.get(item_id) if item_id else None
            if block_idx is None:
                if item_id:
                    previous_deltas = self._orphan_function_argument_deltas.get(item_id, ())
                    self._orphan_function_argument_deltas[item_id] = (*previous_deltas, delta)
                return
            self._queue_function_argument_delta(item_id, block_idx, delta)
            return

        # ---- output item done -> content_block_stop ----
        if event_type == "response.output_item.done":
            item = getattr(event, "item", None) or (event.get("item") if isinstance(event, dict) else None)
            item_id = (
                getattr(item, "id", None) or (item.get("id") if isinstance(item, dict) else None) if item else None
            )
            if item_id and item_id in self._completed_item_ids:
                return
            block_idx = self._ensure_item_block(item) if item is not None else None
            if block_idx is None:
                return
            item_type = self._item_value(item, "type") if item is not None else None
            if item_type == "function_call" and item_id:
                buffered_deltas = self._orphan_function_argument_deltas.pop(item_id, ())
                arguments_value = self._item_value(item, "arguments")
                argument_deltas = (
                    buffered_deltas
                    if buffered_deltas
                    else (
                        (arguments_value,)
                        if item_id not in self._streamed_function_argument_item_ids
                        and isinstance(arguments_value, str)
                        and arguments_value
                        else ()
                    )
                )
                for argument_delta in argument_deltas:
                    self._queue_function_argument_delta(item_id, block_idx, argument_delta)
            if item_id:
                self._completed_item_ids.add(item_id)
            # Attach the reasoning carrier only when this done event maps to a real
            # opened reasoning block; never fall back to the current (possibly text)
            # block, which would inject the carrier into the wrong content block.
            mapped_reasoning = item_id is not None and item_id in self._item_id_to_block_index
            token = _reasoning_carrier_token(item) if mapped_reasoning else None
            reasoning_idx = self._item_id_to_block_index[item_id] if mapped_reasoning else block_idx

            if token is not None and self.reasoning_carrier == "redacted_thinking":
                # B: close the summary thinking block, then a separate redacted_thinking
                # carrier block (two independent content blocks, spec §4.2).
                self._queue_content_block_stop(reasoning_idx)
                red_idx = self._next_block_index()
                self._queue_content_block_start(red_idx, {"type": "redacted_thinking", "data": token})
                self._queue_content_block_stop(red_idx)
                return

            if token is not None and self.reasoning_carrier == "signature":
                # A: signature_delta on the reasoning (thinking) block before its stop.
                self._chunk_queue.append(
                    {
                        "type": "content_block_delta",
                        "index": reasoning_idx,
                        "delta": {"type": "signature_delta", "signature": token},
                    }
                )
            self._queue_content_block_stop(block_idx)
            return

        # ---- response completed -> message_delta + message_stop ----
        if event_type in (
            "response.completed",
            "response.failed",
            "response.incomplete",
        ):
            response_obj = getattr(event, "response", None) or (
                event.get("response") if isinstance(event, dict) else None
            )
            stop_reason = "end_turn"
            input_tokens = 0
            output_tokens = 0
            cache_creation_tokens = 0
            cache_read_tokens = 0

            if response_obj is not None:
                status = getattr(response_obj, "status", None)
                if status == "incomplete":
                    stop_reason = "max_tokens"
                usage = getattr(response_obj, "usage", None)
                if usage is not None:
                    input_tokens = getattr(usage, "input_tokens", 0) or 0
                    output_tokens = getattr(usage, "output_tokens", 0) or 0
                    cache_creation_tokens = getattr(usage, "input_tokens_details", None)  # type: ignore[assignment]
                    cache_read_tokens = getattr(usage, "output_tokens_details", None)  # type: ignore[assignment]
                    # Prefer direct cache fields if present
                    cache_creation_tokens = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
                    cache_read_tokens = int(getattr(usage, "cache_read_input_tokens", 0) or 0)

            # Check if tool_use was in the output to override stop_reason
            if response_obj is not None:
                output = getattr(response_obj, "output", []) or []
                for out_item in output:
                    out_type = getattr(out_item, "type", None) or (
                        out_item.get("type") if isinstance(out_item, dict) else None
                    )
                    if out_type == "function_call":
                        stop_reason = "tool_use"
                        break

            usage_delta: Dict[str, Any] = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
            if cache_creation_tokens:
                usage_delta["cache_creation_input_tokens"] = cache_creation_tokens
            if cache_read_tokens:
                usage_delta["cache_read_input_tokens"] = cache_read_tokens

            self._queue_terminal_events(stop_reason=stop_reason, usage=usage_delta)
            return

    def __aiter__(self) -> "AnthropicResponsesStreamWrapper":
        return self

    async def __anext__(self) -> Dict[str, Any]:
        # Return any queued chunks first
        if self._chunk_queue:
            return self._chunk_queue.popleft()

        # Emit message_start if not yet done (fallback if response.created wasn't fired)
        if not self._sent_message_start:
            self._sent_message_start = True
            self._chunk_queue.append(self._make_message_start())
            return self._chunk_queue.popleft()

        # Consume the upstream stream
        try:
            async for event in self.responses_stream:
                self._process_event(event)
                if self._chunk_queue:
                    return self._chunk_queue.popleft()
        except StopAsyncIteration:
            pass
        except Exception as e:
            verbose_logger.error(f"AnthropicResponsesStreamWrapper error: {e}\n{traceback.format_exc()}")

        self._queue_terminal_events()

        # Drain any remaining queued chunks
        if self._chunk_queue:
            return self._chunk_queue.popleft()

        raise StopAsyncIteration

    async def async_anthropic_sse_wrapper(self) -> AsyncIterator[bytes]:
        """Yield SSE-encoded bytes for each Anthropic event chunk."""
        async for chunk in self:
            if isinstance(chunk, dict):
                event_type: str = str(chunk.get("type", "message"))
                payload = f"event: {event_type}\ndata: {json.dumps(chunk)}\n\n"
                yield payload.encode()
            else:
                yield chunk
