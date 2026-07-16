"""dedup_adjacent_tool_use 测试(伪 SSE bytes fixture,无需 litellm 运行时)。
守:相邻字节相同的 tool_use 去重、input 不同不误伤、非相邻不误伤、禁用透传、
丢弃后 index 连续、三连相同折叠为一。
运行:python3 -m unittest hookpkg.tests.test_dedup -v  (从 litellm 根目录)"""
import asyncio
import json
import unittest
from unittest import mock

from hookpkg import dedup as dedup_mod
from hookpkg.dedup import dedup_adjacent_tool_use


def sse(event_type, **fields):
    obj = {"type": event_type, **fields}
    return f"event: {event_type}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()


def tool_block(index, name, partials, tid="t"):
    """一个 tool_use 块:start + 每个 partial_json 一个 input_json_delta + stop。"""
    out = [sse("content_block_start", index=index,
               content_block={"type": "tool_use", "id": tid, "name": name, "input": {}})]
    for p in partials:
        out.append(sse("content_block_delta", index=index,
                       delta={"type": "input_json_delta", "partial_json": p}))
    out.append(sse("content_block_stop", index=index))
    return out


def text_block(index, text):
    return [
        sse("content_block_start", index=index, content_block={"type": "text", "text": ""}),
        sse("content_block_delta", index=index, delta={"type": "text_delta", "text": text}),
        sse("content_block_stop", index=index),
    ]


async def _collect(chunks, cfg):
    async def gen():
        for c in chunks:
            yield c
    with mock.patch.object(dedup_mod, "load_config", return_value=cfg):
        return [c async for c in dedup_adjacent_tool_use(gen(), {"model": "test"})]


def run(chunks, cfg):
    return asyncio.run(_collect(chunks, cfg))


def parse_events(out):
    evs = []
    for c in out:
        raw = c.decode() if isinstance(c, (bytes, bytearray)) else c
        for line in raw.split("\n"):
            line = line.strip()
            if line.startswith("data:"):
                p = line[len("data:"):].strip()
                if p and p != "[DONE]":
                    try:
                        o = json.loads(p)
                        if isinstance(o, dict) and "type" in o:
                            evs.append(o)
                    except Exception:
                        pass
    return evs


def tool_starts(evs):
    return [e for e in evs if e["type"] == "content_block_start"
            and e.get("content_block", {}).get("type") == "tool_use"]


def block_start_indices(evs):
    return [e.get("index") for e in evs if e["type"] == "content_block_start"]


ON = {"dedup_tool_use": {"enabled": True}, "stream_fix": {}}
OFF = {"dedup_tool_use": {"enabled": False}}


class TestDedupToolUse(unittest.TestCase):
    def test_duplicate_adjacent_dropped(self):
        """两个相邻、name+input 逐字节相同的 AskUserQuestion -> 只留一个。"""
        chunks = (
            [sse("message_start", message={"id": "m"})]
            + tool_block(0, "AskUserQuestion", ['{"questions":', '[{"q":1}]}'], tid="a")
            + tool_block(1, "AskUserQuestion", ['{"questions":', '[{"q":1}]}'], tid="b")
            + [sse("message_delta", delta={"stop_reason": "tool_use"})]
        )
        evs = parse_events(run(chunks, ON))
        self.assertEqual(len(tool_starts(evs)), 1, "重复的第二个 tool_use 必须被丢弃")
        # 只剩一个 tool_use 块,且其 stop 存在
        stops = [e for e in evs if e["type"] == "content_block_stop"]
        self.assertEqual(len(stops), 1)
        # message_delta 仍在
        self.assertTrue(any(e["type"] == "message_delta" for e in evs))

    def test_distinct_input_not_dropped(self):
        """同名但 input 不同(SF vs CHI)的合法背靠背 -> 都保留。"""
        chunks = (
            tool_block(0, "get_weather", ['{"city":"SF"}'], tid="a")
            + tool_block(1, "get_weather", ['{"city":"CHI"}'], tid="b")
        )
        evs = parse_events(run(chunks, ON))
        self.assertEqual(len(tool_starts(evs)), 2, "input 不同不得去重")

    def test_chunked_identical_input_still_deduped(self):
        """两个块的 partial_json 分片边界不同但拼接后相同 -> 仍判为重复。"""
        chunks = (
            tool_block(0, "AskUserQuestion", ['{"a":', '1}'], tid="a")
            + tool_block(1, "AskUserQuestion", ['{"a":1}'], tid="b")
        )
        evs = parse_events(run(chunks, ON))
        self.assertEqual(len(tool_starts(evs)), 1)

    def test_distinct_tools_adjacent_not_dropped(self):
        """相邻但不同工具名 -> 都保留。"""
        chunks = (
            tool_block(0, "Bash", ['{"command":"ls"}'], tid="a")
            + tool_block(1, "Read", ['{"path":"/x"}'], tid="b")
        )
        evs = parse_events(run(chunks, ON))
        self.assertEqual(len(tool_starts(evs)), 2)

    def test_non_adjacent_identical_not_dropped(self):
        """两个相同 tool_use 之间夹了一个 text 块 -> 相邻性被打断,都保留。"""
        chunks = (
            tool_block(0, "AskUserQuestion", ['{"q":1}'], tid="a")
            + text_block(1, "some text")
            + tool_block(2, "AskUserQuestion", ['{"q":1}'], tid="b")
        )
        evs = parse_events(run(chunks, ON))
        self.assertEqual(len(tool_starts(evs)), 2, "非相邻不得去重")

    def test_three_identical_collapse_to_one(self):
        """三连相同 -> 只留第一个。"""
        chunks = (
            tool_block(0, "AskUserQuestion", ['{"q":1}'], tid="a")
            + tool_block(1, "AskUserQuestion", ['{"q":1}'], tid="b")
            + tool_block(2, "AskUserQuestion", ['{"q":1}'], tid="c")
        )
        evs = parse_events(run(chunks, ON))
        self.assertEqual(len(tool_starts(evs)), 1)

    def test_index_contiguous_after_drop(self):
        """丢弃一个块后,后续块 index 递减保持连续:text@0, tool@1, tool@2(dup), text@3
        -> 输出块 index 应为 0,1,2(dup 丢弃,末尾 text 从 3 降到 2)。"""
        chunks = (
            text_block(0, "intro")
            + tool_block(1, "AskUserQuestion", ['{"q":1}'], tid="a")
            + tool_block(2, "AskUserQuestion", ['{"q":1}'], tid="b")
            + text_block(3, "outro")
        )
        evs = parse_events(run(chunks, ON))
        idxs = block_start_indices(evs)
        self.assertEqual(idxs, [0, 1, 2], f"丢弃后 index 未保持连续: {idxs}")
        # 末尾 text 内容仍在且 index=2
        outro = [e for e in evs if e["type"] == "content_block_start" and e.get("index") == 2]
        self.assertEqual(len(outro), 1)
        self.assertEqual(outro[0]["content_block"]["type"], "text")

    def test_disabled_is_passthrough(self):
        """dedup 关:完全字节透传。"""
        chunks = (
            tool_block(0, "AskUserQuestion", ['{"q":1}'], tid="a")
            + tool_block(1, "AskUserQuestion", ['{"q":1}'], tid="b")
        )
        out = run(chunks, OFF)
        self.assertEqual(out, chunks)


if __name__ == "__main__":
    unittest.main()
