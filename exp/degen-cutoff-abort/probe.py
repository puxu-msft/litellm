from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime
import os
import sys
from dataclasses import dataclass
from typing import AsyncIterator, Callable

import httpx
from openai import AsyncOpenAI, AsyncStream
from openai.types.chat import ChatCompletionChunk

PROJECT_ROOT = "/home/xp/refs/ai-agents/litellm"
HOOK_ROOT = "/home/xp/.config/litellm"
NO_CONFIG = os.path.join(PROJECT_ROOT, "exp/degen-cutoff-abort/no-such-config.json")
os.environ["LITELLM_HOOKS_CONFIG"] = NO_CONFIG
sys.path.insert(0, HOOK_ROOT)

from hookpkg.stream import stream_transform as real_stream_transform  # noqa: E402
from litellm.litellm_core_utils.litellm_logging import Logging  # noqa: E402
from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper  # noqa: E402
from litellm.llms.anthropic.experimental_pass_through.adapters.streaming_iterator import (  # noqa: E402
    AnthropicStreamWrapper,
)
from litellm.proxy.pass_through_endpoints.streaming_handler import (  # noqa: E402
    PassThroughStreamingHandler,
)
from litellm.types.passthrough_endpoints.pass_through_endpoints import EndpointType  # noqa: E402
from litellm.types.utils import ModelResponseStream  # noqa: E402


@dataclass
class State:
    total: int
    produced: int = 0
    generator_exit: bool = False
    finally_ran: bool = False
    stream_aclose_calls: int = 0
    response_aclose_calls: int = 0

    @property
    def closed(self) -> bool:
        return self.finally_ran


class SentinelByteStream(httpx.AsyncByteStream):
    def __init__(self, state: State, make_bytes: Callable[[int], bytes]) -> None:
        self.state = state
        self.make_bytes = make_bytes
        self._generator = self._generate()

    async def _generate(self) -> AsyncIterator[bytes]:
        try:
            for index in range(self.state.total):
                self.state.produced += 1
                yield self.make_bytes(index)
                await asyncio.sleep(0)
        except GeneratorExit:
            self.state.generator_exit = True
            raise
        finally:
            self.state.finally_ran = True

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._generator

    async def aclose(self) -> None:
        self.state.stream_aclose_calls += 1
        await self._generator.aclose()


class OpenAIStyleSentinelStream:
    """Minimal stand-in for openai.AsyncStream: async iterable plus close(), no aclose()."""

    def __init__(self, state: State) -> None:
        self.state = state
        self._generator = sentinel_openai_chunks(state)

    def __aiter__(self) -> AsyncIterator[ModelResponseStream]:
        return self._generator

    async def close(self) -> None:
        self.state.stream_aclose_calls += 1
        await self._generator.aclose()


class CountingResponse(httpx.Response):
    def __init__(self, state: State, stream: SentinelByteStream) -> None:
        super().__init__(200, request=httpx.Request("POST", "https://sentinel.invalid/v1/messages"), stream=stream)
        self.state = state

    async def aclose(self) -> None:
        self.state.response_aclose_calls += 1
        await super().aclose()


class LoggingStub:
    model_call_details = {"model": "sentinel-model"}


def openai_chunk(index: int) -> ModelResponseStream:
    return ModelResponseStream(
        id=f"chunk-{index}",
        model="sentinel-model",
        choices=[{"index": 0, "delta": {"content": f"token-{index}"}, "finish_reason": None}],
    )


def anthropic_sse_chunk(index: int) -> bytes:
    payload = f'{{"type":"ping","index":{index}}}'
    return f"event: ping\ndata: {payload}\n\n".encode()


def openai_sse_chunk(index: int) -> bytes:
    payload = (
        f'{{"id":"chunk-{index}","object":"chat.completion.chunk",'
        f'"created":1,"model":"sentinel-model","choices":['
        f'{{"index":0,"delta":{{"content":"token-{index}"}},"finish_reason":null}}]}}'
    )
    return f"data: {payload}\n\n".encode()


