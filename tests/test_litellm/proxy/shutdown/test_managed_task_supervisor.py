"""Tests for the Phase 1b accounting supervisor state machine.

These pin the invariants that four review rounds surfaced as the failure modes
of a naive design:

- root-admission close must NOT invalidate already-running leases (so their
  detached children keep being tracked during quiesce);
- a root lease settles when the root coroutine COMPLETES, not when it first
  spawns a child, so a single root can spawn several sibling children;
- hard shutdown (deadline / force-exit) invalidates every lease.
"""

from __future__ import annotations

import asyncio

import pytest

from litellm.proxy.shutdown.managed_task_supervisor import (
    ManagedTaskSupervisor,
    current_accounting_scope,
)


@pytest.mark.asyncio
async def test_acquire_root_lease_returns_valid_lease_while_admission_open():
    sup = ManagedTaskSupervisor()
    lease = sup.acquire_root_lease()
    assert lease is not None
    assert lease.is_valid() is True


@pytest.mark.asyncio
async def test_acquire_root_lease_returns_none_after_root_admission_closed():
    sup = ManagedTaskSupervisor()
    sup.close_root_admission()
    assert sup.acquire_root_lease() is None


@pytest.mark.asyncio
async def test_existing_lease_stays_valid_after_root_admission_closed():
    """Blocker-1 invariant: closing root admission only stops NEW roots; an
    already-acquired lease must remain valid so its in-flight children keep
    being admitted and tracked through quiesce."""
    sup = ManagedTaskSupervisor()
    lease = sup.acquire_root_lease()
    assert lease is not None
    sup.close_root_admission()
    assert lease.is_valid() is True


@pytest.mark.asyncio
async def test_hard_shutdown_invalidates_existing_lease():
    sup = ManagedTaskSupervisor()
    lease = sup.acquire_root_lease()
    assert lease is not None
    sup.begin_hard_shutdown()
    assert lease.is_valid() is False


@pytest.mark.asyncio
async def test_spawn_root_settles_lease_only_after_root_coroutine_completes():
    """Blocker-2 invariant: the scope stays valid for the whole root coroutine,
    so a root can spawn multiple sibling children before it settles."""
    sup = ManagedTaskSupervisor()
    release = asyncio.Event()
    scope_valid_during_run: list[bool] = []

    async def _root():
        scope = current_accounting_scope.get()
        assert scope is not None
        # a root that spawns two sibling children, then keeps running
        scope_valid_during_run.append(scope.is_valid())
        await release.wait()
        scope_valid_during_run.append(scope.is_valid())

    sup.spawn_root(_root())
    await asyncio.sleep(0)  # let the root task start
    assert sup.active_task_count() == 1  # root still running, lease not settled

    release.set()
    await asyncio.sleep(0.01)  # let the root finish + done-callback fire
    assert scope_valid_during_run == [True, True]
    assert sup.active_task_count() == 0  # root settled and removed


@pytest.mark.asyncio
async def test_root_can_spawn_multiple_sibling_children_all_tracked():
    """A single root spawns two detached accounting children (mirrors
    _batch_database_updates + update_cache); both must be tracked and the
    supervisor must not go empty until root AND both children finish."""
    sup = ManagedTaskSupervisor()
    child_release = asyncio.Event()

    async def _child():
        await child_release.wait()

    async def _root():
        scope = current_accounting_scope.get()
        sup.spawn_child(scope, _child())
        sup.spawn_child(scope, _child())

    sup.spawn_root(_root())
    await asyncio.sleep(0.01)  # root completes; two children still pending
    assert sup.active_task_count() == 2  # root gone, two children remain tracked

    child_release.set()
    await asyncio.sleep(0.01)
    assert sup.active_task_count() == 0


@pytest.mark.asyncio
async def test_spawn_child_on_invalid_scope_is_dropped_and_closes_coroutine():
    sup = ManagedTaskSupervisor()
    lease = sup.acquire_root_lease()
    scope = lease.scope
    sup.begin_hard_shutdown()  # invalidates the scope

    ran = False

    async def _child():
        nonlocal ran
        ran = True

    sup.spawn_child(scope, _child())
    await asyncio.sleep(0.01)
    assert ran is False
    assert sup.active_task_count() == 0


@pytest.mark.asyncio
async def test_spawn_root_drops_and_closes_coroutine_when_admission_closed():
    sup = ManagedTaskSupervisor()
    sup.close_root_admission()
    ran = False

    async def _root():
        nonlocal ran
        ran = True

    sup.spawn_root(_root())
    await asyncio.sleep(0.01)
    assert ran is False
    assert sup.active_task_count() == 0
