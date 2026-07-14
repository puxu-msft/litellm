"""Unified asyncio-level absolute deadline enforcement, built on asyncio.wait_for so the
same code path is correct on Python 3.10 through 3.13 (no version branching on
asyncio.timeout/timeout_at). See docs/superpowers/specs/2026-07-13-upstream-http-client-config-design.md.
"""

import asyncio
import inspect
from typing import (
    AsyncIterator,
    Awaitable,
    Callable,
    Generic,
    Optional,
    Protocol,
    TypeVar,
    Union,
    runtime_checkable,
)

import anyio

T = TypeVar("T")


@runtime_checkable
class _AsyncCloseable(Protocol):
    def aclose(self) -> Awaitable[None]: ...


@runtime_checkable
class _SyncCloseable(Protocol):
    def close(self) -> Union[None, Awaitable[None]]: ...


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


class DeadlineBoundAsyncIterator(Generic[T]):
    """Wrap an async byte/chunk iterator so each `__anext__` is subject to the same absolute
    deadline as the request's initial non-streaming await. On timeout, invokes
    `on_timeout_close` (sync or async) inside a shielded cancel scope -- mirroring the existing
    precedent in CustomStreamWrapper.aclose() -- so cleanup itself is never cut short by the
    same cancellation that produced the timeout."""

    def __init__(
        self,
        inner: AsyncIterator[T],
        deadline: Optional[float],
        *,
        on_timeout_close: Callable[[], Union[None, Awaitable[None]]],
        now: Optional[Callable[[], float]] = None,
    ) -> None:
        self._inner = inner
        self._deadline = deadline
        self._on_timeout_close = on_timeout_close
        self._now = now

    def __aiter__(self) -> "DeadlineBoundAsyncIterator[T]":
        return self

    async def __anext__(self) -> T:
        try:
            return await with_deadline(self._deadline, self._inner.__anext__(), now=self._now)
        except DeadlineExceeded:
            await self._shielded_close()
            raise

    async def _shielded_close(self) -> None:
        with anyio.CancelScope(shield=True):
            result = self._on_timeout_close()
            if inspect.isawaitable(result):
                await result

    async def aclose(self) -> None:
        """Delegate to the wrapped inner iterator's own aclose/close. Once CustomStreamWrapper
        wraps its raw completion_stream in one of these (Task 16), `self.completion_stream` IS
        this object, so anything closing it must reach the real connection through here (review
        finding #6). Shielded independently of any caller's own cancel scope (minor #3)."""
        with anyio.CancelScope(shield=True):
            if isinstance(self._inner, _AsyncCloseable):
                await self._inner.aclose()
            elif isinstance(self._inner, _SyncCloseable):
                result = self._inner.close()
                if inspect.isawaitable(result):
                    await result
