# 下游 SSE 保活（two-face keepalive，防下游超时）

状态：设计已确认（用户拍板四个分叉决策 + 三点补充确认），待 subagent 对抗性评审 + 用户复核
日期：2026-07-14
分支：`ghc`
关联：[上游 HTTP client 细粒度配置](./2026-07-13-upstream-http-client-config-design.md)（本 spec 依赖其 read gap 超时 + total asyncio deadline 作为「上游真挂死」的兜底）

## 背景与问题

Claude Code 客户端经 litellm 代理打 `/v1/messages` 到 github_copilot 上游。慢推理模型（reasoning）在**首 token 之前**可能沉默数十秒，生成中途事件之间也可能出现长间隙。litellm 现在对这种上游沉默是**原样透传**：不注入任何保活字节，导致下游客户端的读超时 / 整体超时被触发，连接被客户端主动断开，一次本可成功的请求失败。

已核实的现状（读码结论，非推断）:

1. **litellm 自己完全不发 ping**。github_copilot 的 Claude 模型走 native passthrough，上游 Anthropic SSE 字节被逐字转发（`litellm/proxy/pass_through_endpoints/streaming_handler.py:59` `async for chunk in response.aiter_bytes()`）。客户端看到的任何 ping 都来自 copilot 上游；上游沉默时 litellm 转发沉默，无保活。全库无 `event: ping` / `"type": "ping"` 注入点（仅 xAI realtime / redis / `/cache/ping` 等无关引用）

2. **决定性发现——TTFB 窗口连 HTTP 200 响应头都还没发**。`create_response`（`litellm/proxy/common_request_processing.py:405`）会先 `await` 缓冲第一个 chunk（`_buffer_first_chunk_honoring_disconnect`，`:354`，在 `:433` 调用）**才**把 `StreamingResponse` 交给 Starlette。这是故意的:
   - 为把「首 chunk 就是错误」的流转成干净的 JSON 错误响应（`:437-460`）
   - 为在 TTFT 期间检测客户端断连并取消上游调用（LIT-3568，`:358-367`）

   后果：等 copilot 首字节的整个窗口里，客户端连响应头都收不到——这不是「SSE 事件间的间隙」，是「响应还没开始」，是慢模型最容易触发下游超时的主战场

3. 因此下游超时有**两个不同的面**:
   - **面 1 — TTFB（响应头还没发）**：等上游首字节，客户端在等整个响应开始
   - **面 2 — chunk 间隙（流已经开始）**：`message_start` 之后事件间的长间隙

只处理面 2 解决不了主要问题；处理面 1 需要在「还不知道上游会不会报错」时就提前提交 200 SSE 流，与现在「首 chunk 转 JSON 错误」的行为有张力，本设计用「延迟提交」化解。

### 三个 SSE 面汇于一个咽喉点（已核实）

真实上游流式的成功路径全部经 `create_response`:

| 面 | 入口 | 到达 `create_response` |
|---|---|---|
| anthropic_messages | `endpoints.py:95` → `base_process_llm_request` 的 `anthropic_messages` 分支 `common_request_processing.py:1574` | `:1587` |
| OpenAI chat（acompletion） | `proxy_server.py:8472` `route_type="acompletion"` + `select_data_generator` → `select_data_generator` 分支 `common_request_processing.py:1594` | `:1618` |
| responses（aresponses） | `proxy_server.py` responses 端点 | `common_request_processing.py:1618`（select_data_generator 分支，含 `_wrap_responses_stream_for_container_ownership` 包裹）**和/或** `proxy_server.py:10145`——确切接线点留 plan 阶段核准 |

注：`proxy_server.py:8518/8556` 那些**直接** `return StreamingResponse(...)` 只在 guardrail-passthrough / RejectedRequestError 异常分支里，是即时合成的短流（`ModelResponseIterator` 单响应），无上游沉默、不需要保活，本设计不动它们。

## 目标

在三个 SSE 面（anthropic_messages / chat / responses）的**真实上游流式**路径上，向下游客户端注入周期性保活字节，覆盖 TTFB（面 1）与 chunk 间隙（面 2）两个窗口，使上游沉默不再触发下游超时；快路径（上游不慢）行为字节级不变；可配置、默认开启、`enabled=false` 时完全回退现状。

## 非目标（本次不做，记录以备后续）

