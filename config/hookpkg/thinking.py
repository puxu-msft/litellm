"""thinking block 请求侧修复 / Request-side thinking-block fixes.

客户端(Claude Code)可能把损坏的 thinking block 排列作为历史发回,导致后端报错:
1. 连续 thinking block:同一 message 内两个 thinking 相邻不合法,中间插空格文本块。
2. 空 signature 的 thinking:signature 空是错误的
   - "to_text":转普通文本(text=thinking 内容;内容为空则删该 block;删后 content 变空
     则换空格占位文本块)。
   - "remove":整个删除该 block(删后 content 空则空格占位)。
   - "off":不动。
   注:message/thinking 内容为空是新版常态,不作错误处理(只有 signature 空才修)。
3. strip_all:一键剥离所有 thinking block(优先级最高,开了就不做 1/2)。

只改 content 为 list 的 message。redacted_thinking 视作 thinking 同类(有 data 无 signature,
strip_all 时一并剥离;empty_signature 规则不适用于它——它本就无 signature 字段)。

每个修复动作记为一条 action(kind + msg + 结果形态),末尾聚合成一行人类可读的日志摘要,
方便直接从日志看清"修了什么、结果是什么形态"。fix_thinking_blocks 返回 action 元组(供
上游/测试取用);占位补白(placeholder)算结果形态的一部分,但不计入"问题数"。
"""
from __future__ import annotations

import logging
from collections import Counter

logger = logging.getLogger("litellm.hookpkg.thinking")

_THINKING_TYPES = ("thinking", "redacted_thinking")
# placeholder 是补白,不是被修复的"问题",不计入问题数
_NON_ISSUE_KINDS = ("placeholder",)


def _is_thinking(b):
    return isinstance(b, dict) and b.get("type") in _THINKING_TYPES


def _placeholder():
    return {"type": "text", "text": " "}


def _summarize_actions(actions):
    """把 action 记录聚合成一行人类可读摘要(供日志)。

    形如:  →text×2 (312c), →dropped×1 (empty), consecutive-split×1, stripped×3 (2 thinking+1 redacted_thinking); msgs=[0,2,5]
    每段都带结果形态:转文本给出总字符数,删除给出原因(空内容/remove 模式),剥离给出类型细分。
    """
    kinds = Counter(a["kind"] for a in actions)
    parts = []

    n_to_text = kinds.get("to_text", 0)
    if n_to_text:
        chars = sum(a["chars"] for a in actions if a["kind"] == "to_text")
        parts.append(f"→text×{n_to_text} ({chars}c)")

    n_dropped = kinds.get("dropped", 0)
    if n_dropped:
        modes = Counter(a["mode"] for a in actions if a["kind"] == "dropped")
        mode_str = "+".join(f"{v} {k}" for k, v in sorted(modes.items()))
        parts.append(f"→dropped×{n_dropped} ({mode_str})")

    n_consecutive = kinds.get("consecutive", 0)
    if n_consecutive:
        parts.append(f"consecutive-split×{n_consecutive}")

    n_strip = kinds.get("strip", 0)
    if n_strip:
        blocks = Counter(a["block"] for a in actions if a["kind"] == "strip")
        block_str = "+".join(f"{v} {k}" for k, v in sorted(blocks.items()))
        parts.append(f"stripped×{n_strip} ({block_str})")

    n_placeholder = kinds.get("placeholder", 0)
    if n_placeholder:
        parts.append(f"placeholder-refill×{n_placeholder}")

    msgs = sorted({a["msg"] for a in actions if a["kind"] not in _NON_ISSUE_KINDS})
    summary = ", ".join(parts)
    if msgs:
        summary = f"{summary}; msgs={msgs}"
    return summary


def fix_thinking_blocks(data, cfg):
    """按 fix_thinking 配置修复所有 message 的 thinking block。

    返回 action 元组(每条含 kind/msg 及结果形态字段)。命中问题时打一行聚合摘要日志。
    """
    ft = cfg.get("fix_thinking") or {}
    strip_all = ft.get("strip_all")
    insert_text = ft.get("insert_text", True)
    empty_sig = ft.get("empty_signature", "to_text")
    # 全关则跳过
    if not strip_all and not insert_text and empty_sig in (None, "off"):
        return ()
    messages = data.get("messages")
    if not isinstance(messages, list):
        return ()

    actions = []
    for mi, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue

        # 1) strip_all 优先:剥离所有 thinking,不做其余
        if strip_all:
            removed_blocks = [b for b in content if _is_thinking(b)]
            if removed_blocks:
                new_content = [b for b in content if not _is_thinking(b)]
                for b in removed_blocks:
                    actions.append({"kind": "strip", "msg": mi, "block": b.get("type")})
                if not new_content:
                    new_content = [_placeholder()]
                    actions.append({"kind": "placeholder", "msg": mi})
                m["content"] = new_content
            continue

        # 2) 空 signature 处理(to_text / remove)
        if empty_sig in ("to_text", "remove"):
            new_content = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "thinking" and not b.get("signature"):
                    # signature 空 = 错误。thinking 内容非空且 to_text -> 转文本;否则删。
                    text = b.get("thinking")
                    if empty_sig == "to_text" and text:
                        new_content.append({"type": "text", "text": text})
                        actions.append({"kind": "to_text", "msg": mi, "chars": len(text)})
                    else:
                        # remove 模式,或 to_text 但内容为空 -> 删除(不 append)
                        mode = "remove" if empty_sig == "remove" else "empty"
                        actions.append({"kind": "dropped", "msg": mi, "mode": mode})
                else:
                    new_content.append(b)
            if not new_content:
                new_content = [_placeholder()]
                actions.append({"kind": "placeholder", "msg": mi})
            content = new_content
            m["content"] = content

        # 3) 连续 thinking:同一 message 内相邻两个 thinking 之间插空格文本块
        if insert_text:
            fixed = []
            for i, b in enumerate(content):
                if i > 0 and _is_thinking(content[i - 1]) and _is_thinking(b):
                    fixed.append(_placeholder())
                    actions.append({"kind": "consecutive", "msg": mi})
                fixed.append(b)
            m["content"] = fixed

    issues = [a for a in actions if a["kind"] not in _NON_ISSUE_KINDS]
    if issues:
        logger.warning("hookpkg: fixed %d thinking block issue(s) on model=%r: %s",
                       len(issues), data.get("model"), _summarize_actions(actions))
    return tuple(actions)
