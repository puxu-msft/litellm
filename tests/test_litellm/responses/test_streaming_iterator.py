import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from litellm.litellm_core_utils.asyncio_deadline import DeadlineExceeded
from litellm.responses.streaming_iterator import ResponsesAPIStreamingIterator


@pytest.mark.asyncio
async def test_streaming_iterator_raises_deadline_exceeded_and_closes_response():
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
        _http_client_deadline=asyncio.get_event_loop().time() + 0.05,
    )

    with pytest.raises(DeadlineExceeded):
        async for _ in iterator:
            pass
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
