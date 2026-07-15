# Prisma engine-death reconnect during shutdown

Tracks the bug where the Prisma query-engine watcher resurrects the engine while the proxy is shutting down, and what is fixed vs still open. Related to the Phase 1a/1b graceful-shutdown work (`GracefulShutdownManager`, `DrainingServer`).

## The bug

Under a local Ctrl+C, SIGINT reaches the whole foreground process group, so the prisma-query-engine child dies from the signal. Uvicorn runs the ASGI lifespan shutdown (where `GracefulShutdownManager.start_shutdown()` is called) only at the tail of its own teardown: `main_loop` polls `should_exit` at a 0.1s interval, then `Server.shutdown` does a fixed `await asyncio.sleep(0.1)` and waits for in-flight tasks before `lifespan.shutdown()`. Net effect: the shutdown flag lands ~200ms after the engine already died. The still-armed watcher read the death as a crash and called `attempt_db_reconnect(force=True)`, spawning a fresh engine mid-shutdown and logging a misleading `triggering reconnect` ERROR.

Independently verified against uvicorn 0.33.0 (real subprocess + process-group SIGINT): death callback and its deferred task both run at `flag=False`; `start_shutdown` lands ~180ms later.

## What is fixed (commits 898d6a6a26 + follow-up)

The waitpid detector reads the child's terminating signal off the exit status via `WIFSIGNALED`/`WTERMSIG` (a core-dumping signal sets the 0x80 bit, so the raw status is not the signal number) and forwards it to the death handler. Both waitpid entry points do this: the blocking wait thread and the watch-start `WNOHANG` probe (which reaps an already-dead child and so holds its status too).

Suppression rule in `_reconnect_after_engine_death`:

- `SIGINT` -> unconditional suppress. The terminal delivered Ctrl+C to the whole foreground process group, so this is deterministic and does not depend on the shutdown flag's timing or the server runner.
- `SIGTERM` -> suppress only when `_is_shutting_down()` is also set. In the normal shutdown path the engine is not signalled directly, so a SIGTERM reaching the engine while the flag is False is an individual kill (a real failure) that must reconnect; a process-group SIGTERM during a known shutdown is caught by the flag gate.
- Anything else (crash signal like SIGSEGV, individually-killed engine, clean exit while healthy) reconnects.

This kills the exact symptom in the reported log (the `waitpid thread` detector under Ctrl+C).

## Out of scope

- Non-uvicorn runners (out of scope for this fork). Granian (`Granian(**kwargs).serve()`) has no early-GSM wiring, so its pidfd / os.kill-poll fallback detectors (which cannot read an exit status) still race a process-group signal there. This fork runs uvicorn standalone (`litellm --config ...`), so Granian is not used; the waitpid detector's SIGINT gate already covers the common path on any runner. If Granian is ever adopted, mirror `DrainingServer`'s `handle_exit` hook for it. Left unfixed deliberately (YAGNI here).

## Already landed

By this fix:

- Reconnect task lifecycle. The engine-death reconnect is now stored on `_engine_reconnect_task` while it runs (asyncio only weakly references a bare `create_task`, so it could otherwise be GC'd mid-flight) and is cancelled/awaited by `stop_db_health_watchdog_task`.

By the shutdown fleet:

- TOCTOU. `_attempt_reconnect_inside_lock` re-checks `_is_shutting_down()` after acquiring `_db_reconnect_lock`, covering every reconnect caller (death path, health watchdog, request-driven) at the destructive point.
- Health-watchdog lifecycle. The lifespan now stops the watchdog right after drain (before tearing down shared deps), and the lock gate above blocks a shutdown probe failure from recreating the engine.
- Early-shutdown signal on uvicorn. `DrainingServer` overrides `handle_exit` to call `GracefulShutdownManager.start_shutdown()` synchronously at signal-delivery time; `litellm/proxy/shutdown/uvicorn_runner.py` + `proxy_cli` wire it into the serve path. So on uvicorn the shutdown flag is set before the death callback runs, making the flag-based gate effective for the pidfd/poll detectors and SIGTERM too.
- Real-uvicorn E2E. `tests/e2e/shutdown/test_graceful_shutdown_e2e.py` stands up the proxy, sends SIGINT/SIGTERM to a real process with an in-flight request, and asserts it quiesces and exits promptly without emitting reconnect/DB/redis-spam lines.

## Dependency

Deferred items should build on `GracefulShutdownManager.is_shutting_down()` / the frozen deadline and, for non-uvicorn runners, an equivalent early-signal hook rather than re-implementing signal handling.

