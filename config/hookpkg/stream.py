"""stream_transform 状态机 / Streaming rewrite state machine.

改写发往客户端的 Anthropic SSE 流(chunk 多为 SSE bytes,见 sse.py)。职责:
- 匹配工具的 tool_use 块:缓冲分片 JSON -> 宽松解析(json/unicode 修复) -> 四类修复
  pipeline(fixes.apply_item_fixes) -> 重发。
- 泄漏 <invoke> 转换(convert_text_invoke):text block 拆成 text+tool_use、传播
  index 偏移、改 stop_reason、白名单(glob)防误伤。
- 各种异常/截断兜底:异常必闭合块、不 raise 到薄壳。
业务逻辑不变,仅从旧 hook_impl.stream_transform 迁入并改用包内模块。
"""
from __future__ import annotations

import json
import logging

from hookpkg.config import load_config, default_degen_trim
from hookpkg.probes import ProbeContext
from hookpkg.sse import sse_parse, sse_serialize, is_truncated_json_frame
from hookpkg.fixes import apply_item_fixes
from hookpkg.fixes.json_repair import loads_lenient
from hookpkg.degen import fold_degenerate, LiveDedup
from hookpkg.invoke_convert import (
    emit_plain_text_block,
    extract_invoke_from_text,
    name_in_whitelist,
    synth_tool_use_events,
    text_maybe_has_invoke,
)

logger = logging.getLogger("litellm.hookpkg.stream")


def _text_delta_event(index, text, template):
    """构造一个 content_block_delta(text_delta)的已序列化 chunk。用于 live 去重按段实时外发。"""
    return sse_serialize(
        {"type": "content_block_delta", "index": index,
         "delta": {"type": "text_delta", "text": text}}, template)


def _tool_out_integrity(input_obj, rule):
    """给一个补全工具的**最终出站 input** 做完整性摘要,供 catch-all 审计(生产也记)。
    只落结构信号、不含敏感全文:items_key 的实际类型、条目数、以及哪些条目里补全目标字段
    (copy_within_items[].dst)仍为空。missing 非空 = 这条流发给客户端时仍缺必填字段 ->
    客户端会报 `<dst> is missing`。修复健康时 missing 恒空,一旦有行即回归警报。"""
    items_key = (rule or {}).get("items_key")
    dsts = tuple(cr.get("dst") for cr in (rule or {}).get("copy_within_items") or [] if cr.get("dst"))
    items = input_obj.get(items_key) if isinstance(input_obj, dict) else None
    if not isinstance(items, list):
        return {"items_key": items_key, "items_type": type(items).__name__,
                "n_items": None, "missing": None}
    missing = [
        {"i": i, "not_dict": True} if not isinstance(it, dict)
        else {"i": i, "empty": [d for d in dsts if not it.get(d)], "keys": sorted(it.keys())}
        for i, it in enumerate(items)
        if not isinstance(it, dict) or any(not it.get(d) for d in dsts)
    ]
    return {"items_key": items_key, "items_type": "list", "n_items": len(items), "missing": missing}


