"""每个模型请求成功/失败后打一行紧凑访问日志到 stdout / Per-request access log line.

由官方 CustomLogger 钩子 `async_log_success_event` / `async_log_failure_event` 触发
(见 hooks.py 薄壳)。字段来源:
  - model / provider / 用时 / call_type / stream:kwargs["standard_logging_object"](StandardLoggingPayload)
  - 请求/响应内容字节:standard_logging_object 的 messages / response 的 UTF-8 序列化长度(内容近似,
    非真实 HTTP content-length;messages 被 redact 清空时降级为缺省)
  - token 明细(cache_creation / cache_read / fresh input):response_obj.usage(prompt_tokens 已含 cache)
  - 结束原因(end_turn 等):response_obj 多源提取并映射回 Anthropic 语义

一行形如(靠空格分组 + ANSI 着色,而非 `|` 区隔)::

    claude-sonnet-4-5  ↑338.2KB ↓11.0KB  ↑2+104.7k+534 ↻99%+1% ↓370  3.42s end_turn stream

token 用 SI 缩写(≥1k→k、≥1m→m,一位小数,<1k 原样),字节用 1024 进制(KB/MB)。cache 明细:
creation 黄(本次写入缓存)/read 绿(命中省钱)/fresh 青(新输入);命中率 ↻ 按高低绿/黄/红。logger 自带
stdout handler 且 propagate=False。默认抑制 uvicorn 的 per-request access log,由本行接管。仅本地开发观测用。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from hookpkg.config import load_config
from hookpkg.probes import append_jsonl
from litellm.proxy.observability.terminal.render.rich_renderer import RichLiveRenderer
from litellm.proxy.observability.terminal.bootstrap import captured_byte_total, session_hash_from_id
from litellm.proxy.observability.terminal.events import BodyBoundary
from rich.console import Console

# OpenAI finish_reason → Anthropic stop_reason。anthropic_messages 端点的 logging 走聚合
# OpenAI 式 ModelResponse(Anthropic 回转在 logging 之外),故 finish_reason 多为 OpenAI 术语。
_OPENAI_TO_ANTHROPIC = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "stop_sequence",
}
_ANTHROPIC_REASONS = frozenset({"end_turn", "max_tokens", "tool_use", "stop_sequence"})

# --- ANSI 颜色 ---
_RESET = "\033[0m"
_CYAN, _GREEN, _YELLOW, _RED, _DIM, _BOLD_RED, _BLUE, _MAGENTA = "36", "32", "33", "31", "2", "1;31", "34", "35"

_STOP_COLOR = {"end_turn": _GREEN, "tool_use": _CYAN, "max_tokens": _YELLOW, "stop_sequence": _YELLOW}

# provider / call_type 缩写。model 常带 provider 前缀(github_copilot/claude-sonnet-5),故把 provider
# 与 call_type 缩写并列显式(ghc/am),并从 model 剥掉前缀,消除 provider 重复。
_PROVIDER_ABBR = {"github_copilot": "ghc"}


def _short_provider(provider: Optional[str]) -> str:
    if not provider:
        return "?"
    return _PROVIDER_ABBR.get(provider, provider)


def _short_call_type(call_type: Optional[str]) -> str:
    """anthropic_messages→am、*responses*→re、*completion*→cc,其余原样。"""
    if not call_type:
        return "?"
    if call_type == "anthropic_messages":
        return "am"
    if "responses" in call_type:
        return "re"
    if "completion" in call_type:
        return "cc"
    return call_type


def _short_model(model: Optional[str]) -> str:
    """剥掉 provider 前缀:github_copilot/claude-sonnet-5 → claude-sonnet-5。无前缀则原样。"""
    if not model:
        return "?"
    return model.split("/", 1)[1] if "/" in model else model


@dataclass(frozen=True, slots=True)
class _Usage:
    """token 明细。prompt_tokens 已含 cache(creation+read);fresh = prompt - read - creation。"""

    prompt: int
    completion: int
    cache_read: int
    cache_creation: int

    @property
    def fresh(self) -> int:
        return max(0, self.prompt - self.cache_read - self.cache_creation)

    @property
    def hit_pct(self) -> int:
        return round(100 * self.cache_read / self.prompt) if self.prompt > 0 else 0


@dataclass(frozen=True, slots=True)
class _InFlight:
    call_id: str
    model: str
    started_at: float


class _AccessLogHandler(logging.Handler):
    """默认 stdout handler；真实 TTY 时把记录交给 footer 管理器。"""

    _litellm_reqlog_handler = True

    def emit(self, record: logging.LogRecord) -> None:
        try:
            display = globals().get("_LIVE_DISPLAY")
            live_status = bool(getattr(record, "live_status", False))
            call_id = getattr(record, "litellm_call_id", None)
            if live_status and display is not None and display._is_tty():
                display.finish_and_emit(call_id, self.format(record))
                return
            if not sys.stdout.isatty() and os.getenv("LITELLM_TERMINAL_ARCHIVE_DIR"):
                if display is not None:
                    display.discard(call_id)
                return
            if display is not None:
                display.discard(call_id)
            sys.stdout.write(self.format(record) + "\n")
            sys.stdout.flush()
        except Exception:
            self.handleError(record)


def _make_logger() -> logging.Logger:
    """专用 logger + 幂等 stdout handler。热重载时替换旧版自有 handler，不动外部附加 handler。"""
    log = logging.getLogger("litellm.hookpkg.reqlog")
    log.setLevel(logging.INFO)
    log.propagate = False
    owned_handlers = tuple(
        handler
        for handler in log.handlers
        if getattr(handler, "_litellm_reqlog_handler", False)
        or (
            type(handler) is logging.StreamHandler
            and getattr(handler, "stream", None) is sys.stdout
            and getattr(getattr(handler, "formatter", None), "_fmt", None) == "%(message)s"
        )
    )
    for handler in owned_handlers:
        log.removeHandler(handler)
    handler = _AccessLogHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)
    return log


_LOGGER = _make_logger()


def _c(text: str, code: str, on: bool) -> str:
    return f"\033[{code}m{text}{_RESET}" if on else text


def _si(n: int) -> str:
    """token SI 缩写:≥1m→m、≥1k→k(一位小数),<1k 原样整数。"""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}m"


def _bytes(n: Optional[int]) -> Optional[str]:
    """字节 1024 进制:<1KB→B、<1MB→KB、否则 MB(一位小数)。None 透传。"""
    if n is None:
        return None
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / (1024 * 1024):.1f}MB"


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _int(v: Any) -> Optional[int]:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _extract_usage(response_obj: Any) -> Optional[_Usage]:
    """从 response_obj.usage 多源提取 token 明细。cache_read 优先 prompt_tokens_details.cached_tokens,
    cache_creation 优先 prompt_tokens_details.cache_creation_tokens,均兜底顶层/私有属性。缺 usage 返回 None。"""
    usage = _get(response_obj, "usage")
    if usage is None:
        return None
    prompt = _int(_get(usage, "prompt_tokens")) or 0
    completion = _int(_get(usage, "completion_tokens")) or 0

    ptd = _get(usage, "prompt_tokens_details")
    read = _int(_get(ptd, "cached_tokens")) if ptd is not None else None
    creation = _int(_get(ptd, "cache_creation_tokens")) if ptd is not None else None
    if read is None:
        read = _int(_get(usage, "cache_read_input_tokens")) or _int(getattr(usage, "_cache_read_input_tokens", None))
    if creation is None:
        creation = _int(_get(usage, "cache_creation_input_tokens")) or _int(
            getattr(usage, "_cache_creation_input_tokens", None)
        )
    return _Usage(prompt=prompt, completion=completion, cache_read=read or 0, cache_creation=creation or 0)


def _json_bytes(obj: Any) -> Optional[int]:
    if obj is None:
        return None
    if isinstance(obj, str):
        return len(obj.encode("utf-8"))
    try:
        return len(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return None


def _raw_finish_reason(response_obj: Any) -> Optional[str]:
    """多源提取原始结束原因:先 Anthropic 式 stop_reason,再 OpenAI 式 choices[0].finish_reason。"""
    if isinstance(response_obj, dict):
        sr = response_obj.get("stop_reason")
        if isinstance(sr, str):
            return sr
        choices = response_obj.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            fr = choices[0].get("finish_reason")
            if isinstance(fr, str):
                return fr
        return None
    choices = getattr(response_obj, "choices", None)
    if isinstance(choices, (list, tuple)) and choices:
        fr = getattr(choices[0], "finish_reason", None)
        if isinstance(fr, str):
            return fr
    sr = getattr(response_obj, "stop_reason", None)
    return sr if isinstance(sr, str) else None


def _extract_tool_names(response_obj: Any) -> tuple[str, ...]:
    """从聚合 ModelResponse 提取实际调用的工具名，按首次出现顺序去重。"""
    choices = _get(response_obj, "choices")
    if not isinstance(choices, (list, tuple)):
        return ()
    names = tuple(
        name
        for choice in choices
        for message in (_get(choice, "message"),)
        for tool_call in (_get(message, "tool_calls") or ())
        for function in (_get(tool_call, "function"),)
        for name in (_get(function, "name") or _get(tool_call, "name"),)
        if isinstance(name, str) and name
    )
    return names


def _extract_thinking(response_obj: Any, model_parameters: Any) -> tuple[int, Optional[str]]:
    """返回带不透明 signature/data 的 thinking block 数量和请求的 thinking 模式。"""
    choices = _get(response_obj, "choices")
    blocks = (
        tuple(
            block
            for choice in choices
            if isinstance(choices, (list, tuple))
            for message in (_get(choice, "message"),)
            for block in (_get(message, "thinking_blocks") or ())
            if any(_get(block, key) for key in ("signature", "data", "encrypted_content"))
        )
        if isinstance(choices, (list, tuple))
        else ()
    )
    thinking = _get(model_parameters, "thinking")
    mode = _get(thinking, "type") if thinking is not None else None
    if not isinstance(mode, str) and isinstance(thinking, str):
        mode = thinking
    return len(blocks), mode if isinstance(mode, str) and mode else None


def _to_anthropic_stop_reason(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    if raw in _ANTHROPIC_REASONS:
        return raw
    return _OPENAI_TO_ANTHROPIC.get(raw, raw)


def _dur_color(response_time: Optional[float]) -> str:
    if not isinstance(response_time, (int, float)):
        return _DIM
    if response_time >= 30:
        return _RED
    if response_time >= 10:
        return _YELLOW
    return _DIM


def _hit_color(pct: int) -> str:
    if pct >= 90:
        return _GREEN
    if pct >= 50:
        return _YELLOW
    return _RED


def _tokens_group(usage: Optional[_Usage], slp: dict, color: bool) -> str:
    """token 段:↑creation+read+fresh ↻creation%+read%+fresh% ↓output。"""
    if usage is None:
        pt = _int(slp.get("prompt_tokens")) or 0
        ct = _int(slp.get("completion_tokens")) or 0
        return f"↑{_si(pt)} ↓{_si(ct)}"
    up = "+".join(
        (
            _c(_si(usage.cache_creation), _YELLOW, color),
            _c(_si(usage.cache_read), _GREEN, color),
            _c(_si(usage.fresh), _CYAN, color),
        )
    )
    values = (usage.cache_creation, usage.cache_read, usage.fresh)
    percentages = tuple(round(100 * value / usage.prompt) if usage.prompt else 0 for value in values)
    rate = "↻" + "+".join(
        (
            _c(f"{percentages[0]}%", _YELLOW, color),
            _c(f"{percentages[1]}%", _GREEN, color),
            _c(f"{percentages[2]}%", _CYAN, color),
        )
    )
    return f"↑{up} {rate} ↓{_si(usage.completion)}"


def _duration(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return "?"
    return f"{seconds:.2f}s"


def _session_color(session_hash: Optional[str]) -> str:
    if not session_hash:
        return _DIM
    palette = ("38;5;39", "38;5;75", "38;5;111", "38;5;141", "38;5;177", "38;5;213", "38;5;221")
    return palette[sum(ord(char) for char in session_hash) % len(palette)]


def _completed_at(slp: dict) -> str:
    epoch = _epoch(slp.get("endTime"))
    completed = datetime.fromtimestamp(epoch) if epoch is not None else datetime.now()
    return completed.strftime("%H:%M:%S")


def format_inflight(requests: tuple[_InFlight, ...], now: float, color: bool) -> str:
    """按模型合并在途请求；每组显示数量和该组最早请求的耗时。"""
    if not requests:
        return ""
    models = tuple(dict.fromkeys(request.model for request in requests))
    groups = tuple(
        (
            model,
            tuple(request for request in requests if request.model == model),
        )
        for model in models
    )
    details = tuple(
        f"{model}{f' ×{len(group)}' if len(group) > 1 else ''} "
        f"{_duration(max(0.0, now - min(request.started_at for request in group)))}"
        for model, group in groups
    )
    marker = _c("[ .. ]", _CYAN, color)
    return f"{marker} {len(requests)} in-flight  " + "  ".join(details)


class _LiveDisplay:
    """在真实 TTY 最后一行显示在途请求，日志继续在上方滚动区输出。"""

    def __init__(
        self,
        *,
        stream: Any = sys.stdout,
        clock: Any = time.monotonic,
        terminal_size: Any = shutil.get_terminal_size,
        refresh_interval: float = 0.2,
        auto_refresh: bool = True,
    ) -> None:
        self._stream = stream
        self._clock = clock
        self._terminal_size = terminal_size
        self._refresh_interval = refresh_interval
        self._auto_refresh = auto_refresh
        self._requests: tuple[_InFlight, ...] = ()
        self._rows: Optional[int] = None
        self._color = False
        self._closed = False
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _is_tty(self) -> bool:
        try:
            return bool(self._stream.isatty())
        except Exception:
            return False

    def snapshot(self) -> tuple[_InFlight, ...]:
        with self._lock:
            return self._requests

    def _size(self) -> tuple[int, int]:
        try:
            size = self._terminal_size()
            return max(20, int(size[0])), max(2, int(size[1]))
        except Exception:
            return 120, 24

    def _clear_footer_locked(self, rows: int) -> None:
        self._stream.write(f"\0337\033[{rows};1H\033[2K\0338")

    def _ensure_layout_locked(self) -> tuple[int, int]:
        columns, rows = self._size()
        if self._rows != rows:
            if self._rows is not None:
                self._clear_footer_locked(self._rows)
            self._stream.write(f"\033[1;{rows - 1}r\033[{rows - 1};1H")
            self._rows = rows
        return columns, rows

    def _render_locked(self) -> None:
        if not self._requests:
            return
        columns, rows = self._ensure_layout_locked()
        plain = format_inflight(self._requests, self._clock(), color=False)
        fitted = plain if len(plain) < columns else plain[: max(0, columns - 1)]
        status = _c(fitted, _CYAN, self._color)
        self._stream.write(f"\0337\033[{rows};1H\033[2K{status}\0338")
        self._stream.flush()

    def _teardown_locked(self) -> None:
        if self._rows is None:
            return
        rows = self._rows
        self._clear_footer_locked(rows)
        self._stream.write(f"\033[r\033[{rows};1H")
        self._stream.flush()
        self._rows = None

    def _refresh_loop(self) -> None:
        while True:
            self._wake.wait(self._refresh_interval)
            self._wake.clear()
            with self._lock:
                if self._closed:
                    return
                if self._requests:
                    self._render_locked()

    def _ensure_thread_locked(self) -> None:
        if not self._auto_refresh or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._refresh_loop, name="litellm-request-footer", daemon=True)
        self._thread.start()

    def start(self, call_id: str, model: str, *, started_at: Optional[float] = None, color: bool = False) -> None:
        if not call_id or not self._is_tty():
            return
        with self._lock:
            if self._closed or any(request.call_id == call_id for request in self._requests):
                return
            self._requests = (*self._requests, _InFlight(call_id, model, started_at or self._clock()))
            self._color = color
            self._ensure_thread_locked()
            self._render_locked()
            self._wake.set()

    def finish_and_emit(self, call_id: Optional[str], line: str) -> None:
        with self._lock:
            if not self._is_tty():
                return
            if self._rows is None:
                self._stream.write(line + "\n")
                self._stream.flush()
                return
            self._clear_footer_locked(self._rows)
            self._stream.write(f"\r\033[2K{line}\n")
            self._requests = tuple(request for request in self._requests if request.call_id != call_id)
            if self._requests:
                self._render_locked()
            else:
                self._teardown_locked()

    def discard(self, call_id: Optional[str]) -> None:
        with self._lock:
            self._requests = tuple(request for request in self._requests if request.call_id != call_id)
            if self._requests:
                self._render_locked()
            else:
                self._teardown_locked()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._requests = ()
            self._teardown_locked()
            self._wake.set()


class _RichLiveDisplay:
    """Rich owns terminal redraw; request state remains immutable and lock-protected."""

    def __init__(
        self,
        *,
        stream: Any = sys.stdout,
        clock: Any = time.monotonic,
        refresh_interval: float = 0.25,
        auto_refresh: bool = True,
    ) -> None:
        self._stream = stream
        self._clock = clock
        self._refresh_interval = refresh_interval
        self._auto_refresh = auto_refresh
        self._requests: tuple[_InFlight, ...] = ()
        self._closed = False
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._renderer = RichLiveRenderer(Console(file=stream, force_terminal=None), refresh_hz=4)

    def _is_tty(self) -> bool:
        try:
            return bool(self._stream.isatty())
        except Exception:
            return False

    def snapshot(self) -> tuple[_InFlight, ...]:
        with self._lock:
            return self._requests

    def is_active(self) -> bool:
        with self._lock:
            return bool(self._requests) and not self._closed

    def emit_log(self, line: str) -> None:
        with self._lock:
            self._renderer.log(line)

    def _render_locked(self) -> None:
        if self._requests:
            self._renderer.update(format_inflight(self._requests, self._clock(), color=False))

    def _refresh_loop(self) -> None:
        while True:
            self._wake.wait(self._refresh_interval)
            self._wake.clear()
            with self._lock:
                if self._closed:
                    return
                self._render_locked()

    def _ensure_thread_locked(self) -> None:
        if not self._auto_refresh or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._refresh_loop, name="litellm-rich-footer", daemon=True)
        self._thread.start()

    def start(self, call_id: str, model: str, *, started_at: Optional[float] = None, color: bool = False) -> None:
        del color
        if not call_id or not self._is_tty():
            return
        with self._lock:
            if self._closed or any(request.call_id == call_id for request in self._requests):
                return
            self._requests = (*self._requests, _InFlight(call_id, model, started_at or self._clock()))
            self._ensure_thread_locked()
            self._render_locked()
            self._wake.set()

    def finish_and_emit(self, call_id: Optional[str], line: str) -> None:
        with self._lock:
            if not self._is_tty():
                return
            self._renderer.log(line)
            self._requests = tuple(request for request in self._requests if request.call_id != call_id)
            if self._requests:
                self._render_locked()
            else:
                self._renderer.stop()

    def discard(self, call_id: Optional[str]) -> None:
        with self._lock:
            self._requests = tuple(request for request in self._requests if request.call_id != call_id)
            if self._requests:
                self._render_locked()
            else:
                self._renderer.stop()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._requests = ()
            self._renderer.stop()
            self._wake.set()


class _RichLogBridge(logging.Handler):
    """Route known logger records through Rich only while the footer is active."""

    def __init__(self, display: _RichLiveDisplay, originals: tuple[logging.Handler, ...]) -> None:
        super().__init__()
        self._display = display
        self._originals = originals

    def emit(self, record: logging.LogRecord) -> None:
        if self._display.is_active():
            formatter = next((handler.formatter for handler in self._originals if handler.formatter), None)
            line = formatter.format(record) if formatter is not None else record.getMessage()
            self._display.emit_log(line)
            return
        for handler in self._originals:
            handler.handle(record)


@dataclass(frozen=True, slots=True)
class _LoggerBridgeRegistration:
    logger: logging.Logger
    handlers: tuple[logging.Handler, ...]
    propagate: bool


def _install_rich_log_bridges(display: _RichLiveDisplay) -> tuple[_LoggerBridgeRegistration, ...]:
    registrations: tuple[_LoggerBridgeRegistration, ...] = ()
    for name in ("LiteLLM", "LiteLLM Proxy", "LiteLLM Router", "uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        handlers = tuple(logger.handlers)
        if not handlers:
            continue
        registrations = (*registrations, _LoggerBridgeRegistration(logger, handlers, logger.propagate))
        logger.handlers = [_RichLogBridge(display, handlers)]
        logger.propagate = False
    return registrations


def _restore_rich_log_bridges(registrations: tuple[_LoggerBridgeRegistration, ...]) -> None:
    for registration in registrations:
        registration.logger.handlers = list(registration.handlers)
        registration.logger.propagate = registration.propagate


_previous_display = globals().get("_LIVE_DISPLAY")
if _previous_display is not None:
    _previous_display.close()
_previous_bridges = globals().get("_RICH_LOG_BRIDGES", ())
_restore_rich_log_bridges(_previous_bridges)
_LIVE_DISPLAY = _RichLiveDisplay()
_RICH_LOG_BRIDGES = _install_rich_log_bridges(_LIVE_DISPLAY)


def format_line(
    slp: Optional[dict],
    stop_reason: Optional[str],
    usage: Optional[_Usage] = None,
    req_bytes: Optional[int] = None,
    resp_bytes: Optional[int] = None,
    *,
    failed: bool = False,
    color: bool = False,
    diagnose_suffix: str = "",
    tool_names: tuple[str, ...] = (),
    thinking_count: int = 0,
    thinking_mode: Optional[str] = None,
    completed_at: Optional[str] = None,
    status_code: Optional[int] = None,
    session_hash: Optional[str] = None,
) -> str:
    """把各来源拼成一行竞品风格的完成记录。纯函数，便于单测。"""
    s = slp or {}
    model = _short_model(s.get("model"))
    provider = s.get("custom_llm_provider") or "?"
    provider_badge = _short_provider(provider)
    surface = "anthropic" if s.get("call_type") == "anthropic_messages" else _short_call_type(s.get("call_type"))
    surface_model = f"{surface}/{model}"
    response_time = s.get("response_time")
    is_stream = bool(s.get("stream"))
    marker = _c("[FAIL]", _BOLD_RED, color) if failed else _c("[ OK ]", _GREEN, color)
    code = status_code if status_code is not None else (None if failed else 200)
    session = f"■ {session_hash}" if session_hash else "□ ----"
    groups = [
        marker,
        completed_at or _completed_at(s),
        _c(session, _session_color(session_hash), color),
        _c(surface_model, _CYAN, color),
        _c(f"· {provider_badge}", _DIM, color),
        str(code) if code is not None else "ERR",
        _c(_duration(response_time), _dur_color(response_time), color),
    ]

    byte_bits = [
        b
        for b in (
            (f"↑{_bytes(req_bytes)}" if req_bytes is not None else None),
            (f"↓{_bytes(resp_bytes)}" if resp_bytes is not None else None),
        )
        if b
    ]
    if byte_bits:
        groups.extend((_c(bit, _BLUE, color) for bit in byte_bits))

    groups.append(_tokens_group(usage, s, color))
    if stop_reason:
        tool_suffix = f"({','.join(tool_names)})" if stop_reason == "tool_use" and tool_names else ""
        groups.append(_c(f"{stop_reason}{tool_suffix}", _STOP_COLOR.get(stop_reason, _MAGENTA), color))
    if thinking_count:
        groups.append(_c(f"think:enc({thinking_count})", _MAGENTA, color))
    if not is_stream:
        groups.append(_c("(non-stream)", _DIM, color))

    return " ".join(groups) + diagnose_suffix


def _use_color(mode: Any) -> bool:
    """color 配置:"always"→True、"never"→False、其余("auto")→stdout 是 TTY 才上色。"""
    if mode == "always":
        return True
    if mode == "never":
        return False
    return sys.stdout.isatty()


def _apply_uvicorn_suppression(suppress: Any) -> None:
    """静默/恢复 uvicorn 的 per-request access log(幂等)。热读即时生效,无需 reload。"""
    logging.getLogger("uvicorn.access").disabled = bool(suppress)


def _epoch(v: Any) -> Optional[float]:
    """把 StandardLoggingPayload 的时间字段归一化为 epoch 秒 float。
    litellm 里 startTime/endTime/completionStartTime 多为 float(epoch),偶为 datetime;
    两者都接,其余返回 None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    ts = getattr(v, "timestamp", None)  # datetime
    if callable(ts):
        try:
            return float(ts())
        except Exception:
            return None
    return None


