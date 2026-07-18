"""合成代表性 payload / Synthetic representative payloads.

三档请求体(小/中/大)近似真实 Claude Code 会话形态:多轮 tool_use↔tool_result、
thinking 块、散布的 cache_control、大段 system。以及一条真实形态的 Anthropic SSE 流
(bytes chunk 序列),供离线喂给 stream_transform。

注意:合成形态是构造的、非真实抓取——数值反映"这种规模/结构下 hook 花多少",不等于
线上真实分布(线上真实分布由 log_success 落盘 sink 聚合)。
"""
from __future__ import annotations

import json


def _text_block(text, cache=False):
    b = {"type": "text", "text": text}
    if cache:
        b["cache_control"] = {"type": "ephemeral", "scope": "context"}
    return b


def _tool_use_block(tid, name, inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def _tool_result_block(tid, content):
    return {"type": "tool_result", "tool_use_id": tid, "content": content}


def _thinking_block(sig="abc123signaturedata", text="Let me reason about this step by step. "):
    return {"type": "thinking", "thinking": text * 3, "signature": sig}


def _turn_pair(i, tool_result_len):
    """一轮 assistant(thinking + text + tool_use) / user(tool_result)。"""
    tid = f"toolu_{i:04d}"
    assistant = {
        "role": "assistant",
        "content": [
            _thinking_block(),
            _text_block(f"I'll look at file {i} to understand the structure."),
            _tool_use_block(tid, "Read", {"file_path": f"/repo/src/module_{i}.py"}),
        ],
    }
    user = {
        "role": "user",
        "content": [_tool_result_block(tid, "x" * tool_result_len)],
    }
    return [assistant, user]


def build_request(n_turns, tool_result_len, system_len, n_tools, cache_every=8):
    """构造一个 Anthropic /v1/messages 请求体(pre_call hook 看到的 `data`)。"""
    system = [
        _text_block("You are Claude Code, an agentic coding assistant. " * (system_len // 50 + 1),
                    cache=True),
    ]
    messages = [{"role": "user", "content": [_text_block("Help me refactor this repo.")]}]
    for i in range(n_turns):
        pair = _turn_pair(i, tool_result_len)
        # 每 cache_every 轮给一个块打 cache_control(近似 Claude Code 的分段缓存)
        if i % cache_every == 0:
            pair[0]["content"][1]["cache_control"] = {"type": "ephemeral", "scope": "context"}
        messages.extend(pair)
    messages.append({"role": "user", "content": [_text_block("Now summarize what you changed.")]})

    tools = [
        {
            "name": name,
            "description": f"Tool {name} does something useful. " * 4,
            "input_schema": {
                "type": "object",
                "properties": {"arg": {"type": "string", "description": "an argument " * 5}},
                "required": ["arg"],
            },
        }
        for name in [f"tool_{k}" for k in range(n_tools)]
    ]
    return {
        "model": "github_copilot/claude-opus-4.8",
        "messages": messages,
        "system": system,
        "tools": tools,
        "stream": True,
        "max_tokens": 32000,
        "thinking": {"type": "enabled", "budget_tokens": 8000},
        "provider_specific_header": {
            "custom_llm_provider": "github_copilot",
            "extra_headers": {"anthropic-beta": "context-management-2025-06-27,fine-grained-tool-streaming-2025-05-14"},
        },
    }


def payloads():
    """三档 payload 及其近似字节量。"""
    small = build_request(n_turns=3, tool_result_len=200, system_len=200, n_tools=5)
    medium = build_request(n_turns=40, tool_result_len=800, system_len=1500, n_tools=15)
    large = build_request(n_turns=200, tool_result_len=2500, system_len=6000, n_tools=25)
    out = {}
    for name, p in (("small", small), ("medium", medium), ("large", large)):
        nbytes = len(json.dumps(p, ensure_ascii=False).encode("utf-8"))
        n_msgs = len(p["messages"])
        out[name] = {"data": p, "bytes": nbytes, "n_msgs": n_msgs}
    return out


# ---------------------------------------------------------------------------
# 合成 SSE 流(bytes chunk),真实形态:message_start / 长 text block(多 delta)/
# 一个 tool_use 块(input_json_delta 分片)/ message_delta / message_stop / [DONE]。
# ---------------------------------------------------------------------------

def _sse(event_type, payload):
    return f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def build_sse_stream(n_text_deltas, tool_input, n_tool_deltas):
    """返回 (chunks: list[bytes])。一条完整、合法的 Anthropic 流。"""
    chunks = [
        _sse("message_start", {"type": "message_start", "message": {
            "id": "msg_synthetic", "type": "message", "role": "assistant", "model": "claude-opus-4.8",
            "content": [], "stop_reason": None, "usage": {"input_tokens": 1000, "output_tokens": 1}}}),
        _sse("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "text", "text": ""}}),
    ]
    seg = "The quick brown fox jumps over the lazy dog. "
    for _ in range(n_text_deltas):
        chunks.append(_sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                   "delta": {"type": "text_delta", "text": seg}}))
    chunks.append(_sse("content_block_stop", {"type": "content_block_stop", "index": 0}))

    # tool_use 块(分片 input_json_delta)
    chunks.append(_sse("content_block_start", {"type": "content_block_start", "index": 1,
                                               "content_block": {"type": "tool_use", "id": "toolu_out",
                                                                 "name": "Read", "input": {}}}))
    raw = json.dumps(tool_input, ensure_ascii=False)
    step = max(1, len(raw) // n_tool_deltas)
    for i in range(0, len(raw), step):
        chunks.append(_sse("content_block_delta", {"type": "content_block_delta", "index": 1,
                                                   "delta": {"type": "input_json_delta",
                                                             "partial_json": raw[i:i + step]}}))
    chunks.append(_sse("content_block_stop", {"type": "content_block_stop", "index": 1}))

    chunks.append(_sse("message_delta", {"type": "message_delta",
                                         "delta": {"stop_reason": "end_turn"},
                                         "usage": {"output_tokens": n_text_deltas * 10}}))
    chunks.append(_sse("message_stop", {"type": "message_stop"}))
    chunks.append(b"data: [DONE]\n\n")
    return chunks
