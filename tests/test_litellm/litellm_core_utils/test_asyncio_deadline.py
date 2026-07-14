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


@pytest.mark.asyncio
async def test_with_deadline_zero_remaining_takes_immediate_path_not_wait_for(monkeypatch):
    """At remaining == 0 (deadline == now) the immediate raise must fire; asyncio.wait_for must
    never be entered. Pins the `<= 0` boundary against a `< 0` mutation."""
    import litellm.litellm_core_utils.asyncio_deadline as mod

    wait_for_calls = []
    original_wait_for = asyncio.wait_for

    async def _spy_wait_for(*args, **kwargs):
        wait_for_calls.append(True)
        return await original_wait_for(*args, **kwargs)

    monkeypatch.setattr(mod.asyncio, "wait_for", _spy_wait_for)

    coro = _noop_coro()
    with pytest.raises(DeadlineExceeded):
        await with_deadline(100.0, coro, now=lambda: 100.0)
    assert wait_for_calls == []


async def _noop_coro():
    return "ok"


@pytest.mark.asyncio
async def test_with_deadline_expired_closes_the_passed_coroutine():
    """When the deadline is already past, the coroutine handed in must be explicitly closed
    (not leaked). A closed coroutine has cr_frame is None. Kills the iscoroutine guard mutation."""

    async def _coro():
        return "never"

    coro = _coro()
    try:
        with pytest.raises(DeadlineExceeded):
            await with_deadline(asyncio.get_event_loop().time() - 1, coro, now=None)
        assert coro.cr_frame is None  # closed, not merely un-awaited
    finally:
        coro.close()


@pytest.mark.asyncio
async def test_with_deadline_immediate_raise_carries_message():
    async def _coro():
        return "never"

    with pytest.raises(DeadlineExceeded, match="deadline already exceeded"):
        await with_deadline(asyncio.get_event_loop().time() - 1, _coro())


@pytest.mark.asyncio
async def test_with_deadline_mid_await_raise_carries_message():
    async def _slow():
        await asyncio.sleep(10)

    with pytest.raises(DeadlineExceeded, match="deadline exceeded"):
        await with_deadline(asyncio.get_event_loop().time() + 0.01, _slow())


@pytest.mark.asyncio
async def test_deadline_bound_async_iterator_uses_injected_now_for_deadline_math():
    """The iterator must thread its injected `now` into with_deadline. The deadline is set in the
    real loop clock's *future* (so the real clock would keep going) but in the injected clock's
    *past* (so the injected clock raises immediately) -- proving the injected clock, not the real
    loop clock, drives the decision. Kills self._now drop / now=None mutations."""
    from litellm.litellm_core_utils.asyncio_deadline import (
        DeadlineBoundAsyncIterator,
        DeadlineExceeded,
    )

    async def _gen():
        yield b"first"  # pragma: no cover -- injected clock is already past the deadline
        yield b"second"  # pragma: no cover

    closed = []

    async def _on_timeout_close():
        closed.append(True)

    loop_now = asyncio.get_event_loop().time()
    wrapped = DeadlineBoundAsyncIterator(
        _gen(),
        deadline=loop_now + 1000.0,  # far future for the real loop clock
        on_timeout_close=_on_timeout_close,
        now=lambda: loop_now + 2000.0,  # but already past for the injected clock
    )
    received = []
    with pytest.raises(DeadlineExceeded):
        async for chunk in wrapped:
            received.append(chunk)
    assert received == []
    assert closed == [True]


@pytest.mark.asyncio
async def test_deadline_bound_iterator_shielded_close_completes_under_cancellation():
    """The on_timeout_close cleanup runs inside a shielded cancel scope, so a checkpoint inside it
    survives an outer cancellation already pending when the deadline fires. Kills the
    shield=True -> False/None mutations on _shielded_close."""
    import anyio

    from litellm.litellm_core_utils.asyncio_deadline import (
        DeadlineBoundAsyncIterator,
        DeadlineExceeded,
    )

    async def _gen():
        yield b"x"  # pragma: no cover

    closed = []

    async def _on_timeout_close():
        await asyncio.sleep(0)  # a checkpoint -- an unshielded scope raises Cancelled here
        closed.append(True)

    loop_now = asyncio.get_event_loop().time()
    wrapped = DeadlineBoundAsyncIterator(
        _gen(), deadline=loop_now, on_timeout_close=_on_timeout_close, now=lambda: loop_now + 1.0
    )
    try:
        with anyio.CancelScope() as scope:
            scope.cancel()
            await wrapped.__anext__()
    except (DeadlineExceeded, asyncio.CancelledError):
        pass
    assert closed == [True]


@pytest.mark.asyncio
async def test_deadline_bound_async_iterator_aclose_awaits_sync_close_awaitable_result():
    """If inner has no aclose but its close() returns an awaitable, aclose must await it.
    Kills the isawaitable guard mutation on the sync-closeable branch."""
    from litellm.litellm_core_utils.asyncio_deadline import DeadlineBoundAsyncIterator

    awaited = []

    class _Inner:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        def close(self):
            async def _finish():
                awaited.append(True)

            return _finish()

    wrapped = DeadlineBoundAsyncIterator(_Inner(), None, on_timeout_close=lambda: None)
    await wrapped.aclose()
    assert awaited == [True]
