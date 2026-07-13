# 下游 SSE 保活（two-face keepalive，防下游 idle/read 超时）

状态：设计经 round-1 GPT reviewer 对抗性评审（6 阻塞 + 6 建议），本版已吸收全部条目，待 round-2 复审 + 用户复核
日期：2026-07-14 初稿；同日 round-1 评审后修订
分支：`ghc`
关联：[上游 HTTP client 细粒度配置](./2026-07-13-upstream-http-client-config-design.md)（**尚未实现**，仅设计稿；本 spec 的「上游真挂死」兜底当前依赖既有默认上游 read 超时，见「上限与兜底」）

## 修订说明（round-1 评审吸收）

初稿经一轮 GPT reviewer 对抗性评审（提取锁定 wheel 的 Anthropic SDK 0.84.0 / OpenAI SDK 2.33.0 源码核对，跑了 `asyncio.wait_for` 取消传播微实验）。吸收的决定性结论:

1. **Anthropic native passthrough 转发的是 `aiter_bytes()` 任意字节块，不是完整 SSE frame**——在两次字节块之间插保活注释会落进未闭合的 `data:` 行内部，损坏 JSON / event。保活**必须**先把上游字节规范化成完整 frame，只在 frame 边界注入（blocker 1）
2. 跨 `create_response` 携带 pending `__anext__` task 有**响应级资源所有权 + 关闭顺序**问题（未启动 body 的 `aclose()` 是 no-op、pending anext 运行中被 `_UpstreamClosingStreamingResponse` 关上游会 `RuntimeError` / 泄漏 task、disconnect watcher 必须在交出响应前拆除以免和 Starlette 抢 ASGI `receive`）（blocker 2）
3. 「提前提交后错误一律变 SSE error 帧」**不成立**：`HTTPException` 被 producer 重新抛出（非序列化）会直接 abort 连接；Anthropic 通用异常 serializer 产出的是**无 `event: error`** 的 `data: {"error":...}`，Anthropic SDK 只在 `event=="error"` 时抛错，故该帧被客户端忽略。需按 surface 的 **post-commit error mapper**（blocker 3）
4. `create_response` 是三面共同咽喉，但 `common_request_processing.py:1618` 是**所有带 `select_data_generator` 的 route 共用分支**（含 `/v1/completions`）；接线必须**按 `route_type` 分派**，不能在 `:1618` 无条件注入。且 `proxy_server.py:10145` 是 Assistants runs，**不是** Responses——真实 responses 路径是 `response_api_endpoints/endpoints.py:200` 的 `route_type="aresponses"`（blocker 4）
5. `stream_keepalive` 若进 `litellm_params` 会被 Router 展开进上游调用（`router.py:1633/2657/4341`）；仅定义 Pydantic 模型或在 `create_response` 处读取**挡不住泄漏**。须正式注册 + provider 映射前消费 + wire-body 回归（blocker 5）
6. resolved 模型自带默认值会让「部分 override」失真（deployment 只写 `enabled:false` 会把 interval 意外重置为默认）。须区分 **Override 类型（全 Optional 无默认）** 与 **Resolved 类型**（blocker 6）
7. 保活只能重置 idle/read 超时，**延不了固定 wall-clock 绝对 deadline**；目标措辞收紧，且 Claude Code 实际触发哪种超时须 PoC（建议 7）
8. default-on 与未实现的上游 total 超时的依赖关系要形成发布门槛 / 启动 warning（建议 8）
9. 首 chunk / disconnect / timer 同 scheduler turn 完成的**优先级须冻结**为 `disconnect > 已完成首 chunk > timer`（建议 9）
10. 慢路径首个真 chunk 仍须经既有 DD tracing / hooks，只有合成 ping 绕过（建议 10）
11. 「局部布尔 fold 满足 no-mutation」表述不对（CLAUDE.md 禁止局部重赋值）——用**两阶段生成器**按控制流切换，不维护可变 flag（建议 11）
12. 测试补齐半帧注入 / 同轮优先级 / 未启动 body 取消 / 三类错误契约 / tracing / cost / 部分覆盖等竞态与协议用例（建议 12）

