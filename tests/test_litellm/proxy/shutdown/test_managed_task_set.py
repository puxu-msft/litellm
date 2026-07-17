"""Tests for ManagedTaskSet: the small stdlib-asyncio primitive shared (by
composition) between the accounting supervisor and LoggingWorker in Phase 1b."""

from __future__ import annotations

import asyncio

import pytest

from litellm.proxy.shutdown.managed_task_set import ManagedTaskSet


@pytest.mark.asyncio
async def test_add_tracks_task_and_reports_not_empty():
    ts = ManagedTaskSet()
    started = asyncio.Event()

    async def _hold():
        started.set()
        await asyncio.sleep(10)

    task = asyncio.create_task(_hold())
    ts.add(task)
    await started.wait()
    assert ts.is_empty() is False
    assert len(ts) == 1

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_completed_task_is_auto_removed_via_done_callback():
    ts = ManagedTaskSet()

    async def _quick():
        return None

    task = asyncio.create_task(_quick())
    ts.add(task)
    await task
    await asyncio.sleep(0)  # let the done-callback run
    assert ts.is_empty() is True
    assert len(ts) == 0


@pytest.mark.asyncio
async def test_cancel_all_counts_zero_for_clean_cancellations():
    ts = ManagedTaskSet()

    async def _hold():
        await asyncio.sleep(10)

    for _ in range(3):
        ts.add(asyncio.create_task(_hold()))

    failures = await ts.cancel_all_and_count_failures()
    assert failures == 0
    assert ts.is_empty() is True


@pytest.mark.asyncio
async def test_cancel_all_counts_tasks_that_raise_non_cancelled_errors():
    ts = ManagedTaskSet()

    async def _hold():
        await asyncio.sleep(10)

    async def _boom_on_cancel():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise RuntimeError("cleanup blew up") from None

    ts.add(asyncio.create_task(_hold()))
    ts.add(asyncio.create_task(_boom_on_cancel()))
    await asyncio.sleep(0)  # let both reach their await point

    failures = await ts.cancel_all_and_count_failures()
    assert failures == 1  # the clean cancel doesn't count; the raising one does
    assert ts.is_empty() is True


@pytest.mark.asyncio
async def test_cancel_all_on_empty_set_is_a_noop():
    ts = ManagedTaskSet()
    failures = await ts.cancel_all_and_count_failures()
    assert failures == 0
    assert ts.is_empty() is True
