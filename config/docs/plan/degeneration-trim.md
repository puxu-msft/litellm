# 流式退化重复裁剪 / Streaming Degeneration Trim

状态：**已实施**（v3 计划三方评审通过；23 测试全绿；线上已启用 + 热重载）
日期：2026-07-13

## 目标 / Goal

上游模型偶发**退化式重复输出**（degeneration）：在一个 text block 里连续吐出数十行短小且完全相同的片段，例如以空行分隔的

```
court

court

court

court
```

这类内容对用户毫无价值、灌满上下文、浪费 token。目标：在 litellm 流式改写层（`hookpkg/stream.py`）**检测**这种退化，**裁剪**掉冗余重复，并**注入一条声明**告知内容已被代理裁剪。

这是继 `convert_text_invoke`（泄漏 invoke → tool_use）之后，text block 全缓冲机制上的第二个消费者。

## 用户决策（已定）

- **检测单位与阈值**：按空行（`\n\n`，回退 `\n`）分段；仅当**连续 ≥4 段**「块长 ≤ N 字符（默认 N=80）」且**完全相同**时判定退化。保守，避免误伤正常列表/代码/表格。
- **裁剪策略**：**全缓冲**——复用现有 `tbuf` 文本块缓冲机制，到 `content_block_stop` 时把 N 次重复折叠为 1 次并附声明。
- **声明形态**：行内 `text_delta`，**中文提示文案，走 `hooks.config.json` 可配置**。
- **缓冲粒度（新增，用户澄清 2026-07-13）**：**无首字节体验需求**。总是缓冲**单个 text block**（到 `content_block_stop` 再统一处理），但**绝不**跨 block 缓冲整个 response。故文本块缓冲不再用 `convert_invoke OR degen_on` 门控，而是**无条件启用**——消除「门控是否覆盖所有 flush/闭合分支」这一整类风险，逻辑更一致。
- **接受全局非流式（用户决策 2026-07-13，回应对抗性评审 R1）**：无条件缓冲意味着——只要 `stream_fix.enabled` 开着（现线上为 AskUserQuestion 补全已开），**即使 degen 与 convert_invoke 都关**，每个普通文本块也会被扣到 `content_block_stop` 才整段重放，全体文本回复从逐字打字机变成「整段蹦出」的非流式体验。**用户已明确接受这一全局影响**（非静默扩展），换取逻辑一致、无门控遗漏风险。工具/结构事件不受影响，只是文本按 block 粒度成段。

## 现状锚点（已核实）

- `stream.py` 已有 text block 全缓冲状态机（`tbuf`/`tbuf_texts`/`tbuf_start_ev`/`tbuf_template`），当前**仅在 `convert_invoke` 为真时启用**（见 193 行 `if convert_invoke and not buffering:` 及 213 行 `content_block_stop` 分支）。
- 缓冲到 stop 后，`convert_invoke` 路径调用 `extract_invoke_from_text` 决定拆分/原样回放，用 `emit_plain_text_block` / `synth_*` 发出，并维护 `index_shift`。
- 折叠**不改变 block 数量**（1 text block → 1 text block），故**不涉及 `index_shift`**，比 invoke 转换简单。
- 配置热读在 `config.py`，`_DEFAULT_CONFIG["stream_fix"]` + `load_config()` 的深合并 key 列表（99 行）已含 `stream_fix`。新增子字段只需加进默认值即可，无需改合并逻辑。
- 无既有单测目录；本包靠 probe 落盘 + 线上验证。计划新增独立的纯函数单测（检测器可脱离 async 流测试）。

## 设计 / Design

### 1. 新增纯函数检测器（`hookpkg/degen.py`，新文件）

把「检测 + 折叠」抽成无副作用纯函数，便于单测，与 SSE/async 解耦（对齐 `invoke_convert.py` 的分层）。

