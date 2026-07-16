"""stream_transform 集成测试(伪 SSE bytes fixture,无需 litellm 运行时)。
守接缝:无条件缓冲的普通块语义等价、degen-only 硬门控不误转 invoke、message_delta 早到不悬挂、
缓冲期 ping 透传。
运行:python3 -m unittest hookpkg.tests.test_stream_degen -v  (从 litellm 根目录)"""
import asyncio
import json
import unittest
from unittest import mock

from hookpkg import stream as stream_mod
from hookpkg.stream import stream_transform


def sse(event_type, **fields):
    """构造一个 Anthropic SSE bytes chunk。"""
    obj = {"type": event_type, **fields}
    return f"event: {event_type}\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()


def text_block(index, texts):
    """一个 text block 的事件序列:start + 每个 text_delta + stop。"""
    out = [sse("content_block_start", index=index,
               content_block={"type": "text", "text": ""})]
    for t in texts:
        out.append(sse("content_block_delta", index=index,
                       delta={"type": "text_delta", "text": t}))
    out.append(sse("content_block_stop", index=index))
    return out


async def _collect(chunks, cfg):
    async def gen():
        for c in chunks:
            yield c
    with mock.patch.object(stream_mod, "load_config", return_value=cfg):
        out = []
        async for c in stream_transform(gen(), {"model": "test"}):
            out.append(c)
        return out


def run(chunks, cfg):
    return asyncio.run(_collect(chunks, cfg))


def parse_events(out_chunks):
    evs = []
    for c in out_chunks:
        raw = c.decode() if isinstance(c, (bytes, bytearray)) else c
        for line in raw.split("\n"):
            line = line.strip()
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
                if payload and payload != "[DONE]":
                    try:
                        obj = json.loads(payload)
                        if isinstance(obj, dict) and "type" in obj:
                            evs.append(obj)
                    except Exception:
                        pass
    return evs


def joined_text(evs):
    return "".join(e.get("delta", {}).get("text", "")
                   for e in evs if e.get("type") == "content_block_delta"
                   and e.get("delta", {}).get("type") == "text_delta")


CFG_PLAIN = {"stream_fix": {"enabled": True}}  # 缓冲无条件,degen/convert 都关
CFG_DEGEN = {"stream_fix": {"enabled": True,
                            "degen_trim": {"enabled": True, "notice": "[裁剪]"}}}
CFG_LIVE = {"stream_fix": {"enabled": True,
                           "degen_trim": {"enabled": True, "mode": "live", "notice": "[裁剪]"}}}


