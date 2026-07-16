"""hookpkg 孤儿 tool_result 三态处理单测。
运行:python3 -m unittest hookpkg.tests.test_orphan_tool_result -v  (从 litellm 根目录)

背景:anthropic_messages→responses 路径,一个 tool_result 若其 tool_use_id 在整个请求里无
匹配的 tool_use,翻译成 Responses API 后即 function_call_output 无配对 function_call,
copilot /responses 报 `invalid_request_body`:"No tool call found for function call output
with call_id ..."。修复点在 async_pre_call_hook(process)的 Anthropic messages 上——该路径
走异步 aresponses 不应用 deployment hook,只有此处对 messages 的改写会传播到 responses 翻译。
策略:passthrough(默认,留给上游拒)/ drop(删块)/ text(转带 tag 的代码块 text 块,保内容)。
非孤儿(有配对 tool_use)永不受影响。"""
from __future__ import annotations

import unittest

from hookpkg import _fix_orphan_tool_result, _find_orphan_tool_results


def _paired():
    return [
        {"role": "user", "content": [{"type": "text", "text": "go"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call_ok", "name": "W", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_ok", "content": "42"}]},
    ]


def _orphan():
    # tool_result 引用 call_missing,但请求里没有该 id 的 tool_use -> 孤儿
    return [
        {"role": "user", "content": [{"type": "text", "text": "go"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_missing", "content": "stale"}]},
    ]


class TestFindOrphanToolResults(unittest.TestCase):
    def test_orphan_detected(self):
        self.assertEqual(_find_orphan_tool_results(_orphan()), [(1, 0, "call_missing")])

    def test_paired_not_flagged(self):
        self.assertEqual(_find_orphan_tool_results(_paired()), [])

    def test_tool_use_anywhere_pairs(self):
        # 配对的 tool_use 即便不是紧邻上一条,只要请求里存在就不算孤儿。
        msgs = [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "call_x", "name": "W", "input": {}}]},
            {"role": "user", "content": [{"type": "text", "text": "mid"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_x", "content": "r"}]},
        ]
        self.assertEqual(_find_orphan_tool_results(msgs), [])

    def test_non_list_safe(self):
        self.assertEqual(_find_orphan_tool_results(None), [])
        self.assertEqual(_find_orphan_tool_results("nope"), [])


class TestFixOrphanToolResultStrategies(unittest.TestCase):
    def test_passthrough_default_no_change(self):
        data = {"model": "gpt", "messages": _orphan()}
        before = [dict(m) for m in data["messages"]]
        n = _fix_orphan_tool_result(data, {})  # no config -> passthrough
        self.assertEqual(n, 0)
        self.assertEqual(data["messages"], before)

    def test_passthrough_explicit_no_change(self):
        data = {"model": "gpt", "messages": _orphan()}
        n = _fix_orphan_tool_result(data, {"orphan_tool_result": {"strategy": "passthrough"}})
        self.assertEqual(n, 0)
        self.assertEqual(data["messages"][1]["content"][0]["type"], "tool_result")

    def test_drop_removes_orphan_and_empty_message(self):
        data = {"model": "gpt", "messages": _orphan()}
        same = data["messages"]
        n = _fix_orphan_tool_result(data, {"orphan_tool_result": {"strategy": "drop"}})
        self.assertEqual(n, 1)
        # 该 user 消息只有孤儿块 -> 删后为空 -> 整条消息被丢弃
        self.assertEqual(len(data["messages"]), 1)
        self.assertEqual(data["messages"][0]["content"][0]["type"], "text")
        self.assertIs(data["messages"], same)  # 原地 mutate,保持同一 list 引用

    def test_drop_keeps_sibling_blocks(self):
        msgs = [
            {"role": "user", "content": [
                {"type": "text", "text": "keep me"},
                {"type": "tool_result", "tool_use_id": "call_missing", "content": "x"},
            ]},
        ]
        data = {"model": "gpt", "messages": msgs}
        n = _fix_orphan_tool_result(data, {"orphan_tool_result": {"strategy": "drop"}})
        self.assertEqual(n, 1)
        self.assertEqual(data["messages"][0]["content"], [{"type": "text", "text": "keep me"}])

    def test_text_converts_to_tagged_code_block(self):
        data = {"model": "gpt", "messages": _orphan()}
        n = _fix_orphan_tool_result(data, {"orphan_tool_result": {"strategy": "text"}})
        self.assertEqual(n, 1)
        block = data["messages"][1]["content"][0]
        self.assertEqual(block, {"type": "text", "text": "```tool_result call_id=call_missing\nstale\n```"})

    def test_text_preserves_list_content(self):
        msgs = [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_missing", "content": [
                    {"type": "text", "text": "L1"}, {"type": "text", "text": "L2"},
                ]},
            ]},
        ]
        data = {"model": "gpt", "messages": msgs}
        _fix_orphan_tool_result(data, {"orphan_tool_result": {"strategy": "text"}})
        self.assertEqual(
            data["messages"][0]["content"][0]["text"],
            "```tool_result call_id=call_missing\nL1\nL2\n```",
        )

    def test_paired_untouched_in_drop(self):
        data = {"model": "gpt", "messages": _paired()}
        n = _fix_orphan_tool_result(data, {"orphan_tool_result": {"strategy": "drop"}})
        self.assertEqual(n, 0)
        self.assertEqual(data["messages"][2]["content"][0]["type"], "tool_result")

    def test_paired_untouched_in_text(self):
        data = {"model": "gpt", "messages": _paired()}
        n = _fix_orphan_tool_result(data, {"orphan_tool_result": {"strategy": "text"}})
        self.assertEqual(n, 0)
        self.assertEqual(data["messages"][2]["content"][0]["type"], "tool_result")

    def test_no_messages_key_no_op(self):
        data = {"model": "gpt", "input": [{"type": "function_call_output", "call_id": "x", "output": "y"}]}
        n = _fix_orphan_tool_result(data, {"orphan_tool_result": {"strategy": "drop"}})
        self.assertEqual(n, 0)

    def test_model_contains_filter_skips_nonmatching(self):
        data = {"model": "github_copilot/claude-opus-4.8", "messages": _orphan()}
        n = _fix_orphan_tool_result(
            data, {"orphan_tool_result": {"strategy": "drop", "model_contains": "gpt"}}
        )
        self.assertEqual(n, 0)
        self.assertEqual(data["messages"][1]["content"][0]["type"], "tool_result")

    def test_model_contains_filter_applies_to_matching(self):
        data = {"model": "github_copilot/gpt-5.6-sol", "messages": _orphan()}
        n = _fix_orphan_tool_result(
            data, {"orphan_tool_result": {"strategy": "drop", "model_contains": "gpt"}}
        )
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
