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
