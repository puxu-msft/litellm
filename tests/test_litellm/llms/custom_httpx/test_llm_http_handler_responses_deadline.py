import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from litellm.litellm_core_utils.asyncio_deadline import DeadlineExceeded
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler
from litellm.types.router import GenericLiteLLMParams


def _mock_responses_api_provider_config():
    return MagicMock(
        validate_environment=MagicMock(return_value={}),
        get_complete_url=MagicMock(return_value="https://example.com/responses"),
        transform_responses_api_request=MagicMock(return_value={}),
        sign_request=MagicMock(return_value=({}, None)),
    )


@pytest.mark.asyncio
async def test_async_response_api_handler_non_streaming_raises_when_deadline_passed():
    handler = BaseLLMHTTPHandler()
    logging_obj = MagicMock()
    logging_obj.http_client_deadline = asyncio.get_event_loop().time() - 1
    logging_obj.pre_call = MagicMock()

    mock_client = MagicMock(spec=AsyncHTTPHandler)
    mock_client.post = AsyncMock()

    with pytest.raises(DeadlineExceeded):
        await handler.async_response_api_handler(
            model="github_copilot/gpt-4",
            input="hi",
            responses_api_provider_config=_mock_responses_api_provider_config(),
            response_api_optional_request_params={},
            custom_llm_provider="github_copilot",
            litellm_params=GenericLiteLLMParams(),
            logging_obj=logging_obj,
            extra_headers=None,
            extra_body=None,
            timeout=600.0,
            client=mock_client,
            fake_stream=False,
            litellm_metadata=None,
        )
    mock_client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_response_api_handler_streaming_raises_when_deadline_passed():
    handler = BaseLLMHTTPHandler()
    logging_obj = MagicMock()
    logging_obj.http_client_deadline = asyncio.get_event_loop().time() - 1
    logging_obj.pre_call = MagicMock()

    mock_client = MagicMock(spec=AsyncHTTPHandler)
    mock_client.post = AsyncMock()

    with pytest.raises(DeadlineExceeded):
        await handler.async_response_api_handler(
            model="github_copilot/gpt-4",
            input="hi",
            responses_api_provider_config=_mock_responses_api_provider_config(),
            response_api_optional_request_params={"stream": True},
            custom_llm_provider="github_copilot",
            litellm_params=GenericLiteLLMParams(),
            logging_obj=logging_obj,
            extra_headers=None,
            extra_body=None,
            timeout=600.0,
            client=mock_client,
            fake_stream=False,
            litellm_metadata=None,
        )
    mock_client.post.assert_not_awaited()
