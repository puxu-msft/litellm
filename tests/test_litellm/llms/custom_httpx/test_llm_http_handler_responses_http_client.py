from unittest.mock import AsyncMock, Mock

import httpx
import pytest

import litellm
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler
from litellm.types.llms.openai import ResponsesAPIResponse
from litellm.types.router import GenericLiteLLMParams


def _make_provider_config() -> Mock:
    config = Mock()
    config.validate_environment.return_value = {}
    config.get_complete_url.return_value = "https://api.openai.com/v1/responses"
    config.transform_responses_api_request.return_value = {"model": "gpt-4o-mini", "input": "hi"}
    config.sign_request.return_value = ({}, None)
    config.transform_response_api_response.return_value = ResponsesAPIResponse(
        id="resp_1", created_at=0, output=[], status="completed", model="gpt-4o-mini"
    )
    return config


def _make_client() -> AsyncHTTPHandler:
    client = AsyncHTTPHandler()
    response = httpx.Response(
        200,
        json={"id": "resp_1", "output": [], "status": "completed"},
        request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
    )
    client.post = AsyncMock(return_value=response)
    return client


def _make_handler() -> BaseLLMHTTPHandler:
    handler = BaseLLMHTTPHandler()
    # post-response agentic-hooks path is out of scope here (the timeout/filtering assertions
    # are all made against the pre-post provider calls); stub it to pass the response through.
    handler._call_agentic_completion_hooks = AsyncMock(side_effect=lambda response, **kwargs: response)
    return handler


@pytest.mark.asyncio
async def test_aresponses_resolves_deployment_http_client_into_wire_timeout():
    handler = _make_handler()
    config = _make_provider_config()
    client = _make_client()

    await handler.async_response_api_handler(
        model="gpt-4o-mini",
        input="hi",
        responses_api_provider_config=config,
        response_api_optional_request_params={},
        custom_llm_provider="openai",
        litellm_params=GenericLiteLLMParams(api_key="sk-test", http_client={"connect_timeout": 2.0, "read_timeout": 9.0}),
        logging_obj=Mock(),
        client=client,
    )

    resolved = client.post.call_args.kwargs["timeout"]
    assert isinstance(resolved, httpx.Timeout)
    assert resolved.connect == 2.0
    assert resolved.read == 9.0


@pytest.mark.asyncio
async def test_aresponses_merges_global_http_client_with_deployment_override():
    handler = _make_handler()
    config = _make_provider_config()
    client = _make_client()

    original_global_http_client = getattr(litellm, "http_client", None)
    litellm.http_client = {"connect_timeout": 4.0, "pool_timeout": 12.0}
    try:
        await handler.async_response_api_handler(
            model="gpt-4o-mini",
            input="hi",
            responses_api_provider_config=config,
            response_api_optional_request_params={},
            custom_llm_provider="openai",
            litellm_params=GenericLiteLLMParams(api_key="sk-test", http_client={"connect_timeout": 2.0}),
            logging_obj=Mock(),
            client=client,
        )
    finally:
        litellm.http_client = original_global_http_client

    resolved = client.post.call_args.kwargs["timeout"]
    assert resolved.connect == 2.0  # deployment wins
    assert resolved.pool == 12.0  # falls back to global


@pytest.mark.asyncio
async def test_aresponses_strips_http_client_before_provider_calls():
    handler = _make_handler()
    config = _make_provider_config()
    client = _make_client()
    litellm_params = GenericLiteLLMParams(api_key="sk-test", http_client={"connect_timeout": 2.0})

    await handler.async_response_api_handler(
        model="gpt-4o-mini",
        input="hi",
        responses_api_provider_config=config,
        response_api_optional_request_params={},
        custom_llm_provider="openai",
        litellm_params=litellm_params,
        logging_obj=Mock(),
        client=client,
    )

    assert config.validate_environment.call_args.kwargs["litellm_params"].http_client is None
    assert "http_client" not in config.get_complete_url.call_args.kwargs["litellm_params"]
    assert config.transform_responses_api_request.call_args.kwargs["litellm_params"].http_client is None
    assert "http_client" not in config.sign_request.call_args.kwargs["optional_params"]

    # the caller's object must be untouched (filter builds a copy, never mutates in place)
    assert litellm_params.http_client is not None
    assert litellm_params.http_client.connect_timeout == 2.0
