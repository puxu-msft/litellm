"""content_block index 生命周期序列审计 / Block-sequence audit.

针对客户端 `RangeError("Content block not found")`(litellm#24765):流式响应里某
`content_block_delta`/`content_block_stop` 引用了一个从未 `content_block_start` 过的
index,客户端按 index 查它的 blocks 数组查不到就抛错并回退非流式。

本模块**只观测、绝不改写**。在 stream_transform 两侧各架一个 index 生命周期状态机:
- 入站(改写前):抓上游/转换层是否已经丢了 start —— issue #24765 本体。
- 出站(发往客户端):抓 stream_fix 自己的 index_shift/折叠/注入有没有把序列改断。
入站干净、出站脏 = `regressed`,即 stream_fix 制造的断裂(铁证)。

seq 用 run-length 压缩记全量事件 (type,index,count),不含正文:delta 全是同 index
重复,压缩后从几百项降到十几项,index 生命周期信息一点不丢。

设计为**完全零侵入 stream.py**:stream_transform_audited 包装原 response(观测入站)并
消费 stream_transform 输出(观测出站),同一 auditor 贯穿,finally 落盘(中途断开的
截断流正是现场,也要落)。audit 关时直接透传,零成本。
"""
from __future__ import annotations

import logging
import time
from collections import Counter

from hookpkg.config import load_config
from hookpkg.probes import append_jsonl
from hookpkg.sse import sse_parse
from hookpkg.stream import stream_transform

logger = logging.getLogger("litellm.hookpkg.block_audit")

_SHORT = {
    "content_block_start": "start",
    "content_block_delta": "delta",
    "content_block_stop": "stop",
}


class _SideState:
    """单侧(入站或出站)的 content_block index 生命周期状态机。

    Anthropic 协议:content block 顺序发出,每个走 start(i) -> delta(i)* -> stop(i),
    stop 后才 start(i+1),index 从 0 连续递增,任意时刻至多一个 block open。违反即记违规。
    """

    def __init__(self):
        self.seq = []            # run-length: [[short, index, count], ...]
        self.violations = []
        self.open = None         # 当前 open 的 block index(None=无)
        self.max_start = None    # 已见过的最大 start index(判 gap)
        self.blocks = []         # 每个 content_block_start 一项: [index, block_type, tool_name]
        self._finalized = False

    def _push_seq(self, short, index):
        if self.seq and self.seq[-1][0] == short and self.seq[-1][1] == index:
            self.seq[-1][2] += 1
        else:
            self.seq.append([short, index, 1])

    def observe(self, ev):
        if not isinstance(ev, dict):
            return
        t = ev.get("type")
        idx = ev.get("index")
        self._push_seq(_SHORT.get(t, t), idx)
        if t == "content_block_start":
            # 记 block 身份(type + tool name),使「同名 tool_use 重复」可见 —— run-length
            # 的 seq 只有 start/delta/stop+index,看不出块类型,更看不出两个 AskUserQuestion。
            cb = ev.get("content_block")
            cb = cb if isinstance(cb, dict) else {}
            self.blocks.append([idx, cb.get("type"), cb.get("name")])
            if self.open is not None:
                self.violations.append(
                    {"kind": "start_without_stop", "index": idx, "open": self.open})
            expected = 0 if self.max_start is None else self.max_start + 1
            if idx != expected:
                self.violations.append(
                    {"kind": "index_gap", "index": idx, "expected": expected})
            self.open = idx
            self.max_start = idx if self.max_start is None else max(self.max_start, idx)
        elif t == "content_block_delta":
            if self.open != idx:
                self.violations.append(
                    {"kind": "orphan_delta", "index": idx, "open": self.open})
        elif t == "content_block_stop":
            if self.open != idx:
                self.violations.append(
                    {"kind": "orphan_stop", "index": idx, "open": self.open})
            else:
                self.open = None
        # 其他事件(message_*/ping/...):记入 seq,不影响 block 状态

    def finalize(self):
        """流结束收尾:仍 open 的 block 记 unclosed。幂等。"""
        if self._finalized:
            return
        self._finalized = True
        if self.open is not None:
            self.violations.append({"kind": "unclosed", "index": self.open})


class BlockSeqAuditor:
    """双侧(入站/出站)审计聚合。observe_in/observe_out 逐事件喂,result() 出裁决。"""

    def __init__(self):
        self._in = _SideState()
        self._out = _SideState()

    def observe_in(self, ev):
        self._in.observe(ev)

    def observe_out(self, ev):
        self._out.observe(ev)

    def result(self):
        self._in.finalize()
        self._out.finalize()
        in_v = self._in.violations
        out_v = self._out.violations
        return {
            "in_seq": self._in.seq,
            "out_seq": self._out.seq,
            "in_violations": in_v,
            "out_violations": out_v,
            # 每个 content_block_start 的 [index, type, tool_name],看得出 tool_use 重复。
            "in_blocks": self._in.blocks,
            "out_blocks": self._out.blocks,
            # 同名 tool_use 出现 >=2 次的观测信号(非违规:合法 parallel 调用也会命中,
            # 需人工核对 input 是否真的相同)。区分成因:in 侧有=上游/转换层就发了两个;
            # 仅 out 侧有=stream_fix 注入(如泄漏 invoke 转换)。
            "dup_tools_in": _dup_tool_names(self._in.blocks),
            "dup_tools_out": _dup_tool_names(self._out.blocks),
            # 入站干净、出站脏 = stream_fix 自己改断的铁证
            "regressed": (not in_v) and bool(out_v),
            "ok": (not in_v) and (not out_v),
        }


def _dup_tool_names(blocks):
    """找出同名 tool_use 块出现 >=2 次的工具。返回 [{name, count, indices}, ...]。
    纯函数,只按 name 计数(block_audit 不缓冲 input,无法判定 input 是否字节相同)。"""
    names = [b[2] for b in blocks if b[2]]
    counts = Counter(names)
    return [{"name": n, "count": c, "indices": [b[0] for b in blocks if b[2] == n]}
            for n, c in counts.items() if c >= 2]


async def stream_transform_audited(response, request_data):
    """包装 stream_transform,双侧观测 content_block 序列。block_audit 关时零成本透传。

    薄壳通过 __init__ 导出的 stream_transform 调用本函数(已替换为审计版)。审计只读,
    不改写任何 chunk;stream_transform 的改写逻辑完全不受影响。
    """
    cfg = load_config()
    ba = cfg.get("block_audit") or {}
    if not ba.get("enabled"):
        async for chunk in stream_transform(response, request_data):
            yield chunk
        return

    auditor = BlockSeqAuditor()
    rd = request_data or {}

    async def _audited_response():
        # 入站观测:透明包装原 response,stream_transform 消费的是本生成器。
        async for chunk in response:
            ev = sse_parse(chunk)
            if isinstance(ev, dict):
                auditor.observe_in(ev)
            yield chunk

    try:
        async for out in stream_transform(_audited_response(), request_data):
            ev = sse_parse(out)
            if isinstance(ev, dict):
                auditor.observe_out(ev)
            yield out
    finally:
        # 中途断开(客户端断连,正是 Content block not found 现场)也要落盘。
        try:
            res = auditor.result()
            if ba.get("violation_only") and res["ok"]:
                return
            rec = {"event": "block_seq", "ts": time.time(),
                   "model": rd.get("model"), "call_id": rd.get("litellm_call_id"),
                   **res}
            path = ba.get("file")
            if path:
                append_jsonl(path, rec)
        except Exception as e:  # 审计绝不影响主流程
            logger.warning("block_audit flush failed (%r)", e)
