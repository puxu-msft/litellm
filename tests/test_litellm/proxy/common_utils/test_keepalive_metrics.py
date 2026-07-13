"""Keepalive Prometheus metrics: record functions increment the default-registry
series when prometheus_client is present, and never raise. The keepalive path
must not depend on the metrics backend, so unavailability is a silent no-op."""

from prometheus_client import REGISTRY

from litellm.proxy.common_utils.keepalive_metrics import (
    record_pings,
    stream_ended,
    stream_started,
)


def _val(name: str, labels: dict) -> float:
    v = REGISTRY.get_sample_value(name, labels)
    return v if v is not None else 0.0


def test_record_pings_increments_counter():
    before = _val("litellm_keepalive_pings_sent_total", {"surface": "anthropic"})
    record_pings("anthropic", 3)
    record_pings("anthropic", 2)
    after = _val("litellm_keepalive_pings_sent_total", {"surface": "anthropic"})
    assert after - before == 5


def test_record_pings_zero_is_noop():
    before = _val("litellm_keepalive_pings_sent_total", {"surface": "openai_chat"})
    record_pings("openai_chat", 0)
    after = _val("litellm_keepalive_pings_sent_total", {"surface": "openai_chat"})
    assert after == before


def test_stream_lifecycle_active_gauge_and_termination():
    surface = "openai_responses"
    active0 = _val("litellm_keepalive_active_streams", {"surface": surface})
    term0 = _val("litellm_keepalive_terminations_total", {"surface": surface, "reason": "completed"})

    stream_started(surface)
    active_mid = _val("litellm_keepalive_active_streams", {"surface": surface})
    assert active_mid - active0 == 1

    stream_ended(surface, "completed", 2.5)
    active_end = _val("litellm_keepalive_active_streams", {"surface": surface})
    term1 = _val("litellm_keepalive_terminations_total", {"surface": surface, "reason": "completed"})
    assert active_end == active0  # gauge back to baseline
    assert term1 - term0 == 1

    # duration histogram recorded a sample
    count = _val("litellm_keepalive_stream_duration_seconds_count", {"surface": surface})
    assert count >= 1
