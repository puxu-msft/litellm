from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FinalToolUse:
    name: str


@dataclass(frozen=True, slots=True)
class FinalThinking:
    carrier: str


FinalSemanticBlock = FinalToolUse | FinalThinking


@dataclass(frozen=True, slots=True)
class SemanticSummary:
    tools: tuple[str, ...]
    thinking: tuple[tuple[str, int], ...]


def summarize_final_blocks(blocks: tuple[FinalSemanticBlock, ...]) -> SemanticSummary:
    tools = tuple(block.name for block in blocks if isinstance(block, FinalToolUse))
    carriers = tuple(block.carrier for block in blocks if isinstance(block, FinalThinking))
    ordered = tuple(dict.fromkeys(carriers))
    return SemanticSummary(tools, tuple((carrier, carriers.count(carrier)) for carrier in ordered))