已核实成立的原设计判断（评审背书）：持久 task + `asyncio.wait(..., timeout=)` 方向正确（`wait_for(anext())` 确会 cancel producer）；`: ping\n\n` 被两 SDK 的 SSE 解析安全忽略；Anthropic SDK 在低层跳过 `event: ping`（故 message_start 前 native ping 不进高层 accumulator，但客户端版本仍留 PoC）；不采用空 `content_block_delta` 的理由成立（accumulator 按 `event.index` 访问当前块，空 text_delta 在无开放 text 块 / tool_use / thinking 阶段有真实状态机风险）。

## 背景与问题

Claude Code 客户端经 litellm 代理打 `/v1/messages` 到 github_copilot 上游。慢推理模型在**首 token 之前**可能沉默数十秒，生成中途事件间也可能长间隙。litellm 现在对上游沉默**原样透传**、不注入任何保活字节，导致下游客户端的 **idle/read 超时**触发、连接被客户端断开，一次本可成功的请求失败。

已核实的现状:

1. **litellm 自己完全不发 ping**。github_copilot 的 Claude 模型走 native passthrough，上游 Anthropic SSE **字节**被逐字转发（`litellm/proxy/pass_through_endpoints/streaming_handler.py:57-61` `async for chunk in response.aiter_bytes(): yield chunk`）。全库无 `event: ping` 注入点
2. **TTFB 窗口连 HTTP 200 响应头都还没发**。`create_response`（`litellm/proxy/common_request_processing.py:405`）先 `await` 缓冲第一个 chunk（`_buffer_first_chunk_honoring_disconnect`，`:354`，在 `:433` 调用）**才**把 `StreamingResponse` 交给 Starlette。目的：把「首 chunk 就是错误」转成干净 JSON 错误响应（`:437-460`）+ 在 TTFT 期间检测断连取消上游（LIT-3568，`:358-367`）。后果：等 copilot 首字节的整个窗口，客户端连响应头都收不到
3. 下游超时有两个面：**面 1 — TTFB（响应头还没发）**、**面 2 — chunk 间隙（流已开始）**

### 目标超时类型（措辞收紧，blocker 7）

本设计**只**解决下游 **idle / read（两段字节间隔）超时**：任何字节到达即重置客户端 httpx 的 read 计时器。**不**承诺解决客户端侧固定的 wall-clock 绝对请求 deadline——那类计时器收到 ping 也不会延长。落地前 PoC 须确认 Claude Code 实际触发的是 inactivity/read 计时器（见「PoC」）。

### 三个 SSE 面汇于一个共享咽喉（已核实，blocker 4 修正）

真实上游流式成功路径全部经 `create_response`；但它是**共享**咽喉，非目标面专用:

| 面 | route_type | 到达 `create_response` | 目标? |
|---|---|---|---|
| anthropic_messages | `anthropic_messages` | `endpoints.py:95` → `common_request_processing.py:1574` → `:1587` | 是 |
| chat | `acompletion` | `proxy_server.py:8472` → 共享 `select_data_generator` 分支 `:1594` → `:1618` | 是 |
| responses | `aresponses` | `response_api_endpoints/endpoints.py:200`（cursor 变体 `:394`）→ container-ownership wrapper `:1601` → 共享 `:1618` | 是 |
| text completions | `acompletion`(`/v1/completions`) | 同走 `:1618` 共享分支（`proxy_server.py:8626`） | **否**（默认 None） |
| Assistants runs | — | `proxy_server.py:10145` `/v1/threads/{id}/runs`，独立 `create_response` 调用 | **否** |

