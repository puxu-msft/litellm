from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

AuthHeaders = Callable[[], Awaitable[tuple[tuple[str, str], ...]]]


@dataclass(frozen=True, slots=True)
class ReplayDryRun:
    method: str
    url: str
    byte_count: int


@dataclass(frozen=True, slots=True)
class ReplaySent:
    status_code: int
    response_body: bytes


@dataclass(frozen=True, slots=True)
class ReplayFailed:
    detail: str


async def replay_request(
    client: httpx.AsyncClient,
    *,
    method: str,
    url: str,
    body: bytes,
    headers: tuple[tuple[str, str], ...],
    auth_headers: AuthHeaders,
    dry_run: bool,
) -> ReplayDryRun | ReplaySent | ReplayFailed:
    if dry_run:
        return ReplayDryRun(method, url, len(body))
    try:
        current_auth = await auth_headers()
        response = await client.request(method, url, content=body, headers=(*headers, *current_auth))
        return ReplaySent(response.status_code, response.content)
    except httpx.HTTPError as exception:
        return ReplayFailed(str(exception))
