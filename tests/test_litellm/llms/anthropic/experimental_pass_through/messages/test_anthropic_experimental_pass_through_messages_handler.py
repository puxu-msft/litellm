import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath("../../../../.."))

from unittest.mock import AsyncMock, MagicMock, patch

from litellm.anthropic_interface import messages
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
from litellm.types.utils import Delta, ModelResponse, StreamingChoices


def test_anthropic_experimental_pass_through_messages_handler():
    """
    Test that api key is passed to litellm.responses for OpenAI models.
    OpenAI and Azure models are routed directly to the Responses API.
    """
    from litellm.llms.anthropic.experimental_pass_through.messages.handler import (
        anthropic_messages_handler,
    )

    with patch("litellm.responses", return_value="test-response") as mock_responses:
        try:
            anthropic_messages_handler(
                max_tokens=100,
                messages=[{"role": "user", "content": "Hello, how are you?"}],
                model="openai/claude-3-5-sonnet-20240620",
                api_key="test-api-key",
            )
        except (ValueError, TypeError, AttributeError) as e:
            print(f"Error: {e}")
        mock_responses.assert_called_once()
        assert mock_responses.call_args.kwargs["api_key"] == "test-api-key"


def test_anthropic_experimental_pass_through_messages_handler_dynamic_api_key_and_api_base_and_custom_values():
    """
    Test that api key, api base, and extra kwargs are forwarded to litellm.completion for Azure models.
    Azure models are routed through chat/completions (not the Responses API).
    """
    from litellm.llms.anthropic.experimental_pass_through.messages.handler import (
        anthropic_messages_handler,
    )

    with patch("litellm.completion", return_value=MagicMock()) as mock_completion:
        try:
            anthropic_messages_handler(
                max_tokens=100,
                messages=[{"role": "user", "content": "Hello, how are you?"}],
                model="azure/o1",
                api_key="test-api-key",
                api_base="test-api-base",
                custom_key="custom_value",
            )
        except (ValueError, TypeError, AttributeError) as e:
            print(f"Error: {e}")
        mock_completion.assert_called_once()
        assert mock_completion.call_args.kwargs["api_key"] == "test-api-key"
        assert mock_completion.call_args.kwargs["api_base"] == "test-api-base"
        assert mock_completion.call_args.kwargs["custom_key"] == "custom_value"


@pytest.mark.asyncio
async def test_anthropic_messages_sanitizes_empty_text_blocks_before_dispatch():
    """Regression test for #22930.  The unified /v1/messages path must
    strip empty text blocks before forwarding, otherwise Anthropic
    returns 400 "text content blocks must be non-empty"."""
    from litellm.llms.anthropic.experimental_pass_through.messages import handler

    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": ""},
                {"type": "tool_use", "id": "t", "name": "B", "input": {}},
            ],
        }
    ]
    captured = {}

    def fake_handler(*args, **kwargs):
        captured["messages"] = kwargs.get("messages")
        return "stub"

    fake_loop = MagicMock()
    fake_loop.run_in_executor = lambda _e, func: _async_return(func())

    with (
        patch.object(handler, "anthropic_messages_handler", side_effect=fake_handler),
        patch("asyncio.get_event_loop", return_value=fake_loop),
    ):
        await handler.anthropic_messages(
            max_tokens=100,
            messages=msgs,
            model="anthropic/claude-sonnet-4-5-20250929",
            custom_llm_provider="anthropic",
            api_key="k",
        )

    assert [b["type"] for b in captured["messages"][0]["content"]] == ["tool_use"]
    assert len(msgs[0]["content"]) == 2  # caller untouched


@pytest.mark.asyncio
async def test_anthropic_messages_sanitizes_tool_use_ids_before_dispatch():
    from litellm.llms.anthropic.experimental_pass_through.messages import handler

    msgs = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "functions.Bash:0",
                    "name": "Bash",
                    "input": {},
                }
            ],
        }
    ]
    captured = {}

    def fake_handler(*args, **kwargs):
        captured["messages"] = kwargs.get("messages")
        return "stub"

    fake_loop = MagicMock()
    fake_loop.run_in_executor = lambda _e, func: _async_return(func())

    with (
        patch.object(handler, "anthropic_messages_handler", side_effect=fake_handler),
        patch("asyncio.get_event_loop", return_value=fake_loop),
    ):
        await handler.anthropic_messages(
            max_tokens=100,
            messages=msgs,
            model="anthropic/claude-sonnet-4-5-20250929",
            custom_llm_provider="anthropic",
            api_key="k",
        )

    assert captured["messages"][0]["content"][0]["id"] == "functions_Bash_0"
    assert msgs[0]["content"][0]["id"] == "functions.Bash:0"


async def _async_return(value):
    return value


