import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from litellm.litellm_core_utils.asyncio_deadline import DeadlineExceeded
from litellm.llms.openai.openai import OpenAIChatCompletion

_DATA = {"messages": [{"role": "user", "content": "hi"}]}


def _handler_with_mocked_client():
    """OpenAIChatCompletion with _get_openai_client stubbed so tests reach the deadline
    wrap at the make_openai_chat_completion_request call without a real OpenAI client."""
    handler = OpenAIChatCompletion()
    handler._get_openai_client = MagicMock(return_value=MagicMock())
    return handler


def _logging_obj(deadline):
    logging_obj = MagicMock()
    logging_obj.http_client_deadline = deadline
    logging_obj.pre_call = MagicMock()
    logging_obj.model_call_details = {}
    return logging_obj


@pytest.mark.asyncio
async def test_acompletion_raises_deadline_exceeded_when_deadline_already_passed():
    handler = _handler_with_mocked_client()
    logging_obj = _logging_obj(asyncio.get_event_loop().time() - 1)

    make_request_mock = AsyncMock()
    handler.make_openai_chat_completion_request = make_request_mock

    with pytest.raises(DeadlineExceeded):
        await handler.acompletion(
            messages=[{"role": "user", "content": "hi"}],
            optional_params={},
            litellm_params={},
            provider_config=MagicMock(async_transform_request=AsyncMock(return_value=_DATA)),
            model="github_copilot/gpt-4",
            model_response=MagicMock(),
            logging_obj=logging_obj,
            timeout=600.0,
        )
    make_request_mock.assert_not_awaited()  # coroutine created but never awaited: request not sent


@pytest.mark.asyncio
async def test_async_streaming_raises_deadline_exceeded_when_deadline_already_passed():
    handler = _handler_with_mocked_client()
    logging_obj = _logging_obj(asyncio.get_event_loop().time() - 1)

    make_request_mock = AsyncMock()
    handler.make_openai_chat_completion_request = make_request_mock

    with pytest.raises(DeadlineExceeded):
        await handler.async_streaming(
            timeout=600.0,
            messages=[{"role": "user", "content": "hi"}],
            optional_params={},
            litellm_params={},
            provider_config=MagicMock(transform_request=MagicMock(return_value=_DATA)),
            model="github_copilot/gpt-4",
            logging_obj=logging_obj,
        )
    make_request_mock.assert_not_awaited()  # coroutine created but never awaited: request not sent


@pytest.mark.asyncio
async def test_acompletion_propagates_deadline_exceeded_without_converting_to_openai_error():
    """Guards against acompletion()'s own except-Exception block flattening DeadlineExceeded
    into a generic OpenAIError -- if that happened, exception_type()'s new DeadlineExceeded
    branch would never see the original exception type and could never fire."""
    handler = _handler_with_mocked_client()
    logging_obj = _logging_obj(None)

    handler.make_openai_chat_completion_request = AsyncMock(
        side_effect=DeadlineExceeded("simulated mid-await timeout")
    )

    with pytest.raises(DeadlineExceeded):
        await handler.acompletion(
            messages=[{"role": "user", "content": "hi"}],
            optional_params={},
            litellm_params={},
            provider_config=MagicMock(async_transform_request=AsyncMock(return_value=_DATA)),
            model="github_copilot/gpt-4",
            model_response=MagicMock(),
            logging_obj=logging_obj,
            timeout=600.0,
        )


@pytest.mark.asyncio
async def test_async_streaming_propagates_deadline_exceeded_without_converting_to_openai_error():
    """Same guard as above, for async_streaming()'s separately-written except-Exception block."""
    handler = _handler_with_mocked_client()
    logging_obj = _logging_obj(None)

    handler.make_openai_chat_completion_request = AsyncMock(
        side_effect=DeadlineExceeded("simulated mid-await timeout")
    )

    with pytest.raises(DeadlineExceeded):
        await handler.async_streaming(
            timeout=600.0,
            messages=[{"role": "user", "content": "hi"}],
            optional_params={},
            litellm_params={},
            provider_config=MagicMock(transform_request=MagicMock(return_value=_DATA)),
            model="github_copilot/gpt-4",
            logging_obj=logging_obj,
        )
