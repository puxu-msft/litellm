"""Unified asyncio-level absolute deadline enforcement, built on asyncio.wait_for so the
same code path is correct on Python 3.10 through 3.13 (no version branching on
asyncio.timeout/timeout_at). See docs/superpowers/specs/2026-07-13-upstream-http-client-config-design.md.
"""

import asyncio
from typing import Awaitable, Callable, Optional, TypeVar

T = TypeVar("T")


class DeadlineExceeded(TimeoutError):
    """Raised when a request's absolute http_client.total_timeout deadline is exceeded,
    whether during the initial non-streaming await or during streaming byte iteration."""


def _default_now() -> float:
    return asyncio.get_event_loop().time()


async def with_deadline(
    deadline: Optional[float],
    awaitable: Awaitable[T],
    *,
    now: Optional[Callable[[], float]] = None,
) -> T:
    """Await `awaitable`, raising DeadlineExceeded if `deadline` (an absolute loop-time value,
    as produced by `establish_request_deadline`) has already passed or is exceeded before the
    awaitable completes. `deadline=None` means "no deadline": await normally. `now` is
    injectable for deterministic tests; defaults to the running loop's own clock."""
    if deadline is None:
        return await awaitable
    current = now() if now is not None else _default_now()
    remaining = deadline - current
    if remaining <= 0:
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        raise DeadlineExceeded(f"http_client total_timeout deadline already exceeded ({remaining=})")
    try:
        return await asyncio.wait_for(awaitable, timeout=remaining)
    except asyncio.TimeoutError as exc:
        raise DeadlineExceeded(f"http_client total_timeout deadline exceeded (remaining was {remaining}s)") from exc
