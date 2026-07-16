"""相邻重复 tool_use 去重 / Adjacent duplicate tool_use dedup.

根因无关的最后一道防线:无论重复源自 litellm 转换层(同一 tool_call 被拆成两个块)
还是上游模型/copilot 真发了两个,只要客户端将收到两个「name + 完整 input 字节完全
相同」且**相邻**的 tool_use 块,就丢弃后一个,并把后续块 index 递减以保持连续。

为什么在这一层、用完整 input 判定:区分「重复」与「合法背靠背」(如 get_weather(SF)
紧跟 get_weather(CHI))唯一可靠的依据是**完整累积 input**——SF≠CHI 的 input 不同,
永不误伤;而转换层在 content_block_start 时刻参数尚未到齐,拿不到这个依据(这正是
在转换层按 name/id 判定会误伤合法背靠背的原因)。

安全性:仅当 name 与完整 input 逐字节相同才丢。两个字节完全相同的相邻工具调用对用户
从无价值(问同一个问题两次、并行跑同一条命令),故丢弃安全。每次丢弃记审计事件。

风格:与 stream.py 一致的命令式流状态机(本包非函数式约束)。
"""
from __future__ import annotations

import json
import logging

from hookpkg.config import load_config
from hookpkg.probes import ProbeContext
from hookpkg.sse import sse_parse, sse_serialize

logger = logging.getLogger("litellm.hookpkg.dedup")


def _shift_index(ev, chunk, shift):
    """content_block_* 事件 index 减 shift 后重序列化;shift=0 时原样字节透传。"""
    if shift == 0 or not isinstance(ev, dict) or "index" not in ev:
        return chunk
    ev2 = dict(ev)
    ev2["index"] = ev.get("index", 0) - shift
    return sse_serialize(ev2, chunk)


def _emit_buffer(start_ev, start_chunk, delta_chunks, shift):
    """把缓冲的 tool_use 块(start + 各 delta 原始 chunk)按 shift 发出。"""
    out = [_shift_index(start_ev, start_chunk, shift)]
    for c in delta_chunks:
        dev = sse_parse(c)
        out.append(_shift_index(dev, c, shift) if isinstance(dev, dict) else c)
    return out


def _identity(name, start_input, partials):
    """一个 tool_use 块的字节指纹:name + start 自带 input + 累积的 partial_json。
    "".join 抹平分片边界,故同一逻辑 input 的不同切分仍判为相同。"""
    body = "".join(partials)
    if start_input:
        body = json.dumps(start_input, ensure_ascii=False, sort_keys=True) + "\x00" + body
    return (name, body)


async def dedup_adjacent_tool_use(response, request_data):
    """流式去重生成器。dedup_tool_use.enabled 关时零成本透传。"""
    cfg = load_config()
    dd = cfg.get("dedup_tool_use") or {}
    if not dd.get("enabled"):
        async for chunk in response:
            yield chunk
        return

    # 审计落 stream_fix.audit_file(与其它修复动作同处),便于线上观测去重是否发生。
    ctx = ProbeContext.from_stream_fix(cfg.get("stream_fix") or {},
                                       model=(request_data or {}).get("model"),
                                       call_id=(request_data or {}).get("litellm_call_id"))

    drop_shift = 0            # 已丢弃的块数;后续 content_block_* 的 index 一律减此值
    last_identity = None      # 紧邻上一个【已发出】tool_use 的指纹(仅相邻才比对)

    buffering = False
    b_start_ev = None
    b_start_chunk = None
    b_name = None
    b_start_input = None
    b_deltas = []
    b_partials = []

    async for chunk in response:
        ev = sse_parse(chunk)
        if ev is None:
            # 不可解析(ping/[DONE]/残片):stop 尚未到,保守把缓冲块原样发出,再透传本 chunk。
            if buffering:
                for c in _emit_buffer(b_start_ev, b_start_chunk, b_deltas, drop_shift):
                    yield c
                buffering = False
                b_deltas = []
                b_partials = []
            yield chunk
            continue

        ctype = ev.get("type")

        # 缓冲中却来了新的 content_block_start(上游违约,Anthropic 应先 stop):
        # 保守把当前缓冲块原样发出,避免丢块,再按常规处理这个新 start。
        if ctype == "content_block_start" and buffering:
            for c in _emit_buffer(b_start_ev, b_start_chunk, b_deltas, drop_shift):
                yield c
            buffering = False
            b_deltas = []
            b_partials = []

        if ctype == "content_block_start":
            cb = ev.get("content_block")
            cb = cb if isinstance(cb, dict) else {}
            if cb.get("type") == "tool_use":
                buffering = True
                b_start_ev = ev
                b_start_chunk = chunk
                b_name = cb.get("name")
                b_start_input = cb.get("input")
                b_deltas = []
                b_partials = []
                continue
            # 非 tool_use 块开始:打断相邻性(只对直接相邻的重复去重),按 shift 发出。
            last_identity = None
            yield _shift_index(ev, chunk, drop_shift)
            continue

        if buffering and ctype == "content_block_delta":
            d = ev.get("delta") or {}
            if d.get("type") == "input_json_delta":
                b_partials.append(d.get("partial_json") or "")
            b_deltas.append(chunk)
            continue

        if buffering and ctype == "content_block_stop":
            identity = _identity(b_name, b_start_input, b_partials)
            if last_identity is not None and identity == last_identity:
                # 与紧邻前一个 tool_use 逐字节相同 -> 丢弃整块,后续 index 递减。
                drop_shift += 1
                logger.warning("dedup: dropped duplicate adjacent tool_use %r", b_name)
                ctx.audit("dropped_duplicate_tool_use", tool=b_name)
            else:
                for c in _emit_buffer(b_start_ev, b_start_chunk, b_deltas, drop_shift):
                    yield c
                yield _shift_index(ev, chunk, drop_shift)
                last_identity = identity
            buffering = False
            b_deltas = []
            b_partials = []
            continue

        # 非缓冲态的 content_block delta/stop(非 tool 块):按 shift 发。
        if ctype in ("content_block_delta", "content_block_stop"):
            yield _shift_index(ev, chunk, drop_shift)
            continue

        # message_start / message_delta / message_stop / 其他:原样透传(无 index)。
        yield chunk

    # 流结束仍在缓冲(异常截断):发出缓冲块并补 stop,绝不丢块。
    if buffering:
        for c in _emit_buffer(b_start_ev, b_start_chunk, b_deltas, drop_shift):
            yield c
        idx = (b_start_ev.get("index", 0) if isinstance(b_start_ev, dict) else 0) - drop_shift
        yield sse_serialize({"type": "content_block_stop", "index": idx}, b_start_chunk)
