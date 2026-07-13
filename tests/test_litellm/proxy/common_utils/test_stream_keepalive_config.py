import math

import pytest
from pydantic import ValidationError

from litellm.proxy.common_utils.stream_keepalive_config import (
    KEEPALIVE_DEFAULT_INTERVAL_SECONDS,
    merge_overrides,
    parse_override,
    resolve,
    should_advise_missing_upstream_timeout,
    validate_global_config,
)


def test_parse_rejects_unknown_key():
    with pytest.raises(ValidationError):
        parse_override({"enabled": True, "interbal": 10})  # typo -> extra=forbid


@pytest.mark.parametrize("bad", [0, -5, math.inf, math.nan, 0.5])  # <=0, inf, nan, <min
def test_parse_rejects_bad_interval(bad):
    with pytest.raises(ValidationError):
        parse_override({"interval": bad})


def test_partial_override_does_not_reset_global_interval():
    # global interval=5; deployment only sets enabled=false -> interval stays 5
    g = parse_override({"interval": 5})
    d = parse_override({"enabled": False})
    resolved = resolve(merge_overrides(g, d))
    assert resolved.enabled is False
    assert resolved.interval == 5


def test_resolve_defaults_when_unset():
    resolved = resolve(merge_overrides(None, None))
    assert resolved.enabled is True
    assert resolved.interval == KEEPALIVE_DEFAULT_INTERVAL_SECONDS


def test_deployment_overrides_global_field():
    g = parse_override({"enabled": True, "interval": 5})
    d = parse_override({"interval": 20})
    assert resolve(merge_overrides(g, d)).interval == 20


def test_deployment_none_falls_back_to_global():
    g = parse_override({"enabled": False, "interval": 7})
    resolved = resolve(merge_overrides(g, None))
    assert resolved.enabled is False
    assert resolved.interval == 7


def test_valid_interval_accepted():
    assert parse_override({"interval": 1}).interval == 1  # exactly min
    assert parse_override({"interval": 30.5}).interval == 30.5


def test_validate_global_config():
    assert validate_global_config({"enabled": True, "interval": 15}) is None
    assert validate_global_config(None) is None  # None is a valid "unset"
    assert validate_global_config({"interval": -5}) is not None  # invalid -> error message
    assert validate_global_config({"typo": 1}) is not None  # extra=forbid


def test_should_advise_missing_upstream_timeout():
    # enabled + no explicit request_timeout -> advise
    assert should_advise_missing_upstream_timeout({"enabled": True, "interval": 15}, False) is True
    assert should_advise_missing_upstream_timeout({"interval": 15}, False) is True  # enabled defaults True
    # explicit timeout set -> no advice
    assert should_advise_missing_upstream_timeout({"enabled": True}, True) is False
    # disabled -> no advice
    assert should_advise_missing_upstream_timeout({"enabled": False}, False) is False
    # unset -> no advice
    assert should_advise_missing_upstream_timeout(None, False) is False
