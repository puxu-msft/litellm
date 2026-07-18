from __future__ import annotations

from uuid import UUID

import pytest

from litellm.proxy.middleware.in_flight_registry import (
    InFlightRegistry,
    RegistryEvent,
    RequestStage,
    RequestTerminalReason,
)


REQUEST_ID = UUID("00000000-0000-4000-8000-000000000501")


def test_registry_lifecycle_stage_monotonic_and_terminal() -> None:
    registry = InFlightRegistry()
    events: list[RegistryEvent] = []
    registry.subscribe(events.append)
    record = registry.register(
        method="POST",
        path="/v1/messages",
        client_ip="127.0.0.1",
        id_source=lambda: REQUEST_ID,
        monotonic_clock=lambda: 10.0,
        wall_clock=lambda: 20.0,
    )
    registry.advance_stage(record.id, RequestStage.STREAMING)
    updated = registry.advance_stage(record.id, RequestStage.AUTH)
    assert updated.stage is RequestStage.STREAMING
    registry.finish(record.id, RequestTerminalReason.COMPLETED)
    assert registry.snapshot() == ()
    assert tuple(event.sequence for event in events) == (1, 2, 3, 4)
    assert events[-1].terminal_reason is RequestTerminalReason.COMPLETED


def test_registry_context_update_preserves_owned_fields() -> None:
    registry = InFlightRegistry()
    record = registry.register(method="POST", path="/v1/messages", client_ip=None, id_source=lambda: REQUEST_ID)
    updated = registry.set_llm_context(
        record.id,
        model="opus",
        call_type="anthropic_messages",
        provider="github_copilot",
        streaming=True,
    )
    assert updated.method == "POST"
    assert updated.model == "opus"
    assert updated.version == 2


def test_registry_rejects_updates_after_terminal() -> None:
    registry = InFlightRegistry()
    record = registry.register(method="POST", path="/", client_ip=None, id_source=lambda: REQUEST_ID)
    registry.finish(record.id, RequestTerminalReason.CANCELLED)
    with pytest.raises(KeyError):
        registry.advance_stage(record.id, RequestStage.UPSTREAM)
