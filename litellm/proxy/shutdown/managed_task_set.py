"""
ManagedTaskSet: a minimal, stdlib-only asyncio primitive that owns a set of
running tasks with strong references (so they are not garbage-collected mid
flight), self-removes them on completion, and can cancel-all-and-await
settlement while counting non-cancellation failures.

Shared by composition between the accounting supervisor and LoggingWorker in
Phase 1b's shutdown quiesce; deliberately holds no accounting/lease/admission
semantics of its own so both owners can layer their own policy on top.
"""

from __future__ import annotations

import asyncio


class ManagedTaskSet:
    def __init__(self) -> None:
        # mutable-ok: a live registry of in-flight tasks is inherently mutable —
        # tasks are added as they spawn and discarded as they finish, so there
        # is no one-shot immutable construction that models it.
        self._tasks: "set[asyncio.Task]" = set()

    def add(self, task: "asyncio.Task") -> None:
        self._tasks.add(task)
        # discard (not remove) so a task that was already cancel-drained out of
        # the set by cancel_all_and_count_failures doesn't raise on its late
        # done-callback.
        task.add_done_callback(self._tasks.discard)

    def is_empty(self) -> bool:
        return not self._tasks

    def __len__(self) -> int:
        return len(self._tasks)

    async def cancel_all_and_count_failures(self) -> int:
        """
        Cancel every tracked task, await their settlement, and return the number
        that did NOT settle as a clean cancellation (i.e. raised something other
        than CancelledError). A clean cancel counts as 0; a coroutine that
        swallows cancellation and raises from its finally counts as a failure.
        """
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return sum(
            1
            for result in results
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError)
        )