def test_anthropic_experimental_pass_through_messages_handler_custom_llm_provider():
    """
    Test that litellm.completion is called when a custom LLM provider is given
    """
    from litellm.llms.anthropic.experimental_pass_through.messages.handler import (
        anthropic_messages_handler,
    )

    with patch("litellm.completion", return_value="test-response") as mock_completion:
        try:
            anthropic_messages_handler(
                max_tokens=100,
                messages=[{"role": "user", "content": "Hello, how are you?"}],
                model="my-custom-model",
                custom_llm_provider="my-custom-llm",
                api_key="test-api-key",
            )
        except (ValueError, TypeError, AttributeError) as e:
            print(f"Error: {e}")

        # Assert that litellm.completion was called when using a custom LLM provider
        mock_completion.assert_called_once()

        # Verify that the custom provider was passed through
        call_kwargs = mock_completion.call_args.kwargs
        assert call_kwargs["custom_llm_provider"] == "my-custom-llm"
        assert call_kwargs["model"] == "my-custom-llm/my-custom-model"
        assert call_kwargs["api_key"] == "test-api-key"


@pytest.mark.asyncio
async def test_bedrock_converse_budget_tokens_preserved():
    """
    Test that budget_tokens value in thinking parameter is correctly passed to Bedrock Converse API
    when using messages.acreate with bedrock/converse model.

    The bug was that the messages -> completion adapter was converting thinking to reasoning_effort
    and losing the original budget_tokens value, causing it to use the default (128) instead.
    """
    # Mock litellm.acompletion which is called internally by anthropic_messages_handler
    mock_response = ModelResponse(
        id="test-id",
        model="bedrock/converse/us.anthropic.claude-sonnet-4-20250514-v1:0",
        choices=[
            {
                "index": 0,
                "message": {"role": "assistant", "content": "4"},
                "finish_reason": "stop",
            }
        ],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )

    with patch("litellm.acompletion", new_callable=AsyncMock) as mock_acompletion:
        mock_acompletion.return_value = mock_response

        try:
            await messages.acreate(
                max_tokens=1024,
                messages=[{"role": "user", "content": "What is 2+2?"}],
                model="bedrock/converse/us.anthropic.claude-sonnet-4-20250514-v1:0",
                thinking={"budget_tokens": 1024, "type": "enabled"},
            )
        except (ValueError, TypeError, AttributeError):
            pass  # Expected due to response format conversion

        mock_acompletion.assert_called_once()

        call_kwargs = mock_acompletion.call_args.kwargs
        print(
            "acompletion call kwargs: ", json.dumps(call_kwargs, indent=4, default=str)
        )

        # Verify thinking parameter is passed through with budget_tokens preserved
        thinking_param = call_kwargs.get("thinking")
        assert (
            thinking_param is not None
        ), "thinking parameter should be passed to acompletion"
        assert (
            thinking_param.get("type") == "enabled"
        ), "thinking.type should be 'enabled'"
        assert (
            thinking_param.get("budget_tokens") == 1024
        ), f"thinking.budget_tokens should be 1024, but got {thinking_param.get('budget_tokens')}"


def test_openai_model_with_thinking_converts_to_reasoning():
    """
    Test that when using an OpenAI model with thinking parameter, the thinking is
    converted to a Responses API `reasoning` param (NOT passed as thinking).

    OpenAI models are routed directly to the Responses API, so we verify that
    litellm.responses() is called with `reasoning` properly set.
    """
    from litellm.llms.anthropic.experimental_pass_through.messages.handler import (
        anthropic_messages_handler,
    )

    with patch("litellm.responses", return_value="test-response") as mock_responses:
        try:
            anthropic_messages_handler(
                max_tokens=1024,
                messages=[{"role": "user", "content": "What is 2+2?"}],
                model="openai/gpt-5.2",
                api_key="test-api-key",
                thinking={"type": "enabled", "budget_tokens": 1024},
            )
        except (ValueError, TypeError, AttributeError) as e:
            print(f"Error: {e}")

        mock_responses.assert_called_once()

        call_kwargs = mock_responses.call_args.kwargs

        # Verify reasoning is set (converted from thinking)
        assert (
            "reasoning" in call_kwargs
        ), "reasoning should be passed to litellm.responses"

        # budget_tokens=1024 -> effort="low" (at the LOW budget threshold)
        # reasoning_auto_summary is False by default, so no summary key
        expected_reasoning = {"effort": "low"}
        assert call_kwargs["reasoning"] == expected_reasoning, (
            f"reasoning should be {expected_reasoning} for budget_tokens=1024, "
            f"got {call_kwargs.get('reasoning')}"
        )
        assert "summary" not in call_kwargs["reasoning"]

        # Verify thinking is NOT passed directly to the Responses API
        assert (
            "thinking" not in call_kwargs
        ), "thinking should NOT be passed directly to litellm.responses"


