from __future__ import annotations

from typing import Protocol

from litellm.proxy.observability.terminal.events import BodyBoundary


class ChunkObserver(Protocol):
    def observe(self, boundary: BodyBoundary, chunk: bytes) -> None: ...


def observe_fail_open(observer: ChunkObserver, boundary: BodyBoundary, chunk: bytes) -> bool:
    try:
        observer.observe(boundary, chunk)
        return True
    except Exception:
        return False
