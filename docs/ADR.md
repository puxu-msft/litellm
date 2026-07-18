# 架构决策记录 / ADR

> 每条记录「决策 + 为什么」,不写实现细节(实现见 DESIGN / 主题文档)。

## ADR-0001 — github_copilot `/v1/messages` 路由判据用动态 `/models`,不用 hardcode/静态注册表

日期：2026-07-13 · 状态：已采纳（`ghc` 分支实现）

**背景**：Copilot 不同模型开放不同端点(claude→`/v1/messages`;新 gpt→仅 `/responses`;旧模型→仅 `/chat/completions`)。litellm 拉 `/models` 时丢弃 `supported_endpoints`,路由缺权威判据,只能退回 `"claude" in model` 字符串或静态 `model_cost` 注册表。responses-only 的新 gpt 若落到 chat/completions 会直接失败。

**决策**：`/v1/messages` 的三向端点路由(messages / responses / chat),判据来源**必须是动态取值**——定时拉上游 `/models` 保留 `supported_endpoints` 并缓存,**不引入任何 hardcode 端点表**;operator 可用 `model_info.mode`(含新增 `anthropic`)硬 override。静态 `model_cost` 仅作冷启动/失败兜底。

**为什么**：上游模型(尤其 gpt-5.x 家族)快速迭代,任何静态表都会漂移——实测注册表连 `claude-haiku-4.5` 的端点都标错、root 与 backup 文件自相矛盾。上游 `/models` 是唯一权威真相源。用户明确否决了「补齐静态注册表」和「hardcode 映射表」两种方案。

**取舍**：动态取值引入拉取+缓存+后台刷新+非交互认证的复杂度,并在单账号单 base 之外留有已知限制(多租户/共享别名,见主题文档)。接受,因为正确性(responses-only 模型不能失败)与抗漂移优先。

**详见**：[github-copilot-endpoint-routing.md](./github-copilot-endpoint-routing.md)

## ADR-0002 — 终端可观测采用 durable typed events + 单一 Rich renderer，不继续扩展 logger/ANSI PoC

日期：2026-07-18 · 状态：已采纳（用户决策、Phase 0 门禁与 TDD 实施计划均冻结）

**背景**：现有 `config/hookpkg/logline.py` 已证明紧凑完成记录和 TTY 最后一行在途聚合可行，但它把采集、共享状态、日志格式和 ANSI DECSTBM 生命周期耦合在一个可热重载模块，只拥有专用请求 logger，无法可靠覆盖其它 LiteLLM/uvicorn 日志、多 worker、进程恢复、非 TTY 结构化输出和精确四边界 body/chunk 档案。继续在该模块追加状态会形成第二套请求生命周期，并让终端 UI 故障影响代理主路径。

**决策**：现有 PoC 继续在线直到新系统验收。长期方案以版本化 immutable typed events 为采集边界，复用既有 `InFlightRegistry` 作为请求生命周期唯一事实源；SQLite WAL 是 durable source of truth，内存 reducer/Rich Live 只是可重建投影。交互 TTY 由唯一 Rich renderer 串行拥有已知 Python logger、完成记录和单行 footer；非 TTY 默认输出元数据 JSONL。多 worker 由 uvicorn 主进程持有 collector/renderer，worker 经 Unix stream 上报并在断连时写独立 SQLite spool；该 ownership 必须先通过真实进程模型 PoC，失败则回到 ADR 重新裁决 collector 子进程，不静默换方案。

**数据边界**：保存客户端请求、上游请求、上游响应、客户端响应四个边界的完整 body、headers 与原始 chunk 时序；TUI `↑/↓` 固定表示 GitHub Copilot upstream request/response HTTP body bytes。Claude Code 现有 `X-Claude-Code-Session-Id` 是 session 真相源，TUI 显示确定性短 hash。固定凭据 header 只保存掩码前后缀，网络 replay 使用当前 authenticator。segments 达 2 天或 1GiB 即封存并开新库，不自动删除；zip-transcripts 归档接入仅列 TODO。

**为什么**：typed events 把业务事实与表现层解耦，使 TTY、JSONL、Web/SQL、档案与 replay 共享同一语义，并允许故障恢复、跨 worker 幂等汇入和影子比对。SQLite WAL + worker spool 在不阻塞模型请求的前提下避免无界内存队列与生命周期事件丢失。Rich 已是正式依赖，能承担宽度、resize、自然换行和 Live 重绘；只替换已知 logger handlers，不劫持 stdout/stderr。四边界原始档案保留转换前后和 split-frame 的一手证据，避免继续从聚合 `ModelResponse` 或语义 JSON 长度推断真实 wire 行为。

**取舍**：接受 SQLite schema/rotation、IPC、spool replay、主进程 collector、transport observer 和 Web 检查器的实现复杂度；公开底层 SQL 表也提高 migration 兼容责任。拒绝用功能缩减换简单：通过分阶段 PoC、影子双写、PTY、多进程故障注入和真实流量 oracle 控制风险。终端/collector/archive 任意故障均 fail-open，最多重建 renderer 一次，随后降级 plain/JSON，不影响模型请求。

**Phase 0 验证结果**：真实 uvicorn direct/multiprocess/reload 均支持父进程唯一 collector；direct runner 必须持有 uvicorn 外层 SIGTERM handler，才能在 uvicorn re-raise 后执行 collector cleanup。httpx transport observer contract 可保持 request/response chunk、异常与关闭传播，request wrapper 由 observer transport `finally` 关闭、response wrapper 由 `Response.aclose()` 关闭。DuckDB wheel 不内置离线 sqlite scanner，故采用构建期准备并随版本/平台打包的官方 extension，运行时禁止下载。详见 `exp/terminal-observability-phase0/CONCLUSION.md`。

**未采纳**：手写 DECSTBM 作为长期 renderer、Textual 全屏、只接管请求 logger、劫持 stdout/stderr、asyncio renderer actor、共享锁状态作为长期事件模型、无界内存队列、worker leader election、IP/User-Agent/history session 推断、transformation 层近似统计上游 bytes、直接替换当前 PoC。

**详见**：[terminal-observability-event-archive-design.md](./superpowers/specs/2026-07-18-terminal-observability-event-archive-design.md)
