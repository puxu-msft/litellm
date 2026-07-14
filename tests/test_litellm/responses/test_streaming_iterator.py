import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import litellm
from litellm.exceptions import MidStreamFallbackError
from litellm.responses.streaming_iterator import ResponsesAPIStreamingIterator


@pytest.mark.asyncio
async def test_streaming_iterator_deadline_maps_to_mid_stream_fallback_error():
    """3rd-round review finding C (responses half): a mid-stream total_timeout expiry must
    surface as MidStreamFallbackError wrapping litellm.Timeout, not the internal
    DeadlineExceeded -- otherwise Router._aresponses_streaming_iterator's `except
    MidStreamFallbackError` never engages and cross-deployment fallback silently never fires."""

    async def _aiter_bytes():
        yield b'data: {"type": "response.output_text.delta"}\n\n'
        await asyncio.sleep(10)
        yield b"never"  # pragma: no cover

    response = MagicMock()
    response.headers = {}
    response.aiter_bytes = _aiter_bytes
    response.aclose = AsyncMock()

    iterator = ResponsesAPIStreamingIterator(
        response=response,
        model="github_copilot/gpt-4",
        responses_api_provider_config=MagicMock(),
        logging_obj=MagicMock(),
        custom_llm_provider="openai",
        _http_client_deadline=asyncio.get_event_loop().time() + 0.05,
    )

    with pytest.raises(MidStreamFallbackError) as exc_info:
        async for _ in iterator:
            pass
    assert isinstance(exc_info.value.original_exception, litellm.Timeout)
    # one delta chunk was yielded before the deadline -> not a pre-first-chunk failure
    assert exc_info.value.is_pre_first_chunk is False
    response.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_streaming_iterator_no_deadline_unaffected():
    async def _aiter_bytes():
        yield b'data: {"type": "response.completed"}\n\n'

    response = MagicMock()
    response.headers = {}
    response.aiter_bytes = _aiter_bytes

    iterator = ResponsesAPIStreamingIterator(
        response=response,
        model="github_copilot/gpt-4",
        responses_api_provider_config=MagicMock(),
        logging_obj=MagicMock(),
    )
    with pytest.raises(StopAsyncIteration):
        while True:
            await iterator.__anext__()
