"""logline 单测:结束原因映射、token 明细/cache 命中率提取、字节、缩写、行格式化、端到端。
运行:python3 -m unittest hookpkg.tests.test_logline -v  (从 litellm 根目录)"""
import logging
import re
import unittest

from hookpkg import logline
from hookpkg.logline import (
    _Usage, _bytes, _extract_usage, _json_bytes, _raw_finish_reason, _short_call_type,
    _short_model, _short_provider, _si, _to_anthropic_stop_reason, format_line,
)


def _strip_ansi(s):
    return re.sub(r"\033\[[0-9;]*m", "", s)


class _Choice:
    def __init__(self, finish_reason):
        self.finish_reason = finish_reason


class _PTD:
    def __init__(self, cached_tokens=None, cache_creation_tokens=None):
        self.cached_tokens = cached_tokens
        self.cache_creation_tokens = cache_creation_tokens


class _UsageObj:
    def __init__(self, prompt_tokens, completion_tokens, prompt_tokens_details=None,
                 _cache_read_input_tokens=0, _cache_creation_input_tokens=0):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.prompt_tokens_details = prompt_tokens_details
        self._cache_read_input_tokens = _cache_read_input_tokens
        self._cache_creation_input_tokens = _cache_creation_input_tokens


class _ModelResponse:
    """模拟 litellm 聚合 ModelResponse:choices[0].finish_reason(OpenAI 术语) + usage。"""
    def __init__(self, finish_reason, usage=None):
        self.choices = [_Choice(finish_reason)]
        self.usage = usage


class TestRawFinishReason(unittest.TestCase):
    def test_openai_object(self):
        self.assertEqual(_raw_finish_reason(_ModelResponse("tool_calls")), "tool_calls")

    def test_anthropic_dict_priority(self):
        obj = {"stop_reason": "end_turn", "choices": [{"finish_reason": "stop"}]}
        self.assertEqual(_raw_finish_reason(obj), "end_turn")

    def test_missing_none(self):
        self.assertIsNone(_raw_finish_reason({}))
        self.assertIsNone(_raw_finish_reason(object()))


class TestStopReasonMapping(unittest.TestCase):
    def test_openai_to_anthropic(self):
        self.assertEqual(_to_anthropic_stop_reason("stop"), "end_turn")
        self.assertEqual(_to_anthropic_stop_reason("length"), "max_tokens")
        self.assertEqual(_to_anthropic_stop_reason("tool_calls"), "tool_use")

    def test_passthrough_and_none(self):
        self.assertEqual(_to_anthropic_stop_reason("end_turn"), "end_turn")
        self.assertIsNone(_to_anthropic_stop_reason(None))


class TestSiAndBytes(unittest.TestCase):
    def test_si_one_decimal(self):
        self.assertEqual(_si(587), "587")
        self.assertEqual(_si(104734), "104.7k")
        self.assertEqual(_si(1_500_000), "1.5m")

    def test_bytes_1024(self):
        self.assertIsNone(_bytes(None))
        self.assertEqual(_bytes(512), "512B")
        self.assertEqual(_bytes(346317), "338.2KB")  # 用户示例 ↑338.2KB
        self.assertEqual(_bytes(11 * 1024), "11.0KB")

    def test_json_bytes(self):
        self.assertIsNone(_json_bytes(None))
        self.assertEqual(_json_bytes("héllo"), len("héllo".encode("utf-8")))
        self.assertTrue(_json_bytes([{"a": 1}]) > 0)


class TestAbbrev(unittest.TestCase):
    def test_provider(self):
        self.assertEqual(_short_provider("github_copilot"), "ghc")
        self.assertEqual(_short_provider("openai"), "openai")
        self.assertEqual(_short_provider(None), "?")

    def test_call_type(self):
        self.assertEqual(_short_call_type("anthropic_messages"), "am")
        self.assertEqual(_short_call_type("aresponses"), "re")
        self.assertEqual(_short_call_type("acompletion"), "cc")
        self.assertEqual(_short_call_type("completion"), "cc")

    def test_model_strips_provider_prefix(self):
        self.assertEqual(_short_model("github_copilot/claude-sonnet-5"), "claude-sonnet-5")
        self.assertEqual(_short_model("claude-sonnet-5"), "claude-sonnet-5")


