# 退化重复处理:方向决策记录 / Degeneration Handling — Decision Record

状态：**决策定案 —— 线上用 buffered fold(默认,已启用);live 边流边去重已实现+评审+测试但**暂不启用**(与 convert_invoke 互斥,用户选择保留 convert_invoke);否掉「中途截断 / 掐断上游」**
日期：2026-07-14
关联:`degeneration-trim.md`（现有 buffered fold，已实施 + 线上启用 + 本次端到端验证通过）

## 背景 / 触发

会话 `~/.claude/projects/-home-xp-src-neighbors/3e30e2db-*.jsonl` 后半程反复出现「输出 token 循环」:上游模型(长上下文诱发)在一个 text block 里连续吐几万次 `court\n\ncourt\n\n...`(实测单块 31850 次、255KB、约 6 万 token)。用户要求在 hook 机制中修复。

## 核心事实(用户澄清 2026-07-14,决定了方向)

**上游模型会主动发现重复并自动恢复,最终产出有效数据。** 退化不是永久卡死——真实数据里恢复叙述随处可见,例如:
- line 26:`My output glitched into a repeating loop. Let me refocus on the actual work...`(有效内容在前、court 在后)。
- line 249:`court×N ... I need to stop this glitch pattern and just make the tool call. Let me dispatch...`(恢复在 court 之后)。

有效数据可能在退化之**前、中、后或交错**。故任何「检测到重复就结束这一轮」的做法都会**丢掉模型恢复后的有效数据**。

## 决策:保留现有 buffered fold

现有 `fold_degenerate`(见 `degeneration-trim.md`)在 `content_block_stop` 全缓冲后折叠:**只去掉冗余重复、保留所有非重复段**(含模型恢复数据),位置无关。本次已端到端验证(线上 config + 完整 `stream_transform` + 真实退化文本):

| 真实块 | 输入 court | 输出 court | notice | 恢复数据 |
|---|---|---|---|---|
| line 26 | 31850 | 1 | ✓ | ✓ 完整保留 |
| line 249 | 52 | 2 | ✓ | ✓ 完整保留 |
| line 293 | 101 | 3 | ✓ | ✓ 完整保留 |

**它就是正确的修复,已生效。** 当初那两个会话没生效,纯粹是**部署时机**——fold 是那之后才启用的(notice 在旧 transcript 里一次都没出现),非逻辑缺陷。用户明确选择「就用现有 buffered fold、不改流式」。

## 否掉的三个方向(record-not-adopted)

### 1. 中途截断(mid-stream cutoff,结束 turn)—— 否

检测到退化游程即发折叠结果 + `message_delta` 结束这一轮。**致命缺陷**:会丢掉模型恢复后的有效数据(见上「核心事实」)。用户明确否掉。曾为此写了 `CutoffDetector` 增量检测器 + 9 单测,决策后已移除(git 历史可查)。

### 2. 掐断上游连接(abort upstream,省生成 token)—— 否(且技术上不能靠 hook 单独做)

目标:检测到退化即关掉到 copilot 的 HTTP 流、真正停止生成、省 token。**PoC 结论(已独立复现 ASSERTIONS: PASS,产物 `exp/degen-cutoff-abort/`)**:

- 让 `stream_transform` return/break **不能**掐断上游。整条包装链(opus/claude 经 copilot 走**原生 anthropic messages 路径**,owner 是 `httpx.Response`;`GithubCopilotAnthropicMessagesConfig` 坐实)**没有任何一层** `finally: await owner.aclose()`,而 Python async generator 的 `aclose()` **不向下级联**。实测 return 后 `httpx_response_is_closed=False`。
- 只有显式 `httpx.Response.aclose()` / `CustomStreamWrapper.aclose()` 才实测掐断,但 hook 只拿到最外层包装 generator,够不到 owner。
- 阻断点:`litellm/proxy/pass_through_endpoints/streaming_handler.py` `chunk_processor` finally 只调度日志、不 close;`litellm/llms/anthropic/experimental_pass_through/adapters/streaming_iterator.py:793` 无 finally close。

**即真省 token 需 litellm 引擎侧 patch**(本仓是 fork,可改):把 owner 暴露给 hook 或加 close 传播。**因方向 1/2 都与「保留恢复数据」冲突(截断/掐断都会丢恢复),整体否掉。** 若未来在「已确认不会恢复」的场景下重启省 token,此 PoC 结论仍是起点。

### 3. live 流式去重(边流边去重,不结束 turn)—— **已实现**(2026-07-14 改判为改进)

初判「不改流式、搁置」;用户随后指出这是**改进而非取舍**(去重正确性与恢复保留和 buffered 一致,额外解冻屏幕,无功能性下风,只有实现工作量),按 `long-termism-wins` 不该降级为 backlog。故已实现:

