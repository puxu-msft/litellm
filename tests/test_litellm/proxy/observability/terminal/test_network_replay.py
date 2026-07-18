from __future__ import annotations

import httpx
import pytest

from litellm.proxy.observability.terminal.replay.network import ReplayDryRun, ReplaySent, replay_request


@pytest.mark.asyncio
async def test_network_replay_is_dry_run_by_default_path() -> None:
    called = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    async def auth():
        return (("authorization", "Bearer current"),)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await replay_request(
            client,
            method="POST",
            url="https://example.invalid",
            body=b"payload",
            headers=(("content-type", "application/json"),),
            auth_headers=auth,
            dry_run=True,
        )
    assert result == ReplayDryRun("POST", "https://example.invalid", 7)
    assert called is False


@pytest.mark.asyncio
async def test_network_replay_uses_current_authenticator() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer current"
        assert await request.aread() == b"payload"
        return httpx.Response(201, content=b"ok")

    async def auth():
        return (("authorization", "Bearer current"),)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await replay_request(
            client,
            method="POST",
            url="https://example.invalid",
            body=b"payload",
            headers=(),
            auth_headers=auth,
            dry_run=False,
        )
    assert result == ReplaySent(201, b"ok")