因 `:1618` 是共享分支，**注入必须按 `route_type` 分派**，不能在 `:1618` 无条件注入（否则误覆盖 `/v1/completions`）。`proxy_server.py:8518/8556` 的直接 `StreamingResponse` 只在 guardrail-passthrough / rejected 异常分支，是即时合成短流，不动。

## 冻结的 route → strategy 映射（blocker 4）

| route_type | strategy | keepalive 参数 |
|---|---|---|
| `anthropic_messages` | `AnthropicKeepaliveStrategy` | 传入 |
| `acompletion`（仅 `/chat/completions`，非 `/v1/completions`） | `CommentOnlyKeepaliveStrategy` | 传入 |
| `aresponses`（含 cursor 变体） | `CommentOnlyKeepaliveStrategy` | 传入 |
| 其它（text completions / Assistants / …） | 无 | `None`（走原路径，字节级不变） |

`acompletion` 需按端点区分 `/chat/completions` 与 `/v1/completions`；实现时以调用点传入 strategy 而非在 `:1618` 内按 route_type 猜，避免共享分支误伤。

## 目标

在三个目标面的真实上游流式路径注入周期性保活字节，覆盖面 1 / 面 2，使上游沉默不再触发下游 idle/read 超时；快路径下游 **payload 字节不变**；可配置、默认开启、`enabled=false` 时完全回退现状且下游字节级等同今日。

## 非目标

- 客户端侧配置（Claude Code `API_TIMEOUT_MS` 等）、上游保活 / 上游超时（后者由关联 upstream spec 负责，本设计仅依赖）
- 保活的独立上限 / 最大时长（用户选择「只跟随上游存活」）
- 客户端固定 wall-clock 绝对 deadline（保活物理上延不了，blocker 7）
- guardrail-passthrough / rejected 合成短流、`/v1/completions`、Assistants runs 的保活
- 空 `content_block_delta` 保活变体（record-not-adopted，理由见下）

## 设计（two-face keepalive）

### 前置层 — Anthropic 字节流帧规范化（blocker 1，仅 native passthrough 需要）

问题：native passthrough 内层是 `aiter_bytes()`，产物是**任意字节块**，可能在 `event:` / `data:` / UTF-8 多字节 / JSON 中间分片。若在此插 `: ping\n\n`，会落进未闭合行内损坏流。

方案：为 Anthropic native passthrough 路径插入一层 **frame normalizer**，把上游字节累积到 `\n\n` 边界后按**完整 SSE frame** 逐个交给上层；未闭合的残片**暂不下发**（保留在 normalizer 缓冲）。这样:

- 保活只在 normalizer「当前无半帧在缓冲、且已把已完成帧全部下发」时注入——此刻下游处于 frame 边界，插 `: ping\n\n` 安全
- 若上游发了半帧后沉默：normalizer 尚未下发该半帧，仍可安全在其**之前**注入保活（下游未见半帧）
- 保留最大未闭合 frame 上限（防上游恶意不闭合撑爆内存）与 EOF 残片下发策略

复用 `proxy_server.py:6902-6915` 已有的 SSE delimiter 逻辑（抽取为共享 helper，避免 API-fragmentation 各写一份）。chat / responses 面内层产物已是**完整 frame 字符串**（`return_sse_chunk` 把 dict 格式成整帧；OpenAI generator 产出整帧），**无需** normalizer。

### 面 2 — chunk 间隙「保活组合子」

纯组合子 `_sse_keepalive(inner, strategy)`，包在 `combined_generator`（`:528`）外层。核心纪律:

- 每次 `inner.__anext__()` 作为**持久 task**，与 `interval` 用 `asyncio.wait({task}, timeout=interval)` 竞速；**绝不** `wait_for(anext())`（会 cancel producer 打断上游 read，评审微实验已复现）
- 超时（task 未完成）→ `yield` 保活帧，**继续等同一 task**
- task 完成 → 取结果（`StopAsyncIteration` 结束；异常见「错误契约」）→ `strategy.observe(frame)` 更新阶段 → `yield frame` → 建下一个 task
- **同轮优先级冻结（blocker 9）**：`wait` 超时返回后若 task 已 done，**先消费真实 frame，不插 ping**
- 生成器被 close（断连）→ 取消 pending task、shield 下 `await` 确认取消进入 producer、再 `aclose` inner（见「所有权与关闭协议」）