def build_timing_record(slp: dict, usage: Optional[_Usage]) -> dict:
    """从 standard_logging_object 抽端到端计时,拆成 total / ttft / gen 三段。纯函数,便于单测。

    - response_time: litellm 已算好的端到端总耗时(秒)。
    - ttft: completionStartTime - startTime,首 token 到达(含上游模型首字节 + 请求侧 hook + 网络)。
    - gen:  endTime - completionStartTime,首 token 之后的生成/流式时长(响应侧 hook 分摊在此段)。
    非流式请求 completionStartTime 常等于 endTime,ttft≈total、gen≈0。字段缺失时对应量为 None。
    """
    start = _epoch(slp.get("startTime"))
    comp = _epoch(slp.get("completionStartTime"))
    end = _epoch(slp.get("endTime"))
    rt = slp.get("response_time")
    total = (
        float(rt)
        if isinstance(rt, (int, float)) and not isinstance(rt, bool)
        else (end - start if start is not None and end is not None else None)
    )
    ttft = comp - start if start is not None and comp is not None else None
    gen = end - comp if end is not None and comp is not None else None
    return {
        "ts": end,
        "model": _short_model(slp.get("model")),
        "provider": slp.get("custom_llm_provider"),
        "call_type": slp.get("call_type"),
        "stream": bool(slp.get("stream")),
        "total_s": round(total, 4) if total is not None else None,
        "ttft_s": round(ttft, 4) if ttft is not None else None,
        "gen_s": round(gen, 4) if gen is not None else None,
        "prompt_tokens": usage.prompt if usage else _int(slp.get("prompt_tokens")),
        "completion_tokens": usage.completion if usage else _int(slp.get("completion_tokens")),
        "cache_read": usage.cache_read if usage else None,
        "req_bytes": _json_bytes(slp.get("messages")),
    }


