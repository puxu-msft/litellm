"""Downstream SSE keepalive config — Override (partial, all-optional) vs Resolved.

The proxy accepts ``stream_keepalive`` both globally (``litellm_settings``) and
per-deployment (``litellm_params``). A partial per-deployment override must not
clobber unset global fields, so the wire/config type is all-optional
(``StreamKeepaliveOverride``) and defaults are applied exactly once at the end
via ``resolve`` into a fully-populated ``ResolvedStreamKeepaliveConfig``.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, field_validator

KEEPALIVE_MIN_INTERVAL_SECONDS = 1.0
KEEPALIVE_DEFAULT_INTERVAL_SECONDS = 15.0


class StreamKeepaliveOverride(BaseModel):
    """Partial config: every field optional so merge can tell "unset" from "set"."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    enabled: bool | None = None
    interval: float | None = None

    @field_validator("interval")
    @classmethod
    def _validate_interval(cls, v: float | None) -> float | None:
        if v is None:
            return v
        if not math.isfinite(v) or v < KEEPALIVE_MIN_INTERVAL_SECONDS:
            raise ValueError(
                f"stream_keepalive.interval must be finite and >= {KEEPALIVE_MIN_INTERVAL_SECONDS}"
            )
        return v


class ResolvedStreamKeepaliveConfig(BaseModel):
    """Fully-populated config used by the streaming path."""

    model_config = ConfigDict(frozen=True)
    enabled: bool
    interval: float


def parse_override(raw: object) -> StreamKeepaliveOverride:
    if isinstance(raw, StreamKeepaliveOverride):
        return raw
    return StreamKeepaliveOverride.model_validate(raw)


def merge_overrides(
    global_o: StreamKeepaliveOverride | None,
    deployment_o: StreamKeepaliveOverride | None,
) -> StreamKeepaliveOverride:
    """Deployment fields that were explicitly set override the global ones."""
    g = global_o if global_o is not None else StreamKeepaliveOverride()
    d = deployment_o if deployment_o is not None else StreamKeepaliveOverride()
    enabled = d.enabled if "enabled" in d.model_fields_set else g.enabled
    interval = d.interval if "interval" in d.model_fields_set else g.interval
    return StreamKeepaliveOverride(enabled=enabled, interval=interval)


def resolve(merged: StreamKeepaliveOverride) -> ResolvedStreamKeepaliveConfig:
    """Apply defaults exactly once at the end."""
    return ResolvedStreamKeepaliveConfig(
        enabled=merged.enabled if merged.enabled is not None else True,
        interval=(
            merged.interval
            if merged.interval is not None
            else KEEPALIVE_DEFAULT_INTERVAL_SECONDS
        ),
    )
