"""
Tests for the LoggingWorker class to ensure graceful shutdown handling.
"""

import asyncio
import contextvars
from dataclasses import dataclass
import io
import logging
from unittest.mock import AsyncMock, patch

import pytest

from litellm.constants import LOGGING_WORKER_AGGRESSIVE_CLEAR_COOLDOWN_SECONDS
from litellm.litellm_core_utils.logging_worker import (
    LoggingDeadlineExceeded,
    LoggingDrained,
    LoggingForcedExit,
    LoggingTask,
    LoggingWorker,
)


@dataclass
class RecordingCompletionToken:
    settlements: int = 0

    def settle(self) -> None:
        self.settlements += 1


class RaisingCompletionToken:
    def settle(self) -> None:
        raise RuntimeError("settlement failed")


@dataclass
class RaisingRecordingCompletionToken:
    settlements: int = 0

    def settle(self) -> None:
        self.settlements += 1
        raise RuntimeError("settlement failed")


class TestLoggingWorker:
    """Test cases for LoggingWorker functionality."""

    @pytest.fixture
    def logging_worker(self):
        """Create a LoggingWorker instance for testing."""
        return LoggingWorker(timeout=1.0, max_queue_size=10)

    @pytest.mark.asyncio
    async def test_graceful_shutdown_with_clear_queue(self, logging_worker):
        """Test that cancellation triggers clear_queue to prevent 'never awaited' warnings."""
        # Mock the clear_queue method to verify it's called during cancellation
        with patch.object(logging_worker, "clear_queue", new_callable=AsyncMock) as mock_clear_queue:
            # Start the worker
            logging_worker.start()

            # Give it a moment to start
            await asyncio.sleep(0.1)

            # Cancel the worker task to simulate shutdown
            if logging_worker._worker_task:
                logging_worker._worker_task.cancel()

                # Wait for the task to handle the cancellation
                try:
                    await logging_worker._worker_task
                except asyncio.CancelledError:
                    # Expected during cancellation
                    pass

            # Verify that clear_queue was called during cancellation
            mock_clear_queue.assert_called_once()

    @pytest.mark.asyncio
    async def test_clear_queue_processes_remaining_items(self, logging_worker):
        """Test that clear_queue processes remaining coroutines to prevent warnings."""
        # Create mock coroutines
        mock_coro1 = AsyncMock()
        mock_coro2 = AsyncMock()

        # Initialize the worker and add items to queue
        logging_worker._ensure_queue()
        logging_worker.enqueue(mock_coro1())
        logging_worker.enqueue(mock_coro2())

        # Clear the queue
        await logging_worker.clear_queue()

        # Verify the queue is empty after clearing
        assert logging_worker._queue.empty()

    def test_flush_on_exit_suppresses_closed_handler_errors(self, capsys):
        """Atexit flushing should not print logging errors after streams close."""
        worker = LoggingWorker(timeout=1.0, max_queue_size=10)
        worker._queue = asyncio.Queue(maxsize=10)

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("test_logging_worker_closed_handler")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        async def log_with_closed_handler():
            logger.debug("flush me during shutdown")

        previous_raise_exceptions = logging.raiseExceptions
        logging.raiseExceptions = True

        try:
            worker.enqueue(log_with_closed_handler())
            stream.close()

            worker._flush_on_exit()

            captured = capsys.readouterr()
            assert "I/O operation on closed file" not in captured.err
        finally:
            logging.raiseExceptions = previous_raise_exceptions
            logger.removeHandler(handler)

    def test_flush_on_exit_swallows_errors_and_drains_remaining(self):
        """A failing queued coroutine must not abort the atexit drain of later events."""
        worker = LoggingWorker(timeout=1.0, max_queue_size=10)
        worker._queue = asyncio.Queue(maxsize=10)

        processed = []

        async def raises_during_flush():
            raise RuntimeError("boom during shutdown flush")

        async def records_during_flush():
            processed.append("ran")

        worker.enqueue(raises_during_flush())
        worker.enqueue(records_during_flush())

        worker._flush_on_exit()

        assert processed == ["ran"]
        assert worker._queue.empty()

    @pytest.mark.asyncio
    async def test_worker_handles_cancellation_gracefully(self, logging_worker):
        """Test that the worker handles cancellation without throwing exceptions."""
        # Mock verbose_logger to capture debug messages
        with patch("litellm.litellm_core_utils.logging_worker.verbose_logger") as mock_logger:
            # Start the worker
            logging_worker.start()

            # Give it a moment to start
            await asyncio.sleep(0.1)

            # Cancel and wait for completion
            await logging_worker.stop()

            # Verify debug message was logged instead of exception
            debug_calls = [
                call
                for call in mock_logger.debug.call_args_list
                if "LoggingWorker cancelled during shutdown" in str(call)
            ]
            assert len(debug_calls) >= 1

    @pytest.mark.asyncio
    async def test_enqueue_and_process_single_item(self, logging_worker):
        """Test basic enqueue and process functionality."""
        # Create a mock coroutine that we can track
        mock_coro = AsyncMock()

        # Start the worker
        logging_worker.start()

        # Enqueue a coroutine
        logging_worker.enqueue(mock_coro())

        # Give the worker time to process the item
        await asyncio.sleep(0.2)

        # Stop the worker
        await logging_worker.stop()

        # The mock should have been awaited (processed)
        assert mock_coro.called

    @pytest.mark.asyncio
    async def test_token_settles_only_after_callback_finishes(self):
        worker = LoggingWorker(timeout=1.0, max_queue_size=10, concurrency=1)
        callback_started = asyncio.Event()
        allow_callback_to_finish = asyncio.Event()
        token = RecordingCompletionToken()

        async def blocked_callback() -> None:
            callback_started.set()
            await allow_callback_to_finish.wait()

        worker.start()
        worker.enqueue(blocked_callback(), token=token)
        await asyncio.wait_for(callback_started.wait(), timeout=1.0)

        assert token.settlements == 0

        allow_callback_to_finish.set()
        await asyncio.wait_for(worker.flush(), timeout=1.0)
        assert token.settlements == 1
        await worker.stop()

    def test_enqueue_without_queue_closes_coroutine_and_settles_token(self):
        worker = LoggingWorker(timeout=1.0, max_queue_size=10)
        token = RecordingCompletionToken()

        async def callback() -> None:
            pass

        coroutine = callback()
        worker.enqueue(coroutine, token=token)

        assert coroutine.cr_frame is None
        assert token.settlements == 1

    @pytest.mark.asyncio
    async def test_clear_queue_settles_token(self):
        worker = LoggingWorker(timeout=1.0, max_queue_size=10)
        token = RecordingCompletionToken()
        callback_ran = False

        async def callback() -> None:
            nonlocal callback_ran
            callback_ran = True

        worker._ensure_queue()
        worker.enqueue(callback(), token=token)
        await worker.clear_queue()

        assert callback_ran is True
        assert token.settlements == 1

    @pytest.mark.asyncio
    async def test_settlement_failure_does_not_strand_queue_join(self):
        worker = LoggingWorker(timeout=1.0, max_queue_size=10)
        worker._ensure_queue()

        async def callback() -> None:
            pass

        worker.enqueue(callback(), token=RaisingCompletionToken())
        with pytest.raises(RuntimeError, match="settlement failed"):
            await worker.clear_queue()

        assert worker._queue is not None
        await asyncio.wait_for(worker._queue.join(), timeout=1.0)

    @pytest.mark.asyncio
    async def test_retry_without_queue_closes_coroutine_and_settles_token(self):
        worker = LoggingWorker(timeout=1.0, max_queue_size=10)
        token = RecordingCompletionToken()

        async def callback() -> None:
            pass

        coroutine = callback()
        task = LoggingTask(coroutine=coroutine, context=contextvars.copy_context(), token=token)
        await worker._retry_enqueue_task(task, delay=0)

        assert coroutine.cr_frame is None
        assert token.settlements == 1

    @pytest.mark.asyncio
    async def test_retry_helper_is_tracked_and_cancellation_settles_token(self):
        worker = LoggingWorker(timeout=1.0, max_queue_size=10)
        worker._ensure_queue()
        token = RecordingCompletionToken()

        async def callback() -> None:
            pass

        coroutine = callback()
        task = LoggingTask(coroutine=coroutine, context=contextvars.copy_context(), token=token)
        with patch.object(worker, "_calculate_retry_delay", return_value=60.0):
            worker._schedule_delayed_enqueue_retry(task)

        assert len(worker._helper_tasks) == 1
        assert await worker._helper_tasks.cancel_all_and_count_failures() == 0
        assert coroutine.cr_frame is None
        assert token.settlements == 1

    def test_retry_without_running_loop_closes_coroutine_and_settles_token(self):
        worker = LoggingWorker(timeout=1.0, max_queue_size=10)
        token = RecordingCompletionToken()

        async def callback() -> None:
            pass

        coroutine = callback()
        task = LoggingTask(coroutine=coroutine, context=contextvars.copy_context(), token=token)
        with patch("litellm.litellm_core_utils.logging_worker.asyncio.get_running_loop", side_effect=RuntimeError):
            worker._schedule_delayed_enqueue_retry(task)

        assert coroutine.cr_frame is None
        assert token.settlements == 1

    @pytest.mark.asyncio
    async def test_loop_rebind_drops_old_queue_task_and_settles_token(self):
        worker = LoggingWorker(timeout=1.0, max_queue_size=10)
        token = RecordingCompletionToken()

        async def callback() -> None:
            pass

        coroutine = callback()
        worker._queue = asyncio.Queue(maxsize=10)
        worker._bound_loop = object()
        old_queue = worker._queue
        old_queue.put_nowait(LoggingTask(coroutine=coroutine, context=contextvars.copy_context(), token=token))

        worker._ensure_queue()

        assert worker._queue is not old_queue
        assert old_queue.empty()
        await asyncio.wait_for(old_queue.join(), timeout=1.0)
        assert coroutine.cr_frame is None
        assert token.settlements == 1

    @pytest.mark.asyncio
    async def test_aggressive_clear_direct_task_does_not_decrement_queue_counter(self):
        worker = LoggingWorker(timeout=1.0, max_queue_size=10)
        worker._ensure_queue()
        token = RecordingCompletionToken()
        callback_ran = False

        async def callback() -> None:
            nonlocal callback_ran
            callback_ran = True

        direct_task = LoggingTask(coroutine=callback(), context=contextvars.copy_context(), token=token)
        await worker._aggressively_clear_queue_async(direct_task)

        assert callback_ran is True
        assert token.settlements == 1
        assert worker._queue is not None
        await asyncio.wait_for(worker._queue.join(), timeout=1.0)

    @pytest.mark.asyncio
    async def test_aggressive_helper_cancelled_before_start_settles_direct_task(self):
        worker = LoggingWorker(timeout=1.0, max_queue_size=10)
        worker._ensure_queue()
        token = RecordingCompletionToken()

        async def callback() -> None:
            raise AssertionError("cancelled helper must not execute its callback")

        coroutine = callback()
        task = LoggingTask(coroutine=coroutine, context=contextvars.copy_context(), token=token)
        worker._handle_queue_full(task)

        assert len(worker._helper_tasks) == 1
        assert await worker._helper_tasks.cancel_all_and_count_failures() == 0
        assert coroutine.cr_frame is None
        assert token.settlements == 1

    def test_flush_on_exit_settles_processed_and_dropped_tokens(self):
        worker = LoggingWorker(timeout=1.0, max_queue_size=10)
        worker._queue = asyncio.Queue(maxsize=10)
        processed_token = RecordingCompletionToken()
        dropped_token = RecordingCompletionToken()
        processed = False

        async def processed_callback() -> None:
            nonlocal processed
            processed = True

        async def dropped_callback() -> None:
            raise AssertionError("time-limit remainder must not run")

        worker.enqueue(processed_callback(), token=processed_token)
        worker.enqueue(dropped_callback(), token=dropped_token)

        with patch(
            "litellm.litellm_core_utils.logging_worker.MAX_ITERATIONS_TO_CLEAR_QUEUE",
            1,
        ):
            worker._flush_on_exit()

        assert processed is True
        assert processed_token.settlements == 1
        assert dropped_token.settlements == 1
        assert worker._queue.empty()

    @pytest.mark.asyncio
    async def test_quiesce_waits_for_worker_and_downstream_fixed_point(self):
        worker = LoggingWorker(timeout=1.0, max_queue_size=10, concurrency=1)
        callback_started = asyncio.Event()
        allow_callback_to_finish = asyncio.Event()
        downstream_quiescent = asyncio.Event()
        token = RecordingCompletionToken()

        async def callback() -> None:
            callback_started.set()
            await allow_callback_to_finish.wait()

        worker.start()
        worker.enqueue(callback(), token=token)
        quiesce_task = asyncio.create_task(
            worker.quiesce(
                deadline_remaining=lambda: 10.0,
                is_force_exit=lambda: False,
                admission_policy=lambda _task: True,
                downstream_is_quiescent=downstream_quiescent.is_set,
            )
        )

        await asyncio.wait_for(callback_started.wait(), timeout=1.0)
        assert quiesce_task.done() is False
        allow_callback_to_finish.set()
        await asyncio.wait_for(worker.flush(), timeout=1.0)
        await asyncio.sleep(0)
        assert token.settlements == 1
        assert quiesce_task.done() is False

        downstream_quiescent.set()
        outcome = await asyncio.wait_for(quiesce_task, timeout=1.0)
        assert isinstance(outcome, LoggingDrained)
        await worker.stop_after_quiesce()

    @pytest.mark.asyncio
    async def test_quiesce_deadline_drops_queued_callback_without_executing_it(self):
        worker = LoggingWorker(timeout=1.0, max_queue_size=10, concurrency=1)
        worker.start()
        assert worker._sem is not None
        await worker._sem.acquire()
        token = RecordingCompletionToken()
        callback_ran = False

        async def callback() -> None:
            nonlocal callback_ran
            callback_ran = True

        worker.enqueue(callback(), token=token)
        outcome = await worker.quiesce(
            deadline_remaining=lambda: 0.0,
            is_force_exit=lambda: False,
            admission_policy=lambda _task: True,
            downstream_is_quiescent=lambda: False,
        )

        assert isinstance(outcome, LoggingDeadlineExceeded)
        assert outcome.cancelled == 1
        assert callback_ran is False
        assert token.settlements == 1
        assert worker._queue is not None
        await asyncio.wait_for(worker._queue.join(), timeout=1.0)
        worker._sem.release()
        await worker.stop_after_quiesce()

    @pytest.mark.asyncio
    async def test_quiesce_drop_continues_after_one_token_settlement_fails(self):
        worker = LoggingWorker(timeout=1.0, max_queue_size=10, concurrency=1)
        worker.start()
        assert worker._sem is not None
        await worker._sem.acquire()
        raising_token = RaisingRecordingCompletionToken()
        recording_token = RecordingCompletionToken()

        async def callback() -> None:
            raise AssertionError("deadline-dropped callback must not execute")

        first_coroutine = callback()
        second_coroutine = callback()
        worker.enqueue(first_coroutine, token=raising_token)
        worker.enqueue(second_coroutine, token=recording_token)

        outcome = await worker.quiesce(
            deadline_remaining=lambda: 0.0,
            is_force_exit=lambda: False,
            admission_policy=lambda _task: True,
            downstream_is_quiescent=lambda: False,
        )

        assert isinstance(outcome, LoggingDeadlineExceeded)
        assert outcome.cancellation_failed == 1
        assert first_coroutine.cr_frame is None
        assert second_coroutine.cr_frame is None
        assert raising_token.settlements == 1
        assert recording_token.settlements == 1
        assert worker._queue is not None
        await asyncio.wait_for(worker._queue.join(), timeout=1.0)
        worker._sem.release()
        await worker.stop_after_quiesce()

    @pytest.mark.asyncio
    async def test_quiesce_stops_worker_before_snapshotting_processing_tasks(self):
        worker = LoggingWorker(timeout=10.0, max_queue_size=10, concurrency=1)
        worker.start()
        callback_started = asyncio.Event()
        callback_cancelled = asyncio.Event()
        token = RecordingCompletionToken()

        async def callback() -> None:
            callback_started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                callback_cancelled.set()
                raise

        worker.enqueue(callback(), token=token)
        await asyncio.wait_for(callback_started.wait(), timeout=1.0)

        outcome = await worker.quiesce(
            deadline_remaining=lambda: 0.0,
            is_force_exit=lambda: False,
            admission_policy=lambda _task: True,
            downstream_is_quiescent=lambda: False,
        )

        assert isinstance(outcome, LoggingDeadlineExceeded)
        assert callback_cancelled.is_set()
        assert token.settlements == 1
        assert not worker._running_tasks
        await worker.stop_after_quiesce()

    @pytest.mark.asyncio
    async def test_quiesce_deadline_cancels_running_callback_and_settles_token(self):
        worker = LoggingWorker(timeout=10.0, max_queue_size=10, concurrency=1)
        callback_started = asyncio.Event()
        callback_cancelled = asyncio.Event()
        token = RecordingCompletionToken()

        async def callback() -> None:
            callback_started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                callback_cancelled.set()
                raise

        worker.start()
        worker.enqueue(callback(), token=token)
        await asyncio.wait_for(callback_started.wait(), timeout=1.0)
        outcome = await worker.quiesce(
            deadline_remaining=lambda: 0.0,
            is_force_exit=lambda: False,
            admission_policy=lambda _task: True,
            downstream_is_quiescent=lambda: False,
        )

        assert isinstance(outcome, LoggingDeadlineExceeded)
        assert callback_cancelled.is_set()
        assert token.settlements == 1
        await worker.stop_after_quiesce()

    @pytest.mark.asyncio
    async def test_quiesce_deadline_cancels_running_aggressive_helper(self):
        worker = LoggingWorker(timeout=10.0, max_queue_size=10)
        worker._ensure_queue()
        callback_started = asyncio.Event()
        callback_cancelled = asyncio.Event()
        token = RecordingCompletionToken()

        async def callback() -> None:
            callback_started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                callback_cancelled.set()
                raise

        worker._handle_queue_full(
            LoggingTask(
                coroutine=callback(),
                context=contextvars.copy_context(),
                token=token,
            )
        )
        await asyncio.wait_for(callback_started.wait(), timeout=1.0)

        outcome = await worker.quiesce(
            deadline_remaining=lambda: 0.0,
            is_force_exit=lambda: False,
            admission_policy=lambda _task: True,
            downstream_is_quiescent=lambda: False,
        )

        assert isinstance(outcome, LoggingDeadlineExceeded)
        assert callback_cancelled.is_set()
        assert token.settlements == 1
        assert worker._helper_tasks.is_empty()
        await worker.stop_after_quiesce()

    @pytest.mark.asyncio
    async def test_quiesce_force_exit_returns_forced_outcome(self):
        worker = LoggingWorker(timeout=1.0, max_queue_size=10)
        worker._ensure_queue()

        outcome = await worker.quiesce(
            deadline_remaining=lambda: 10.0,
            is_force_exit=lambda: True,
            admission_policy=lambda _task: True,
            downstream_is_quiescent=lambda: False,
        )

        assert isinstance(outcome, LoggingForcedExit)
        await worker.stop_after_quiesce()

    @pytest.mark.asyncio
    async def test_quiesce_admission_policy_rejects_enqueue_and_retry(self):
        worker = LoggingWorker(timeout=1.0, max_queue_size=10)
        worker.start()
        downstream_quiescent = asyncio.Event()
        quiesce_task = asyncio.create_task(
            worker.quiesce(
                deadline_remaining=lambda: 10.0,
                is_force_exit=lambda: False,
                admission_policy=lambda _task: False,
                downstream_is_quiescent=downstream_quiescent.is_set,
            )
        )
        await asyncio.sleep(0)

        enqueue_token = RecordingCompletionToken()
        retry_token = RecordingCompletionToken()

        async def callback() -> None:
            raise AssertionError("rejected callback must not execute")

        enqueue_coroutine = callback()
        retry_coroutine = callback()
        worker.enqueue(enqueue_coroutine, token=enqueue_token)
        await worker._retry_enqueue_task(
            LoggingTask(
                coroutine=retry_coroutine,
                context=contextvars.copy_context(),
                token=retry_token,
            ),
            delay=0,
        )

        assert enqueue_coroutine.cr_frame is None
        assert retry_coroutine.cr_frame is None
        assert enqueue_token.settlements == 1
        assert retry_token.settlements == 1

        downstream_quiescent.set()
        assert isinstance(await asyncio.wait_for(quiesce_task, timeout=1.0), LoggingDrained)
        await worker.stop_after_quiesce()

    @pytest.mark.asyncio
    async def test_stop_after_quiesce_restores_pure_sdk_restartability(self):
        worker = LoggingWorker(timeout=1.0, max_queue_size=10, concurrency=1)
        worker.start()
        await asyncio.sleep(0)
        outcome = await worker.quiesce(
            deadline_remaining=lambda: 10.0,
            is_force_exit=lambda: False,
            admission_policy=lambda _task: True,
            downstream_is_quiescent=lambda: True,
        )
        assert isinstance(outcome, LoggingDrained)
        await worker.stop_after_quiesce()

        callback_ran = asyncio.Event()

        async def callback() -> None:
            callback_ran.set()

        worker.ensure_initialized_and_enqueue(callback())
        await asyncio.wait_for(worker.flush(), timeout=1.0)
        assert callback_ran.is_set()
        await worker.stop()

    @pytest.mark.asyncio
    async def test_stop_after_deadline_quiesce_restores_pure_sdk_restartability(self):
        worker = LoggingWorker(timeout=1.0, max_queue_size=10, concurrency=1)
        worker.start()
        outcome = await worker.quiesce(
            deadline_remaining=lambda: 0.0,
            is_force_exit=lambda: False,
            admission_policy=lambda _task: True,
            downstream_is_quiescent=lambda: False,
        )
        assert isinstance(outcome, LoggingDeadlineExceeded)
        await worker.stop_after_quiesce()

        callback_ran = asyncio.Event()

        async def callback() -> None:
            callback_ran.set()

        worker.ensure_initialized_and_enqueue(callback())
        await asyncio.wait_for(worker.flush(), timeout=1.0)
        assert callback_ran.is_set()
        await worker.stop()

    @pytest.mark.asyncio
    async def test_clear_queue_with_time_limit(self, logging_worker):
        """Test that clear_queue respects the time limit."""

        async def slow_coro() -> None:
            await asyncio.sleep(0.1)

        # Initialize the worker and add items
        logging_worker._ensure_queue()
        for _ in range(5):
            logging_worker.enqueue(slow_coro())

        with patch(
            "litellm.litellm_core_utils.logging_worker.MAX_TIME_TO_CLEAR_QUEUE",
            0.05,
        ):
            start_time = asyncio.get_event_loop().time()
            await logging_worker.clear_queue()
            elapsed_time = asyncio.get_event_loop().time() - start_time

        assert elapsed_time < 0.5
        assert logging_worker._queue is not None
        assert logging_worker._queue.qsize() == 4
        logging_worker._drop_queued_tasks(logging_worker._queue)

    @pytest.mark.asyncio
    async def test_queue_full_handling(self, logging_worker):
        """Test that queue full condition is handled gracefully."""
        # Create a worker with very small queue size
        small_worker = LoggingWorker(timeout=1.0, max_queue_size=2)
        small_worker._ensure_queue()

        # Mock verbose_logger to capture exception messages
        with patch("litellm.litellm_core_utils.logging_worker.verbose_logger") as mock_logger:
            # Fill the queue beyond capacity
            mock_coro = AsyncMock()
            for _ in range(5):  # More than max_queue_size of 2
                small_worker.enqueue(mock_coro())

            # Should have logged queue full exceptions
            exception_calls = [call for call in mock_logger.exception.call_args_list if "queue is full" in str(call)]
            assert len(exception_calls) > 0

        await small_worker.clear_queue()

    @pytest.mark.asyncio
    async def test_context_propagation(self, logging_worker):
        """Test that enqueued tasks execute in their original context."""
        # Create a context variable for testing
        test_context_var: contextvars.ContextVar[str] = contextvars.ContextVar("test_context_var")

        # Track results from multiple tasks using asyncio.Event for synchronization
        task_results = []
        completion_events = {}

        async def test_task(task_id: str):
            """A test coroutine that checks if it can access the context variable."""
            try:
                # Try to get the context variable value
                value = test_context_var.get()
                task_results.append(
                    {
                        "task_id": task_id,
                        "context_value": value,
                        "context_accessible": True,
                    }
                )
            except LookupError:
                # Context variable not found
                task_results.append(
                    {
                        "task_id": task_id,
                        "context_accessible": False,
                        "context_value": None,
                    }
                )
            finally:
                # Signal that this task is complete
                completion_events[task_id].set()

        # Create completion events for each task
        completion_events["task_1"] = asyncio.Event()
        completion_events["task_2"] = asyncio.Event()
        completion_events["task_3"] = asyncio.Event()

        # Start the logging worker
        logging_worker.start()

        # Give the worker a moment to start
        await asyncio.sleep(0.1)

        # Create two separate contexts and enqueue tasks from each

        # Context 1: Set context var to "context_1"
        ctx1 = contextvars.copy_context()
        ctx1.run(test_context_var.set, "context_1")
        ctx1.run(logging_worker.enqueue, test_task("task_1"))

        # Context 2: Set context var to "context_2"
        ctx2 = contextvars.copy_context()
        ctx2.run(test_context_var.set, "context_2")
        ctx2.run(logging_worker.enqueue, test_task("task_2"))

        # Context 3: No context variable set (should get LookupError)
        ctx3 = contextvars.copy_context()
        ctx3.run(logging_worker.enqueue, test_task("task_3"))

        # Wait for all tasks to complete with a reasonable timeout
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    completion_events["task_1"].wait(),
                    completion_events["task_2"].wait(),
                    completion_events["task_3"].wait(),
                ),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            pytest.fail("Tasks did not complete within timeout")

        # Stop the worker
        await logging_worker.stop()

        # Sort results by task_id for consistent testing
        task_results.sort(key=lambda x: x["task_id"])

        # Verify that each task saw its own context
        assert len(task_results) == 3, f"Expected 3 results, got {len(task_results)}: {task_results}"

        # Task 1 should see "context_1"
        task1_result = next((r for r in task_results if r["task_id"] == "task_1"), None)
        assert task1_result is not None, "Task 1 result not found"
        assert task1_result["context_accessible"] is True, "Task 1 should have access to context variable"
        assert task1_result["context_value"] == "context_1", (
            f"Task 1 should see 'context_1', got: {task1_result['context_value']}"
        )

        # Task 2 should see "context_2"
        task2_result = next((r for r in task_results if r["task_id"] == "task_2"), None)
        assert task2_result is not None, "Task 2 result not found"
        assert task2_result["context_accessible"] is True, "Task 2 should have access to context variable"
        assert task2_result["context_value"] == "context_2", (
            f"Task 2 should see 'context_2', got: {task2_result['context_value']}"
        )

        # Task 3 should not have access to the context variable
        task3_result = next((r for r in task_results if r["task_id"] == "task_3"), None)
        assert task3_result is not None, "Task 3 result not found"
        assert task3_result["context_accessible"] is False, "Task 3 should not have access to context variable"

    @pytest.mark.asyncio
    async def test_semaphore_concurrency_limit(self):
        """Test that the worker respects the semaphore concurrency limit."""
        worker = LoggingWorker(timeout=5.0, max_queue_size=20, concurrency=2)
        worker.start()

        running_tasks, max_concurrent, lock = set(), 0, asyncio.Lock()
        completed = asyncio.Event()

        async def tracked_task(task_id: int):
            async with lock:
                running_tasks.add(task_id)
                nonlocal max_concurrent
                max_concurrent = max(max_concurrent, len(running_tasks))
            await asyncio.sleep(0.2)
            async with lock:
                running_tasks.remove(task_id)
                if not running_tasks:
                    completed.set()

        for i in range(5):
            worker.enqueue(tracked_task(i))

        await asyncio.wait_for(completed.wait(), timeout=5.0)
        await worker.stop()

        assert max_concurrent <= 2, f"Max {max_concurrent} exceeded limit 2"
        assert max_concurrent >= 2, f"Expected 2+ concurrent, got {max_concurrent}"

    @pytest.mark.asyncio
    async def test_aggressive_queue_clearing(self):
        """Test that aggressive queue clearing processes tasks when queue is full."""
        worker = LoggingWorker(timeout=2.0, max_queue_size=4, concurrency=1)
        worker.start()

        processed, lock = [], asyncio.Lock()

        async def tracked_task(task_id: int):
            async with lock:
                processed.append(task_id)
            await asyncio.sleep(0.01)

        for i in range(4):
            worker.enqueue(tracked_task(i))
        await asyncio.sleep(0.1)

        for i in range(4, 8):
            worker.enqueue(tracked_task(i))

        await asyncio.sleep(LOGGING_WORKER_AGGRESSIVE_CLEAR_COOLDOWN_SECONDS + 0.3)
        await worker.stop()
        await worker.clear_queue()

        assert len(processed) >= 4, f"Expected 4+ tasks processed, got {len(processed)}"

    @pytest.mark.asyncio
    async def test_event_loop_change_handling(self):
        """Test that LoggingWorker handles event loop changes correctly.

        This tests the fix for GitHub issue #17813 where asyncio.Queue
        was bound to a different event loop when using multiprocessing.
        """
        worker = LoggingWorker(timeout=1.0, max_queue_size=10)

        # Start the worker in the current event loop
        worker.start()

        # Verify queue was created and bound to current loop
        assert worker._queue is not None
        assert worker._bound_loop is not None
        original_queue = worker._queue

        await worker.stop()

        # Simulate a new event loop by creating a mock scenario
        # In a real multiprocessing case, asyncio.run() creates a new loop
        # We test the internal state detection

        # Create a new worker to test the _ensure_queue logic
        worker2 = LoggingWorker(timeout=1.0, max_queue_size=10)
        worker2._queue = original_queue  # Pretend we have an old queue
        worker2._bound_loop = None  # No bound loop (simulates first call)

        # Calling start should create a new queue since _bound_loop != current
        worker2.start()

        # The queue should be reinitialized since bound_loop was None
        assert worker2._queue is not None
        assert worker2._bound_loop is not None

        await worker2.stop()
