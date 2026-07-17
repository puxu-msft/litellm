"""
Tests for GracefulShutdownManager.

These verify the drain logic that lets a pod terminate as soon as its real
in-flight work is done (bounded by GRACEFUL_SHUTDOWN_TIMEOUT) rather than
sleeping for a fixed worst-case duration.
"""

import time

import pytest

from litellm.proxy.shutdown.graceful_shutdown_manager import (
    DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT,
    GracefulShutdownManager,
)


@pytest.fixture(autouse=True)
def _reset():
    GracefulShutdownManager.reset()
    yield
    GracefulShutdownManager.reset()


def _counter_that_drains_after(calls_before_zero: int):
    """Return a count_fn that reports N in-flight until it has been polled
    `calls_before_zero` times, then reports 0."""
    state = {"polls": 0}

    def count_fn() -> int:
        state["polls"] += 1
        return 0 if state["polls"] > calls_before_zero else 3

    return count_fn


# ── shutdown flag ───────────────────────────────────────────────────────────


def test_not_shutting_down_by_default():
    assert GracefulShutdownManager.is_shutting_down() is False


def test_start_shutdown_sets_flag():
    GracefulShutdownManager.start_shutdown()
    assert GracefulShutdownManager.is_shutting_down() is True


def test_start_shutdown_is_idempotent_and_does_not_reset_clock():
    GracefulShutdownManager.start_shutdown()
    first = GracefulShutdownManager._shutdown_started_at
    time.sleep(0.01)
    GracefulShutdownManager.start_shutdown()
    assert GracefulShutdownManager._shutdown_started_at == first


def test_reset_clears_flag():
    GracefulShutdownManager.start_shutdown()
    GracefulShutdownManager.reset()
    assert GracefulShutdownManager.is_shutting_down() is False


# ── timeout config ────────────────────────────────────────────────────────────


def test_timeout_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("GRACEFUL_SHUTDOWN_TIMEOUT", raising=False)
    assert GracefulShutdownManager.get_timeout() == DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT


def test_timeout_reads_env(monkeypatch):
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT", "5")
    assert GracefulShutdownManager.get_timeout() == 5.0


def test_timeout_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT", "not-a-number")
    assert GracefulShutdownManager.get_timeout() == DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT


# ── wait_for_drain ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_returns_immediately_when_already_drained():
    start = time.monotonic()
    drained = await GracefulShutdownManager.wait_for_drain(timeout=10, count_fn=lambda: 0)
    assert drained == 0
    assert time.monotonic() - start < 0.5


@pytest.mark.asyncio
async def test_waits_until_counter_reaches_zero_then_returns_drained_count():
    count_fn = _counter_that_drains_after(calls_before_zero=3)
    drained = await GracefulShutdownManager.wait_for_drain(timeout=10, count_fn=count_fn)
    assert drained == 3


@pytest.mark.asyncio
async def test_times_out_when_counter_never_drains():
    start = time.monotonic()
    drained = await GracefulShutdownManager.wait_for_drain(timeout=0.3, count_fn=lambda: 2)
    elapsed = time.monotonic() - start
    assert 0.3 <= elapsed < 2.0
    assert drained == 0


@pytest.mark.asyncio
async def test_zero_timeout_does_not_block():
    start = time.monotonic()
    drained = await GracefulShutdownManager.wait_for_drain(timeout=0, count_fn=lambda: 5)
    assert time.monotonic() - start < 0.2
    assert drained == 5


@pytest.mark.asyncio
async def test_exclude_self_treats_one_inflight_as_drained():
    """The /health/drain request counts itself, so a steady count of 1 must be
    treated as fully drained rather than timing out."""
    start = time.monotonic()
    drained = await GracefulShutdownManager.wait_for_drain(timeout=5, exclude_self=True, count_fn=lambda: 1)
    assert time.monotonic() - start < 0.5
    assert drained == 0


@pytest.mark.asyncio
async def test_without_exclude_self_one_inflight_blocks_until_timeout():
    start = time.monotonic()
    await GracefulShutdownManager.wait_for_drain(timeout=0.3, count_fn=lambda: 1)
    assert time.monotonic() - start >= 0.3


@pytest.mark.asyncio
async def test_defaults_to_get_timeout_and_live_counter(monkeypatch):
    """With no timeout/count_fn passed, it falls back to get_timeout() and the
    live InFlightRequestsMiddleware counter."""
    from litellm.proxy.middleware.in_flight_requests_middleware import (
        InFlightRequestsMiddleware,
    )

    monkeypatch.delenv("GRACEFUL_SHUTDOWN_TIMEOUT", raising=False)
    InFlightRequestsMiddleware._in_flight = 0
    drained = await GracefulShutdownManager.wait_for_drain()
    assert drained == 0


