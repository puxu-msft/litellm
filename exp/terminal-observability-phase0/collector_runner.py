from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import struct
import threading
from pathlib import Path

import uvicorn
from uvicorn.supervisors import ChangeReload, Multiprocess

from litellm.proxy.shutdown.draining_server import DrainingServer


class Collector:
    def __init__(self, socket_path: Path, event_path: Path) -> None:
        self._socket_path = socket_path
        self._event_path = event_path
        self._loop = asyncio.new_event_loop()
        self._server: asyncio.AbstractServer | None = None
        self._thread = threading.Thread(target=self._run, name="phase0-collector", daemon=False)

    async def _read(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            size = struct.unpack("!I", await reader.readexactly(4))[0]
            payload = json.loads(await reader.readexactly(size))
            record = {"collector_pid": os.getpid(), **payload}
            with self._event_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        finally:
            writer.close()
            await writer.wait_closed()

    async def _serve(self) -> None:
        self._server = await asyncio.start_unix_server(self._read, path=self._socket_path)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
            self._loop.run_forever()
        finally:
            if self._server is not None:
                self._server.close()
                self._loop.run_until_complete(self._server.wait_closed())
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    def start(self) -> None:
        self._socket_path.unlink(missing_ok=True)
        self._thread.start()
        while not self._socket_path.exists():
            self._thread.join(0.01)
            if not self._thread.is_alive():
                raise RuntimeError("collector failed before creating its Unix socket")

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise TimeoutError("collector thread did not stop")
        self._socket_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--mode", choices=("direct", "multiprocess", "reload"), required=True)
    parser.add_argument("--reload-dir", type=Path)
    args = parser.parse_args()

    os.environ["TERMINAL_OBSERVABILITY_COLLECTOR_SOCKET"] = str(args.socket)
    collector = Collector(args.socket, args.events)
    collector.start()
    config_kwargs = {
        "app": "collector_app:app",
        "host": "127.0.0.1",
        "port": args.port,
        "workers": 2 if args.mode == "multiprocess" else 1,
        "reload": args.mode == "reload",
        "log_level": "warning",
    }
    if args.mode == "reload" and args.reload_dir is not None:
        config_kwargs["reload_dirs"] = [str(args.reload_dir)]
    config = uvicorn.Config(
        **config_kwargs,
    )
    server = DrainingServer(config)
    previous_sigterm = None
    try:
        if args.mode == "direct":
            previous_sigterm = signal.signal(signal.SIGTERM, lambda _sig, _frame: None)
            server.run()
        else:
            socket = config.bind_socket()
            supervisor = (
                Multiprocess(config, target=server.run, sockets=[socket])
                if args.mode == "multiprocess"
                else ChangeReload(config, target=server.run, sockets=[socket])
            )
            supervisor.run()
    finally:
        collector.close()
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    main()