### 面 1 — TTFB「延迟提交」（blocker 2 重写所有权协议）

把首 chunk 竞速从「首 chunk / 断连」扩成「首 chunk / interval 定时器 / 断连」三方，返回**强类型 tagged union**（不返回裸 pending task）:

```
FirstChunkRace =
  | Disconnected                                   # 断连先到
  | FirstChunk(frame: str)                         # interval 内到达（快路径）
  | SlowCommit(producer_task, inner, strategy)     # interval 先到，须提交流
```

- **同轮优先级冻结（blocker 9）**：`disconnect > 已完成首 chunk > timer`。与现有 `:375-389` 规则一致（disconnect watcher 已消费 ASGI 消息，必须优先）
- **`FirstChunk` 快路径** → 完全保持现行为：错误首 chunk 转 JSON、正常首 chunk 走 `combined_generator`、断连转 499。下游 payload 字节不变
- **`SlowCommit` 慢路径**:
  - **交出响应前**必须先取消并完整 `await` 旧 `_wait_for_http_disconnect` task（`:381-386` 同款），否则它与 Starlette `listen_for_disconnect` 抢同一 ASGI `receive`、可能偷走 `http.disconnect`
  - 返回 `_UpstreamClosingStreamingResponse`，其 body 生成器：先 `yield` 面 1 保活帧，把 `producer_task`（pending 首 `__anext__`）**原样带入**、以 `interval` 竞速边等边发保活，首个真 frame 到达后 `yield`，随后 `async for` 续流（面 2 组合子接管）
  - **所有权与关闭协议（blocker 2）**：慢路径的 `SlowCommit` 成员**拥有** producer_task + inner + 关闭动作。响应级 owner（`_UpstreamClosingStreamingResponse` 或其 body 生成器的 `finally`）在 close 时必须：`producer_task.cancel()` → shield 下 `await producer_task`（确认取消已进入 producer，吞异常）→ 再 `await inner.aclose()`。**顺序不可颠倒**：直接关 inner 而 pending anext 仍在跑会 `RuntimeError: aclose(): asynchronous generator is already running` 并泄漏 task。即使 Starlette 从未启动 body（`aclose()` 对未启动生成器是 no-op），该清理也必须由响应对象自身可执行
  - 慢路径**放弃**「首 chunk 转 JSON 错误」（已提交 200）；上游后续错误按「错误契约」以 surface 可识别的 SSE error 下发。仅慢路径付此代价

### 错误契约（blocker 3，提交后按 surface 映射）

提前提交 200 后，后续异常必须变成**客户端可识别**的 SSE error，而非 abort 或被忽略的帧。现状两个缺口:

- `HTTPException` 被 `async_streaming_data_generator:2566` / `proxy_server.py:7077` 重新抛出 → Starlette abort，无 error 帧
- Anthropic 通用 serializer（`common_request_processing.py:2603-2611`）产出无 `event: error` 的 `data: {"error":...}` → Anthropic SDK（`event=="error"` 才抛）忽略之。**注**：此为既有 latent bug（今日 mid-stream Anthropic 错误也不可见），延迟提交会放大其触发面，故一并修

方案：**post-commit error mapper**，按 surface:

