"""thinking block 请求侧修复测试 —— 结构化 action + 日志摘要。

两层:
- fix_thinking_blocks 返回的 action 元组:每类修复(连续插空格/空签名转文本/空签名删除/
  strip_all 剥离/占位补白)都精确记录 kind + msg + 结果形态(转文本字符数、删除原因、
  剥离类型细分),且真正改动了 message content。
- _summarize_actions 纯逻辑:把 action 聚合成一行带结果形态的可读摘要;并验证 warning
  日志确实带上这行摘要(而非旧版只有一个总数)。
运行:python3 -m unittest hookpkg.tests.test_thinking -v  (从 litellm 根目录)"""
from __future__ import annotations

import logging
import unittest
from unittest import mock

from hookpkg.thinking import fix_thinking_blocks, _summarize_actions


def _thinking(text="reasoning...", signature="sig"):
    b = {"type": "thinking", "thinking": text}
    if signature is not None:
        b["signature"] = signature
    return b


def _msg(content):
    return {"role": "assistant", "content": content}


def _kinds(actions):
    return [a["kind"] for a in actions]


class TestConsecutive(unittest.TestCase):
    def test_inserts_spacer_between_adjacent_thinking(self):
        data = {"model": "gpt", "messages": [_msg([_thinking("a"), _thinking("b")])]}
        actions = fix_thinking_blocks(data, {"fix_thinking": {"insert_text": True, "empty_signature": "off"}})
        self.assertEqual(_kinds(actions), ["consecutive"])
        self.assertEqual(actions[0]["msg"], 0)
        # 结果:两 thinking 之间插了一个空格文本块
        content = data["messages"][0]["content"]
        self.assertEqual([b["type"] for b in content], ["thinking", "text", "thinking"])
        self.assertEqual(content[1], {"type": "text", "text": " "})

    def test_non_adjacent_thinking_not_touched(self):
        data = {"model": "gpt", "messages": [_msg([_thinking("a"), {"type": "text", "text": "x"}, _thinking("b")])]}
        actions = fix_thinking_blocks(data, {"fix_thinking": {"insert_text": True, "empty_signature": "off"}})
        self.assertEqual(actions, ())
        self.assertEqual(len(data["messages"][0]["content"]), 3)


class TestEmptySignature(unittest.TestCase):
    def test_to_text_records_char_count(self):
        data = {"model": "gpt", "messages": [_msg([_thinking("hello world", signature="")])]}
        actions = fix_thinking_blocks(data, {"fix_thinking": {"insert_text": False, "empty_signature": "to_text"}})
        self.assertEqual(_kinds(actions), ["to_text"])
        self.assertEqual(actions[0]["chars"], len("hello world"))
        # 结果:thinking 降级为同内容的 text 块
        self.assertEqual(data["messages"][0]["content"], [{"type": "text", "text": "hello world"}])

    def test_to_text_empty_content_is_dropped_empty(self):
        data = {"model": "gpt", "messages": [_msg([_thinking("", signature=""), {"type": "text", "text": "x"}])]}
        actions = fix_thinking_blocks(data, {"fix_thinking": {"insert_text": False, "empty_signature": "to_text"}})
        self.assertEqual(_kinds(actions), ["dropped"])
        self.assertEqual(actions[0]["mode"], "empty")
        self.assertEqual(data["messages"][0]["content"], [{"type": "text", "text": "x"}])

    def test_remove_mode_is_dropped_remove(self):
        data = {"model": "gpt", "messages": [_msg([_thinking("keeps content", signature=""), {"type": "text", "text": "x"}])]}
        actions = fix_thinking_blocks(data, {"fix_thinking": {"insert_text": False, "empty_signature": "remove"}})
        self.assertEqual(_kinds(actions), ["dropped"])
        self.assertEqual(actions[0]["mode"], "remove")
        self.assertEqual(data["messages"][0]["content"], [{"type": "text", "text": "x"}])

    def test_valid_signature_untouched(self):
        data = {"model": "gpt", "messages": [_msg([_thinking("ok", signature="present")])]}
        actions = fix_thinking_blocks(data, {"fix_thinking": {"insert_text": False, "empty_signature": "to_text"}})
        self.assertEqual(actions, ())

    def test_emptying_content_refills_placeholder(self):
        data = {"model": "gpt", "messages": [_msg([_thinking("", signature="")])]}
        actions = fix_thinking_blocks(data, {"fix_thinking": {"insert_text": False, "empty_signature": "to_text"}})
        self.assertEqual(_kinds(actions), ["dropped", "placeholder"])
        self.assertEqual(data["messages"][0]["content"], [{"type": "text", "text": " "}])