- **改客户端侧配置**（如 Claude Code 的 `API_TIMEOUT_MS`）：本设计是服务端 SSE 保活，客户端配置正交，另行文档指引
- **上游保活 / 上游超时**：由已冻结的 [上游 HTTP client spec](./2026-07-13-upstream-http-client-config-design.md) 负责，本设计仅**依赖**其作为「上游真挂死」的兜底
- **保活的独立上限 / 最大时长**：用户明确选择「只跟随上游存活」，不设独立 cap（见「上限与兜底」）
- **guardrail-passthrough / rejected 合成短流**的保活（无必要）
- **空 `content_block_delta` 保活变体**：评估后不采用（见「保活帧」的 record-not-adopted）

## 设计（two-face keepalive）

单一注入点：`create_response`。两个面对应两个窗口，共用同一 `interval`。

### 面 1 — TTFB「延迟提交」

把 `_buffer_first_chunk_honoring_disconnect` 从「首 chunk / 断连」两方竞速，扩成「首 chunk / interval 定时器 / 断连」**三方竞速**:

- **首 chunk 在 interval 内到达** → 完全保持现行为：错误首 chunk 转干净 JSON 响应、正常首 chunk 走 `combined_generator`、TTFT 断连转 499。快路径零影响
- **interval 先到（上游慢）** → 此刻**提交 200 SSE**:
  - 返回 `_UpstreamClosingStreamingResponse`，其生成器：先 `yield` 一帧保活（面 1 保活帧，见下），把**仍在 pending 的首 chunk 取值 task 原样带入**（不 cancel），继续以 `interval` 竞速边等边发保活帧，首个真 chunk 到达后 `yield` 之，随后 `async for` 续流（面 2 组合子接管）
  - 此路径**放弃**「首 chunk 转 JSON 错误」能力：既然已提交 200，上游若最终报错，则以 SSE error 帧下发（Anthropic 流本就允许 error 事件；这是慢路径才付的代价，用户已认可）
  - **断连**：一旦交出 `StreamingResponse`，改由 Starlette 的 `listen_for_disconnect` + 生成器 `GeneratorExit` 路径处理（`async_streaming_data_generator` 的 `except (CancelledError, GeneratorExit)` 已负责退款 + 关上游，`:2534-2550`）。三方竞速里若断连先到，仍走现有的取消 + 499 路径（`:391-402`），不受影响

### 面 2 — chunk 间隙「保活组合子」

新增纯组合子 `_sse_keepalive(inner, interval, strategy)`，包在 `combined_generator`（`:528`）外层，天然覆盖三个面。核心纪律:

- 每次 `inner.__anext__()` 作为**持久 task**，与 `interval` 用 `asyncio.wait({task}, timeout=interval)` 竞速
- 超时（`task` 未完成）→ `yield` 保活帧，**继续等同一个 task**，绝不重建、绝不 cancel。**这是关键**：`asyncio.wait_for(anext(...))` 在超时会把 `CancelledError` 抛进生成器当前 await 点，等于打断上游 read——必须避免。复用现有 `_buffer_first_chunk_honoring_disconnect:375` 同款「wait 而非 wait_for」写法
- task 完成 → 取结果；`StopAsyncIteration` 则结束；否则 `strategy.observe(chunk)`（更新面 1→面 2 状态）后 `yield chunk`，再建下一个 `anext` task
- 生成器被 close（客户端断连）→ 取消 pending task 并 `aclose` inner，保持 `_UpstreamClosingStreamingResponse` 的关闭语义

### 保活帧：格式感知策略对象（按面注入）

三个调用点各传入一个 `KeepaliveStrategy`（依赖注入，便于单测传假实现）:

- **通用保底（三个面都发）**：SSE 注释 `: ping\n\n`。SSE 协议级注释，所有合规解析器（含 Anthropic SDK 的 `SSEDecoder`，按 spec 跳过 `:` 开头的注释行）直接忽略，且任何字节到达都会重置客户端 httpx 的 read 超时。message_start 前后都合法、不碰事件状态机——这是能覆盖面 1（尚未发 message_start）的唯一安全帧
- **Anthropic 面 2 叠加（策略观测到 `event: message_start` 之后）**：额外发原生 `event: ping\ndata: {"type": "ping"}\n\n`。满足用户选择的「防御纵深 / 两者都发」——注释 + 原生 ping 两套机制。原生 ping「任意位置合法」，Anthropic 真流也会在 message_start 后、content_block_start 前发 ping，故安全
- **面 1（message_start 前）只发注释**：Anthropic 自己从不在 message_start 前发 ping，严格客户端对「message_start 前的 ping」行为未知 → 留作 PoC 验证项，默认不发，仅注释
- **OpenAI chat / responses**：仅注释。这两个面无原生心跳事件；合成空 delta chunk 有污染下游组装（token 计数 / 工具参数拼接）的风险，不做