- **anthropic**：输出至少 `event: error\ndata: {"type":"error","error":{...}}\n\n`（SDK 可识别）
- **chat / responses**：保留其 SDK 可识别的错误形态（chat 现有 `data: {"error":...}` + `[DONE]`；responses 用其 iterator 的 failure 形态）
- 区分「producer 已生成合法错误帧」与「pending task 以异常结束」两种来源；`HTTPException` 也须被 mapper 捕获转帧而非重新抛
- **failure hook 只触发一次（blocker 3 + 12）**：明确归属——producer 内 `except` 已调 `post_call_failure_hook` 的，mapper 不得重复调；mapper 兜底的分支才调。验收覆盖三类来源（HTTPException / 普通异常 / producer 已序列化 error）× 每类日志恰一次

### 保活帧：格式感知策略（两阶段生成器，blocker 11）

- **通用保底（三面都发）**：SSE 注释 `: ping\n\n`。协议级注释，两 SDK 的 SSE 解析安全忽略，任何字节重置客户端 read 计时器，frame 边界注入安全
- **Anthropic 面 2 叠加（观测到 `event: message_start` 之后）**：额外发原生 `event: ping\ndata: {"type": "ping"}\n\n`。这是**协议层防御纵深**（非独立的第二套 timeout 修复机制——传输层保活由注释单独完成，blocker/建议 措辞）。Anthropic 真流在 message_start 后、content_block_start 前也发 ping，安全
- **面 1（message_start 前）只发注释**：Anthropic 自身从不在 message_start 前发 ping，严格客户端行为留 PoC
- **OpenAI chat / responses**：仅注释（无原生心跳事件；合成空 delta 有污染下游组装 / usage 的风险）

**状态用两阶段生成器表达，不维护可变 flag（blocker 11）**：`AnthropicKeepaliveStrategy` 的空闲注入拆成阶段 1（未见 message_start，仅注释）与阶段 2（已见，注释 + ping），由控制流从阶段 1 切到阶段 2（观测到 message_start frame 时），而非重赋值 `seen_message_start`。`observe(frame)` 检测的是完整 frame（已由 normalizer 保证），不做字节子串匹配。task 等并发状态用局部所有权 + `try/finally` 生命周期，不塞进可变 strategy 字段。

#### record-not-adopted：不用空 `content_block_delta`

空 `content_block_delta` 的 `text_delta` 需当前开放的 text 块 index；发在 `content_block_stop` 后、下一个 `content_block_start` 前，或 tool_use / thinking 块内会破坏客户端组装（评审据 SDK accumulator `event.index` 访问逻辑背书）。原生 `ping` 无记账负担、任意位置合法，更稳。

### 层次顺序（blocker 10，显式冻结）

```
上游 producer（含 async_post_call_streaming_hook / cost injection / guardrail / spend logging）
  → [Anthropic native: frame normalizer]
  → combined_generator（真实 chunk 的 DD span / first-chunk 处理）
  → _sse_keepalive（仅注入合成 ping）
  → Starlette
```

合成 ping **不得**进 `_process_chunk_with_cost_injection`（`:2502-2524`）、guardrail、usage 统计、DD per-model-chunk span。**所有真实 frame（含慢路径 pending task 得到的首帧）仍须经既有 tracing / hooks**。因 `_sse_keepalive` 在 `combined_generator` 外层、只产出注释/ping 字符串，天然不进内层 hooks；须验收慢路径首真帧仍进 `combined_generator` 的 DD span。

### 上限与兜底（blocker 8）

无独立上限，保活**只跟随上游存活**（用户选择）。「上游真挂死」的兜底:

- **当前**：既有上游 httpx 默认 read 超时（litellm 默认 timeout 语义，通常 600s）——half-open 挂死的上游在既有 read 超时到点抛异常 → 经错误契约以 error 帧下发、保活停。故**非无限**，上界约为既有上游 read 超时
- **关联 upstream spec 实现后**：由其 read gap + total asyncio deadline 提供更细的兜底
- **发布门槛（blocker 8）**：`enabled=true` 且检测不到有效上游 read/total 超时时，启动 warning，提示「保活应与上游超时配套」。BACKLOG 记依赖 + 可观测性（active keepalive streams / ping count / stream age / timeout termination 指标）