class TestThinkingParameterTransformation:
    """Core tests for thinking parameter transformation logic."""

    def test_claude_model_preserves_thinking_with_budget_tokens(self):
        """Test that Claude models get thinking parameter passed through with exact budget_tokens."""
        from litellm.llms.anthropic.experimental_pass_through.adapters.transformation import (
            LiteLLMAnthropicMessagesAdapter,
        )

        thinking = {"type": "enabled", "budget_tokens": 5000}
        result = LiteLLMAnthropicMessagesAdapter.translate_thinking_for_model(
            thinking=thinking,
            model="bedrock/converse/us.anthropic.claude-sonnet-4-20250514-v1:0",
        )

        assert result == {"thinking": thinking}
        assert result["thinking"]["budget_tokens"] == 5000

    def test_non_claude_model_converts_thinking_to_reasoning_effort(self):
        """Test that non-Claude models convert thinking to reasoning_effort."""
        from litellm.llms.anthropic.experimental_pass_through.adapters.transformation import (
            LiteLLMAnthropicMessagesAdapter,
        )

        thinking = {"type": "enabled", "budget_tokens": 1024}
        result = LiteLLMAnthropicMessagesAdapter.translate_thinking_for_model(
            thinking=thinking,
            model="openai/gpt-5.2",
        )

        # reasoning_auto_summary is False by default, so no summary key
        assert result == {"reasoning_effort": "low"}
        assert "thinking" not in result
        assert "summary" not in str(result["reasoning_effort"])

    def test_translate_thinking_for_model_summary_when_enabled(self):
        """When reasoning_auto_summary is True, summary='detailed' is injected."""
        import litellm
        from litellm.llms.anthropic.experimental_pass_through.adapters.transformation import (
            LiteLLMAnthropicMessagesAdapter,
        )

        original = litellm.reasoning_auto_summary
        try:
            litellm.reasoning_auto_summary = True
            thinking = {"type": "enabled", "budget_tokens": 5000}
            result = LiteLLMAnthropicMessagesAdapter.translate_thinking_for_model(
                thinking=thinking,
                model="openai/gpt-5.2",
            )
            assert result == {
                "reasoning_effort": {"effort": "high", "summary": "detailed"}
            }
        finally:
            litellm.reasoning_auto_summary = original

    def test_translate_thinking_for_model_preserves_user_summary(self):
        """User-provided summary is always preserved regardless of flag."""
        from litellm.llms.anthropic.experimental_pass_through.adapters.transformation import (
            LiteLLMAnthropicMessagesAdapter,
        )

        thinking = {"type": "enabled", "budget_tokens": 10000, "summary": "concise"}
        result = LiteLLMAnthropicMessagesAdapter.translate_thinking_for_model(
            thinking=thinking,
            model="openai/gpt-5.2",
        )
        assert result == {"reasoning_effort": {"effort": "high", "summary": "concise"}}


