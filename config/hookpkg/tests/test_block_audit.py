"""block_audit 测试 —— content_block index 生命周期序列审计。

两层:
- BlockSeqAuditor 纯逻辑:逐事件喂,断言五类违规精确检出、clean 序列零违规、
  入站干净出站脏 = regressed(stream_fix 改坏的铁证)、run-length 压缩不丢信息。
- stream_transform_audited async 接线:关时零成本透传、开时双侧观测并在流结束(含
  中途断开)落盘一条记录。
运行:python3 -m unittest hookpkg.tests.test_block_audit -v  (从 litellm 根目录)"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

from hookpkg.block_audit import BlockSeqAuditor, stream_transform_audited
from hookpkg import block_audit as ba_mod
from hookpkg import stream as stream_mod


def start(i):
    return {"type": "content_block_start", "index": i,
            "content_block": {"type": "text", "text": ""}}


def delta(i):
    return {"type": "content_block_delta", "index": i,
            "delta": {"type": "text_delta", "text": "x"}}


def stop(i):
    return {"type": "content_block_stop", "index": i}


def kinds(violations):
    return [v["kind"] for v in violations]


class TestBlockSeqAuditor(unittest.TestCase):
    def _feed_in(self, events):
        a = BlockSeqAuditor()
        for e in events:
            a.observe_in(e)
        return a.result()

    def test_clean_sequence_no_violations(self):
        """规范序列 start/delta*/stop 递增:零违规、ok=True、not regressed。"""
        evs = [{"type": "message_start"},
               start(0), delta(0), delta(0), stop(0),
               start(1), delta(1), stop(1),
               {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}]
        r = self._feed_in(evs)
        self.assertEqual(r["in_violations"], [])
        self.assertTrue(r["ok"])
        self.assertFalse(r["regressed"])

    def test_orphan_delta_detected(self):
        """delta 引用了从未 start 的 index —— Content block not found 的直接成因。"""
        r = self._feed_in([start(0), delta(0), stop(0), delta(1)])
        self.assertIn("orphan_delta", kinds(r["in_violations"]))
        v = next(v for v in r["in_violations"] if v["kind"] == "orphan_delta")
        self.assertEqual(v["index"], 1)
        self.assertIsNone(v["open"])
        self.assertFalse(r["ok"])

    def test_orphan_stop_detected(self):
        """stop 引用了非当前 open 的 index。"""
        r = self._feed_in([start(0), stop(2)])
        self.assertIn("orphan_stop", kinds(r["in_violations"]))
        v = next(v for v in r["in_violations"] if v["kind"] == "orphan_stop")
        self.assertEqual(v["index"], 2)
        self.assertEqual(v["open"], 0)

    def test_index_gap_detected(self):
        """start 跳号(0 then 2,缺 1):留下空洞,客户端数组会有 undefined。"""
        r = self._feed_in([start(0), stop(0), start(2), stop(2)])
        self.assertIn("index_gap", kinds(r["in_violations"]))
        v = next(v for v in r["in_violations"] if v["kind"] == "index_gap")
        self.assertEqual(v["index"], 2)
        self.assertEqual(v["expected"], 1)

    def test_start_without_stop_detected(self):
        """上一个 block 未 stop 就 start 下一个:两个 block 同时 open。"""
        r = self._feed_in([start(0), delta(0), start(1)])
        self.assertIn("start_without_stop", kinds(r["in_violations"]))
        v = next(v for v in r["in_violations"] if v["kind"] == "start_without_stop")
        self.assertEqual(v["index"], 1)
        self.assertEqual(v["open"], 0)

    def test_unclosed_at_stream_end(self):
        """流结束仍有 open block(截断):unclosed 违规带 open 的 index。"""
        r = self._feed_in([start(0), delta(0)])
        self.assertIn("unclosed", kinds(r["in_violations"]))
        v = next(v for v in r["in_violations"] if v["kind"] == "unclosed")
        self.assertEqual(v["index"], 0)

    def test_first_start_nonzero_is_gap(self):
        """首个 start 不是 index 0:也是 gap(expected 0)。"""
        r = self._feed_in([start(3), stop(3)])
        v = next(v for v in r["in_violations"] if v["kind"] == "index_gap")
        self.assertEqual(v["expected"], 0)
        self.assertEqual(v["index"], 3)

    def test_regressed_in_clean_out_dirty(self):
        """入站干净、出站有 orphan:regressed=True —— stream_fix 自己改断的铁证。"""
        a = BlockSeqAuditor()
        for e in [start(0), delta(0), stop(0)]:
            a.observe_in(e)
        # 出站:stream_fix 假想 bug 导致 delta 引用未 start 的 index
        for e in [delta(0), stop(0)]:
            a.observe_out(e)
        r = a.result()
        self.assertEqual(r["in_violations"], [])
        self.assertTrue(r["out_violations"])
        self.assertTrue(r["regressed"])

    def test_not_regressed_when_in_already_dirty(self):
        """入站本身就脏:不算 regressed(锅在上游/转换层,不是 stream_fix)。"""
        a = BlockSeqAuditor()
        for e in [delta(5)]:            # 入站就 orphan
            a.observe_in(e)
        for e in [delta(5)]:            # 出站照样脏
            a.observe_out(e)
        r = a.result()
        self.assertTrue(r["in_violations"])
        self.assertFalse(r["regressed"])

    def test_run_length_compression(self):
        """连续同 (type,index) 事件折叠为 [short, index, count],不丢生命周期信息。"""
        evs = [start(0)] + [delta(0)] * 5 + [stop(0)]
        r = self._feed_in(evs)
        self.assertEqual(r["in_seq"],
                         [["start", 0, 1], ["delta", 0, 5], ["stop", 0, 1]])

    def test_non_block_events_recorded_not_affecting_state(self):
        """message_*/ping 记入 seq(全量画像)但不改 block 状态、不触发违规。"""
        evs = [{"type": "message_start"}, start(0),
               {"type": "ping"}, delta(0), stop(0),
               {"type": "message_stop"}]
        r = self._feed_in(evs)
        self.assertEqual(r["in_violations"], [])
        seq_types = [row[0] for row in r["in_seq"]]
        self.assertEqual(seq_types,
                         ["message_start", "start", "ping", "delta", "stop", "message_stop"])

    def test_result_is_idempotent(self):
        """result() 多次调用(内部 finalize)不重复累计违规。"""
        a = BlockSeqAuditor()
        for e in [start(0), delta(0)]:
            a.observe_in(e)
        r1 = a.result()
        r2 = a.result()
        self.assertEqual(r1["in_violations"], r2["in_violations"])


def tool_start(i, name):
    return {"type": "content_block_start", "index": i,
            "content_block": {"type": "tool_use", "id": f"t{i}", "name": name, "input": {}}}


class TestBlockIdentityAndDupTools(unittest.TestCase):
    """记录每个 content_block_start 的 type+tool_name,并对同名 tool_use 重复给观测信号。"""

    def _feed_in(self, events):
        a = BlockSeqAuditor()
        for e in events:
            a.observe_in(e)
        return a.result()

    def test_blocks_record_type_and_name(self):
        """in_blocks 记 [index, block_type, tool_name];text 块 name 为 None。"""
        r = self._feed_in([start(0), stop(0), tool_start(1, "AskUserQuestion"), stop(1)])
        self.assertEqual(r["in_blocks"],
                         [[0, "text", None], [1, "tool_use", "AskUserQuestion"]])

    def test_duplicate_tool_use_flagged(self):
        """一条流里两个同名 AskUserQuestion tool_use -> dup_tools_in 报名/次数/indices。
        这是用户「连发两个相同 AskUserQuestion」现象在 litellm 侧的可见信号。"""
        r = self._feed_in([
            tool_start(0, "AskUserQuestion"), delta(0), stop(0),
            tool_start(1, "AskUserQuestion"), delta(1), stop(1),
        ])
        self.assertEqual(r["dup_tools_in"],
                         [{"name": "AskUserQuestion", "count": 2, "indices": [0, 1]}])
        # 纯 dup 不改序列合法性(合法 parallel 也可能同名),不污染 ok/violations
        self.assertEqual(r["in_violations"], [])

    def test_distinct_tools_not_flagged(self):
        """不同名工具并列 -> 不误报为重复。"""
        r = self._feed_in([
            tool_start(0, "Bash"), stop(0),
            tool_start(1, "Read"), stop(1),
        ])
        self.assertEqual(r["dup_tools_in"], [])

    def test_dup_only_on_out_side_signals_injection(self):
        """入站一个 tool_use、出站两个同名 -> 仅 dup_tools_out 命中,指向 stream_fix 注入
        (如泄漏 invoke 转换);入站已有两个则 dup_tools_in 命中,指向上游/转换层。"""
        a = BlockSeqAuditor()
        for e in [tool_start(0, "AskUserQuestion"), stop(0)]:
            a.observe_in(e)
        for e in [tool_start(0, "AskUserQuestion"), stop(0),
                  tool_start(1, "AskUserQuestion"), stop(1)]:
            a.observe_out(e)
        r = a.result()
        self.assertEqual(r["dup_tools_in"], [])
        self.assertEqual(r["dup_tools_out"],
                         [{"name": "AskUserQuestion", "count": 2, "indices": [0, 1]}])


# ---- async wrapper 接线 ----

async def _drain(gen):
    return [c async for c in gen]


def sse(event_type, **fields):
    obj = {"type": event_type, **fields}
    return f"event: {event_type}\ndata: {json.dumps(obj)}\n\n".encode()


class TestStreamTransformAudited(unittest.TestCase):
    def _run(self, chunks, cfg, break_after=None):
        records = []

        async def upstream():
            for i, c in enumerate(chunks):
                yield c
                if break_after is not None and i == break_after:
                    return

        async def go():
            with mock.patch.object(ba_mod, "load_config", return_value=cfg), \
                 mock.patch.object(stream_mod, "load_config", return_value=cfg), \
                 mock.patch.object(ba_mod, "append_jsonl",
                                   side_effect=lambda path, rec: records.append(rec)):
                out = []
                async for c in stream_transform_audited(upstream(), {"model": "m", "litellm_call_id": "cid"}):
                    out.append(c)
                return out

        out = asyncio.run(go())
        return out, records

    def test_disabled_passthrough_no_write(self):
        """block_audit.enabled 关:字节完全透传、绝不落盘。"""
        chunks = [sse("content_block_start", index=0,
                      content_block={"type": "text"}), sse("content_block_stop", index=0)]
        cfg = {"block_audit": {"enabled": False}, "stream_fix": {"enabled": False}}
        out, records = self._run(chunks, cfg)
        self.assertEqual(out, chunks)
        self.assertEqual(records, [])

    def test_enabled_writes_one_record_with_both_sides(self):
        """开启:流结束落且仅落一条记录,含 in_seq/out_seq 与 model/call_id。"""
        chunks = [sse("content_block_start", index=0, content_block={"type": "text"}),
                  sse("content_block_delta", index=0, delta={"type": "text_delta", "text": "hi"}),
                  sse("content_block_stop", index=0),
                  sse("message_delta", delta={"stop_reason": "end_turn"})]
        cfg = {"block_audit": {"enabled": True, "file": "/x.jsonl"},
               "stream_fix": {"enabled": False}}
        out, records = self._run(chunks, cfg)
        self.assertEqual(out, chunks, "audit 不得改写输出")
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["model"], "m")
        self.assertEqual(rec["call_id"], "cid")
        self.assertIn(["start", 0, 1], rec["in_seq"])
        self.assertIn(["start", 0, 1], rec["out_seq"])

    def test_violation_only_skips_clean_stream(self):
        """violation_only 开 + 干净流:不落盘(修好后应永远为空)。"""
        chunks = [sse("content_block_start", index=0, content_block={"type": "text"}),
                  sse("content_block_stop", index=0)]
        cfg = {"block_audit": {"enabled": True, "file": "/x.jsonl", "violation_only": True},
               "stream_fix": {"enabled": False}}
        out, records = self._run(chunks, cfg)
        self.assertEqual(records, [])

    def test_violation_only_writes_dirty_stream(self):
        """violation_only 开 + 脏流(orphan delta):落盘,且 ok=False。"""
        chunks = [sse("content_block_delta", index=7, delta={"type": "text_delta", "text": "x"})]
        cfg = {"block_audit": {"enabled": True, "file": "/x.jsonl", "violation_only": True},
               "stream_fix": {"enabled": False}}
        out, records = self._run(chunks, cfg)
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["ok"])
        self.assertIn("orphan_delta", kinds(records[0]["in_violations"]))

    def test_flush_on_midstream_break(self):
        """流中途断开(客户端断连,正是 Content block not found 现场):finally 仍落盘,
        且 unclosed 被记录。"""
        chunks = [sse("content_block_start", index=0, content_block={"type": "text"}),
                  sse("content_block_delta", index=0, delta={"type": "text_delta", "text": "x"}),
                  sse("content_block_stop", index=0)]
        cfg = {"block_audit": {"enabled": True, "file": "/x.jsonl"},
               "stream_fix": {"enabled": False}}
        # break_after=1:只发到 delta 就断,stop 永不到达 -> unclosed
        out, records = self._run(chunks, cfg, break_after=1)
        self.assertEqual(len(records), 1)
        self.assertIn("unclosed", kinds(records[0]["in_violations"]))


if __name__ == "__main__":
    unittest.main()
