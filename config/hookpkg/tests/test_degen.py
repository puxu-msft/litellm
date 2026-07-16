"""degen.py 纯函数测试。用 unittest(标准库,无需 pytest)。
运行:python3 -m unittest hookpkg.tests.test_degen -v  (从 litellm 根目录)"""
import unittest
from unittest import mock

from hookpkg.degen import LiveDedup, fold_degenerate, notice_is_safe

N = "[裁剪]"  # 短 notice 便于断言


class TestFoldDegenerate(unittest.TestCase):
    def test_basic_court_sample(self):
        """样例 court\\n\\n×4 → 折叠为 1 次 + notice。"""
        text = "court\n\ncourt\n\ncourt\n\ncourt"
        out, n = fold_degenerate(text, notice=N)
        self.assertEqual(n, 1)
        self.assertEqual(out.count("court"), 1)
        self.assertIn(N, out)

    def test_below_min_run(self):
        """3 次 < min_run=4 → 不折叠。"""
        text = "court\n\ncourt\n\ncourt"
        out, n = fold_degenerate(text, notice=N)
        self.assertEqual(n, 0)
        self.assertEqual(out, text)

    def test_long_segment_not_folded(self):
        """段长 > max_seg_len → 不折叠。"""
        seg = "x" * 100
        text = "\n\n".join([seg] * 5)
        out, n = fold_degenerate(text, notice=N, max_seg_len=80)
        self.assertEqual(n, 0)
        self.assertEqual(out, text)

    def test_two_independent_degen(self):
        """一段内两处独立退化 → n_folded=2。"""
        text = ("a\n\na\n\na\n\na\n\n"
                "MIDDLE\n\n"
                "b\n\nb\n\nb\n\nb")
        out, n = fold_degenerate(text, notice=N)
        self.assertEqual(n, 2)
        self.assertEqual(out.count("MIDDLE"), 1)
        # a 与 b 各剩一次
        self.assertEqual(len([s for s in out.split("\n\n") if s.strip() == "a"]), 1)
        self.assertEqual(len([s for s in out.split("\n\n") if s.strip() == "b"]), 1)

    def test_normal_text_untouched(self):
        """正常无重复文本 → 原样。"""
        text = "alpha\n\nbeta\n\ngamma\n\ndelta"
        out, n = fold_degenerate(text, notice=N)
        self.assertEqual(n, 0)
        self.assertEqual(out, text)

    def test_degen_at_head(self):
        """退化在开头,后接正常文本 → 只折叠退化,尾部保留。"""
        text = "x\n\nx\n\nx\n\nx\n\nTAIL_A\n\nTAIL_B"
        out, n = fold_degenerate(text, notice=N)
        self.assertEqual(n, 1)
        self.assertTrue(out.rstrip().endswith("TAIL_B"))
        self.assertIn("TAIL_A", out)

    def test_degen_at_tail(self):
        """退化在末尾 → 前文保留,不多吞空行。"""
        text = "HEAD_A\n\nHEAD_B\n\ny\n\ny\n\ny\n\ny"
        out, n = fold_degenerate(text, notice=N)
        self.assertEqual(n, 1)
        self.assertTrue(out.startswith("HEAD_A\n\nHEAD_B"))
        self.assertEqual(out.count("y\n\ny"), 0)  # 无残留连续重复

    def test_run_broken_by_different_seg(self):
        """court×3 + X + court×4 → 前 3 未达阈值保留、后 4 折叠。"""
        text = ("court\n\ncourt\n\ncourt\n\n"
                "X\n\n"
                "court\n\ncourt\n\ncourt\n\ncourt")
        out, n = fold_degenerate(text, notice=N)
        # 后 4 个折叠成 1;前 3 个各自保留(与后段被 X 隔开,groupby 不续接跨 X)
        self.assertEqual(n, 1)
        segs = [s.strip() for s in out.split("\n\n") if s.strip()]
        # 前 3 court 保留 + 1 折叠后的 court + X + notice
        self.assertEqual(segs.count("court"), 4)  # 3 保留 + 1 折叠首段
        self.assertIn("X", segs)

    def test_empty_segments_do_not_break_run(self):
        """红线:真空段(四换行 \\n\\n\\n\\n)夹入仍折叠(先滤空段再 groupby)。
        用四换行才产生 norm=='' 的真空段——三换行的中段是 '\\ncourt'(非空),
        删掉滤空段逻辑也能过,守不住红线(评审 F2)。"""
        # 四换行:court, "", court, "", court, "", court —— 真空段夹入
        text = "court\n\n\n\ncourt\n\n\n\ncourt\n\n\n\ncourt"
        out, n = fold_degenerate(text, notice=N)
        self.assertEqual(n, 1, "真空段夹入必须仍能续接成游程(滤空段红线)")
        self.assertEqual(len([s for s in out.split("\n\n") if s.strip() == "court"]), 1)

    def test_invalid_threshold_config_no_crash(self):
        """根因兜底:阈值为 null/字符串/负数 → 不崩,降级为不折叠(评审 F4)。"""
        text = "court\n\ncourt\n\ncourt\n\ncourt"
        for bad in (None, "x", 0, -1):
            out, n = fold_degenerate(text, min_run=bad, notice=N)
            self.assertEqual(n, 0, f"min_run={bad!r} 应降级不折叠、不崩")
            self.assertEqual(out, text)
        # notice 非字符串也不崩
        out, n = fold_degenerate(text, notice=None)
        self.assertIsInstance(out, str)

    def test_strip_semantics_keep_first_raw(self):
        """各段仅尾随空白差异(strip 后相同)→ 折叠,保留首段原文 raw。"""
        text = "court  \n\ncourt\n\ncourt\t\n\ncourt"
        out, n = fold_degenerate(text, notice=N)
        self.assertEqual(n, 1)
        self.assertIn("court  ", out)  # 首段原文含尾随空格

    def test_line_fallback_strict_threshold(self):
        """\\n 回退:连续 ≥6 行相同短行 → 折叠;5 行 → 不折叠。"""
        text6 = "\n".join(["dup"] * 6)
        out6, n6 = fold_degenerate(text6, notice=N)
        self.assertEqual(n6, 1)
        text5 = "\n".join(["dup"] * 5)
        out5, n5 = fold_degenerate(text5, notice=N)
        self.assertEqual(n5, 0)

    def test_markdown_table_not_folded_by_line_fallback(self):
        """4~5 行 |---| 表格分隔 → 不被 \\n 回退误折叠(line_min_run=6 挡住)。"""
        text = "\n".join(["|---|"] * 5)
        out, n = fold_degenerate(text, notice=N)
        self.assertEqual(n, 0)
        self.assertEqual(out, text)

    def test_mixed_outer_and_inner(self):
        """混合:空行分隔退化 + 某非退化段内部行级退化 → 两者都折叠,n_folded=2。"""
        # 外层:p×4(空行分隔) 折叠 1;另一段内部 6 行 q 折叠 1
        inner_block = "\n".join(["q"] * 6)
        text = "p\n\np\n\np\n\np\n\n" + inner_block
        out, n = fold_degenerate(text, notice=N)
        self.assertEqual(n, 2)

    def test_notice_with_invoke_marker_refused(self):
        """notice 含 invoke 标记 → 拒绝折叠,原样返回。"""
        text = "court\n\ncourt\n\ncourt\n\ncourt"
        out, n = fold_degenerate(text, notice="<invoke bad>")
        self.assertEqual(n, 0)
        self.assertEqual(out, text)

    def test_notice_safety_helper(self):
        self.assertTrue(notice_is_safe("[ok]"))
        self.assertTrue(notice_is_safe(""))
        self.assertFalse(notice_is_safe("x<invoke"))
        self.assertFalse(notice_is_safe("antml:invoke here"))
        self.assertFalse(notice_is_safe("<function_calls>"))

    def test_empty_text(self):
        self.assertEqual(fold_degenerate("", notice=N), ("", 0))