class TestThinkingSummaryPreservation:
    """Tests for thinking.summary preservation and reasoning_auto_summary flag."""

    def test_thinking_summary_concise_preserved_for_openai(self):
        """User-provided summary='concise' should not be replaced with 'detailed'."""
        from litellm.llms.anthropic.experimental_pass_through.adapters.handler import (
            LiteLLMMessagesToCompletionTransformationHandler,
        )

        thinking = {"type": "enabled", "budget_tokens": 5000, "summary": "concise"}
        completion_kwargs = {"model": "openai/gpt-5.1", "reasoning_effort": "medium"}
        LiteLLMMessagesToCompletionTransformationHandler._route_openai_thinking_to_responses_api_if_needed(
            completion_kwargs, thinking=thinking
        )
        assert completion_kwargs["reasoning_effort"] == {
            "effort": "medium",
            "summary": "concise",
        }

    def test_thinking_summary_auto_preserved_for_openai(self):
        """User-provided summary='auto' should be preserved."""
        from litellm.llms.anthropic.experimental_pass_through.adapters.handler import (
            LiteLLMMessagesToCompletionTransformationHandler,
        )

        thinking = {"type": "enabled", "budget_tokens": 10000, "summary": "auto"}
        completion_kwargs = {"model": "openai/gpt-5.1", "reasoning_effort": "high"}
        LiteLLMMessagesToCompletionTransformationHandler._route_openai_thinking_to_responses_api_if_needed(
            completion_kwargs, thinking=thinking
        )
        assert completion_kwargs["reasoning_effort"] == {
            "effort": "high",
            "summary": "auto",
        }

    def test_summary_added_when_auto_summary_enabled(self):
        """When reasoning_auto_summary is True, summary='detailed' is added."""
        import litellm
        from litellm.llms.anthropic.experimental_pass_through.adapters.handler import (
            LiteLLMMessagesToCompletionTransformationHandler,
        )

        original = litellm.reasoning_auto_summary
        try:
            litellm.reasoning_auto_summary = True
            completion_kwargs = {
                "model": "responses/gpt-5.2",
                "custom_llm_provider": "openai",
                "reasoning_effort": "medium",
            }
            LiteLLMMessagesToCompletionTransformationHandler._route_openai_thinking_to_responses_api_if_needed(
                completion_kwargs, thinking={"type": "enabled", "budget_tokens": 5000}
            )
            assert completion_kwargs["reasoning_effort"] == {
                "effort": "medium",
                "summary": "detailed",
            }
        finally:
            litellm.reasoning_auto_summary = original

    def test_no_summary_by_default_string_reasoning(self):
        """By default (reasoning_auto_summary=False), summary is not added for string reasoning_effort."""
        import litellm
        from litellm.llms.anthropic.experimental_pass_through.adapters.handler import (
            LiteLLMMessagesToCompletionTransformationHandler,
        )

        original = litellm.reasoning_auto_summary
        try:
            litellm.reasoning_auto_summary = False
            completion_kwargs = {
                "model": "responses/gpt-5.2",
                "custom_llm_provider": "openai",
                "reasoning_effort": "high",
            }
            LiteLLMMessagesToCompletionTransformationHandler._route_openai_thinking_to_responses_api_if_needed(
                completion_kwargs, thinking={"type": "enabled", "budget_tokens": 10000}
            )
            assert completion_kwargs["reasoning_effort"] == {"effort": "high"}
            assert "summary" not in completion_kwargs["reasoning_effort"]
        finally:
            litellm.reasoning_auto_summary = original

    def test_no_summary_by_default_dict_reasoning(self):
        """By default (reasoning_auto_summary=False), summary is not injected into dict reasoning_effort."""
        import litellm
        from litellm.llms.anthropic.experimental_pass_through.adapters.handler import (
            LiteLLMMessagesToCompletionTransformationHandler,
        )

        original = litellm.reasoning_auto_summary
        try:
            litellm.reasoning_auto_summary = False
            completion_kwargs = {
                "model": "responses/gpt-5.2",
                "custom_llm_provider": "openai",
                "reasoning_effort": {"effort": "medium"},
            }
            LiteLLMMessagesToCompletionTransformationHandler._route_openai_thinking_to_responses_api_if_needed(
                completion_kwargs, thinking={"type": "enabled", "budget_tokens": 5000}
            )
            assert completion_kwargs["reasoning_effort"] == {"effort": "medium"}
            assert "summary" not in completion_kwargs["reasoning_effort"]
        finally:
            litellm.reasoning_auto_summary = original

    def test_summary_added_when_env_var_set(self):
        """When LITELLM_REASONING_AUTO_SUMMARY env var is true, summary is added."""
        import litellm
        from litellm.llms.anthropic.experimental_pass_through.adapters.handler import (
            LiteLLMMessagesToCompletionTransformationHandler,
        )

        original = litellm.reasoning_auto_summary
        try:
            litellm.reasoning_auto_summary = False
            os.environ["LITELLM_REASONING_AUTO_SUMMARY"] = "true"
            completion_kwargs = {
                "model": "responses/gpt-5.2",
                "custom_llm_provider": "openai",
                "reasoning_effort": "high",
            }
            LiteLLMMessagesToCompletionTransformationHandler._route_openai_thinking_to_responses_api_if_needed(
                completion_kwargs, thinking={"type": "enabled", "budget_tokens": 10000}
            )
            assert completion_kwargs["reasoning_effort"] == {
                "effort": "high",
                "summary": "detailed",
            }
        finally:
            litellm.reasoning_auto_summary = original
            os.environ.pop("LITELLM_REASONING_AUTO_SUMMARY", None)

    def test_user_provided_summary_preserved_even_when_flag_off(self):
        """When user already set summary in dict reasoning_effort, it's preserved regardless of flag."""
        import litellm
        from litellm.llms.anthropic.experimental_pass_through.adapters.handler import (
            LiteLLMMessagesToCompletionTransformationHandler,
        )

        original = litellm.reasoning_auto_summary
        try:
            litellm.reasoning_auto_summary = False
            completion_kwargs = {
                "model": "responses/gpt-5.2",
                "custom_llm_provider": "openai",
                "reasoning_effort": {"effort": "high", "summary": "concise"},
            }
            LiteLLMMessagesToCompletionTransformationHandler._route_openai_thinking_to_responses_api_if_needed(
                completion_kwargs, thinking={"type": "enabled", "budget_tokens": 10000}
            )
            assert completion_kwargs["reasoning_effort"]["summary"] == "concise"
        finally:
            litellm.reasoning_auto_summary = original

    def test_openai_model_with_thinking_summary_end_to_end(self):
        """End-to-end: anthropic_messages_handler should preserve thinking.summary for OpenAI models."""
        from litellm.llms.anthropic.experimental_pass_through.messages.handler import (
            anthropic_messages_handler,
        )

        with patch("litellm.responses", return_value="test-response") as mock_responses:
            try:
                anthropic_messages_handler(
                    max_tokens=1024,
                    messages=[{"role": "user", "content": "What is 2+2?"}],
                    model="openai/gpt-5.2",
                    api_key="test-api-key",
                    thinking={
                        "type": "enabled",
                        "budget_tokens": 5000,
                        "summary": "concise",
                    },
                )
            except (ValueError, TypeError, AttributeError):
                pass

            mock_responses.assert_called_once()
            call_kwargs = mock_responses.call_args.kwargs
            reasoning = call_kwargs["reasoning"]
            assert (
                reasoning["summary"] == "concise"
            ), f"Expected summary='concise', got summary='{reasoning.get('summary')}'"

    def test_responses_adapter_preserves_summary(self):
        """translate_thinking_to_reasoning should include summary when user provides it."""
        from litellm.llms.anthropic.experimental_pass_through.responses_adapters.transformation import (
            LiteLLMAnthropicToResponsesAPIAdapter,
        )

        thinking = {"type": "enabled", "budget_tokens": 5000, "summary": "concise"}
        result = LiteLLMAnthropicToResponsesAPIAdapter.translate_thinking_to_reasoning(
            thinking
        )
        assert result == {"effort": "high", "summary": "concise"}

    def test_responses_adapter_no_summary_by_default(self):
        """translate_thinking_to_reasoning should not include summary by default (opt-in)."""
        import litellm
        from litellm.llms.anthropic.experimental_pass_through.responses_adapters.transformation import (
            LiteLLMAnthropicToResponsesAPIAdapter,
        )

        original = litellm.reasoning_auto_summary
        try:
            litellm.reasoning_auto_summary = False
            thinking = {"type": "enabled", "budget_tokens": 5000}
            result = (
                LiteLLMAnthropicToResponsesAPIAdapter.translate_thinking_to_reasoning(
                    thinking
                )
            )
            assert result == {"effort": "high"}
            assert result is not None and "summary" not in result
        finally:
            litellm.reasoning_auto_summary = original

    def test_translate_thinking_for_model_preserves_summary(self):
        """translate_thinking_for_model should include summary in reasoning_effort dict when user provides it."""
        from litellm.llms.anthropic.experimental_pass_through.adapters.transformation import (
            LiteLLMAnthropicMessagesAdapter,
        )

        thinking = {"type": "enabled", "budget_tokens": 5000, "summary": "concise"}
        result = LiteLLMAnthropicMessagesAdapter.translate_thinking_for_model(
            thinking=thinking,
            model="openai/gpt-5.2",
        )
        assert result == {
            "reasoning_effort": {"effort": "high", "summary": "concise"}
        }