def request_started(data: Any, call_type: Any = None) -> None:
    """在 pre-call 生命周期登记请求；非 TTY 或关闭 live_status 时无输出。"""
    cfg = load_config()
    request_log = cfg.get("request_log") or {}
    _apply_uvicorn_suppression(request_log.get("suppress_uvicorn_access"))
    if not request_log.get("enabled") or not request_log.get("live_status", True) or not isinstance(data, dict):
        return
    call_id = data.get("litellm_call_id")
    if not isinstance(call_id, str) or not call_id:
        return
    provider = str(data.get("model") or "").split("/", 1)[0] if "/" in str(data.get("model") or "") else "?"
    surface = "anthropic" if call_type == "anthropic_messages" else _short_call_type(str(call_type))
    model_label = f"{surface}/{_short_model(data.get('model'))} · {_short_provider(provider)}"
    _LIVE_DISPLAY.start(
        call_id,
        model_label,
        color=_use_color(request_log.get("color")),
    )


def _call_id(kwargs: Any, slp: Any) -> Optional[str]:
    value = _get(slp, "litellm_call_id") or _get(kwargs, "litellm_call_id")
    return value if isinstance(value, str) and value else None


def _status_code(response_obj: Any, slp: Any) -> Optional[int]:
    error_information = _get(slp, "error_information")
    for value in (
        _get(response_obj, "status_code"),
        _get(error_information, "status_code"),
        _get(error_information, "error_code"),
    ):
        if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
            return value
        if isinstance(value, str) and value.isdigit() and 100 <= int(value) <= 599:
            return int(value)
    return None


