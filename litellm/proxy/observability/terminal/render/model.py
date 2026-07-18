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
    if record.ttft_seconds is not None:
        parts.append(f"ttft:{record.ttft_seconds:.2f}s")
    if record.upstream_request_bytes is not None:
        parts.append(f"↑{_bytes(record.upstream_request_bytes)}")
    if record.upstream_response_bytes is not None:
        parts.append(f"↓{_bytes(record.upstream_response_bytes)}")
    if record.tokens is not None:
        token_parts = tuple(
            _count(value) for value in (record.tokens.cache_write, record.tokens.cache_read, record.tokens.uncached)
        )
        parts.append("↑" + "+".join(token_parts))
        if all(
            value is not None for value in (record.tokens.cache_write, record.tokens.cache_read, record.tokens.uncached)
        ):
            values = (record.tokens.cache_write or 0, record.tokens.cache_read or 0, record.tokens.uncached or 0)
            total = sum(values)
            if total:
                parts.append("↻" + "+".join(f"{round(value * 100 / total)}%" for value in values))
        if record.tokens.output is not None:
            parts.append(f"↓{_count(record.tokens.output)}")
    if record.retry_summary:
        parts.append("retry(" + ",".join(record.retry_summary) + ")")
    if record.tools:
        parts.append("tool_use(" + ",".join(record.tools) + ")")
    parts.extend(f"think:{kind}({count})" for kind, count in record.thinking)
    if record.non_stream:
        parts.append("(non-stream)")
    if record.reason:
        parts.append(f"reason={record.reason}")
    return " ".join(parts)


def format_footer(groups: tuple[InFlightGroup, ...], *, width: int) -> str:
    ordered = tuple(sorted(groups, key=lambda group: group.oldest_elapsed_seconds, reverse=True))
    prefix = f"[ .. ] {sum(group.count for group in ordered)} in-flight"
    rendered: tuple[str, ...] = ()
    for index, group in enumerate(ordered):
        item = (
            f"{group.surface}/{group.model} · {group.provider}"
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
