"""Regression: http_client config must never reach the provider-boundary call (and thus the
outbound wire body) for the github_copilot chat path. completion() pops http_client in its
timeout-resolution block (Task 13) and all_litellm_params excludes it (Task 6), before the
provider handler is dispatched.

Note (deviation from plan Task 17): the plan patched AsyncHTTPHandler.post, but the
github_copilot chat path runs through the OpenAI SDK transport (OpenAIChatCompletion), which
AsyncHTTPHandler.post never intercepts (captures 0 bodies). The provider-boundary handler
`litellm.main.openai_chat_completions.completion` -- the same choke point Task 13's tests patch
-- is where http_client must be absent; leak-prevention happens before this dispatch and is
provider-agnostic."""

from unittest.mock import patch

import pytest

import litellm


@pytest.mark.asyncio
async def test_http_client_config_does_not_leak_into_chat_provider_call():
    captured_kwargs = []

    def _capture(*args, **kwargs):
        captured_kwargs.append(kwargs)
        return {"choices": [{"message": {"content": "ok"}}]}

    with patch("litellm.main.openai_chat_completions.completion", side_effect=_capture):
        await litellm.acompletion(
            model="github_copilot/gpt-4",
            messages=[{"role": "user", "content": "hi"}],
            http_client={"connect_timeout": 2.0, "total_timeout": 30.0},
        )

    assert len(captured_kwargs) == 1
    kwargs = captured_kwargs[0]
    assert "http_client" not in kwargs
    # http_client must not have leaked into optional_params either (the provider-visible body)
    assert "http_client" not in (kwargs.get("optional_params") or {})
