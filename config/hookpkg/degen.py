"""退化重复检测与折叠 / Degeneration repeat detect & fold.

上游模型偶发退化(degeneration):在一个 text block 里连续吐出数十行短小且完全相同的
片段(如以空行分隔的 "court\\n\\ncourt\\n\\ncourt\\n\\ncourt")。本模块提供无副作用纯函数
把这类退化重复折叠为「首段一次 + 声明」,与 SSE/async 解耦,便于单测。

分段策略(两级,各自阈值):
- 主分段按 \\n\\n(段落级),阈值 min_run / max_seg_len。
- \\n 回退(行级),更严阈值 line_min_run / line_max_seg_len,只在主分段留下的**非退化段
  内部**再跑一遍(混合场景:部分空行分隔、部分仅换行分隔)。

关键正确性红线:groupby 按**相邻** key 分组,空段(strip 后为 "")会物理打断前后相同非空段
的相邻性(court,"",court,... 被切成一堆游程=1),导致 \\n\\n\\n 场景漏检。故**分组前先滤除
空段**,只对非空段序列 groupby,使被空段隔开的相同段仍能续接成游程。
"""
from __future__ import annotations

import re
from collections import namedtuple
from itertools import groupby

# raw=原文, norm=strip 后归一化键(比较用), sep=该段之后的原始分隔符(重建用)
Seg = namedtuple("Seg", "raw norm sep")

_SPLIT_RE = {
    "\n\n": re.compile(r"(\n\n)"),
    "\n": re.compile(r"(\n)"),
}


def _split_segments(text, mode):
    """按 mode(\\n\\n 或 \\n)切分,保留每段原文 raw、归一化键 norm、及其后原始分隔符 sep。
    用带捕获组的 re.split 使重建能字节级还原(含多余空行/行尾空白)。"""
    pat = _SPLIT_RE[mode]
    parts = pat.split(text)  # [seg, sep, seg, sep, ..., seg]
    segs = []
    i = 0
    n = len(parts)
    while i < n:
        raw = parts[i]
        sep = parts[i + 1] if i + 1 < n else ""
        segs.append(Seg(raw=raw, norm=raw.strip(), sep=sep))
        i += 2
    return segs


def _rebuild(segs):
    """把 Seg 序列拼回文本(字节级:raw + sep 逐段)。"""
    return "".join(s.raw + s.sep for s in segs)


def _fold_segs(segs, min_run, max_seg_len, notice, fold_sep):
    """对一个 Seg 序列做游程折叠。返回 (new_segs, n_folded)。
    先滤空段再 groupby(见模块红线),折叠区间保留首段 raw + 一段 notice。
    fold_sep 是首段与 notice 之间插入的分隔符(外层 \\n\\n / 内层 \\n)。"""
    # 先滤空段:记录非空段及其在原序列中的位置,空段稍后按原位保留。
    non_empty = [(idx, s) for idx, s in enumerate(segs) if s.norm != ""]
    if not non_empty:
        return segs, 0

    # 对非空段按 norm 分组;每组是「原始下标集合 + Seg 列表」。
    fold_ranges = []  # [(set_of_original_indices, first_seg, last_seg)]
    pos = 0
    m = len(non_empty)
    n_folded = 0
    # groupby 在 (idx, seg) 上按 seg.norm 分组;相邻非空段即使被空段隔开也在此续接。
    for key, grp in groupby(non_empty, key=lambda t: t[1].norm):
        run = list(grp)
        if len(run) >= min_run and len(key) <= max_seg_len:
            orig_idxs = {idx for idx, _ in run}
            fold_ranges.append((orig_idxs, run[0][1], run[-1][1]))
            n_folded += 1

    if not fold_ranges:
        return segs, 0

    # 重建:遍历原 segs,命中折叠区间的——首段位置发首段+notice,其余重复段(及夹在中间的
    # 空段)跳过;未命中的原样保留。
    fold_by_first = {}   # first_original_idx -> (orig_idxs, first_seg, last_seg)
    drop_idxs = set()
    for orig_idxs, first_seg, last_seg in fold_ranges:
        first_idx = min(orig_idxs)
        last_idx = max(orig_idxs)
        fold_by_first[first_idx] = (orig_idxs, first_seg, last_seg, last_idx)
        # 折叠区间覆盖 [first_idx, last_idx] 的所有段(含中间空段)一并折叠。
        for k in range(first_idx, last_idx + 1):
            drop_idxs.add(k)

    out = []
    for idx, s in enumerate(segs):
        if idx in fold_by_first:
            orig_idxs, first_seg, last_seg, last_idx = fold_by_first[idx]
            # 折叠产物:首段原文 raw,后接 notice 作为独立一段。区间末尾 sep 沿用 last_seg.sep。
            out.append(Seg(raw=first_seg.raw, norm=first_seg.norm, sep=fold_sep))
            out.append(Seg(raw=notice, norm=notice.strip(), sep=last_seg.sep))
            continue
        if idx in drop_idxs:
            continue  # 被折叠的重复段/中间空段
        out.append(s)
    return out, n_folded


