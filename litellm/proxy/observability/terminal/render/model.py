from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CompletionStatus(StrEnum):
    OK = "[ OK ]"
    FAIL = "[FAIL]"
    CANCELLED = "[CANC]"
    TIMEOUT = "[TIME]"


@dataclass(frozen=True, slots=True)
class TokenBreakdown:
    cache_write: int | None
    cache_read: int | None
    uncached: int | None
    output: int | None


@dataclass(frozen=True, slots=True)
class CompletionRecord:
    status: CompletionStatus
    completed_at: str
    session_symbol: str
    session_hash: str
    surface: str
    model: str
    provider: str
    http_status: int
    duration_seconds: float
    ttft_seconds: float | None
    upstream_request_bytes: int | None
    upstream_response_bytes: int | None
    tokens: TokenBreakdown | None
    tools: tuple[str, ...] = ()
    thinking: tuple[tuple[str, int], ...] = ()
    retry_summary: tuple[str, ...] = ()
    non_stream: bool = False
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class InFlightGroup:
    surface: str
    model: str
    provider: str
    count: int
    oldest_elapsed_seconds: float


def format_completion(record: CompletionRecord) -> str:
    parts = [
        record.status.value,
        record.completed_at,
        record.session_symbol,
        record.session_hash,
        f"{record.surface}/{record.model}",
        f"· {record.provider}",
        str(record.http_status),
        f"{record.duration_seconds:.2f}s",
    ]
    return " ".join((*parts, *_timing_and_bytes(record), *_token_parts(record.tokens), *_detail_parts(record)))


def _timing_and_bytes(record: CompletionRecord) -> tuple[str, ...]:
    return tuple(
        value
        for value in (
            f"ttft:{record.ttft_seconds:.2f}s" if record.ttft_seconds is not None else None,
            f"↑{_bytes(record.upstream_request_bytes)}" if record.upstream_request_bytes is not None else None,
            f"↓{_bytes(record.upstream_response_bytes)}" if record.upstream_response_bytes is not None else None,
        )
        if value is not None
    )


def _token_parts(tokens: TokenBreakdown | None) -> tuple[str, ...]:
    if tokens is None:
        return ()
    values = (tokens.cache_write, tokens.cache_read, tokens.uncached)
    inputs = "↑" + "+".join(_count(value) for value in values)
    percentages: tuple[str, ...] = ()
    if all(value is not None for value in values):
        known = tuple(value or 0 for value in values)
        total = sum(known)
        if total:
            percentages = ("↻" + "+".join(f"{round(value * 100 / total)}%" for value in known),)
    output = (f"↓{_count(tokens.output)}",) if tokens.output is not None else ()
    return (inputs, *percentages, *output)


def _detail_parts(record: CompletionRecord) -> tuple[str, ...]:
    retry = ("retry(" + ",".join(record.retry_summary) + ")",) if record.retry_summary else ()
    tools = ("tool_use(" + ",".join(record.tools) + ")",) if record.tools else ()
    thinking = tuple(f"think:{kind}({count})" for kind, count in record.thinking)
    stream = ("(non-stream)",) if record.non_stream else ()
    reason = (f"reason={record.reason}",) if record.reason else ()
    return (*retry, *tools, *thinking, *stream, *reason)


def format_footer(groups: tuple[InFlightGroup, ...], *, width: int) -> str:
    ordered = tuple(sorted(groups, key=lambda group: group.oldest_elapsed_seconds, reverse=True))
    prefix = f"[ .. ] {sum(group.count for group in ordered)} in-flight"
    rendered: tuple[str, ...] = ()
    for index, group in enumerate(ordered):
        item = (
            f"{group.surface}/{group.model}@{group.provider}"
            f"{' ×' + str(group.count) if group.count > 1 else ''} {group.oldest_elapsed_seconds:.2f}s"
        )
        remaining = len(ordered) - index - 1
        suffix = f"  +{remaining} groups" if remaining else ""
        candidate = prefix + ("  " + "  ".join((*rendered, item)) if rendered or item else "") + suffix
        if len(candidate) > width and rendered:
            return prefix + "  " + "  ".join(rendered) + f"  +{len(ordered) - len(rendered)} groups"
        if len(candidate) > width:
            return prefix + f"  +{len(ordered)} groups"
        rendered = (*rendered, item)
    return prefix if not rendered else prefix + "  " + "  ".join(rendered)


def _count(value: int | None) -> str:
    if value is None:
        return "?"
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1_000:.1f}k"
    return f"{value / 1_000_000:.1f}m"


def _bytes(value: int) -> str:
    if value < 1024:
        return f"{value}B"
    if value < 1024**2:
        return f"{value / 1024:.1f}KB"
    return f"{value / 1024**2:.1f}MB"
