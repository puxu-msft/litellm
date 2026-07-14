"""Regression tests for the responses -> completion fallback bridge guard.

When the Responses API falls back to chat completions (no native responses
config), it must tag the forwarded ``litellm.completion`` / ``litellm.acompletion``
call with ``_skip_responses_api_bridge=True`` so ``completion()`` does not bridge
the request straight back to the Responses API and mutually recurse forever.

Both fallback paths are covered: the sync ``response_api_handler`` (``_is_async``
False) and the async ``async_response_api_handler`` (``_is_async`` True). The
module-level ``litellm.completion`` / ``litellm.acompletion`` are patched to
capture the forwarded kwargs; if the flag-setting line is removed the captured
kwargs lack the flag and these tests fail.
"""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath("../../../.."))

from litellm.responses.litellm_completion_transformation.handler import (
    LiteLLMCompletionTransformationHandler,
)


class _StopForwarding(Exception):
    """Raised by the mocked (a)completion once the forwarded kwargs are captured."""


def test_sync_fallback_tags_skip_responses_api_bridge():
    handler = LiteLLMCompletionTransformationHandler()
    captured: dict = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        raise _StopForwarding()

    with patch("litellm.completion", fake_completion):
        with pytest.raises(_StopForwarding):
            handler.response_api_handler(
                model="gpt-4o",
                input="hello",
                responses_api_request={},
                custom_llm_provider="openai",
                _is_async=False,
            )

    assert captured.get("_skip_responses_api_bridge") is True


@pytest.mark.asyncio
async def test_async_fallback_tags_skip_responses_api_bridge():
    handler = LiteLLMCompletionTransformationHandler()
    captured: dict = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        raise _StopForwarding()

    with patch("litellm.acompletion", fake_acompletion):
        coro = handler.response_api_handler(
            model="gpt-4o",
            input="hello",
            responses_api_request={},
            custom_llm_provider="openai",
            _is_async=True,
        )
        with pytest.raises(_StopForwarding):
            await coro

    assert captured.get("_skip_responses_api_bridge") is True


@pytest.mark.asyncio
async def test_completion_bridge_inherits_chat_deadline_enforcement():
    """The responses-api completion-bridge path delegates to litellm.acompletion under the
    hood; it must raise DeadlineExceeded via the exact same seam Task 14 added, with no
    bridge-specific deadline code."""
    from unittest.mock import AsyncMock, patch

    import litellm
    from litellm.litellm_core_utils.asyncio_deadline import DeadlineExceeded
    from litellm.responses.litellm_completion_transformation.handler import (
        LiteLLMCompletionTransformationHandler,
    )

    with patch(
        "litellm.main.OpenAIChatCompletion.acompletion",
        new=AsyncMock(side_effect=DeadlineExceeded("simulated")),
    ):
        # Deviation from plan: acompletion()'s exception_type() maps DeadlineExceeded to the
        # public litellm.Timeout (Task 8a), same as Task 16 -- so the bridge surfaces Timeout.
        with pytest.raises(litellm.Timeout) as exc_info:
            await LiteLLMCompletionTransformationHandler().async_response_api_handler(
                litellm_completion_request={
                    "model": "github_copilot/gpt-4",
                    "messages": [{"role": "user", "content": "hi"}],
                    "http_client": {"total_timeout": 1.0},
                },
                request_input="hi",
                responses_api_request={},
            )
    assert "AsyncioDeadlineExceeded" in str(exc_info.value)