```python
Seg = namedtuple("Seg", "raw norm sep")   # raw=原文, norm=strip 后归一化键, sep=该段后的原始分隔符

def split_segments(text, mode):
    """按 mode(\\n\\n 或 \\n)切分，保留每段原文 raw、归一化键 norm、以及其后的原始分隔符 sep。
    用 re 带捕获分隔符切分，使重建 100% 还原原文(含 \\n\\n\\n、行尾空白等)。空段(norm=='')
    保留但永不参与游程(见下)。"""

def fold_degenerate(text, *, min_run=4, max_seg_len=80, notice="",
                    line_min_run=6, line_max_seg_len=40):
    """把 text 里的退化重复区间折叠:连续 N≥min_run 段、每段 norm≤max_seg_len、norm 完全相同
    → 保留首段原文一次 + notice，跳过其余。返回 (new_text, n_folded)。无退化返回 (text, 0)。
    分段策略见下(\\n\\n 主 + \\n 回退，各自阈值)。"""
```

要点（已吸收两轮评审）：
- **分段用带捕获的 `re.split`**：保留分隔符实体（`Seg.sep`），重建时逐段 `raw + sep` 拼回，**字节级还原**非退化区原文（修正评审五.3「纯 split 丢失分隔符数量」）。
- **分段策略（用户决策 2026-07-13）**：
  - 主分段按 `\n\n`，用默认阈值 `min_run=4 / max_seg_len=80`。
  - **`\n` 回退：保留但用更严阈值** `line_min_run=6 / line_max_seg_len=40`，以压低代码/表格误伤（连续 `};`、`pass`、markdown 表格分隔行）。
  - 回退**不是全有或全无**：先按 `\n\n` 分段跑一遍折叠；对折叠后仍无命中的**每个非退化段**，若其内部按 `\n` 切有 ≥`line_min_run` 的短行游程，再折叠该段内部。这样混合场景（部分空行分隔、部分仅换行分隔）也能抓到（修正评审五.2「二选一漏检」）。
    - **二次折叠就地在 `Seg.raw` 上递归**（复审补明 4a）：内层 `\n` 折叠只重写该段的 `Seg.raw` 字符串本身，**禁止**重新解析已重建的 `new_text`——否则会把已插入的 notice 段和跨段 `\n\n` 边界一并纳入 `\n` 切分，导致误折叠/错算。
    - **外层分隔符不变**（复审补明 4b）：内层折叠只改 `Seg.raw`，该段之后的外层 `\n\n` `sep` 保持原样，重建时照旧 `raw + sep`。
    - **n_folded 累加**：内外层各自命中的区间数累加计入 `n_folded`。
- **groupby 实现游程**（技术评审采纳，替代手写扫描）：**先滤除空段**再 `itertools.groupby(non_empty_segs, key=lambda s: s.norm)`。每组 `run`：
  - `len(run) >= min_run and len(key) <= max_seg_len` → 折叠为 `run[0].raw + notice`（其后接 `run[-1].sep` 一个分隔）。
  - 否则**整组原样保留**——这是 groupby 的 else 自然分支，消除手写「run 未达标要吐回」的易漏点（修正评审一.a）。
- **空段处理（⚠️ 红线，修正终审提醒 1）**：`norm == ""` 的段（源自 `\n\n\n`、首尾空行）**永不参与游程判定**。**关键**：裸 `groupby` 按相邻 key 分组，空段会成独立组并**物理打断**前后相同非空段的相邻性——`court, "", court, "", court, "", court` 被切成 4 个单元素组、游程全 1、永达不到 min_run，导致 `\n\n\n` 场景**漏检**。仅靠 `key != ""` 守卫**不够**。正确做法：**分组前先把空段过滤出去**（保留其原文+位置用于最终重建），只对非空段序列 groupby，使被空段隔开的相同段仍能续接成游程。此为最易翻车处，测试「`\n\n\n` 不打断真游程」须真正覆盖并通过。
- **strip 语义**：比较用 `norm`（strip 后），但折叠**保留首段 `raw` 原文**（含其缩进/尾随空白），notice 已声明裁剪，无需归一化（修正评审一.b、richest-context-flow）。这是有意决策：各段仅空白差异时按退化处理、留首段原文。
- **notice 约束**：`notice` **不得包含** `<invoke` / `antml:invoke` / `<function_calls`（否则 degen+invoke 叠加时 notice 自身被后续 invoke 提取误解析）。默认值满足；文档在配置节声明此约束（修正评审三）。
- **`n_folded` 语义**：= 被折叠的**区间数**（非删除段数），audit 落盘 `"n"` 与测试断言统一用此定义（修正评审七）。

