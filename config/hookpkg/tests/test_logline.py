"""logline 单测:结束原因映射、token 明细/cache 命中率提取、字节、缩写、行格式化、端到端。
运行:python3 -m unittest hookpkg.tests.test_logline -v  (从 litellm 根目录)"""
import logging
import re
import unittest
from dataclasses import dataclass
from unittest.mock import patch

from hookpkg import logline
from hookpkg.logline import (
    _InFlight, _LiveDisplay, _Usage, _bytes, _extract_thinking, _extract_tool_names, _extract_usage,
    _json_bytes, _raw_finish_reason, _short_call_type, _short_model, _short_provider, _si,
    _to_anthropic_stop_reason, format_inflight, format_line,
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


@dataclass
class _Function:
    name: str


@dataclass
class _ToolCall:
    function: _Function


@dataclass
class _Message:
    tool_calls: list
    thinking_blocks: list


class _RichChoice(_Choice):
    def __init__(self, finish_reason, message):
        super().__init__(finish_reason)
        self.message = message


class _RichModelResponse(_ModelResponse):
    def __init__(self, finish_reason, message, usage=None):
        super().__init__(finish_reason, usage)
        self.choices = [_RichChoice(finish_reason, message)]


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


class TestResponseDetails(unittest.TestCase):
    def test_extracts_tool_names_and_encrypted_thinking(self):
        response = _RichModelResponse(
            "tool_calls",
            _Message(
                tool_calls=[_ToolCall(_Function("Bash")), _ToolCall(_Function("Read"))],
                thinking_blocks=[{"type": "thinking", "thinking": "...", "signature": "opaque"}],
            ),
        )
        self.assertEqual(_extract_tool_names(response), ("Bash", "Read"))
        self.assertEqual(_extract_thinking(response, {"thinking": {"type": "adaptive"}}), (1, "adaptive"))

    def test_missing_details_are_omitted(self):
        self.assertEqual(_extract_tool_names(_ModelResponse("stop")), ())
        self.assertEqual(_extract_thinking(_ModelResponse("stop"), {}), (0, None))


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
        line = format_line(
            self._slp(), "end_turn", self._usage(), req_bytes=346317, resp_bytes=11 * 1024,
            completed_at="17:18:53", session_hash="7K3M",
        )
        self.assertEqual(
            line,
            "[ OK ] 17:18:53 ■ 7K3M anthropic/claude-sonnet-5@ghc 200 3.42s "
            "↑338.2KB ↓11.0KB ↑2+104.7k+534 ↻0%+99%+1% ↓370 end_turn",
        )

    def test_meta_replaces_repeated_provider_and_tail_calltype(self):
        line = format_line(self._slp(), "end_turn", self._usage())
        self.assertIn("anthropic/claude-sonnet-5@ghc", line)
        self.assertNotIn("github_copilot", line)
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
        self.assertIn("[FAIL]", line)

    def test_tool_and_thinking_details(self):
        line = format_line(
            self._slp(), "tool_use", self._usage(), tool_names=("Bash",), thinking_count=1,
            thinking_mode="adaptive", completed_at="17:18:53",
        )
        self.assertIn("tool_use(Bash)", line)
        self.assertIn("think:enc(1)", line)
        self.assertNotIn("thinking:adaptive", line)

    def test_color_wraps_and_strips_back(self):
        colored = format_line(
            self._slp(), "end_turn", self._usage(), req_bytes=346317, resp_bytes=11 * 1024, color=True,
        )
        self.assertIn("\033[32mend_turn\033[0m", colored)   # 绿 end_turn
        self.assertIn("\033[32m104.7k\033[0m", colored)      # cache_read 绿
        self.assertIn("\033[33m2\033[0m", colored)           # cache_creation 黄
        self.assertEqual(
            _strip_ansi(colored),
            format_line(self._slp(), "end_turn", self._usage(), req_bytes=346317, resp_bytes=11 * 1024, color=False),
        )


class TestInflightLine(unittest.TestCase):
    def test_groups_requests_by_model_and_uses_oldest_elapsed(self):
        requests = (
            _InFlight("a", "claude-opus-4.8", 100.0),
            _InFlight("b", "claude-opus-4.8", 104.0),
            _InFlight("c", "claude-sonnet-5", 108.0),
        )
        self.assertEqual(
            format_inflight(requests, now=110.0, color=False),
            "[ .. ] 3 in-flight  claude-opus-4.8 ×2 10.00s  claude-sonnet-5 2.00s",
        )

    def test_empty_requests_have_no_footer(self):
        self.assertEqual(format_inflight((), now=110.0, color=False), "")


class _MemoryTTY:
    def __init__(self):
        self.output = ""

    def isatty(self):
        return True

    def write(self, value):
        self.output += value

    def flush(self):
        pass


class TestLiveDisplay(unittest.TestCase):
    def test_reserves_footer_groups_requests_and_restores_terminal(self):
        stream = _MemoryTTY()
        display = _LiveDisplay(
            stream=stream,
            clock=lambda: 110.0,
            terminal_size=lambda: (100, 24),
            auto_refresh=False,
        )

        display.start("a", "claude-opus-4.8", started_at=100.0, color=False)
        display.start("b", "claude-opus-4.8", started_at=104.0, color=False)
        self.assertIn("\033[1;23r", stream.output)
        self.assertIn("claude-opus-4.8 ×2 10.00s", stream.output)

        display.finish_and_emit("a", "DONE-A")
        self.assertIn("DONE-A", stream.output)
        self.assertIn("[ .. ] 1 in-flight", stream.output)

        display.finish_and_emit("b", "DONE-B")
        self.assertIn("DONE-B", stream.output)
        self.assertTrue(stream.output.endswith("\033[r\033[24;1H"))
        self.assertEqual(display.snapshot(), ())


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
        self.config = patch.object(logline, "load_config", return_value={"request_log": {
            "enabled": True,
            "diagnose": False,
            "suppress_uvicorn_access": False,
            "color": "never",
            "live_status": False,
            "timing_file": None,
        }})
        self.config.start()

    def tearDown(self):
        self.config.stop()
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
        self.assertIn("[ OK ]", msg)
        self.assertIn("anthropic/claude-sonnet-5@ghc 200", msg)
        self.assertIn("↑10+900+90", msg)   # creation+read+fresh
        self.assertIn("↻1%+90%+9%", msg)
        self.assertIn("↓20", msg)
        self.assertIn("end_turn", msg)     # OpenAI stop 已映射
        self.assertRegex(msg, r"↑\d+B ↓\d+B")  # 字节段(messages/response 存在,小内容显示为 B)

    def test_formatting_failure_still_discards_inflight_request(self):
        stream = _MemoryTTY()
        display = _LiveDisplay(stream=stream, terminal_size=lambda: (100, 24), auto_refresh=False)
        display.start("call-1", "claude-opus-4.8", started_at=100.0)
        kwargs = {"standard_logging_object": {"litellm_call_id": "call-1"}}

        with patch.object(logline, "_LIVE_DISPLAY", display), patch.object(
            logline, "format_line", side_effect=RuntimeError("broken formatter"),
        ):
            with self.assertRaisesRegex(RuntimeError, "broken formatter"):
                logline.log_success(kwargs, _ModelResponse("stop"), 0.0, 1.0)

        self.assertEqual(display.snapshot(), ())


class TestLogFailureEndToEnd(unittest.TestCase):
    def setUp(self):
        self.cap = _Capture()
        logline._LOGGER.addHandler(self.cap)
        self.config = patch.object(logline, "load_config", return_value={"request_log": {
            "enabled": True,
            "diagnose": False,
            "suppress_uvicorn_access": False,
            "color": "never",
            "live_status": False,
        }})
        self.config.start()

    def tearDown(self):
        self.config.stop()
        logline._LOGGER.removeHandler(self.cap)

    def test_emits_failure_status_and_truncated_error(self):
        kwargs = {"standard_logging_object": {
            "litellm_call_id": "call-fail",
            "model": "github_copilot/claude-opus-4.8",
            "custom_llm_provider": "github_copilot",
            "response_time": 2.5,
            "call_type": "anthropic_messages",
            "error_str": "x" * 250,
            "error_information": {"status_code": 429},
        }}
        logline.log_failure(kwargs, None, 0.0, 2.5)
        self.assertEqual(len(self.cap.messages), 1)
        message = _strip_ansi(self.cap.messages[0])
        self.assertIn("[FAIL]", message)
        self.assertIn("anthropic/claude-opus-4.8@ghc 429", message)
        self.assertTrue(message.endswith("x" * 200))

    def test_formatting_failure_still_discards_inflight_request(self):
        stream = _MemoryTTY()
        display = _LiveDisplay(stream=stream, terminal_size=lambda: (100, 24), auto_refresh=False)
        display.start("call-fail", "claude-opus-4.8", started_at=100.0)
        kwargs = {"standard_logging_object": {"litellm_call_id": "call-fail"}}

        with patch.object(logline, "_LIVE_DISPLAY", display), patch.object(
            logline, "format_line", side_effect=RuntimeError("broken formatter"),
        ):
            with self.assertRaisesRegex(RuntimeError, "broken formatter"):
                logline.log_failure(kwargs, None, 0.0, 1.0)

        self.assertEqual(display.snapshot(), ())


class TestAccessLogHandler(unittest.TestCase):
    def test_tty_record_is_emitted_above_footer_and_request_is_removed(self):
        stream = _MemoryTTY()
        display = _LiveDisplay(stream=stream, terminal_size=lambda: (100, 24), auto_refresh=False)
        display.start("call-1", "claude-opus-4.8", started_at=100.0)
        handler = logline._AccessLogHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        record = logging.LogRecord("test", logging.INFO, "", 0, "DONE", (), None)
        record.litellm_call_id = "call-1"
        record.live_status = True

        with patch.object(logline, "_LIVE_DISPLAY", display):
            handler.emit(record)

        self.assertIn("DONE", stream.output)
        self.assertEqual(display.snapshot(), ())
        self.assertTrue(stream.output.endswith("\033[r\033[24;1H"))


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
