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


class _FakeResp:
    def __init__(self, status_code: int, payload: object):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> object:
        return self._payload


class _FakeClient:
    def __init__(self, resp: _FakeResp):
        self._resp = resp
        self.calls: list = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, timeout))
        return self._resp


_MODELS_PAYLOAD = {
    "data": [
        {"id": "claude-opus-4.8", "supported_endpoints": ["/v1/messages", "/chat/completions"]},
        {"id": "gpt-5.5", "supported_endpoints": ["/responses", "ws:/responses"]},
        {"id": "gpt-4o"},
    ]
}


def test_fetch_endpoint_pairs_preserves_endpoints():
    from litellm.llms.github_copilot.model_capabilities import fetch_endpoint_pairs

    client = _FakeClient(_FakeResp(200, _MODELS_PAYLOAD))
    pairs = fetch_endpoint_pairs("k", "https://api.githubcopilot.com", client)
    as_map = dict(pairs)
    assert as_map["claude-opus-4.8"] == frozenset({"messages", "chat"})
    assert as_map["gpt-5.5"] == frozenset({"responses"})
    assert as_map["gpt-4o"] == frozenset()
    assert client.calls[0][0] == "https://api.githubcopilot.com/models"


def test_fetch_endpoint_pairs_non_200_raises():
    import pytest

    from litellm.llms.github_copilot.model_capabilities import fetch_endpoint_pairs

    client = _FakeClient(_FakeResp(401, {"error": "no auth"}))
    with pytest.raises(RuntimeError):
        fetch_endpoint_pairs("k", "https://api.githubcopilot.com", client)


def test_fetch_endpoint_pairs_malformed_raises():
    import pytest

    from litellm.llms.github_copilot.model_capabilities import fetch_endpoint_pairs

    client = _FakeClient(_FakeResp(200, {"unexpected": "shape"}))
    with pytest.raises(Exception):
        fetch_endpoint_pairs("k", "https://api.githubcopilot.com", client)


def test_refresh_and_get_cached():
    import litellm.llms.github_copilot.model_capabilities as mc

    mc._CAP_CACHE.flush_cache()
    client = _FakeClient(_FakeResp(200, _MODELS_PAYLOAD))
    pairs = mc.refresh_capabilities("k", "https://api.githubcopilot.com", client)
    assert dict(pairs)["gpt-5.5"] == frozenset({"responses"})
    cached = mc.get_cached_pairs("https://api.githubcopilot.com")
    assert cached is not None
    assert dict(cached)["gpt-5.5"] == frozenset({"responses"})


def test_refresh_replaces_stale_value():
    import litellm.llms.github_copilot.model_capabilities as mc

    mc._CAP_CACHE.flush_cache()
    first = {"data": [{"id": "gpt-5.5", "supported_endpoints": ["/responses"]}]}
    second = {"data": [{"id": "gpt-5.5", "supported_endpoints": ["/chat/completions"]}]}
    mc.refresh_capabilities("k", "https://b", _FakeClient(_FakeResp(200, first)))
    mc.refresh_capabilities("k", "https://b", _FakeClient(_FakeResp(200, second)))
    assert dict(mc.get_cached_pairs("https://b"))["gpt-5.5"] == frozenset({"chat"})


def test_refresh_failure_returns_empty_no_raise():
    import litellm.llms.github_copilot.model_capabilities as mc

    mc._CAP_CACHE.flush_cache()
    assert mc.refresh_capabilities("k", "https://b", _FakeClient(_FakeResp(500, {"e": 1}))) == ()


def test_get_cached_miss_returns_none():
    import litellm.llms.github_copilot.model_capabilities as mc

    mc._CAP_CACHE.flush_cache()
    assert mc.get_cached_pairs("https://unseen.example") is None


def test_resolve_prefers_dynamic_cache():
    import litellm.llms.github_copilot.model_capabilities as mc

    mc._CAP_CACHE.flush_cache()
    mc._CAP_CACHE.set_cache("https://b", (("gpt-5.5", frozenset({"responses"})),), ttl=300)
    eps = mc.resolve_endpoints("github_copilot/gpt-5.5", model_info={}, api_base="https://b")
    assert eps == frozenset({"responses"})