class TestExtractUsage(unittest.TestCase):
    def test_from_prompt_tokens_details(self):
        u = _extract_usage(_ModelResponse("stop", _UsageObj(
            prompt_tokens=105236, completion_tokens=370,
            prompt_tokens_details=_PTD(cached_tokens=104700, cache_creation_tokens=2))))
        self.assertEqual(u.cache_read, 104700)
        self.assertEqual(u.cache_creation, 2)
        self.assertEqual(u.fresh, 105236 - 104700 - 2)  # 534
        self.assertEqual(u.completion, 370)
        self.assertEqual(u.hit_pct, round(100 * 104700 / 105236))  # 99

    def test_fallback_to_private_attrs(self):
        u = _extract_usage(_ModelResponse("stop", _UsageObj(
            prompt_tokens=1000, completion_tokens=10,
            _cache_read_input_tokens=800, _cache_creation_input_tokens=50)))
        self.assertEqual(u.cache_read, 800)
        self.assertEqual(u.cache_creation, 50)
        self.assertEqual(u.fresh, 150)

    def test_no_usage_returns_none(self):
        self.assertIsNone(_extract_usage(_ModelResponse("stop", None)))

    def test_hit_pct_zero_when_no_prompt(self):
        u = _Usage(prompt=0, completion=0, cache_read=0, cache_creation=0)
        self.assertEqual(u.hit_pct, 0)


class TestFormatLine(unittest.TestCase):
    def _slp(self, **over):
        base = {
            "model": "github_copilot/claude-sonnet-5",
            "custom_llm_provider": "github_copilot",
            "response_time": 3.42,
            "call_type": "anthropic_messages",
            "stream": True,
        }
        base.update(over)
        return base

    def _usage(self):
        return _Usage(prompt=105236, completion=370, cache_read=104700, cache_creation=2)

    def test_full_line_no_color(self):
        line = format_line(self._slp(), "end_turn", self._usage(), req_bytes=346317, resp_bytes=11 * 1024)
        self.assertEqual(
            line,
            "claude-sonnet-5 ghc/am  ↑338.2KB ↓11.0KB  ↑2+104.7k+534 ↻99%+1% ↓370  3.42s end_turn stream",
        )

    def test_meta_replaces_repeated_provider_and_tail_calltype(self):
        line = format_line(self._slp(), "end_turn", self._usage())
        self.assertIn("claude-sonnet-5 ghc/am", line)
        self.assertNotIn("github_copilot", line)     # provider 前缀已剥、不重复
        self.assertNotIn("anthropic_messages", line)  # call_type 已缩写并移到头部

    def test_usage_none_degrades_to_simple_tokens(self):
        slp = self._slp(prompt_tokens=120, completion_tokens=20)
        line = format_line(slp, "end_turn", None)
        self.assertIn("↑120 ↓20", line)
        self.assertNotIn("↻", line)

    def test_no_bytes_group_when_absent(self):
        line = format_line(self._slp(), "end_turn", self._usage())
        self.assertNotIn("KB", line)
        self.assertNotIn("↑338", line)

    def test_failed_marker(self):
        line = format_line(self._slp(stream=False), None, None, failed=True)
        self.assertIn("FAILED", line)

    def test_color_wraps_and_strips_back(self):
        colored = format_line(self._slp(), "end_turn", self._usage(), req_bytes=346317, resp_bytes=11 * 1024, color=True)
        self.assertIn("\033[32mend_turn\033[0m", colored)   # 绿 end_turn
        self.assertIn("\033[32m104.7k\033[0m", colored)      # cache_read 绿
        self.assertIn("\033[33m2\033[0m", colored)           # cache_creation 黄
        self.assertEqual(
            _strip_ansi(colored),
            format_line(self._slp(), "end_turn", self._usage(), req_bytes=346317, resp_bytes=11 * 1024, color=False),
        )


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class TestLogSuccessEndToEnd(unittest.TestCase):
    def setUp(self):
        self.cap = _Capture()
        logline._LOGGER.addHandler(self.cap)

    def tearDown(self):
        logline._LOGGER.removeHandler(self.cap)

    def test_emits_mapped_line_with_cache(self):
        kwargs = {"standard_logging_object": {
            "model": "github_copilot/claude-sonnet-5",
            "custom_llm_provider": "github_copilot",
            "response_time": 1.5,
            "call_type": "anthropic_messages",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
            "response": {"content": "hello"},
        }}
        resp = _ModelResponse("stop", _UsageObj(
            prompt_tokens=1000, completion_tokens=20,
            prompt_tokens_details=_PTD(cached_tokens=900, cache_creation_tokens=10)))
        logline.log_success(kwargs, resp, 0.0, 1.5)
        self.assertEqual(len(self.cap.messages), 1)
        msg = _strip_ansi(self.cap.messages[0])
        self.assertIn("claude-sonnet-5 ghc/am", msg)
        self.assertIn("↑10+900+90", msg)   # creation+read+fresh
        self.assertIn("↻90%+10%", msg)
        self.assertIn("↓20", msg)
        self.assertIn("end_turn", msg)     # OpenAI stop 已映射
        self.assertRegex(msg, r"↑\d+B ↓\d+B")  # 字节段(messages/response 存在,小内容显示为 B)


