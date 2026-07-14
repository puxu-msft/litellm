"""Regression: http_client config must never reach the outbound wire body sent to the upstream
responses API endpoint by the native async_response_api_handler, including via the
dict(litellm_params) values threaded through get_complete_url/sign_request internally.

Note (deviation from plan): the plan drove litellm.aresponses(model="github_copilot/gpt-4"),
but that github_copilot path bridges to chat completions (never touching AsyncHTTPHandler.post,
so 0 wire bodies are captured). This uses the same direct-injection harness as Task 18a against
the *native* responses handler, capturing the actual bytes handed to client.post()."""

import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler
from litellm.types.llms.openai import ResponsesAPIResponse
from litellm.types.router import GenericLiteLLMParams


@pytest.mark.asyncio
async def test_http_client_config_does_not_leak_into_responses_wire_body():
    captured = {}

    async def _fake_post(*args, **kwargs):
        captured["body"] = kwargs.get("data") if kwargs.get("data") is not None else kwargs.get("json")
        return httpx.Response(
            200,
            json={"id": "resp_1", "output": [], "status": "completed"},
            request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
        )

    client = AsyncHTTPHandler()
    client.post = AsyncMock(side_effect=_fake_post)

    config = Mock()
    config.validate_environment.return_value = {}
    config.get_complete_url.return_value = "https://api.openai.com/v1/responses"
    # a real provider transform: forwards the model/input as the request body (never http_client)
    config.transform_responses_api_request.return_value = {"model": "gpt-4o-mini", "input": "hi"}
    config.sign_request.return_value = ({}, None)  # signed_body None -> body_kwargs uses `json=data`
    config.transform_response_api_response.return_value = ResponsesAPIResponse(
        id="resp_1", created_at=0, output=[], status="completed", model="gpt-4o-mini"
    )

    handler = BaseLLMHTTPHandler()
    handler._call_agentic_completion_hooks = AsyncMock(side_effect=lambda response, **kwargs: response)

    await handler.async_response_api_handler(
        model="gpt-4o-mini",
        input="hi",
        responses_api_provider_config=config,
        response_api_optional_request_params={},
        custom_llm_provider="openai",
        litellm_params=GenericLiteLLMParams(api_key="sk-test", http_client={"connect_timeout": 2.0, "total_timeout": 30.0}),
        logging_obj=Mock(http_client_deadline=None),
        client=client,
    )

    body = captured["body"]
    assert body is not None
    assert "http_client" not in json.dumps(body)
