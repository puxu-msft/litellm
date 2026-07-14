"""Fixtures for the spawned-proxy graceful-shutdown suite.

Uses a custom ``spawned_proxy_e2e`` marker (NOT the shared ``e2e`` marker) so
these tests are not skipped by the parent conftest's "requires an already
running external proxy" gate — this suite spawns its own proxy.
"""

from __future__ import annotations

import pathlib
import textwrap

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "spawned_proxy_e2e: spawns its own litellm proxy subprocess and sends real OS signals",
    )


@pytest.fixture
def slow_mock_config(tmp_path: pathlib.Path) -> pathlib.Path:
    """A minimal config with a mock model whose completion sleeps ~2s, so a
    request is genuinely in-flight when the shutdown signal arrives. No DB/redis
    is configured: this is the process-lifecycle variant."""
    config = tmp_path / "slow_mock_config.yaml"
    config.write_text(
        textwrap.dedent(
            """
            model_list:
              - model_name: mock-slow-model
                litellm_params:
                  model: openai/mock-slow-model
                  api_key: sk-fake
                  mock_response: "hello from mock"
                  mock_delay: 2.0
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return config
