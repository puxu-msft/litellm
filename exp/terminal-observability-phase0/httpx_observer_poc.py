from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

import httpx


class ScriptedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...], fail_after: int | None = None) -> None:
        self.chunks = chunks
        self.fail_after = fail_after
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for index, chunk in enumerate(self.chunks):
            if self.fail_after == index:
                raise RuntimeError("scripted upstream failure")
            yield chunk
        if self.fail_after == len(self.chunks):
            raise RuntimeError("scripted upstream failure")

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class Observation:
    chunks: list[bytes]
    closed: bool = False
    errors: list[str] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


class ObservedStream(httpx.AsyncByteStream):
    def __init__(
        self,
        inner: httpx.AsyncByteStream,
        observation: Observation,
        observer_fail_at: int | None = None,
    ) -> None:
        self.inner = inner
        self.observation = observation
        self.observer_fail_at = observer_fail_at
        self.observer_enabled = True

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.inner:
            if self.observer_enabled:
                try:
                    if self.observer_fail_at == len(self.observation.chunks):
                        raise RuntimeError("scripted observer failure")
                    self.observation.chunks.append(chunk)
                except Exception as exception:
                    assert self.observation.errors is not None
                    self.observation.errors.append(str(exception))
                    self.observer_enabled = False
            yield chunk

    async def aclose(self) -> None:
        self.observation.closed = True
        await self.inner.aclose()


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, response_chunks: tuple[bytes, ...]) -> None:
        self.request_chunks: list[bytes] = []
        self.response_stream = ScriptedStream(response_chunks)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        async for chunk in request.stream:
            self.request_chunks.append(chunk)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=self.response_stream)


class FailingTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        async for _chunk in request.stream:
            pass
        raise RuntimeError("scripted transport failure")


class ObservingTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        request_observation: Observation,
        response_observation: Observation,
    ) -> None:
        self.inner = inner
        self.request_observation = request_observation
        self.response_observation = response_observation

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request_stream = ObservedStream(request.stream, self.request_observation)
        request.stream = request_stream
        try:
            response = await self.inner.handle_async_request(request)
        finally:
            await request_stream.aclose()
        response.stream = ObservedStream(response.stream, self.response_observation)
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


async def _consume(stream: httpx.AsyncByteStream, stop_after: int | None = None) -> tuple[list[bytes], str | None]:
    chunks: list[bytes] = []
    error: str | None = None
    try:
        async for chunk in stream:
            chunks.append(chunk)
            if stop_after is not None and len(chunks) == stop_after:
                break
    except RuntimeError as exception:
        error = str(exception)
    finally:
        await stream.aclose()
    return chunks, error


async def _case(
    payload: bytes,
    cuts: tuple[int, ...],
    fail_after: int | None,
    observer_fail_at: int | None = None,
    stop_after: int | None = None,
) -> dict[str, object]:
    boundaries = (0, *cuts, len(payload))
    chunks = tuple(payload[start:end] for start, end in zip(boundaries, boundaries[1:]) if start != end)
    control_inner = ScriptedStream(chunks, fail_after)
    observed_inner = ScriptedStream(chunks, fail_after)
    observation = Observation([])
    control_chunks, control_error = await _consume(control_inner, stop_after)
    observed_chunks, observed_error = await _consume(
        ObservedStream(observed_inner, observation, observer_fail_at), stop_after
    )
    observer_errors = observation.errors or []
    return {
        "passed": (
            control_chunks == observed_chunks
            and control_error == observed_error
            and control_inner.closed
            and observed_inner.closed
            and observation.closed
            and (observer_fail_at is None or observer_errors == ["scripted observer failure"])
            and (observer_fail_at is not None or observation.chunks == observed_chunks)
        ),
        "cuts": cuts,
        "fail_after": fail_after,
        "observer_fail_at": observer_fail_at,
        "stop_after": stop_after,
        "chunk_lengths": [len(chunk) for chunk in chunks],
        "error": observed_error,
        "observer_errors": observer_errors,
    }


async def _chunked_request_case() -> dict[str, object]:
    chunks = (b'{"text":', '"hé'.encode(), 'llo"}'.encode())
    control_inner = ScriptedStream(chunks)
    observed_inner = ScriptedStream(chunks)
    observation = Observation([])
    observed_stream = ObservedStream(observed_inner, observation)
    control_request = httpx.Request("POST", "https://example.invalid/v1/messages", content=control_inner)
    observed_request = httpx.Request(
        "POST", "https://example.invalid/v1/messages", content=observed_stream
    )
    control_body = await control_request.aread()
    observed_body = await observed_request.aread()
    await control_inner.aclose()
    await observed_stream.aclose()
    return {
        "passed": (
            control_body == observed_body == b"".join(chunks)
            and observation.chunks == list(chunks)
            and control_inner.closed
            and observed_inner.closed
            and observation.closed
        ),
        "body_hex": observed_body.hex(),
        "chunk_lengths": [len(chunk) for chunk in chunks],
    }


