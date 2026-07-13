# Backlog / 延后事项

非关键、可延后、nice-to-have / nice-to-fix 的条目。每条注明来源与延后理由。

## Anthropic ↔ GPT 全保真格式转换（拆解后的延后块）

来源: 2026-07-14 brainstorming「全面、全保真 Anthropic↔GPT 格式转换」。整体目标拆成四块，**block 1（reasoning↔thinking 全保真）已立项优先做**，以下三块不丢、延后，各自独立 spec→plan→实现。判定标准仍是「gpt 对 Claude Code 表现得像原生 Anthropic 模型」。

- **block 2 — 协议信封正确性**: 修双 `message_start`、空壳 thinking、断流时 `message_delta`/`message_stop` 缺失等，让 gpt/Responses→Anthropic 的 SSE 事件序列严格合规。部分空壳 thinking 会被 block 1 顺带解决，其余留此块。相关观测见 `~/.claude/litellm/docs/illformed-fix.md`
- **block 3 — 工具调用保真**: tool_use ↔ Responses function call 双向、参数完整性。部分已在 hookpkg（`stream_fix`）与转换层处理，此块需盘点现状、把该进 fork 的迁进来、补齐缺口
- **block 4 — 流式健壮性**: orphan delta / index_gap / 截断帧 / 未闭合块等畸形谱系。已大量在 hookpkg 覆盖（见 `illformed-fix.md` 映射表），此块主要是「是否迁进 fork 转换层 + 补齐观测中未改写项（`orphan_delta`/`orphan_stop`/`index_gap` 主动改写、未匹配透传块的 `unclosed` 收尾补 stop、断流补全信封）」

优先级: block 1 > block 2 > block 3 ≈ block 4（按对全保真的贡献与当前缺口大小排）。

## 引擎侧掐断上游流(取消/超时/按预算早停)

来源: 2026-07-14 退化重复处理排查 + PoC(`exp/degen-cutoff-abort/`,已独立复现)。

现状: hook 层(`~/.claude/litellm/hookpkg/stream.py` `stream_transform`)**无法**掐断到 copilot 的上游 HTTP 流——async generator 的 `aclose()` 不向下级联,整条包装链没有一层 `finally: await owner.aclose()`,`stream_transform` 里 `return`/`break` 只关客户端连接、不关上游(实测 `httpx_response_is_closed=False`,靠 GC/背压兜)。owner 是原生 anthropic-messages 路径的 `httpx.Response`,hook 够不到。

延后项: 若将来要在响应侧做「取消 / 超时 / 按 token 预算早停 / 退化不可恢复时早停省 token」,需引擎侧改动把 owner 关闭能力接到 hook(暴露 `httpx.Response` 给 hook,或在 `chunk_processor` 加关闭传播),`hook 检测到 → await owner.aclose()`。需单独 PoC(owner 引用怎么穿到 hook)+ 覆盖原生 messages 路径的集成测试 + block_audit 传 `termination=abort` 标记区分 intentional abort 与真故障。

延后理由: 退化场景最终选「去重不掐断」(上游会自恢复、掐断丢有效数据),token 省不下的代价用户已接受;当前无其它取消需求。详见 `~/.claude/litellm/docs/plan/degeneration-cutoff.md` 决策记录与该 skill 的 `reference/stream-cancellation-and-abort.md`。
