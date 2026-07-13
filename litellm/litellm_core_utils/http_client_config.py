"""Config surface for per-provider upstream HTTP client tuning (connect/read/pool/total
timeouts, HTTP/2 reservation). See docs/superpowers/specs/2026-07-13-upstream-http-client-config-design.md.
"""

from typing import Optional, TypedDict, Union

from pydantic import BaseModel, ConfigDict, Field


class HttpClientConfig(BaseModel):
    """Parsed, validated upstream HTTP client configuration.

    All fields are optional: an absent field means "fall back to the next layer"
    (deployment -> global -> legacy per-face default), resolved by
    `merge_http_client_config` / `resolve_http_client_timeout`. Every present timeout
    field must be strictly positive (`gt=0`); 0 or negative is rejected at parse time
    rather than silently producing an instantly-expiring or infinite timeout.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    connect_timeout: Optional[float] = Field(default=None, gt=0)
    read_timeout: Optional[float] = Field(default=None, gt=0)
    pool_timeout: Optional[float] = Field(default=None, gt=0)
    total_timeout: Optional[float] = Field(default=None, gt=0)
    http2: Optional[bool] = None  # schema-reserved only; not wired to any transport this round


class HttpClientConfigDict(TypedDict, total=False):
    connect_timeout: Optional[float]
    read_timeout: Optional[float]
    pool_timeout: Optional[float]
    total_timeout: Optional[float]
    http2: Optional[bool]


def parse_http_client_config(
    raw: Optional[Union["HttpClientConfig", HttpClientConfigDict, dict]],
) -> Optional[HttpClientConfig]:
    """Coerce a raw `http_client` value (None, dict/TypedDict from YAML or kwargs, or an
    already-constructed HttpClientConfig) into a validated HttpClientConfig, or None."""
    if raw is None:
        return None
    if isinstance(raw, HttpClientConfig):
        return raw
    return HttpClientConfig(**raw)


def merge_http_client_config(
    global_cfg: Optional[HttpClientConfig],
    deployment_cfg: Optional[HttpClientConfig],
) -> Optional[HttpClientConfig]:
    """Merge global and per-deployment http_client config, field by field. A field the
    deployment config explicitly sets to a non-None value wins; a field the deployment config
    leaves unset, OR explicitly sets to None (e.g. an explicit `null` in a deployment's YAML
    http_client block), falls back to the global config's value for that field. There is no
    "explicit null clears the global value" semantic — per the frozen spec, explicit null and
    "not set at all" are equivalent from the deployment's perspective."""
    if global_cfg is None and deployment_cfg is None:
        return None
    base_values = global_cfg.model_dump() if global_cfg is not None else {}
    override_values = (
        {
            field: value
            for field, value in deployment_cfg.model_dump(include=deployment_cfg.model_fields_set).items()
            if value is not None
        }
        if deployment_cfg is not None
        else {}
    )
    return HttpClientConfig(**{**base_values, **override_values})