def test_resolve_falls_back_to_raw_model_info():
    import litellm.llms.github_copilot.model_capabilities as mc

    mc._CAP_CACHE.flush_cache()
    eps = mc.resolve_endpoints(
        "gpt-x", model_info={"supported_endpoints": ["/responses"]}, api_base="https://b"
    )
    assert eps == frozenset({"responses"})


def test_resolve_empty_when_nothing_known():
    import litellm.llms.github_copilot.model_capabilities as mc

    mc._CAP_CACHE.flush_cache()
    assert mc.resolve_endpoints("mystery", model_info={}, api_base=None) == frozenset()


def test_route_messages_mode_anthropic_forces():
    import litellm.llms.github_copilot.model_capabilities as mc

    mc._CAP_CACHE.flush_cache()
    assert mc.route_supports_messages("m", model_info={"mode": "anthropic"}, api_base=None) is True
    assert mc.route_supports_responses("m", model_info={"mode": "anthropic"}, api_base=None) is False


def test_mode_chat_does_not_block_messages():
    import litellm.llms.github_copilot.model_capabilities as mc

    mc._CAP_CACHE.flush_cache()
    info = {"mode": "chat", "supported_endpoints": ["/v1/chat/completions", "/v1/messages"]}
    assert mc.route_supports_messages("claude-x", model_info=info, api_base=None) is True
    assert mc.route_supports_responses("claude-x", model_info=info, api_base=None) is False


def test_mode_responses_forces_responses():
    import litellm.llms.github_copilot.model_capabilities as mc

    mc._CAP_CACHE.flush_cache()
    assert mc.route_supports_responses("m", model_info={"mode": "responses"}, api_base=None) is True
    assert mc.route_supports_messages("m", model_info={"mode": "responses"}, api_base=None) is False


def test_route_unset_mode_uses_endpoints():
    import litellm.llms.github_copilot.model_capabilities as mc

    mc._CAP_CACHE.flush_cache()
    mc._CAP_CACHE.set_cache("https://b", (("gpt-5.5", frozenset({"responses"})),), ttl=300)
    assert mc.route_supports_responses("gpt-5.5", model_info={}, api_base="https://b") is True
    assert mc.route_supports_messages("gpt-5.5", model_info={}, api_base="https://b") is False


def test_refresh_default_capabilities_di_writes_cache():
    import litellm.llms.github_copilot.model_capabilities as mc

    mc._CAP_CACHE.flush_cache()
    client = _FakeClient(_FakeResp(200, _MODELS_PAYLOAD))
    mc.refresh_default_capabilities(api_key="k", api_base="https://b", client=client)
    cached = mc.get_cached_pairs("https://b")
    assert cached is not None
    assert dict(cached)["gpt-5.5"] == frozenset({"responses"})


def test_refresh_default_capabilities_no_base_is_noop():
    import litellm.llms.github_copilot.model_capabilities as mc

    mc._CAP_CACHE.flush_cache()
    mc.refresh_default_capabilities(api_key="k", api_base=None, client=_FakeClient(_FakeResp(200, _MODELS_PAYLOAD)))
    assert mc.get_cached_pairs("https://b") is None


def test_raw_model_info_reads_supported_endpoints():
    import litellm
    import litellm.llms.github_copilot.model_capabilities as mc

    litellm.register_model({
        "github_copilot/probe-model-xyz": {
            "mode": "chat",
            "supported_endpoints": ["/v1/chat/completions", "/v1/messages"],
        }
    })
    info = mc.raw_model_info("github_copilot/probe-model-xyz")
    assert info is not None
    assert info.get("mode") == "chat"
    assert info.get("supported_endpoints") == ["/v1/chat/completions", "/v1/messages"]


def test_resolver_none_model_info_safe():
    import litellm.llms.github_copilot.model_capabilities as mc

    mc._CAP_CACHE.flush_cache()
    assert mc.resolve_endpoints("x", model_info=None, api_base=None) == frozenset()
    assert mc.route_supports_messages("x", model_info=None, api_base=None) is False
    assert mc.route_supports_responses("x", model_info=None, api_base=None) is False