def _emit_access_line(line: str, call_id: Optional[str], request_log: dict, level: int) -> None:
    _LOGGER.log(
        level,
        line,
        extra={"litellm_call_id": call_id, "live_status": request_log.get("live_status", True)},
    )


def log_success(kwargs: Any, response_obj: Any, start_time: Any, end_time: Any) -> None:
    """成功后打一行；即使格式化或落盘失败，也必须注销在途请求。"""
    slp = kwargs.get("standard_logging_object") if isinstance(kwargs, dict) else None
    call_id = _call_id(kwargs, slp)
    try:
        _log_success(kwargs, response_obj, start_time, end_time, slp, call_id)
    finally:
        _LIVE_DISPLAY.discard(call_id)


def _log_success(
    kwargs: Any, response_obj: Any, start_time: Any, end_time: Any, slp: Any, call_id: Optional[str]
) -> None:
    cfg = load_config()
    rc = cfg.get("request_log") or {}
    _apply_uvicorn_suppression(rc.get("suppress_uvicorn_access"))
    if not rc.get("enabled"):
        return
    s = slp or {}
    usage = _extract_usage(response_obj)
    req_bytes = _json_bytes(s.get("messages"))
    resp_bytes = _json_bytes(s.get("response"))
    if call_id:
        try:
            request_uuid = UUID(call_id)
            req_bytes = captured_byte_total(request_uuid, BodyBoundary.UPSTREAM_REQUEST) or req_bytes
            resp_bytes = captured_byte_total(request_uuid, BodyBoundary.UPSTREAM_RESPONSE) or resp_bytes
        except ValueError:
            pass
    stop = _to_anthropic_stop_reason(_raw_finish_reason(response_obj))
    response_for_details = response_obj if _get(response_obj, "choices") is not None else s.get("response")
    tool_names = _extract_tool_names(response_for_details)
    thinking_count, _thinking_mode = _extract_thinking(response_for_details, s.get("model_parameters"))
    suffix = ""
    if rc.get("diagnose"):
        u = _get(response_obj, "usage")
        suffix = (
            f"  [obj={type(response_obj).__name__} usage={type(u).__name__ if u is not None else None}"
            f" ptd={_get(u, 'prompt_tokens_details') is not None if u is not None else None}"
            f" msgs={s.get('messages') is not None} resp={s.get('response') is not None}]"
        )
    line = format_line(
        slp,
        stop,
        usage,
        req_bytes,
        resp_bytes,
        color=_use_color(rc.get("color")),
        diagnose_suffix=suffix,
        tool_names=tool_names,
        thinking_count=thinking_count,
        status_code=_status_code(response_obj, slp),
        session_hash=session_hash_from_id(str(s.get("trace_id"))) if s.get("trace_id") else None,
    )
    _emit_access_line(line, call_id, rc, logging.INFO)
    # 端到端计时落盘(可选):配了 timing_file 才落,供离线聚合 total/ttft/gen 分布。
    timing_file = rc.get("timing_file")
    if timing_file and isinstance(slp, dict):
        append_jsonl(timing_file, build_timing_record(slp, usage))


