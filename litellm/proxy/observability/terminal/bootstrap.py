from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from litellm.proxy.middleware.in_flight_registry import InFlightRegistry
from litellm.proxy.observability.terminal.collector.registry_adapter import RegistryEventAdapter
from litellm.proxy.observability.terminal.collector.runtime import (
    ShadowRuntime,
    ShadowRuntimeOpened,
    open_shadow_runtime,
)
from litellm.proxy.observability.terminal.config import ConfigLoaded, load_terminal_logging_config


@dataclass(frozen=True, slots=True)
class ShadowBootstrapStarted:
    runtime: ShadowRuntime


@dataclass(frozen=True, slots=True)
class ShadowBootstrapDisabled:
    reason: str


ShadowBootstrapResult = ShadowBootstrapStarted | ShadowBootstrapDisabled

_started_registries: frozenset[int] = frozenset()
_runtimes: tuple[ShadowRuntime, ...] = ()


def bootstrap_shadow_from_env(registry: InFlightRegistry) -> ShadowBootstrapResult:
    global _started_registries, _runtimes
    root = os.getenv("LITELLM_TERMINAL_ARCHIVE_DIR")
    if not root:
        return ShadowBootstrapDisabled("LITELLM_TERMINAL_ARCHIVE_DIR is not set")
    registry_key = id(registry)
    if registry_key in _started_registries:
        return ShadowBootstrapDisabled("registry is already bootstrapped")
    loaded = load_terminal_logging_config(
        {"central_path": Path(root), "shadow_enabled": True},
        poc_live_status=True,
    )
    if not isinstance(loaded, ConfigLoaded):
        return ShadowBootstrapDisabled(loaded.detail)
    opened = open_shadow_runtime(loaded.config)
    if not isinstance(opened, ShadowRuntimeOpened):
        return ShadowBootstrapDisabled(opened.detail)
    runtime = opened.runtime
    adapter = RegistryEventAdapter(uuid4())
    registry.subscribe(adapter.subscriber(runtime))
    _started_registries = _started_registries | {registry_key}
    _runtimes = (*_runtimes, runtime)
    return ShadowBootstrapStarted(runtime)


def shutdown_shadow_runtimes() -> tuple[str, ...]:
    global _started_registries, _runtimes
    errors: tuple[str, ...] = ()
    for runtime in _runtimes:
        try:
            runtime.close()
        except (OSError, ValueError) as exception:
            errors = (*errors, str(exception))
    _runtimes = ()
    _started_registries = frozenset()
    return errors
