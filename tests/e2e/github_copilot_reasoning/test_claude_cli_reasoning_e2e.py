"""Group 3: drive the real ``claude`` CLI against the proxy and verify R1.

R1 (does Claude Code store + replay our private carrier) is a client-storage behavior no
unit test can reach. This spawns the real ``claude`` CLI on a gpt model routed through the
proxy, runs a multi-step reasoning task (so gpt reasons across turns), then inspects the
resulting Claude Code transcript and asserts the ``ghc-rsn`` carrier was stored verbatim
in a thinking-block signature, with a decodable real encrypted_content.

Marked ``claude_cli``; opt-in via LITELLM_RUN_CLAUDE_CLI=1 and ``claude`` on PATH. Assumes
the invoked ``claude`` routes the gpt model through this proxy (ANTHROPIC_BASE_URL /
ANTHROPIC_API_KEY are exported to it; adjust to your claude config if it differs).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from litellm.llms.github_copilot.reasoning_carrier import DecodedCarrier, decode_carrier

_TASK = (
    "Reason step by step, do not skip steps: a farmer has 17 sheep, all but 9 die, then he "
    "buys 5 more and half wander off; how many remain? Then run one bash command "
    "`echo <your number>` to verify, and report the final number."
)


def _project_transcripts(cwd: Path, started_at: float) -> list[Path]:
    project_slug = re.sub(r"[^A-Za-z0-9-]", "-", str(cwd.resolve()))
    project_dir = Path.home() / ".claude" / "projects" / project_slug
    if not project_dir.is_dir():
        return []
    return [
        jsonl
        for jsonl in project_dir.rglob("*.jsonl")
        if jsonl.stat().st_mtime >= started_at - 1
    ]


def _stored_carrier_signatures(cwd: Path, started_at: float) -> list[str]:
    """Collect ghc-rsn carrier signatures from any Claude Code transcript touched since
    ``started_at`` (main sessions and subagents)."""
    sigs: list[str] = []
    for jsonl in _project_transcripts(cwd, started_at):
        try:
            for line in jsonl.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                message = obj.get("message") or {}
                if message.get("role") != "assistant":
                    continue
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "thinking":
                        sig = block.get("signature") or ""
                        if isinstance(sig, str) and sig.startswith("ghc-rsn:v1:"):
                            sigs.append(sig)
        except OSError:
            continue
    return sigs


def _stored_paired_tool_uses(cwd: Path, started_at: float) -> list[dict]:
    """Return tool_use blocks whose id has a tool_result in the same transcript."""
    paired: list[dict] = []
    for jsonl in _project_transcripts(cwd, started_at):
        try:
            tool_uses: dict[str, dict] = {}
            result_ids: set[str] = set()
            for line in jsonl.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                message = obj.get("message") or {}
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                        tool_uses[block["id"]] = block
                    elif block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                        result_ids.add(block["tool_use_id"])
            paired.extend(tool_uses[tool_id] for tool_id in result_ids if tool_id in tool_uses)
        except OSError:
            continue
    return paired


@pytest.mark.claude_cli
class TestClaudeCliReasoning:
    def test_claude_cli_stores_reasoning_carrier(self, tmp_path, proxy_url, gpt_model):
        from tests.e2e.github_copilot_reasoning.conftest import MASTER_KEY  # type: ignore

        env = {
            **os.environ,
            "ANTHROPIC_BASE_URL": proxy_url,
            "ANTHROPIC_API_KEY": MASTER_KEY,
            "GHC_REASONING_DISABLE": "0",
        }
        started_at = time.time()
        proc = subprocess.run(
            ["claude", "-p", _TASK, "--model", gpt_model, "--dangerously-skip-permissions"],
            cwd=str(tmp_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=240,
        )
        assert proc.returncode == 0, f"claude CLI failed: {proc.stderr[-800:]}"

        sigs = _stored_carrier_signatures(tmp_path, started_at)
        assert sigs, (
            "Claude Code should store a ghc-rsn reasoning carrier for a gpt turn "
            "(R1). If empty, confirm the run used gpt via this proxy and triggered reasoning."
        )
        res = decode_carrier({"type": "thinking", "thinking": "", "signature": sigs[0]})
        assert isinstance(res, DecodedCarrier)
        assert len(res.envelope.encrypted_content) > 100, "stored carrier should decode to real encrypted reasoning"

        bash_calls = [
            block
            for block in _stored_paired_tool_uses(tmp_path, started_at)
            if block.get("name") == "Bash"
            and isinstance(block.get("input"), dict)
            and block["input"].get("command")
        ]
        assert bash_calls, (
            "Claude Code should store a Bash tool_use with a non-empty command "
            "and a matching tool_result in the same transcript"
        )
