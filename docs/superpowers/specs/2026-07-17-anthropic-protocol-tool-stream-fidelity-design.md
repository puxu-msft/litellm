# Anthropic 协议、工具与流式保真设计

状态：进行中，第一阶段已实现

日期：2026-07-17

## 目标

让经 GitHub Copilot 的 GPT 模型在 Claude Code 与严格 Anthropic SDK 看来遵守 Anthropic Messages 协议，并在 Anthropic、OpenAI Chat Completions、OpenAI Responses 三种表示之间尽可能无损地保留工具调用语义

本设计覆盖 BACKLOG block 2、3、4。block 1 的 reasoning carrier 已有独立设计，但其 direct Responses 流与本设计共享协议信封状态机

## 所有权边界

- Chat bridge：`litellm/llms/anthropic/experimental_pass_through/adapters/`
- direct Responses bridge：`litellm/llms/anthropic/experimental_pass_through/responses_adapters/`
- provider chunk 解析前重组与部署审计：`config/hookpkg/`
- callback 输出后的最终 frame/keepalive 边界：`litellm/proxy/common_utils/sse_frame_normalizer.py`

通用、确定性的协议转换进入 fork 核心 adapter。依赖具体工具 schema 的参数补全、文本 `<invoke>` 恢复、相邻重复工具调用末端去重和生产审计保留在 hookpkg

## 协议不变量

1. 每条流恰好一个 `message_start`，且它是首个 Anthropic message 事件
2. 每个 content block 在 delta 前有 start，在终止前有且只有一个 stop；index 从 0 连续增长，不发负 index
3. `message_delta` 位于所有 content block stop 之后，`message_stop` 位于最后；自然 EOF 与上游异常也必须生成完整终止信封
4. 上游提供真实 stop_reason/usage 时原样映射；缺失终止事件时使用 `end_turn` 与零 usage 作为协议级恢复值
5. 同一 Responses item_id 的重复 added/done 必须幂等，不得建立第二个块或重复 carrier

## 工具保真不变量

1. tool id、name 与 arguments 分片按原顺序保留；正常 delta、orphan 缓冲、done-only 参数三个来源互斥，不得丢失或重复
2. Anthropic `disable_parallel_tool_use` 映射为 OpenAI `parallel_tool_calls` 的逻辑取反；字段未提供时不覆盖 provider 默认值
3. 未知 `tool_result.content` 块不得使 tool result 消失；核心 adapter 至少生成配对 tool message
4. 模型漏填必填业务字段不是协议转换职责，继续由可配置 hook 修复并审计
5. `tool_result.is_error` 没有 OpenAI Chat/Responses 标准等价槽位。live PoC 证明 Responses 私有字段被 400 拒绝，Chat 私有字段虽被接受但被模型忽略且无法恢复；保留 content 与配对 id，明确标记 error bit 有损，不使用内容前缀或私有字段伪装无损

## 畸形流恢复

- reasoning delta 缺 added：按 item_id 合成 thinking start
- function arguments delta 缺 added：按 item_id 缓冲；done 提供 call_id/name 后建立 tool_use 并逐片重放
- done 缺 added：从完整 done item 合成对应块，再 stop；unknown item 不误停当前块
- 重复 added：复用既有 item_id→index 映射
- EOF 或异常：关闭全部 open block，再终止 message
- SSE 帧跨网络 chunk 劈开与尾部截断：解析前 hook 重组 raw provider chunks，callback 输出后 core normalizer 保护 keepalive 注入边界；两者共享 core delimiter 与未终止帧上限语义，hook 额外保留 mixed chunk 和正向审计

## 验收

- direct 单元：覆盖双 start、reasoning/function orphan、orphan done、duplicate added、负 index、正常/异常 EOF、参数单次重放
- Chat 单元：sync/async 的正常/异常 EOF 事件序列完全一致；已有并行工具、交错文本、分片参数与未知 tool_result 回归继续通过
- 合并回归：两个 Anthropic adapter 子树全绿，编辑器类型检查无新增错误
- live 门禁：严格 Anthropic SDK 流能累积可见 thinking summary 并重建 reasoning carrier；Claude Code 无协议错误
- 后续帧迁移门禁：每字节切点 SSE 重组 property test、截断尾帧测试、真实 wire 审计事件三者同时成立

live 验收结果：显式 `summary=detailed` 的 strict SDK thinking_delta、流式/非流式 carrier、Claude CLI carrier 存储与 Bash 工具配对、valid/tampered carrier 差分及跨轮 continuity 均通过。`summary=auto` 只承诺请求 best-effort summary，不承诺每次产生非空 delta

## 未采纳方案

- 在 Chat wrapper 猜测性去重 `message_start`：证据表明重复源在 direct Responses fallback + `response.created`，故应在真实控制点做幂等处理
- 把 `tool_use.input=None` 原样变成 JSON `null`：Anthropic 合同要求 input object，保留非法值不是保真；缺口报告未被采纳
- 把所有 hookpkg 修复整体搬入核心：业务 schema 补全与末端审计不属于通用协议 adapter，整体迁移会扩大耦合并丢失部署可观测性
- 删除 hook 或 core 任一 SSE normalizer：真实调用顺序证明它们分别保护解析前和 keepalive 前边界；删除任一都会重新打开劈帧解析或错误 keepalive 注入窗口