### 2. 在 `stream.py` 接入（文本块缓冲无条件化）

- 读取新配置：`degen = sf.get("degen_trim") or {}`；`degen_on = degen.get("enabled") and not probe_only`；参数 `min_run`/`max_seg_len`/`notice`。
- **文本块缓冲无条件化**（用户澄清）：现状 text block 全缓冲仅在 `convert_invoke` 为真时启用。改为**总是缓冲单个 text block**（`tbuf` 机制无条件生效），但仍**只缓冲一个 block、不跨 block**。把 193 行 `if convert_invoke and not buffering:`、结尾 flush（381 行）、except（402 行）里的 `convert_invoke` 判据去掉门控——**但保留 193 行的 `and not buffering`**（别在 tool_use 缓冲期间又开 text 缓冲，修正评审二末段）。这样 `convert_invoke` 与 `degen_on` 都只决定「在 stop 折叠点里做不做各自处理」，而非「要不要缓冲」。
  - **message_delta 分支（172 行）必须同步无条件化**（修正评审一.1，确凿遗漏）：176–181 行的防御性 tbuf flush（上游违约、message_delta 早于 content_block_stop 到达时先吐缓冲 text）要改为无条件执行，否则 message_delta 落到 366 行常规透传先发，缓冲 text 要等到末尾 381 flush 才发——**顺序倒置/block 悬挂**。但 182 行起的 `stop_reason=tool_use` 注入部分**仍保持 `if injected`（convert_invoke 独占）**。即：flush 无条件、注入仍门控，二者拆开。`saw_message_delta=True` 归入**无条件段**（服务 387 行「注入了 tool_use 却无 message_delta 时补发」），不随注入分支被拆走（修正复审 R5）。
  - **`ev is None` 分支（108–120 行）的 tbuf 安全性**（修正复审 R3）：该分支现只 flush `buffering`（tool_use 缓冲）、从不 flush `tbuf`。无条件缓冲后 tbuf 近乎常驻，不可解析 chunk（SSE ping/空行，无 index）会在 119 行先于缓冲文本透传。**论证安全**：ping 无 `index`、不改 block 计数，客户端忽略；缓冲文本仍会在其后的 `content_block_stop` 正常成段发出，顺序不倒置（与 message_delta 不同，ping 非终止事件）。实现时补一个集成用例：缓冲期间插入 ping → 断言 ping 透传、文本块随后完整发出。
- 在 `content_block_stop` 折叠点（213–253 行区块）内，处理顺序：
  1. **先跑退化折叠**（若 `degen_on`）：`full, n = fold_degenerate(full, ...)`。折叠只改文本内容，不改 block 结构。
  2. **invoke 提取必须硬门控 `if convert_invoke`**（修正评审二，**不做就出 bug** 而非可选）：216 行起 `extract_invoke_from_text` + 合成 + `index_shift`/`injected` 注入整段，必须用 `if convert_invoke:` 包住。否则 convert_invoke 关时，普通文本里正常提到的 `<invoke>` 会被误转成真实 tool_use、污染 index_shift。明确包裹边界（216–253）与新分支的 `continue`。
  - 顺序理由：退化重复不含 `<invoke>`；先折叠能顺带缩短喂给 invoke 解析的文本。二者作用域不重叠。