# ---------------------------------------------------------------------------
# Parity tests: redundant empty-text-block sanitization scan removal.
# The async wrapper sanitizes once and tells the handler to skip its second
# (redundant) full-messages scan; the sync entry point still sanitizes.
# ---------------------------------------------------------------------------


def _empty_block_msgs():
    return [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "   "},  # whitespace-only -> stripped
                {"type": "tool_use", "id": "t", "name": "B", "input": {}},
            ],
        }
    ]


def test_handler_strips_when_no_presanitized_flag():
    """Sync entry point (no async wrapper): handler must still sanitize."""
    from litellm.llms.anthropic.experimental_pass_through.messages import handler

    with patch.object(
        handler,
        "strip_empty_text_blocks_from_anthropic_messages",
        wraps=handler.strip_empty_text_blocks_from_anthropic_messages,
    ) as spy:
        result = handler.anthropic_messages_handler(
            max_tokens=10,
            messages=_empty_block_msgs(),
            model="anthropic/claude-3-5-sonnet-20241022",
            custom_llm_provider="anthropic",
            mock_response="hi there",
        )
    assert spy.call_count == 1  # sanitized exactly once here
    assert result is not None


def test_handler_skips_strip_when_presanitized():
    """Async wrapper already sanitized -> handler must NOT rescan."""
    from litellm.llms.anthropic.experimental_pass_through.messages import handler

    with patch.object(
        handler,
        "strip_empty_text_blocks_from_anthropic_messages",
        wraps=handler.strip_empty_text_blocks_from_anthropic_messages,
    ) as spy:
        result = handler.anthropic_messages_handler(
            max_tokens=10,
            messages=_empty_block_msgs(),
            model="anthropic/claude-3-5-sonnet-20241022",
            custom_llm_provider="anthropic",
            mock_response="hi there",
            _litellm_messages_presanitized=True,
        )
    assert spy.call_count == 0  # skipped the redundant scan
    assert result is not None


def test_presanitized_flag_not_leaked_to_provider_params():
    """The private sentinel must be popped, never forwarded as a request param."""
    from litellm.llms.anthropic.experimental_pass_through.messages import handler

    captured = {}

    def fake_base_handler(*args, **kwargs):
        captured.update(kwargs)
        captured["optional"] = kwargs.get(
            "anthropic_messages_optional_request_params", {}
        )
        return "stub"

    with patch.object(
        handler.base_llm_http_handler,
        "anthropic_messages_handler",
        side_effect=fake_base_handler,
    ):
        handler.anthropic_messages_handler(
            max_tokens=10,
            messages=[{"role": "user", "content": "hi"}],
            model="anthropic/claude-3-5-sonnet-20241022",
            custom_llm_provider="anthropic",
            _litellm_messages_presanitized=True,
        )

    assert "_litellm_messages_presanitized" not in captured.get("optional", {})
    assert "_litellm_messages_presanitized" not in captured.get("kwargs", {})


