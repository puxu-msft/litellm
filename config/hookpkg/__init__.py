"""litellm hook 实现包入口 / Package entry.

由稳定薄壳 hooks.py 加载,导出五个官方 CustomLogger hook 入口:
  process            <- async_pre_call_hook           (转换前,原始 Anthropic 载荷)
  process_deployment <- async_pre_call_deployment_hook (转换后/发出前,OpenAI 载荷)
  observe_failure    <- async_post_call_failure_hook   (失败时)
  observe_success    <- async_post_call_success_hook   (成功后)
  stream_transform   <- async_post_call_streaming_iterator_hook (流式响应)
  log_success        <- async_log_success_event        (成功后:打一行访问日志)
  log_failure        <- async_log_failure_event        (失败后:打一行访问日志)

请求侧逻辑(process 及其辅助)保留在本文件;跨模块能力从子模块导入:
  config   配置热读     probes   审计落盘    orphans  孤儿检测/修复
  stream   流式状态机
热重载靠 SIGUSR2(见 reload.py)。
"""
from __future__ import annotations

import json
import logging
import fnmatch

from hookpkg import config as _config
from hookpkg.config import load_config
from hookpkg.probes import append_jsonl, ProbeContext
from hookpkg import orphans as _orphans
from hookpkg.thinking import fix_thinking_blocks
from hookpkg.logline import log_success, log_failure, request_started  # noqa: F401  薄壳经 impl 调用
# 薄壳通过 impl.stream_transform 调用。链路(从上游到客户端):
#   response -> [block_audit 观测入站] -> stream.stream_transform(改写) ->
#   [block_audit 观测出站] -> dedup(相邻重复 tool_use 去重) -> 客户端
# dedup 在 block_audit 之后:block_audit 仍如实记录上游/转换后的重复(dup_tools_in/out),
# dedup 在最后一层丢重复并记自己的审计,且不依赖 block_audit 是否开启。
from hookpkg.block_audit import stream_transform_audited as _stream_transform_audited  # noqa: E402
from hookpkg.dedup import dedup_adjacent_tool_use as _dedup_adjacent_tool_use  # noqa: E402


async def stream_transform(response, request_data):
    async for chunk in _dedup_adjacent_tool_use(
        _stream_transform_audited(response, request_data), request_data
    ):
        yield chunk

logger = logging.getLogger("litellm.hookpkg")

_DEFAULT_CONFIG = _config._DEFAULT_CONFIG

# 去重已观测过的 litellm_call_id(重试幂等,避免重复落盘)。有上限,溢出即清空。
_seen_deployment_ids = set()


def _has_usable_tools(data):
    tools = data.get("tools")
    return isinstance(tools, list) and len(tools) > 0


def _tool_name(t):
    return t.get("name") or (t.get("function") or {}).get("name")


