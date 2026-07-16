"""孤儿 tool_use/tool_call 检测与修复 / Orphan detection & fix.

请求侧(Anthropic 格式)与转换后(OpenAI 格式)两种形态的孤儿检测,以及 OpenAI 侧的
就地补合成 tool 消息。用于 process_deployment / observe_failure 等观测/修复点。
"""
from __future__ import annotations


def find_openai_orphan_tool_calls(messages):
    """在 OpenAI 格式 messages 里找孤儿 tool_call。

    返回 [{"assistant_index": i, "missing": [id,...]}]。孤儿=某 assistant 的
    tool_calls[].id 在紧随其后的连续 role:"tool" 消息里无对应 tool_call_id。
    """
    orphans = []
    if not isinstance(messages, list):
        return orphans
    for i, m in enumerate(messages):
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        tcs = m.get("tool_calls") or []
        ids = [tc.get("id") for tc in tcs if isinstance(tc, dict) and tc.get("id")]
        if not ids:
            continue
        provided = set()
        j = i + 1
        while j < len(messages) and isinstance(messages[j], dict) and messages[j].get("role") == "tool":
            tid = messages[j].get("tool_call_id")
            if tid:
                provided.add(tid)
            j += 1
        missing = [x for x in ids if x not in provided]
        if missing:
            orphans.append({"assistant_index": i, "missing": missing})
    return orphans


def find_anthropic_orphan_tool_use(messages):
    """在 Anthropic 原生格式 messages 里找孤儿 tool_use。

    返回 [{"assistant_index": i, "missing": [id,...]}]。孤儿=某 assistant 消息
    content 里的 tool_use.id,在紧邻的下一条消息 content 里无对应
    tool_result.tool_use_id(位置感知,copilot 要求 immediately after)。
    """
    orphans = []
    if not isinstance(messages, list):
        return orphans

    def blocks(m):
        c = m.get("content") if isinstance(m, dict) else None
        return c if isinstance(c, list) else []

    for i, m in enumerate(messages):
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        ids = [
            b.get("id")
            for b in blocks(m)
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")
        ]
        if not ids:
            continue
        nxt = messages[i + 1] if i + 1 < len(messages) else None
        provided = set()
        for b in blocks(nxt) if isinstance(nxt, dict) else []:
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id"):
                provided.add(b.get("tool_use_id"))
        missing = [x for x in ids if x not in provided]
        if missing:
            orphans.append({"assistant_index": i, "missing": missing})
    return orphans


def find_orphans_any_format(messages):
    """对两种格式都探测,合并结果并标注 format。用于 failure/success 这类
    无法确定载荷格式(转换前 Anthropic / 转换后 OpenAI)的观测点。"""
    result = {}
    oa = find_openai_orphan_tool_calls(messages)
    an = find_anthropic_orphan_tool_use(messages)
    if oa:
        result["openai"] = oa
    if an:
        result["anthropic"] = an
    return result


def fix_openai_orphan_tool_calls(messages, orphans):
    """为 OpenAI 格式的孤儿 tool_call 就地补合成 role:"tool" 消息。返回补的条数。"""
    if not orphans:
        return 0
    # 从后往前插,避免索引漂移
    inserted = 0
    for o in sorted(orphans, key=lambda x: x["assistant_index"], reverse=True):
        i = o["assistant_index"]
        for tid in reversed(o["missing"]):
            messages.insert(
                i + 1,
                {
                    "role": "tool",
                    "tool_call_id": tid,
                    "content": "Tool result unavailable (synthesized to satisfy API).",
                },
            )
            inserted += 1
    return inserted
