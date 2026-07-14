"""
Spawns a real litellm proxy subprocess and sends real OS signals to it, for
graceful-shutdown E2E coverage that the shared live-proxy harness (Transport/
Gateway, which assumes an already-running externally-addressed proxy) can't
express. This suite tests process lifecycle — how the process itself responds
to SIGINT/SIGTERM and exits — not request/response contracts.

Each proxy runs in its own session (start_new_session=True) so the test's own
signals never leak to it, and cleanup can kill the whole process group
(reload/workers spawn children) without orphans.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import signal
import socket
import subprocess
import sys
import time
from typing import List, Literal

import httpx

SpawnMode = Literal["direct", "reload", "workers"]

_BANNED_LOG_PATTERNS = (
    "triggering reconnect",
    "Attempting Prisma DB reconnect",
    "ClientNotConnectedError",
    "LiteLLM Redis Caching: async async_increment",
)


@dataclasses.dataclass(frozen=True, slots=True)
class SpawnedProxy:
    process: "subprocess.Popen[str]"
    port: int
    log_path: pathlib.Path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def spawn_proxy(
    *,
    mode: SpawnMode,
    config_path: pathlib.Path,
    tmp_path: pathlib.Path,
    graceful_shutdown_timeout: float,
) -> SpawnedProxy:
    port = _free_port()
    args: List[str] = [
        sys.executable,
        "-m",
        "litellm.proxy.proxy_cli",
        "--config",
        str(config_path),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    if mode == "reload":
        args.append("--reload")
    elif mode == "workers":
        args += ["--num_workers", "2"]

    env = dict(os.environ)
    env["GRACEFUL_SHUTDOWN_TIMEOUT"] = str(graceful_shutdown_timeout)

    log_path = tmp_path / f"proxy_{mode}_{port}.log"
    log_file = open(log_path, "w", encoding="utf-8")
    process = subprocess.Popen(
        args,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,  # own process group; test signals don't leak in
        env=env,
    )
    return SpawnedProxy(process=process, port=port, log_path=log_path)


def wait_for_health(proxy: SpawnedProxy, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        if proxy.process.poll() is not None:
            raise RuntimeError(f"proxy exited early (rc={proxy.process.returncode}) before healthy:\n{read_log(proxy)}")
        try:
            resp = httpx.get(f"http://127.0.0.1:{proxy.port}/health/liveliness", timeout=1.0)
            if resp.status_code == 200:
                return
        except httpx.HTTPError as exc:
            last = exc
        time.sleep(0.2)
    raise TimeoutError(f"proxy on port {proxy.port} never became healthy: {last}\n{read_log(proxy)}")


def read_log(proxy: SpawnedProxy) -> str:
    try:
        return proxy.log_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def wait_for_log(proxy: SpawnedProxy, needle: str, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in read_log(proxy):
            return True
        time.sleep(0.05)
    return False


def assert_no_shutdown_race(proxy: SpawnedProxy) -> None:
    """The merged stdout+stderr must not contain the reconnect/DB/redis lines
    that mean shutdown raced a background producer instead of quiescing it."""
    text = read_log(proxy)
    hits = [p for p in _BANNED_LOG_PATTERNS if p in text]
    assert not hits, f"shutdown-race log lines present: {hits}\n---\n{text[-2000:]}"


def terminate_process_group(proxy: SpawnedProxy) -> None:
    """Best-effort cleanup: TERM then KILL the whole group so reload/workers
    children never orphan."""
    if proxy.process.poll() is None:
        try:
            os.killpg(os.getpgid(proxy.process.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proxy.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proxy.process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            proxy.process.wait(timeout=5)