- 折叠后若 `convert_invoke` 关或 `segs` 为空（无 invoke，常见情形）：走现有 `emit_plain_text_block(up_idx + index_shift, full, ...)` 分支，**天然发出折叠后的文本**。缓冲无条件化后，这条路径本就是「普通 text block 原样重放」，degen 只是在重放前改了 `full` 内容。
- **兜底路径不折叠（已知降级，如实标注，修正评审七）**：176/381/402 三条 flush 及 200–212 行「tbuf 遇非 text_delta 中途 flush」均调用 `emit_plain_text_block` 原样发出，**不跑折叠**。即防御/截断/异常/被打断四种兜底路径下退化块不裁剪。属功能降级非正确性问题——为保证「异常也要闭合块、不吞内容」的既有不变量，兜底路径不引入折叠（折叠只在正常 stop 点做）。风险节如实记录，不宣称 100% 覆盖。
- 审计：折叠发生时 `append_jsonl(audit, {"_diag": "degen_folded", "n": n, "seg_head": seg[:40]})` + `logger.warning`，与现有 `invoke_converted` 审计一致。`n` = 折叠区间数。

### 3. 配置（`config.py` + `hooks.config.json`）

在 `_DEFAULT_CONFIG["stream_fix"]` 增补：

```python
"degen_trim": {
    "enabled": False,            # 默认关,先线上灰度
    "min_run": 4,                # \n\n 分段:连续 ≥4 段相同才判退化
    "max_seg_len": 80,           # \n\n 分段:仅短段(≤80 字符)参与
    "line_min_run": 6,           # \n 回退分段:更严,连续 ≥6 行
    "line_max_seg_len": 40,      # \n 回退分段:更严,仅 ≤40 字符短行
    "notice": "[上游退化重复输出已被代理裁剪]",   # 不含首尾 \n\n(分隔交给重建)
},
```

**notice 无 `\n\n` 包裹**（修正评审二·技术评审第 2 点）：默认值改为纯文本，首尾不带 `\n\n`。折叠时 notice 作为独立一段插入，由重建逻辑统一负责其前后分隔符（沿用被折叠区间的 `sep`），避免与 notice 自带 `\n\n` 叠加多吞空行。**约束**：notice 不得包含 `<invoke`/`antml:invoke`/`<function_calls`（见算法节）。

**默认值单一真相源**（修正评审四 + 复审 R4）：`stream.py` 读取**全部 5 个参数**（`min_run`/`max_seg_len`/`line_min_run`/`line_max_seg_len`/`notice`）的兜底默认，都从 config 侧的**单一真相源**取，不在 stream.py 硬编码字面量。为避免跨模块读私有 `_DEFAULT_CONFIG`，在 `config.py` 暴露一个公开 getter，例如 `default_degen_trim() -> dict`（返回 `_DEFAULT_CONFIG["stream_fix"]["degen_trim"]` 的浅拷贝）；stream.py 用 `dt = config.default_degen_trim(); min_run = degen.get("min_run", dt["min_run"])` 等逐参数兜底。

`load_config()` 深合并只覆盖 `stream_fix` 顶层；`degen_trim` 子 dict 会被用户 partial 整体替换，故用上述 `.get(k, default)` 兜底而非依赖深合并（与现有 `sf.get(...)` 风格一致）。

线上启用：在 `hooks.config.json` 的 `stream_fix` 加 `"degen_trim": {"enabled": true}`。

### 4. 测试 / Tests

**4a. 纯函数** `hookpkg/tests/test_degen.py`（无需 litellm 运行时）：
- 样例 `court\n\ncourt\n\ncourt\n\ncourt` → 折叠为 1 次 + notice，`n_folded=1`。
- 3 次重复（< min_run）→ 不折叠。
- 长段落重复（> max_seg_len）→ 不折叠。
- 一段 text 内两处独立退化 → 折叠两处（`n_folded=2`）。
- 正常列表/代码（无连续 ≥4 相同短段）→ 原样。
- **退化在文本首/中/末三种位置** → 只折叠退化区间，且**断言前后正常段的空行数量与原文字节级一致**（守评审二·五.3 分隔符重建盲区）。
- **重复段之间夹一个不同段打断游程**（`court×3 + X + court×4`）→ 前 3 个未达阈值原样保留、后 4 个折叠（守评审一.a groupby else 分支）。
- **空段/多空行（⚠️ 红线）**：`\n\n\n` 与首尾空行不构成假游程、**且不打断真游程**（`court\n\n\ncourt\n\n\ncourt\n\n\ncourt` 仍折叠）——守终审提醒 1：先滤空段再 groupby，此为最易翻车用例，必须绿。
- **strip 语义**：各段仅尾随空白差异（`court ` vs `court`）→ 按退化折叠、保留首段原文 raw。
- **`\n` 回退**：无 `\n\n` 但连续 ≥6 行相同短行 → 折叠；连续 5 行 → 不折叠（严阈值）。
- **混合场景**：部分空行分隔的退化 + 某非退化段内部又有 `\n` 行级退化 → 两者都折叠，**断言 `n_folded=2`**（守评审五.2 + 复审 4c 计数）。
- **markdown 表格分隔行/连续 `};`** 4~5 行 → **不**被 `\n` 回退误折叠（line_min_run=6 挡住）。

