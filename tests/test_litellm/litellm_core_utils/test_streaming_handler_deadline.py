import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from litellm.exceptions import MidStreamFallbackError
from litellm.litellm_core_utils.asyncio_deadline import DeadlineBoundAsyncIterator
from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper


def _logging_obj(deadline):
    logging_obj = MagicMock()
    logging_obj.http_client_deadline = deadline
    # the mid-stream failure path does asyncio.create_task(async_failure_handler(...))
    logging_obj.async_failure_handler = AsyncMock()
    return logging_obj


# NOTE (deviation from plan Task 16 literal tests): the plan's iteration tests asserted
# `pytest.raises(DeadlineExceeded)`, but that contradicts Task 8a + Task 16a (both landed):
# a mid-stream DeadlineExceeded is mapped to litellm.Timeout (408) and, being fallback-eligible,
# wrapped into MidStreamFallbackError -- it never surfaces raw. These tests assert that true
# behavior plus the structural wrapping and raw-stream close, which is what Task 16's production
# change (wrapping completion_stream in DeadlineBoundAsyncIterator) actually guarantees. Raw
# sleeping streams isolate deadline enforcement from chunk_creator (a bare MagicMock chunk is not
# processable and would IndexError before the deadline).


@pytest.mark.asyncio
async def test_custom_stream_wrapper_enforces_deadline_on_async_iteration():
    class _RawStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(10)  # pragma: no cover

    wrapper = CustomStreamWrapper(
        completion_stream=_RawStream(),
        model="github_copilot/gpt-4",
        logging_obj=_logging_obj(asyncio.get_event_loop().time() + 0.05),
        custom_llm_provider="github_copilot",
    )
    assert isinstance(wrapper.completion_stream, DeadlineBoundAsyncIterator)

    with pytest.raises(MidStreamFallbackError):
        async for _ in wrapper:
            pass


@pytest.mark.asyncio
async def test_custom_stream_wrapper_no_deadline_leaves_stream_unwrapped():
    async def _chunks():
        yield MagicMock()  # pragma: no cover

    wrapper = CustomStreamWrapper(
        completion_stream=_chunks(),
        model="github_copilot/gpt-4",
        logging_obj=_logging_obj(None),
        custom_llm_provider="github_copilot",
    )
    # deadline None -> stream is not wrapped, so iteration behaviour is byte-for-byte unchanged
    assert not isinstance(wrapper.completion_stream, DeadlineBoundAsyncIterator)


@pytest.mark.asyncio
async def test_custom_stream_wrapper_deadline_timeout_closes_the_raw_completion_stream():
    """Review finding #6 regression: the timeout-close callback must reach the RAW
    completion_stream (the connection-owning object), routed through the wrapping
    DeadlineBoundAsyncIterator's aclose() delegation -- not stop at the wrapper."""

    class _RawStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(10)  # pragma: no cover

        async def aclose(self):
            closed.append(True)

    closed: list = []
    wrapper = CustomStreamWrapper(
        completion_stream=_RawStream(),
        model="github_copilot/gpt-4",
        logging_obj=_logging_obj(asyncio.get_event_loop().time() + 0.05),
        custom_llm_provider="github_copilot",
    )

    with pytest.raises(MidStreamFallbackError):
        async for _ in wrapper:
            pass

    assert closed == [True]


@pytest.mark.asyncio
async def test_custom_stream_wrapper_aclose_closes_raw_stream_exactly_once_on_client_disconnect():
    """3rd-round review, minor #1: a client disconnect reaches CustomStreamWrapper.aclose()
    directly and must still close the RAW completion_stream exactly once, routed through the
    wrapping DeadlineBoundAsyncIterator (Task 9's aclose() delegation)."""

    class _RawStream:
        def __init__(self):
            self.close_count = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def aclose(self):
            self.close_count += 1

    raw_stream = _RawStream()
    wrapper = CustomStreamWrapper(
        completion_stream=raw_stream,
        model="github_copilot/gpt-4",
        logging_obj=_logging_obj(asyncio.get_event_loop().time() + 30),
        custom_llm_provider="github_copilot",
    )

    assert isinstance(wrapper.completion_stream, DeadlineBoundAsyncIterator)

    await wrapper.aclose()

    assert raw_stream.close_count == 1
