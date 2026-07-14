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


def test_get_openai_client_warns_when_custom_client_bypasses_http_client_config(caplog):
    import logging

    handler = OpenAIChatCompletion()
    fake_client = MagicMock()

    with caplog.at_level(logging.WARNING, logger="LiteLLM"):
        handler._get_openai_client(
            is_async=True,
            api_key="fake",
            api_base="https://example.com",
            timeout=600.0,
            client=fake_client,
            http_client_config_present=True,
        )

    assert any(
        "http_client" in record.message and "custom client" in record.message.lower() for record in caplog.records
    )


def test_get_openai_client_silent_when_no_http_client_config(caplog):
    import logging

    handler = OpenAIChatCompletion()
    fake_client = MagicMock()

    with caplog.at_level(logging.WARNING, logger="LiteLLM"):
        handler._get_openai_client(
            is_async=True,
            api_key="fake",
            api_base="https://example.com",
            timeout=600.0,
            client=fake_client,
            http_client_config_present=False,
        )

    assert not any("will be ignored" in record.message for record in caplog.records)


def test_get_async_http_client_warns_when_aclient_session_bypasses_http_client_config(caplog):
    import logging

    import litellm
    from litellm.llms.openai.common_utils import BaseOpenAILLM

    original_aclient_session = litellm.aclient_session
    litellm.aclient_session = MagicMock()
    try:
        with caplog.at_level(logging.WARNING, logger="LiteLLM"):
            BaseOpenAILLM._get_async_http_client(http_client_config_present=True)
    finally:
        litellm.aclient_session = original_aclient_session

    assert any(
        "http_client" in record.message and "custom client" in record.message.lower() for record in caplog.records
    )
