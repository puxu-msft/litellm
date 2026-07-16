#!/usr/bin/env python3
"""Blue-green hot reload for the litellm proxy (dev only).

Why not `proxy_cli --reload`? That uses uvicorn's StatReload, which does a
graceful *restart* of the (single) worker: in-flight requests are drained and
the process is replaced, so there is a switch-over blip and no "old code keeps
serving old requests" guarantee.

This launcher instead runs gunicorn with uvicorn workers and preload OFF, and on
a code change sends SIGHUP to the gunicorn arbiter. Per gunicorn's documented HUP
behavior, the arbiter boots fresh workers that import the *new* code and then
gracefully shuts down the old workers, which finish their in-flight requests on
the *old* code (up to --graceful-timeout) before exiting. Result: in-flight
requests complete on old code, new requests hit new code, no dropped connections.

preload must be OFF so each worker imports the app on boot (fresh code per new
worker). litellm's built-in gunicorn path hardcodes preload=True, which is why
we invoke gunicorn directly here.

Run:
    python /home/xp/.config/litellm/hot_reload.py
Then edit any file under the watched litellm/ package; it blue-green reloads.
Ctrl-C stops gunicorn.

Notes:
- cwd is the config dir so the `hooks` callback module and its hooks.config.json
  resolve, and the hookpkg SIGUSR2 reload keeps working independently.
- Redis/API env vars are inherited from your shell (config.yaml reads
  os.environ/REDIS_*), same as your normal run.
- The hookpkg has its own SIGUSR2 reload (./reload.sh); this launcher only
  blue-green reloads the litellm package code.
"""
import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_CONFIG = "/home/xp/.config/litellm/config.yaml"
DEFAULT_WATCH = "/home/xp/refs/ai-agents/litellm/litellm"
DEFAULT_WORKDIR = "/home/xp/.config/litellm"
DEFAULT_GUNICORN = "/home/xp/refs/ai-agents/litellm/.venv/bin/gunicorn"


def snapshot(root: Path) -> dict:
    out = {}
    for p in root.rglob("*.py"):
        try:
            out[str(p)] = p.stat().st_mtime
        except OSError:
            continue
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Blue-green hot reload for the litellm proxy")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--watch", default=DEFAULT_WATCH, help="dir whose *.py trigger a blue-green reload")
    ap.add_argument("--workdir", default=DEFAULT_WORKDIR, help="cwd so `hooks` + .env resolve")
    ap.add_argument("--gunicorn", default=DEFAULT_GUNICORN)
    ap.add_argument("--bind", default="0.0.0.0:4000")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=600, help="request + graceful drain timeout (LLM streams are slow)")
    ap.add_argument("--poll", type=float, default=1.0)
    ap.add_argument("--debounce", type=float, default=0.4)
    args = ap.parse_args()

    os.chdir(args.workdir)
    env = dict(os.environ)
    env["CONFIG_FILE_PATH"] = args.config

    cmd = [
        args.gunicorn,
        "litellm.proxy.proxy_server:app",
        "-k",
        "uvicorn.workers.UvicornWorker",
        "-w",
        str(args.workers),
        "--bind",
        args.bind,
        "--timeout",
        str(args.timeout),
        "--graceful-timeout",
        str(args.timeout),
        "--access-logfile",
        "-",
    ]
    print(f"[hot_reload] cwd={args.workdir}", flush=True)
    print(f"[hot_reload] CONFIG_FILE_PATH={args.config}", flush=True)
    print(f"[hot_reload] launching: {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(cmd, env=env)

    def shutdown(*_: object) -> None:
        print("\n[hot_reload] stopping gunicorn (SIGTERM)...", flush=True)
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=args.timeout)
        except (subprocess.TimeoutExpired, ProcessLookupError):
            proc.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    root = Path(args.watch)
    state = snapshot(root)
    print(
        f"[hot_reload] watching {root} ({len(state)} .py files); "
        f"edit code -> SIGHUP -> blue-green reload (arbiter pid {proc.pid})",
        flush=True,
    )
    while True:
        if proc.poll() is not None:
            print(f"[hot_reload] gunicorn exited (code {proc.returncode})", flush=True)
            sys.exit(proc.returncode or 0)
        time.sleep(args.poll)
        current = snapshot(root)
        if current == state:
            continue
        time.sleep(args.debounce)
        current = snapshot(root)
        changed = {k for k in current if current.get(k) != state.get(k)} | {k for k in state if k not in current}
        state = current
        sample = ", ".join(sorted(os.path.relpath(c, str(root)) for c in changed)[:3])
        print(f"[hot_reload] {len(changed)} file(s) changed ({sample}...) -> SIGHUP", flush=True)
        proc.send_signal(signal.SIGHUP)


if __name__ == "__main__":
    main()