def fold_degenerate(text, *, min_run=4, max_seg_len=80, notice="",
                    line_min_run=6, line_max_seg_len=40):
    """把 text 里的退化重复折叠。返回 (new_text, n_folded)。无退化返回 (text, 0)。

    两级:先 \\n\\n 段落级折叠;对折叠后仍未命中的**非退化段**,就地在其 raw 上按 \\n 行级
    再折叠一次(更严阈值)。内层只重写 Seg.raw,外层 sep 不变;禁止重解析已重建的 new_text。
    n_folded = 外层区间数 + 内层区间数。
    """
    if not text:
        return text, 0
    if not notice_is_safe(notice):
        # notice 含 invoke 标记时拒绝折叠(否则会被后续 invoke 提取误解析)。
        return text, 0
    # 阈值类型兜底(根因):config 可能把参数写成 null/字符串。非法/非正值 → 关闭该级折叠
    # (阈值设为极大,使任何游程都达不到),而非崩溃。notice 强制 str。
    min_run = _as_pos_int(min_run)
    max_seg_len = _as_pos_int(max_seg_len)
    line_min_run = _as_pos_int(line_min_run)
    line_max_seg_len = _as_pos_int(line_max_seg_len)
    notice = notice if isinstance(notice, str) else ""
    total = 0

    # --- 外层:\n\n 段落级 ---
    segs = _split_segments(text, "\n\n")
    segs, n_outer = _fold_segs(segs, min_run, max_seg_len, notice, "\n\n")
    total += n_outer

    # --- 内层:对每个「非折叠产物、非 notice」的段,就地按 \n 行级折叠其 raw ---
    # 折叠产物的 notice 段 norm==notice.strip();跳过它与紧邻首段,避免二次处理。
    new_segs = []
    for s in segs:
        # 只处理还含多行、且未被外层折叠语义标记的段。notice 段原样保留。
        if s.raw and "\n" in s.raw and s.raw != notice:
            inner_segs = _split_segments(s.raw, "\n")
            folded_inner, n_inner = _fold_segs(
                inner_segs, line_min_run, line_max_seg_len, notice, "\n")
            if n_inner:
                total += n_inner
                s = Seg(raw=_rebuild(folded_inner), norm=s.norm, sep=s.sep)
        new_segs.append(s)

    if total == 0:
        return text, 0
    return _rebuild(new_segs), total


_INVOKE_MARKERS = ("<invoke", "antml:invoke", "<function_calls")

# 一个大到任何真实游程/段长都达不到的哨兵,用于「非法阈值 → 关闭该级折叠」。
_UNREACHABLE = 1 << 30


def _as_pos_int(v):
    """把阈值参数强制成正整数;非法(None/非数/≤0)则返回一个不可达大值,等效关闭该级折叠。"""
    try:
        iv = int(v)
    except (TypeError, ValueError):
        return _UNREACHABLE
    return iv if iv > 0 else _UNREACHABLE


def notice_is_safe(notice):
    """notice 不得包含 invoke 标记(否则 degen+invoke 叠加时 notice 自身被误解析为泄漏)。"""
    if not notice:
        return True
    return not any(m in notice for m in _INVOKE_MARKERS)


