from __future__ import annotations

from contextvars import ContextVar, Token
from uuid import UUID

_current_request_id: ContextVar[UUID | None] = ContextVar("terminal_capture_request_id", default=None)


def set_current_request(request_id: UUID) -> Token[UUID | None]:
    return _current_request_id.set(request_id)


def reset_current_request(token: Token[UUID | None]) -> None:
    _current_request_id.reset(token)


def current_request_id() -> UUID | None:
    return _current_request_id.get()
