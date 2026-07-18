from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from litellm.proxy.middleware.in_flight_registry import InFlightRegistry
from litellm.proxy.observability.terminal.collector.registry_adapter import RegistryEventAdapter
from litellm.proxy.observability.terminal.collector.runtime import (
    ShadowRuntime,
    ShadowRuntimeOpened,
    open_shadow_runtime,
)
from litellm.proxy.observability.terminal.config import ConfigLoaded, load_terminal_logging_config
from litellm.proxy.observability.terminal.capture.context import current_request_id
from litellm.proxy.observability.terminal.events import BodyBoundary
from litellm.proxy.observability.terminal.identity import SessionHasher
from litellm.proxy.observability.terminal.render.plain_renderer import JsonLineSink


@dataclass(frozen=True, slots=True)
class ShadowBootstrapStarted:
    runtime: ShadowRuntime


@dataclass(frozen=True, slots=True)
class ShadowBootstrapDisabled:
    reason: str


ShadowBootstrapResult = ShadowBootstrapStarted | ShadowBootstrapDisabled


@dataclass(slots=True)
class _BootstrapState:
    started_registries: frozenset[int] = frozenset()
    runtimes: tuple[ShadowRuntime, ...] = ()


_state = _BootstrapState()


def bootstrap_shadow_from_env(registry: InFlightRegistry) -> ShadowBootstrapResult:
    root = os.getenv("LITELLM_TERMINAL_ARCHIVE_DIR")
    if not root:
        return ShadowBootstrapDisabled("LITELLM_TERMINAL_ARCHIVE_DIR is not set")
    registry_key = id(registry)
    if registry_key in _state.started_registries:
        return ShadowBootstrapDisabled("registry is already bootstrapped")
    loaded = load_terminal_logging_config(
        {"central_path": Path(root), "shadow_enabled": True},
        poc_live_status=True,
    )
    if not isinstance(loaded, ConfigLoaded):
        return ShadowBootstrapDisabled(loaded.detail)
    sink = None if sys.stdout.isatty() else JsonLineSink(sys.stdout)
    opened = open_shadow_runtime(loaded.config, sink=sink)
    if not isinstance(opened, ShadowRuntimeOpened):
        return ShadowBootstrapDisabled(opened.detail)
    runtime = opened.runtime
    adapter = RegistryEventAdapter(uuid4())
    registry.subscribe(adapter.subscriber(runtime))
    _state.started_registries = _state.started_registries | {registry_key}
    _state.runtimes = (*_state.runtimes, runtime)
    return ShadowBootstrapStarted(runtime)


def shutdown_shadow_runtimes() -> tuple[str, ...]:
    errors: tuple[str, ...] = ()
    for runtime in _state.runtimes:
        try:
            runtime.close()
        except (OSError, ValueError) as exception:
            errors = (*errors, str(exception))
    _state.runtimes = ()
    _state.started_registries = frozenset()
    return errors


def observe_current_request_chunk(boundary: BodyBoundary, chunk: bytes) -> bool:
    request_id = current_request_id()
    if request_id is None or not _state.runtimes:
        return False
    return all(runtime.observe_chunk(request_id, boundary, chunk) for runtime in _state.runtimes)


def captured_byte_total(request_id: UUID, boundary: BodyBoundary) -> int | None:
    totals = tuple(
        total for runtime in _state.runtimes if (total := runtime.captured_byte_total(request_id, boundary)) is not None
    )
    return sum(totals) if totals else None


class CurrentShadowObserver:
    def observe(self, boundary: BodyBoundary, chunk: bytes) -> None:
        observe_current_request_chunk(boundary, chunk)


CURRENT_SHADOW_OBSERVER = CurrentShadowObserver()


def session_hash_from_headers(headers: tuple[tuple[bytes, bytes], ...]) -> str | None:
    session_id = next(
        (value.decode("utf-8") for name, value in headers if name.lower() == b"x-claude-code-session-id"),
        None,
    )
    return session_hash_from_id(session_id) if session_id is not None else None


def session_hash_from_id(session_id: str) -> str | None:
    root = os.getenv("LITELLM_TERMINAL_ARCHIVE_DIR")
    if root is None:
        return None
    salt_path = Path(root) / "session-hash.key"
    salt_path.parent.mkdir(parents=True, exist_ok=True)
    if not salt_path.exists():
        descriptor = os.open(salt_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, os.urandom(32))
        finally:
            os.close(descriptor)
    digest = SessionHasher(salt_path.read_bytes()).digest(session_id)
    if _state.runtimes:
        return _state.runtimes[0].resolve_session_alias(digest, seen_at_ns=time.time_ns()).display_hash
    return digest.display(4)