class TestStreamDegen(unittest.TestCase):
    def test_plain_block_semantic_equivalence(self):
        """普通 block(无退化无 invoke)经无条件缓冲后语义等价:拼接文本一致、index 单调、
        block 计数一致。chunk 边界可变,故不断言逐字节。"""
        chunks = (text_block(0, ["Hello, ", "world", "!"])
                  + [sse("message_delta", delta={"stop_reason": "end_turn"})])
        out = run(chunks, CFG_PLAIN)
        evs = parse_events(out)
        self.assertEqual(joined_text(evs), "Hello, world!")
        starts = [e for e in evs if e["type"] == "content_block_start"]
        stops = [e for e in evs if e["type"] == "content_block_stop"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(stops), 1)
        idxs = [e["index"] for e in evs if "index" in e]
        self.assertEqual(idxs, sorted(idxs))

    def test_degen_only_folds_and_no_invoke_conversion(self):
        """degen-only(convert_invoke 关):退化折叠;文本里正常 <invoke> 字样不被误转 tool_use。"""
        leak = 'Use <invoke name="Foo"><parameter name="x">1</parameter></invoke> like this.'
        degen_text = "court\n\ncourt\n\ncourt\n\ncourt\n\n" + leak
        chunks = (text_block(0, [degen_text])
                  + [sse("message_delta", delta={"stop_reason": "end_turn"})])
        out = run(chunks, CFG_DEGEN)
        evs = parse_events(out)
        # 无 tool_use block 生成(硬门控)
        tool_starts = [e for e in evs if e["type"] == "content_block_start"
                       and e.get("content_block", {}).get("type") == "tool_use"]
        self.assertEqual(tool_starts, [], "convert_invoke 关时绝不生成 tool_use")
        txt = joined_text(evs)
        self.assertIn("[裁剪]", txt)
        self.assertEqual(txt.count("court"), 1)
        self.assertIn("<invoke", txt)  # invoke 字样原样保留在文本里

    def test_message_delta_before_stop_no_hang(self):
        """message_delta 早于 content_block_stop 到达:缓冲 text 在 message_delta 之前外发。"""
        chunks = [
            sse("content_block_start", index=0, content_block={"type": "text", "text": ""}),
            sse("content_block_delta", index=0, delta={"type": "text_delta", "text": "buffered"}),
            # 上游违约:没发 content_block_stop 就来 message_delta
            sse("message_delta", delta={"stop_reason": "end_turn"}),
        ]
        out = run(chunks, CFG_PLAIN)
        evs = parse_events(out)
        types = [e["type"] for e in evs]
        # 缓冲文本(content_block_delta/start)必须在 message_delta 之前
        md_idx = types.index("message_delta")
        text_idxs = [i for i, e in enumerate(evs)
                     if e["type"] == "content_block_delta"]
        self.assertTrue(text_idxs, "缓冲文本必须外发")
        self.assertTrue(all(i < md_idx for i in text_idxs),
                        "缓冲文本必须在 message_delta 之前,不悬挂")
        self.assertEqual(joined_text(evs), "buffered")

    def test_ping_during_buffer_passes_through(self):
        """缓冲期插入不可解析 chunk(ping):ping 透传,文本块随后完整成段。"""
        ping = b"event: ping\ndata: [DONE]\n\n"  # sse_parse -> None(走 ev is None 分支,守 R3)
        chunks = [
            sse("content_block_start", index=0, content_block={"type": "text", "text": ""}),
            sse("content_block_delta", index=0, delta={"type": "text_delta", "text": "hi"}),
            ping,
            sse("content_block_stop", index=0),
            sse("message_delta", delta={"stop_reason": "end_turn"}),
        ]
        out = run(chunks, CFG_PLAIN)
        # ping 原样出现在输出里
        self.assertIn(ping, out)
        # 次序契约(当前行为,F3):ping 是不可解析 chunk,ev is None 分支不 flush tbuf,
        # 故 ping 先于缓冲文本外发。固化此契约,未来若变化能被察觉。
        ping_pos = out.index(ping)
        text_positions = [i for i, c in enumerate(out)
                          if b"text_delta" in (c if isinstance(c, bytes) else c.encode())]
        self.assertTrue(all(ping_pos < tp for tp in text_positions),
                        "当前契约:不可解析 chunk 先于缓冲文本(非终止事件,无害)")
        evs = parse_events(out)
        self.assertEqual(joined_text(evs), "hi")
        stops = [e for e in evs if e["type"] == "content_block_stop"]
        self.assertEqual(len(stops), 1)

    def test_fold_exception_still_emits_text(self):
        """F1 回归:折叠期抛异常绝不丢块——降级为原样重发缓冲文本。
        用非法 degen_kwargs(min_run 由 config 传 null)在 degen.py 已兜底不崩;
        这里 mock fold_degenerate 直接抛,验证 stream 层降级路径本身。"""
        real_text = "important content that must not vanish"
        chunks = (text_block(0, [real_text])
                  + [sse("message_delta", delta={"stop_reason": "end_turn"})])
        with mock.patch.object(stream_mod, "fold_degenerate",
                               side_effect=RuntimeError("boom")):
            out = run(chunks, CFG_DEGEN)
        evs = parse_events(out)
        self.assertEqual(joined_text(evs), real_text,
                         "折叠异常时真实文本必须完整外发,绝不蒸发")
        stops = [e for e in evs if e["type"] == "content_block_stop"]
        self.assertEqual(len(stops), 1)

    def test_convert_invoke_still_works(self):
        """回归:convert_invoke 开启时,泄漏的 <invoke> 仍转成真实 tool_use block。"""
        leak = ('前言\n\n<function_calls>\n<invoke name="Bash">'
                '<parameter name="command">ls</parameter></invoke>\n</function_calls>')
        cfg = {"stream_fix": {"enabled": True, "convert_text_invoke": True,
                              "convert_text_invoke_tools": ["Bash"]}}
        chunks = (text_block(0, [leak])
                  + [sse("message_delta", delta={"stop_reason": "end_turn"})])
        out = run(chunks, cfg)
        evs = parse_events(out)
        tool_starts = [e for e in evs if e["type"] == "content_block_start"
                       and e.get("content_block", {}).get("type") == "tool_use"]
        self.assertEqual(len(tool_starts), 1)
        self.assertEqual(tool_starts[0]["content_block"]["name"], "Bash")
        # message_delta 的 stop_reason 被改成 tool_use
        md = [e for e in evs if e["type"] == "message_delta"]
        self.assertEqual(md[0]["delta"]["stop_reason"], "tool_use")

    def test_leaked_askuserquestion_fills_missing_question(self):
        """回归:AskUserQuestion 以泄漏 <invoke> 形式出现且 questions[].question 缺失时,
        泄漏转换路径必须和缓冲路径一样跑 apply_item_fixes——questions 从字符串 coerce 成数组、
        question 从 header 补。修复前 synth 直接透传原始 input(questions 是字符串、无 question),
        客户端报 `questions[0].question is missing`。"""
        leak = ('<function_calls>\n<invoke name="AskUserQuestion">'
                '<parameter name="questions">'
                '[{"header":"Auth method","options":[{"label":"OAuth"}]}]'
                '</parameter></invoke>\n</function_calls>')
        cfg = {"stream_fix": {"enabled": True, "convert_text_invoke": True,
                              "convert_text_invoke_tools": ["AskUserQuestion"],
                              "tools": {"AskUserQuestion": {
                                  "items_key": "questions",
                                  "copy_within_items": [{"src": "header", "dst": "question"}]}}}}
        chunks = (text_block(0, [leak])
                  + [sse("message_delta", delta={"stop_reason": "end_turn"})])
        out = run(chunks, cfg)
        evs = parse_events(out)
        tool_starts = [e for e in evs if e["type"] == "content_block_start"
                       and e.get("content_block", {}).get("type") == "tool_use"]
        self.assertEqual(len(tool_starts), 1)
        self.assertEqual(tool_starts[0]["content_block"]["name"], "AskUserQuestion")
        # 合成 tool_use 的 input 在 input_json_delta 的 partial_json 里
        deltas = [e for e in evs if e["type"] == "content_block_delta"
                  and e.get("delta", {}).get("type") == "input_json_delta"]
        self.assertEqual(len(deltas), 1)
        inp = json.loads(deltas[0]["delta"]["partial_json"])
        # 修复核心:questions 被 coerce 成数组,且缺失的 question 从 header 补上
        self.assertIsInstance(inp["questions"], list, "questions 必须 coerce 成数组")
        self.assertEqual(inp["questions"][0]["question"], "Auth method",
                         "缺失的 question 必须从 header 补全")

    def test_degen_plus_convert_invoke_order(self):
        """回归:degen 与 convert_invoke 同开——先折叠退化,再转 invoke,二者不冲突。"""
        text = ('court\n\ncourt\n\ncourt\n\ncourt\n\n'
                '<function_calls>\n<invoke name="Bash">'
                '<parameter name="command">ls</parameter></invoke>\n</function_calls>')
        cfg = {"stream_fix": {"enabled": True, "convert_text_invoke": True,
                              "convert_text_invoke_tools": ["Bash"],
                              "degen_trim": {"enabled": True, "notice": "[裁剪]"}}}
        chunks = (text_block(0, [text])
                  + [sse("message_delta", delta={"stop_reason": "end_turn"})])
        out = run(chunks, cfg)
        evs = parse_events(out)
        # 退化被折叠
        self.assertIn("[裁剪]", joined_text(evs))
        self.assertEqual(joined_text(evs).count("court"), 1)
        # invoke 被转
        tool_starts = [e for e in evs if e["type"] == "content_block_start"
                       and e.get("content_block", {}).get("type") == "tool_use"]
        self.assertEqual(len(tool_starts), 1)

    def test_disabled_passthrough(self):
        """stream_fix.enabled 关:完全透传。"""
        chunks = text_block(0, ["a", "b"])
        out = run(chunks, {"stream_fix": {"enabled": False}})
        self.assertEqual(out, chunks)