class TestStripAll(unittest.TestCase):
    def test_strips_thinking_and_redacted_with_type_breakdown(self):
        content = [_thinking("a"), {"type": "redacted_thinking", "data": "xx"}, {"type": "text", "text": "keep"}]
        data = {"model": "gpt", "messages": [_msg(content)]}
        actions = fix_thinking_blocks(data, {"fix_thinking": {"strip_all": True}})
        self.assertEqual(_kinds(actions), ["strip", "strip"])
        self.assertEqual({a["block"] for a in actions}, {"thinking", "redacted_thinking"})
        self.assertEqual(data["messages"][0]["content"], [{"type": "text", "text": "keep"}])

    def test_strip_all_takes_priority_over_empty_sig(self):
        # strip_all 开启时,即便有空签名也走剥离而非 to_text
        data = {"model": "gpt", "messages": [_msg([_thinking("a", signature=""), {"type": "text", "text": "x"}])]}
        actions = fix_thinking_blocks(data, {"fix_thinking": {"strip_all": True, "empty_signature": "to_text"}})
        self.assertEqual(_kinds(actions), ["strip"])


class TestSummaryAndLog(unittest.TestCase):
    def test_summary_lists_each_action_form_and_msgs(self):
        actions = (
            {"kind": "to_text", "msg": 0, "chars": 100},
            {"kind": "to_text", "msg": 2, "chars": 50},
            {"kind": "dropped", "msg": 2, "mode": "empty"},
            {"kind": "consecutive", "msg": 5},
            {"kind": "placeholder", "msg": 0},
        )
        summary = _summarize_actions(actions)
        self.assertIn("→text×2 (150c)", summary)
        self.assertIn("→dropped×1 (1 empty)", summary)
        self.assertIn("consecutive-split×1", summary)
        self.assertIn("placeholder-refill×1", summary)
        # msgs 仅统计真正的问题(排除 placeholder 补白),去重排序
        self.assertIn("msgs=[0, 2, 5]", summary)

    def test_warning_log_carries_the_breakdown(self):
        data = {"model": "gpt", "messages": [_msg([_thinking("hi", signature="")])]}
        with self.assertLogs("litellm.hookpkg.thinking", level="WARNING") as cm:
            fix_thinking_blocks(data, {"fix_thinking": {"insert_text": False, "empty_signature": "to_text"}})
        line = "\n".join(cm.output)
        self.assertIn("fixed 1 thinking block issue(s) on model='gpt'", line)
        self.assertIn("→text×1 (2c)", line)

    def test_no_log_when_nothing_fixed(self):
        data = {"model": "gpt", "messages": [_msg([{"type": "text", "text": "x"}])]}
        logger = logging.getLogger("litellm.hookpkg.thinking")
        with mock.patch.object(logger, "warning") as warn:
            actions = fix_thinking_blocks(data, {"fix_thinking": {"insert_text": True}})
        self.assertEqual(actions, ())
        warn.assert_not_called()


class TestGating(unittest.TestCase):
    def test_all_off_returns_empty(self):
        data = {"model": "gpt", "messages": [_msg([_thinking("a"), _thinking("b")])]}
        actions = fix_thinking_blocks(data, {"fix_thinking": {"insert_text": False, "empty_signature": "off", "strip_all": False}})
        self.assertEqual(actions, ())

    def test_missing_messages_returns_empty(self):
        actions = fix_thinking_blocks({"model": "gpt"}, {"fix_thinking": {"insert_text": True}})
        self.assertEqual(actions, ())


if __name__ == "__main__":
    unittest.main()