- `hookpkg/degen.py` 新增 `LiveDedup`(forward-then-suppress 纯状态机):有效内容实时转发;`\n\n` 相同短段游程达 `min_run` 时插一次 notice 并抑制后续重复;遇不同段(恢复)即续流。红线与 fold 对齐(空段透明、长段打断、仅 `\n\n` 级);非法阈值/unsafe notice 降级。
- `hookpkg/stream.py` 加**附加 live 分支**(`degen_trim.mode=="live"` 且 convert_invoke 关时启用):text block 不全缓冲,start 立即外发、每 delta 过 `LiveDedup` 按段外发、stop 前 flush;message_delta/流末/异常三处均 flush live 尾段(不丢内容)。**不改 block 结构 / index_shift / stop_reason**(1 text block → 1 text block,不结束 turn)。
- `degen_trim.mode` 默认 `"buffered"`(现有已验证路径不动);切 `"live"` 才走流式去重。与 `convert_text_invoke` 互斥(后者需整块缓冲),同开时回落 buffered。
- 代价(可接受):按 `\n\n` 段为外发粒度,故留 `min_run-1` 个重复(实测 31850→3);段内无 `\n\n` 的长段落在段完成前仍暂缓(比整块缓冲的冻结好,非逐 token)。
- 验证:纯函数 13 单测 + 集成 5 单测 + 真实退化数据端到端(31850→3、恢复完整保留、`n_text_deltas>1` 证明边流边发)。

## 采纳的评审发现(gpt-souls:reviewer,针对已否掉的截断计划)

评审虽针对已否方向,但两条通用结论对现有 fold 仍有价值,记录备查:
- **兜底路径不折叠**(现有已知降级):截断/异常/防御 flush 四条路径不跑 fold,退化在这些场景不裁剪。为保「异常必闭合块、不吞内容」有意为之,非 100% 覆盖。保持现状。
- **`\n\n\n\n` 真空段红线、分片跨 `\n\n`、长段打断**三项语义,现有 `fold_degenerate` 已正确覆盖(见 `test_degen.py`)。

## 结论

无需新增修复代码。现有 buffered fold 已是正确、生效、且保留恢复数据的方案;本次工作为**验证 + 决策定案 + 清理探索弯路**。省 token(引擎 patch)与 live 去重(流式改造)均记入 backlog,当前不做。

## live 去重代码评审处置(gpt-souls:reviewer 对合并态代码,2026-07-14)

评审对 live 实现挖出 2 BLOCKER + 多 Important,均逐条判过:

- **[BLOCKER] 异常退出路径只发尾段不补 content_block_stop → 非法 SSE(未闭合 block)** —— **已修**。抽 `_live_close_events()`(flush 尾段 + 补 stop),用于全部「live 块未闭合就退出」路径:message_delta 早到、新 block start、非 text_delta、流末、异常。正常收到上游 stop 时转发真 stop 闭合、不双补。回归测试断言 start/stop 计数相等。
- **[BLOCKER] `LiveDedup.feed()` 异常时直接转发当前片、丢 `_pending` 中已滞留真实文本** —— **已修**。except 里先 `live.flush()` 取回 pending 再接本片;并在 feed 前校验 text 为 str(非 str 强转)。回归测试 mock feed 抛异常、断言 prefix 不丢 + block 闭合。
- **[Important] live 块未闭合就来新 content_block_start → 覆盖实例丢 pending + 双开 block** —— **已修**。任意新 start 前若 live 活跃,先 `_live_close_events` 闭合旧块。覆盖 text/tool_use/thinking/未知。
- **[Important] `flush()` 尾段不参与去重、结果依赖是否带尾随 `\n\n`** —— **已修**。`flush()` 把尾段当最后一段跑 `_consume_seg(sep="")`,与带尾随 `\n\n` 结果一致。
- **[Important] live 与 convert_invoke 同开静默回落 buffered** —— **已修**(加显式 warning)。**注意**:线上 `hooks.config.json` 现有 `convert_text_invoke=true`,故启用 live 需先关 convert_invoke(取舍,待用户定)。
- **[Minor] 非法 max_seg_len 变成无限长度上限而非禁用** —— **已修**(非法 max_seg_len → 整体禁用)。
- **[Important] `[DONE]` 混进 live 块会先透传再补块** —— **暂记**。`/v1/messages` 路径不产生 OpenAI `[DONE]`;且 after-loop 现已补 stop,不丢内容(仅次序次优)。ping 期间 live 保持是正确的(ping 非终止事件)。
- **[Important] 抑制期空段被删、恢复前空白布局归一** —— **有意**。仅空白布局(非真实内容)归一,与 `fold_degenerate` 折叠区间 sep 归一一致;恢复段本身完整保留。文档声明。
- **[Minor] 长无 `\n\n` 段 O(n²)/无界 pending** —— **暂记 backlog**。仅 live 开启 + 单段超长且碎片流式才触发;perf-only 非正确性;live 需转发字节故不能像检测器那样截断 pending,改「超长即直通」有改动风险,待需要再做。
- **[Minor] 全套测试当时非全绿(AskUserQuestion 回归失败)** —— **已解决**:那是并行会话在改 `_tool_out_integrity`/AskUserQuestion 去重,现已落地,全套 105 tests 绿。