def make_logging() -> Logging:
    return Logging(
        model="sentinel-model",
        messages=[{"role": "user", "content": "probe"}],
        stream=True,
        call_type="acompletion",
        start_time=datetime.datetime.now(),
        litellm_call_id="degen-cutoff-abort-poc",
        function_id="degen-cutoff-abort-poc",
        kwargs={"custom_llm_provider": "openai"},
    )


async def passthrough_stream(response: httpx.Response) -> AsyncIterator[bytes]:
    async for chunk in PassThroughStreamingHandler.chunk_processor(
        response=response,
        request_body={"model": "sentinel-model"},
        litellm_logging_obj=LoggingStub(),  # type: ignore[arg-type]
        endpoint_type=EndpointType.ANTHROPIC,
        start_time=datetime.datetime.now(),
        passthrough_success_handler_obj=object(),  # type: ignore[arg-type]
        url_route="/v1/messages",
    ):
        yield chunk


async def passthrough_transform(response: AsyncIterator[bytes], request_data: dict[str, str]) -> AsyncIterator[bytes]:
    del request_data
    async for chunk in response:
        yield chunk


async def early_return_transform(
    response: AsyncIterator[bytes], request_data: dict[str, str], *, stop_after: int
) -> AsyncIterator[bytes]:
    del request_data
    seen = 0
    async for chunk in response:
        seen += 1
        yield chunk
        if seen == stop_after:
            return


async def run_consumer(
    transformed: AsyncIterator[bytes], *, stop_after: int | None, close_transform: bool
) -> int:
    consumed = 0
    async for _ in transformed:
        consumed += 1
        if stop_after is not None and consumed == stop_after:
            break
    if close_transform:
        await transformed.aclose()  # type: ignore[attr-defined]
    await asyncio.sleep(0)
    return consumed


async def adapter_case(mode: str, total: int, stop_after: int) -> dict[str, object]:
    state = State(total=total)
    sentinel = OpenAIStyleSentinelStream(state)

    custom = CustomStreamWrapper(
        completion_stream=sentinel,
        model="sentinel-model",
        logging_obj=make_logging(),
        custom_llm_provider="openai",
    )
    anthropic = AnthropicStreamWrapper(completion_stream=custom, model="sentinel-model")
    sse = anthropic.async_anthropic_sse_wrapper()

    if mode == "drain":
        transformed = passthrough_transform(sse, {})
        consumed = await run_consumer(transformed, stop_after=None, close_transform=False)
    elif mode == "consumer-break":
        transformed = passthrough_transform(sse, {})
        consumed = await run_consumer(transformed, stop_after=stop_after, close_transform=True)
    elif mode == "transform-return":
        transformed = early_return_transform(sse, {}, stop_after=stop_after)
        consumed = await run_consumer(transformed, stop_after=None, close_transform=False)
    elif mode == "real-transform-close":
        transformed = real_stream_transform(sse, {})
        consumed = await run_consumer(transformed, stop_after=stop_after, close_transform=True)
    else:
        raise ValueError(mode)

    before_explicit_close = snapshot(state, consumed)
    await sse.aclose()
    await asyncio.sleep(0)
    after_adapter_sse_close = snapshot(state, consumed)
    await custom.aclose()
    await asyncio.sleep(0)
    after_explicit_close = snapshot(state, consumed)
    return {
        "case": f"adapter:{mode}",
        "before_explicit_upstream_close": before_explicit_close,
        "after_explicit_adapter_sse_aclose": after_adapter_sse_close,
        "after_explicit_custom_aclose": after_explicit_close,
    }


async def sentinel_openai_chunks(state: State) -> AsyncIterator[ModelResponseStream]:
    try:
        for index in range(state.total):
            state.produced += 1
            yield openai_chunk(index)
            await asyncio.sleep(0)
    except GeneratorExit:
        state.generator_exit = True
        raise
    finally:
        state.finally_ran = True


