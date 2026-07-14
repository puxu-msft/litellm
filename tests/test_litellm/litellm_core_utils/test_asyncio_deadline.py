import asyncio

import pytest

from litellm.litellm_core_utils.asyncio_deadline import DeadlineExceeded, with_deadline


@pytest.mark.asyncio
async def test_with_deadline_none_awaits_normally():
    async def _coro():
        return "ok"

    assert await with_deadline(None, _coro()) == "ok"


@pytest.mark.asyncio
async def test_with_deadline_future_deadline_awaits_normally():
    async def _coro():
        return "ok"

    assert await with_deadline(asyncio.get_event_loop().time() + 10, _coro()) == "ok"


@pytest.mark.asyncio
async def test_with_deadline_past_deadline_raises_immediately_without_entering_wait_for():
    """remaining<=0 must raise directly; it must never call asyncio.wait_for at all,
    since wait_for(coro, timeout<=0) has version-dependent edge behavior we do not want
    to depend on."""
    calls = []

    async def _coro():
        calls.append("awaited")
        return "should not run"

    with pytest.raises(DeadlineExceeded):
        await with_deadline(asyncio.get_event_loop().time() - 1, _coro())
    assert calls == []


@pytest.mark.asyncio
async def test_with_deadline_exceeded_mid_await_raises_deadline_exceeded():
    async def _slow():
        await asyncio.sleep(10)
        return "too slow"

    with pytest.raises(DeadlineExceeded):
        await with_deadline(asyncio.get_event_loop().time() + 0.01, _slow())


@pytest.mark.asyncio
async def test_with_deadline_uses_injected_clock():
    """Dependency-injected clock must be used instead of the real loop clock, so tests
    don't need real sleeps to prove deadline math."""
    fake_now = [100.0]

    async def _coro():
        return "ok"

    # deadline is 100.0 (i.e. "now"): remaining == 0 -> must raise, not await
    with pytest.raises(DeadlineExceeded):
        await with_deadline(100.0, _coro(), now=lambda: fake_now[0])


@pytest.mark.asyncio
async def test_deadline_bound_async_iterator_passes_through_with_no_deadline():
    from litellm.litellm_core_utils.asyncio_deadline import DeadlineBoundAsyncIterator

    async def _gen():
        yield b"a"
        yield b"b"

    closed = []
    wrapped = DeadlineBoundAsyncIterator(_gen(), None, on_timeout_close=lambda: closed.append(True))
    received = [chunk async for chunk in wrapped]
    assert received == [b"a", b"b"]
    assert closed == []


@pytest.mark.asyncio
async def test_deadline_bound_async_iterator_raises_and_closes_on_timeout():
    from litellm.litellm_core_utils.asyncio_deadline import (
        DeadlineBoundAsyncIterator,
        DeadlineExceeded,
    )

    async def _gen():
        yield b"first"
        await asyncio.sleep(10)
        yield b"never"  # pragma: no cover

    closed = []

    async def _on_timeout_close():
        closed.append(True)

    wrapped = DeadlineBoundAsyncIterator(
        _gen(), asyncio.get_event_loop().time() + 0.05, on_timeout_close=_on_timeout_close
    )
    received = []
    with pytest.raises(DeadlineExceeded):
        async for chunk in wrapped:
            received.append(chunk)
    assert received == [b"first"]
    assert closed == [True]


@pytest.mark.asyncio
async def test_deadline_bound_async_iterator_does_not_close_on_normal_completion():
    from litellm.litellm_core_utils.asyncio_deadline import DeadlineBoundAsyncIterator

    async def _gen():
        yield b"only"

    closed = []
    wrapped = DeadlineBoundAsyncIterator(
        _gen(), asyncio.get_event_loop().time() + 10, on_timeout_close=lambda: closed.append(True)
    )
    received = [chunk async for chunk in wrapped]
    assert received == [b"only"]
    assert closed == []


@pytest.mark.asyncio
async def test_deadline_bound_async_iterator_aclose_delegates_to_inner_aclose():
    from litellm.litellm_core_utils.asyncio_deadline import DeadlineBoundAsyncIterator

    closed_inner = []

    class _Inner:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def aclose(self):
            closed_inner.append(True)

    wrapped = DeadlineBoundAsyncIterator(_Inner(), None, on_timeout_close=lambda: None)
    await wrapped.aclose()
    assert closed_inner == [True]


@pytest.mark.asyncio
async def test_deadline_bound_async_iterator_aclose_delegates_to_inner_close_when_no_aclose():
    from litellm.litellm_core_utils.asyncio_deadline import DeadlineBoundAsyncIterator

    closed_inner = []

    class _Inner:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        def close(self):
            closed_inner.append(True)

    wrapped = DeadlineBoundAsyncIterator(_Inner(), None, on_timeout_close=lambda: None)
    await wrapped.aclose()
    assert closed_inner == [True]


@pytest.mark.asyncio
async def test_deadline_bound_async_iterator_aclose_shields_itself_from_cancellation():
    import anyio

    from litellm.litellm_core_utils.asyncio_deadline import DeadlineBoundAsyncIterator

    closed_inner = []

    class _Inner:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def aclose(self):
            await asyncio.sleep(0)  # a checkpoint -- an unshielded scope raises Cancelled here
            closed_inner.append(True)

    wrapped = DeadlineBoundAsyncIterator(_Inner(), None, on_timeout_close=lambda: None)

    with anyio.CancelScope() as scope:
        scope.cancel()
        await wrapped.aclose()

    assert closed_inner == [True]
