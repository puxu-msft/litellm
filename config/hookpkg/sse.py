"""SSE 解析/序列化 / SSE parse & serialize.

litellm 的 anthropic_messages 流式路径在 iterator hook【之前】就把事件序列化成
SSE bytes(b"event: <type>\\ndata: <json>\\n\\n"),所以 hook 拿到的多是 bytes/str
而非 dict。这里提供解析回事件 dict、以及按原 wire 形态重序列化的工具。
"""
from __future__ import annotations

import json


def chunk_get(chunk, key, default=None):
    """兼容 dict 与带属性对象两种 chunk 形态。"""
    if isinstance(chunk, dict):
        return chunk.get(key, default)
    return getattr(chunk, key, default)


def chunk_to_plain(chunk):
    """尽量把 chunk 转成可读 dict(仅用于探针落盘)。"""
    if isinstance(chunk, dict):
        return chunk
    for attr in ("model_dump", "dict"):
        fn = getattr(chunk, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    return {"repr": repr(chunk)[:500], "type": chunk_get(chunk, "type")}


def sse_parse(chunk):
    """把一个流式 chunk 归一化为 Anthropic 事件 dict。还原失败返回 None(不可解析,透传)。"""
    if isinstance(chunk, dict):
        return chunk
    raw = None
    if isinstance(chunk, (bytes, bytearray)):
        try:
            raw = bytes(chunk).decode("utf-8")
        except Exception:
            return None
    elif isinstance(chunk, str):
        raw = chunk
    else:
        return None
    # 从 SSE 文本里取 data: 行的 JSON
    for line in raw.split("\n"):
        line = line.strip()
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
            if not payload or payload == "[DONE]":
                return None
            try:
                obj = json.loads(payload)
                return obj if isinstance(obj, dict) else None
            except Exception:
                return None
    return None


def is_truncated_json_frame(chunk):
    """判断一个 sse_parse 返回 None 的 chunk 是否是「被截断/损坏的 JSON 事件帧」。

    上游(github_copilot 双重转换)中途断流时,交给 hook 的最后一个 chunk 可能是半截 SSE
    帧:`data: {"type":"content_block_delta",...,"text":"hello wor`(字符串未闭合)。这类帧
    原样转发给客户端会让其 SSE 的 JSON 解析器报 `JSON Parse error: Unterminated string`。

    Anthropic 事件恒为 JSON **对象**(`{...}`),故判据:某 `data:` 行 payload 以 `{` 起头
    却 json.loads 失败 = 截断/损坏,应丢弃。合法的非 JSON 帧(`[DONE]` 以 `[` 起头、ping、
    SSE 注释 `: ...`、空行)一律返回 False,照常透传。解码用 errors="replace",使尾部截断
    在多字节 UTF-8 边界时仍能识别 `data: {` 前缀。
    """
    if isinstance(chunk, (bytes, bytearray)):
        raw = bytes(chunk).decode("utf-8", errors="replace")
    elif isinstance(chunk, str):
        raw = chunk
    else:
        return False
    for line in raw.split("\n"):
        line = line.strip()
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
            if not payload or payload == "[DONE]" or not payload.startswith("{"):
                return False
            try:
                json.loads(payload)
                return False  # 竟能解析(理论上不会走到这):非截断
            except Exception:
                return True
    return False


def sse_serialize(event_dict, template_chunk):
    """把改写后的事件 dict 重序列化回与 template_chunk 相同的 wire 形态。
    SSE 格式与 litellm 的 async_anthropic_sse_wrapper 一致:event: <type>\\ndata: <json>\\n\\n。
    """
    etype = str(event_dict.get("type", "message"))
    text = f"event: {etype}\ndata: {json.dumps(event_dict, ensure_ascii=False)}\n\n"
    if isinstance(template_chunk, (bytes, bytearray)):
        return text.encode("utf-8")
    if isinstance(template_chunk, str):
        return text
    # 原本就是 dict:直接返回 dict(litellm 下游 return_sse_chunk 会序列化)
    return event_dict
