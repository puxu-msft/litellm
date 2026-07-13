import asyncio

import pytest
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

from litellm.proxy.common_request_processing import create_response
from litellm.proxy.common_utils.sse_keepalive import (
    KEEPALIVE_COMMENT,
    DownstreamSSESurface,
)
from litellm.proxy.common_utils.stream_keepalive_config import (
    ResolvedStreamKeepaliveConfig,
)


async def _drain(resp: StreamingResponse) -> list:
    return [chunk async for chunk in resp.body_iterator]


async def _gen(frames, gate=None):
    for f in frames:
        if gate is not None:
            await gate.wait()
        yield f


@pytest.mark.asyncio
async def test_keepalive_none_behaves_like_today():
    resp = await create_response(
        generator=_gen(["data: a\n\n", "data: b\n\n"]),
        media_type="text/event-stream",
        headers={},
        request=None,
        keepalive=None,
        surface=None,
    )
    assert isinstance(resp, StreamingResponse)
    body = await _drain(resp)
    assert body == ["data: a\n\n", "data: b\n\n"]
    assert KEEPALIVE_COMMENT not in body


@pytest.mark.asyncio
async def test_disabled_keepalive_behaves_like_today():
    resp = await create_response(
        generator=_gen(["data: a\n\n"]),
        media_type="text/event-stream",
        headers={},
        request=None,
        keepalive=ResolvedStreamKeepaliveConfig(enabled=False, interval=0.01),
        surface=DownstreamSSESurface.OPENAI_CHAT,
    )
    assert isinstance(resp, StreamingResponse)
    assert await _drain(resp) == ["data: a\n\n"]


@pytest.mark.asyncio
async def test_slow_commit_emits_keepalive_before_first_real_frame():
    gate = asyncio.Event()
    resp = await asyncio.wait_for(
        create_response(
            generator=_gen(["data: real\n\n"], gate),
            media_type="text/event-stream",
            headers={},
            request=None,
            keepalive=ResolvedStreamKeepaliveConfig(enabled=True, interval=0.05),
            surface=DownstreamSSESurface.OPENAI_CHAT,
        ),
        timeout=1.0,  # must return without waiting for the (gated) first frame
    )
    assert isinstance(resp, StreamingResponse)

    out = []

    async def drain():
        async for chunk in resp.body_iterator:
            out.append(chunk)
            if len(out) >= 3 and not gate.is_set():
                gate.set()  # release the real frame after a few keepalives

    await asyncio.wait_for(drain(), timeout=2.0)
    assert KEEPALIVE_COMMENT in out
    assert out[-1] == "data: real\n\n"
    # keepalive frames precede the real frame
    assert out.index("data: real\n\n") > out.index(KEEPALIVE_COMMENT)


@pytest.mark.asyncio
async def test_fast_first_frame_error_still_returns_json():
    # first frame is an error SSE frame that arrives within interval -> fast path
    # must still convert to a JSON error response (not commit to SSE).
    err = 'data: {"error": {"message": "bad", "code": 400}}\n\n'
    resp = await create_response(
        generator=_gen([err]),
        media_type="text/event-stream",
        headers={},
        request=None,
        keepalive=ResolvedStreamKeepaliveConfig(enabled=True, interval=0.5),
        surface=DownstreamSSESurface.OPENAI_CHAT,
    )
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_fast_first_frame_ok_streams_with_face2_wrapping():
    resp = await create_response(
        generator=_gen(["data: a\n\n", "data: b\n\n"]),
        media_type="text/event-stream",
        headers={},
        request=None,
        keepalive=ResolvedStreamKeepaliveConfig(enabled=True, interval=10),
        surface=DownstreamSSESurface.OPENAI_CHAT,
    )
    assert isinstance(resp, StreamingResponse)
    body = await _drain(resp)
    assert body == ["data: a\n\n", "data: b\n\n"]  # no keepalive when fast


@pytest.mark.asyncio
async def test_slow_commit_httpexception_becomes_anthropic_event_error():
    # Producer raises HTTPException after commit (slow path). Client must see a
    # recognizable event: error frame, not an aborted connection.
    from fastapi import HTTPException

    gate = asyncio.Event()

    async def raising():
        await gate.wait()
        raise HTTPException(status_code=503, detail="upstream boom")
        yield b"unreached\n\n"  # noqa: unreachable - makes this an async generator

    resp = await asyncio.wait_for(
        create_response(
            generator=raising(),
            media_type="text/event-stream",
            headers={},
            request=None,
            keepalive=ResolvedStreamKeepaliveConfig(enabled=True, interval=0.05),
            surface=DownstreamSSESurface.ANTHROPIC,
        ),
        timeout=1.0,
    )
    assert isinstance(resp, StreamingResponse)  # committed to 200 stream

    out = []

    async def drain():
        async for chunk in resp.body_iterator:
            out.append(chunk)
            if not gate.is_set() and len(out) >= 2:
                gate.set()

    await asyncio.wait_for(drain(), timeout=2.0)
    joined = "".join(c.decode() if isinstance(c, bytes) else c for c in out)
    assert "event: error" in joined
    assert '"type": "error"' in joined
    assert "503" in joined


@pytest.mark.asyncio
async def test_disconnect_during_race_returns_499_and_closes_upstream():
    closed = {"aclose": 0}

    async def gen():
        try:
            await asyncio.sleep(3600)
            yield "data: never\n\n"
        finally:
            closed["aclose"] += 1

    class _FakeRequest:
        async def receive(self):
            return {"type": "http.disconnect"}

    resp = await asyncio.wait_for(
        create_response(
            generator=gen(),
            media_type="text/event-stream",
            headers={},
            request=_FakeRequest(),
            keepalive=ResolvedStreamKeepaliveConfig(enabled=True, interval=5),
            surface=DownstreamSSESurface.OPENAI_CHAT,
        ),
        timeout=1.0,
    )
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 499
    await asyncio.sleep(0)
    assert closed["aclose"] == 1  # upstream generator closed exactly once
