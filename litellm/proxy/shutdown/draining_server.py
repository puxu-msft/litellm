"""
uvicorn.Server subclass that wires signal/shutdown lifecycle hooks into
GracefulShutdownManager, without altering uvicorn's own reload/multiprocess/
direct dispatch or its own should_exit/force-exit bookkeeping.
"""

from __future__ import annotations

import signal
import socket
from types import FrameType
from typing import List, Optional

import uvicorn

from litellm.proxy.shutdown.graceful_shutdown_manager import GracefulShutdownManager


class DrainingServer(uvicorn.Server):
    """
    Two overrides only:

    - ``handle_exit``: runs synchronously on the signal handler. Starts the
      shutdown deadline (or, on a second SIGINT while uvicorn is already
      exiting, requests an immediate force exit) before delegating to
      uvicorn's own handling.
    - ``shutdown``: uvicorn calls this once ``should_exit``/``limit_max_requests``
      trips, whether from a signal or programmatically. Idempotently starts
      shutdown (covers the ``limit_max_requests`` path, which never goes
      through ``handle_exit``) and aligns uvicorn's own graceful-shutdown
      timeout with the single frozen deadline instead of a second, disjoint
      timeout value.
    """

    def handle_exit(self, sig: int, frame: Optional[FrameType]) -> None:
        if self.should_exit and sig == signal.SIGINT:
            # uvicorn's own base handle_exit already set should_exit=True on
            # the first signal; a second SIGINT while already exiting is the
            # operator's "stop waiting, exit now" signal.
            GracefulShutdownManager.request_force_exit()
        else:
            GracefulShutdownManager.start_shutdown()
        super().handle_exit(sig, frame)

    async def shutdown(self, sockets: Optional[List[socket.socket]] = None) -> None:
        # Idempotent: covers limit_max_requests / any programmatic should_exit
        # path that never goes through handle_exit.
        GracefulShutdownManager.start_shutdown()
        self.config.timeout_graceful_shutdown = max(0.0, GracefulShutdownManager.deadline_remaining())
        await super().shutdown(sockets)
