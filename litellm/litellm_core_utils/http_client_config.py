"""Config surface for per-provider upstream HTTP client tuning (connect/read/pool/total
timeouts, HTTP/2 reservation). See docs/superpowers/specs/2026-07-13-upstream-http-client-config-design.md.
"""

from typing import Optional, TypedDict

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