def log_failure(kwargs: Any, response_obj: Any, start_time: Any, end_time: Any) -> None:
    """失败后打一行；即使格式化失败，也必须注销在途请求。"""
    slp = kwargs.get("standard_logging_object") if isinstance(kwargs, dict) else None
    call_id = _call_id(kwargs, slp)
    try:
        _log_failure(kwargs, response_obj, start_time, end_time, slp, call_id)
    finally:
        _LIVE_DISPLAY.discard(call_id)


def _log_failure(
    kwargs: Any, response_obj: Any, start_time: Any, end_time: Any, slp: Any, call_id: Optional[str]
) -> None:
    cfg = load_config()
    rc = cfg.get("request_log") or {}
    _apply_uvicorn_suppression(rc.get("suppress_uvicorn_access"))
    if not rc.get("enabled"):
        return
    err = (slp or {}).get("error_str") if isinstance(slp, dict) else None
    color = _use_color(rc.get("color"))
    line = format_line(
        slp,
        None,
        _extract_usage(response_obj),
        failed=True,
        color=color,
        status_code=_status_code(response_obj, slp),
        session_hash=session_hash_from_id(str((slp or {}).get("trace_id"))) if (slp or {}).get("trace_id") else None,
    )
    if isinstance(err, str) and err:
        line += " " + _c(err[:200], _RED, color)
    _emit_access_line(line, call_id, rc, logging.WARNING)


# 进程启动/热重载时按当前配置应用一次 uvicorn 抑制(best-effort;之后每次 log 再同步)。
try:
    _apply_uvicorn_suppression((load_config().get("request_log") or {}).get("suppress_uvicorn_access"))
except Exception:  # pragma: no cover - 启动期配置不可读时不致命,log 时会再试
    pass