@pytest.mark.asyncio
async def test_async_wrapper_sets_presanitized_and_sanitizes_once():
    """End-to-end: wrapper sanitizes (once) AND signals the handler to skip."""
    from litellm.llms.anthropic.experimental_pass_through.messages import handler

    captured = {}

    def fake_handler(*args, **kwargs):
        captured["messages"] = kwargs.get("messages")
        captured["presanitized"] = kwargs.get("_litellm_messages_presanitized")
        return "stub"

    fake_loop = MagicMock()
    fake_loop.run_in_executor = lambda _e, func: _async_return(func())

    with (
        patch.object(handler, "anthropic_messages_handler", side_effect=fake_handler),
        patch("asyncio.get_event_loop", return_value=fake_loop),
        patch.object(
            handler,
            "strip_empty_text_blocks_from_anthropic_messages",
            wraps=handler.strip_empty_text_blocks_from_anthropic_messages,
        ) as spy,
    ):
        await handler.anthropic_messages(
            max_tokens=100,
            messages=_empty_block_msgs(),
            model="anthropic/claude-sonnet-4-5-20250929",
            custom_llm_provider="anthropic",
            api_key="k",
        )

    # Wrapper stripped exactly once (the handler is faked, so its skipped
    # call never runs anyway -- the point is the wrapper still sanitizes).
    assert spy.call_count == 1
    assert captured["presanitized"] is True
    assert [b["type"] for b in captured["messages"][0]["content"]] == ["tool_use"]


def _gate_stubs(monkeypatch):
    """Patch the gate's downstream dispatch targets so config selection can be
    observed without making a network call.

    Returns ``(captured, translation_calls)`` where ``captured["config"]`` is the
    provider config handed to the native passthrough path and ``translation_calls``
    counts hits on the Anthropic->OpenAI translation handlers.
    """
    from litellm.llms.anthropic.experimental_pass_through.messages import handler

    captured = {}
    translation_calls = {"count": 0}

    def fake_native(**kwargs):
        captured["config"] = kwargs.get("anthropic_messages_provider_config")
        return "native-passthrough"

    def fake_translation(**kwargs):
        translation_calls["count"] += 1
        return "translated"

    monkeypatch.setattr(handler.base_llm_http_handler, "anthropic_messages_handler", fake_native)
    monkeypatch.setattr(
        handler.LiteLLMMessagesToResponsesAPIHandler,
        "anthropic_messages_handler",
        staticmethod(fake_translation),
    )
    monkeypatch.setattr(
        handler.LiteLLMMessagesToCompletionTransformationHandler,
        "anthropic_messages_handler",
        staticmethod(fake_translation),
    )
    return captured, translation_calls


def test_gate_passthrough_when_supported_endpoints_opts_in(monkeypatch):
    """provider=openai + model_info.supported_endpoints containing /v1/messages
    must route to the native passthrough config, NOT the translation handlers."""
    from litellm.llms.anthropic.experimental_pass_through.messages.handler import (
        anthropic_messages_handler,
    )
    from litellm.llms.openai_like.messages.transformation import (
        OpenAILikeAnthropicMessagesConfig,
    )

    captured, translation_calls = _gate_stubs(monkeypatch)

    result = anthropic_messages_handler(
        max_tokens=100,
        messages=[{"role": "user", "content": "Hello"}],
        model="openai/some-model",
        api_key="sk-test",
        api_base="https://host/v1",
        model_info={"supported_endpoints": ["/v1/chat/completions", "/v1/messages"]},
    )

    assert result == "native-passthrough"
    assert isinstance(captured["config"], OpenAILikeAnthropicMessagesConfig)
    assert translation_calls["count"] == 0


def test_gate_translates_when_supported_endpoints_absent(monkeypatch):
    """Default behavior is unchanged: without the /v1/messages opt-in, an openai
    deployment is translated (Responses API), never passed through natively."""
    from litellm.llms.anthropic.experimental_pass_through.messages.handler import (
        anthropic_messages_handler,
    )

    captured, translation_calls = _gate_stubs(monkeypatch)

    result = anthropic_messages_handler(
        max_tokens=100,
        messages=[{"role": "user", "content": "Hello"}],
        model="openai/some-model",
        api_key="sk-test",
        api_base="https://host/v1",
    )

    assert result == "translated"
    assert translation_calls["count"] == 1
    assert "config" not in captured


