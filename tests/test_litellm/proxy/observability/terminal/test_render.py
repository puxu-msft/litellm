from __future__ import annotations

import io
import logging

from rich.console import Console

from litellm.proxy.observability.terminal.render.logging_adapter import install_handler, restore_handler
from litellm.proxy.observability.terminal.render.model import (
    CompletionRecord,
    CompletionStatus,
    InFlightGroup,
    TokenBreakdown,
    format_completion,
    format_footer,
)
from litellm.proxy.observability.terminal.render.rich_renderer import RichLiveRenderer


def _record() -> CompletionRecord:
    return CompletionRecord(
        CompletionStatus.OK,
        "17:18:53",
        "■",
        "7K3M",
        "anthropic",
        "claude-opus-4.8",
        "ghc",
        200,
        27.3,
        1.24,
        1_572_864,
        18_022,
        TokenBreakdown(2, 567_300, 4_700, 1_800),
        tools=("Bash", "Bash", "Read"),
        thinking=(("enc", 1),),
    )


def test_completion_golden_text() -> None:
    assert format_completion(_record()) == (
        "[ OK ] 17:18:53 ■ 7K3M anthropic/claude-opus-4.8@ghc 200 27.30s ttft:1.24s "
        "↑1.5MB ↓17.6KB ↑2+567.3k+4.7k ↻0%+99%+1% ↓1.8k tool_use(Bash,Bash,Read) think:enc(1)"
    )


def test_footer_keeps_oldest_groups_and_reports_hidden_count() -> None:
    groups = (
        InFlightGroup("anthropic", "opus", "ghc", 2, 12.4),
        InFlightGroup("responses", "gpt", "ghc", 1, 2.1),
    )
    assert "anthropic/opus" in format_footer(groups, width=100)
    narrow = format_footer(groups, width=65)
    assert "anthropic/opus" in narrow
    assert "+1 groups" in narrow


def test_rich_renderer_writes_log_and_footer_to_one_console() -> None:
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, width=80)
    renderer = RichLiveRenderer(console)
    renderer.start("[ .. ] 1 in-flight")
    renderer.log(format_completion(_record()))
    renderer.update("[ .. ] 0 in-flight")
    renderer.stop()
    rendered = output.getvalue()
    assert "1 in-flight" in rendered
    assert "0 in-flight" in rendered
    assert "tool_use(Bash,Bash,Read)" in rendered


def test_logging_adapter_restores_original_handlers() -> None:
    logger = logging.getLogger("terminal-render-test")
    original = logging.NullHandler()
    replacement = logging.NullHandler()
    logger.handlers = [original]
    logger.propagate = True
    snapshot = install_handler(logger, replacement)
    assert logger.handlers == [replacement]
    assert logger.propagate is False
    restore_handler(snapshot)
    assert logger.handlers == [original]
    assert logger.propagate is True