class TestUvicornSuppression(unittest.TestCase):
    def tearDown(self):
        logging.getLogger("uvicorn.access").disabled = False

    def test_toggle(self):
        logline._apply_uvicorn_suppression(True)
        self.assertTrue(logging.getLogger("uvicorn.access").disabled)
        logline._apply_uvicorn_suppression(False)
        self.assertFalse(logging.getLogger("uvicorn.access").disabled)


class TestBuildTimingRecord(unittest.TestCase):
    """端到端计时记录:total/ttft/gen 三段拆分 + 缺字段降级。"""

    def test_stream_three_way_split(self):
        # startTime=100, 首 token=100.5, end=103.0 -> total=3.0, ttft=0.5, gen=2.5
        slp = {"startTime": 100.0, "completionStartTime": 100.5, "endTime": 103.0,
               "response_time": 3.0, "model": "github_copilot/claude-opus-4.8",
               "custom_llm_provider": "github_copilot", "call_type": "anthropic_messages",
               "stream": True}
        rec = logline.build_timing_record(slp, None)
        self.assertEqual(rec["total_s"], 3.0)
        self.assertEqual(rec["ttft_s"], 0.5)
        self.assertEqual(rec["gen_s"], 2.5)
        self.assertEqual(rec["model"], "claude-opus-4.8")  # provider 前缀已剥
        self.assertTrue(rec["stream"])

    def test_response_time_preferred_over_endminusstart(self):
        # response_time 存在时用它当 total(不重算 end-start,二者可能因取样点略异)
        slp = {"startTime": 10.0, "endTime": 20.0, "response_time": 9.5,
               "completionStartTime": 12.0}
        rec = logline.build_timing_record(slp, None)
        self.assertEqual(rec["total_s"], 9.5)
        self.assertEqual(rec["ttft_s"], 2.0)

    def test_missing_completion_start_yields_none_ttft(self):
        slp = {"startTime": 10.0, "endTime": 20.0, "response_time": 10.0}
        rec = logline.build_timing_record(slp, None)
        self.assertEqual(rec["total_s"], 10.0)
        self.assertIsNone(rec["ttft_s"])
        self.assertIsNone(rec["gen_s"])

    def test_total_falls_back_to_endminusstart_when_no_response_time(self):
        slp = {"startTime": 10.0, "endTime": 14.0}
        rec = logline.build_timing_record(slp, None)
        self.assertEqual(rec["total_s"], 4.0)

    def test_usage_tokens_preferred(self):
        slp = {"startTime": 1.0, "endTime": 2.0, "response_time": 1.0,
               "prompt_tokens": 5, "completion_tokens": 7}
        usage = _Usage(prompt=999, completion=888, cache_read=100, cache_creation=0)
        rec = logline.build_timing_record(slp, usage)
        self.assertEqual(rec["prompt_tokens"], 999)   # usage 优先于 slp 顶层
        self.assertEqual(rec["completion_tokens"], 888)
        self.assertEqual(rec["cache_read"], 100)

    def test_datetime_fields_coerced(self):
        from datetime import datetime, timezone
        s = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        e = datetime(2026, 1, 1, 0, 0, 5, tzinfo=timezone.utc)
        slp = {"startTime": s, "endTime": e, "completionStartTime": s}
        rec = logline.build_timing_record(slp, None)
        self.assertAlmostEqual(rec["total_s"], 5.0, places=3)
        self.assertAlmostEqual(rec["ttft_s"], 0.0, places=3)


if __name__ == "__main__":
    unittest.main()
