from __future__ import annotations

from collections.abc import AsyncIterator

import httpx

from litellm.proxy.observability.terminal.capture.stream import ChunkObserver, observe_fail_open
from litellm.proxy.observability.terminal.events import BodyBoundary


class ObservedAsyncStream(httpx.AsyncByteStream):
    def __init__(self, inner: httpx.AsyncByteStream, observer: ChunkObserver, boundary: BodyBoundary) -> None:
        self._inner = inner
        self._observer = observer
        self._boundary = boundary
        self._enabled = True

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            if self._enabled:
                self._enabled = observe_fail_open(self._observer, self._boundary, chunk)
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class GithubCopilotObservingTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport, observer: ChunkObserver, *, provider: str) -> None:
        self._inner = inner
        self._observer = observer
        self._enabled = provider == "github_copilot"

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self._enabled:
            return await self._inner.handle_async_request(request)
        if not isinstance(request.stream, httpx.AsyncByteStream):
            raise TypeError("async observer requires an AsyncByteStream request")
        request_stream = ObservedAsyncStream(request.stream, self._observer, BodyBoundary.UPSTREAM_REQUEST)
        request.stream = request_stream
        try:
            response = await self._inner.handle_async_request(request)
        finally:
            await request_stream.aclose()
        if not isinstance(response.stream, httpx.AsyncByteStream):
            await response.aclose()
            raise TypeError("async observer requires an AsyncByteStream response")
        response.stream = ObservedAsyncStream(response.stream, self._observer, BodyBoundary.UPSTREAM_RESPONSE)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()
