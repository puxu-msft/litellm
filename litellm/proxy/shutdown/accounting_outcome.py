"""
Tagged results for accounting boundaries that short-circuit during shutdown
instead of blocking on or retrying a DB/redis call that's unlikely to succeed
while the process is tearing down its connections.

Phase 1a has exactly one member (AccountingSkippedDuringShutdown). Phase 1b
widens this into a full AccountingCompleted | AccountingSkippedDuringShutdown |
AccountingFailed union once LoggingWorker.quiesce()/ManagedTaskSupervisor land
and every accounting boundary routes through it uniformly. The ``kind``
discriminant is stable now so Phase 1b can add members without changing this
one's shape at every call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class AccountingSkippedDuringShutdown:
    """A DB/redis touch was skipped because shutdown is in progress."""

    reason: str
    kind: Literal["skipped_during_shutdown"] = "skipped_during_shutdown"
