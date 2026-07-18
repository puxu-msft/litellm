from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console
from rich.live import Live
from rich.text import Text


@dataclass(frozen=True, slots=True)
class RendererStarted:
    pass


@dataclass(frozen=True, slots=True)
class RendererDegraded:
    detail: str


class RichLiveRenderer:
    def __init__(self, console: Console, *, refresh_hz: int = 4) -> None:
        self._console = console
        self._refresh_hz = refresh_hz
        self._live: Live | None = None
        self._failed_once = False
        self._degraded = False

    def start(self, footer: str) -> RendererStarted | RendererDegraded:
        if self._degraded:
            return RendererDegraded("renderer permanently degraded")
        try:
            self._live = Live(Text(footer), console=self._console, refresh_per_second=self._refresh_hz)
            self._live.start(refresh=True)
            return RendererStarted()
        except Exception as exception:
            return self._handle_failure(exception)

    def update(self, footer: str) -> RendererStarted | RendererDegraded:
        if self._live is None:
            return self.start(footer)
        try:
            self._live.update(Text(footer), refresh=True)
            return RendererStarted()
        except Exception as exception:
            return self._handle_failure(exception)

    def log(self, line: str) -> RendererStarted | RendererDegraded:
        try:
            self._console.print(Text.from_ansi(line))
            return RendererStarted()
        except Exception as exception:
            return self._handle_failure(exception)

    def stop(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def _handle_failure(self, exception: Exception) -> RendererDegraded:
        self.stop()
        if self._failed_once:
            self._degraded = True
            return RendererDegraded(str(exception))
        self._failed_once = True
        return RendererDegraded(str(exception))
