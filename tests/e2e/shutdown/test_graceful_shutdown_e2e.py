"""
Real subprocess + real OS signal graceful-shutdown coverage. Each test spawns
an actual litellm proxy process (not an in-process TestClient), fires a
mock-slow request that is genuinely in-flight, sends SIGINT/SIGTERM, and
asserts the process quiesces and exits promptly WITHOUT emitting the reconnect
/ ClientNotConnectedError / redis-spam lines from the original bug report.

This is the process-lifecycle variant (no DB/redis). The DB/redis shutdown-race
variant that exercises the watchdog/reseed/redis boundaries end-to-end needs a
real Postgres+Redis and is covered at unit level (Tasks 4/5/6); it is out of
scope here and would `pytest.skip` without those services.
"""

from __future__ import annotations

import signal
import threading
import time
from typing import Dict

import httpx
import pytest

from .subprocess_harness import (
    assert_no_shutdown_race,
    read_log,
    spawn_proxy,
    terminate_process_group,
    wait_for_health,
    wait_for_log,
)

pytestmark = pytest.mark.spawned_proxy_e2e

_GRACEFUL_TIMEOUT_S = 6.0
_HEALTH_TIMEOUT_S = 40.0


def _fire_slow_request(port: int, results: Dict[str, object]) -> None:
    try:
        resp = httpx.post(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            headers={"Authorization": "Bearer sk-fake"},
            json={"model": "mock-slow-model", "messages": [{"role": "user", "content": "hi"}]},
            timeout=_GRACEFUL_TIMEOUT_S + 10.0,
        )
        results["status_code"] = resp.status_code
    except httpx.HTTPError as exc:
        results["error"] = str(exc)


def test_sigterm_drains_inflight_request_then_exits_promptly(slow_mock_config, tmp_path):
    """SIGTERM mid-request: uvicorn waits for the in-flight mock-slow request to
    finish (2s), then the process exits well within the graceful window — no
    reconnect/DB/redis-spam lines, unlike the original 3-minute silent hang."""
    proxy = spawn_proxy(
        mode="direct", config_path=slow_mock_config, tmp_path=tmp_path, graceful_shutdown_timeout=_GRACEFUL_TIMEOUT_S
    )
    try:
        wait_for_health(proxy, timeout=_HEALTH_TIMEOUT_S)
        results: Dict[str, object] = {}
        thread = threading.Thread(target=_fire_slow_request, args=(proxy.port, results))
        thread.start()
        time.sleep(0.5)  # let the request reach the worker and start its mock_delay

        start = time.monotonic()
        proxy.process.send_signal(signal.SIGTERM)
        rc = proxy.process.wait(timeout=_GRACEFUL_TIMEOUT_S + 8.0)
        elapsed = time.monotonic() - start
        thread.join(timeout=5.0)

        assert results.get("status_code") == 200, f"in-flight request did not complete: {results}\n{read_log(proxy)}"
        assert elapsed < _GRACEFUL_TIMEOUT_S + 4.0, f"shutdown took {elapsed:.1f}s (rc={rc})\n{read_log(proxy)}"
        assert "Application shutdown complete" in read_log(proxy)
        assert_no_shutdown_race(proxy)
    finally:
        terminate_process_group(proxy)


def test_second_sigint_forces_prompt_exit(slow_mock_config, tmp_path):
    """A second SIGINT while draining forces a prompt exit — nowhere near the
    (deliberately long) graceful window. Guards against the original "Ctrl+C
    does nothing, keep waiting" behavior."""
    long_timeout = 60.0
    proxy = spawn_proxy(
        mode="direct", config_path=slow_mock_config, tmp_path=tmp_path, graceful_shutdown_timeout=long_timeout
    )
    try:
        wait_for_health(proxy, timeout=_HEALTH_TIMEOUT_S)
        results: Dict[str, object] = {}
        thread = threading.Thread(target=_fire_slow_request, args=(proxy.port, results))
        thread.start()
        time.sleep(0.5)

        proxy.process.send_signal(signal.SIGINT)
        assert wait_for_log(proxy, "Shutting down", timeout=10.0), read_log(proxy)

        start = time.monotonic()
        proxy.process.send_signal(signal.SIGINT)  # second SIGINT -> force exit
        proxy.process.wait(timeout=10.0)
        elapsed = time.monotonic() - start

        assert elapsed < 5.0, (
            f"second SIGINT took {elapsed:.1f}s, nowhere near {long_timeout}s expected\n{read_log(proxy)}"
        )
        thread.join(timeout=5.0)
    finally:
        terminate_process_group(proxy)