async def openai_sdk_case(mode: str, total: int, stop_after: int) -> dict[str, object]:
    state = State(total=total)
    byte_stream = SentinelByteStream(state, openai_sse_chunk)
    response = CountingResponse(state, byte_stream)
    client = AsyncOpenAI(api_key="sentinel", base_url="https://sentinel.invalid")
    sdk_stream = AsyncStream(cast_to=ChatCompletionChunk, response=response, client=client)
    custom = CustomStreamWrapper(
        completion_stream=sdk_stream,
        model="sentinel-model",
        logging_obj=make_logging(),
        custom_llm_provider="openai",
    )
    anthropic = AnthropicStreamWrapper(completion_stream=custom, model="sentinel-model")
    sse = anthropic.async_anthropic_sse_wrapper()

    if mode == "drain":
        transformed = passthrough_transform(sse, {})
        consumed = await run_consumer(transformed, stop_after=None, close_transform=False)
    elif mode == "transform-return":
        transformed = early_return_transform(sse, {}, stop_after=stop_after)
        consumed = await run_consumer(transformed, stop_after=None, close_transform=False)
    elif mode == "real-transform-close":
        transformed = real_stream_transform(sse, {})
        consumed = await run_consumer(transformed, stop_after=stop_after, close_transform=True)
    else:
        raise ValueError(mode)

    before_explicit_close = snapshot(state, consumed, response)
    await sse.aclose()
    await asyncio.sleep(0)
    after_adapter_sse_close = snapshot(state, consumed, response)
    await custom.aclose()
    await asyncio.sleep(0)
    after_custom_close = snapshot(state, consumed, response)
    await sdk_stream.close()
    await asyncio.sleep(0)
    after_sdk_close = snapshot(state, consumed, response)
    await client.close()
    return {
        "case": f"openai-sdk:{mode}",
        "before_explicit_upstream_close": before_explicit_close,
        "after_explicit_adapter_sse_aclose": after_adapter_sse_close,
        "after_explicit_custom_aclose": after_custom_close,
        "after_explicit_sdk_close": after_sdk_close,
    }


async def native_http_case(mode: str, total: int, stop_after: int) -> dict[str, object]:
    state = State(total=total)
    stream = SentinelByteStream(state, anthropic_sse_chunk)
    response = CountingResponse(state, stream)
    upstream = passthrough_stream(response)

    if mode == "drain":
        transformed = passthrough_transform(upstream, {})
        consumed = await run_consumer(transformed, stop_after=None, close_transform=False)
    elif mode == "transform-return":
        transformed = early_return_transform(upstream, {}, stop_after=stop_after)
        consumed = await run_consumer(transformed, stop_after=None, close_transform=False)
    elif mode == "real-transform-close":
        transformed = real_stream_transform(upstream, {})
        consumed = await run_consumer(transformed, stop_after=stop_after, close_transform=True)
    else:
        raise ValueError(mode)

    before_explicit_close = snapshot(state, consumed, response)
    await upstream.aclose()
    await asyncio.sleep(0)
    after_chunk_processor_close = snapshot(state, consumed, response)
    await response.aclose()
    await asyncio.sleep(0)
    after_explicit_close = snapshot(state, consumed, response)
    return {
        "case": f"native-http:{mode}",
        "before_explicit_response_close": before_explicit_close,
        "after_explicit_chunk_processor_aclose": after_chunk_processor_close,
        "after_explicit_response_aclose": after_explicit_close,
    }


def snapshot(state: State, consumed: int, response: httpx.Response | None = None) -> dict[str, object]:
    result: dict[str, object] = {
        "consumed": consumed,
        "produced": state.produced,
        "total": state.total,
        "closed": state.closed,
        "generator_exit": state.generator_exit,
        "stream_aclose_calls": state.stream_aclose_calls,
        "response_aclose_calls": state.response_aclose_calls,
    }
    if response is not None:
        result["httpx_response_is_closed"] = response.is_closed
    return result