def _all_data_lines_parse(out_chunks):
    """遍历输出的每条 SSE `data:` 行,断言都能 json.loads([DONE] 除外)。
    返回无法解析的 payload 列表(空 = 全部合法)。"""
    bad = []
    for c in out_chunks:
        raw = c.decode() if isinstance(c, (bytes, bytearray)) else c
        for line in raw.split("\n"):
            line = line.strip()
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    json.loads(payload)
                except Exception:
                    bad.append(payload)
    return bad


class TestTruncatedFrameDrop(unittest.TestCase):
    """回归:上游中途断流的半截 JSON 帧必须被丢弃,不能原样转发给客户端
    (否则客户端报 "JSON Parse error: Unterminated string")。"""

    def test_truncated_trailing_frame_not_forwarded(self):
        """复现真实故障:message_start + text start@1 后,上游吐一个未闭合字符串的半截
        content_block_delta 帧就断流。修复前该帧被原样转发 → 客户端 Unterminated string。
        修复后:半截帧被丢弃,输出的每条 data 行都能解析,缓冲块仍被干净收尾。"""
        truncated = (b'event: content_block_delta\n'
                     b'data: {"type":"content_block_delta","index":1,'
                     b'"delta":{"type":"text_delta","text":"hello wor')
        chunks = [
            sse("message_start", message={"id": "m", "role": "assistant", "content": []}),
            sse("content_block_start", index=1, content_block={"type": "text", "text": ""}),
            truncated,
        ]
        out = run(chunks, CFG_PLAIN)
        # 核心断言:没有任何不可解析的 data 行(修复前这里会有 "hello wor 的半截 JSON)
        self.assertEqual(_all_data_lines_parse(out), [],
                         "半截 JSON 帧不得转发给客户端")
        # 半截帧的字节整体不得出现在输出里(既不改写也不透传,直接丢弃)
        self.assertNotIn(truncated, out)
        joined = b"".join(c if isinstance(c, bytes) else c.encode() for c in out)
        self.assertNotIn(b"hello wor", joined, "截断内容不得泄漏到客户端流")
        # 缓冲的 text block 仍被干净收尾(start + stop 齐全)
        evs = parse_events(out)
        starts = [e for e in evs if e["type"] == "content_block_start"]
        stops = [e for e in evs if e["type"] == "content_block_stop"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(stops), 1)

    def test_truncated_frame_during_tool_buffer_dropped(self):
        """缓冲匹配工具期间上游吐半截帧就断流:半截帧丢弃,工具块由收尾补 stop。"""
        cfg = {"stream_fix": {"enabled": True,
                              "tools": {"AskUserQuestion": {"items_key": "questions"}}}}
        truncated = (b'event: content_block_delta\n'
                     b'data: {"type":"content_block_delta","index":0,'
                     b'"delta":{"type":"input_json_delta","partial_json":"{\\"que')
        chunks = [
            sse("content_block_start", index=0,
                content_block={"type": "tool_use", "id": "t", "name": "AskUserQuestion", "input": {}}),
            truncated,
        ]
        out = run(chunks, cfg)
        self.assertEqual(_all_data_lines_parse(out), [])
        self.assertNotIn(truncated, out)
        evs = parse_events(out)
        self.assertEqual(len([e for e in evs if e["type"] == "content_block_stop"]), 1)

    def test_done_and_ping_still_pass_through(self):
        """合法的不可解析帧([DONE] / 以 [DONE] 为 data 的 ping)仍须原样透传,不被误丢。"""
        done = b"data: [DONE]\n\n"
        ping = b"event: ping\ndata: [DONE]\n\n"
        chunks = [
            sse("content_block_start", index=0, content_block={"type": "text", "text": ""}),
            sse("content_block_delta", index=0, delta={"type": "text_delta", "text": "hi"}),
            ping,
            sse("content_block_stop", index=0),
            done,
        ]
        out = run(chunks, CFG_PLAIN)
        self.assertIn(ping, out, "ping 不得被误判为截断帧丢弃")
        self.assertIn(done, out, "[DONE] 不得被误判为截断帧丢弃")
        self.assertEqual(joined_text(parse_events(out)), "hi")