class LiveDedup:
    """流式退化去重状态机 / Live streaming degeneration dedup.

    与 `fold_degenerate`(事后全缓冲折叠)去重结果一致、同样**保留模型恢复后的有效数据**,但**边流边做**:
    有效内容实时转发;进入 \\n\\n 相同短段游程后,放行前 `min_run-1` 段、达阈值时插一次 notice 并抑制后续
    重复;遇不同段(恢复)立即恢复转发。forward-then-suppress:前几段已流出无法收回,故会留 `min_run-1`
    个重复(可接受,notice 标记),换取实时解冻——不结束 turn、不碰上游。**每个 text block 一个实例。**

    红线与 fold 对齐:空段(\\n\\n\\n\\n)透明不打断游程;长段(>max_seg_len)打断游程 + 结束抑制(视作
    恢复);仅 \\n\\n 级(单换行退化不在流式处理,与 fold 的 \\n 回退是两条路)。非法阈值经 `_as_pos_int`
    降级为永不触发;notice 含 invoke 标记则整体禁用(pass-through,防 notice 自身被误解析)。

    字节布局:非退化文本按 \\n\\n 切段后原样重发(段内单换行、\\n\\n\\n+ 空段都能重建);仅被折叠的重复
    区间归一为 notice + \\n\\n。这是有状态流式对象,计数原地更新是其本质。
    """

    __slots__ = ("_min_run", "_max_seg_len", "_notice", "_disabled",
                 "_pending", "_last", "_run", "_suppressing", "folded")

    def __init__(self, min_run, max_seg_len, notice):
        self._min_run = _as_pos_int(min_run)          # 非法 → _UNREACHABLE,永不触发
        self._max_seg_len = _as_pos_int(max_seg_len)
        notice = notice if isinstance(notice, str) else ""
        self._notice = notice
        # 禁用条件:notice 含 invoke 标记(防自解析);或 max_seg_len 非法(_UNREACHABLE 会让任意
        # 长度段都参与去重 → 过激误判,故整体禁用)。min_run 非法则游程永不达标,天然 pass-through。
        self._disabled = (not notice_is_safe(notice)) or self._max_seg_len == _UNREACHABLE
        self._pending = ""      # 尾部未被 \n\n 终结的半截段
        self._last = None       # 当前候选重复段 norm
        self._run = 0           # 尾部游程长度
        self._suppressing = False
        self.folded = 0         # 触发抑制的区间数(审计用)

    def feed(self, text_delta):
        """喂入一个 text_delta,返回应转发给客户端的文本(可能为 "")。

        **pending 提交事务性**:任何段处理异常都把 `_pending` 回滚到本次调用入口的快照(含本次
        text_delta 之前的全部未提交内容)再重抛,使调用方能用 `drain_raw()` 原样取回全部未提交文本、
        绝不丢内容。注:仅回滚 `_pending`,不回滚游程状态(`_last`/`_run`/`_suppressing`)——调用方在
        feed 异常后应 `disable()`,此后游程字段不再参与去重,故无需回滚。"""
        if not text_delta or self._disabled:
            return text_delta or ""
        snapshot = self._pending
        try:
            self._pending += text_delta
            out = []
            while True:
                i = self._pending.find("\n\n")
                if i < 0:
                    break
                seg = self._pending[:i]
                self._pending = self._pending[i + 2:]
                fwd = self._consume_seg(seg, "\n\n")
                if fwd:
                    out.append(fwd)
            return "".join(out)
        except Exception:
            # 回滚 pending 到入口快照,交由调用方降级(drain_raw 取回 snapshot + 原样接本片)。
            self._pending = snapshot
            raise

    def disable(self):
        """降级:停止去重(后续 feed 原样透传、flush/drain 仅返回残留 pending),**但保留实例**——
        调用方据「实例非 None」判定 block 仍打开、仍会被收尾补 content_block_stop。用于 feed 异常降级、
        或 text block 内出现非 text_delta(如 citations_delta)时停止去重但不提前关块。"""
        self._disabled = True

    def drain_raw(self):
        """原样返回并清空 pending(**不做去重处理、不会抛**)。降级路径用它取回未提交文本,
        避免 flush() 的 _consume_seg 二次抛异常再丢内容。"""
        p = self._pending
        self._pending = ""
        return p

    def _consume_seg(self, seg, sep):
        """处理一个完整段 seg(其后原始分隔符 sep),返回应转发的文本。更新游程/抑制状态。
        正常段 sep="\\n\\n";flush 的尾段 sep=""(原文无尾随 \\n\\n)。"""
        norm = seg.strip()
        if norm == "":
            # 空段透明:不打断游程;抑制中丢弃,否则原样转发(保留空行布局)。
            return "" if self._suppressing else seg + sep
        if len(norm) > self._max_seg_len:
            # 长段打断游程 + 结束抑制(视作恢复),转发。
            self._last = None
            self._run = 0
            self._suppressing = False
            return seg + sep
        if norm == self._last:
            self._run += 1
            if self._run >= self._min_run:
                if not self._suppressing:
                    self._suppressing = True
                    self.folded += 1
                    return (self._notice + sep) if self._notice else ""
                return ""  # 抑制后续重复
            return seg + sep  # 未达阈值,转发
        # 不同短段(恢复或新游程起点):重置,转发。
        self._last = norm
        self._run = 1
        self._suppressing = False
        return seg + sep

    def flush(self):
        """block 结束:把尾部未终结段当作最后一个真实段跑一遍判定(sep=""),使「恰好在第 min_run
        个重复段结束、且无尾随 \\n\\n」也能被去重(与带尾随 \\n\\n 的结果一致,不因终止分隔符而分叉)。

        **pending 提交事务性**:只在 `_consume_seg` 成功后才清空 `_pending`;若它抛异常,`_pending`
        保持不动,调用方仍可用 `drain_raw()` 原样取回尾文,绝不丢内容。"""
        if self._disabled:
            p = self._pending
            self._pending = ""
            return p
        p = self._pending
        if not p:
            return ""
        result = self._consume_seg(p, "")  # 可能抛;抛则下方不执行,_pending 保持原样供 drain_raw 取回
        self._pending = ""
        return result