## 配置表面（blocker 5/6）

沿用上游 http_client spec 风格，独立命名空间 `stream_keepalive`:

```yaml
litellm_settings:
  stream_keepalive:          # 全局兜底
    enabled: true
    interval: 15

model_list:
  - model_name: github_copilot/claude-opus-4.8
    litellm_params:
      stream_keepalive:      # 部分覆盖全局
        interval: 15
```

**Override vs Resolved 双类型（blocker 6）**:

- `StreamKeepaliveOverride`（Pydantic `frozen`，`extra="forbid"`）：`enabled: bool | None = None`、`interval: float | None = None`（有限、`>0`、设合理最小值如 1s 拒绝忙循环）。**全 Optional 无默认**，据 `model_fields_set` / 非 None 键 merge
- `merge(global_override, deployment_override) -> StreamKeepaliveOverride`：deployment 已设字段覆盖 global
- `resolve(merged) -> ResolvedStreamKeepaliveConfig`（frozen，字段完整）：`enabled=True`、`interval=15` 兜底。**默认只在 resolve 末端加一次**，故「deployment 只写 enabled:false」不会重置 global interval

**注册与防泄漏（blocker 5）**:

- 在 `GenericLiteLLMParams` / 相关 typed dict 正式注册 `stream_keepalive`（nested Override 模型校验），使其**不进** provider 可见 kwargs
- Router 选定 deployment 后，从 provider-visible 调用副本**消费** `stream_keepalive`（pop），保留到 typed downstream context / deployment metadata；不靠 provider mapper 碰巧丢弃
- 全局 `litellm_settings.stream_keepalive` 在 proxy 加载边界立即校验（非 raw `setattr`）
- `base_process_llm_request` 取得选中 deployment 后解析 global + deployment override → resolve → 按 route_type 建 strategy；`enabled=false` → 传 `None`
- **wire-body 回归**：chat / responses / messages 三面真实 Router 路径断言上游 body 不含 `stream_keepalive`

## 组件设计

1. **配置**（新建 `litellm/proxy/common_utils/stream_keepalive_config.py`）：`StreamKeepaliveOverride` / `ResolvedStreamKeepaliveConfig` / `parse` / `merge` / `resolve`
2. **策略**：`KeepaliveStrategy` 基（frozen，承载 interval + 帧模板）；`AnthropicKeepaliveStrategy`（两阶段 + `observe`）；`CommentOnlyKeepaliveStrategy`（仅注释，`observe` no-op）。组合优于继承——策略作为注入对象
3. **Anthropic frame normalizer**（抽取自 `proxy_server.py:6902-6915` 的共享 SSE delimiter helper）：仅 anthropic native passthrough 接线
4. **`_sse_keepalive` 组合子**（common_request_processing）：面 2 逻辑
5. **`create_response` / 首 chunk 竞速改造**：三方竞速 + `FirstChunkRace` tagged union + 慢路径提交 + 所有权/关闭协议 + post-commit error mapper 接线；新增 `keepalive: KeepaliveStrategy | None` 参数（None → 原路径）
6. **三调用点接线**：anthropic 分支 `:1587` 传 Anthropic strategy；chat `/chat/completions` 调用点传 CommentOnly；responses `response_api_endpoints/endpoints.py:200`（+cursor `:394`）传 CommentOnly。各自解析 global + deployment override

### 未采用的现成库（spot-unneeded-homegrown）

`sse-starlette` 的 `EventSourceResponse(ping=N)` 验证了 `: ping` 注释心跳路线，但只覆盖面 2、发不了原生 ping、接不进 litellm 自有的首 chunk 缓冲 / 错误检测 / 断连机制，换用要大改且只解决次要问题，不采用，仅借鉴。`_sse_keepalive` 是标准「stream heartbeat」模式，手写合理。

## 测试（对齐 CLAUDE.md：能被 mutate 时失败，>90% kill；blocker 12）