def test_gate_passthrough_skipped_when_only_chat_completions_supported(monkeypatch):
    """A deployment that lists only /v1/chat/completions is still translated;
    the opt-in is specifically the /v1/messages entry."""
    from litellm.llms.anthropic.experimental_pass_through.messages.handler import (
        anthropic_messages_handler,
    )

    captured, translation_calls = _gate_stubs(monkeypatch)

    result = anthropic_messages_handler(
        max_tokens=100,
        messages=[{"role": "user", "content": "Hello"}],
        model="openai/some-model",
        api_key="sk-test",
        api_base="https://host/v1",
        model_info={"supported_endpoints": ["/v1/chat/completions"]},
    )

    assert result == "translated"
    assert translation_calls["count"] == 1
    assert "config" not in captured


class TestShouldRouteToResponsesApiCopilot:
    def _fn(self):
        from litellm.llms.anthropic.experimental_pass_through.messages.handler import (
            _should_route_to_responses_api,
        )

        return _should_route_to_responses_api

    def test_openai_still_responses(self):
        assert self._fn()("openai", model="gpt-5.5", model_info=None) is True

    def test_copilot_responses_only(self, monkeypatch):
        import litellm.llms.github_copilot.model_capabilities as mc

        monkeypatch.setattr(mc, "route_supports_responses", lambda *a, **k: True)
        assert self._fn()("github_copilot", model="gpt-5.5", model_info={}) is True

    def test_copilot_chat_only(self, monkeypatch):
        import litellm.llms.github_copilot.model_capabilities as mc

        monkeypatch.setattr(mc, "route_supports_responses", lambda *a, **k: False)
        assert self._fn()("github_copilot", model="gpt-4o", model_info={}) is False

    def test_global_flag_forces_chat(self, monkeypatch):
        import litellm

        monkeypatch.setattr(litellm, "use_chat_completions_url_for_anthropic_messages", True)
        assert self._fn()("github_copilot", model="gpt-5.5", model_info={}) is False
        assert self._fn()("openai", model="gpt-5.5", model_info=None) is False


class TestCopilotMergedStateRouting:
    """Drive anthropic_messages_handler end-to-end for github_copilot and assert
    each model class reaches exactly one terminal (native messages / responses
    bridge / chat bridge)."""

    def _run(self, monkeypatch, model, supports_messages, supports_responses):
        from unittest.mock import patch

        import litellm.llms.github_copilot.model_capabilities as mc
        from litellm.llms.anthropic.experimental_pass_through.messages import handler as h

        monkeypatch.setattr(mc, "route_supports_messages", lambda *a, **k: supports_messages)
        monkeypatch.setattr(mc, "route_supports_responses", lambda *a, **k: supports_responses)
        monkeypatch.setattr(mc, "copilot_api_base", lambda *a, **k: None)

        common = dict(
            max_tokens=16,
            messages=[{"role": "user", "content": "hi"}],
            model=model,
            api_key="k",
        )
        with patch.object(h.base_llm_http_handler, "anthropic_messages_handler", return_value="MESSAGES") as m_msg, \
             patch.object(h.LiteLLMMessagesToResponsesAPIHandler, "anthropic_messages_handler", return_value="RESPONSES") as m_resp, \
             patch.object(h.LiteLLMMessagesToCompletionTransformationHandler, "anthropic_messages_handler", return_value="CHAT") as m_chat:
            h.anthropic_messages_handler(**common)
        return (m_msg.called, m_resp.called, m_chat.called)

    def test_messages_capable_model_hits_native_messages(self, monkeypatch):
        called = self._run(monkeypatch, "github_copilot/claude-opus-4.8", True, False)
        assert called == (True, False, False)

    def test_responses_only_model_hits_responses_bridge(self, monkeypatch):
        called = self._run(monkeypatch, "github_copilot/gpt-5.6-sol", False, True)
        assert called == (False, True, False)

    def test_chat_only_model_hits_chat_bridge(self, monkeypatch):
        called = self._run(monkeypatch, "github_copilot/gpt-4o", False, False)
        assert called == (False, False, True)


@pytest.mark.asyncio
async def test_anthropic_messages_maps_deadline_exceeded_to_litellm_timeout():
    """Fix E regression: unlike chat/responses, the messages face has NO exception_type()
    wrapper anywhere in its call chain. A raw DeadlineExceeded surfacing from the native
    handler's internal await must be converted to litellm.Timeout right here, or it leaks to
    direct-SDK callers of litellm.anthropic_messages() as an internal exception type."""
    import litellm
    from litellm.litellm_core_utils.asyncio_deadline import DeadlineExceeded
    from litellm.llms.anthropic.experimental_pass_through.messages import handler

    async def _raise_deadline_exceeded():
        raise DeadlineExceeded("simulated total_timeout deadline exceeded")

    def fake_handler(*args, **kwargs):
        return _raise_deadline_exceeded()

    fake_loop = MagicMock()
    fake_loop.run_in_executor = lambda _e, func: _async_return(func())

    with (
        patch.object(handler, "anthropic_messages_handler", side_effect=fake_handler),
        patch("asyncio.get_event_loop", return_value=fake_loop),
    ):
        with pytest.raises(litellm.Timeout):
            await handler.anthropic_messages(
                max_tokens=100,
                messages=[{"role": "user", "content": "hi"}],
                model="anthropic/claude-sonnet-4-5-20250929",
                custom_llm_provider="anthropic",
                api_key="k",
            )


