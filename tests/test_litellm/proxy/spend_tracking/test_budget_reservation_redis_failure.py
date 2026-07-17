"""
Regression test for enforced-spend underreporting when Redis fails during the
budget-reservation reconcile step of ``increment_spend_counters``.

Production failure mode: a managed Redis returns an intermittent timeout on the
reconcile increment. Reconcile deletes (invalidates) the shared counter and
gives up, but ``increment_spend_counters`` still treats the counter as
"already reconciled" and skips the direct increment. The actual call cost never
lands in the enforced counter, so budgets stop gating until the next cold
reseed pulls a lagging value from the DB.

The fix makes the reconcile path fall back to the direct increment when it
fails, so the actual cost is always written to the shared counter.
"""

import pytest

from litellm.caching import DualCache
from litellm.proxy import proxy_server


class _FlakyRedisCache:
    def __init__(self) -> None:
        self._store: dict = {}
        self._increment_calls = 0

    async def async_increment(self, key, value, **kwargs):
        self._increment_calls += 1
        if self._increment_calls == 1:
            raise Exception("Redis timeout")
        self._store[key] = float(self._store.get(key, 0.0)) + float(value)
        return self._store[key]

    async def async_get_cache(self, key, *args, **kwargs):
        return self._store.get(key)

    async def async_delete_cache(self, key, *args, **kwargs):
        self._store.pop(key, None)

    async def async_set_cache(self, key, value, *args, **kwargs):
        self._store[key] = float(value)
        return True


@pytest.mark.asyncio
async def test_direct_increment_runs_when_reservation_reconcile_hits_redis_failure(
    monkeypatch,
):
    hashed_token = "hashed_test_token"
    counter_key = f"spend:key:{hashed_token}"
    reserved_cost = 0.5
    response_cost = 1.0

    flaky_redis = _FlakyRedisCache()
    flaky_redis._store[counter_key] = reserved_cost

    monkeypatch.setattr(proxy_server, "prisma_client", None)
    monkeypatch.setattr(proxy_server, "user_api_key_cache", DualCache())
    monkeypatch.setattr(proxy_server.spend_counter_cache, "redis_cache", flaky_redis)
    proxy_server.spend_counter_cache.in_memory_cache.set_cache(key=counter_key, value=reserved_cost)

    budget_reservation = {
        "reserved_cost": reserved_cost,
        "finalized": False,
        "entries": [
            {
                "counter_key": counter_key,
                "entity_type": "Key",
                "entity_id": hashed_token,
                "reserved_cost": reserved_cost,
                "applied_adjustment": 0.0,
            }
        ],
    }

    await proxy_server.increment_spend_counters(
        token=hashed_token,
        team_id=None,
        user_id=None,
        response_cost=response_cost,
        budget_reservation=budget_reservation,
    )

    enforced_spend = await flaky_redis.async_get_cache(key=counter_key)
    assert enforced_spend == response_cost


@pytest.mark.asyncio
async def test_reserve_counter_returns_none_without_typeerror_during_shutdown(monkeypatch):
    """Regression: _increment_spend_counter_cache short-circuits to None during
    shutdown; _reserve_counter does `float(reserved_value)` on it, so returning
    anything non-float-able (an earlier tagged dataclass) would raise TypeError,
    fall into the except branch, log with exc_info and re-touch Redis — exactly
    the shutdown spam/teardown race the guard is meant to remove. The reserve
    must instead cleanly skip (return None)."""
    from litellm.proxy.spend_tracking.budget_reservation import _BudgetCounter, _reserve_counter

    counter = _BudgetCounter(
        counter_key="spend:key:shutdown",
        max_budget=10.0,
        fallback_spend=0.0,
        entity_type="Key",
        entity_id="k",
    )  # no source_cache_key / window -> goes straight to _increment_spend_counter_cache

    proxy_server.GracefulShutdownManager.reset()
    proxy_server.GracefulShutdownManager.start_shutdown()
    try:
        result = await _reserve_counter(counter, reservation_cost=1.0)
    finally:
        proxy_server.GracefulShutdownManager.reset()

    assert result is None