**4b. 集成层**（修正评审六，守 stream.py 接缝）`hookpkg/tests/test_stream_degen.py`：手搓伪 SSE bytes fixture 喂进 `stream_transform`（async），断言输出事件序列：
- 普通 text block（无退化无 invoke）经无条件缓冲后**语义等价**于原输入（修正复审 R2）：多个 `text_delta` chunk 缓冲后合并成单 delta 重放，**chunk 边界必变**，故不能断言逐字节等价；改断言「事件类型序列合理、拼接后文本一致、index 单调、block 计数一致」。此用例同时点破「无条件缓冲改变 wire 分片」这一 R1 技术根因。
- degen-only（convert_invoke=False）：退化块被折叠，且文本里正常的 `<invoke>` 字样**不被**误转 tool_use（守评审二硬门控）。
- message_delta 早于 content_block_stop 到达：缓冲 text 在 message_delta **之前**外发，无 block 悬挂（守评审一.1）。
- 缓冲期间插入 SSE ping（不可解析 chunk）：ping 透传，文本块随后完整成段发出，顺序不倒置（守复审 R3）。
- index 单调、content_block 计数与预期一致。

## 影响面 / Blast radius

- 新文件 `hookpkg/degen.py`、`hookpkg/tests/test_degen.py`、`hookpkg/tests/test_stream_degen.py`。
- 改 `hookpkg/stream.py`（文本块缓冲无条件化 + message_delta flush 无条件化 + invoke 提取硬门控 `if convert_invoke` + stop 点插入折叠调用）。
- 改 `hookpkg/config.py`（`degen_trim` 默认值增补）。
- 改 `hooks.config.json`（启用开关，可后置）。
- 默认 `enabled:false`，不影响现网行为，直到显式开启。

## 风险与权衡 / Risks

