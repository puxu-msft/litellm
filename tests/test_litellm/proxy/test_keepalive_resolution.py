import litellm
from litellm.proxy.common_request_processing import (
    _resolve_downstream_keepalive,
    _surface_for_route,
)
from litellm.proxy.common_utils.sse_keepalive import DownstreamSSESurface


def test_surface_for_route():
    assert _surface_for_route("anthropic_messages") is DownstreamSSESurface.ANTHROPIC
    assert _surface_for_route("acompletion") is DownstreamSSESurface.OPENAI_CHAT
    assert _surface_for_route("aresponses") is DownstreamSSESurface.OPENAI_RESPONSES
    # non-target routes get no keepalive
    assert _surface_for_route("atext_completion") is None
    assert _surface_for_route("acreate_run") is None


def test_resolve_returns_none_for_non_target_route():
    ka, surface = _resolve_downstream_keepalive("atext_completion", object(), None, {})
    assert ka is None and surface is None


def test_resolve_global_config(monkeypatch):
    monkeypatch.setattr(litellm, "stream_keepalive", {"enabled": True, "interval": 12})
    ka, surface = _resolve_downstream_keepalive("anthropic_messages", object(), None, {})
    assert surface is DownstreamSSESurface.ANTHROPIC
    assert ka is not None and ka.enabled is True and ka.interval == 12


def test_resolve_defaults_when_global_unset(monkeypatch):
    monkeypatch.setattr(litellm, "stream_keepalive", None)
    ka, surface = _resolve_downstream_keepalive("acompletion", object(), None, {})
    assert surface is DownstreamSSESurface.OPENAI_CHAT
    assert ka is not None and ka.enabled is True and ka.interval == 15  # default


def test_resolve_bad_global_config_disables_not_raises(monkeypatch):
    monkeypatch.setattr(litellm, "stream_keepalive", {"interval": -5})  # invalid
    ka, surface = _resolve_downstream_keepalive("anthropic_messages", object(), None, {})
    assert ka is None and surface is None  # degrades to off, no exception


class _Deployment:
    def __init__(self, params):
        self.litellm_params = params


class _Router:
    def __init__(self, params):
        self._params = params

    def get_deployment(self, model_id):
        return _Deployment(self._params)


class _Resp:
    _hidden_params = {"model_id": "dep-1"}


def test_deployment_override_wins_over_global(monkeypatch):
    monkeypatch.setattr(litellm, "stream_keepalive", {"enabled": True, "interval": 30})
    router = _Router({"stream_keepalive": {"interval": 5}})
    ka, surface = _resolve_downstream_keepalive("anthropic_messages", _Resp(), router, {})
    assert ka is not None
    assert ka.interval == 5  # deployment override
    assert ka.enabled is True  # inherited from global
