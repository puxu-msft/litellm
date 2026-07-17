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
    Drained,
    DeadlineExceeded,
    ForcedExit,
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
async def test_is_quiescent_observes_running_task_until_completion():
    sup = ManagedTaskSupervisor()
    started = asyncio.Event()
    finish = asyncio.Event()

    async def _root():
        started.set()
        await finish.wait()

    assert sup.is_quiescent() is True
    sup.spawn_root(_root())
    await started.wait()
    assert sup.is_quiescent() is False

    finish.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert sup.is_quiescent() is True


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


# ── drain / fixed-point ──────────────────────────────────────────────────────


def _never_force() -> bool:
    return False


@pytest.mark.asyncio
async def test_drain_returns_drained_immediately_when_empty():
    sup = ManagedTaskSupervisor()
    outcome = await sup.drain(deadline_remaining=lambda: 5.0, is_force_exit=_never_force)
    assert isinstance(outcome, Drained)


@pytest.mark.asyncio
async def test_drain_waits_for_inflight_children_then_returns_drained():
    """The core teardown guarantee: drain does not return until the root AND its
    detached children have finished, so DB/cache teardown can't race them."""
    sup = ManagedTaskSupervisor()
    child_done = asyncio.Event()

    async def _child():
        await asyncio.sleep(0.05)
        child_done.set()

    async def _root():
        sup.spawn_child(current_accounting_scope.get(), _child())

    sup.spawn_root(_root())
    await asyncio.sleep(0)  # root scheduled
    outcome = await sup.drain(deadline_remaining=lambda: 5.0, is_force_exit=_never_force)
    assert isinstance(outcome, Drained)
    assert child_done.is_set() is True  # drain waited for the child
    assert sup.active_task_count() == 0


@pytest.mark.asyncio
async def test_drain_deadline_exceeded_cancels_remaining_and_reports_counts():
    sup = ManagedTaskSupervisor()

    async def _hang():
        await asyncio.sleep(100)

    async def _root():
        sup.spawn_child(current_accounting_scope.get(), _hang())
        await asyncio.sleep(100)

    sup.spawn_root(_root())
    await asyncio.sleep(0.01)  # root + child both parked
    outcome = await sup.drain(deadline_remaining=lambda: 0.0, is_force_exit=_never_force)
    assert isinstance(outcome, DeadlineExceeded)
    assert outcome.cancelled >= 2  # root + child
    assert outcome.cancellation_failed == 0
    assert sup.active_task_count() == 0


@pytest.mark.asyncio
async def test_drain_force_exit_returns_forced_exit_variant():
    sup = ManagedTaskSupervisor()

    async def _hang():
        await asyncio.sleep(100)

    sup.spawn_root(_hang())
    await asyncio.sleep(0.01)
    outcome = await sup.drain(deadline_remaining=lambda: 30.0, is_force_exit=lambda: True)
    assert isinstance(outcome, ForcedExit)
    assert outcome.cancelled >= 1
    assert sup.active_task_count() == 0


@pytest.mark.asyncio
async def test_drain_deadline_hard_shutdown_stops_new_children_during_cancel():
    """When drain hits the deadline it hard-shuts-down first, so a task caught
    mid-run cannot smuggle a new child past the cancellation."""
    sup = ManagedTaskSupervisor()
    smuggled_child_ran = False

    async def _smuggled_child():
        nonlocal smuggled_child_ran
        smuggled_child_ran = True

    async def _smuggle():
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            # try to spawn a child on the way out — must be dropped (scope now invalid)
            sup.spawn_child(current_accounting_scope.get(), _smuggled_child())
            raise

    sup.spawn_root(_smuggle())
    await asyncio.sleep(0.01)
    outcome = await sup.drain(deadline_remaining=lambda: 0.0, is_force_exit=_never_force)
    await asyncio.sleep(0.01)  # give any (wrongly) admitted child a chance to run
    assert isinstance(outcome, DeadlineExceeded)
    assert smuggled_child_ran is False
    assert sup.active_task_count() == 0
