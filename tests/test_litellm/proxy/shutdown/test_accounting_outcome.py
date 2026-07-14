"""AccountingSkippedDuringShutdown: the distinguishable result accounting
boundaries return when a shutdown-in-progress check short-circuits a DB/redis
touch. Phase 1b widens this into a full tagged union; this is the one member
Phase 1a needs."""

import dataclasses

import pytest

from litellm.proxy.shutdown.accounting_outcome import AccountingSkippedDuringShutdown


def test_is_frozen_dataclass_with_reason_and_stable_kind():
    outcome = AccountingSkippedDuringShutdown(reason="redis_connection_error_during_shutdown")
    assert outcome.reason == "redis_connection_error_during_shutdown"
    assert outcome.kind == "skipped_during_shutdown"
    with pytest.raises(dataclasses.FrozenInstanceError):
        outcome.reason = "mutated"  # type: ignore[misc]


def test_two_instances_with_same_reason_are_equal():
    assert AccountingSkippedDuringShutdown(reason="x") == AccountingSkippedDuringShutdown(reason="x")