@pytest.mark.asyncio
async def test_second_drain_is_a_noop_so_window_is_not_doubled():
    """preStop /health/drain and the lifespan SIGTERM handler both drain; the
    second call must return immediately rather than waiting another full
    timeout (which would require doubling terminationGracePeriodSeconds)."""
    await GracefulShutdownManager.wait_for_drain(timeout=0.2, count_fn=lambda: 1)

    start = time.monotonic()
    drained = await GracefulShutdownManager.wait_for_drain(timeout=5, count_fn=lambda: 1)
    assert time.monotonic() - start < 0.1
    assert drained == 0


@pytest.mark.asyncio
async def test_emits_periodic_drain_waiting_log_while_waiting():
    """With a zero log interval, the periodic drain_waiting branch runs on each
    poll until the counter finally drains."""
    count_fn = _counter_that_drains_after(calls_before_zero=2)
    drained = await GracefulShutdownManager.wait_for_drain(
        timeout=10, count_fn=count_fn, poll_interval=0, log_interval=0
    )
    assert drained == 3


def test_deadline_remaining_is_zero_before_shutdown_starts():
    assert GracefulShutdownManager.deadline_remaining() == 0.0


def test_deadline_remaining_reflects_configured_timeout(monkeypatch):
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT", "10")
    GracefulShutdownManager.start_shutdown()
    remaining = GracefulShutdownManager.deadline_remaining()
    assert 9.5 < remaining <= 10.0


def test_deadline_is_frozen_on_first_call_not_reset_by_second(monkeypatch):
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT", "10")
    GracefulShutdownManager.start_shutdown()
    first_remaining = GracefulShutdownManager.deadline_remaining()
    time.sleep(0.05)
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT", "999")
    GracefulShutdownManager.start_shutdown()  # second call, idempotent
    second_remaining = GracefulShutdownManager.deadline_remaining()
    assert second_remaining < first_remaining  # clock kept running, env change ignored


def test_deadline_remaining_reaches_zero_after_timeout_elapses(monkeypatch):
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT", "0.05")
    GracefulShutdownManager.start_shutdown()
    time.sleep(0.1)
    assert GracefulShutdownManager.deadline_remaining() == 0.0


def test_is_force_exit_false_by_default():
    assert GracefulShutdownManager.is_force_exit() is False


def test_request_force_exit_sets_flag_idempotently():
    GracefulShutdownManager.request_force_exit()
    GracefulShutdownManager.request_force_exit()
    assert GracefulShutdownManager.is_force_exit() is True


def test_force_exit_makes_deadline_remaining_zero_even_with_time_left(monkeypatch):
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT", "30")
    GracefulShutdownManager.start_shutdown()
    assert GracefulShutdownManager.deadline_remaining() > 0.0
    GracefulShutdownManager.request_force_exit()
    assert GracefulShutdownManager.deadline_remaining() == 0.0


def test_reset_clears_deadline_and_force_exit(monkeypatch):
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT", "10")
    GracefulShutdownManager.start_shutdown()
    GracefulShutdownManager.request_force_exit()
    GracefulShutdownManager.reset()
    assert GracefulShutdownManager.deadline_remaining() == 0.0
    assert GracefulShutdownManager.is_force_exit() is False


@pytest.mark.asyncio
async def test_wait_for_drain_no_arg_consumes_remaining_frozen_deadline(monkeypatch):
    """After start_shutdown freezes the deadline (and uvicorn's connection drain
    has already spent part of it), a no-arg wait_for_drain must wait only the
    REMAINING deadline, not open a fresh full timeout window — otherwise the
    total shutdown window is uvicorn-drain + full-timeout, breaking the single
    frozen-deadline bound."""
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT", "0.3")
    GracefulShutdownManager.start_shutdown()
    time.sleep(0.2)  # simulate the deadline partly consumed by uvicorn's drain

    start = time.monotonic()
    # counter never drains -> it waits out the REMAINING (~0.1s), not a fresh 0.3s
    await GracefulShutdownManager.wait_for_drain(count_fn=lambda: 5)
    elapsed = time.monotonic() - start
    assert elapsed < 0.25  # remaining ~0.1s; a regression to get_timeout() would wait ~0.3s


@pytest.mark.asyncio
async def test_wait_for_drain_no_arg_returns_immediately_on_force_exit(monkeypatch):
    """A second SIGINT (force exit) collapses the remaining deadline to 0, so a
    no-arg wait_for_drain must not block at all."""
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT", "30")
    GracefulShutdownManager.start_shutdown()
    GracefulShutdownManager.request_force_exit()

    start = time.monotonic()
    await GracefulShutdownManager.wait_for_drain(count_fn=lambda: 5)
    assert time.monotonic() - start < 0.2