async def _transport_case() -> dict[str, object]:
    request_chunks = (b'{"messages":', b"[]", b"}")
    response_chunks = (b"event: ping\n", b"data: {}\n\n")
    control_transport = RecordingTransport(response_chunks)
    observed_inner = RecordingTransport(response_chunks)
    request_observation = Observation([])
    response_observation = Observation([])
    observed_transport = ObservingTransport(observed_inner, request_observation, response_observation)

    async with httpx.AsyncClient(transport=control_transport) as control_client:
        control_request = httpx.Request(
            "POST", "https://example.invalid/v1/messages", content=ScriptedStream(request_chunks)
        )
        control_response = await control_client.send(control_request, stream=True)
        control_response_chunks = [chunk async for chunk in control_response.aiter_raw()]
        await control_response.aclose()

    async with httpx.AsyncClient(transport=observed_transport) as observed_client:
        observed_request = httpx.Request(
            "POST", "https://example.invalid/v1/messages", content=ScriptedStream(request_chunks)
        )
        observed_response = await observed_client.send(observed_request, stream=True)
        observed_response_chunks = [chunk async for chunk in observed_response.aiter_raw()]
        await observed_response.aclose()

    return {
        "passed": (
            control_transport.request_chunks
            == observed_inner.request_chunks
            == request_observation.chunks
            == list(request_chunks)
            and control_response_chunks
            == observed_response_chunks
            == response_observation.chunks
            == list(response_chunks)
            and control_transport.response_stream.closed
            and observed_inner.response_stream.closed
            and request_observation.closed
            and response_observation.closed
        ),
        "request_chunk_lengths": [len(chunk) for chunk in observed_inner.request_chunks],
        "response_chunk_lengths": [len(chunk) for chunk in observed_response_chunks],
    }


async def _transport_error_case() -> dict[str, object]:
    request_chunks = (b"first", b"second")
    request_observation = Observation([])
    response_observation = Observation([])
    transport = ObservingTransport(FailingTransport(), request_observation, response_observation)
    error: str | None = None
    try:
        async with httpx.AsyncClient(transport=transport) as client:
            request = httpx.Request(
                "POST", "https://example.invalid/v1/messages", content=ScriptedStream(request_chunks)
            )
            await client.send(request, stream=True)
    except RuntimeError as exception:
        error = str(exception)
    return {
        "passed": (
            error == "scripted transport failure"
            and request_observation.chunks == list(request_chunks)
            and request_observation.closed
            and response_observation.chunks == []
        ),
        "error": error,
        "request_chunk_lengths": [len(chunk) for chunk in request_observation.chunks],
    }


async def run() -> dict[str, object]:
    payload = "event: content_block_delta\ndata: {\"text\":\"héllo\"}\n\n".encode()
    cases: list[dict[str, object]] = []
    for cut in range(1, len(payload)):
        cases.append(await _case(payload, (cut,), None))
    chunks = (5, 11, 23, len(payload) - 1)
    for fail_after in range(5):
        cases.append(await _case(payload, chunks, fail_after))
    for observer_fail_at in range(4):
        cases.append(await _case(payload, chunks, None, observer_fail_at=observer_fail_at))
    for stop_after in range(1, 5):
        cases.append(await _case(payload, chunks, None, stop_after=stop_after))

    request = httpx.Request("POST", "https://example.invalid/v1/messages", json={"text": "héllo"})
    request_body = await request.aread()
    expected_request = '{"text":"héllo"}'.encode()
    chunked_request = await _chunked_request_case()
    transport = await _transport_case()
    transport_error = await _transport_error_case()
    return {
        "passed": (
            all(bool(case["passed"]) for case in cases)
            and request_body == expected_request
            and bool(chunked_request["passed"])
            and bool(transport["passed"])
            and bool(transport_error["passed"])
        ),
        "case_count": len(cases),
        "request_body_hex": request_body.hex(),
        "expected_request_body_hex": expected_request.hex(),
        "chunked_request": chunked_request,
        "transport": transport,
        "transport_error": transport_error,
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run())
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    summary = {key: value for key, value in result.items() if key != "cases"}
    sys.stdout.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()