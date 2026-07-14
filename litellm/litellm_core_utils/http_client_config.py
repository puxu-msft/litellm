"""Config surface for per-provider upstream HTTP client tuning (connect/read/pool/total
timeouts, HTTP/2 reservation). See docs/superpowers/specs/2026-07-13-upstream-http-client-config-design.md.
"""

from dataclasses import dataclass
from typing import Optional, TypedDict, Union

import httpx
from pydantic import BaseModel, ConfigDict, Field

from litellm._logging import verbose_logger
from litellm.constants import HTTP_HANDLER_CONNECT_TIMEOUT_SECONDS


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


@dataclass(frozen=True, slots=True)
class ResolvedHttpClientTimeout:
    httpx_timeout: httpx.Timeout
    total_timeout: Optional[float]


def resolve_http_client_timeout(
    cfg: Optional[HttpClientConfig],
    legacy_effective_timeout: Union[float, httpx.Timeout],
) -> ResolvedHttpClientTimeout:
    """Resolve a validated HttpClientConfig (or None) plus the caller's face-specific legacy
    timeout into a concrete httpx.Timeout plus an optional absolute total_timeout.

    Two-mode contract (review finding A): when `cfg` is None this is a pure passthrough -- an
    already-constructed legacy httpx.Timeout is returned unmodified (same object), and a bare
    legacy float is materialized into an httpx.Timeout using the connect-default constant. When
    `cfg` is non-None, every axis is merged with `is not None` checks (never `or`): a field cfg
    sets wins on its own axis; an axis cfg leaves unset falls back to legacy's own value for
    that axis. `total_timeout` is only ever populated from cfg."""
    if cfg is None:
        if isinstance(legacy_effective_timeout, httpx.Timeout):
            return ResolvedHttpClientTimeout(httpx_timeout=legacy_effective_timeout, total_timeout=None)
        return ResolvedHttpClientTimeout(
            httpx_timeout=httpx.Timeout(legacy_effective_timeout, connect=HTTP_HANDLER_CONNECT_TIMEOUT_SECONDS),
            total_timeout=None,
        )

    if isinstance(legacy_effective_timeout, httpx.Timeout):
        legacy_connect = legacy_effective_timeout.connect
        legacy_read = legacy_effective_timeout.read
        legacy_pool = legacy_effective_timeout.pool
        legacy_write = legacy_effective_timeout.write
    else:
        legacy_connect = None
        legacy_read = legacy_effective_timeout
        legacy_pool = legacy_effective_timeout
        legacy_write = legacy_effective_timeout

    connect = (
        cfg.connect_timeout
        if cfg.connect_timeout is not None
        else (legacy_connect if legacy_connect is not None else HTTP_HANDLER_CONNECT_TIMEOUT_SECONDS)
    )
    read = cfg.read_timeout if cfg.read_timeout is not None else legacy_read
    pool = cfg.pool_timeout if cfg.pool_timeout is not None else legacy_pool
    return ResolvedHttpClientTimeout(
        httpx_timeout=httpx.Timeout(legacy_write, connect=connect, read=read, pool=pool),
        total_timeout=cfg.total_timeout,
    )


def warn_if_legacy_timeout_coexists_with_http_client(
    *,
    legacy_timeout: Optional[float],
    http_client: Optional[HttpClientConfig],
    context: str,
) -> None:
    """Log a load-time warning when both the legacy `timeout` field and the new `http_client`
    config are set on the same scope (global litellm_settings, or a single deployment's
    litellm_params). `http_client` always wins in practice (see resolve_http_client_timeout /
    merge_http_client_config) -- this only surfaces that precedence so operators are not
    silently confused about which setting is actually in effect."""
    if legacy_timeout is None or http_client is None:
        return
    verbose_logger.warning(
        "%s: both the legacy `timeout=%s` and `http_client` are configured; "
        "`http_client` takes priority and `timeout` will be ignored for the axes it covers.",
        context,
        legacy_timeout,
    )
