from __future__ import annotations

import io
import sqlite3
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from pydantic import TypeAdapter
from litellm.proxy.observability.terminal.collector.runtime import EventSink, ShadowCommitted, ShadowRuntime
from litellm.proxy.observability.terminal.collector.runtime import ShadowRuntimeOpened, open_shadow_runtime
from litellm.proxy.observability.terminal.config import (
    ConfigInvalid,
    ConfigLoaded,
    DynamicTerminalConfig,
    TerminalLoggingConfig,
    TerminalLoggingMode,
    load_terminal_logging_config,
)
from litellm.proxy.observability.terminal.events import EventType
from litellm.proxy.observability.terminal.render.plain_renderer import JsonLineSink, PlainLineSink, parse_json_line
from tests.test_litellm.proxy.observability.terminal.test_events import REQUEST_ID, envelope

_JSON_OBJECT = TypeAdapter(dict[str, object])


def _config(root: Path) -> TerminalLoggingConfig:
    loaded = load_terminal_logging_config({"central_path": root}, poc_live_status=False)
    assert isinstance(loaded, ConfigLoaded)
    return loaded.config


def _runtime(root: Path, sink: EventSink | None = None) -> ShadowRuntime:
    opened = open_shadow_runtime(_config(root), sink=sink, clock=lambda: 0)
    assert isinstance(opened, ShadowRuntimeOpened)
    return opened.runtime


def test_config_defaults_and_dynamic_reconfigure() -> None:
    loaded = load_terminal_logging_config({}, poc_live_status=False)
    assert isinstance(loaded, ConfigLoaded)
    assert loaded.config.static.mode is TerminalLoggingMode.AUTO
    updated = loaded.config.reconfigure(DynamicTerminalConfig(8, 5.0, 20.0, "none"))
    assert updated.static is loaded.config.static
    assert updated.dynamic.refresh_hz == 8


def test_config_rejects_dual_interactive_owners() -> None:
    result = load_terminal_logging_config({"mode": "interactive-experimental"}, poc_live_status=True)
    assert isinstance(result, ConfigInvalid)
    assert "conflicts" in result.detail


def test_config_rejects_invalid_threshold_refresh_and_unknown_field() -> None:
    for raw in ({"segment_max_bytes": 0}, {"refresh_hz": 0}, {"unknown": True}):
        assert isinstance(load_terminal_logging_config(raw, poc_live_status=False), ConfigInvalid)


def test_shadow_runtime_commits_once_projects_and_emits_json(tmp_path: Path) -> None:
    output = io.StringIO()
    runtime = _runtime(tmp_path, JsonLineSink(output))
    accepted = envelope(EventType.REQUEST_ACCEPTED)
    result = runtime.commit(accepted)
    duplicate = runtime.commit(accepted)
    runtime.close()
    assert isinstance(result, ShadowCommitted) and result.inserted
    assert isinstance(duplicate, ShadowCommitted) and not duplicate.inserted
    assert result.state.requests[0].request_id == REQUEST_ID
    lines = output.getvalue().splitlines()
    assert len(lines) == 1
    assert _JSON_OBJECT.validate_python(parse_json_line(lines[0]))["event_type"] == "request.accepted"
    segment = next((tmp_path / "segments").glob("*.active.sqlite"))
    with sqlite3.connect(segment) as connection:
        assert connection.execute("SELECT COUNT(*) FROM terminal_events").fetchone() == (1,)


def test_terminal_event_removes_projected_request(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    accepted = envelope(EventType.REQUEST_ACCEPTED)
    completed = replace(
        envelope(EventType.REQUEST_COMPLETED),
        event_id=UUID("00000000-0000-4000-8000-000000000077"),
        worker_sequence=accepted.worker_sequence + 1,
    )
    runtime.commit(accepted)
    result = runtime.commit(completed)
    runtime.close()
    assert isinstance(result, ShadowCommitted)
    assert result.state.requests == ()


def test_runtime_restores_projection_from_durable_events(tmp_path: Path) -> None:
    first = _runtime(tmp_path)
    first.commit(envelope(EventType.REQUEST_ACCEPTED))
    first.close()
    second = _runtime(tmp_path)
    assert second.state.requests[0].request_id == REQUEST_ID
    second.close()


def test_lifecycle_after_terminal_records_anomaly_without_resurrection(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    accepted = envelope(EventType.REQUEST_ACCEPTED)
    completed = replace(
        envelope(EventType.REQUEST_COMPLETED),
        event_id=UUID("00000000-0000-4000-8000-000000000088"),
        worker_sequence=8,
    )
    late = replace(
        envelope(EventType.REQUEST_ROUTED),
        event_id=UUID("00000000-0000-4000-8000-000000000089"),
        worker_sequence=9,
    )
    runtime.commit(accepted)
    runtime.commit(completed)
    result = runtime.commit(late)
    runtime.close()
    assert isinstance(result, ShadowCommitted)
    assert result.state.requests == ()
    assert result.state.anomalies == (f"lifecycle_after_terminal:{REQUEST_ID}",)


def test_restart_rebuilds_missing_catalog_owner_from_durable_event(tmp_path: Path) -> None:
    first = _runtime(tmp_path)
    first.commit(envelope(EventType.REQUEST_ACCEPTED))
    first.close()
    with sqlite3.connect(tmp_path / "catalog.sqlite") as connection:
        connection.execute("DELETE FROM request_owners")
    second = _runtime(tmp_path)
    segment_id = second.owner_segment(REQUEST_ID)
    second.close()
    assert segment_id is not None


def test_request_terminal_event_stays_in_owner_segment_after_rotation(tmp_path: Path) -> None:
    loaded = load_terminal_logging_config(
        {"central_path": tmp_path, "segment_max_age_seconds": 1}, poc_live_status=False
    )
    assert isinstance(loaded, ConfigLoaded)
    ticks = iter((0, 0, 2_000_000_000, 2_000_000_000))
    opened = open_shadow_runtime(loaded.config, clock=lambda: next(ticks))
    assert isinstance(opened, ShadowRuntimeOpened)
    runtime = opened.runtime
    accepted = envelope(EventType.REQUEST_ACCEPTED)
    runtime.commit(accepted)
    owner = runtime.owner_segment(REQUEST_ID)
    unrelated = replace(
        envelope(EventType.REQUEST_ACCEPTED),
        event_id=UUID(int=990),
        request_id=UUID(int=991),
        worker_sequence=9,
    )
    runtime.commit(unrelated)
    terminal = replace(envelope(EventType.REQUEST_COMPLETED), event_id=UUID(int=992), worker_sequence=10)
    runtime.commit(terminal)
    assert owner is not None
    with sqlite3.connect(runtime.segment_path(owner)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM terminal_events").fetchone() == (2,)
    runtime.close()


def test_plain_sink_emits_readable_metadata_only() -> None:
    output = io.StringIO()
    PlainLineSink(output).emit(envelope(EventType.LOG_RECORD))
    line = output.getvalue()
    assert "log.record" in line
    assert "worker=" in line
    assert "payload" not in line
