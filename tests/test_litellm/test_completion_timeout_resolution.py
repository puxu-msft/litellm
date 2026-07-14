"""Unit tests for litellm.litellm_core_utils.completion_timeout.CompletionTimeout."""

import os
import sys

import httpx

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

from litellm.litellm_core_utils.completion_timeout import CompletionTimeout
from litellm.utils import supports_httpx_timeout


def test_explicit_timeout_wins():
    assert (
        CompletionTimeout.resolve(
            12.5,
            {"timeout": 99.0, "request_timeout": 88.0},
            "openai",
            global_timeout=None,
            supports_httpx_timeout=supports_httpx_timeout,
        )
        == 12.5
    )


def test_kwargs_timeout_when_param_none():
    assert (
        CompletionTimeout.resolve(
            None,
            {"timeout": 21.0},
            "azure_ai",
            global_timeout=None,
            supports_httpx_timeout=supports_httpx_timeout,
        )
        == 21.0
    )


def test_request_timeout_alias_in_kwargs():
    assert (
        CompletionTimeout.resolve(
            None,
            {"request_timeout": 33.0},
            "bedrock",
            global_timeout=None,
            supports_httpx_timeout=supports_httpx_timeout,
        )
        == 33.0
    )


def test_global_timeout_from_litellm_settings():
    assert (
        CompletionTimeout.resolve(
            None,
            {},
            "vertex_ai",
            global_timeout=360.0,
            supports_httpx_timeout=supports_httpx_timeout,
        )
        == 360.0
    )


def test_explicit_global_timeout_6000_is_preserved():
    """The caller passes the explicitly-configured value (or None); an explicit
    6000 must be honored, not silently coerced to 600."""
    assert (
        CompletionTimeout.resolve(
            None,
            {},
            "openai",
            global_timeout=6000.0,
            supports_httpx_timeout=supports_httpx_timeout,
        )
        == 6000.0
    )


def test_explicit_request_timeout_6000_preserved():
    """Explicit deployment/request timeout must not be truncated by the package sentinel."""
    assert (
        CompletionTimeout.resolve(
            None,
            {"request_timeout": 6000.0},
            "openai",
            global_timeout=None,
            supports_httpx_timeout=supports_httpx_timeout,
        )
        == 6000.0
    )


def test_explicit_model_timeout_6000_preserved():
    assert (
        CompletionTimeout.resolve(
            6000.0,
            {"timeout": 1.0, "request_timeout": 2.0},
            "openai",
            global_timeout=None,
            supports_httpx_timeout=supports_httpx_timeout,
        )
        == 6000.0
    )


def test_fallback_600_when_no_global_timeout():
    assert (
        CompletionTimeout.resolve(
            None,
            {},
            "azure_ai",
            global_timeout=None,
            supports_httpx_timeout=supports_httpx_timeout,
        )
        == 600.0
    )


def test_httpx_timeout_coerced_for_provider_without_httpx_timeout_support():
    t = httpx.Timeout(50.0, connect=2.0)
    out = CompletionTimeout.resolve(
        t,
        {},
        "azure_ai",
        global_timeout=None,
        supports_httpx_timeout=supports_httpx_timeout,
    )
    assert out == 50.0
    assert not isinstance(out, httpx.Timeout)


def test_httpx_timeout_preserved_for_openai():
    t = httpx.Timeout(40.0, connect=5.0)
    out = CompletionTimeout.resolve(
        t,
        {},
        "openai",
        global_timeout=None,
        supports_httpx_timeout=supports_httpx_timeout,
    )
    assert out is t
    assert isinstance(out, httpx.Timeout)


def test_completion_pops_http_client_and_resolves_httpx_timeout():
    from unittest.mock import patch

    import httpx

    import litellm

    captured_timeout = []

    def _fake_provider_call(*args, **kwargs):
        captured_timeout.append(kwargs.get("timeout"))
        return {"choices": [{"message": {"content": "ok"}}]}

    with patch("litellm.main.openai_chat_completions.completion", side_effect=_fake_provider_call):
        litellm.completion(
            model="github_copilot/gpt-4",
            messages=[{"role": "user", "content": "hi"}],
            http_client={"connect_timeout": 2.0, "read_timeout": 9.0},
        )

    assert len(captured_timeout) == 1
    resolved = captured_timeout[0]
    assert isinstance(resolved, httpx.Timeout)
    assert resolved.connect == 2.0
    assert resolved.read == 9.0


def test_completion_does_not_leak_http_client_key_into_optional_params():
    from unittest.mock import patch

    import litellm

    captured_optional_params = []
    original_get_optional_params = litellm.utils.get_optional_params

    def _capture(*args, **kwargs):
        result = original_get_optional_params(*args, **kwargs)
        captured_optional_params.append(result)
        return result

    with (
        patch("litellm.main.get_optional_params", side_effect=_capture),
        patch(
            "litellm.main.openai_chat_completions.completion",
            return_value={"choices": [{"message": {"content": "ok"}}]},
        ),
    ):
        litellm.completion(
            model="github_copilot/gpt-4",
            messages=[{"role": "user", "content": "hi"}],
            http_client={"connect_timeout": 2.0},
        )

    assert len(captured_optional_params) == 1
    assert "http_client" not in captured_optional_params[0]


def test_completion_http_client_bypasses_supports_httpx_timeout_degrade_for_unlisted_provider():
    """Regression for review finding #3(b): http_client is a universal opt-in, not gated behind
    supports_httpx_timeout. A provider NOT on that allowlist (cohere) must still receive the full
    per-axis httpx.Timeout http_client resolves to, not the degraded float."""
    from unittest.mock import patch

    import httpx

    import litellm

    captured_timeout = []

    def _fake_provider_call(*args, **kwargs):
        captured_timeout.append(kwargs.get("timeout"))
        return {"choices": [{"message": {"content": "ok"}}]}

    with patch("litellm.main.base_llm_http_handler.completion", side_effect=_fake_provider_call):
        litellm.completion(
            model="cohere/command-r",
            messages=[{"role": "user", "content": "hi"}],
            http_client={"connect_timeout": 2.0, "read_timeout": 9.0},
        )

    assert len(captured_timeout) == 1
    resolved = captured_timeout[0]
    assert isinstance(resolved, httpx.Timeout), (
        "http_client-resolved httpx.Timeout must not be degraded to a float merely because the "
        "provider is absent from supports_httpx_timeout's allowlist"
    )
    assert resolved.connect == 2.0
    assert resolved.read == 9.0
