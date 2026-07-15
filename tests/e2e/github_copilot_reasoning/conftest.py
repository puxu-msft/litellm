"""On-demand e2e suite for the GitHub Copilot gpt reasoning<->thinking bridge.

Three groups, all scoped to the GHC API (github_copilot gpt models):

- ``anthropic_sdk`` / ``billed``: drive the real ``anthropic`` SDK (and thus the real
  copilot backend, consuming quota) through the proxy and assert the reasoning carrier
  round-trips.
- ``billed``: real-backend differential (valid carrier accepted vs tampered rejected)
  and cross-turn continuity.
- ``claude_cli``: drive the real ``claude`` CLI against the proxy and assert Claude Code
  stores the carrier verbatim (the R1 client-storage behavior no unit test can reach).

Everything is opt-in and skips cleanly: billed tests need ``LITELLM_RUN_BILLED=1`` and a
reachable GHC proxy; claude_cli tests additionally need ``LITELLM_RUN_CLAUDE_CLI=1`` and
``claude`` on PATH. Never runs by accident in CI.
"""
from __future__ import annotations

import os
import shutil
from typing import Optional

import httpx
import pytest

PROXY_URL = os.environ.get("GHC_REASONING_PROXY_URL", "http://127.0.0.1:4143").rstrip("/")
MASTER_KEY = os.environ.get("GHC_REASONING_MASTER_KEY", "admin")
GPT_MODEL = os.environ.get("GHC_REASONING_MODEL", "gpt")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "billed: hits the real GHC copilot backend and consumes quota (opt-in)")
    config.addinivalue_line("markers", "anthropic_sdk: uses the real anthropic SDK against the proxy (billed)")
    config.addinivalue_line("markers", "claude_cli: drives the real claude CLI against the proxy (opt-in)")


def _proxy_reason() -> Optional[str]:
    try:
        r = httpx.get(f"{PROXY_URL}/health/liveliness", timeout=5)
    except httpx.HTTPError as exc:
        return f"no GHC proxy at {PROXY_URL}: {exc}"
    return None if r.status_code < 500 else f"GHC proxy at {PROXY_URL} returned {r.status_code}"


def pytest_runtest_setup(item: pytest.Item) -> None:
    billed = item.get_closest_marker("billed") or item.get_closest_marker("anthropic_sdk")
    cli = item.get_closest_marker("claude_cli")
    if billed is not None:
        if os.environ.get("LITELLM_RUN_BILLED") != "1":
            pytest.skip("billed test: set LITELLM_RUN_BILLED=1 to run (consumes copilot quota)")
        reason = _proxy_reason()
        if reason is not None:
            pytest.skip(reason)
    if cli is not None:
        if os.environ.get("LITELLM_RUN_CLAUDE_CLI") != "1":
            pytest.skip("claude_cli test: set LITELLM_RUN_CLAUDE_CLI=1 to run")
        if shutil.which("claude") is None:
            pytest.skip("claude CLI not on PATH")
        reason = _proxy_reason()
        if reason is not None:
            pytest.skip(reason)


@pytest.fixture
def proxy_url() -> str:
    return PROXY_URL


@pytest.fixture
def gpt_model() -> str:
    return GPT_MODEL


@pytest.fixture
def anthropic_client():
    """A real anthropic SDK client pointed at the proxy."""
    anthropic = pytest.importorskip("anthropic")
    return anthropic.Anthropic(base_url=PROXY_URL, api_key=MASTER_KEY, max_retries=0)
