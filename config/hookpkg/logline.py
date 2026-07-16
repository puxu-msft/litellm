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
import sys
from dataclasses import dataclass
from typing import Any, Optional

from hookpkg.config import load_config
from hookpkg.probes import append_jsonl

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


def _make_logger() -> logging.Logger:
    """专用 logger + 幂等 stdout handler。热重载重跑本模块,getLogger 返回同名单例,不重复加 handler。"""
    log = logging.getLogger("litellm.hookpkg.reqlog")
    log.setLevel(logging.INFO)
    log.propagate = False
    if not log.handlers:
        handler = logging.StreamHandler(sys.stdout)
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
        creation = (_int(_get(usage, "cache_creation_input_tokens"))
                    or _int(getattr(usage, "_cache_creation_input_tokens", None)))
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
    """token 段:↑creation+read+fresh ↻命中率%+未命中% ↓output。usage 缺失时退化为 ↑prompt ↓completion。"""
    if usage is None:
        pt = _int(slp.get("prompt_tokens")) or 0
        ct = _int(slp.get("completion_tokens")) or 0
        return f"↑{_si(pt)} ↓{_si(ct)}"
    up = "+".join((
        _c(_si(usage.cache_creation), _YELLOW, color),
        _c(_si(usage.cache_read), _GREEN, color),
        _c(_si(usage.fresh), _CYAN, color),
    ))
    hit = usage.hit_pct
    rate = _c(f"↻{hit}%+{100 - hit}%", _hit_color(hit), color)
    return f"↑{up} {rate} ↓{_si(usage.completion)}"


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
) -> str:
    """把各来源拼成一行。纯函数,便于单测。组间双空格,组内符号,靠颜色区分语义。"""
    s = slp or {}
    model = _short_model(s.get("model"))
    meta = f"{_short_provider(s.get('custom_llm_provider'))}/{_short_call_type(s.get('call_type'))}"
    response_time = s.get("response_time")
    is_stream = bool(s.get("stream"))
    dur = f"{response_time:.2f}s" if isinstance(response_time, (int, float)) else "?"

    groups = [f"{_c(model, _CYAN, color)} {_c(meta, _DIM, color)}"]

    byte_bits = [b for b in (
        (f"↑{_bytes(req_bytes)}" if req_bytes is not None else None),
        (f"↓{_bytes(resp_bytes)}" if resp_bytes is not None else None),
    ) if b]
    if byte_bits:
        groups.append(_c(" ".join(byte_bits), _BLUE, color))

    groups.append(_tokens_group(usage, s, color))

    tail = [_c(dur, _dur_color(response_time), color)]
    if failed:
        tail.append(_c("FAILED", _BOLD_RED, color))
    if stop_reason:
        tail.append(_c(stop_reason, _STOP_COLOR.get(stop_reason, _MAGENTA), color))
    if is_stream:
        tail.append(_c("stream", _DIM, color))
    groups.append(" ".join(tail))

    return "  ".join(groups) + diagnose_suffix


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
    total = float(rt) if isinstance(rt, (int, float)) and not isinstance(rt, bool) else (
        end - start if start is not None and end is not None else None)
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


def log_success(kwargs: Any, response_obj: Any, start_time: Any, end_time: Any) -> None:
    """成功后打一行。用时已在 standard_logging_object.response_time,此处不重复计算。"""
    cfg = load_config()
    rc = cfg.get("request_log") or {}
    _apply_uvicorn_suppression(rc.get("suppress_uvicorn_access"))
    if not rc.get("enabled"):
        return
    slp = kwargs.get("standard_logging_object") if isinstance(kwargs, dict) else None
    s = slp or {}
    usage = _extract_usage(response_obj)
    req_bytes = _json_bytes(s.get("messages"))
    resp_bytes = _json_bytes(s.get("response"))
    stop = _to_anthropic_stop_reason(_raw_finish_reason(response_obj))
    suffix = ""
    if rc.get("diagnose"):
        u = _get(response_obj, "usage")
        suffix = (f"  [obj={type(response_obj).__name__} usage={type(u).__name__ if u is not None else None}"
                  f" ptd={_get(u, 'prompt_tokens_details') is not None if u is not None else None}"
                  f" msgs={s.get('messages') is not None} resp={s.get('response') is not None}]")
    _LOGGER.info(format_line(slp, stop, usage, req_bytes, resp_bytes,
                             color=_use_color(rc.get("color")), diagnose_suffix=suffix))
    # 端到端计时落盘(可选):配了 timing_file 才落,供离线聚合 total/ttft/gen 分布。
    timing_file = rc.get("timing_file")
    if timing_file and isinstance(slp, dict):
        append_jsonl(timing_file, build_timing_record(slp, usage))


def log_failure(kwargs: Any, response_obj: Any, start_time: Any, end_time: Any) -> None:
    """失败后打一行(标 FAILED + 截断错误串)。字段与成功行对齐,便于同屏对照。"""
    cfg = load_config()
    rc = cfg.get("request_log") or {}
    _apply_uvicorn_suppression(rc.get("suppress_uvicorn_access"))
    if not rc.get("enabled"):
        return
    slp = kwargs.get("standard_logging_object") if isinstance(kwargs, dict) else None
    err = (slp or {}).get("error_str") if isinstance(slp, dict) else None
    color = _use_color(rc.get("color"))
    line = format_line(slp, None, _extract_usage(response_obj), failed=True, color=color)
    if isinstance(err, str) and err:
        line += " " + _c(err[:200], _RED, color)
    _LOGGER.warning(line)


# 进程启动/热重载时按当前配置应用一次 uvicorn 抑制(best-effort;之后每次 log 再同步)。
try:
    _apply_uvicorn_suppression((load_config().get("request_log") or {}).get("suppress_uvicorn_access"))
except Exception:  # pragma: no cover - 启动期配置不可读时不致命,log 时会再试
    pass
