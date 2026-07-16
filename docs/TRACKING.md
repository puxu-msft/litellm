# TRACKING — 跨会话在途工作

> 本文件跟踪跨会话的进行中工作(当前功能、WIP、阶段状态)。稳定知识沉淀到 ADR/ARCH/DESIGN/spec;这里只记「谁在做什么、做到哪、下一步」。分支 `ghc`(私有 fork,不合并上游)。多会话并行,提交请只 stage 自己的文件。

## 状态速览

| 功能 | 状态 | spec | plan | 下一步 |
|---|---|---|---|---|
| 下游 SSE 保活 | ✅ 已实现 + E2E 验证 | [keepalive](superpowers/specs/2026-07-14-downstream-sse-keepalive-design.md) | [keepalive](superpowers/plans/2026-07-14-downstream-sse-keepalive.md) | 完成;BACKLOG 有延后项 |
| 上游 http_client 超时 | ✅ 完成(全 5 phase + 变异测试 98.4%) | [upstream-timeout](superpowers/specs/2026-07-13-upstream-http-client-config-design.md) | [upstream-timeout](superpowers/plans/2026-07-14-upstream-http-client-config.md) | 已完成;plan 已标 ALL PHASES COMPLETE,详见下方小节 |
| github_copilot messages 原生路由 | 见 plan | [routing](superpowers/specs/2026-07-13-github-copilot-messages-native-routing-design.md) | [routing](superpowers/plans/2026-07-13-github-copilot-messages-native-routing.md) | 见该 plan |
| gpt reasoning↔thinking 保真 | 见 plan(PoC 已出结果) | — | [fidelity](superpowers/plans/2026-07-14-gpt-reasoning-thinking-fidelity.md) / [poc](superpowers/plans/2026-07-14-gpt-reasoning-poc-results.md) | 见该 plan |

---

## 下游 SSE 保活 — ✅ 已实现

防上游(copilot)沉默时下游 Claude Code 因 idle/read 超时断连。两面:面 1 TTFB 延迟提交、面 2 chunk 间隙组合子。

- **落地**:`litellm/proxy/common_utils/{stream_keepalive_config,sse_keepalive,sse_frame_normalizer,keepalive_metrics}.py` + `common_request_processing.py` 的 `create_response` 三方竞速/慢路径提交 + 三面 surface 接线(`base_process_llm_request` 的 `_resolve_downstream_keepalive`)
- **契约**:bytes-safe frame normalizer(跨 chunk UTF-8 字节等价)、唯一幂等 `StreamLease`、按 surface 的 committed error 帧(HTTPException post-commit 转 `event: error`)、`all_litellm_params` 防泄漏、Override/Resolved 双类型配置、加载期 fail-fast 校验 + 缺上游超时 advisory
- **配置**:`litellm_settings.stream_keepalive: {enabled, interval}` + per-model `litellm_params.stream_keepalive` 覆盖
- **可观测性**:Prometheus 4 series(`litellm_keepalive_active_streams` / `_pings_sent_total` / `_stream_duration_seconds` / `_terminations_total{reason}`),default registry,lazy+guarded
- **验证**:60 单测 + `create_response`/既有回归全绿;A 档 E2E(mock 慢上游 + 真实 litellm 代理 + 紧 read 探针)实测 PASS,见 `exp/downstream-keepalive-e2e/`(A 档 `run-a.sh` 自动化;B 档 `run-claude.sh` 真实 Claude 手动)。超时类型 PoC 见 `exp/downstream-keepalive-timeout/`(确认 read/idle 型)
- **部署链路**:Caddy(`~/.claude/litellm/Caddyfile`,Claude→Caddy:4143→litellm:4142/4141)对保活透明(`response_header_timeout 0` / `read_timeout 0` / `stream_timeout 0` / `flush_interval -1`),无需改
- **延后**(BACKLOG):可观测性指标已做;外部固定 deadline(ingress/LB,非代码可解)、面 1 message_start 前原生 ping(留 PoC,默认只发注释)