注入 timer/clock 或事件屏障控制 producer，**避免只靠墙钟 `≈interval` 的易抖测试**；把持久 task mutate 成 `wait_for`、漏 await task、优先级反转时须稳定失败。

- **组合子**：慢 fake gen（2×interval 无帧）→ 恰好 N 帧、producer task 未被 cancel（ping 后上游结果仍转发）；快 gen → 零保活帧
- **半帧注入（blocker 1，重点）**：Anthropic raw SSE 在 `event:` / `data:` / UTF-8 多字节 / JSON 中间分片，停顿期间断言 ping **不落进半帧**、下游只见完整 frame
- **面 1 延迟提交**：首 chunk 于 2×interval 后到 → 已提交、头已发、保活帧先于首真帧；快路径错误流 → 仍走 JSON 错误（现行为保住）
- **同轮优先级（blocker 9）**：interval / 首 chunk / disconnect 同 turn 完成 → 断言 `disconnect > chunk > timer`
- **未启动 body 取消（blocker 2，重点）**：`StreamingResponse` 已建但 body 从未启动即取消 → pending task 完成取消、上游关闭、`asyncio.all_tasks()` 无孤儿
- **错误契约（blocker 3，重点）**：producer 分别以 `HTTPException` / 普通异常 / 已序列化 error frame 结束 → 每 surface 客户端可识别错误格式；failure hook 每类恰一次
- **Anthropic 两阶段**：见 message_start 后空闲 → 注释 + `event: ping`；之前 → 仅注释
- **OpenAI / responses**：空闲 → 仅注释，绝不合成 content chunk
- **断连各资源恰一次**：首 ping 已发、首真帧未到时断连 → max-parallel release / budget reservation release / 499 metadata / deferred logging 各恰一次
- **tracing / cost 隔离**：DD 开关两条路径断言只跟真实 chunk 不跟 ping；`include_cost_in_streaming_usage=True` 时 ping 不进成本注入、终态 usage 不变
- **配置**：global + deployment 部分覆盖（`enabled:false` 不重置 global interval）、未知键 `extra=forbid`、`NaN`/`inf`/极小 interval 拒绝；三面 wire-body 无泄漏
- **aresponses background polling**：`response_polling/background_streaming.py:153-165` 直接迭代 `body_iterator`（非真实下游 SSE 客户端）——断言合成注释被忽略且不破坏该路径
- **`enabled=false`**：零保活帧、下游字节级等同现状（对拍）

## PoC（进 plan 前）

- **面 1 原生 ping 兼容性**：验证「message_start 前发 `event: ping`」是否被 Claude Code 实际客户端接受；安全则可把面 1 也升级为注释 + ping，否则维持仅注释
- **Claude Code 超时类型（blocker 7）**：真实 E2E——上游首字节延迟超过原失败阈值，确认无 keepalive 时断开、启用后成功；另做固定 total deadline 对照，明确该类不在承诺内

## 落地顺序

1. 组件 1（Override/Resolved parse/merge/resolve）+ 组件 2（策略两阶段）+ 纯单测
2. 组件 3（frame normalizer 抽取共享 helper）+ normalizer 单测（半帧 / 多字节 / 上限 / EOF）
3. 组件 4（`_sse_keepalive`）+ 组合子单测（producer-not-cancelled / 同轮优先级 / close 传播）
4. 组件 5（`create_response` 三方竞速 + `FirstChunkRace` + 慢路径提交 + 所有权/关闭协议 + post-commit error mapper）+ 面 1 / 未启动 body / 错误契约测试 + 快路径回归
5. 组件 6 三调用点接线 + 配置注册/防泄漏 + 三面端到端 + wire-body + `enabled=false` 对拍 + 启动 warning
6. PoC（面 1 原生 ping / Claude Code 超时类型）；BACKLOG 记 upstream-timeout 依赖门槛 + 可观测性指标