@pytest.mark.asyncio
async def test_anthropic_messages_establishes_http_client_deadline():
    import litellm

    captured = []
    original = litellm.litellm_core_utils.litellm_logging.Logging.set_http_client_deadline

    def _capture(self, deadline):
        captured.append(deadline)
        return original(self, deadline)

    with (
        patch(
            "litellm.litellm_core_utils.litellm_logging.Logging.set_http_client_deadline",
            new=_capture,
            autospec=False,
        ),
        patch(
            "litellm.llms.anthropic.experimental_pass_through.messages.handler.anthropic_messages_handler",
            return_value=MagicMock(),
        ),
    ):
        await litellm.anthropic_messages(
            max_tokens=100,
            messages=[{"role": "user", "content": "hi"}],
            model="github_copilot/claude-3-haiku",
            http_client={"total_timeout": 20.0},
        )

    assert len(captured) == 1
    assert captured[0] is not None


@pytest.mark.asyncio
async def test_async_post_anthropic_messages_passes_explicit_timeout():
    """Regression: the wire-level post() previously had no timeout= kwarg at all, so per-request
    http_client config (and even the legacy per-face default) was silently ignored."""
    import httpx

    from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler

    handler = BaseLLMHTTPHandler()
    captured_kwargs = []

    async def _fake_post(**kwargs):
        captured_kwargs.append(kwargs)
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        return response

    mock_client = MagicMock()
    mock_client.post = AsyncMock(side_effect=_fake_post)

    await handler._async_post_anthropic_messages_with_http_error_retry(
        async_httpx_client=mock_client,
        request_url="https://api.example.com/v1/messages",
        headers={},
        signed_json_body=None,
        request_body={"model": "claude-3-haiku", "messages": []},
        stream=False,
        logging_obj=MagicMock(http_client_deadline=None),
        provider_config=MagicMock(max_retry_on_anthropic_messages_http_error=1),
        litellm_params=MagicMock(http_client=None, timeout=None),
        api_key="fake-key",
        model="claude-3-haiku",
        timeout=httpx.Timeout(600.0, connect=5.0),
    )

    assert len(captured_kwargs) == 1
    assert "timeout" in captured_kwargs[0]
    assert isinstance(captured_kwargs[0]["timeout"], httpx.Timeout)


@pytest.mark.asyncio
async def test_async_anthropic_messages_handler_merges_global_http_client_with_deployment_override():
    """Regression for review finding #8: this call site previously fed resolve_http_client_timeout
    only litellm_params.http_client, ignoring any global litellm.http_client setting."""
    import httpx

    import litellm
    from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler
    from litellm.types.router import GenericLiteLLMParams

    handler = BaseLLMHTTPHandler()
    captured_kwargs = []

    async def _fake_post(**kwargs):
        captured_kwargs.append(kwargs)
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        return response

    mock_client = MagicMock()
    mock_client.post = AsyncMock(side_effect=_fake_post)

    # provider_config methods the handler calls before the post(), returning correctly-shaped
    # values so request construction reaches the wire call this test inspects.
    provider_config = MagicMock()
    provider_config.validate_anthropic_messages_environment.return_value = ({}, "https://api.example.com")
    provider_config.should_filter_anthropic_beta_headers.return_value = False
    provider_config.transform_anthropic_messages_request.return_value = {"model": "claude-3-haiku", "messages": []}
    provider_config.get_complete_url.return_value = "https://api.example.com/v1/messages"
    provider_config.sign_request.return_value = ({}, None)
    provider_config.max_retry_on_anthropic_messages_http_error = 1

    original_global_http_client = litellm.http_client
    litellm.http_client = {"connect_timeout": 4.0, "pool_timeout": 12.0}
    try:
        with patch(
            "litellm.llms.custom_httpx.llm_http_handler.get_async_httpx_client",
            return_value=mock_client,
        ):
            try:
                await handler.async_anthropic_messages_handler(
                    model="claude-3-haiku",
                    messages=[{"role": "user", "content": "hi"}],
                    anthropic_messages_provider_config=provider_config,
                    anthropic_messages_optional_request_params={},
                    custom_llm_provider="anthropic",
                    litellm_params=GenericLiteLLMParams(http_client={"connect_timeout": 2.0}),
                    logging_obj=MagicMock(http_client_deadline=None),
                )
            except Exception:
                pass  # request construction beyond the post() call is out of scope here
    finally:
        litellm.http_client = original_global_http_client

    assert len(captured_kwargs) == 1
    resolved = captured_kwargs[0]["timeout"]
    assert resolved.connect == 2.0  # deployment overrides global
    assert resolved.pool == 12.0  # falls back to global