class TestIsTruncatedJsonFrame(unittest.TestCase):
    """判别函数 is_truncated_json_frame 的边界(直接单测,守住 [DONE]/ping/对象前缀等分叉)。"""

    def test_truncated_object_frame(self):
        from hookpkg.sse import is_truncated_json_frame
        self.assertTrue(is_truncated_json_frame(
            b'event: content_block_delta\ndata: {"type":"x","text":"hello wor'))

    def test_done_is_not_truncated(self):
        from hookpkg.sse import is_truncated_json_frame
        self.assertFalse(is_truncated_json_frame(b"data: [DONE]\n\n"))
        self.assertFalse(is_truncated_json_frame(b"event: ping\ndata: [DONE]\n\n"))

    def test_valid_object_frame_is_not_truncated(self):
        from hookpkg.sse import is_truncated_json_frame
        self.assertFalse(is_truncated_json_frame(
            b'event: ping\ndata: {"type":"ping"}\n\n'))

    def test_no_data_line_is_not_truncated(self):
        from hookpkg.sse import is_truncated_json_frame
        self.assertFalse(is_truncated_json_frame(b"event: content_block_del"))
        self.assertFalse(is_truncated_json_frame(b": keepalive comment\n\n"))
        self.assertFalse(is_truncated_json_frame(b"\n\n"))

    def test_truncated_at_utf8_boundary(self):
        """尾部在多字节 UTF-8 边界被截断:仍能识别 data: { 前缀并判为截断。"""
        from hookpkg.sse import is_truncated_json_frame
        frame = ('event: content_block_delta\ndata: {"type":"x","text":"你好世'
                 .encode("utf-8"))[:-1]  # 砍掉最后一个字节 → 半个汉字
        self.assertTrue(is_truncated_json_frame(frame))


