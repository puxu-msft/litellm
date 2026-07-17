"""
Replaces the bare ``uvicorn.run(**uvicorn_args, workers=num_workers)`` call in
proxy_cli.py with the same three-branch dispatch uvicorn.main.run() itself uses
(should_reload / workers>1 / direct), swapping in DrainingServer so graceful
shutdown is wired regardless of which branch runs. Kept in lockstep with
uvicorn's own dispatch order deliberately, not reimplemented from memory.
"""

from __future__ import annotations

import sys
from typing import Any, Dict

import uvicorn
from uvicorn.main import STARTUP_FAILURE
from uvicorn.supervisors import ChangeReload, Multiprocess

from litellm.proxy.shutdown.draining_server import DrainingServer


def run_uvicorn_with_draining_server(uvicorn_args: Dict[str, Any], *, workers: int) -> None:
    config = uvicorn.Config(**uvicorn_args, workers=workers)
    server = DrainingServer(config=config)
    try:
        if server.config.should_reload:
            sock = server.config.bind_socket()
            ChangeReload(server.config, target=server.run, sockets=[sock]).run()
        elif server.config.workers > 1:
            sock = server.config.bind_socket()
            Multiprocess(server.config, target=server.run, sockets=[sock]).run()
        else:
            server.run()
    except KeyboardInterrupt:
        # Mirrors uvicorn.main.run()'s own ``try/except KeyboardInterrupt: pass``:
        # a foreground Ctrl+C surfaces here as a real KeyboardInterrupt once
        # Server.capture_signals restores the default handler and re-raises.
        # Swallow it so this function returns normally, matching uvicorn's own
        # contract instead of letting the exception change the process's exit
        # behavior out from under the CLI.
        pass

    # Match uvicorn.main.run(): a direct (non-reload, single-worker) server that
    # never reached "started" is a startup failure and must exit non-zero (3),
    # so the CLI/orchestrator doesn't misread a failed boot as a clean exit.
    if not server.started and not server.config.should_reload and server.config.workers == 1:
        sys.exit(STARTUP_FAILURE)
