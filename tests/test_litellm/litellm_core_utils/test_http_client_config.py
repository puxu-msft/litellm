import pytest
from pydantic import ValidationError

from litellm.litellm_core_utils.http_client_config import HttpClientConfig


def test_http_client_config_defaults_to_all_none():
    cfg = HttpClientConfig()
    assert cfg.connect_timeout is None
    assert cfg.read_timeout is None
    assert cfg.pool_timeout is None
    assert cfg.total_timeout is None
    assert cfg.http2 is None


def test_http_client_config_is_frozen():
    cfg = HttpClientConfig(connect_timeout=5.0)
    with pytest.raises(ValidationError):
        cfg.connect_timeout = 10.0


def test_http_client_config_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        HttpClientConfig(unknown_field=1)


@pytest.mark.parametrize("field_name", ["connect_timeout", "read_timeout", "pool_timeout", "total_timeout"])
@pytest.mark.parametrize("bad_value", [0, 0.0, -1, -0.5])
def test_http_client_config_rejects_non_positive_timeout_values(field_name, bad_value):
    """Every timeout field must reject 0 and negative values -- a 0s or negative
    connect/read/pool/total timeout is not a meaningful configuration and almost
    certainly indicates a misconfiguration that should fail loudly at parse time,
    not silently produce a client that times out instantly or never."""
    with pytest.raises(ValidationError):
        HttpClientConfig(**{field_name: bad_value})


@pytest.mark.parametrize("field_name", ["connect_timeout", "read_timeout", "pool_timeout", "total_timeout"])
def test_http_client_config_accepts_small_positive_timeout_values(field_name):
    cfg = HttpClientConfig(**{field_name: 0.001})
    assert getattr(cfg, field_name) == 0.001


def test_parse_http_client_config_none_returns_none():
    from litellm.litellm_core_utils.http_client_config import parse_http_client_config

    assert parse_http_client_config(None) is None


def test_parse_http_client_config_from_dict():
    from litellm.litellm_core_utils.http_client_config import (
        HttpClientConfig,
        parse_http_client_config,
    )

    parsed = parse_http_client_config({"connect_timeout": 5.0, "total_timeout": 30.0})
    assert parsed == HttpClientConfig(connect_timeout=5.0, total_timeout=30.0)


def test_parse_http_client_config_passthrough_for_existing_model():
    from litellm.litellm_core_utils.http_client_config import (
        HttpClientConfig,
        parse_http_client_config,
    )

    cfg = HttpClientConfig(read_timeout=2.0)
    assert parse_http_client_config(cfg) is cfg


def test_parse_http_client_config_rejects_unknown_keys():
    from pydantic import ValidationError

    from litellm.litellm_core_utils.http_client_config import parse_http_client_config

    with pytest.raises(ValidationError):
        parse_http_client_config({"not_a_real_field": 1})
