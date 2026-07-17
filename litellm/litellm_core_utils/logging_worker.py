# This file may be a good candidate to be the first one to be refactored into a separate process,
# for the sake of performance and scalability.

import asyncio
import contextvars
from dataclasses import dataclass
import logging
from typing import Callable, Coroutine, Optional, Protocol, runtime_checkable
import atexit

from litellm._logging import verbose_logger
from litellm.constants import (
    LOGGING_WORKER_CONCURRENCY,
    LOGGING_WORKER_MAX_QUEUE_SIZE,
    LOGGING_WORKER_MAX_TIME_PER_COROUTINE,
    LOGGING_WORKER_CLEAR_PERCENTAGE,
    LOGGING_WORKER_AGGRESSIVE_CLEAR_COOLDOWN_SECONDS,
    MAX_ITERATIONS_TO_CLEAR_QUEUE,
    MAX_TIME_TO_CLEAR_QUEUE,
)
from litellm.litellm_core_utils.managed_task_set import ManagedTaskSet

_QUIESCE_POLL_INTERVAL_SECONDS = 0.02


@dataclass(frozen=True, slots=True)
class LoggingDrained:
    """All admitted logging and downstream work finished before the deadline."""


@dataclass(frozen=True, slots=True)
class LoggingDeadlineExceeded:
    """The shutdown deadline elapsed; remaining logging work was cancelled or dropped."""

    cancelled: int
    cancellation_failed: int


@dataclass(frozen=True, slots=True)
class LoggingForcedExit:
    """A forced exit cancelled or dropped all remaining logging work."""

    cancelled: int
    cancellation_failed: int


LoggingDrainOutcome = LoggingDrained | LoggingDeadlineExceeded | LoggingForcedExit


class CompletionToken(Protocol):
    def settle(self) -> None: ...


@runtime_checkable
class ClosableStream(Protocol):
    @property
    def closed(self) -> bool: ...


@runtime_checkable
class HandlerWithStream(Protocol):
    stream: Optional[ClosableStream]


@dataclass(frozen=True, slots=True)
class LoggingTask:
    """
    A logging task with its associated context to ensure logging is executed in
    the original task's context.
    """

    coroutine: Coroutine[object, object, object]
    context: contextvars.Context
    token: Optional[CompletionToken] = None


LoggingAdmissionPolicy = Callable[[LoggingTask], bool]


