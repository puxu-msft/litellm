from typing import Literal, Mapping, Optional, Protocol

from pydantic import BaseModel, TypeAdapter

from litellm._logging import verbose_logger
from litellm.caching.in_memory_cache import InMemoryCache
from litellm.llms.github_copilot.common_utils import get_copilot_default_headers

CopilotEndpoint = Literal["messages", "responses", "chat"]


class _HTTPResponse(Protocol):
    status_code: int
    text: str

    def json(self) -> object: ...


class _HTTPGetClient(Protocol):
    def get(self, url: str, headers: "Mapping[str, str]", timeout: float) -> _HTTPResponse: ...

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
    return model[len(prefix):] if model.startswith(prefix) else model


class _ModelEntry(BaseModel):
    id: str
    supported_endpoints: tuple[str, ...] = ()


class _ModelsResponse(BaseModel):
    data: tuple[_ModelEntry, ...]


_MODELS_ADAPTER = TypeAdapter(_ModelsResponse)


def fetch_endpoint_pairs(
    api_key: str,
    api_base: str,
    client: _HTTPGetClient,
    timeout: float = 5.0,
) -> "tuple[tuple[str, frozenset[CopilotEndpoint]], ...]":
    url = f"{api_base.rstrip('/')}/models"
    resp = client.get(url, headers=get_copilot_default_headers(api_key), timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"github_copilot /models HTTP {resp.status_code}: {resp.text[:200]}")
    parsed = _MODELS_ADAPTER.validate_python(resp.json())
    return tuple((entry.id, normalize_endpoints(entry.supported_endpoints)) for entry in parsed.data)


_CAP_CACHE = InMemoryCache(max_size_in_memory=64, default_ttl=300)


def refresh_capabilities(
    api_key: str,
    api_base: str,
    client: _HTTPGetClient,
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
    return cached if isinstance(cached, tuple) else None