策略状态（是否已见 message_start）通过 `observe(chunk)` 推进。该状态是流式状态机固有的演进状态，实现时优先用「在组合子循环里以局部布尔 fold」而非对象字段 mutate（满足 LIT001/LIT002 与 no-mutation 强默认）；`KeepaliveStrategy` 本身用 frozen dataclass 承载不变配置（interval、面 1/面 2 帧模板）+ 一个纯函数 `idle_frames(seen_message_start: bool) -> tuple[str, ...]`。

#### record-not-adopted：为何不用空 `content_block_delta`

用户被给到「原生 ping / 空 text_delta」选项并选了「两者都发」。落地选「注释 + 原生 ping」而非空 text_delta，理由：空 `content_block_delta` 的 `text_delta` 需要一个**当前开放的 text 内容块 index**，若发在 `content_block_stop` 之后、下一个 `content_block_start` 之前，或发在 tool_use / thinking 块内，会破坏客户端的块组装（index 错配、非 text 块收到 text_delta）。原生 `ping` 无此记账负担、任意位置合法，更稳。空 text_delta 变体不采用。

### 上限与兜底

无独立上限，保活**只跟随上游存活**（用户选择）。真正挂死的兜底是关联的上游 HTTP client spec 里的 **read gap 超时**（两段字节间隔超时）+ **total asyncio 绝对 deadline**——上游到点抛异常，经 `async_streaming_data_generator` 的 `except Exception`（`:2551`）转成 SSE error 帧下发，保活随之停止。

**显式依赖记录（写入 BACKLOG 提醒）**：若用户未配置上游 read/total 超时，则 half-open 挂死的上游会让保活一直发帧，直到 copilot socket 自身断开或客户端断连。本设计不为此加独立 cap（尊重用户选择），但在配置文档中明确提示「保活应与上游超时配套使用」。

### 配置表面

沿用上游 http_client spec 的 yaml 风格，独立命名空间 `stream_keepalive`:

```yaml
litellm_settings:
  stream_keepalive:          # 全局兜底
    enabled: true            # 默认开（注释帧惰性、零副作用）
    interval: 15             # 空闲多少秒发一帧，默认 15s

model_list:
  - model_name: github_copilot/claude-opus-4.8
    litellm_params:
      stream_keepalive:      # 可选覆盖全局
        interval: 15
```

- `interval` 默认 15s，安全低于常见 30–60s 的下游空闲超时（nginx `proxy_read_timeout` 默认 60s；多数客户端 30–60s）
- `enabled` 默认 `true`；`enabled=false` 时**字节级等同现状**（不进 `_sse_keepalive`、不改 `_buffer_first_chunk` 竞速）
- Pydantic `frozen` 模型边界校验（`interval>0`、未知键报错，不放 `Any` 下游）；全局在 proxy 加载边界立即校验，per-deployment 在 model-list 构造边界校验
- per-deployment 覆盖全局；解析产出 typed 内部配置，**不进 provider 映射、不泄漏上游**（保活是纯下游行为，本就不该出现在上游 body，但仍加 wire-body 回归断言）

## 组件设计

### 1. 配置模型（新建 `litellm/proxy/common_utils/stream_keepalive_config.py` 或就近于 common_request_processing）

- `StreamKeepaliveConfig`（Pydantic `frozen`）：`enabled: bool = True`、`interval: float`（>0 校验，默认 15）
- `parse(raw) -> StreamKeepaliveConfig`：边界校验
- `merge(global_cfg, deployment_cfg)`：deployment 非 None 键覆盖全局

### 2. 保活策略（`KeepaliveStrategy`）

- frozen dataclass，承载 `interval` + 面 1 帧（注释）+ 面 2 帧模板
- `AnthropicKeepaliveStrategy`：面 1 = 注释；面 2 = 注释 + `event: ping`；`observe` 检测 `event: message_start` 字节翻面
- `CommentOnlyKeepaliveStrategy`（chat / responses）：两面均仅注释；`observe` no-op
- 纯函数 `idle_frames(seen_message_start) -> tuple[str, ...]`

