import asyncio

import pytest

from litellm.proxy.common_utils.sse_keepalive import (
    ANTHROPIC_PING_EVENT,
    KEEPALIVE_COMMENT,
    AnthropicKeepaliveStrategy,
    CommentOnlyKeepaliveStrategy,
    StreamLease,
    sse_keepalive,
)


async def _gen_gated(items, gate):
    for it in items:
        if gate is not None:
            await gate.wait()
        yield it


@pytest.mark.asyncio
async def test_idle_emits_comment_then_forwards_real_frame():
    gate = asyncio.Event()
    real = _gen_gated([b"data: real\n\n"], gate)
    lease = StreamLease(inner=real)
    out = []

    async def drive():
        async for f in sse_keepalive(
            real, CommentOnlyKeepaliveStrategy(), interval=0.05, lease=lease
        ):
            out.append(f)

    task = asyncio.ensure_future(drive())
    await asyncio.sleep(0.17)  # ~3 idle intervals, no real frame yet
    gate.set()  # release the real frame
    await task
    assert out.count(KEEPALIVE_COMMENT) >= 2
    assert out[-1] == b"data: real\n\n"


@pytest.mark.asyncio
async def test_fast_stream_emits_no_keepalive():
    async def fast():
        yield b"a\n\n"
        yield b"b\n\n"

    g = fast()
    lease = StreamLease(inner=g)
    out = [
        f
        async for f in sse_keepalive(
            g, CommentOnlyKeepaliveStrategy(), interval=10, lease=lease
        )
    ]
    assert out == [b"a\n\n", b"b\n\n"]


@pytest.mark.asyncio
async def test_producer_task_not_cancelled_across_pings():
    # Positive control: if the combinator recreated/cancelled the __anext__ task
    # per idle tick (e.g. via wait_for), the real frame would never arrive or an
    # "anext already running" RuntimeError would surface.
    gate = asyncio.Event()
    real = _gen_gated([b"slow\n\n"], gate)
    lease = StreamLease(inner=real)
    out = []

    async def drive():
        async for f in sse_keepalive(
            real, CommentOnlyKeepaliveStrategy(), interval=0.03, lease=lease
        ):
            out.append(f)

    t = asyncio.ensure_future(drive())
    await asyncio.sleep(0.1)
    gate.set()
    await t
    assert b"slow\n\n" in out


@pytest.mark.asyncio
async def test_same_tick_prefers_real_frame_over_ping():
    # A frame that resolves right at the interval boundary must be forwarded,
    # not preempted by a keepalive.
    async def resolves_immediately():
        yield b"x\n\n"

    g = resolves_immediately()
    lease = StreamLease(inner=g)
    out = [
        f
        async for f in sse_keepalive(
            g, CommentOnlyKeepaliveStrategy(), interval=0.001, lease=lease
        )
    ]
    assert out == [b"x\n\n"]


@pytest.mark.asyncio
async def test_anthropic_phase2_only_after_message_start_no_ping_on_that_frame():
    gate2 = asyncio.Event()

    async def staged():
        yield b"event: message_start\ndata: {}\n\n"
        await gate2.wait()
        yield b"event: content_block_delta\ndata: {}\n\n"

    g = staged()
    lease = StreamLease(inner=g)
    out = []

    async def drive():
        async for f in sse_keepalive(
            g, AnthropicKeepaliveStrategy(), interval=0.04, lease=lease
        ):
            out.append(f)

    t = asyncio.ensure_future(drive())
    await asyncio.sleep(0.13)  # idle between message_start and next frame -> phase 2
    gate2.set()
    await t

    ms = b"event: message_start\ndata: {}\n\n"
    assert ms in out
    idx_ms = out.index(ms)
    # native ping only after message_start (phase 2)
    assert ANTHROPIC_PING_EVENT in out
    assert ANTHROPIC_PING_EVENT not in out[: idx_ms + 1]
    # message_start frame itself carried no synchronous ping right before it
    assert out[:idx_ms].count(ANTHROPIC_PING_EVENT) == 0
