from typing import Literal, Mapping, Protocol

from pydantic import BaseModel, TypeAdapter

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

EndpointPairs = tuple


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
