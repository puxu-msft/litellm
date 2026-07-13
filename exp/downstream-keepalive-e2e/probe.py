"""A-tier probe: hit the litellm proxy /v1/messages (streaming) with a TIGHT read
timeout, shorter than the mock's TTFB. With keepalive ON the proxy emits ``: ping``
comment frames that reset the client's inactivity/read timer, so the stream
survives and completes; with keepalive OFF the proxy holds the response during
TTFB and the client raises ReadTimeout.

Prints the outcome and whether keepalive frames were observed. Exit code 0 on
the EXPECTED outcome for the given mode, 1 otherwise — so run.sh can assert.

Usage: python probe.py --expect {survive,timeout} [--read-timeout 5] [--url ...] [--key ...]
"""

# ruff: noqa: T201 - a CLI probe; print IS the intended output

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx

PROXY_URL = "http://127.0.0.1:4000/v1/messages"
MASTER_KEY = "sk-keepalive-test"


async def probe(url: str, key: str, read_timeout: float) -> tuple[str, int, int, float]:
    body = {
        "model": "test-claude",
        "max_tokens": 64,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    timeout = httpx.Timeout(connect=5.0, read=read_timeout, write=5.0, pool=5.0)
    loop = asyncio.get_event_loop()
    start = loop.time()
    ping_frames = 0
    real_events = 0
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                buf = ""
                async for chunk in resp.aiter_text():
                    buf += chunk
                    while "\n\n" in buf:
                        frame, buf = buf.split("\n\n", 1)
                        frame = frame.strip("\n")
                        if not frame:
                            continue
                        if all(ln.startswith(":") for ln in frame.splitlines()):
                            ping_frames += 1
                            print(f"  [{loop.time() - start:5.1f}s] keepalive comment: {frame!r}")
                        else:
                            real_events += 1
                            first_line = frame.splitlines()[0]
                            print(f"  [{loop.time() - start:5.1f}s] event: {first_line}")
        return "survive", ping_frames, real_events, loop.time() - start
    except httpx.ReadTimeout:
        return "timeout", ping_frames, real_events, loop.time() - start


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect", choices=["survive", "timeout"], required=True)
    ap.add_argument("--read-timeout", type=float, default=5.0)
    ap.add_argument("--url", default=PROXY_URL)
    ap.add_argument("--key", default=MASTER_KEY)
    args = ap.parse_args()

    print(f"probe: expect={args.expect} read_timeout={args.read_timeout}s url={args.url}")
    outcome, pings, events, elapsed = await probe(args.url, args.key, args.read_timeout)
    print(f"result: outcome={outcome} pings={pings} real_events={events} elapsed={elapsed:.1f}s")

    ok = outcome == args.expect
    if args.expect == "survive":
        ok = ok and pings >= 1 and events >= 1  # keepalive fired AND real content arrived
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
