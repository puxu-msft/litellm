from typing import Literal, Mapping, Optional, Protocol

from pydantic import BaseModel, TypeAdapter

from litellm._logging import verbose_logger
from litellm.caching.in_memory_cache import InMemoryCache
from litellm.llms.custom_httpx.http_handler import HTTPHandler
from litellm.llms.github_copilot.common_utils import get_copilot_default_headers

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


_CAP_CACHE: _TTLCache = InMemoryCache(max_size_in_memory=64, default_ttl=300)


def refresh_capabilities(
    api_key: str,
    api_base: str,
    client: HTTPHandler,
) -> "tuple[tuple[str, frozenset[CopilotEndpoint]], ...]":
    try:
        pairs = fetch_endpoint_pairs(api_key=api_key, api_base=api_base, client=client)
    except Exception as e:
        verbose_logger.debug("github_copilot refresh_capabilities failed for %s: %s", api_base, e)
        return ()
    _CAP_CACHE.delete_cache(api_base)
    _CAP_CACHE.set_cache(api_base, pairs, ttl=300)
    return pairs


def get_cached_pairs(
    api_base: str,
) -> "Optional[tuple[tuple[str, frozenset[CopilotEndpoint]], ...]]":
    cached = _CAP_CACHE.get_cache(api_base)
    if cached is None:
        return None
    try:
        return _PAIRS_ADAPTER.validate_python(cached)
    except Exception:
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
            if hit:
                return hit
    raw = model_info.get("supported_endpoints") if model_info else None
    if raw is None:
        return frozenset()
    try:
        names = _ENDPOINTS_ADAPTER.validate_python(raw)
    except Exception:
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
    except Exception:
        return None


def copilot_api_base(explicit: Optional[str] = None) -> Optional[str]:
    from litellm.llms.github_copilot.authenticator import Authenticator

    if explicit:
        return explicit.rstrip("/")
    try:
        base = Authenticator().get_api_base()
    except Exception as e:
        verbose_logger.debug("github_copilot copilot_api_base failed: %s", e)
        return None
    return base.rstrip("/") if base else None


def refresh_default_capabilities(
    *,
    api_key: Optional[str] = None,
    api_base: Optional[str] = None,
    client: Optional[HTTPHandler] = None,
) -> None:
    from litellm.llms.github_copilot.authenticator import Authenticator

    base = copilot_api_base(api_base)
    if base is None:
        return
    key = api_key
    if key is None:
        try:
            key = Authenticator().get_api_key()
        except Exception as e:
            verbose_logger.debug("github_copilot refresh_default_capabilities: no api key (%s)", e)
            return
    refresh_capabilities(key, base, client if client is not None else HTTPHandler())


async def periodic_capability_refresh_loop(interval_seconds: float = 300.0) -> None:
    import asyncio

    while True:
        try:
            refresh_default_capabilities()
        except Exception as e:
            verbose_logger.debug("github_copilot periodic refresh error: %s", e)
        await asyncio.sleep(interval_seconds)
