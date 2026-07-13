from typing import Literal

CopilotEndpoint = Literal["messages", "responses", "chat"]

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