class TestLiveDedupStream(unittest.TestCase):
    """live 去重经 stream_transform 的集成:边流边外发、退化抑制、恢复保留、block 结构完好。"""

    def test_live_suppresses_and_keeps_recovery(self):
        """退化块 live 去重:前缀 + 恢复保留、court 抑制到 min_run-1、notice、单 start/stop。"""
        pieces = ["prefix line\n\n"] + ["court\n\n"] * 10 + ["recovered and dispatched\n\n"]
        chunks = (text_block(0, pieces)
                  + [sse("message_delta", delta={"stop_reason": "end_turn"})])
        out = run(chunks, CFG_LIVE)
        evs = parse_events(out)
        txt = joined_text(evs)
        self.assertIn("prefix line", txt)
        self.assertIn("recovered and dispatched", txt)
        self.assertEqual(txt.count("court"), 3, "min_run-1=3 个重复流出,其余抑制")
        self.assertIn("[裁剪]", txt)
        starts = [e for e in evs if e["type"] == "content_block_start"]
        stops = [e for e in evs if e["type"] == "content_block_stop"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(stops), 1)
        # message_delta 原样透传(不改 stop_reason;live 不结束 turn)
        md = [e for e in evs if e["type"] == "message_delta"]
        self.assertEqual(md[0]["delta"]["stop_reason"], "end_turn")

    def test_live_streams_per_segment(self):
        """live 逐段外发(非整块缓冲成一个 delta):3 段落 → 多个 content_block_delta。"""
        pieces = ["Para one.\n\n", "Para two.\n\n", "Para three.\n\n"]
        chunks = (text_block(0, pieces)
                  + [sse("message_delta", delta={"stop_reason": "end_turn"})])
        out = run(chunks, CFG_LIVE)
        evs = parse_events(out)
        deltas = [e for e in evs if e["type"] == "content_block_delta"]
        self.assertGreater(len(deltas), 1, "live 应逐段外发,非缓冲成单 delta")
        self.assertEqual(joined_text(evs), "Para one.\n\nPara two.\n\nPara three.\n\n")

    def test_live_message_delta_before_stop_flushes(self):
        """live 块未闭合就来 message_delta(上游违约):尾段先 flush 外发 + 补 content_block_stop。"""
        chunks = [
            sse("content_block_start", index=0, content_block={"type": "text", "text": ""}),
            sse("content_block_delta", index=0,
                delta={"type": "text_delta", "text": "trailing no newline"}),
            sse("message_delta", delta={"stop_reason": "end_turn"}),
        ]
        out = run(chunks, CFG_LIVE)
        evs = parse_events(out)
        self.assertEqual(joined_text(evs), "trailing no newline")
        types = [e["type"] for e in evs]
        md_idx = types.index("message_delta")
        d_idxs = [i for i, e in enumerate(evs) if e["type"] == "content_block_delta"]
        self.assertTrue(d_idxs and all(i < md_idx for i in d_idxs),
                        "flush 的尾段须在 message_delta 之前外发")
        # BLOCKER:被 message_delta 打断的 live 块也必须补 content_block_stop(否则非法 SSE)
        starts = [e for e in evs if e["type"] == "content_block_start"]
        stops = [e for e in evs if e["type"] == "content_block_stop"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(stops), 1, "live 块被 message_delta 打断也须闭合")

    def test_live_eof_truncation_closes_block(self):
        """流在 live 块未闭合时截断(无 stop、无 message_delta):收尾补 flush + content_block_stop。"""
        chunks = [
            sse("content_block_start", index=0, content_block={"type": "text", "text": ""}),
            sse("content_block_delta", index=0,
                delta={"type": "text_delta", "text": "partial tail no newline"}),
        ]
        out = run(chunks, CFG_LIVE)
        evs = parse_events(out)
        self.assertIn("partial tail no newline", joined_text(evs))
        starts = [e for e in evs if e["type"] == "content_block_start"]
        stops = [e for e in evs if e["type"] == "content_block_stop"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(stops), 1, "截断也须补 content_block_stop")

    def test_live_new_start_while_open_closes_prior(self):
        """live 块未闭合就来新 content_block_start(上游违约):先闭合旧块(不丢 pending),两块都闭合。"""
        chunks = [
            sse("content_block_start", index=0, content_block={"type": "text", "text": ""}),
            sse("content_block_delta", index=0,
                delta={"type": "text_delta", "text": "block zero tail"}),
            # 未发 stop 就开新块
            sse("content_block_start", index=1, content_block={"type": "text", "text": ""}),
            sse("content_block_delta", index=1, delta={"type": "text_delta", "text": "block one\n\n"}),
            sse("content_block_stop", index=1),
            sse("message_delta", delta={"stop_reason": "end_turn"}),
        ]
        out = run(chunks, CFG_LIVE)
        evs = parse_events(out)
        txt = joined_text(evs)
        self.assertIn("block zero tail", txt, "旧块 pending 不丢")
        self.assertIn("block one", txt)
        starts = [e for e in evs if e["type"] == "content_block_start"]
        stops = [e for e in evs if e["type"] == "content_block_stop"]
        self.assertEqual(len(starts), 2)
        self.assertEqual(len(stops), 2, "两个块都须闭合")

    def test_live_feed_exception_preserves_pending(self):
        """BLOCKER 回归:live.feed 抛异常时,先前滞留在 _pending 的真实文本不丢、block 仍闭合。"""
        from hookpkg.degen import LiveDedup
        chunks = [
            sse("content_block_start", index=0, content_block={"type": "text", "text": ""}),
            sse("content_block_delta", index=0,
                delta={"type": "text_delta", "text": "important-prefix"}),  # 无 \n\n → 留 pending
            sse("content_block_delta", index=0, delta={"type": "text_delta", "text": " and more"}),
            sse("content_block_stop", index=0),
            sse("message_delta", delta={"stop_reason": "end_turn"}),
        ]
        orig_feed = LiveDedup.feed
        calls = [0]

        def flaky(self, t):
            calls[0] += 1
            if calls[0] == 2:
                raise RuntimeError("boom")
            return orig_feed(self, t)

        with mock.patch.object(LiveDedup, "feed", flaky):
            out = run(chunks, CFG_LIVE)
        evs = parse_events(out)
        self.assertIn("important-prefix", joined_text(evs),
                      "异常时 _pending 已滞留的真实文本必须完整外发,绝不蒸发")
        stops = [e for e in evs if e["type"] == "content_block_stop"]
        self.assertEqual(len(stops), 1, "降级后真 stop 仍闭合 block")

    def _flaky_feed_on_second(self):
        """返回 (LiveDedup, flaky_fn):第 2 次 feed 抛异常,其余走原实现。"""
        from hookpkg.degen import LiveDedup
        orig = LiveDedup.feed
        calls = [0]

        def flaky(self, t):
            calls[0] += 1
            if calls[0] == 2:
                raise RuntimeError("boom")
            return orig(self, t)
        return LiveDedup, flaky

    def test_live_feed_exception_then_eof_closes_block(self):
        """新 BLOCKER 回归:feed 降级后流截断(无真 stop)→ block 仍须补 content_block_stop。"""
        LiveDedup, flaky = self._flaky_feed_on_second()
        chunks = [
            sse("content_block_start", index=0, content_block={"type": "text", "text": ""}),
            sse("content_block_delta", index=0, delta={"type": "text_delta", "text": "pending"}),
            sse("content_block_delta", index=0, delta={"type": "text_delta", "text": " current"}),
            # EOF:无 content_block_stop
        ]
        with mock.patch.object(LiveDedup, "feed", flaky):
            out = run(chunks, CFG_LIVE)
        evs = parse_events(out)
        self.assertIn("pending", joined_text(evs))
        starts = [e for e in evs if e["type"] == "content_block_start"]
        stops = [e for e in evs if e["type"] == "content_block_stop"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(stops), 1, "feed 降级后 EOF 也须补 stop(不悬挂)")

    def test_live_feed_exception_then_message_delta_closes_block(self):
        """新 BLOCKER 回归:feed 降级后来 message_delta(无真 stop)→ block 仍须补 stop。"""
        LiveDedup, flaky = self._flaky_feed_on_second()
        chunks = [
            sse("content_block_start", index=0, content_block={"type": "text", "text": ""}),
            sse("content_block_delta", index=0, delta={"type": "text_delta", "text": "aaa"}),
            sse("content_block_delta", index=0, delta={"type": "text_delta", "text": " bbb"}),
            sse("message_delta", delta={"stop_reason": "end_turn"}),
        ]
        with mock.patch.object(LiveDedup, "feed", flaky):
            out = run(chunks, CFG_LIVE)
        evs = parse_events(out)
        self.assertIn("aaa", joined_text(evs))
        stops = [e for e in evs if e["type"] == "content_block_stop"]
        self.assertEqual(len(stops), 1, "feed 降级后 message_delta 也须补 stop")

    def test_live_feed_exception_then_new_start_closes_prior(self):
        """新 BLOCKER 回归:feed 降级后来新 content_block_start → 旧块补 stop、新块独立闭合。"""
        LiveDedup, flaky = self._flaky_feed_on_second()
        chunks = [
            sse("content_block_start", index=0, content_block={"type": "text", "text": ""}),
            sse("content_block_delta", index=0, delta={"type": "text_delta", "text": "aaa"}),
            sse("content_block_delta", index=0, delta={"type": "text_delta", "text": " bbb"}),
            sse("content_block_start", index=1, content_block={"type": "text", "text": ""}),
            sse("content_block_delta", index=1, delta={"type": "text_delta", "text": "ccc\n\n"}),
            sse("content_block_stop", index=1),
            sse("message_delta", delta={"stop_reason": "end_turn"}),
        ]
        with mock.patch.object(LiveDedup, "feed", flaky):
            out = run(chunks, CFG_LIVE)
        evs = parse_events(out)
        txt = joined_text(evs)
        self.assertIn("aaa", txt)
        self.assertIn("ccc", txt)
        starts = [e for e in evs if e["type"] == "content_block_start"]
        stops = [e for e in evs if e["type"] == "content_block_stop"]
        self.assertEqual(len(starts), 2)
        self.assertEqual(len(stops), 2, "两块都须闭合(feed 降级不丢块结构)")

    def test_live_citations_delta_no_orphan(self):
        """评审 Important:合法 citations_delta 不被当块异常——不提前补 stop、无 orphan;
        文本保留、citations 透传、block 恰好闭合一次。"""
        chunks = [
            sse("content_block_start", index=0, content_block={"type": "text", "text": ""}),
            sse("content_block_delta", index=0, delta={"type": "text_delta", "text": "hello"}),
            sse("content_block_delta", index=0,
                delta={"type": "citations_delta", "citation": {"x": 1}}),
            sse("content_block_stop", index=0),
            sse("message_delta", delta={"stop_reason": "end_turn"}),
        ]
        out = run(chunks, CFG_LIVE)
        evs = parse_events(out)
        self.assertIn("hello", joined_text(evs))
        starts = [e for e in evs if e["type"] == "content_block_start"]
        stops = [e for e in evs if e["type"] == "content_block_stop"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(stops), 1, "citations_delta 不得导致双 stop/orphan")
        cit = [e for e in evs if e.get("type") == "content_block_delta"
               and e.get("delta", {}).get("type") == "citations_delta"]
        self.assertEqual(len(cit), 1, "citations_delta 须原样透传")

    def test_live_flush_exception_on_close_preserves_pending(self):
        """评审第三轮 BLOCKER 回归:收尾时 live.flush() 抛异常,尾文经 drain_raw 仍完整外发 +
        补 content_block_stop(绝不因 flush 异常吞内容)。覆盖 EOF 收尾路径。"""
        from hookpkg.degen import LiveDedup
        chunks = [
            sse("content_block_start", index=0, content_block={"type": "text", "text": ""}),
            sse("content_block_delta", index=0, delta={"type": "text_delta", "text": "MUST_KEEP"}),
            # EOF:无 content_block_stop,收尾走 _live_close_events
        ]
        with mock.patch.object(LiveDedup, "flush", side_effect=RuntimeError("flush boom")):
            out = run(chunks, CFG_LIVE)
        evs = parse_events(out)
        self.assertIn("MUST_KEEP", joined_text(evs),
                      "flush 异常时尾文仍须经 drain_raw 完整外发,绝不蒸发")
        starts = [e for e in evs if e["type"] == "content_block_start"]
        stops = [e for e in evs if e["type"] == "content_block_stop"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(stops), 1, "flush 异常收尾仍须恰好一条 stop")

    def test_live_non_degen_byte_preserved(self):
        """live 下非退化文本字节级还原(单换行、三换行、真空段不损)。"""
        text = "alpha\nbeta\n\ngamma\n\n\ndelta\n\n\n\nepsilon"
        pieces = [text[i:i + 4] for i in range(0, len(text), 4)]
        chunks = (text_block(0, pieces)
                  + [sse("message_delta", delta={"stop_reason": "end_turn"})])
        out = run(chunks, CFG_LIVE)
        self.assertEqual(joined_text(parse_events(out)), text)

    def test_live_disabled_when_convert_invoke_on(self):
        """live 与 convert_invoke 互斥:两者同开时回落 buffered(convert_invoke 需整块缓冲),
        退化仍被 buffered fold 处理成 1 次。"""
        cfg = {"stream_fix": {"enabled": True, "convert_text_invoke": True,
                              "convert_text_invoke_tools": ["Bash"],
                              "degen_trim": {"enabled": True, "mode": "live", "notice": "[裁剪]"}}}
        pieces = ["court\n\n"] * 8
        chunks = (text_block(0, pieces)
                  + [sse("message_delta", delta={"stop_reason": "end_turn"})])
        out = run(chunks, cfg)
        txt = joined_text(parse_events(out))
        # buffered fold 把 8 次折成 1 次(非 live 的 min_run-1=3)
        self.assertEqual(txt.count("court"), 1)
        self.assertIn("[裁剪]", txt)


