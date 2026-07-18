from __future__ import annotations

from pathlib import Path
from dataclasses import replace
from uuid import UUID

import pytest

from litellm.proxy.observability.terminal.archive.spool import LoadCommitted, Opened, WorkerSpool, open_worker_spool
from litellm.proxy.observability.terminal.collector.client import ReplayCompleted, ReplayDeferred, replay_pending
from litellm.proxy.observability.terminal.collector.protocol import RangeNotice
from litellm.proxy.observability.terminal.collector.server import CollectorIPCServer
from tests.test_litellm.proxy.observability.terminal.test_events import WORKER_ID
from tests.test_litellm.proxy.observability.terminal.test_events import envelope
from litellm.proxy.observability.terminal.events import EventEnvelope, EventType


def _event(sequence: int) -> EventEnvelope:
    return replace(
        envelope(EventType.REQUEST_ACCEPTED),
        event_id=UUID(int=sequence + 100),
        worker_sequence=sequence,
    )


def _spool(path: Path) -> WorkerSpool:
    opened = open_worker_spool(path, WORKER_ID)
    assert isinstance(opened, Opened)
    return opened.spool


@pytest.mark.asyncio
async def test_spool_range_is_acked_and_compacted_after_durable_commit(tmp_path: Path) -> None:
    spool = _spool(tmp_path / "spool.sqlite")
    spool.store((_event(0), _event(1)))
    notices: list[RangeNotice] = []

    async def commit(notice: RangeNotice) -> bool:
        notices.append(notice)
        return True

    server = CollectorIPCServer(tmp_path / "collector.sock", commit)
    await server.start()
    result = await replay_pending(spool, tmp_path / "collector.sock")
    await server.close()
    assert result == ReplayCompleted(2, 2)
    assert len(notices) == 1
    pending = spool.load_pending(limit=10)
    assert isinstance(pending, LoadCommitted) and pending.events == ()
    spool.close()


@pytest.mark.asyncio
async def test_no_ack_leaves_spool_pending(tmp_path: Path) -> None:
    spool = _spool(tmp_path / "spool.sqlite")
    spool.store((_event(0),))

    async def reject(_notice: RangeNotice) -> bool:
        return False

    server = CollectorIPCServer(tmp_path / "collector.sock", reject)
    await server.start()
    result = await replay_pending(spool, tmp_path / "collector.sock")
    await server.close()
    assert isinstance(result, ReplayDeferred)
    pending = spool.load_pending(limit=10)
    assert isinstance(pending, LoadCommitted) and pending.events == (_event(0),)
    spool.close()


@pytest.mark.asyncio
async def test_disconnected_collector_leaves_spool_pending(tmp_path: Path) -> None:
    spool = _spool(tmp_path / "spool.sqlite")
    spool.store((_event(0),))
    result = await replay_pending(spool, tmp_path / "missing.sock")
    assert isinstance(result, ReplayDeferred)
    spool.close()
