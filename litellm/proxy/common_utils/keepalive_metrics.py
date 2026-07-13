"""Prometheus metrics for downstream SSE keepalive.

Defined on the default prometheus_client registry (the one litellm's ``/metrics``
endpoint scrapes) rather than on the ``PrometheusLogger`` callback, because the
keepalive code runs deep in the request path (``create_response``) with no handle
to that logger instance. Metrics are created lazily and guarded: if
``prometheus_client`` is not installed the record functions are silent no-ops, so
keepalive never depends on the metrics backend.

Series (all labelled by ``surface`` = anthropic / openai_chat / openai_responses):
- ``litellm_keepalive_active_streams`` (gauge) — currently-open keepalive streams
- ``litellm_keepalive_pings_sent_total`` (counter) — keepalive frames emitted
- ``litellm_keepalive_stream_duration_seconds`` (histogram) — committed-stream age
- ``litellm_keepalive_terminations_total`` (counter, +``reason``) — how streams ended
"""

from __future__ import annotations

# prometheus_client ships incomplete type info; its metric methods (.labels/.inc/
# .observe) read as Unknown. This file is pure metric plumbing against that lib.
# pyright: reportUnknownMemberType=false, reportAttributeAccessIssue=false

from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from prometheus_client import Counter, Gauge, Histogram


@dataclass(frozen=True, slots=True)
class _KeepaliveMetrics:
    active_streams: "Gauge"
    pings_sent: "Counter"
    stream_duration: "Histogram"
    terminations: "Counter"


@lru_cache(maxsize=1)
def _metrics() -> Optional[_KeepaliveMetrics]:
    try:
        from prometheus_client import Counter, Gauge, Histogram
    except ImportError:
        return None
    return _KeepaliveMetrics(
        active_streams=Gauge(
            "litellm_keepalive_active_streams",
            "Currently-open downstream SSE keepalive streams",
            ["surface"],
        ),
        pings_sent=Counter(
            "litellm_keepalive_pings_sent_total",
            "Total downstream SSE keepalive frames emitted",
            ["surface"],
        ),
        stream_duration=Histogram(
            "litellm_keepalive_stream_duration_seconds",
            "Age of a committed keepalive stream at end",
            ["surface"],
        ),
        terminations=Counter(
            "litellm_keepalive_terminations_total",
            "Keepalive streams by how they ended",
            ["surface", "reason"],
        ),
    )


def record_pings(surface: str, count: int) -> None:
    m = _metrics()
    if m is not None and count > 0:
        m.pings_sent.labels(surface=surface).inc(count)


def stream_started(surface: str) -> None:
    m = _metrics()
    if m is not None:
        m.active_streams.labels(surface=surface).inc()


def stream_ended(surface: str, reason: str, duration_seconds: float) -> None:
    m = _metrics()
    if m is not None:
        m.active_streams.labels(surface=surface).dec()
        m.stream_duration.labels(surface=surface).observe(duration_seconds)
        m.terminations.labels(surface=surface, reason=reason).inc()
