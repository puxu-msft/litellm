"""Tests for DrainingServer: uvicorn.Server subclass that wires signal/shutdown
hooks into GracefulShutdownManager without changing uvicorn's own semantics."""

from __future__ import annotations

import signal
from unittest.mock import MagicMock, patch

import pytest
import uvicorn

from litellm.proxy.shutdown.draining_server import DrainingServer
from litellm.proxy.shutdown.graceful_shutdown_manager import GracefulShutdownManager


@pytest.fixture(autouse=True)
def _reset():
    GracefulShutdownManager.reset()
    yield
    GracefulShutdownManager.reset()


def _make_server() -> DrainingServer:
    config = uvicorn.Config(app=lambda scope, receive, send: None, host="127.0.0.1", port=0)
    return DrainingServer(config=config)


async def _noop_coro():
    return None


def test_handle_exit_first_sigint_starts_shutdown():
    server = _make_server()
    with patch.object(uvicorn.Server, "handle_exit") as mock_super_handle_exit:
        server.handle_exit(signal.SIGINT, None)
    assert GracefulShutdownManager.is_shutting_down() is True
    assert GracefulShutdownManager.is_force_exit() is False
    mock_super_handle_exit.assert_called_once_with(signal.SIGINT, None)


def test_handle_exit_second_sigint_requests_force_exit():
    server = _make_server()
    with patch.object(uvicorn.Server, "handle_exit"):
        server.handle_exit(signal.SIGINT, None)
        server.should_exit = True  # uvicorn's own base handle_exit sets this on first call
        server.handle_exit(signal.SIGINT, None)
    assert GracefulShutdownManager.is_force_exit() is True


def test_handle_exit_sigterm_never_requests_force_exit_even_if_already_exiting():
    server = _make_server()
    server.should_exit = True
    with patch.object(uvicorn.Server, "handle_exit"):
        server.handle_exit(signal.SIGTERM, None)
    assert GracefulShutdownManager.is_force_exit() is False
    assert GracefulShutdownManager.is_shutting_down() is True


@pytest.mark.asyncio
async def test_shutdown_starts_graceful_shutdown_and_sets_uvicorn_timeout(monkeypatch):
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT", "12")
    server = _make_server()
    with patch.object(uvicorn.Server, "shutdown", MagicMock(return_value=_noop_coro())) as mock_super_shutdown:
        await server.shutdown(sockets=None)
    assert GracefulShutdownManager.is_shutting_down() is True
    assert 11.5 < server.config.timeout_graceful_shutdown <= 12.0
    mock_super_shutdown.assert_called_once_with(None)


@pytest.mark.asyncio
async def test_shutdown_clamps_uvicorn_timeout_to_zero_when_deadline_already_passed(monkeypatch):
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT", "0")
    server = _make_server()
    GracefulShutdownManager.start_shutdown()
    with patch.object(uvicorn.Server, "shutdown", MagicMock(return_value=_noop_coro())):
        await server.shutdown(sockets=None)
    assert server.config.timeout_graceful_shutdown == 0.0
