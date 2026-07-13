from litellm.llms.github_copilot.model_capabilities import (
    normalize_endpoints,
    strip_copilot_prefix,
)


def test_normalize_upstream_and_config_names():
    assert normalize_endpoints(("/responses", "ws:/responses")) == frozenset({"responses"})
    assert normalize_endpoints(("/v1/messages", "/chat/completions")) == frozenset({"messages", "chat"})
    assert normalize_endpoints(("/v1/chat/completions", "/v1/responses")) == frozenset({"chat", "responses"})


def test_normalize_drops_unknown_and_empty():
    assert normalize_endpoints(("/foo", "ws:/responses")) == frozenset({"responses"})
    assert normalize_endpoints(()) == frozenset()


def test_strip_only_copilot_prefix():
    assert strip_copilot_prefix("github_copilot/gpt-5.6-sol") == "gpt-5.6-sol"
    assert strip_copilot_prefix("claude-opus-4.8") == "claude-opus-4.8"
    assert strip_copilot_prefix("vendor/weird/model") == "vendor/weird/model"
