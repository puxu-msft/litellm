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

## Still open (deferred; coordinate with Phase 1a/1b)

- Health-watchdog resurrection path. `_db_health_watchdog_loop` calls `attempt_db_reconnect` on probe failure. On uvicorn this is now blocked from doing damage: `DrainingServer` sets the shutdown flag at signal time and `_attempt_reconnect_inside_lock` re-checks it after taking the lock, so a shutdown probe failure no longer recreates the engine. Still open: unify the watchdog's own lifecycle (stop it promptly on shutdown rather than after drain) and confirm the same holds for runners without an early-GSM signal.
- Reconnect task lifecycle. The engine-death reconnect is a bare `asyncio.create_task` with no stored reference (can be GC'd mid-flight) and is not cancelled by `stop_db_health_watchdog_task`. Track it and cancel on stop.
- Non-uvicorn runners. Granian (`Granian(**kwargs).serve()`) has no early-GSM wiring, so its pidfd / os.kill-poll detectors (which cannot read an exit status) still race a process-group signal there. The waitpid detector's SIGINT gate already covers the common path on any runner.
- Integration coverage at the real-uvicorn level (spawn uvicorn + child + process-group SIGINT, assert no reconnect / no `triggering reconnect` log / no new engine PID). The current regression test spawns a real child and sends SIGINT to exercise the real `os.waitpid` -> `WTERMSIG` path, but does not stand up uvicorn.

## Already landed by the shutdown fleet

- TOCTOU. `_attempt_reconnect_inside_lock` now re-checks `_is_shutting_down()` after acquiring `_db_reconnect_lock`, covering every reconnect caller (death path, health watchdog, request-driven) at the destructive point.
- Early-shutdown signal on uvicorn. `DrainingServer` overrides `handle_exit` to call `GracefulShutdownManager.start_shutdown()` synchronously at signal-delivery time; `litellm/proxy/shutdown/uvicorn_runner.py` + `proxy_cli` wire it into the serve path. So on uvicorn the shutdown flag is set before the death callback runs, making the flag-based gate effective for the pidfd/poll detectors and SIGTERM too.

## Dependency

Deferred items should build on `GracefulShutdownManager.is_shutting_down()` / the frozen deadline and, for non-uvicorn runners, an equivalent early-signal hook rather than re-implementing signal handling.