def _probe(data, call_type, cfg):
    probe = cfg.get("probe") or {}
    if not probe.get("enabled"):
        return
    try:
        tools = data.get("tools")
        summary = None
        if isinstance(tools, list):
            summary = [
                {"keys": sorted(t.keys()), "name": _tool_name(t)}
                for t in tools
                if isinstance(t, dict)
            ]
        rec = {
            "model": data.get("model"),
            "call_type": call_type,
            "has_tool_choice": "tool_choice" in data,
            "tool_choice": data.get("tool_choice")
            if not isinstance(data.get("tool_choice"), dict)
            else "dict",
            "n_tools": len(tools) if isinstance(tools, list) else None,
            "tools": summary,
        }
        with open(probe.get("file", _DEFAULT_CONFIG["probe"]["file"]), "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        if probe.get("dump_messages"):
            path = probe.get("dump_file", "/tmp/litellm-messages.jsonl")
            with open(path, "a") as f:
                f.write(json.dumps(data.get("messages"), ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        logger.warning("hookpkg probe failed: %r", e)


def _inject_tools(data, cfg):
    inj = cfg.get("inject_tools") or {}
    if not inj.get("enabled"):
        return
    extra = inj.get("tools") or []
    if not extra:
        return
    model = data.get("model") or ""
    needle = inj.get("model_contains") or ""
    if needle and needle not in model:
        return
    if inj.get("only_if_tools_present", True) and not _has_usable_tools(data):
        return
    existing = data.get("tools")
    if not isinstance(existing, list):
        existing = []
    present = {_tool_name(t) for t in existing if isinstance(t, dict)}
    added = []
    for t in extra:
        if isinstance(t, dict) and _tool_name(t) not in present:
            existing.append(t)
            added.append(_tool_name(t))
    if added:
        data["tools"] = existing
        logger.warning("hookpkg: injected tools %r into model=%r", added, model)


def _fix_tool_choice(data, call_type, cfg):
    if not cfg.get("fix_tool_choice", True):
        return
    if "tool_choice" in data and not _has_usable_tools(data):
        removed = data.pop("tool_choice", None)
        if "tools" in data and not _has_usable_tools(data):
            data.pop("tools", None)
        logger.warning(
            "hookpkg: dropped tool_choice=%r on model=%r (call_type=%r), no usable tools",
            removed,
            data.get("model"),
            call_type,
        )


def _strip_cache_control_scope(data, cfg):
    """删除请求中所有 cache_control 对象的 scope 字段。

    Anthropic 的 cache_control 允许携带 `scope` 字段(如按上下文分区),但部分上游
    后端不接受它。这里递归遍历整个请求体,凡是形如 {"type": "ephemeral", ...} 的
    cache_control 字典,一律删掉其中的 scope 键。
    """
    if not cfg.get("strip_cache_control_scope", True):
        return
    n = [0]

    def walk(obj):
        if isinstance(obj, dict):
            cc = obj.get("cache_control")
            if isinstance(cc, dict) and "scope" in cc:
                cc.pop("scope", None)
                n[0] += 1
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(data)
    if n[0]:
        logger.warning(
            "hookpkg: stripped cache_control.scope from %d block(s) on model=%r",
            n[0],
            data.get("model"),
        )


def _strip_unsupported_top_level(data, cfg):
    """剥离 copilot 原生 /v1/messages 端点不接受的顶层字段。
    走原生 endpoint 后,客户端带的某些 Anthropic 新特性字段(如 context_management)
    copilot 后端还不支持,会报 "Extra inputs are not permitted"。"""
    keys = cfg.get("strip_unsupported_top_level")
    if not keys or not isinstance(data, dict):
        return
    removed = []
    for k in keys:
        if k in data:
            data.pop(k, None)
            removed.append(k)
    if removed:
        logger.warning("hookpkg: stripped unsupported top-level fields %r on model=%r",
                       removed, data.get("model"))


def _strip_beta_by_model(data, cfg):
    """按模型剥离特定 anthropic-beta 值。作用于 provider_specific_header.extra_headers 里
    的 anthropic-beta(litellm 走原生 endpoint 时透传的 beta 载体)。model 支持 glob。"""
    rules = cfg.get("strip_beta_by_model")
    if not rules or not isinstance(data, dict):
        return
    model = data.get("model") or ""
    # 兼容带 provider 前缀的 model 名(github_copilot/claude-opus-4.8)——也用 base 名比对。
    base = model.split("/", 1)[1] if "/" in model else model
    # 收集本模型要剥的 beta 值
    to_strip = set()
    for pat, betas in rules.items():
        if pat in (model, base) or fnmatch.fnmatchcase(model, pat) or fnmatch.fnmatchcase(base, pat):
            for b in (betas or []):
                to_strip.add(b)
    if not to_strip:
        return
    psh = data.get("provider_specific_header")
    if not isinstance(psh, dict):
        return
    extra = psh.get("extra_headers")
    if not isinstance(extra, dict):
        return
    beta_str = extra.get("anthropic-beta")
    if not beta_str:
        return
    kept = [b.strip() for b in beta_str.split(",") if b.strip() and b.strip() not in to_strip]
    removed = [b.strip() for b in beta_str.split(",") if b.strip() in to_strip]
    if removed:
        if kept:
            extra["anthropic-beta"] = ",".join(kept)
        else:
            extra.pop("anthropic-beta", None)
        logger.warning("hookpkg: stripped beta %r for model=%r", removed, model)


def _fix_orphan_tool_use(data, cfg):
    """为“有 tool_use 但缺 tool_result”的孤儿补上合成的 tool_result。

    Anthropic 要求:某条 assistant 消息里的每个 `tool_use` 块,都必须在**紧接着的
    下一条 user 消息**里有对应 id 的 `tool_result` 块,否则报
    "tool_use ids were found without tool_result blocks immediately after"。

    截断历史、代理丢块、消息错位都可能造成孤儿。修复对两种 messages 格式都生效
    (Anthropic 原生 content 块 / OpenAI 的 tool_calls + role:"tool")。关键是**位置
    感知**:结果只在“紧接着的下一条消息”里查找,即使同 id 的结果存在于更靠后的位置
    也照样补合成结果——因为 copilot 后端要求的正是相邻。
    """
    if not cfg.get("fix_orphan_tool_use", True):
        return 0
    messages = data.get("messages")
    if not isinstance(messages, list):
        return 0

    def blocks(msg):
        c = msg.get("content") if isinstance(msg, dict) else None
        return c if isinstance(c, list) else []

    def results_in(msg):
        """返回某条消息(作为“下一条”)所提供的 tool_result / tool_call id 集合。"""
        ids = set()
        if not isinstance(msg, dict):
            return ids
        if msg.get("role") == "tool":  # OpenAI: 单条 tool 消息
            tid = msg.get("tool_call_id")
            if tid:
                ids.add(tid)
        for b in blocks(msg):  # Anthropic: user 消息里的 tool_result 块
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tid = b.get("tool_use_id")
                if tid:
                    ids.add(tid)
        return ids

    inserted = 0
    i = 0
    while i < len(messages):
        msg = messages[i]
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            i += 1
            continue

        # 结果必须来自“紧接着的下一条消息”(copilot 强调 immediately after)。
        # OpenAI 格式下,一次 assistant 的多个 tool_call 会由多条连续 tool 消息回应,
        # 故把紧随其后的连续 tool 消息都算作已满足。
        provided = set()
        j = i + 1
        if j < len(messages):
            provided |= results_in(messages[j])
            while j < len(messages) and isinstance(messages[j], dict) and messages[j].get("role") == "tool":
                provided |= results_in(messages[j])
                j += 1

        # Anthropic 风格 tool_use 块
        anthropic_orphans = [
            b.get("id")
            for b in blocks(msg)
            if isinstance(b, dict)
            and b.get("type") == "tool_use"
            and b.get("id")
            and b.get("id") not in provided
        ]
        # OpenAI 风格 tool_calls
        tool_calls = msg.get("tool_calls")
        openai_orphans = []
        if isinstance(tool_calls, list):
            openai_orphans = [
                tc.get("id")
                for tc in tool_calls
                if isinstance(tc, dict) and tc.get("id") and tc.get("id") not in provided
            ]

        if not anthropic_orphans and not openai_orphans:
            i += 1
            continue

        nxt = messages[i + 1] if i + 1 < len(messages) else None

        if anthropic_orphans:
            synthetic = [
                {
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "content": "Tool result unavailable (synthesized to satisfy API).",
                    "is_error": True,
                }
                for tid in anthropic_orphans
            ]
            if (
                isinstance(nxt, dict)
                and nxt.get("role") == "user"
                and isinstance(nxt.get("content"), list)
            ):
                nxt["content"][0:0] = synthetic
            else:
                messages.insert(i + 1, {"role": "user", "content": synthetic})
            inserted += len(anthropic_orphans)

        if openai_orphans:
            # OpenAI 要求每个 tool_call 紧跟独立的 role:"tool" 消息。倒序插入以保持顺序。
            for tid in reversed(openai_orphans):
                messages.insert(
                    i + 1,
                    {
                        "role": "tool",
                        "tool_call_id": tid,
                        "content": "Tool result unavailable (synthesized to satisfy API).",
                    },
                )
            inserted += len(openai_orphans)

        i += 1

    if inserted:
        logger.warning(
            "hookpkg: synthesized %d tool_result block(s) for orphan tool_use on model=%r",
            inserted,
            data.get("model"),
        )
    return inserted


def _dump_orphan_evidence(pre_fix_messages, n_orphans, data, cfg):
    """仅当检测到孤儿时,把修复前的 messages 留一份证据(便于事后核验)。"""
    probe = cfg.get("probe") or {}
    if not probe.get("dump_orphans_only") or not n_orphans:
        return
    try:
        path = probe.get("orphan_dump_file", "/tmp/litellm-orphans.jsonl")
        rec = {
            "model": data.get("model"),
            "n_orphans": n_orphans,
            "messages": pre_fix_messages,
        }
        with open(path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        logger.warning("hookpkg orphan-dump failed: %r", e)


def _dump_toolref(data, cfg):
    """探测 ToolSearch/MCP 机制:抓 tool_reference 块、beta 头、MCP 工具概况。
    仅当 probe.dump_toolref 开启时生效。每类首次出现即落盘,便于定位
    litellm 是否丢弃了 tool_reference。"""
    probe = cfg.get("probe") or {}
    if not probe.get("dump_toolref"):
        return
    try:
        path = probe.get("toolref_file", "/tmp/litellm-toolref.jsonl")
        tools = data.get("tools") or []
        # 统计工具类型分布
        type_hist = {}
        mcp_names = []
        toolref_blocks = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            ttype = t.get("type", "<none>")
            type_hist[ttype] = type_hist.get(ttype, 0) + 1
            name = _tool_name(t)
            if isinstance(name, str) and name.startswith("mcp__"):
                mcp_names.append(name)
            if ttype == "tool_reference" or "tool_reference" in str(ttype):
                toolref_blocks.append(t)
        # betas / tool-search 相关头
        betas = data.get("betas") or data.get("anthropic_beta")
        # system 里可能藏 tool-search 提示
        rec = {
            "model": data.get("model"),
            "n_tools": len(tools),
            "type_hist": type_hist,
            "n_mcp": len(mcp_names),
            "mcp_sample": mcp_names[:8],
            "n_toolref": len(toolref_blocks),
            "toolref_sample": toolref_blocks[:3],
            "betas": betas,
            "provider_specific_header": data.get("provider_specific_header"),
            "proxy_server_request_headers": (data.get("proxy_server_request") or {}).get("headers")
            if isinstance(data.get("proxy_server_request"), dict) else None,
            "extra_keys": sorted(k for k in data.keys()
                                 if k not in ("model", "messages", "tools", "tool_choice")),
        }
        with open(path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        logger.warning("hookpkg toolref-dump failed: %r", e)


def _diag_thinking_blocks(data, ctx):
    """诊断:抓 thinking block 的真实 wire 结构与排列,供设计请求侧修复。
    只在 probe_only 时落盘,记录:每个含 thinking 的 message 的 block 类型序列、
    thinking block 的字段名、signature 是否为空、是否有连续 thinking。"""
    messages = data.get("messages")
    if not isinstance(messages, list):
        return
    for mi, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        types = [b.get("type") for b in content if isinstance(b, dict)]
        if "thinking" not in types and "redacted_thinking" not in types:
            continue
        # 找连续 thinking
        consecutive = any(
            types[i] in ("thinking", "redacted_thinking") and
            types[i + 1] in ("thinking", "redacted_thinking")
            for i in range(len(types) - 1))
        thinking_samples = []
        for b in content:
            if isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking"):
                thinking_samples.append({
                    "keys": sorted(b.keys()),
                    "sig_empty": not b.get("signature"),
                    "thinking_empty": not b.get("thinking"),
                    "sig_head": (b.get("signature") or "")[:20],
                })
        ctx.diag("thinking_block", msg_index=mi, role=m.get("role"),
                 block_types=types, consecutive_thinking=consecutive,
                 samples=thinking_samples[:3])


def _orphan_pre_fix_snapshot(msgs, cfg):
    """修复前的 messages 深拷贝快照——**仅当**开启 dump_orphans_only 且确有孤儿时才取,否则 None。

    背景:此快照只在 _dump_orphan_evidence 里(n_orphans>0 时)用到。旧代码无条件对每个请求
    `json.loads(json.dumps(msgs))` 整份历史,在大上下文(~600KB)上占 process() 的 71%(~2.8ms)
    且分配一份全量副本——而孤儿极罕见,99%+ 请求这份快照当场丢弃。改为先只读检测,确有孤儿才快照。

    find_orphans_any_format 与 _fix_orphan_tool_use 同为位置感知("紧接下一条")判定,作门控足够:
    极端下漏判仅少一次证据 dump、误判(fix 实得 n=0)时 _dump_orphan_evidence 自会跳过——两向都安全。
    """
    probe = cfg.get("probe") or {}
    if not probe.get("dump_orphans_only") or not isinstance(msgs, list):
        return None
    if not _orphans.find_orphans_any_format(msgs):
        return None
    return json.loads(json.dumps(msgs, default=str))


def _tool_result_output_text(content) -> str:
    """把 Anthropic tool_result 的 content(str / text-block 列表 / None)抽成纯文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
        )
    return str(content)


def _find_orphan_tool_results(messages):
    """孤儿 tool_result:其 tool_use_id 在整个请求里无匹配的 tool_use。返回 [(mi, bi, tid), ...]。

    翻译成 Responses API 后即 function_call_output 无配对 function_call,copilot /responses 报
    "No tool call found for function call output with call_id ..."。内联于 __init__(而非
    orphans.py)——orphans 不在 SIGUSR2 RELOAD_ORDER,内联保证改动热重载即时生效。"""
    if not isinstance(messages, list):
        return []

    def blocks(m):
        c = m.get("content") if isinstance(m, dict) else None
        return c if isinstance(c, list) else []

    known = {
        b.get("id")
        for m in messages
        for b in blocks(m)
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")
    }
    return [
        (mi, bi, b.get("tool_use_id"))
        for mi, m in enumerate(messages)
        for bi, b in enumerate(blocks(m))
        if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") and b.get("tool_use_id") not in known
    ]


def _fix_orphan_tool_result(data, cfg) -> int:
    """按配置策略处理孤儿 tool_result(tool_use_id 无匹配 tool_use)。返回处理的块数。

    作用于 async_pre_call_hook 阶段的 Anthropic `messages`:anthropic_messages→responses 走
    异步 aresponses,不应用 deployment hook,只有此处对 data["messages"] 的改写会传播到
    responses 翻译(已实测)。

    strategy(hooks.config.json 的 orphan_tool_result.strategy,热读可即时切换):
      passthrough(默认,不动,留给上游拒)/ drop(删块,块删空的消息整条丢弃)/
      text(把 tool_result 转成带 tag 的代码块 text 块,保留内容而不破坏请求)。
    可选 model_contains 过滤(留空=所有模型)。只影响孤儿,配对的 tool_result 永不受影响。
    原地 mutate(slice 赋值)以传播到 litellm 持有的同一 messages 引用。
    """
    otr = cfg.get("orphan_tool_result") or {}
    strategy = (otr.get("strategy") or "passthrough").strip().lower()
    if strategy not in ("drop", "text"):
        return 0  # passthrough / 未知 -> 不动
    needle = otr.get("model_contains") or ""
    if needle and needle not in (data.get("model") or ""):
        return 0
    messages = data.get("messages")
    orphans = _find_orphan_tool_results(messages)
    if not orphans:
        return 0

    by_msg: dict = {}
    for mi, bi, tid in orphans:
        by_msg.setdefault(mi, {})[bi] = tid

    new_messages = []
    for mi, m in enumerate(messages):
        if mi not in by_msg or not isinstance(m.get("content"), list):
            new_messages.append(m)
            continue
        repl = by_msg[mi]
        new_content = []
        for bi, b in enumerate(m["content"]):
            if bi not in repl:
                new_content.append(b)
            elif strategy == "text":
                new_content.append(
                    {"type": "text", "text": f"```tool_result call_id={repl[bi]}\n{_tool_result_output_text(b.get('content'))}\n```"}
                )
            # drop: 跳过该块
        if new_content:  # 删空的消息整条丢弃(空 content 无意义且可能被拒)
            new_messages.append({**m, "content": new_content})
    messages[:] = new_messages
    logger.warning(
        "hookpkg: rewrote %d orphan tool_result block(s) via strategy=%r on model=%r",
        len(orphans), strategy, data.get("model"),
    )
    return len(orphans)



def process(data: dict, call_type: str) -> dict:
    """薄壳调用的入口。"""
    request_started(data, call_type)
    cfg = load_config()
    # 诊断:记录每个请求的 stream 标志与工具名单,判定 AskUserQuestion 走不走流式。
    sf = cfg.get("stream_fix") or {}
    _ctx = ProbeContext.from_stream_fix(sf, model=data.get("model"),
                                        call_id=data.get("litellm_call_id"))
    if _ctx.diag_enabled:
        try:
            tools = data.get("tools")
            names = [_tool_name(t) for t in tools if isinstance(t, dict)] if isinstance(tools, list) else []
            _ctx.diag("request_seen", stream=data.get("stream"), call_type=call_type,
                      has_AskUserQuestion="AskUserQuestion" in names, n_tools=len(names))
            # 诊断:请求是否带 thinking/reasoning 参数(判定"看不到 thinking"是否因请求没开启)
            _ctx.diag("request_thinking_params",
                      thinking=data.get("thinking"),
                      reasoning_effort=data.get("reasoning_effort"),
                      has_thinking_key="thinking" in data,
                      keys=sorted(k for k in data.keys()
                                  if k not in ("messages", "tools", "model")))
            # 诊断:抓 thinking block 的真实结构(字段、signature、相邻排列),供设计修复。
            _diag_thinking_blocks(data, _ctx)
            # 诊断:anthropic-beta header 的真实载体与内容(定位 beta 透传/剥离点)
            psh = data.get("provider_specific_header")
            psr = data.get("proxy_server_request")
            psr_headers = psr.get("headers") if isinstance(psr, dict) else None
            _ctx.diag("beta_headers",
                      provider_specific_header=psh,
                      psr_anthropic_beta=(psr_headers or {}).get("anthropic-beta") if isinstance(psr_headers, dict) else None,
                      top_anthropic_beta=data.get("anthropic_beta") or data.get("betas"),
                      has_context_management="context_management" in data)
        except Exception:
            pass
    _dump_toolref(data, cfg)
    _probe(data, call_type, cfg)
    _inject_tools(data, cfg)
    _fix_tool_choice(data, call_type, cfg)
    _strip_cache_control_scope(data, cfg)
    _strip_unsupported_top_level(data, cfg)
    _strip_beta_by_model(data, cfg)
    fix_thinking_blocks(data, cfg)
    # 修复前快照:仅当确有孤儿时才取(见 _orphan_pre_fix_snapshot),避免每请求整份历史深拷贝。
    pre = _orphan_pre_fix_snapshot(data.get("messages"), cfg)
    n = _fix_orphan_tool_use(data, cfg)
    if pre is not None:
        _dump_orphan_evidence(pre, n, data, cfg)
    # 反方向孤儿(tool_result 无匹配 tool_use → function_call_output 无 function_call)。
    # anthropic_messages→responses 异步路径不应用 deployment hook,只能在此(Anthropic 格式,
    # async_pre_call_hook)改——已实测此处对 data["messages"] 的改写会传播到 responses 翻译。
    _fix_orphan_tool_result(data, cfg)
    return data
def process_deployment(kwargs: dict, call_type):
    """转换后、发出前(deployment 已选定)。返回修改后的 kwargs 或 None。

    这是 async_pre_call_hook 看不到的盲区:对 github_copilot 等 OpenAI 系 provider,
    此处 kwargs['messages'] 已是 Anthropic→OpenAI 转换后的载荷。
    """
    cfg = load_config()
    dp = cfg.get("deployment_probe") or {}
    if not dp.get("enabled"):
        return None
    try:
        model = kwargs.get("model") or ""
        needle = dp.get("model_contains") or ""
        if needle and needle not in model:
            return None
        messages = kwargs.get("messages")
        orphans = _orphans.find_openai_orphan_tool_calls(messages)

        # 去重:completion_with_retries 会对同一 litellm_call_id 重跑本 hook,
        # 避免同一请求的观测被重复落盘(仅去重 dump,不影响 fix)。
        call_id = kwargs.get("litellm_call_id")
        dedup = call_id and call_id in _seen_deployment_ids
        if call_id and not dedup:
            _seen_deployment_ids.add(call_id)
            if len(_seen_deployment_ids) > 4096:
                _seen_deployment_ids.clear()

        if dp.get("dump_all") and not dedup:
            append_jsonl(dp.get("dump_file", "/tmp/litellm-deployment.jsonl"), {
                "model": model,
                "call_type": str(call_type),
                "n_msgs": len(messages) if isinstance(messages, list) else None,
                "n_orphans": len(orphans),
                "messages": messages,
            })

        if orphans and dp.get("orphan_only", True) and not dedup:
            append_jsonl(dp.get("file", "/tmp/litellm-deployment-orphans.jsonl"), {
                "model": model,
                "call_type": str(call_type),
                "orphans": orphans,
                "n_msgs": len(messages) if isinstance(messages, list) else None,
                "messages": messages,
            })

        if orphans and dp.get("fix_orphans"):
            n = _orphans.fix_openai_orphan_tool_calls(messages, orphans)
            if n:
                logger.warning(
                    "hookpkg(deployment): synthesized %d tool_result(s) for orphan tool_call on model=%r",
                    n, model,
                )
                return kwargs
    except Exception as e:
        logger.warning("hookpkg process_deployment failed: %r", e)
    return None


def observe_failure(request_data: dict, original_exception, traceback_str=None):
    """失败时观测。只落盘,不改写。"""
    cfg = load_config()
    fp = cfg.get("failure_probe") or {}
    if not fp.get("enabled"):
        return
    try:
        exc_str = str(original_exception)
        needle = fp.get("match_exception_contains") or ""
        if needle and needle not in exc_str:
            return
        messages = request_data.get("messages") if isinstance(request_data, dict) else None
        record = {
            "model": (request_data or {}).get("model"),
            "exception": exc_str[:2000],
            "traceback": (traceback_str or "")[:4000],
            "n_msgs": len(messages) if isinstance(messages, list) else None,
            "orphans": _orphans.find_orphans_any_format(messages),
            "messages": messages,
        }
        # 完整请求快照:供离线重放到 /responses(replay 需 system/tools/thinking/tool_choice
        # /max_tokens 等,不止 messages)。仅在 dump_full_request 时落盘,避免默认体积膨胀。
        if fp.get("dump_full_request") and isinstance(request_data, dict):
            skip = {"metadata", "litellm_metadata", "proxy_server_request", "litellm_logging_obj"}
            record["request_full"] = {k: v for k, v in request_data.items() if k not in skip}
        append_jsonl(fp.get("file", "/tmp/litellm-failures.jsonl"), record)
    except Exception as e:
        logger.warning("hookpkg observe_failure failed: %r", e)


def observe_success(data: dict, response):
    """成功后观测。只落盘,不改写。"""
    cfg = load_config()
    sp = cfg.get("success_probe") or {}
    if not sp.get("enabled"):
        return
    try:
        messages = data.get("messages") if isinstance(data, dict) else None
        append_jsonl(sp.get("file", "/tmp/litellm-success.jsonl"), {
            "model": (data or {}).get("model"),
            "n_msgs": len(messages) if isinstance(messages, list) else None,
            "orphans": _orphans.find_orphans_any_format(messages),
        })
    except Exception as e:
        logger.warning("hookpkg observe_success failed: %r", e)
