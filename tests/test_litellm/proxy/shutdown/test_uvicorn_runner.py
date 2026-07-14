"""Tests for run_uvicorn_with_draining_server: replicates uvicorn.main.run()'s
own should_reload / workers>1 / direct dispatch, swapping in DrainingServer,
and mirrors uvicorn's own KeyboardInterrupt swallow."""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock, patch

from litellm.proxy.shutdown.uvicorn_runner import run_uvicorn_with_draining_server


def _base_args(**overrides: Any) -> Dict[str, Any]:
    args: Dict[str, Any] = {"app": "litellm.proxy.proxy_server:app", "host": "0.0.0.0", "port": 4000}
    args.update(overrides)
    return args


def _mock_server(*, should_reload: bool, workers: int) -> MagicMock:
    server = MagicMock()
    server.config.should_reload = should_reload
    server.config.workers = workers
    server.config.bind_socket.return_value = "SOCK"
    return server


def test_direct_mode_calls_server_run_when_no_reload_and_single_worker():
    with patch("litellm.proxy.shutdown.uvicorn_runner.DrainingServer") as mock_server_cls:
        server = _mock_server(should_reload=False, workers=1)
        mock_server_cls.return_value = server
        run_uvicorn_with_draining_server(_base_args(), workers=1)
    server.run.assert_called_once_with()
    server.config.bind_socket.assert_not_called()


def test_reload_mode_uses_change_reload_supervisor_with_server_run_as_target():
    with (
        patch("litellm.proxy.shutdown.uvicorn_runner.DrainingServer") as mock_server_cls,
        patch("litellm.proxy.shutdown.uvicorn_runner.ChangeReload") as mock_change_reload,
    ):
        server = _mock_server(should_reload=True, workers=1)
        mock_server_cls.return_value = server
        run_uvicorn_with_draining_server(_base_args(reload=True), workers=1)
    mock_change_reload.assert_called_once_with(server.config, target=server.run, sockets=["SOCK"])
    mock_change_reload.return_value.run.assert_called_once_with()
    server.run.assert_not_called()


def test_multiprocess_mode_used_when_workers_greater_than_one():
    with (
        patch("litellm.proxy.shutdown.uvicorn_runner.DrainingServer") as mock_server_cls,
        patch("litellm.proxy.shutdown.uvicorn_runner.Multiprocess") as mock_multiprocess,
    ):
        server = _mock_server(should_reload=False, workers=2)
        mock_server_cls.return_value = server
        run_uvicorn_with_draining_server(_base_args(), workers=2)
    mock_multiprocess.assert_called_once_with(server.config, target=server.run, sockets=["SOCK"])
    mock_multiprocess.return_value.run.assert_called_once_with()
    server.run.assert_not_called()


def test_keyboard_interrupt_from_server_run_is_swallowed_and_returns_normally():
    """A foreground Ctrl+C surfaces as KeyboardInterrupt after uvicorn re-raises
    the captured signal; the runner must swallow it (like uvicorn.main.run) so
    the CLI returns normally instead of a nonzero exit / traceback."""
    with patch("litellm.proxy.shutdown.uvicorn_runner.DrainingServer") as mock_server_cls:
        server = _mock_server(should_reload=False, workers=1)
        server.run.side_effect = KeyboardInterrupt()
        mock_server_cls.return_value = server
        run_uvicorn_with_draining_server(_base_args(), workers=1)  # must not raise
    server.run.assert_called_once_with()