class LoggingWorker:
    """
    A simple, async logging worker that processes log coroutines in the background.
    Designed to be best-effort with bounded queues to prevent backpressure.

    This leads to a +200 RPS performance improvement when using LiteLLM Python SDK or Proxy Server.
    - Use this to queue coroutine tasks that are not critical to the main flow of the application. e.g Success/Error callbacks, logging, etc.
    """

    def __init__(
        self,
        timeout: float = LOGGING_WORKER_MAX_TIME_PER_COROUTINE,
        max_queue_size: int = LOGGING_WORKER_MAX_QUEUE_SIZE,
        concurrency: int = LOGGING_WORKER_CONCURRENCY,
    ):
        self.timeout = timeout
        self.max_queue_size = max_queue_size
        self.concurrency = concurrency
        self._queue: Optional[asyncio.Queue[LoggingTask]] = None
        self._worker_task: Optional[asyncio.Task[None]] = None
        self._running_tasks: set[asyncio.Task[None]] = set()
        self._helper_tasks = ManagedTaskSet()
        self._sem: Optional[asyncio.Semaphore] = None
        self._bound_loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_aggressive_clear_time: float = 0.0
        self._aggressive_clear_in_progress: bool = False
        self._quiescing = False
        self._quiesced = False
        self._admission_policy: Optional[LoggingAdmissionPolicy] = None
        self._quiesce_outcome: Optional[LoggingDrainOutcome] = None

        # Register cleanup handler to flush remaining events on exit
        atexit.register(self._flush_on_exit)

    def _ensure_queue(self) -> None:
        """Initialize the queue if it doesn't exist or if event loop has changed."""
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop, can't initialize
            return

        # Check if we need to reinitialize due to event loop change
        if self._queue is not None and self._bound_loop is not current_loop:
            verbose_logger.debug("LoggingWorker: Event loop changed, reinitializing queue and worker")
            old_queue = self._queue
            self._drop_queued_tasks(old_queue)
            # Clear old state - these are bound to the old loop
            self._queue = None
            self._sem = None
            self._worker_task = None
            self._running_tasks.clear()

        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self.max_queue_size)
            self._bound_loop = current_loop

    def start(self) -> None:
        """Start the logging worker. Idempotent - safe to call multiple times."""
        if self._quiesced:
            return
        self._ensure_queue()
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.concurrency)
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker_loop())

    @staticmethod
    def _settle_task(task: LoggingTask) -> None:
        if task.token is not None:
            task.token.settle()

    @classmethod
    def _finish_task(cls, task: LoggingTask, source_queue: Optional[asyncio.Queue[LoggingTask]]) -> None:
        try:
            cls._settle_task(task)
        finally:
            if source_queue is not None:
                source_queue.task_done()

    @classmethod
    def _drop_task(cls, task: LoggingTask, source_queue: Optional[asyncio.Queue[LoggingTask]] = None) -> None:
        try:
            task.coroutine.close()
        finally:
            cls._finish_task(task, source_queue)

    @classmethod
    def _drop_queued_tasks(cls, queue: asyncio.Queue[LoggingTask]) -> int:
        failures = 0
        while True:
            try:
                task = queue.get_nowait()
            except asyncio.QueueEmpty:
                return failures
            try:
                cls._drop_task(task, queue)
            except Exception as error:
                failures += 1
                verbose_logger.exception(f"LoggingWorker failed to settle dropped task: {error}")

    async def _process_log_task(
        self,
        task: LoggingTask,
        sem: asyncio.Semaphore,
        source_queue: asyncio.Queue[LoggingTask],
    ):
        """Runs the logging task and handles cleanup. Releases semaphore when done."""
        try:
            try:
                # Run the coroutine in its original context
                await asyncio.wait_for(
                    task.context.run(asyncio.create_task, task.coroutine),
                    timeout=self.timeout,
                )
            except Exception as e:
                verbose_logger.exception(f"LoggingWorker error: {e}")
            finally:
                self._finish_task(task, source_queue)
        finally:
            # Always release semaphore, even if queue is None
            sem.release()

    async def _worker_loop(self) -> None:
        """Main worker loop that gets tasks and schedules them to run concurrently."""
        try:
            if self._queue is None or self._sem is None:
                return

            while True:
                # Acquire semaphore before removing task from queue to prevent
                # unbounded growth of waiting tasks
                await self._sem.acquire()
                try:
                    source_queue = self._queue
                    task = await source_queue.get()
                    # Track each spawned coroutine so we can cancel on shutdown.
                    processing_task = asyncio.create_task(self._process_log_task(task, self._sem, source_queue))
                    self._running_tasks.add(processing_task)
                    processing_task.add_done_callback(self._running_tasks.discard)
                except BaseException:
                    # If task creation fails, release semaphore to prevent deadlock
                    self._sem.release()
                    raise

        except asyncio.CancelledError:
            verbose_logger.debug("LoggingWorker cancelled during shutdown")
            if not self._quiescing and not self._quiesced:
                # Regular SDK stop preserves the existing best-effort behavior.
                await self.clear_queue()

    def _is_admitted(self, task: LoggingTask) -> bool:
        if self._quiesced:
            return False
        if not self._quiescing or self._admission_policy is None:
            return True
        return self._admission_policy(task)

    def enqueue(
        self,
        coroutine: Coroutine[object, object, object],
        *,
        token: Optional[CompletionToken] = None,
    ) -> None:
        """
        Add a coroutine to the logging queue.
        Hot path: never blocks, aggressively clears queue if full.
        """
        # Capture the current context when enqueueing
        task = LoggingTask(coroutine=coroutine, context=contextvars.copy_context(), token=token)
        if self._queue is None:
            self._drop_task(task)
            return

        try:
            admitted = self._is_admitted(task)
        except BaseException:
            self._drop_task(task)
            raise
        if not admitted:
            self._drop_task(task)
            return

        try:
            self._queue.put_nowait(task)
        except asyncio.QueueFull:
            # Queue is full - handle it appropriately
            verbose_logger.exception("LoggingWorker queue is full")
            self._handle_queue_full(task)

    def _should_start_aggressive_clear(self) -> bool:
        """
        Check if we should start a new aggressive clear operation.
        Returns True if cooldown period has passed and no clear is in progress.
        """
        if self._aggressive_clear_in_progress:
            return False

        try:
            loop = asyncio.get_running_loop()
            current_time = loop.time()
            time_since_last_clear = current_time - self._last_aggressive_clear_time

            if time_since_last_clear < LOGGING_WORKER_AGGRESSIVE_CLEAR_COOLDOWN_SECONDS:
                return False

            return True
        except RuntimeError:
            # No event loop running, drop the task
            return False

    def _mark_aggressive_clear_started(self) -> None:
        """
        Mark that an aggressive clear operation has started.

        Note: This should only be called after _should_start_aggressive_clear()
        returns True, which guarantees an event loop exists.
        """
        loop = asyncio.get_running_loop()
        self._last_aggressive_clear_time = loop.time()
        self._aggressive_clear_in_progress = True

    def _handle_queue_full(self, task: LoggingTask) -> None:
        """
        Handle queue full condition by either starting an aggressive clear
        or scheduling a delayed retry.
        """

        if self._should_start_aggressive_clear():
            self._mark_aggressive_clear_started()
            # Schedule clearing as async task so enqueue returns immediately (non-blocking)
            helper_started = asyncio.Event()

            async def run_aggressive_clear() -> None:
                helper_started.set()
                await self._aggressively_clear_queue_async(task)

            helper_task = asyncio.create_task(run_aggressive_clear())
            self._helper_tasks.add(helper_task)

            def drop_if_cancelled_before_start(completed_task: asyncio.Task[None]) -> None:
                if completed_task.cancelled() and not helper_started.is_set():
                    self._drop_task(task)
                    self._aggressive_clear_in_progress = False

            helper_task.add_done_callback(drop_if_cancelled_before_start)
        else:
            # Cooldown active or clear in progress, schedule a delayed retry
            self._schedule_delayed_enqueue_retry(task)

    def _calculate_retry_delay(self) -> float:
        """
        Calculate the delay before retrying an enqueue operation.
        Returns the delay in seconds.
        """
        try:
            loop = asyncio.get_running_loop()
            current_time = loop.time()
            time_since_last_clear = current_time - self._last_aggressive_clear_time
            remaining_cooldown = max(
                0.0,
                LOGGING_WORKER_AGGRESSIVE_CLEAR_COOLDOWN_SECONDS - time_since_last_clear,
            )
            # Add a small buffer (10% of cooldown or 50ms, whichever is larger) to ensure
            # cooldown has expired and aggressive clear has completed
            return remaining_cooldown + max(0.05, LOGGING_WORKER_AGGRESSIVE_CLEAR_COOLDOWN_SECONDS * 0.1)
        except RuntimeError:
            # No event loop, return minimum delay
            return 0.1

    def _schedule_delayed_enqueue_retry(self, task: LoggingTask) -> None:
        """
        Schedule a delayed retry to enqueue the task after cooldown expires.
        This prevents dropping tasks when the queue is full during cooldown.
        Preserves the original task context.
        """
        try:
            # Check that we have a running event loop (will raise RuntimeError if not)
            asyncio.get_running_loop()
            delay = self._calculate_retry_delay()

            # Schedule the retry as a background task
            helper_task = asyncio.create_task(self._retry_enqueue_task(task, delay))
            self._helper_tasks.add(helper_task)

            def drop_if_cancelled(completed_task: asyncio.Task[None]) -> None:
                if completed_task.cancelled():
                    self._drop_task(task)

            helper_task.add_done_callback(drop_if_cancelled)
        except RuntimeError:
            # No event loop, drop the task as we can't schedule a retry
            self._drop_task(task)

    async def _retry_enqueue_task(self, task: LoggingTask, delay: float) -> None:
        """
        Retry enqueueing the task after delay, preserving original context.
        This is called as a background task from _schedule_delayed_enqueue_retry.
        """
        await asyncio.sleep(delay)

        # Try to enqueue the task directly, preserving its original context
        if self._queue is None:
            self._drop_task(task)
            return

        try:
            admitted = self._is_admitted(task)
        except BaseException:
            self._drop_task(task)
            raise
        if not admitted:
            self._drop_task(task)
            return

        try:
            self._queue.put_nowait(task)
        except asyncio.QueueFull:
            # Still full - handle it appropriately (clear or retry again)
            self._handle_queue_full(task)

    def _extract_tasks_from_queue(self) -> list[LoggingTask]:
        """
        Extract tasks from the queue to make room.
        Returns a list of extracted tasks based on percentage of queue size.
        """
        if self._queue is None:
            return []

        # Calculate items based on percentage of queue size
        items_to_extract = (self.max_queue_size * LOGGING_WORKER_CLEAR_PERCENTAGE) // 100
        # Use actual queue size to avoid unnecessary iterations
        actual_size = self._queue.qsize()
        if actual_size == 0:
            return []
        items_to_extract = min(items_to_extract, actual_size)

        # Extract tasks from queue (using list comprehension would require wrapping in try/except)
        extracted_tasks: list[LoggingTask] = []
        for _ in range(items_to_extract):
            try:
                extracted_tasks.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        return extracted_tasks

    async def _aggressively_clear_queue_async(self, new_task: Optional[LoggingTask] = None) -> None:
        """
        Aggressively clear the queue by extracting and processing items.
        This is called when the queue is full to prevent dropping logs.
        Fully async and non-blocking - runs in background task.
        """
        try:
            if self._queue is None:
                if new_task is not None:
                    self._drop_task(new_task)
                return

            source_queue = self._queue
            extracted_tasks = self._extract_tasks_from_queue()
            if extracted_tasks or new_task is not None:
                await self._process_extracted_tasks(extracted_tasks, source_queue, new_task)
        except Exception as e:
            verbose_logger.exception(f"LoggingWorker error during aggressive clear: {e}")
        finally:
            # Always reset the flag even if an error occurs
            self._aggressive_clear_in_progress = False

    async def _process_single_task(
        self,
        task: LoggingTask,
        source_queue: Optional[asyncio.Queue[LoggingTask]],
    ) -> None:
        """Process a single task and mark it done."""
        try:
            await asyncio.wait_for(
                task.context.run(asyncio.create_task, task.coroutine),
                timeout=self.timeout,
            )
        except Exception:
            # Suppress errors during processing to ensure we keep going
            pass
        finally:
            self._finish_task(task, source_queue)

    async def _process_extracted_tasks(
        self,
        tasks: list[LoggingTask],
        source_queue: asyncio.Queue[LoggingTask],
        direct_task: Optional[LoggingTask],
    ) -> None:
        """
        Process tasks that were extracted from the queue to make room.
        Processes them concurrently without semaphore limits for maximum speed.
        """
        if not tasks and direct_task is None:
            return

        # Process all tasks concurrently for maximum speed
        queued_coroutines = tuple(self._process_single_task(task, source_queue) for task in tasks)
        direct_coroutines = () if direct_task is None else (self._process_single_task(direct_task, None),)
        await asyncio.gather(*queued_coroutines, *direct_coroutines)

    def ensure_initialized_and_enqueue(
        self,
        async_coroutine: Coroutine[object, object, object],
        *,
        token: Optional[CompletionToken] = None,
    ):
        """
        Ensure the logging worker is initialized and enqueue the coroutine.
        """
        self.start()
        self.enqueue(async_coroutine, token=token)

    async def stop(self) -> None:
        """Stop the logging worker and clean up resources."""
        if self._worker_task is None and not self._running_tasks and self._helper_tasks.is_empty():
            # No worker launched and no in-flight tasks to drain.
            return

        tasks_to_cancel: list[asyncio.Task[None]] = list(self._running_tasks)
        if self._worker_task:
            # Include the main worker loop so it stops fetching work.
            tasks_to_cancel.append(self._worker_task)

        for task in tasks_to_cancel:
            # Propagate cancellation to every pending task.
            task.cancel()

        # Wait for cancellation to settle; ignore errors raised during shutdown.
        await asyncio.gather(
            asyncio.gather(*tasks_to_cancel, return_exceptions=True),
            self._helper_tasks.cancel_all_and_count_failures(),
            return_exceptions=True,
        )

        self._worker_task = None
        # Drop references to completed tasks so we can restart cleanly.
        self._running_tasks.clear()

    async def flush(self) -> None:
        """Flush the logging queue.

        Waits until every enqueued task has completed. ``queue.join()`` blocks
        on the queue's unfinished-task counter (decremented by ``task_done()``),
        so it correctly handles items that have been dequeued but whose
        callback hasn't finished yet — ``queue.empty()`` would return True in
        that window and cause us to skip the wait.
        """
        if self._queue is None:
            return
        await self._queue.join()

    async def quiesce(
        self,
        *,
        deadline_remaining: Callable[[], float],
        is_force_exit: Callable[[], bool],
        admission_policy: LoggingAdmissionPolicy,
        downstream_is_quiescent: Callable[[], bool],
    ) -> LoggingDrainOutcome:
        """Drain logging and downstream work to a stable fixed point.

        The injected callbacks keep this shared SDK component independent from
        proxy accounting types. During quiesce, every new enqueue and retry is
        checked by ``admission_policy``. Deadline and forced-exit paths cancel
        running/helper work and drop queued callbacks without executing them.
        """
        if self._quiesce_outcome is not None:
            return self._quiesce_outcome

        self._quiescing = True
        self._admission_policy = admission_policy
        if (
            self._queue is not None
            and not self._queue.empty()
            and (self._worker_task is None or self._worker_task.done())
        ):
            self.start()

        while True:
            if is_force_exit():
                return await self._cancel_for_quiesce(forced=True)
            if deadline_remaining() <= 0:
                return await self._cancel_for_quiesce(forced=False)
            if self._is_locally_quiescent() and downstream_is_quiescent():
                await asyncio.sleep(0)
                if self._is_locally_quiescent() and downstream_is_quiescent():
                    outcome = LoggingDrained()
                    self._quiesced = True
                    self._quiesce_outcome = outcome
                    return outcome
            await asyncio.sleep(min(_QUIESCE_POLL_INTERVAL_SECONDS, max(deadline_remaining(), 0.0)))

    def _is_locally_quiescent(self) -> bool:
        return (
            (self._queue is None or self._queue.empty()) and not self._running_tasks and self._helper_tasks.is_empty()
        )

    async def _cancel_for_quiesce(self, *, forced: bool) -> LoggingDrainOutcome:
        self._quiesced = True

        # Stop every producer before snapshotting work. Otherwise the worker can
        # dequeue and register a processing task after the snapshot, escaping
        # cancellation and the strong-reference set.
        worker_tasks = () if self._worker_task is None or self._worker_task.done() else (self._worker_task,)
        for task in worker_tasks:
            task.cancel()
        await asyncio.gather(*worker_tasks, return_exceptions=True)

        helper_count = len(self._helper_tasks)
        helper_failures = await self._helper_tasks.cancel_all_and_count_failures()

        running_tasks = tuple(self._running_tasks)
        for task in running_tasks:
            task.cancel()
        running_results = await asyncio.gather(*running_tasks, return_exceptions=True)

        queued = 0 if self._queue is None else self._queue.qsize()
        drop_failures = 0
        if self._queue is not None:
            drop_failures = self._drop_queued_tasks(self._queue)

        cancellation_failed = (
            helper_failures
            + drop_failures
            + sum(
                1
                for result in running_results
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError)
            )
        )
        cancelled = queued + len(running_tasks) + helper_count
        self._worker_task = None
        self._running_tasks.clear()
        outcome: LoggingDrainOutcome
        if forced:
            outcome = LoggingForcedExit(cancelled=cancelled, cancellation_failed=cancellation_failed)
        else:
            outcome = LoggingDeadlineExceeded(cancelled=cancelled, cancellation_failed=cancellation_failed)
        self._quiesce_outcome = outcome
        return outcome

    async def stop_after_quiesce(self) -> None:
        """Stop the worker loop after quiesce, then restore pure-SDK restartability."""
        if self._quiesce_outcome is None:
            raise RuntimeError("LoggingWorker.stop_after_quiesce() requires a completed quiesce()")

        if self._worker_task is not None and not self._worker_task.done():
            self._worker_task.cancel()
            await asyncio.gather(self._worker_task, return_exceptions=True)
        await self._helper_tasks.cancel_all_and_count_failures()
        self._worker_task = None
        self._running_tasks.clear()
        self._quiescing = False
        self._quiesced = False
        self._admission_policy = None
        self._quiesce_outcome = None

    async def clear_queue(self):
        """
        Clear the queue with a maximum time limit.
        """
        if self._queue is None:
            return

        source_queue = self._queue
        start_time = asyncio.get_event_loop().time()

        for _ in range(MAX_ITERATIONS_TO_CLEAR_QUEUE):
            # Check if we've exceeded the maximum time
            if asyncio.get_event_loop().time() - start_time >= MAX_TIME_TO_CLEAR_QUEUE:
                verbose_logger.warning(f"clear_queue exceeded max_time of {MAX_TIME_TO_CLEAR_QUEUE}s, stopping early")
                break

            try:
                task = source_queue.get_nowait()
                # Await the coroutine to properly execute and avoid "never awaited" warnings
                try:
                    await asyncio.wait_for(
                        task.context.run(asyncio.create_task, task.coroutine),
                        timeout=self.timeout,
                    )
                except Exception:
                    # Suppress errors during cleanup
                    pass
                finally:
                    try:
                        self._finish_task(task, source_queue)
                    finally:
                        # Clear reference to prevent memory leaks
                        task = None
            except asyncio.QueueEmpty:
                break

    def _safe_log(self, level: str, message: str) -> None:
        """
        Safely log a message during shutdown, suppressing errors if logging is closed.
        """
        # Check if logger has valid handlers before attempting to log
        # During shutdown, handlers may be closed, causing ValueError when writing
        if not hasattr(verbose_logger, "handlers") or not verbose_logger.handlers:
            return

        # Check if any handler has a valid stream
        has_valid_handler = False
        for handler in verbose_logger.handlers:
            try:
                if isinstance(handler, HandlerWithStream) and handler.stream is not None and not handler.stream.closed:
                    has_valid_handler = True
                    break
                elif not isinstance(handler, HandlerWithStream):
                    # Non-stream handlers (like NullHandler) are always valid
                    has_valid_handler = True
                    break
            except (AttributeError, ValueError):
                continue

        if not has_valid_handler:
            return

        try:
            if level == "debug":
                verbose_logger.debug(message)
            elif level == "info":
                verbose_logger.info(message)
            elif level == "warning":
                verbose_logger.warning(message)
            elif level == "error":
                verbose_logger.error(message)
        except (ValueError, OSError, AttributeError):
            # Logging handlers may be closed during shutdown
            # Silently ignore logging errors to prevent breaking shutdown
            pass

    def _flush_on_exit(self):
        """
        Flush remaining events synchronously before process exit.
        Called automatically via atexit handler.

        This ensures callbacks queued by async completions are processed
        even when the script exits before the worker loop can handle them.

        Note: All logging in this method is wrapped to handle cases where
        logging handlers are closed during shutdown.
        """
        if self._queue is None:
            self._safe_log("debug", "[LoggingWorker] atexit: No queue initialized")
            return

        if self._queue.empty():
            self._safe_log("debug", "[LoggingWorker] atexit: Queue is empty")
            return

        source_queue = self._queue
        queue_size = source_queue.qsize()
        self._safe_log("info", f"[LoggingWorker] atexit: Flushing {queue_size} remaining events...")

        # Create a new event loop since the original is closed
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            # Process remaining queue items with time limit
            processed = 0
            start_time = loop.time()

            # logging.raiseExceptions is a process-wide global; scope the
            # suppression to just the drain loop, where shutdown callbacks may
            # log to already-closed handler streams, so other threads keep their
            # logging error reporting for as little of the window as possible.
            previous_raise_exceptions = logging.raiseExceptions
            logging.raiseExceptions = False
            try:
                while not source_queue.empty() and processed < MAX_ITERATIONS_TO_CLEAR_QUEUE:
                    if loop.time() - start_time >= MAX_TIME_TO_CLEAR_QUEUE:
                        self._safe_log(
                            "warning",
                            f"[LoggingWorker] atexit: Reached time limit ({MAX_TIME_TO_CLEAR_QUEUE}s), stopping flush",
                        )
                        break

                    try:
                        task = source_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                    # Run the coroutine synchronously in new loop
                    # Note: We run the coroutine directly, not via create_task,
                    # since we're in a new event loop context
                    try:
                        loop.run_until_complete(task.coroutine)
                        processed += 1
                    except Exception:
                        # Silent failure to not break user's program
                        pass
                    finally:
                        try:
                            self._finish_task(task, source_queue)
                        finally:
                            # Clear reference to prevent memory leaks
                            task = None
            finally:
                self._drop_queued_tasks(source_queue)
                logging.raiseExceptions = previous_raise_exceptions

            self._safe_log(
                "info",
                f"[LoggingWorker] atexit: Successfully flushed {processed} events!",
            )

        finally:
            loop.close()


# Global instance for backward compatibility
GLOBAL_LOGGING_WORKER = LoggingWorker()