async def _reassemble_sse_frames(response, ctx=None):
    """把上游按网络 chunk 边界产出的 SSE bytes/str 重组成「一 chunk 一完整帧」。

    上游(github_copilot 双重转换 + httpx 分块)不保证 chunk 对齐 SSE 帧边界:一个
    `event: X\\ndata: {...}\\n\\n` 帧可能被劈到相邻两个 chunk,或多帧挤进一个 chunk。逐
    chunk 的 sse_parse 只认单帧、且只取第一个 data 行 → 半截帧丢内容、多帧丢后续,甚至半截
    `event:` 泄漏给客户端报 `JSON Parse error: Unexpected identifier "event"`。这里维护一个
    字节 carry:累积到完整帧(以 \\n\\n 结尾)才逐帧下发,尾部不完整的半截留到下一个 chunk 拼
    接。dict chunk(已是解析后事件,非 SSE 文本)原样透传。流末尾残留的半截(真·截断)照原样
    下发,交由下游 is_truncated_json_frame 丢弃。

    ``reassembled_split_frame`` 审计(n=拼接发生的 chunk 次数)是「新代码已生效」的正向可观测
    量:真实流量上一旦出现,即证明本重组逻辑 live 且在干活(对抗 SIGUSR2 静默失效)。
    """
    carry = b""
    emit_str = False
    stitched = 0
    async for chunk in response:
        if carry and not isinstance(chunk, dict):
            # 上一个 chunk 结尾留下了半截帧,本 chunk 在续接它 → 记一次拼接。
            stitched += 1
        if isinstance(chunk, (bytes, bytearray)):
            carry += bytes(chunk)
        elif isinstance(chunk, str):
            carry += chunk.encode("utf-8")
            emit_str = True
        else:
            # 非 SSE 文本(dict 等):先吐掉 carry 保序,再透传本 chunk。
            if carry:
                yield carry.decode("utf-8", "replace") if emit_str else carry
                carry = b""
            yield chunk
            continue
        while b"\n\n" in carry:
            frame, carry = carry.split(b"\n\n", 1)
            full = frame + b"\n\n"
            yield full.decode("utf-8", "replace") if emit_str else full
    if carry:
        yield carry.decode("utf-8", "replace") if emit_str else carry
    if stitched and ctx is not None:
        ctx.audit("reassembled_split_frame", n=stitched)


