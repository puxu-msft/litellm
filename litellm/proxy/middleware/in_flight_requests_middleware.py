"""
Tracks the number of HTTP requests currently in-flight on this uvicorn worker.

Used by /health/backlog to expose per-pod queue depth, and emitted as the
Prometheus gauge `litellm_in_flight_requests`.
"""

import os
from typing import Any, Optional

from starlette.types import ASGIApp, Receive, Scope, Send

from litellm.proxy.middleware.in_flight_registry import (
    GLOBAL_IN_FLIGHT_REGISTRY,
    InFlightRegistry,
    RequestTerminalReason,
)
from litellm.proxy.observability.terminal.bootstrap import bootstrap_shadow_from_env, session_hash_from_headers
from litellm.proxy.observability.terminal.bootstrap import observe_current_request_chunk
from litellm.proxy.observability.terminal.capture.context import reset_current_request, set_current_request
from litellm.proxy.observability.terminal.events import BodyBoundary


class InFlightRequestsMiddleware:
    """
    ASGI middleware that increments a counter when a request arrives and
    decrements it when the response is sent (or an error occurs).

    The counter is class-level and therefore scoped to a single uvicorn worker
    process — exactly the per-pod granularity we want.

    Also updates the `litellm_in_flight_requests` Prometheus gauge if
    prometheus_client is installed. The gauge is lazily initialised on the
    first request so that PROMETHEUS_MULTIPROC_DIR is already set by the time
    we register the metric. Initialisation is attempted only once — if
    prometheus_client is absent the class remembers and never retries.
    """

    _in_flight: int = 0
    _gauge: Optional[Any] = None
    _gauge_init_attempted: bool = False

    def __init__(self, app: ASGIApp, registry: InFlightRegistry = GLOBAL_IN_FLIGHT_REGISTRY) -> None:
        self.app = app
        self.registry = registry
        self.shadow_bootstrap = bootstrap_shadow_from_env(registry)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _exclude_control_plane(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        InFlightRequestsMiddleware._in_flight += 1
        client = scope.get("client")
        record = self.registry.register(
            method=scope.get("method", ""),
            path=scope.get("path", ""),
            client_ip=client[0] if client else None,
            session_hash=session_hash_from_headers(tuple(scope.get("headers", ()))),
        )
        gauge = InFlightRequestsMiddleware._get_gauge()
        if gauge is not None:
            gauge.inc()  # type: ignore
        terminal_reason = RequestTerminalReason.COMPLETED
        capture_token = set_current_request(record.id)

        async def observed_receive():
            message = await receive()
            if message["type"] == "http.request" and message.get("body"):
                observe_current_request_chunk(BodyBoundary.CLIENT_REQUEST, message["body"])
            return message

        async def observed_send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                observe_current_request_chunk(BodyBoundary.CLIENT_RESPONSE, message["body"])
            await send(message)
        try:
            await self.app(scope, observed_receive, observed_send)
        except BaseException:
            terminal_reason = RequestTerminalReason.FAILED
            raise
        finally:
            reset_current_request(capture_token)
            self.registry.finish(record.id, terminal_reason)
            InFlightRequestsMiddleware._in_flight -= 1
            if gauge is not None:
                gauge.dec()  # type: ignore

    @staticmethod
    def get_count() -> int:
        """Return the number of HTTP requests currently in-flight."""
        return InFlightRequestsMiddleware._in_flight

    @staticmethod
    def _get_gauge() -> Optional[Any]:
        if InFlightRequestsMiddleware._gauge_init_attempted:
            return InFlightRequestsMiddleware._gauge
        InFlightRequestsMiddleware._gauge_init_attempted = True
        try:
            from prometheus_client import Gauge

            if "PROMETHEUS_MULTIPROC_DIR" in os.environ:
                # livesum aggregates across all worker processes in the scrape response
                InFlightRequestsMiddleware._gauge = Gauge(
                    "litellm_in_flight_requests",
                    "Number of HTTP requests currently in-flight on this uvicorn worker",
                    multiprocess_mode="livesum",
                )
            else:
                InFlightRequestsMiddleware._gauge = Gauge(
                    "litellm_in_flight_requests",
                    "Number of HTTP requests currently in-flight on this uvicorn worker",
                )
        except Exception:
            InFlightRequestsMiddleware._gauge = None
        return InFlightRequestsMiddleware._gauge


def get_in_flight_requests() -> int:
    """Module-level convenience wrapper used by the /health/backlog endpoint."""
    return InFlightRequestsMiddleware.get_count()


def _exclude_control_plane(path: str) -> bool:
    return path == "/metrics" or path.startswith("/health") or path.startswith("/terminal-archive")