### 3. `_sse_keepalive` 组合子（common_request_processing）

- `async def _sse_keepalive(inner, strategy) -> AsyncGenerator[str, None]`：面 2 逻辑（持久 task + `asyncio.wait` 竞速 + fold 状态 + close 传播）

### 4. `create_response` / `_buffer_first_chunk_honoring_disconnect` 改造

- 新增 `keepalive: Optional[KeepaliveStrategy]` 参数（None → 关闭，走原路径）
- 首 chunk 竞速加入 interval 定时器（三方竞速）；慢路径构造「保活帧 + pending 首 chunk task + 续流」的生成器，并以 `_sse_keepalive` 包裹面 2
- 保留：错误首 chunk 转 JSON（仅快路径命中）、断连退款 / 499、`_UpstreamClosingStreamingResponse` 关闭、DD tracing 两条路径

### 5. 三个调用点接线

- anthropic 分支（`:1587`）传 `AnthropicKeepaliveStrategy`
- select_data_generator 分支（`:1618`）传 `CommentOnlyKeepaliveStrategy`
- responses（`common_request_processing.py:1618` 的 select_data_generator 分支和/或 `proxy_server.py:10145`，plan 阶段核准）传 `CommentOnlyKeepaliveStrategy`
- 各自从 global + per-deployment `stream_keepalive` 解析配置；`enabled=false` 传 None

### 未采用的现成库（spot-unneeded-homegrown）

评估过 `sse-starlette` 的 `EventSourceResponse(ping=N)`：它验证了 `: ping` 注释心跳路线，但**只覆盖面 2**、发不了原生 ping、且接不进 litellm 自有的首 chunk 缓冲 / 错误检测 / 断连机制。换用它要大改 `create_response` 全栈且只解决次要问题，故不采用，仅借鉴其注释心跳做法。`_sse_keepalive` 本体约 15 行标准「stream heartbeat」模式，手写合理，无更贴合的库。

## 测试（对齐 CLAUDE.md：能被 mutate 时失败，>90% kill）

- **组合子（重点）**：慢 fake gen（2×interval 无 chunk）→ 断言恰好发 N 帧、间隔 ≈interval；快 gen → 零保活帧；**断言 producer task 未被 cancel**——ping 后上游 `anext` 结果仍被转发（mutate 成 `wait_for` 应失败）
- **面 1 延迟提交（重点）**：首 chunk 于 2×interval 后到 → 响应已提交、头已发、保活帧先于首真 chunk；快路径错误流（首 chunk 在 interval 内且是错误）→ 仍走 JSON 错误响应（现行为保住，mutate 掉快路径分支应失败）
- **Anthropic 面 2**：观测到 `event: message_start` 后空闲 → 注释 + `event: ping` 都发；之前空闲 → 仅注释（mutate 掉 observe 翻面应失败）
- **OpenAI / responses**：空闲 → 仅注释，绝不合成 content chunk
- **面 1/面 2 断连**：断连 → 退款 / 499 仍正确、上游被关（`aclose` 被调）
- **上游中途报错**：idle 中上游抛错 → 以 error 帧下发、保活停
- **`enabled=false`**：零保活帧、字节级等同现状（对拍现有输出）
- **配置**：`parse` 边界（interval≤0 / 未知键报错）；`merge` 覆盖；wire-body 不含 `stream_keepalive`（三面各一）
- **PoC（面 1 原生 ping）**：验证「message_start 前发 `event: ping`」是否被 Anthropic SDK / Claude Code 接受；若安全，可把面 1 也升级为注释 + ping。默认保守只发注释

## 落地顺序

1. 组件 1（配置模型 parse/merge）+ 组件 2（策略对象）+ 纯单测
2. 组件 3（`_sse_keepalive` 组合子）+ 组合子单测（含 producer-not-cancelled、慢/快、断连传播）
3. 组件 4（`create_response` 三方竞速 + 慢路径提交）+ 面 1 延迟提交测试 + 快路径回归
4. 组件 5 三个调用点接线 + 三面端到端 + wire-body 回归 + `enabled=false` 对拍
5. PoC：面 1 原生 ping 兼容性；BACKLOG 记「保活须与上游超时配套」的依赖提醒