## 上游 http_client 超时 — ✅ 完成

per-provider 上游 connect/read/pool 分轴超时 + total(asyncio 绝对 deadline,覆盖 SDK 重试与流式),三面(chat/responses/messages),github_copilot。

- **Phase 1 已完成(Tasks 1-7、7a、4a 步骤 1-2,均已提交)**:`litellm/litellm_core_utils/http_client_config.py`(schema/parse/merge/resolve/coexistence-warn)、`GenericLiteLLMParams.http_client` + TypedDict 字段、`all_litellm_params` 防泄漏、`litellm.http_client` 全局 + proxy 加载校验、legacy-timeout 共存警告(全局 & per-deployment)、`github_copilot` 加入 `supports_httpx_timeout`。约 50 单测全绿,**零运行时行为变化**
- **Phase 2-5 已完成**:Phase 2 deadline 基础设施(`asyncio_deadline.py`:`with_deadline`/`DeadlineBoundAsyncIterator`/`DeadlineExceeded`→`litellm.Timeout` 映射)、Phase 3/4 三面(chat/responses/messages)deadline 接线(非流式 + 流式 phase①/②包裹 + Router mid-stream fallback 分类)、Phase 5 Router 回归 / 自带 client 绕过告警(4 个 choke point)/ http2 schema-only / mutation。Task 4a 步骤 3 已随 Task 13 补
- **变异测试**:`http_client_config.py` + `asyncio_deadline.py` 123/125 = 98.4% 击杀(2 存活为已证明的等价变异体)
- **收尾**:dashboard `schema.d.ts` 已回填 http_client 字段;LIT006 预算 cast 已重构/加 `# cast-ok` 回到天花板下

## 关联加固(已随会话完成)

- proxy 加载期:`stream_keepalive` fail-fast 校验 + 缺上游超时 advisory(`should_advise_missing_upstream_timeout`);`http_client` 全局校验(见上)

## gpt→responses invalid_request_body 排查与修复(2026-07-15..17,已随会话完成)

起因:`gpt`(→`github_copilot/gpt-5.6-sol`,mode=responses)经 `/v1/messages` 返回 `{"error":{"message":"","code":"invalid_request_body"}}`(空 message)。

- **空 message 本体**:主动派真实 agent 流量 + 累计约 200 分钟捕获,始终未复现;失败发生在实例启动后 29 秒 → 判定 copilot 网关**启动期瞬态**,非转换层缺陷。持久失败捕获仍武装(`~/.config/litellm` 的 failure_probe + hookpkg `observe_failure` 全量 dump `request_full`),真复现自动落 `probe-logs/failures.jsonl` 供离线重放。详见 memory `ghc-responses-invalid-request-body`。
- **Bug 1 结构化输出(litellm core,已修 `2d94670276`)**:`responses_adapters/transformation.py` 的 `text.format` json_schema 设 `strict:True` 却原样透传客户端 schema,漏 `additionalProperties:false`+全 required → copilot 拒。改用 openai SDK 的 `_ensure_strict_json_schema`(litellm 已依赖同模块)。TDD 回归 + live 验证(loose→400、strict→200)。用户重启后已生效。
- **Bug 3 孤儿 tool_result(hookpkg,已修 `538b5678d1`)**:tool_result 无匹配 tool_use → function_call_output 无 function_call → copilot 拒 "No tool call found ..."。三态可配置(`orphan_tool_result.strategy`:passthrough/drop/text,默认 passthrough 空转),见 `config/README.md`。**关键**:anthropic_messages→responses 走异步 aresponses **不应用 deployment hook**,只能在 `process`(async_pre_call_hook)改 Anthropic messages(实测传播);另修 hookpkg RELOAD_ORDER 漏 `orphans` 的静默陷阱。详见 memory `ghc-hook-mutation-propagation-map`。三态 live 验证 + 169 hookpkg 测试全绿。
- **待用户决定**:孤儿机制线上默认留 `passthrough`(空转),启用 drop/text 由用户改 config(热读,无需重启)。
