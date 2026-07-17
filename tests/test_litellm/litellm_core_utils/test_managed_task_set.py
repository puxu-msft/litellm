"""Tests for the stdlib-asyncio task set shared by LoggingWorker and the accounting supervisor."""

from __future__ import annotations

import asyncio

import pytest

from litellm.litellm_core_utils.managed_task_set import ManagedTaskSet


@pytest.mark.asyncio
async def test_add_tracks_task_and_reports_not_empty():
    task_set = ManagedTaskSet()
    started = asyncio.Event()

    async def hold():
        started.set()
        await asyncio.sleep(10)

    task = asyncio.create_task(hold())
    task_set.add(task)
    await started.wait()
    assert task_set.is_empty() is False
    assert len(task_set) == 1

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_completed_task_is_auto_removed_via_done_callback():
    task_set = ManagedTaskSet()

    async def quick():
        return None

    task = asyncio.create_task(quick())
    task_set.add(task)
    await task
    await asyncio.sleep(0)
    assert task_set.is_empty() is True
    assert len(task_set) == 0


@pytest.mark.asyncio
async def test_cancel_all_counts_zero_for_clean_cancellations():
    task_set = ManagedTaskSet()

    async def hold():
        await asyncio.sleep(10)

    for _ in range(3):
        task_set.add(asyncio.create_task(hold()))

    failures = await task_set.cancel_all_and_count_failures()
    assert failures == 0
    assert task_set.is_empty() is True


@pytest.mark.asyncio
async def test_cancel_all_counts_tasks_that_raise_non_cancelled_errors():
    task_set = ManagedTaskSet()

    async def hold():
        await asyncio.sleep(10)

    async def boom_on_cancel():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise RuntimeError("cleanup blew up") from None

    task_set.add(asyncio.create_task(hold()))
    task_set.add(asyncio.create_task(boom_on_cancel()))
    await asyncio.sleep(0)

    failures = await task_set.cancel_all_and_count_failures()
    assert failures == 1
    assert task_set.is_empty() is True


@pytest.mark.asyncio
async def test_cancel_all_on_empty_set_is_a_noop():
    task_set = ManagedTaskSet()
    failures = await task_set.cancel_all_and_count_failures()
    assert failures == 0
    assert task_set.is_empty() is True
