import os
from typing import Literal, Mapping, Optional, Protocol

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from litellm._logging import verbose_logger
from litellm.caching.in_memory_cache import InMemoryCache
from litellm.llms.custom_httpx.http_handler import HTTPHandler
from litellm.llms.github_copilot.common_utils import GetAPIKeyError, get_copilot_default_headers

CopilotEndpoint = Literal["messages", "responses", "chat"]


class _TTLCache(Protocol):
    def get_cache(self, key: str) -> object: ...
    def set_cache(self, key: str, value: object, *, ttl: int) -> None: ...
    def delete_cache(self, key: str) -> None: ...


_ALIASES: tuple[tuple[str, CopilotEndpoint], ...] = (
    ("/v1/messages", "messages"),
    ("messages", "messages"),
    ("/responses", "responses"),
    ("/v1/responses", "responses"),
    ("ws:/responses", "responses"),
    ("responses", "responses"),
    ("/chat/completions", "chat"),
    ("/v1/chat/completions", "chat"),
    ("chat", "chat"),
)


def normalize_endpoints(raw: tuple[str, ...]) -> "frozenset[CopilotEndpoint]":
    return frozenset(canon for name in raw for alias, canon in _ALIASES if alias == name)


def strip_copilot_prefix(model: str) -> str:
    prefix = "github_copilot/"
    return model[len(prefix) :] if model.startswith(prefix) else model


class _ModelEntry(BaseModel):
    id: str
    supported_endpoints: tuple[str, ...] = ()


class _ModelsResponse(BaseModel):
    data: tuple[_ModelEntry, ...]


_MODELS_ADAPTER = TypeAdapter(_ModelsResponse)
_PAIRS_ADAPTER: "TypeAdapter[tuple[tuple[str, frozenset[CopilotEndpoint]], ...]]" = TypeAdapter(
    tuple[tuple[str, frozenset[CopilotEndpoint]], ...]
)
_ENDPOINTS_ADAPTER: "TypeAdapter[tuple[str, ...]]" = TypeAdapter(tuple[str, ...])
_INFO_ADAPTER: "TypeAdapter[Mapping[str, object]]" = TypeAdapter(Mapping[str, object])


def fetch_endpoint_pairs(
    api_key: str,
    api_base: str,
    client: HTTPHandler,
    timeout: float = 5.0,
) -> "tuple[tuple[str, frozenset[CopilotEndpoint]], ...]":
    url = f"{api_base.rstrip('/')}/models"
    resp = client.get(url, headers=get_copilot_default_headers(api_key), timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"github_copilot /models HTTP {resp.status_code}: {resp.text[:200]}")
    parsed = _MODELS_ADAPTER.validate_python(resp.json())
    return tuple((entry.id, normalize_endpoints(entry.supported_endpoints)) for entry in parsed.data)


_CAP_CACHE: _TTLCache = InMemoryCache(max_size_in_memory=64, default_ttl=1800)
_CACHE_TTL_SECONDS = 1800
_REFRESH_INTERVAL_SECONDS = 300


def refresh_capabilities(
    api_key: str,
    api_base: str,
    client: HTTPHandler,
) -> "tuple[tuple[str, frozenset[CopilotEndpoint]], ...]":
    try:
        pairs = fetch_endpoint_pairs(api_key=api_key, api_base=api_base, client=client)
    except (httpx.HTTPError, RuntimeError, ValidationError, ValueError) as e:
        verbose_logger.debug("github_copilot refresh_capabilities failed for %s: %s", api_base, e)
        return ()
    _CAP_CACHE.delete_cache(api_base)
    _CAP_CACHE.set_cache(api_base, pairs, ttl=_CACHE_TTL_SECONDS)
    return pairs


def get_cached_pairs(
    api_base: str,
) -> "Optional[tuple[tuple[str, frozenset[CopilotEndpoint]], ...]]":
    cached = _CAP_CACHE.get_cache(api_base)
    if cached is None:
        return None
    try:
        return _PAIRS_ADAPTER.validate_python(cached)
    except ValidationError:
        return None


def resolve_endpoints(
    model: str,
    *,
    model_info: "Optional[Mapping[str, object]]",
    api_base: Optional[str],
) -> "frozenset[CopilotEndpoint]":
    bare = strip_copilot_prefix(model)
    if api_base is not None:
        cached = get_cached_pairs(api_base)
        if cached is not None:
            hit = next((eps for m, eps in cached if m == bare), None)
            if hit is not None:
                return hit
    raw = model_info.get("supported_endpoints") if model_info else None
    if raw is None:
        return frozenset()
    try:
        names = _ENDPOINTS_ADAPTER.validate_python(raw)
    except ValidationError:
        return frozenset()
    return normalize_endpoints(names)