def assert_results(results: list[dict[str, object]], total: int) -> None:
    indexed = {result["case"]: result for result in results}

    adapter_drain = indexed["adapter:drain"]["before_explicit_upstream_close"]
    assert isinstance(adapter_drain, dict)
    assert adapter_drain["produced"] == total and adapter_drain["closed"] is True

    adapter_break = indexed["adapter:consumer-break"]["before_explicit_upstream_close"]
    assert isinstance(adapter_break, dict)
    assert int(adapter_break["produced"]) < total and adapter_break["closed"] is False

    adapter_return = indexed["adapter:transform-return"]["before_explicit_upstream_close"]
    assert isinstance(adapter_return, dict)
    assert int(adapter_return["produced"]) < total and adapter_return["closed"] is False

    adapter_real = indexed["adapter:real-transform-close"]["before_explicit_upstream_close"]
    assert isinstance(adapter_real, dict)
    assert int(adapter_real["produced"]) < total and adapter_real["closed"] is False

    sdk_drain = indexed["openai-sdk:drain"]["before_explicit_upstream_close"]
    assert isinstance(sdk_drain, dict)
    assert sdk_drain["produced"] == total and sdk_drain["closed"] is True
    assert sdk_drain["httpx_response_is_closed"] is True

    for case_name in ("openai-sdk:transform-return", "openai-sdk:real-transform-close"):
        before = indexed[case_name]["before_explicit_upstream_close"]
        assert isinstance(before, dict)
        assert int(before["produced"]) < total and before["closed"] is False
        assert before["httpx_response_is_closed"] is False
        after_adapter = indexed[case_name]["after_explicit_adapter_sse_aclose"]
        assert isinstance(after_adapter, dict)
        assert after_adapter["closed"] is False and after_adapter["httpx_response_is_closed"] is False
        after_custom = indexed[case_name]["after_explicit_custom_aclose"]
        assert isinstance(after_custom, dict)
        assert after_custom["closed"] is True and after_custom["httpx_response_is_closed"] is True
        assert int(after_custom["produced"]) < total
        after_sdk = indexed[case_name]["after_explicit_sdk_close"]
        assert isinstance(after_sdk, dict)
        assert after_sdk["closed"] is True and after_sdk["httpx_response_is_closed"] is True

    native_drain = indexed["native-http:drain"]["before_explicit_response_close"]
    assert isinstance(native_drain, dict)
    assert native_drain["produced"] == total and native_drain["closed"] is True
    assert native_drain["httpx_response_is_closed"] is True

    native_return = indexed["native-http:transform-return"]["before_explicit_response_close"]
    assert isinstance(native_return, dict)
    assert int(native_return["produced"]) < total and native_return["closed"] is False
    assert native_return["httpx_response_is_closed"] is False

    native_real = indexed["native-http:real-transform-close"]["before_explicit_response_close"]
    assert isinstance(native_real, dict)
    assert int(native_real["produced"]) < total and native_real["closed"] is False
    assert native_real["httpx_response_is_closed"] is False

    for case_name in (
        "adapter:consumer-break",
        "adapter:transform-return",
        "adapter:real-transform-close",
    ):
        after_adapter = indexed[case_name]["after_explicit_adapter_sse_aclose"]
        assert isinstance(after_adapter, dict)
        assert after_adapter["closed"] is False and int(after_adapter["produced"]) < total
        after = indexed[case_name]["after_explicit_custom_aclose"]
        assert isinstance(after, dict)
        assert after["closed"] is True and int(after["produced"]) < total

    for case_name in ("native-http:transform-return", "native-http:real-transform-close"):
        after_processor = indexed[case_name]["after_explicit_chunk_processor_aclose"]
        assert isinstance(after_processor, dict)
        assert after_processor["closed"] is False and after_processor["httpx_response_is_closed"] is False
        after = indexed[case_name]["after_explicit_response_aclose"]
        assert isinstance(after, dict)
        assert after["closed"] is True and int(after["produced"]) < total
        assert after["httpx_response_is_closed"] is True


async def main(total: int, stop_after: int) -> None:
    results = [
        await adapter_case("drain", total, stop_after),
        await adapter_case("consumer-break", total, stop_after),
        await adapter_case("transform-return", total, stop_after),
        await adapter_case("real-transform-close", total, stop_after),
        await openai_sdk_case("drain", total, stop_after),
        await openai_sdk_case("transform-return", total, stop_after),
        await openai_sdk_case("real-transform-close", total, stop_after),
        await native_http_case("drain", total, stop_after),
        await native_http_case("transform-return", total, stop_after),
        await native_http_case("real-transform-close", total, stop_after),
    ]
    for result in results:
        print(result)  # noqa: T201 - probe output is evidence
    assert_results(results, total)
    print("ASSERTIONS: PASS")  # noqa: T201 - probe output is evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--total", type=int, default=100)
    parser.add_argument("--stop-after", type=int, default=5)
    args = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(args.total, args.stop_after))