if __name__ == "__main__":
    unittest.main()


class TestSSEFrameReassembly(unittest.TestCase):
    """回归:上游按网络 chunk 边界产出 bytes,可能把一个 SSE 帧劈到两个 chunk,或把多帧
    挤进一个 chunk。进入主循环前必须重组成「一 chunk 一完整帧」,否则半截帧被 sse_parse
    判 None -> 丢弃(丢内容),或半截 `event:` 泄漏给客户端(`Unexpected identifier "event"`)。"""

    def test_frame_split_across_chunks_keeps_all_deltas(self):
        frames = text_block(0, ["hello ", "world"])
        blob = b"".join(frames)
        # 在最后一个 delta 帧的 data JSON 内部切开(mid-frame),制造帧跨 chunk。
        cut = blob.index(b"world") + 2  # 落在 "world" 中间
        chunks = [blob[:cut], blob[cut:]]
        evs = parse_events(run(chunks, CFG_PLAIN))
        self.assertEqual(joined_text(evs), "hello world")

    def test_many_random_cuts_never_lose_text(self):
        frames = text_block(0, ["alpha ", "beta ", "gamma"])
        blob = b"".join(frames)
        for cut in range(1, len(blob)):
            chunks = [blob[:cut], blob[cut:]]
            evs = parse_events(run(chunks, CFG_PLAIN))
            self.assertEqual(joined_text(evs), "alpha beta gamma", f"lost text at cut={cut}")

    def test_multiple_frames_in_one_chunk_all_emitted(self):
        # 两个完整 delta 帧挤进同一个 chunk。当前 sse_parse 只取第一个 data 行 -> 第二帧丢失。
        frames = text_block(0, ["one ", "two"])
        # 把中间两个 delta 帧合并成一个 chunk,首尾各自成 chunk。
        chunks = [frames[0], frames[1] + frames[2], frames[3]]
        evs = parse_events(run(chunks, CFG_PLAIN))
        self.assertEqual(joined_text(evs), "one two")

    def test_genuine_trailing_truncation_still_dropped(self):
        # 真·流末尾截断(半截 data 且后面再无 chunk):重组后仍是半截,交由既有截断丢弃逻辑处理,
        # 不得泄漏给客户端。断言输出里不含未闭合的 `partial_json` 半截字符串。
        good = text_block(0, ["ok"])
        truncated = b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"half'
        chunks = [*good, truncated]
        out = run(chunks, CFG_PLAIN)
        joined = b"".join(c if isinstance(c, (bytes, bytearray)) else c.encode() for c in out)
        self.assertNotIn(b'"text":"half', joined)