def forced_mode(
    model_info: "Optional[Mapping[str, object]]",
) -> "Optional[Literal['anthropic', 'responses', 'chat']]":
    mode = model_info.get("mode") if model_info else None
    if mode == "anthropic":
        return "anthropic"
    if mode == "responses":
        return "responses"
    if mode == "chat":
        return "chat"
    return None


def route_supports_messages(
    model: str,
    *,
    model_info: "Optional[Mapping[str, object]]",
    api_base: Optional[str],
) -> bool:
    mode = forced_mode(model_info)
    if mode == "anthropic":
        return True
    if mode == "responses":
        return False
    return "messages" in resolve_endpoints(model, model_info=model_info, api_base=api_base)


def route_supports_responses(
    model: str,
    *,
    model_info: "Optional[Mapping[str, object]]",
    api_base: Optional[str],
) -> bool:
    mode = forced_mode(model_info)
    if mode == "responses":
        return True
    if mode in ("chat", "anthropic"):
        return False
    return "responses" in resolve_endpoints(model, model_info=model_info, api_base=api_base)


def raw_model_info(model: str) -> "Optional[Mapping[str, object]]":
    import litellm

    entry = litellm.model_cost.get(f"github_copilot/{strip_copilot_prefix(model)}")
    if not isinstance(entry, dict):
        return None
    try:
        return _INFO_ADAPTER.validate_python(entry)
    except ValidationError:
        return None


def copilot_api_base(explicit: Optional[str] = None) -> Optional[str]:
    from litellm.llms.github_copilot.authenticator import Authenticator

    if explicit:
        return explicit.rstrip("/")
    try:
        base = Authenticator().get_api_base()
    except (GetAPIKeyError, OSError, ValueError, KeyError) as e:
        verbose_logger.debug("github_copilot copilot_api_base failed: %s", e)
        return None
    return base.rstrip("/") if base else None


class _DeploymentEntry(BaseModel):
    litellm_params: "Optional[Mapping[str, object]]" = None


_DEPLOYMENTS_ADAPTER: "TypeAdapter[tuple[_DeploymentEntry, ...]]" = TypeAdapter(tuple[_DeploymentEntry, ...])


def _deployment_base(params: "Mapping[str, object]", default_base: Optional[str]) -> str:
    api_base = params.get("api_base")
    chosen = api_base if isinstance(api_base, str) and api_base else default_base
    return chosen.rstrip("/") if chosen else ""


def _copilot_deployment_bases(router: object) -> "tuple[str, ...]":
    get_list = getattr(router, "get_model_list", None)
    if get_list is None:
        return ()
    try:
        deployments = _DEPLOYMENTS_ADAPTER.validate_python(get_list() or ())
    except ValidationError:
        return ()
    default_base = copilot_api_base()
    bases = frozenset(
        _deployment_base(d.litellm_params, default_base)
        for d in deployments
        if d.litellm_params is not None and str(d.litellm_params.get("model", "")).startswith("github_copilot/")
    )
    return tuple(b for b in bases if b)


def _non_interactive_api_key() -> Optional[str]:
    from litellm.llms.github_copilot.authenticator import Authenticator

    auth = Authenticator()
    try:
        has_oauth_token = os.path.getsize(auth.access_token_file) > 0
    except OSError:
        has_oauth_token = False
    if not has_oauth_token:
        verbose_logger.debug("github_copilot refresh: no non-empty oauth token file, skipping to avoid device flow")
        return None
    try:
        return auth.get_api_key()
    except GetAPIKeyError as e:
        verbose_logger.debug("github_copilot refresh: get_api_key failed (%s)", e)
        return None


def refresh_all_deployments(router: object, client: Optional[HTTPHandler] = None) -> None:
    bases = _copilot_deployment_bases(router)
    if not bases:
        return
    key = _non_interactive_api_key()
    if key is None:
        return
    http = client if client is not None else HTTPHandler()
    for base in bases:
        refresh_capabilities(key, base, http)


def refresh_default_capabilities(
    *,
    api_key: Optional[str] = None,
    api_base: Optional[str] = None,
    client: Optional[HTTPHandler] = None,
) -> None:
    base = copilot_api_base(api_base)
    if base is None:
        return
    key = api_key if api_key is not None else _non_interactive_api_key()
    if key is None:
        return
    refresh_capabilities(key, base, client if client is not None else HTTPHandler())


async def periodic_capability_refresh_loop(
    router: object,
    interval_seconds: float = float(_REFRESH_INTERVAL_SECONDS),
) -> None:
    import asyncio

    while True:
        try:
            await asyncio.to_thread(refresh_all_deployments, router)
        except (httpx.HTTPError, OSError, ValueError, RuntimeError) as e:
            verbose_logger.debug("github_copilot periodic refresh error: %s", e)
        await asyncio.sleep(interval_seconds)
