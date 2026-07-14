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