class TestLiveDedup(unittest.TestCase):
    """流式去重(forward-then-suppress):有效内容实时转发、重复游程达阈值后抑制、恢复即续流。
    去重结果与 fold 一致、保留恢复数据,但会留 min_run-1 个重复(已流出无法收回)。"""

    def dedup(self, pieces, *, min_run=4, max_seg_len=80, notice=N):
        d = LiveDedup(min_run=min_run, max_seg_len=max_seg_len, notice=notice)
        out = "".join(d.feed(p) for p in pieces)
        out += d.flush()
        return out, d.folded

    def test_prefix_streams_degen_suppressed_recovery_kept(self):
        """核心:前缀实时流出 + court 游程被抑制(留 min_run-1 个)+ notice + 恢复数据保留。"""
        pieces = (["Real prefix line.\n\n"] + ["court\n\n"] * 10
                  + ["I recovered and made the call.\n\n"])
        out, folded = self.dedup(pieces, min_run=4)
        self.assertIn("Real prefix line.", out)
        self.assertIn("I recovered and made the call.", out)
        self.assertEqual(out.count("court"), 3, "min_run-1=3 个重复已流出,其余抑制")
        self.assertIn(N, out)
        self.assertEqual(folded, 1)

    def test_below_threshold_all_forwarded(self):
        """短游程(< min_run)不抑制:全部转发、无 notice。"""
        out, folded = self.dedup(["dup\n\n"] * 3, min_run=4)
        self.assertEqual(out.count("dup"), 3)
        self.assertNotIn(N, out)
        self.assertEqual(folded, 0)

    def test_recovery_resumes_then_second_degen(self):
        """恢复后可再次进入新退化并再抑制:a×6 + RECOVER + b×6 → 两处各抑制、notice×2。"""
        pieces = ["a\n\n"] * 6 + ["RECOVER\n\n"] + ["b\n\n"] * 6
        out, folded = self.dedup(pieces, min_run=4)
        self.assertIn("RECOVER", out)
        self.assertEqual(folded, 2)
        self.assertEqual(out.count(N), 2)
        self.assertEqual(out.count("a\n\n"), 3)
        self.assertEqual(out.count("b\n\n"), 3)

    def test_empty_segments_transparent(self):
        """红线:真空段(四换行)不打断游程,仍触发抑制。"""
        out, folded = self.dedup(["court\n\n\n\n"] * 10, min_run=4)
        self.assertEqual(folded, 1)
        self.assertEqual(out.count("court"), 3)
        self.assertIn(N, out)

    def test_long_segment_resets_run(self):
        """长段打断游程:court×3 + 长段 + court×3 → 各不足阈值,不抑制。"""
        long_seg = "x" * 100 + "\n\n"
        pieces = ["court\n\n"] * 3 + [long_seg] + ["court\n\n"] * 3
        out, folded = self.dedup(pieces, min_run=4, max_seg_len=80)
        self.assertEqual(folded, 0)
        self.assertEqual(out.count("court"), 6)
        self.assertIn("x" * 100, out)

    def test_alternating_no_suppression(self):
        """不同短段交替(a,b,a,b...)不构成同段游程 → 不抑制。"""
        out, folded = self.dedup(["a\n\nb\n\n"] * 6, min_run=4)
        self.assertEqual(folded, 0)
        self.assertNotIn(N, out)

    def test_fragmented_deltas(self):
        """分片任意切碎(跨 \\n\\n 边界):增量分段仍正确抑制。"""
        whole = "court\n\n" * 12
        pieces = [whole[i:i + 3] for i in range(0, len(whole), 3)]
        out, folded = self.dedup(pieces, min_run=4)
        self.assertEqual(folded, 1)
        self.assertLessEqual(out.count("court"), 3)
        self.assertIn(N, out)

    def test_notice_emitted_once(self):
        """长退化只插一次 notice(非每个被抑制段一次)。"""
        out, _ = self.dedup(["court\n\n"] * 30, min_run=4)
        self.assertEqual(out.count(N), 1)

    def test_byte_layout_preserved_for_nondegen(self):
        """非退化文本按 \\n\\n 切段后原样重建(单换行、三换行、真空段都不损)。"""
        text = "alpha\nbeta\n\ngamma\n\n\ndelta\n\n\n\nepsilon"
        # 每 4 字符切碎喂
        pieces = [text[i:i + 4] for i in range(0, len(text), 4)]
        out, folded = self.dedup(pieces, min_run=4)
        self.assertEqual(folded, 0)
        self.assertEqual(out, text, "非退化文本必须字节级还原")

    def test_flush_forwards_trailing_segment(self):
        """尾部未被 \\n\\n 终结的非退化段在 flush 时转发。"""
        out, _ = self.dedup(["hello world (no trailing newlines)"], min_run=4)
        self.assertEqual(out, "hello world (no trailing newlines)")

    def test_invalid_min_run_never_suppresses(self):
        """非法 min_run → 降级为永不抑制(全转发、不崩)。"""
        for bad in (None, 0, -1, "x"):
            out, folded = self.dedup(["court\n\n"] * 50, min_run=bad)
            self.assertEqual(folded, 0, f"min_run={bad!r} 应永不抑制")
            self.assertEqual(out.count("court"), 50)

    def test_unsafe_notice_disables_dedup(self):
        """notice 含 invoke 标记 → 整体禁用(pass-through),防 notice 自身被误解析。"""
        out, folded = self.dedup(["court\n\n"] * 20, notice="<invoke bad>")
        self.assertEqual(folded, 0)
        self.assertEqual(out.count("court"), 20)

    def test_empty_notice_suppresses_without_marker(self):
        """notice 为空:仍抑制重复,只是不插标记文本。"""
        out, folded = self.dedup(["court\n\n"] * 20, notice="")
        self.assertEqual(folded, 1)
        self.assertEqual(out.count("court"), 3)

    def test_flush_final_segment_deduped(self):
        """尾段无尾随 \\n\\n 也参与去重:court×4(min_run=4,末段无 \\n\\n)→ 3 court + notice,
        与带尾随 \\n\\n 的结果一致(不因终止分隔符而分叉;评审 Important)。"""
        out, folded = self.dedup(["court\n\ncourt\n\ncourt\n\ncourt"], min_run=4)
        self.assertEqual(folded, 1)
        self.assertEqual(out.count("court"), 3)
        self.assertIn(N, out)

    def test_flush_final_segment_below_threshold_kept(self):
        """尾段使游程仍不足 min_run → 原样保留(不误抑制)。"""
        out, folded = self.dedup(["court\n\ncourt\n\ncourt"], min_run=4)  # 只 3 段
        self.assertEqual(folded, 0)
        self.assertEqual(out.count("court"), 3)
        self.assertNotIn(N, out)

    def test_invalid_max_seg_len_disables(self):
        """非法 max_seg_len → 整体禁用(pass-through),不因 _UNREACHABLE 变成无限长度上限而过激
        (评审 Minor)。"""
        for bad in (None, "x", 0, -5):
            out, folded = self.dedup(["court\n\n"] * 20, max_seg_len=bad)
            self.assertEqual(folded, 0, f"max_seg_len={bad!r} 应禁用")
            self.assertEqual(out.count("court"), 20)

    def test_disable_passes_through(self):
        """disable() 后:后续 feed 原样透传、不再抑制;但实例保留(调用方据此仍闭合 block)。"""
        d = LiveDedup(min_run=4, max_seg_len=80, notice=N)
        d.feed("court\n\n")
        d.feed("court\n\n")
        d.disable()
        out = "".join(d.feed("court\n\n") for _ in range(10))
        self.assertEqual(out.count("court"), 10, "disable 后原样透传")

    def test_drain_raw_returns_pending_no_process(self):
        """drain_raw() 原样取回并清空 pending,不做去重处理、不抛。"""
        d = LiveDedup(min_run=4, max_seg_len=80, notice=N)
        d.feed("partial no newline")  # 无 \n\n,留 pending
        self.assertEqual(d.drain_raw(), "partial no newline")
        self.assertEqual(d.drain_raw(), "", "取回后清空")

    def test_feed_transactional_rollback(self):
        """feed 中 _consume_seg 抛异常 → pending 回滚到入口快照,drain_raw 能取回完整未提交文本
        (评审 Important:异常安全,绝不丢内容)。"""
        d = LiveDedup(min_run=4, max_seg_len=80, notice=N)
        d.feed("IMPORTANT")  # pending="IMPORTANT"
        with mock.patch.object(LiveDedup, "_consume_seg", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                d.feed("\n\nTAIL")
        # 回滚:pending 恢复到入口快照 IMPORTANT(TAIL 在本次 text_delta 参数里,由 stream 层接回)
        self.assertEqual(d.drain_raw(), "IMPORTANT", "异常回滚后未提交内容可原样取回")

    def test_flush_transactional_on_consume_error(self):
        """flush 中 _consume_seg 抛异常 → pending 不清空,drain_raw 仍能取回尾文
        (评审第三轮 BLOCKER:flush 也须 pending-事务化,绝不丢内容)。"""
        d = LiveDedup(min_run=4, max_seg_len=80, notice=N)
        d.feed("KEEPME")  # pending="KEEPME"(无 \n\n)
        with mock.patch.object(LiveDedup, "_consume_seg", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                d.flush()
        self.assertEqual(d.drain_raw(), "KEEPME", "flush 抛异常后 pending 不清,可 drain 取回")


if __name__ == "__main__":
    unittest.main()
