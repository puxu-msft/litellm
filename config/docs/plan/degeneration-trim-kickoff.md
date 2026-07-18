# Kick-off：流式退化重复裁剪实施 / Degeneration Trim Impl

> 用途：把 `docs/plan/degeneration-trim.md`（v3，已批准）交给实施 subagent 的开场指令。
> 本功能已由主会话内联实现（**流程违规：本应 subagent-driven**）。本文档补齐配套产物，
> 并作为「若重做/迁移/复用」的开场提示。

## 上下文

litellm 代理（`~/.config/litellm/`）的 hook 包 `hookpkg/` 在流式响应改写层新增一个修复项：
检测上游退化重复输出（一个 text block 连续吐 ≥4 段短小且完全相同的片段，如空行分隔的
`court\n\ncourt\n\ncourt\n\ncourt`），折叠为「首段 + 声明」并注入。

## 任务

按 `docs/plan/degeneration-trim.md`（v3）实施，TDD。计划已含完整算法、接缝、测试清单、
四项用户决策、三轮评审结论与合并态评审的 F1–F4 修复。**严格遵循计划，勿自行缩减范围。**

## 交付物

1. `hookpkg/degen.py` — 纯函数 `fold_degenerate`：
   - groupby 游程折叠，**红线：先滤空段再 groupby**（否则 `\n\n\n\n` 真空段夹入打断游程漏检）。
   - 两级分段：`\n\n` 默认阈值（min_run=4/max_seg_len=80）+ `\n` 严阈值回退（6/40），
     回退支持混合场景（非退化段内部就地在 `Seg.raw` 递归、禁止重解析 new_text）。
   - `_as_pos_int` 阈值兜底（null/字符串/≤0 → 不可达哨兵，等效关该级）、notice 禁含 invoke 标记。
2. `hookpkg/stream.py` 接入：
   - **文本块缓冲无条件化**（用户明确接受全局非流式）。
   - stop 折叠点：**先 degen 折叠、后 invoke 提取（硬门控 `if convert_invoke`）**；
     二者包进**局部 try 降级**（异常发原文，绝不丢块——合并态 F1）。
   - message_delta：flush 无条件、注入 `if injected`、`saw_message_delta` 无条件置位。
3. `hookpkg/config.py`：`degen_trim` 默认值 + 公开 getter `default_degen_trim()`（单一真相源，
   stream.py 逐参数兜底，勿跨模块读私有 `_DEFAULT_CONFIG`）。
4. `hookpkg/reload.py`：`RELOAD_ORDER` 加入 `hookpkg.degen`（在 `hookpkg.stream` 之前）。
5. 测试（标准库 `unittest`，无 pytest）：
   - `hookpkg/tests/test_degen.py`：含红线用例用 `\n\n\n\n`（**四换行**才产生真空段；三换行
     假绿——合并态 F2）、阈值非法不崩（F4）。
   - `hookpkg/tests/test_stream_degen.py`：语义等价、degen-only 不误转 invoke、message_delta
     早到不悬挂、ping 次序契约、**折叠异常仍发文本**（mock 抛异常，F1 回归）、invoke 转换回归。

## 验收

- `python3 -m unittest hookpkg.tests.test_degen hookpkg.tests.test_stream_degen`（从 `~/.config/litellm/`）全绿。
- `hooks.config.json` 的 `stream_fix.degen_trim.enabled=true` 启用；`./reload.sh` 热重载（SIGUSR2）。

## 边界

- 兜底路径（末尾/异常/message_delta 早到 flush）**不折叠**，是有意降级，勿改。
- 折叠 1→1 block **不动** index_shift。
- 用户价值观：`never-swallow-errors`、`root-cause-over-patch`、`long-termism-wins`；勿以 YAGNI 砍范围。