处置后:纯函数 `TestLiveDedup` + 集成 `TestLiveDedupStream` 含全部 BLOCKER/Important 回归,全套 **105 tests 绿**;真实退化数据 e2e 复验:block 正确闭合(start==stop)、恢复保留、去重生效。

### 第二轮验证(评审复核修复,又挖出 1 新 BLOCKER + 2 Important —— 均已修)

- **[BLOCKER 新] feed 降级后 `live=None` 同时表达「禁用去重」和「块已关」→ 降级后若流截断/message_delta/新 start,收尾守卫因 live=None 失效、block 不闭合** —— **已修**。拆分状态:新增 `LiveDedup.disable()`(停去重、保留实例)+ `drain_raw()`(原样取回 pending、不重处理不抛)。feed 异常 catch 改为 `drain_raw()` + `disable()`(**不置 None**),live 保持非 None → 所有异常退出路径的收尾守卫仍补 stop。回归 3 个(降级后 EOF/message_delta/新 start 均 start==stop)。
- **[Important] 合法 `citations_delta`(text block 伴随 delta)被当块异常 → 提前补 stop 造成 orphan/双 stop** —— **已修**。非 text_delta 分支改为 `flush()` 外发已缓冲文本 + `disable()` + **不补 stop、不 continue**(保持块开),透传该 delta,交真 stop 收尾。回归 `test_live_citations_delta_no_orphan`。
- **[Important] `feed()` 非事务:`_consume_seg` 在 pending 切片后抛异常 → catch 取不回被切掉的段** —— **已修**。`feed()` 用 try/except 把 `_pending` 回滚到入口快照再重抛;catch 用 `drain_raw()`(非 flush,避免二次抛)。回归 `test_feed_transactional_rollback`。

第二轮后全套 **118 tests 绿**;真实数据 e2e 仍 block 正确闭合。

### 第三轮验证(评审再挖 1 BLOCKER + 1 Minor —— 已修,异常安全类彻底闭合)

- **[BLOCKER] `_live_close_events()` 在 `live.flush()` 抛异常时 `rem=""` → 无声丢 pending** —— **已修**。`flush()` 也改为 **pending 提交事务性**(仅 `_consume_seg` 成功后才清 `_pending`,抛则保留);`_live_close_events` 的 flush 异常降级改用不会抛的 `drain_raw()` 取回尾文。至此异常安全链闭合:feed 事务化 + flush 事务化 + close 用 drain_raw,任一单点异常都不丢内容。回归 `test_flush_transactional_on_consume_error`(unit)+ `test_live_flush_exception_on_close_preserves_pending`(集成,mock flush 抛、断言 MUST_KEEP 不丢 + 单 stop)。
- **[Minor] `feed()` 只回滚 pending、不回滚游程状态** —— **文档修正**(评审认可当前路径无内容错误,因 feed 异常后即 `disable()`,游程字段不再参与去重)。feed docstring 收窄为「pending 提交事务性」并注明不回滚游程状态的理由。

第三轮后全套 **120 tests 绿**。

## 部署决策(用户定 2026-07-14):暂不启用 live

`hooks.config.json` 现有 `convert_text_invoke=true`(15 工具白名单,用户在用)。live 与 convert_invoke **互斥**(convert 需整块缓冲检测泄漏 `<invoke>`;live 按段流式不整块缓冲)。三选一(保持 buffered / 切 live 弃 convert / 投入做成可共存),**用户选保持 buffered fold、不启用 live**——buffered fold 已去重正确 + 保留恢复 + 与 convert_invoke 共存,唯一代价是退化期屏幕冻结,用户接受。

故:
- 线上不改 `hooks.config.json`(`degen_trim.enabled=true`,mode 默认 `buffered`,convert_invoke 照常)。
- live 代码作为**已实现、已评审、已测**的可选 `mode="live"` 留在库里,dormant。
- **backlog(`no-silently-cut-but-defer`)**:若将来屏幕冻结成痛点且仍要 convert_invoke,做「live + convert 可共存」——live 流式中增量检测泄漏 `<invoke>`、命中则对该 block 回退缓冲转换。届时可启用 live。
