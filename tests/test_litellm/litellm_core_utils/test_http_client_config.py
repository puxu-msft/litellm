import httpx
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


def test_merge_http_client_config_both_none_returns_none():
    from litellm.litellm_core_utils.http_client_config import merge_http_client_config

    assert merge_http_client_config(None, None) is None


def test_merge_http_client_config_deployment_only():
    from litellm.litellm_core_utils.http_client_config import (
        HttpClientConfig,
        merge_http_client_config,
    )

    deployment = HttpClientConfig(connect_timeout=1.0)
    assert merge_http_client_config(None, deployment) == deployment


def test_merge_http_client_config_global_only():
    from litellm.litellm_core_utils.http_client_config import (
        HttpClientConfig,
        merge_http_client_config,
    )

    glob = HttpClientConfig(total_timeout=60.0)
    assert merge_http_client_config(glob, None) == glob


def test_merge_http_client_config_deployment_field_wins_over_global_field():
    from litellm.litellm_core_utils.http_client_config import (
        HttpClientConfig,
        merge_http_client_config,
    )

    glob = HttpClientConfig(connect_timeout=1.0, total_timeout=60.0)
    deployment = HttpClientConfig(connect_timeout=9.0)
    merged = merge_http_client_config(glob, deployment)
    assert merged == HttpClientConfig(connect_timeout=9.0, total_timeout=60.0)


def test_merge_http_client_config_deployment_none_field_does_not_shadow_global():
    from litellm.litellm_core_utils.http_client_config import (
        HttpClientConfig,
        merge_http_client_config,
    )

    glob = HttpClientConfig(read_timeout=5.0)
    deployment = HttpClientConfig(connect_timeout=2.0)
    merged = merge_http_client_config(glob, deployment)
    assert merged.read_timeout == 5.0
    assert merged.connect_timeout == 2.0


def test_merge_http_client_config_explicit_null_in_deployment_falls_back_to_global_not_cleared():
    from litellm.litellm_core_utils.http_client_config import (
        HttpClientConfig,
        merge_http_client_config,
    )

    glob = HttpClientConfig(connect_timeout=7.0)
    deployment = HttpClientConfig(connect_timeout=None, read_timeout=2.0)
    assert "connect_timeout" in deployment.model_fields_set
    merged = merge_http_client_config(glob, deployment)
    assert merged.connect_timeout == 7.0
    assert merged.read_timeout == 2.0


def test_resolve_http_client_timeout_no_config_uses_legacy_float():
    from litellm.litellm_core_utils.http_client_config import resolve_http_client_timeout

    resolved = resolve_http_client_timeout(None, legacy_effective_timeout=600.0)
    assert resolved.httpx_timeout == httpx.Timeout(600.0, connect=5.0)
    assert resolved.total_timeout is None


def test_resolve_http_client_timeout_no_config_passes_through_legacy_httpx_timeout_unchanged():
    from litellm.litellm_core_utils.http_client_config import resolve_http_client_timeout

    legacy = httpx.Timeout(600.0, connect=10.0, read=20.0, pool=30.0)
    resolved = resolve_http_client_timeout(None, legacy_effective_timeout=legacy)
    assert resolved.httpx_timeout == legacy
    assert resolved.total_timeout is None


def test_resolve_http_client_timeout_cfg_overrides_win_over_legacy_httpx_timeout_per_axis():
    from litellm.litellm_core_utils.http_client_config import (
        HttpClientConfig,
        resolve_http_client_timeout,
    )

    legacy = httpx.Timeout(600.0, connect=10.0, read=20.0, pool=30.0)
    cfg = HttpClientConfig(read_timeout=99.0)
    resolved = resolve_http_client_timeout(cfg, legacy_effective_timeout=legacy)
    assert resolved.httpx_timeout.read == 99.0
    assert resolved.httpx_timeout.connect == 10.0
    assert resolved.httpx_timeout.pool == 30.0


def test_resolve_http_client_timeout_cfg_connect_override_falls_back_to_http_handler_default_not_legacy():
    from litellm.litellm_core_utils.http_client_config import (
        HttpClientConfig,
        resolve_http_client_timeout,
    )

    legacy = httpx.Timeout(600.0, connect=None, read=20.0, pool=30.0)
    resolved = resolve_http_client_timeout(HttpClientConfig(), legacy_effective_timeout=legacy)
    assert resolved.httpx_timeout.connect == 5.0
    assert resolved.httpx_timeout.read == 20.0
    assert resolved.httpx_timeout.pool == 30.0


def test_resolve_http_client_timeout_preserves_legacy_zero_connect_when_cfg_leaves_it_unset():
    from litellm.litellm_core_utils.http_client_config import (
        HttpClientConfig,
        resolve_http_client_timeout,
    )

    legacy = httpx.Timeout(600.0, connect=0.0, read=20.0, pool=30.0)
    cfg = HttpClientConfig(read_timeout=99.0)
    resolved = resolve_http_client_timeout(cfg, legacy_effective_timeout=legacy)
    assert resolved.httpx_timeout.connect == 0.0


def test_resolve_http_client_timeout_partial_config_falls_back_per_component():
    from litellm.litellm_core_utils.http_client_config import (
        HttpClientConfig,
        resolve_http_client_timeout,
    )

    cfg = HttpClientConfig(read_timeout=15.0)
    resolved = resolve_http_client_timeout(cfg, legacy_effective_timeout=600.0)
    assert resolved.httpx_timeout.read == 15.0
    assert resolved.httpx_timeout.connect == 5.0
    assert resolved.httpx_timeout.pool == 600.0
    assert resolved.total_timeout is None


def test_resolve_http_client_timeout_carries_total_timeout_through():
    from litellm.litellm_core_utils.http_client_config import (
        HttpClientConfig,
        resolve_http_client_timeout,
    )

    cfg = HttpClientConfig(total_timeout=45.0)
    resolved = resolve_http_client_timeout(cfg, legacy_effective_timeout=600.0)
    assert resolved.total_timeout == 45.0
    assert resolved.httpx_timeout.connect == 5.0
