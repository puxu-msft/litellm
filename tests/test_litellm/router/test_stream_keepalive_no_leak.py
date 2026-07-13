"""stream_keepalive is a downstream-only param and must never reach the provider.

Adding it to ``all_litellm_params`` makes ``get_non_default_completion_params``
treat it as a LiteLLM-level param and exclude it from the provider-bound params
(otherwise the OpenAI param builder sweeps unknown top-level keys into
``extra_body`` and leaks them upstream).
"""

from litellm.types.utils import all_litellm_params
from litellm.utils import get_non_default_completion_params


def test_stream_keepalive_registered_as_litellm_param():
    assert "stream_keepalive" in all_litellm_params


def test_stream_keepalive_excluded_from_provider_params():
    kwargs = {
        "stream_keepalive": {"enabled": True, "interval": 15},
        "some_real_provider_param": "keep-me",
    }
    non_default = get_non_default_completion_params(kwargs)
    assert "stream_keepalive" not in non_default
    assert non_default.get("some_real_provider_param") == "keep-me"