class TestReassemblyAudit(unittest.TestCase):
    """`reassembled_split_frame` 审计是「新代码生效」的正向可观测量:劈帧时必须出现。"""

    def test_audit_fires_on_split_frame(self):
        import hookpkg.stream as sm
        events = []
        frames = text_block(0, ["hello ", "world"])
        blob = b"".join(frames)
        cut = blob.index(b"world") + 2
        chunks = [blob[:cut], blob[cut:]]

        real_from = sm.ProbeContext.from_stream_fix

        def _spy_from(sf, **kw):
            ctx = real_from(sf, **kw)
            orig_audit = ctx.audit
            def _audit(ev, **f):
                events.append(ev)
                return orig_audit(ev, **f)
            ctx.audit = _audit
            return ctx

        with mock.patch.object(sm.ProbeContext, "from_stream_fix", staticmethod(_spy_from)):
            run(chunks, CFG_PLAIN)
        self.assertIn("reassembled_split_frame", events)

    def test_no_audit_when_frames_already_aligned(self):
        import hookpkg.stream as sm
        events = []
        frames = text_block(0, ["a", "b"])  # 每 chunk 一完整帧,无需拼接

        real_from = sm.ProbeContext.from_stream_fix

        def _spy_from(sf, **kw):
            ctx = real_from(sf, **kw)
            orig_audit = ctx.audit
            def _audit(ev, **f):
                events.append(ev)
                return orig_audit(ev, **f)
            ctx.audit = _audit
            return ctx

        with mock.patch.object(sm.ProbeContext, "from_stream_fix", staticmethod(_spy_from)):
            run(frames, CFG_PLAIN)
        self.assertNotIn("reassembled_split_frame", events)
