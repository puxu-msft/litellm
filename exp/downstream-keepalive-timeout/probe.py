from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

READ_TIMEOUT_SECONDS = 0.30
KEEPALIVE_DELAYS_SECONDS = (0.20, 0.20, 0.20)
SILENT_DELAY_SECONDS = 0.45


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    name: str
    outcome: str
    elapsed_seconds: float
    bytes_received: int


async def serve_scenario(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    delays: Sequence[float],
) -> None:
    try:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Cache-Control: no-cache\r\n"
            b"Connection: close\r\n\r\n"
        )
        await writer.drain()
        for delay in delays:
            await asyncio.sleep(delay)
            writer.write(b": ping\n\n")
            await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        writer.close()
        await writer.wait_closed()


async def run_scenario(name: str, delays: Sequence[float]) -> ScenarioResult:
    server = await asyncio.start_server(
        lambda reader, writer: serve_scenario(reader, writer, delays),
        host="127.0.0.1",
        port=0,
    )
    socket = server.sockets[0]
    port = int(socket.getsockname()[1])
    timeout = httpx.Timeout(
        connect=READ_TIMEOUT_SECONDS,
        read=READ_TIMEOUT_SECONDS,
        write=READ_TIMEOUT_SECONDS,
        pool=READ_TIMEOUT_SECONDS,
    )
    started = time.perf_counter()
    chunks: tuple[bytes, ...] = ()
    outcome = "completed"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("GET", f"http://127.0.0.1:{port}/events") as response:
                response.raise_for_status()
                chunks = tuple([chunk async for chunk in response.aiter_raw()])
    except httpx.ReadTimeout:
        outcome = "read-timeout"
    finally:
        elapsed = time.perf_counter() - started
        server.close()
        await server.wait_closed()

    return ScenarioResult(
        name=name,
        outcome=outcome,
        elapsed_seconds=elapsed,
        bytes_received=sum(map(len, chunks)),
    )


async def main() -> None:
    keepalive = await run_scenario("keepalive", KEEPALIVE_DELAYS_SECONDS)
    silent = await run_scenario("silent", (SILENT_DELAY_SECONDS,))

    assert keepalive.outcome == "completed", keepalive
    assert keepalive.elapsed_seconds > READ_TIMEOUT_SECONDS * 1.8, keepalive
    assert keepalive.bytes_received == len(b": ping\n\n") * len(KEEPALIVE_DELAYS_SECONDS), keepalive
    assert silent.outcome == "read-timeout", silent
    assert READ_TIMEOUT_SECONDS * 0.8 <= silent.elapsed_seconds < SILENT_DELAY_SECONDS, silent

    print(f"httpx={httpx.__version__}")
    print(f"read_timeout={READ_TIMEOUT_SECONDS:.2f}s")
    print(
        f"keepalive outcome={keepalive.outcome} elapsed={keepalive.elapsed_seconds:.3f}s "
        f"bytes={keepalive.bytes_received} intervals={KEEPALIVE_DELAYS_SECONDS}"
    )
    print(
        f"silent outcome={silent.outcome} elapsed={silent.elapsed_seconds:.3f}s "
        f"first_body_delay={SILENT_DELAY_SECONDS:.2f}s"
    )
    print("PASS: each received body chunk reset the inactivity read timeout")


if __name__ == "__main__":
    asyncio.run(main())
