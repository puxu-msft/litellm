from __future__ import annotations

from pathlib import Path
from uuid import UUID

from litellm.proxy.middleware.in_flight_registry import (
    InFlightRegistry,
    RegistryEvent,
    RequestStage,
    RequestTerminalReason,
)
from litellm.proxy.observability.terminal.collector.registry_adapter import RegistryEventAdapter
from litellm.proxy.observability.terminal.collector.runtime import ShadowRuntimeOpened, open_shadow_runtime
from litellm.proxy.observability.terminal.config import ConfigLoaded, load_terminal_logging_config
from litellm.proxy.observability.terminal.events import EventType


def test_registry_events_commit_to_shadow_runtime(tmp_path: Path) -> None:
    loaded = load_terminal_logging_config({"central_path": tmp_path}, poc_live_status=False)
    assert isinstance(loaded, ConfigLoaded)
    opened = open_shadow_runtime(loaded.config, clock=lambda: 0)
    assert isinstance(opened, ShadowRuntimeOpened)
    runtime = opened.runtime
    registry = InFlightRegistry()
    ids = iter((UUID(int=601), UUID(int=602), UUID(int=603)))
    adapter = RegistryEventAdapter(
        UUID(int=600), event_id_source=lambda: next(ids), wall_clock=lambda: 3.0, monotonic_clock=lambda: 2.0
    )
    registry.subscribe(adapter.subscriber(runtime))
    record = registry.register(
        method="POST",
        path="/v1/messages",
        client_ip=None,
        id_source=lambda: UUID(int=699),
        monotonic_clock=lambda: 1.0,
        wall_clock=lambda: 2.0,
    )
    registry.advance_stage(record.id, RequestStage.STREAMING)
    registry.finish(record.id, RequestTerminalReason.COMPLETED)
    assert runtime.state.requests == ()
    assert runtime.state.terminated_request_ids == frozenset({record.id})
    runtime.close()


def test_adapter_maps_all_terminal_reasons() -> None:
    registry = InFlightRegistry()
    captured: list[RegistryEvent] = []
    registry.subscribe(captured.append)
    adapter = RegistryEventAdapter(
        UUID(int=700), event_id_source=lambda: UUID(int=701), wall_clock=lambda: 3.0, monotonic_clock=lambda: 2.0
    )
    expected = (
        (RequestTerminalReason.COMPLETED, EventType.REQUEST_COMPLETED),
        (RequestTerminalReason.FAILED, EventType.REQUEST_FAILED),
        (RequestTerminalReason.CANCELLED, EventType.REQUEST_CANCELLED),
        (RequestTerminalReason.TIMED_OUT, EventType.REQUEST_TIMED_OUT),
        (RequestTerminalReason.SHUTDOWN_DROPPED, EventType.REQUEST_SHUTDOWN_DROPPED),
    )
    for index, (reason, event_type) in enumerate(expected):
        record = registry.register(method="GET", path="/", client_ip=None, id_source=lambda: UUID(int=800 + index))
        registry.finish(record.id, reason)
        assert adapter.convert(captured[-1]).event_type is event_type


def test_adapter_uses_event_time_and_request_relative_monotonic_offset() -> None:
    registry = InFlightRegistry()
    captured: list[RegistryEvent] = []
    registry.subscribe(captured.append)
    registry.register(
        method="GET",
        path="/",
        client_ip=None,
        id_source=lambda: UUID(int=900),
        wall_clock=lambda: 10.0,
        monotonic_clock=lambda: 20.0,
    )
    event = RegistryEventAdapter(
        UUID(int=901), event_id_source=lambda: UUID(int=902), wall_clock=lambda: 12.0, monotonic_clock=lambda: 20.25
    ).convert(captured[0])
    assert event.occurred_at_utc.timestamp() == 12.0
    assert event.monotonic_offset_ns == 250_000_000
