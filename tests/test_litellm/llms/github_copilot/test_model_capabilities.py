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