async def stream_transform(response, request_data):
    """async 生成器:改写流式 chunk。默认注册在薄壳里,但仅在 stream_fix.enabled
    时真正介入;否则原样透传。"""
    cfg = load_config()
    sf = cfg.get("stream_fix") or {}
    if not sf.get("enabled"):
        async for chunk in response:
            yield chunk
        return

    tools_cfg = sf.get("tools") or {}
    probe_only = sf.get("probe_only")
    convert_invoke = sf.get("convert_text_invoke") and not probe_only
    convert_whitelist = set(sf.get("convert_text_invoke_tools") or [])
    # 退化重复裁剪:逐参数从 config.default_degen_trim() 兜底(单一真相源,防 drift)。
    degen = sf.get("degen_trim") or {}
    _dt_def = default_degen_trim()
    degen_on = bool(degen.get("enabled")) and not probe_only
    degen_kwargs = {
        "min_run": degen.get("min_run", _dt_def["min_run"]),
        "max_seg_len": degen.get("max_seg_len", _dt_def["max_seg_len"]),
        "line_min_run": degen.get("line_min_run", _dt_def["line_min_run"]),
        "line_max_seg_len": degen.get("line_max_seg_len", _dt_def["line_max_seg_len"]),
        "notice": degen.get("notice", _dt_def["notice"]),
    }
    # text block 缓冲现**无条件启用**(不再由 convert_invoke 门控):所有 text block 都缓冲
    # 到 content_block_stop 再统一处理,但只缓冲单块、不跨 block。convert_invoke / degen_on
    # 只决定「在 stop 折叠点做不做各自处理」。代价:stream_fix 常开即全局按 block 成段
    # (非流式,用户已明确接受)。详见 docs/plan/degeneration-trim.md。
    #
    # live 去重模式(degen_trim.mode=="live"):对 text block 改走**边流边去重**分支(不全缓冲),
    # 有效内容实时外发、退化重复实时抑制、模型恢复即续流(见 degeneration-cutoff.md 决策记录)。
    # live 与 convert_invoke 互斥(后者需整块缓冲),故 live_mode 要求 convert_invoke 关。
    degen_mode = degen.get("mode", _dt_def.get("mode", "buffered"))
    live_mode = degen_on and degen_mode == "live" and not convert_invoke
    # 显式告警:请求了 live 但 convert_invoke 开着 → live 被互斥禁用、回落 buffered(非静默)。
    if degen_on and degen_mode == "live" and convert_invoke:
        logger.warning(
            "stream_fix: degen_trim.mode=live requested but convert_text_invoke is on "
            "(mutually exclusive; live needs full-block buffering off) -> using buffered fold")
    # live_mode 时 text 走流式分支,不启用整块缓冲;否则维持无条件全缓冲。
    text_buffer_on = not live_mode
    # 统一可观测:audit(修复动作,生产也记) / diag(诊断,仅 probe_only)。
    ctx = ProbeContext.from_stream_fix(sf, model=(request_data or {}).get("model"),
                                       call_id=(request_data or {}).get("litellm_call_id"))

    # 诊断:证明 stream_transform 确实被 litellm 调用(hook 已进链路)。
    ctx.diag("stream_transform_entered")

    # 泄漏转换的跨流状态(偏移量模型 B):
    #   index_shift: 注入 tool_use 后,后续所有 block 的 index 需 +此值。
    #   injected:    本响应发生过泄漏转换 -> message_delta 时改 stop_reason=tool_use。
    index_shift = 0
    injected = False
    saw_message_delta = False   # 是否见过 message_delta(用于异常截断时补发)
    # text block 缓冲(无条件启用):累积 text 到 content_block_stop 才统一处理(退化折叠 /
    # invoke 提取)。缓冲期间 start/delta 都不发。
    tbuf = False
    tbuf_start_ev = None
    tbuf_texts = []
    tbuf_template = None

    # live 去重状态(仅 live_mode 生效):每个 text block 一个 LiveDedup 实例;边流边外发。
    live = None            # 当前 text block 的 LiveDedup 实例(None=未在 live text block 内)
    live_idx = 0           # 当前 live text block 的原始 index
    live_template = None   # 该 block 的 wire 模板(用于 flush/收尾时序列化)
    live_audited = False   # 本 block 是否已记 degen_live_folded 审计(避免重复)

    def _shift_ev(e, template):
        """给一个 content_block_* 事件的 index 加上 index_shift 后重新序列化。
        index_shift 为 0 时返回 None(表示无需改写,可字节透传原 chunk)。"""
        if index_shift == 0 or not isinstance(e, dict):
            return None
        if "index" not in e:
            return None
        e2 = dict(e)
        e2["index"] = e.get("index", 0) + index_shift
        return sse_serialize(e2, template)

    def _live_close_events():
        """live 块**异常收尾**:flush 尾段 + 补一个 content_block_stop,返回要 yield 的 chunk 列表。
        调用方负责 yield 这些并置 live=None。用于所有「live 块未闭合就退出」的路径(message_delta 早到、
        新 block start、非 text_delta、流末、异常)。仅发尾段不补 stop 会生成非法 SSE(未闭合 block),
        故这里统一 flush+stop。正常收到上游 content_block_stop 时不走这里(转发真 stop 即闭合)。"""
        if live is None:
            return ()
        outs = []
        try:
            rem = live.flush()
        except Exception:  # noqa: BLE001 - flush 抛也绝不丢内容:flush 已 pending-事务化(抛时 _pending
            # 未清),用不会抛的 drain_raw 原样取回尾文,再补 stop。
            rem = live.drain_raw()
        if rem:
            outs.append(_text_delta_event(live_idx + index_shift, rem, live_template))
        outs.append(sse_serialize(
            {"type": "content_block_stop", "index": live_idx + index_shift}, live_template))
        return outs


    # 状态机:是否正在缓冲一个匹配工具的 tool_use 块
    buffering = False
    buf_start = None          # 持有的 content_block_start 原始 chunk(wire 形态)
    buf_start_ev = None       # 该 start 解析后的事件 dict(取 index 等)
    buf_deltas = []           # 累积的原始 delta chunk(用于非匹配时回放/probe)
    buf_partial = []          # 累积的 partial_json 片段
    buf_rule = None           # 当前工具的补全规则
    buf_tool = None           # 当前工具名
    buf_start_input = None    # start 里可能自带的 input

    # 只读泄漏探针状态:累积当前 text block 的文本,检测 invoke 泄漏。
    _txt_accum = []           # 当前 text block 累积的 text_delta 文本
    _txt_dumped = False       # 本 block 是否已 dump(避免重复)

    # SSE 帧重组:上游 chunk 边界可能劈开 SSE 帧或多帧挤一个 chunk。进主循环前先重组成
    # 「一 chunk 一完整帧」,让下游 sse_parse / 缓冲逻辑永远只见完整帧,并杜绝半截 `event:`
    # 泄漏给客户端(`JSON Parse error: Unexpected identifier "event"`)。
    response = _reassemble_sse_frames(response, ctx)

    try:
        _diag_n = [0]
        async for chunk in response:
            # chunk 在 anthropic_messages 流式路径下是 SSE 序列化后的 bytes/str
            # (hook 在序列化之后)。先解析回 Anthropic 事件 dict;不可解析则原样透传。
            ev = sse_parse(chunk)

            # 诊断:记录前若干 chunk 的真实 python 类型与 repr 前缀。
            if ctx.diag_enabled and _diag_n[0] < 8:
                _diag_n[0] += 1
                ctx.diag("chunk_shape",
                         py_type=type(chunk).__name__,
                         parsed_type=(ev.get("type") if isinstance(ev, dict) else None),
                         repr_head=repr(chunk)[:180])

            if ev is None:
                # 截断/损坏的 JSON 事件帧(上游中途断流,最后一个 chunk 是半截 `data: {...`)
                # 绝不能原样转发,否则客户端 SSE 的 JSON 解析器报 "Unterminated string"。丢弃它;
                # 缓冲中的块由流末尾/异常收尾逻辑补 content_block_stop,不会悬挂。
                if is_truncated_json_frame(chunk):
                    logger.warning("stream_fix: dropped truncated/corrupt upstream SSE frame")
                    ctx.audit("dropped_truncated_frame", head=repr(chunk)[:200])
                    continue
                # 合法的不可解析 chunk(如 [DONE]、ping、空行):不可能是我们要改的块。
                # 若正在缓冲,先把缓冲吐出再透传本 chunk,避免顺序错乱。
                if buffering and buf_start is not None:
                    yield buf_start
                    for d in buf_deltas:
                        yield d
                    buffering = False
                    buf_start = None
                    buf_deltas = []
                    buf_partial = []
                yield chunk
                continue

            ctype = ev.get("type")

            # 诊断:抓 message_start / message_delta 的完整结构(每条流各一,量小),
            # 用于确认 stop_reason 的确切 wire 位置。
            if ctx.diag_enabled and ctype in ("message_start", "message_delta"):
                ctx.diag("msg_event", event=ev)

            def _ev_block_type(e):
                cb = e.get("content_block") if isinstance(e, dict) else None
                return cb.get("type") if isinstance(cb, dict) else None

            # 诊断:记录每个 content_block_start 的块类型。
            if ctx.diag_enabled and ctype == "content_block_start":
                ctx.diag("block_start", block_type=_ev_block_type(ev))

            # 只读泄漏探针:累积 text block 文本,检测 invoke 泄漏,落盘完整原文。
            # 仅 probe_only 生效,绝不改写输出。用于确定真实泄漏 wire 格式。
            if ctx.diag_enabled:
                if ctype == "content_block_start" and _ev_block_type(ev) == "text":
                    _txt_accum = []
                    _txt_dumped = False
                elif ctype == "content_block_delta":
                    d = ev.get("delta") or {}
                    if d.get("type") == "text_delta":
                        _txt_accum.append(d.get("text") or "")
                        joined = "".join(_txt_accum)
                        # 检测 invoke 泄漏标记(累积到足够长再判,避免半个标签)
                        if not _txt_dumped and ("<invoke" in joined or "<function_calls" in joined
                                                or "antml:invoke" in joined):
                            _txt_dumped = True
                            ctx.diag("text_leak_partial", index=ev.get("index"),
                                     text_so_far=joined[:2000])
                elif ctype == "content_block_stop" and _txt_accum:
                    joined = "".join(_txt_accum)
                    if "<invoke" in joined or "<function_calls" in joined or "antml:invoke" in joined:
                        ctx.diag("text_leak_full", index=ev.get("index"),
                                 full_text=joined[:8000], n_deltas=len(_txt_accum))
                    _txt_accum = []
                    _txt_dumped = False

            # === message_delta 处理 ===
            # flush 部分**无条件**(文本块缓冲已无条件化):若 tbuf 仍活跃(上游违约、
            # message_delta 早于 content_block_stop 到达),先原样 flush 缓冲 text,否则
            # message_delta 会先于缓冲文本外发 → 顺序倒置/block 悬挂。
            # 注入部分**仍门控 injected**(convert_invoke 独占):改 stop_reason=tool_use。
            if ctype == "message_delta":
                saw_message_delta = True   # 归无条件段:服务下方「注入却无 message_delta」补发
                # live 去重块未闭合就来 message_delta(上游违约):先闭合 live 块(flush 尾段 + 补 stop),
                # 否则 block 悬挂(非法 SSE)。
                if live is not None:
                    for out in _live_close_events():
                        yield out
                    live = None
                if tbuf and tbuf_start_ev is not None:
                    up_idx = tbuf_start_ev.get("index", 0) + index_shift
                    for out in emit_plain_text_block(up_idx, "".join(tbuf_texts), tbuf_template):
                        yield out
                    tbuf = False
                    tbuf_texts = []
                if injected:
                    ev2 = dict(ev)
                    d2 = dict(ev2.get("delta") or {})
                    d2["stop_reason"] = "tool_use"
                    ev2["delta"] = d2
                    yield sse_serialize(ev2, chunk)
                    continue
                yield chunk
                continue

            # live 块未闭合就来任意新 content_block_start(上游违约):先闭合旧 live 块,防新 start
            # 覆盖 live 实例(丢 pending)+ 双开 block。覆盖 text/tool_use/thinking/未知块类型。
            if live is not None and ctype == "content_block_start":
                for out in _live_close_events():
                    yield out
                live = None

            # === live 去重分支(仅 live_mode)===
            # text block 不全缓冲:content_block_start 立即外发并起一个 LiveDedup;每个 text_delta
            # 过 LiveDedup 后按需外发(退化重复实时抑制、恢复即续流);content_block_stop 前 flush 尾段。
            # 不结束 turn、不改 block 结构(1 text block → 1 text block),故不动 index_shift/injected。
            if live_mode and not buffering:
                if ctype == "content_block_start" and _ev_block_type(ev) == "text":
                    live = LiveDedup(min_run=degen_kwargs["min_run"],
                                     max_seg_len=degen_kwargs["max_seg_len"],
                                     notice=degen_kwargs["notice"])
                    live_idx = ev.get("index", 0)
                    live_template = chunk
                    live_audited = False
                    sc = _shift_ev(ev, chunk)
                    yield sc if sc is not None else chunk
                    continue
                if live is not None and ctype == "content_block_delta":
                    d = ev.get("delta") or {}
                    if d.get("type") == "text_delta":
                        text = d.get("text")
                        if not isinstance(text, str):
                            text = "" if text is None else str(text)  # 容错:非 str 强转(协议异常)
                        try:
                            fwd = live.feed(text)
                        except Exception as fe:
                            # 降级:去重出错——drain_raw 原样取回未提交 pending(feed 已事务回滚,不会二次抛),
                            # 接本片原文一起外发;**live.disable() 而非置 None**——保留「block 仍打开」状态,
                            # 使后续 EOF/message_delta/新 start 仍会补 content_block_stop(绝不悬挂/丢内容)。
                            logger.warning(
                                "stream_fix: live dedup failed (%r); draining pending + raw, disabling dedup", fe)
                            pend = live.drain_raw()
                            fwd = (pend or "") + text
                            live.disable()
                        if fwd:
                            yield _text_delta_event(live_idx + index_shift, fwd, live_template)
                        if live.folded and not live_audited:
                            live_audited = True
                            logger.warning(
                                "stream_fix: live-deduped degenerate repeat region(s) in text block")
                            ctx.audit("degen_live_folded", n=live.folded)
                        continue
                    # text block 内出现非 text_delta(如合法 citations_delta):flush 已缓冲文本(保序)、
                    # 停止本 block 去重(disable,后续文本原样透传)、**保持 block 打开**(不提前补 stop,
                    # 否则会与真 content_block_stop 叠成 orphan/双 stop),透传本 delta,交真 stop 收尾。
                    rem = live.flush()
                    if rem:
                        yield _text_delta_event(live_idx + index_shift, rem, live_template)
                    live.disable()
                    # 落到下方常规处理透传本 delta(不 continue)
                if live is not None and ctype == "content_block_stop":
                    rem = live.flush()
                    if rem:
                        yield _text_delta_event(live_idx + index_shift, rem, live_template)
                    live = None
                    sc = _shift_ev(ev, chunk)
                    yield sc if sc is not None else chunk
                    continue

            # text block 缓冲(**无条件**:所有 text block 都缓冲到 stop 再统一处理)。
            if text_buffer_on and not buffering:
                if ctype == "content_block_start" and _ev_block_type(ev) == "text":
                    tbuf = True
                    tbuf_start_ev = ev
                    tbuf_texts = []
                    tbuf_template = chunk
                    continue
                if tbuf and ctype == "content_block_delta":
                    d = ev.get("delta") or {}
                    if d.get("type") == "text_delta":
                        tbuf_texts.append(d.get("text") or "")
                        continue
                    # 非 text_delta:异常,回放缓冲(带 shift)再走常规
                    up_idx = tbuf_start_ev.get("index", 0)
                    for out in emit_plain_text_block(up_idx + index_shift, "".join(tbuf_texts),
                                                      tbuf_template):
                        yield out
                    tbuf = False
                    tbuf_texts = []
                    # 落到下方常规处理本 chunk(带 shift)
                if tbuf and ctype == "content_block_stop":
                    full = "".join(tbuf_texts)
                    up_idx = tbuf_start_ev.get("index", 0)
                    tbuf = False
                    tbuf_texts = []
                    # 折叠/提取一律**局部 try 降级**:无条件缓冲后,这两步任何异常都会连累
                    # 已缓冲的真实文本。失败时保持 full 原文、segs=None,即降级为原样重发本
                    # block,绝不丢内容(never-swallow-errors)。
                    segs = None
                    try:
                        # 1) 退化重复折叠(若 degen_on):只改文本内容,不改 block 结构。
                        if degen_on:
                            full, n_degen = fold_degenerate(full, **degen_kwargs)
                            if n_degen:
                                logger.warning(
                                    "stream_fix: folded %d degenerate repeat region(s) in text block", n_degen)
                                ctx.audit("degen_folded", n=n_degen)
                        # 2) invoke 提取**硬门控 convert_invoke**:否则普通文本里正常提到的
                        #    <invoke> 会被误转成真实 tool_use、污染 index_shift。
                        if convert_invoke:
                            segs = extract_invoke_from_text(full) if text_maybe_has_invoke(full) else None
                            # 白名单:非空时,仅当所有泄漏工具名都匹配名单才转换;否则整块当
                            # 普通文本放行(保守)。空名单=任意 invoke。名单项支持 glob 通配。
                            if segs and convert_whitelist:
                                tool_names = [s[1] for s in segs if s[0] == "tool_use"]
                                if not all(name_in_whitelist(n, convert_whitelist) for n in tool_names):
                                    segs = None
                    except Exception as fe:
                        # 降级:折叠/提取失败,发原文(full 可能已被部分折叠,但至少不丢块)。
                        logger.warning(
                            "stream_fix: degen/invoke processing failed (%r); emitting text as-is", fe)
                        segs = None
                    if not segs:
                        # 无泄漏/convert_invoke 关/被白名单挡下/降级:原样重发这个 text block
                        # (带 shift)。degen 折叠后的 full 在此天然发出。
                        for out in emit_plain_text_block(up_idx + index_shift, full, tbuf_template):
                            yield out
                        continue
                    # 有泄漏:按 segments 依次发出,index 从 up_idx+shift 起递增
                    cur = up_idx + index_shift
                    n_tool = 0
                    for seg in segs:
                        if seg[0] == "text":
                            for out in emit_plain_text_block(cur, seg[1], tbuf_template):
                                yield out
                            cur += 1
                        else:  # ("tool_use", name, input_dict)
                            # 泄漏路径同样跑字段补全:extract 出的 input 里 questions 是字符串、
                            # question 常缺。不接 apply_item_fixes 就漏补 -> 客户端报缺字段
                            # (与缓冲路径对齐)。仅对配置了补全规则的工具、非 probe_only 时改。
                            leak_input = seg[2]
                            leak_rule = tools_cfg.get(seg[1])
                            if leak_rule is not None:
                                if not probe_only and apply_item_fixes(leak_input, leak_rule):
                                    ctx.audit("patched", tool=seg[1], via="leaked")
                                ctx.audit("tool_out_integrity", tool=seg[1], path="leaked",
                                          **_tool_out_integrity(leak_input, leak_rule))
                            for out in synth_tool_use_events(cur, seg[1], leak_input, tbuf_template):
                                yield out
                            cur += 1
                            n_tool += 1
                    # 原始只占 1 个 index,现在占 (cur-(up_idx+index_shift)) 个 -> 更新 shift
                    produced = cur - (up_idx + index_shift)
                    index_shift += (produced - 1)
                    injected = True
                    n_tool and logger.warning(
                        "stream_fix: converted %d leaked <invoke> in text block to tool_use", n_tool)
                    ctx.audit("invoke_converted", n_tool=n_tool)
                    continue

            # 1) tool_use 块开始
            if ctype == "content_block_start" and _ev_block_type(ev) == "tool_use":
                cb = ev.get("content_block") or {}
                tool_name = cb.get("name")
                ctx.diag("tool_use_seen", tool=tool_name,
                         matched=tools_cfg.get(tool_name) is not None)
                rule = tools_cfg.get(tool_name)
                if rule is not None:
                    # 命中:进入缓冲,先不发 start(保存原始 chunk 与解析后的 ev)
                    buffering = True
                    buf_start = chunk
                    buf_start_ev = ev
                    buf_deltas = []
                    buf_partial = []
                    buf_rule = rule
                    buf_tool = tool_name
                    buf_start_input = cb.get("input")
                    continue
                yield chunk
                continue

            # 2) 缓冲中:累积 input_json_delta
            if buffering and ctype == "content_block_delta":
                delta = ev.get("delta") or {}
                if delta.get("type") == "input_json_delta":
                    buf_partial.append(delta.get("partial_json") or "")
                    buf_deltas.append(chunk)
                    continue
                # 缓冲中出现非 input_json_delta:保守回放已缓冲再透传本 chunk
                yield buf_start
                for d in buf_deltas:
                    yield d
                buffering = False
                buf_start = None
                yield chunk
                continue

            # 3) 缓冲中:块结束 -> 重组、补全、发出
            if buffering and ctype == "content_block_stop":
                raw = "".join(buf_partial)
                input_obj = None
                json_repaired = False
                if raw.strip():
                    # 宽松解析:tool input 本身也可能被截断(缺尾部 ]})。先直接 loads,
                    # 失败则尝试结构修复。修复后会补齐闭合符,让后续字段修正/类型强制能进行。
                    input_obj, json_repaired = loads_lenient(raw)
                    if input_obj is None:
                        logger.warning("stream_fix: bad tool JSON for %r, passing through", buf_tool)
                        # 抓解析彻底失败的 raw(含坏 unicode 等),供设计针对性修复。
                        ctx.audit("parse_failed", tool=buf_tool,
                                  raw_head=raw[:1000], raw_repr=repr(raw[:500]))
                    elif json_repaired:
                        logger.warning("stream_fix: repaired truncated tool JSON for %r", buf_tool)
                        ctx.audit("json_repaired", tool=buf_tool)
                if input_obj is None and isinstance(buf_start_input, dict) and buf_start_input:
                    input_obj = buf_start_input
                # 注:若 input 整个为空,无源数据可补(question←header 需已有 header),透传。

                if ctx.diag_enabled:
                    ctx.diag("tool_input", tool=buf_tool, raw_partial=raw,
                             parsed_input=input_obj, n_deltas=len(buf_deltas))

                changed = False
                if input_obj is not None and not probe_only:
                    changed = apply_item_fixes(input_obj, buf_rule)
                    if changed:
                        logger.warning("stream_fix: patched %r input (filled missing fields)", buf_tool)
                        # 审计:真正发生补全时记一笔(不含敏感全文,仅工具名)。
                        ctx.audit("patched", tool=buf_tool)
                # catch-all 出站完整性:补全后(或 probe_only 未补)记一次结构信号。missing 非空
                # = 发给客户端时仍缺必填字段(如 header 也缺补不出 question),即回归警报。
                if input_obj is not None and buf_rule:
                    ctx.audit("tool_out_integrity", tool=buf_tool, path="buffered",
                              changed=changed, **_tool_out_integrity(input_obj, buf_rule))

                # 发出:start(带 index_shift;shift=0 时字节透传原 chunk)
                shifted_start = _shift_ev(buf_start_ev, buf_start)
                yield shifted_start if shifted_start is not None else buf_start
                idx = buf_start_ev.get("index", 0) + index_shift
                if input_obj is not None and (changed or json_repaired or not buf_deltas):
                    # 有改动/修复过截断/或原本无 delta -> 发重组后的完整 JSON
                    delta_ev = {
                        "type": "content_block_delta",
                        "index": idx,
                        "delta": {"type": "input_json_delta",
                                  "partial_json": json.dumps(input_obj, ensure_ascii=False)},
                    }
                    yield sse_serialize(delta_ev, buf_start)
                else:
                    # 未改动:回放原始 delta(带 shift)
                    for d in buf_deltas:
                        dev = sse_parse(d)
                        sd = _shift_ev(dev, d) if isinstance(dev, dict) else None
                        yield sd if sd is not None else d
                buffering = False
                buf_start = None
                buf_deltas = []
                buf_partial = []
                # content_block_stop(带 shift)
                sc = _shift_ev(ev, chunk)
                yield sc if sc is not None else chunk
                continue

            # 4) 其他 chunk:透传(content_block_* 需应用 index_shift)
            if index_shift and isinstance(ev, dict) and "index" in ev:
                sc = _shift_ev(ev, chunk)
                yield sc if sc is not None else chunk
            else:
                yield chunk

        # 流结束时若仍在缓冲(异常截断),尽力回放并补 stop 收尾
        if buffering and buf_start is not None:
            yield buf_start
            for d in buf_deltas:
                yield d
            idx = buf_start_ev.get("index", 0) if isinstance(buf_start_ev, dict) else 0
            yield sse_serialize({"type": "content_block_stop", "index": idx}, buf_start)
        # text 缓冲未闭合:原样回放(不转换,避免半个 invoke)
        if tbuf and tbuf_start_ev is not None:
            up_idx = tbuf_start_ev.get("index", 0) + index_shift
            for out in emit_plain_text_block(up_idx, "".join(tbuf_texts), tbuf_template):
                yield out
        # live 去重块未闭合(流末尾截断):闭合(flush 尾段 + 补 stop),绝不丢内容 + 必闭合块。
        if live is not None:
            for out in _live_close_events():
                yield out
            live = None
        # 注入了 tool_use 但流里从未出现 message_delta(异常截断):补发一个,
        # 否则客户端收到 tool_use 却因 stop_reason 不是 tool_use 而不执行。
        if injected and not saw_message_delta:
            logger.warning("stream_fix: synthesizing message_delta(stop_reason=tool_use) for injected tool_use")
            yield sse_serialize(
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {}},
                tbuf_template if tbuf_template is not None else b"")
    except Exception as e:
        # 出错也要尽力把缓冲吐出并补发 content_block_stop,否则客户端 SSE 状态机悬挂。
        # 补 stop 后正常返回(不 raise):薄壳兜底无法重放已消费的上游。
        logger.warning("stream_fix: stream_transform error (%r); closing buffered block", e)
        if buffering and buf_start is not None:
            yield buf_start
            for d in buf_deltas:
                yield d
            idx = buf_start_ev.get("index", 0) if isinstance(buf_start_ev, dict) else 0
            yield sse_serialize({"type": "content_block_stop", "index": idx}, buf_start)
        if tbuf and tbuf_start_ev is not None:
            up_idx = tbuf_start_ev.get("index", 0) + index_shift
            for out in emit_plain_text_block(up_idx, "".join(tbuf_texts), tbuf_template):
                yield out
        if live is not None:
            for out in _live_close_events():
                yield out
            live = None
        return
