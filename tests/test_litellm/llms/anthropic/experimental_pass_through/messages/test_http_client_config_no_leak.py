"""Regression: http_client config must never reach the outbound wire body sent to the upstream
/v1/messages endpoint by the native anthropic-messages handler.

Note (deviation from plan): the plan drove litellm.anthropic_messages(model="github_copilot/..."),
but that path makes a real network call and routes via the openai SDK (model_not_supported),
never touching AsyncHTTPHandler.post. This drives async_anthropic_messages_handler directly with
a configured provider-config mock (the Task 22 harness), capturing the `data` bytes handed to
the wire post()."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler
from litellm.types.router import GenericLiteLLMParams


def _make_configured_provider_config() -> MagicMock:
    config = MagicMock()
    config.validate_anthropic_messages_environment.return_value = ({}, "https://api.anthropic.com")
    config.should_filter_anthropic_beta_headers.return_value = False
    config.transform_anthropic_messages_request.return_value = {"model": "claude-3-haiku", "messages": []}
    config.get_complete_url.return_value = "https://api.anthropic.com/v1/messages"
    config.sign_request.return_value = ({}, None)  # signed_json_body None -> post gets data=json.dumps(request_body)
    config.max_retry_on_anthropic_messages_http_error = 1
    return config


@pytest.mark.asyncio
async def test_http_client_config_does_not_leak_into_anthropic_messages_wire_body():
    captured = {}

    async def _fake_post(**kwargs):
        captured["data"] = kwargs.get("data")
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        return response

    mock_client = MagicMock()
    mock_client.post = AsyncMock(side_effect=_fake_post)

    handler = BaseLLMHTTPHandler()
    with patch(
        "litellm.llms.custom_httpx.llm_http_handler.get_async_httpx_client",
        return_value=mock_client,
    ):
        try:
            await handler.async_anthropic_messages_handler(
                model="claude-3-haiku",
                messages=[{"role": "user", "content": "hi"}],
                anthropic_messages_provider_config=_make_configured_provider_config(),
                anthropic_messages_optional_request_params={},
                custom_llm_provider="anthropic",
                litellm_params=GenericLiteLLMParams(http_client={"connect_timeout": 2.0}),
                logging_obj=MagicMock(http_client_deadline=None),
            )
        except Exception:
            pass  # response handling beyond the post() is out of scope here

    body = captured["data"]
    assert body is not None
    assert "http_client" not in body  # data is a JSON string here


@pytest.mark.asyncio
async def test_anthropic_messages_direct_sdk_timeout_logs_partial_spend_not_failure_handler():
    """Frozen spec "plan b": a direct-SDK anthropic_messages() streaming call whose total_timeout
    fires mid-stream logs partial spend exactly once (via chunk_processor's finally) and never
    calls a failure handler -- matching chunk_processor's existing behavior."""
    import asyncio
    from typing import Coroutine, List

    import litellm
    from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLoggingObj
    from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER
    from litellm.proxy.pass_through_endpoints.streaming_handler import PassThroughStreamingHandler

    async def _slow_aiter_bytes():
        yield b'event: message_start\ndata: {"type": "message_start"}\n\n'
        await asyncio.sleep(10)
        yield b"never"  # pragma: no cover

    async def _fake_post(**kwargs):
        response = MagicMock()
        response.status_code = 200
        response.aiter_bytes = _slow_aiter_bytes
        response.aclose = AsyncMock()
        return response

    captured_coroutines: List[Coroutine] = []

    def _capture_enqueue(async_coroutine: Coroutine) -> None:
        captured_coroutines.append(async_coroutine)

    with (
        patch("litellm.llms.custom_httpx.http_handler.AsyncHTTPHandler.post", side_effect=_fake_post),
        patch.object(GLOBAL_LOGGING_WORKER, "ensure_initialized_and_enqueue", side_effect=_capture_enqueue),
        patch.object(
            PassThroughStreamingHandler, "_route_streaming_logging_to_handler", new=AsyncMock()
        ) as mock_partial_spend_logger,
        patch.object(LiteLLMLoggingObj, "failure_handler") as mock_sync_failure_handler,
        patch.object(LiteLLMLoggingObj, "async_failure_handler", new=AsyncMock()) as mock_async_failure_handler,
    ):
        response = await litellm.anthropic_messages(
            max_tokens=100,
            messages=[{"role": "user", "content": "hi"}],
            model="anthropic/claude-3-haiku",
            api_key="fake-key",
            stream=True,
            http_client={"total_timeout": 0.05},
        )
        with pytest.raises(Exception):
            async for _ in response:
                pass

        assert len(captured_coroutines) == 1, (
            "chunk_processor's finally block should have scheduled exactly one partial-spend coroutine"
        )
        await captured_coroutines[0]

    mock_partial_spend_logger.assert_awaited_once()
    mock_sync_failure_handler.assert_not_called()
    mock_async_failure_handler.assert_not_awaited()
