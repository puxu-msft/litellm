from __future__ import annotations

from pathlib import Path

import pytest

from litellm.proxy.middleware.in_flight_registry import InFlightRegistry, RequestTerminalReason
from litellm.proxy.observability.terminal.bootstrap import (
    ShadowBootstrapDisabled,
    ShadowBootstrapStarted,
    bootstrap_shadow_from_env,
    shutdown_shadow_runtimes,
)


def test_shadow_bootstrap_disabled_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LITELLM_TERMINAL_ARCHIVE_DIR", raising=False)
    assert isinstance(bootstrap_shadow_from_env(InFlightRegistry()), ShadowBootstrapDisabled)


def test_shadow_bootstrap_persists_registry_events(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LITELLM_TERMINAL_ARCHIVE_DIR", str(tmp_path))
    registry = InFlightRegistry()
    result = bootstrap_shadow_from_env(registry)
    assert isinstance(result, ShadowBootstrapStarted)
    record = registry.register(method="GET", path="/", client_ip=None)
    registry.finish(record.id, RequestTerminalReason.COMPLETED)
    assert result.runtime.state.requests == ()
    assert result.runtime.state.terminated_request_ids == frozenset({record.id})
    assert (tmp_path / "catalog.sqlite").exists()
    result.runtime.close()


def test_shutdown_closes_all_shadow_runtimes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    shutdown_shadow_runtimes()
    monkeypatch.setenv("LITELLM_TERMINAL_ARCHIVE_DIR", str(tmp_path))
    result = bootstrap_shadow_from_env(InFlightRegistry())
    assert isinstance(result, ShadowBootstrapStarted)
    assert shutdown_shadow_runtimes() == ()
    assert shutdown_shadow_runtimes() == ()
