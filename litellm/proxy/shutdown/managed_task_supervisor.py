"""
Phase 1b accounting supervisor state machine.

Owns the lifecycle of detached accounting work so shutdown can drain it before
tearing down DB/cache. The design encodes the invariants four review rounds
extracted:

- Two independent gates: ``_root_admission_open`` (accept NEW root work) and
  ``_hard_shutdown`` (deadline / force-exit reached). Closing root admission
  must not invalidate already-running leases.
- ``AccountingLease.is_valid()`` checks only ``settled`` + hard-shutdown, never
  root admission, so an in-flight root keeps spawning children during quiesce.
- A root lease settles when the root COROUTINE completes (in ``_run_root``'s
  ``finally``), not when it first spawns a child, so one root can spawn several
  sibling children (``_batch_database_updates`` + ``update_cache`` + …).

Drain / fixed-point and the neutral CompletionToken wiring for LoggingWorker
land in the following slices; this module is the state-machine core.
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
from typing import Coroutine, Optional

from litellm.proxy.shutdown.managed_task_set import ManagedTaskSet


class AccountingLease:
    """A single unit of detached accounting work. Valid (its scope may still
    admit children) until it is settled OR the supervisor hard-shuts-down."""

    __slots__ = ("_supervisor", "_settled")

    def __init__(self, supervisor: "ManagedTaskSupervisor") -> None:
        self._supervisor = supervisor
        self._settled = False

    def is_valid(self) -> bool:
        return not self._settled and not self._supervisor.is_hard_shutdown()

    def settle(self) -> None:
        """Once-only; idempotent. Marks the root coroutine's work complete."""
        self._settled = True

    @property
    def scope(self) -> "AccountingScope":
        return AccountingScope(self)


@dataclasses.dataclass(frozen=True, slots=True)
class AccountingScope:
    """Child-admission capability handed to a running root coroutine (via the
    ``current_accounting_scope`` ContextVar). Delegates validity to its lease."""

    _lease: AccountingLease

    def is_valid(self) -> bool:
        return self._lease.is_valid()


current_accounting_scope: "contextvars.ContextVar[Optional[AccountingScope]]" = contextvars.ContextVar(
    "current_accounting_scope", default=None
)


class ManagedTaskSupervisor:
    def __init__(self) -> None:
        self._tasks = ManagedTaskSet()
        self._root_admission_open = True
        self._hard_shutdown = False
        self._admissions_in_progress = 0

    # ── gates ────────────────────────────────────────────────────────────────

    def is_hard_shutdown(self) -> bool:
        return self._hard_shutdown

    def close_root_admission(self) -> None:
        """Stop accepting NEW root work. Existing leases stay valid."""
        self._root_admission_open = False

    def begin_hard_shutdown(self) -> None:
        """Deadline / force-exit: invalidate every lease so no further children
        are admitted anywhere."""
        self._hard_shutdown = True

    # ── observation ──────────────────────────────────────────────────────────

    def active_task_count(self) -> int:
        return len(self._tasks)

    def admissions_in_progress(self) -> int:
        return self._admissions_in_progress

    # ── admission ────────────────────────────────────────────────────────────

    def acquire_root_lease(self) -> Optional[AccountingLease]:
        if not self._root_admission_open or self._hard_shutdown:
            return None
        return AccountingLease(self)

    def spawn_root(self, coro: "Coroutine[object, object, object]") -> None:
        lease = self.acquire_root_lease()
        if lease is None:
            coro.close()  # dropped: never scheduled, no RuntimeWarning
            return
        self._spawn_tracked(self._run_root(lease, coro))

    def spawn_child(self, scope: Optional[AccountingScope], coro: "Coroutine[object, object, object]") -> None:
        if scope is None or not scope.is_valid():
            coro.close()  # dropped
            return
        # admissions_in_progress brackets the synchronous spawn so drain's
        # fixed-point can't declare "empty" in the window between the task set
        # momentarily emptying and this child being registered.
        self._admissions_in_progress += 1
        try:
            self._spawn_tracked(coro)
        finally:
            self._admissions_in_progress -= 1

    # ── internals ────────────────────────────────────────────────────────────

    async def _run_root(self, lease: AccountingLease, coro: "Coroutine[object, object, object]") -> None:
        token = current_accounting_scope.set(lease.scope)
        try:
            await coro
        finally:
            current_accounting_scope.reset(token)
            lease.settle()

    def _spawn_tracked(self, coro: "Coroutine[object, object, object]") -> None:
        self._tasks.add(asyncio.create_task(coro))
