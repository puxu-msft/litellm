# Prisma engine-death reconnect during shutdown

Tracks the bug where the Prisma query-engine watcher resurrects the engine while the proxy is shutting down, and what is fixed vs still open. Related to the Phase 1a/1b graceful-shutdown work (`GracefulShutdownManager`, `DrainingServer`).

## The bug

Under a local Ctrl+C, SIGINT reaches the whole foreground process group, so the prisma-query-engine child dies from the signal. Uvicorn runs the ASGI lifespan shutdown (where `GracefulShutdownManager.start_shutdown()` is called) only at the tail of its own teardown: `main_loop` polls `should_exit` at a 0.1s interval, then `Server.shutdown` does a fixed `await asyncio.sleep(0.1)` and waits for in-flight tasks before `lifespan.shutdown()`. Net effect: the shutdown flag lands ~200ms after the engine already died. The still-armed watcher read the death as a crash and called `attempt_db_reconnect(force=True)`, spawning a fresh engine mid-shutdown and logging a misleading `triggering reconnect` ERROR.

Independently verified against uvicorn 0.33.0 (real subprocess + process-group SIGINT): death callback and its deferred task both run at `flag=False`; `start_shutdown` lands ~180ms later.

## What is fixed (commit 898d6a6a26)

The waitpid detector now reads the child's terminating signal off the exit status (`os.WIFSIGNALED` / `os.WTERMSIG`) and forwards it to the death handler. A `SIGINT`/`SIGTERM` there means the engine was killed by the process-group shutdown signal, so the death is treated as a shutdown and the reconnect is suppressed deterministically; it does not depend on the shutdown flag being set in time or on any particular server runner (works for `uvicorn.run` and Granian alike). A crash signal (e.g. SIGSEGV) or a clean exit while the proxy is healthy still reconnects.

This directly kills the exact symptom in the reported log (the `waitpid thread` detector under Ctrl+C).

## Still open (deferred; coordinate with Phase 1a/1b)

These came out of the adversarial review and are intentionally not in the narrow fix above, because they overlap the fleet's in-flight shutdown/deadline framework and should reuse it rather than duplicate it.

- Health-watchdog resurrection path. `_db_health_watchdog_loop` calls `attempt_db_reconnect` on probe failure and is only cancelled after drain. During shutdown a failed probe can still heavy-reconnect and spawn a new engine (the death handler already set `_engine_confirmed_dead`). Gate this on shutdown.
- TOCTOU in the reconnect path. The reconnect decision checks shutdown once; a task can then wait on `_db_reconnect_lock`, acquire it after shutdown starts, and still recreate. Re-check shutdown inside the lock, right before `recreate_prisma_client`. Placing that single check at the top of `_run_reconnect_cycle` covers every caller (death path, health watchdog, request-driven) and is TOCTOU-safe.
- Reconnect task lifecycle. The engine-death reconnect is a bare `asyncio.create_task` with no stored reference (can be GC'd mid-flight) and is not cancelled by `stop_db_health_watchdog_task`. Track it and cancel on stop.
- pidfd / os.kill-poll detectors cannot read an exit status, so they still race under a pre-DrainingServer Ctrl+C. Mitigated once `DrainingServer` is wired into the serve path: its `handle_exit` calls `start_shutdown()` synchronously at signal time, so the GSM gate fires before the death callback. These are fallback detectors (used only when the waitpid thread can't be set up), so the primary path is already covered.
- Integration coverage at the real-uvicorn level (spawn uvicorn + child + process-group SIGINT, assert no reconnect / no `triggering reconnect` log / no new engine PID). The current regression test spawns a real child and sends SIGINT to exercise the real `os.waitpid` -> `WTERMSIG` path, but does not stand up uvicorn.

## Dependency

The general early-shutdown signal is `DrainingServer` (Phase 1a): it wires uvicorn's `handle_exit` into `GracefulShutdownManager.start_shutdown()` at signal-delivery time. Once it is wired into `proxy_cli`'s serve path, the GSM-based gates above become effective for the non-waitpid detectors too. The deferred items should build on `GracefulShutdownManager.is_shutting_down()` / the frozen deadline rather than re-implement signal handling.
