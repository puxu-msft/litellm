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

## 下游 SSE 保活（downstream keepalive）延后项

来源: 2026-07-14 「配置下游超时 / SSE 保活」spec + 实现（`docs/superpowers/specs/2026-07-14-downstream-sse-keepalive-design.md`、`plans/2026-07-14-downstream-sse-keepalive.md`）。核心功能已实现（三面注入、面 1 延迟提交、面 2 组合子、bytes-safe normalizer、StreamLease、按 surface 的 committed error 帧、`all_litellm_params` 防泄漏、global+deployment 配置解析）。以下为有意延后项:

- **上游超时依赖门槛 + 启动 warning**: 保活「只跟随上游存活」，其「上游真挂死」的兜底是既有上游 httpx 默认 read 超时（`COMPLETION_HTTP_FALLBACK_SECONDS=600`），关联 upstream http_client spec（尚未实现）会提供更细的 read gap + total deadline。~~待办: `enabled=true` 且检测不到有效上游 read/total 超时时在 proxy 启动打一次明确 warning~~ **已实现**（2026-07-14）：加载期 `should_advise_missing_upstream_timeout` 在 enabled 且未显式配 `request_timeout` 时打 advisory
- **可观测性指标**: active keepalive streams / ping count / stream age / timeout termination 计数，用于观测 half-open 资源占用。当前无指标（需先定 Prometheus/指标口径，属带设计决策项，未做）
- ~~**加载期 fail-fast 校验**~~ **已实现**（2026-07-14）：全局 `litellm_settings.stream_keepalive` 现在在 proxy 加载边界由 `validate_global_config` fail-fast 校验（非法 → error 日志 + 禁用），不再只靠请求期 resolver 降级
- **部署链路外部固定 deadline**: ingress / LB / NAT 若另设固定绝对 deadline（非 idle 型），保活字节绕不过（PoC `exp/downstream-keepalive-timeout/` 已明确不覆盖）。如需覆盖需在部署层调 idle 超时，非代码可解
- **面 1 message_start 前原生 ping**（非门禁）: 默认面 1 仅发 SSE 注释。若 PoC 验证「message_start 前发 `event: ping`」被 Claude Code 接受，可把面 1 升级为注释 + 原生 ping。当前保守只发注释
