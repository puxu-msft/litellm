from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send
from pydantic import TypeAdapter

from litellm.proxy.observability.terminal.capture.stream import ChunkObserver, observe_fail_open
from litellm.proxy.observability.terminal.events import BodyBoundary

_BYTES = TypeAdapter(bytes)


class FourBoundaryCaptureMiddleware:
    def __init__(self, app: ASGIApp, observer: ChunkObserver) -> None:
        self._app = app
        self._observer = observer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def observed_receive() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                body = _BYTES.validate_python(message.get("body", b""))
                if body:
                    observe_fail_open(self._observer, BodyBoundary.CLIENT_REQUEST, body)
            return message

        async def observed_send(message: Message) -> None:
            if message["type"] == "http.response.body":
                body = _BYTES.validate_python(message.get("body", b""))
                if body:
                    observe_fail_open(self._observer, BodyBoundary.CLIENT_RESPONSE, body)
            await send(message)

        await self._app(scope, observed_receive, observed_send)