- **误伤**：保守阈值（短段 + ≥4 次 + 完全相同 + 空段守卫 + `\n` 回退严阈值）已尽量压低。仍可能误折叠「刻意的 ASCII 重复图案」等极少数场景；用户可关开关或调阈值。记录声明使误伤**可见可诊断**，不静默。
- **缓冲延迟 / 全局非流式（用户决策，回应 R1）**：改为**无条件缓冲单个 text block**（不跨 block）。因线上 `stream_fix.enabled` 常开，这使**所有文本回复**（即便 degen/convert 都关）都变成「按 block 成段蹦出」的非流式体验，非仅 degen 场景。用户已**明确接受**此全局影响，换取无门控遗漏的一致逻辑。内存/延迟有界（单块粒度），工具/结构事件不受影响。
- **兜底路径不折叠（已知降级）**：防御性 flush（176）、末尾截断 flush（381）、异常 flush（402）、tbuf 遇非 text_delta 中途 flush（200–212）四条路径**不跑折叠**，退化块在这些异常/截断场景下不裁剪。为保「异常必闭合块、不吞内容」的既有不变量而有意为之，非 100% 覆盖。
- **与 invoke 转换叠加**：顺序固定「先折叠后提取」，invoke 提取硬门控 `if convert_invoke`，作用域不重叠；折叠 1→1 block 不动 `index_shift`。notice 约束禁含 invoke 标记，防自解析。
- **默认值单一真相源**：stream.py 兜底从 `_DEFAULT_CONFIG` 反查，不硬编码，避免 drift。
- **占比判据（backlog，`no-silently-cut-but-defer`）**：当前靠四重保守（min_run + max_seg_len + 空段守卫 + `\n` 回退严阈值）压误伤，**暂不加**「退化区间字符/段数占整段比例」判据。正确路径：先默认 `enabled:false` 灰度，靠 audit（`degen_folded` 落盘 `seg_head`）收集真实误折叠样本，再据数据定占比阈值——比现在拍脑袋更稳。列入 backlog 待灰度数据评估，当前不加不影响正确性。
- **未采纳的方案**：
  - 「增量截断（发够 4 次后停发 + 声明）」——用户选了缓冲折叠，更干净；记录备选。
  - 「任意子串重复检测」——用户选了按空行/行分段，误伤更低；记录备选。
  - 「`difflib` / repetition-penalty 库」——技术评审查证：difflib 解相似度不对口；repetition penalty 是生成时 logits 层机制，与代理侧事后段级折叠不同层级。事后文本级去退化无成熟标准库，`itertools.groupby`（标准库 RLE 惯用法）即最小自研，符合 `battle-tested-over-hand-rolled`。记录不采纳理由。
  - 「按 token 阈值」——本层无 tokenizer，引入依赖得不偿失；按字符即可。记录不采纳。

## 评审结论 / Review outcome

两轮 subagent 评审（对抗性 + 技术选型）+ 一轮终审已完成，全部实质发现均已吸收进本 v3：
- 采纳：groupby 替手写游程；notice 去 `\n\n` 包裹；空段守卫；message_delta flush 无条件化；invoke 提取硬门控；分隔符带 sep 字节级重建；strip 比较保留 raw；默认值单一真相源；notice 禁含 invoke 标记；集成层测试；兜底路径降级如实标注。
- 用户决策：`\n` 回退保留但用更严阈值（`line_min_run=6/line_max_seg_len=40`）且支持混合场景（非退化段内部再按行折叠）。

## 合并态评审 / Merged-state review（实施后）

实施完成后派专门 subagent 读**最终集成代码**（非 diff）扫集成缺陷，抓到并修复：
- **F1（严重·丢内容，必修）**：stop 折叠点先清 tbuf 状态再调 `fold_degenerate`，若折叠/提取抛异常跳到 except 时 tbuf 已空 → 整个 text block 文本无声蒸发（无条件缓冲亲手引入的窗口）。**修**：折叠/提取包进局部 try，异常降级为原样重发 `full`，绝不丢块。回归测试 `test_fold_exception_still_emits_text`（mock 抛异常断言文本完整外发）。
- **F2（测试假绿，必修）**：空段红线测试原用 `\n\n\n`（三换行），`re.split` 后中段 `\ncourt` strip 非空——删掉滤空段逻辑测试照样绿。**修**：改用 `\n\n\n\n`（四换行）产生真空段（`norm==''`），并验证不滤空段时游程断成全 1、漏检，真正守住红线。
- **F4（根因·配置健壮性）**：`degen.get(k, default)` 只兜键缺失，兜不住 null/字符串（配 F1 即崩→丢内容）。**修**：`fold_degenerate` 内 `_as_pos_int` 把阈值强制正整数，非法值→不可达哨兵（等效关该级折叠），notice 强制 str。测试 `test_invalid_threshold_config_no_crash`。
- **F3（低·次序契约）**：`ev is None` 分支不 flush tbuf，ping 等不可解析 chunk 先于缓冲文本外发（非终止事件，当前无害）。ping 测试补次序断言固化当前契约，未来变化可察觉。

评审确认正确的关键点：fold 覆盖 [first,last] 无 off-by-one、内层折叠不动外层 sep、degen 1→1 不动 index_shift、5 参数兜底无遗漏、四种开关组合行为正确、message_delta 拆分正确。

最终：**25 测试全绿**，含 F1/F2/F4 回归。
