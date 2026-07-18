from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class TerminalLoggingMode(StrEnum):
    AUTO = "auto"
    INTERACTIVE_EXPERIMENTAL = "interactive-experimental"
    PLAIN = "plain"
    JSON = "json"
    OFF = "off"


@dataclass(frozen=True, slots=True)
class StaticTerminalConfig:
    mode: TerminalLoggingMode
    central_path: Path
    spool_path: Path
    segment_max_age_seconds: int
    segment_max_bytes: int
    shadow_enabled: bool


@dataclass(frozen=True, slots=True)
class DynamicTerminalConfig:
    refresh_hz: int
    slow_yellow_seconds: float
    slow_red_seconds: float
    color: str


@dataclass(frozen=True, slots=True)
class TerminalLoggingConfig:
    static: StaticTerminalConfig
    dynamic: DynamicTerminalConfig

    def reconfigure(self, dynamic: DynamicTerminalConfig) -> TerminalLoggingConfig:
        return replace(self, dynamic=dynamic)


@dataclass(frozen=True, slots=True)
class ConfigLoaded:
    config: TerminalLoggingConfig


@dataclass(frozen=True, slots=True)
class ConfigInvalid:
    detail: str


ConfigLoadResult: TypeAlias = ConfigLoaded | ConfigInvalid


class _RawConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: TerminalLoggingMode = TerminalLoggingMode.AUTO
    central_path: Path = Path("~/.local/share/litellm/terminal-events").expanduser()
    spool_path: Path = Path("~/.local/share/litellm/terminal-spool").expanduser()
    segment_max_age_seconds: int = Field(default=2 * 24 * 60 * 60, ge=1)
    segment_max_bytes: int = Field(default=1024**3, ge=1)
    shadow_enabled: bool = False
    refresh_hz: int = Field(default=4, ge=1, le=60)
    slow_yellow_seconds: float = Field(default=10.0, gt=0)
    slow_red_seconds: float = Field(default=30.0, gt=0)
    color: str = "auto"


def load_terminal_logging_config(raw: object, *, poc_live_status: bool) -> ConfigLoadResult:
    try:
        parsed = _RawConfig.model_validate(raw)
        if parsed.slow_red_seconds <= parsed.slow_yellow_seconds:
            return ConfigInvalid("slow_red_seconds must exceed slow_yellow_seconds")
        if poc_live_status and parsed.mode is TerminalLoggingMode.INTERACTIVE_EXPERIMENTAL:
            return ConfigInvalid("interactive terminal logging conflicts with request_log.live_status")
        return ConfigLoaded(
            TerminalLoggingConfig(
                static=StaticTerminalConfig(
                    parsed.mode,
                    parsed.central_path,
                    parsed.spool_path,
                    parsed.segment_max_age_seconds,
                    parsed.segment_max_bytes,
                    parsed.shadow_enabled,
                ),
                dynamic=DynamicTerminalConfig(
                    parsed.refresh_hz,
                    parsed.slow_yellow_seconds,
                    parsed.slow_red_seconds,
                    parsed.color,
                ),
            )
        )
    except ValidationError as exception:
        return ConfigInvalid(str(exception))
