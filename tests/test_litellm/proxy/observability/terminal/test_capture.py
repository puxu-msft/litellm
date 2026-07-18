from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route
from starlette.applications import Starlette

from litellm.proxy.observability.terminal.capture.client_http import FourBoundaryCaptureMiddleware
from litellm.proxy.observability.terminal.capture.semantic import (
    FinalThinking,
    FinalToolUse,
    summarize_final_blocks,
)
from litellm.proxy.observability.terminal.capture.upstream_httpx import GithubCopilotObservingTransport
from litellm.proxy.observability.terminal.events import BodyBoundary


@dataclass
class Observer:
    chunks: list[tuple[BodyBoundary, bytes]]
    fail_on: BodyBoundary | None = None

    def observe(self, boundary: BodyBoundary, chunk: bytes) -> None:
        if boundary is self.fail_on:
            raise RuntimeError("observer failed")
        self.chunks.append((boundary, chunk))


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class Transport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.request_chunks: list[bytes] = []
        self.response_stream = Stream((b"one", b"two"))

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert isinstance(request.stream, httpx.AsyncByteStream)
        self.request_chunks = [chunk async for chunk in request.stream]
        return httpx.Response(200, stream=self.response_stream)


@pytest.mark.asyncio
async def test_github_copilot_transport_preserves_and_observes_chunks() -> None:
    inner = Transport()
    observer = Observer([])
    async with httpx.AsyncClient(
        transport=GithubCopilotObservingTransport(inner, observer, provider="github_copilot")
    ) as client:
        request = httpx.Request("POST", "https://example.invalid", content=Stream((b"a", b"b")))
        response = await client.send(request, stream=True)
        received = [chunk async for chunk in response.aiter_raw()]
        await response.aclose()
    assert inner.request_chunks == [b"a", b"b"]
    assert received == [b"one", b"two"]
    assert observer.chunks == [
        (BodyBoundary.UPSTREAM_REQUEST, b"a"),
        (BodyBoundary.UPSTREAM_REQUEST, b"b"),
        (BodyBoundary.UPSTREAM_RESPONSE, b"one"),
        (BodyBoundary.UPSTREAM_RESPONSE, b"two"),
    ]
    assert inner.response_stream.closed


@pytest.mark.asyncio
async def test_non_copilot_transport_is_not_observed() -> None:
    observer = Observer([])
    async with httpx.AsyncClient(
        transport=GithubCopilotObservingTransport(Transport(), observer, provider="openai")
    ) as client:
        response = await client.post("https://example.invalid", content=b"body")
    assert response.content == b"onetwo"
    assert observer.chunks == []


@pytest.mark.asyncio
async def test_client_middleware_observes_request_and_streaming_response_without_changing_bytes() -> None:
    observer = Observer([])

    async def endpoint(request: Request) -> StreamingResponse:
        assert await request.body() == b"request-body"

        async def body() -> AsyncIterator[bytes]:
            yield b"first"
            yield b"second"

        return StreamingResponse(body())

    app = Starlette(routes=[Route("/", endpoint, methods=["POST"])])
    transport = httpx.ASGITransport(app=FourBoundaryCaptureMiddleware(app, observer))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/", content=b"request-body")
    assert response.content == b"firstsecond"
    assert observer.chunks == [
        (BodyBoundary.CLIENT_REQUEST, b"request-body"),
        (BodyBoundary.CLIENT_RESPONSE, b"first"),
        (BodyBoundary.CLIENT_RESPONSE, b"second"),
    ]


def test_semantic_summary_preserves_tool_order_and_counts_thinking() -> None:
    summary = summarize_final_blocks(
        (
            FinalToolUse("Bash"),
            FinalThinking("enc"),
            FinalToolUse("Bash"),
            FinalThinking("redacted"),
            FinalThinking("enc"),
        )
    )
    assert summary.tools == ("Bash", "Bash")
    assert summary.thinking == (("enc", 2), ("redacted", 1))
