# 下游 SSE 保活（two-face keepalive，防下游 idle/read 超时）

状态：设计经两轮 GPT reviewer 对抗性评审（round-1 6 阻塞 / round-2 6 阻塞，均全程核对真实代码 + SDK 源码），本版冻结全部契约，待用户复核 gate
日期：2026-07-14 初稿；同日 round-1、round-2 评审后两次修订
分支：`ghc`
关联：[上游 HTTP client 细粒度配置](./2026-07-13-upstream-http-client-config-design.md)（**尚未实现**，仅设计稿）

## 修订说明（两轮评审吸收）

round-1 命中 6 阻塞（半帧注入 / task 所有权 / 错误契约 / 接线图错 / 配置泄漏 / 部分覆盖失真）+ 6 建议，全部吸收。round-2 复核确认 4 项彻底解决（upstream 依赖门槛、同轮优先级、测试清单、route 核心修正），其余收紧为**契约冻结**，本版据此定稿。评审背书成立的原判断：持久 task + `asyncio.wait(..., timeout=)` 方向正确（`wait_for(anext())` 确会 cancel producer）；`: ping\n\n` 被 Anthropic SDK 0.84.0 / OpenAI SDK 2.33.0 的 SSE 解析安全忽略；Anthropic SDK 低层跳过 `event: ping`；不采用空 `content_block_delta`（accumulator 按 `event.index` 访问当前块）。

round-2 冻结的 6 项契约（本版全部落定）:

1. **frame normalizer 必须 bytes 安全**：以 `bytes` 缓冲 + 查找 ASCII delimiter（不逐 chunk `decode(errors="replace")`，否则跨 chunk UTF-8 断成 replacement char、破坏字节等价）；支持三种 delimiter `("\r\n\r\n", "\n\n", "\r\r")`；注入不变量单一化；EOF 残片策略明确；对**整个 `anthropic_messages` surface 统一施加**（adapter 完整帧输入是恒等路径，免 native/adapter 判别）
2. **唯一、幂等的响应级 `StreamLease`**：单一持有 pending task + 最内层 producer，`close()` 顺序 `cancel → shielded await → close producer`，各 wrapper 的 `finally` 只调同一个幂等 `lease.close()`，不各自级联关闭
3. **错误契约下沉到 producer 层**（比外层 mapper 猜测干净）：producer 捕获异常 → 调 failure hook → 按 surface 直接 yield 客户端可识别 error 帧 → 结束，**提交后不 re-raise `HTTPException`**；typed outcome 标注 `failure_recorded`，杜绝双发
4. **配置真实防泄漏通路**：`GenericLiteLLMParams` 注册**不够**（`extra="allow"`），须同时加入 `all_litellm_params`（`types/utils.py:3054-3074`）+ 在 Router 三条展开路径（`router.py:1633/2657/4341`）从 provider-visible local copy `pop`
5. **`SSEFrame = str | bytes`**：三面真实产物含 bytes（Anthropic native `aiter_bytes`、chat fast serializer）；慢路径首真帧经**统一 real-frame generator**（负责 DD span）再由 `_sse_keepalive` 包裹，SlowCommit body 不直接 yield 真实帧
6. **PoC 前移到 plan 之前**：Claude Code 超时类型（idle/read vs 固定 total）是功能能否解决原问题的前提，必须先验证并写回 spec

## 背景与问题

Claude Code 客户端经 litellm 代理打 `/v1/messages` 到 github_copilot 上游。慢推理模型在**首 token 之前**可能沉默数十秒，生成中途事件间也可能长间隙。litellm 现在对上游沉默**原样透传**、不注入保活字节，导致下游客户端 **idle/read 超时**触发、连接被断开，一次本可成功的请求失败。

已核实现状:

1. **litellm 自己完全不发 ping**。github_copilot Claude 模型走 native passthrough，上游 Anthropic SSE **字节**逐字转发（`pass_through_endpoints/streaming_handler.py:57-61` `async for chunk in response.aiter_bytes(): yield chunk`）
2. **TTFB 窗口连 HTTP 200 响应头都还没发**。`create_response`（`common_request_processing.py:405`）先 `await` 缓冲第一个 chunk（`_buffer_first_chunk_honoring_disconnect`，`:354`/`:433`）**才**把 `StreamingResponse` 交给 Starlette，为把「首 chunk 错误」转 JSON（`:437-460`）+ TTFT 断连检测（LIT-3568，`:358-367`）
3. 两个面：**面 1 — TTFB（响应头还没发）**、**面 2 — chunk 间隙（流已开始）**

### 目标超时类型（收紧）

**只**解决下游 **idle / read（两段字节间隔）超时**：任何字节到达即重置客户端 httpx read 计时器。**不**承诺固定 wall-clock 绝对 deadline（收 ping 也不延长）。落地前 PoC 确认 Claude Code 触发的是 inactivity 计时器。

### 三面汇于一个共享咽喉（已核实）

| 面 | route_type | 到达 `create_response` | 目标? |
|---|---|---|---|
| anthropic_messages | `anthropic_messages` | `endpoints.py:95` → `common_request_processing.py:1574` → `:1587` | 是 |
| chat | `acompletion` | `proxy_server.py:8472` → 共享 `select_data_generator` 分支 `:1594` → `:1618` | 是 |
| responses | `aresponses` | `response_api_endpoints/endpoints.py:200`（cursor `:394`）→ ownership wrapper `:1601` → 共享 `:1618` | 是 |
| text completions | `atext_completion`(`/v1/completions`) | 同走 `:1618`（`proxy_server.py:8626`） | 否（None） |
| Assistants runs | —(`/v1/threads/{id}/runs`) | `proxy_server.py:10145` 独立 `create_response` | 否 |

`:1618` 是**共享**分支，注入**必须按 surface 分派**、不能在 `:1618` 内按 route_type 猜（否则误覆盖 `atext_completion`）。`proxy_server.py:8518/8556` 的直接 `StreamingResponse` 只在 guardrail-passthrough / rejected 异常分支（即时合成短流），不动。

## 冻结的 surface → strategy 映射

| route_type | surface intent | strategy |
|---|---|---|
| `anthropic_messages` | anthropic | `AnthropicKeepaliveStrategy`（+ frame normalizer） |
| `acompletion`（仅 `/chat/completions`） | openai_chat | `CommentOnlyKeepaliveStrategy` |
| `aresponses`（含 cursor 变体） | openai_responses | `CommentOnlyKeepaliveStrategy` |
| `atext_completion` / Assistants / 其它 | — | `None`（原路径，字节级不变） |

**传参通路（冻结）**：endpoint 调用点显式向 `base_process_llm_request(downstream_sse_surface: DownstreamSSESurface | None)` 传 typed surface intent（枚举 `anthropic` / `openai_chat` / `openai_responses`），再由其构造 strategy 传入内部 `create_response`。**不**靠 URL 字符串或在共享分支重新推断；`atext_completion` 等不传（None）。background polling 内部消费（`response_polling/background_streaming.py:146-165`）显式传 `None`（非真实下游 SSE 客户端，注入 ping 只是徒增 timer/task/日志噪声）。

## 目标

三个目标面真实上游流式路径注入周期性保活字节，覆盖面 1 / 面 2，上游沉默不再触发下游 idle/read 超时；快路径下游 **payload 字节不变**；`enabled=false` 完全回退现状且字节级等同今日。

## 非目标

客户端侧配置、上游保活/超时、保活独立上限、客户端固定 wall-clock deadline、guardrail-passthrough/rejected 合成短流、`atext_completion`/Assistants/background-polling 的保活、空 `content_block_delta` 变体（record-not-adopted）。

## 设计（two-face keepalive）

### 帧类型（冻结）

`SSEFrame = str | bytes`。三面真实产物混有 bytes（Anthropic native `aiter_bytes`；chat fast serializer `_format_streaming_sse_chunk` 可返回 bytes，`proxy_server.py:6866-6899`）。所有新组件（normalizer / lease / `_sse_keepalive` / strategy）签名用 `SSEFrame`，不窄化成 `str`。保活帧本身是 `str`（ASCII），与真实帧混流由 Starlette 逐个 `body` 发送、互不干扰。

### 前置层 — Anthropic 字节流帧规范化（对整个 anthropic surface 统一施加）

问题：native passthrough 内层 `aiter_bytes()` 产物是任意字节块，可能在 `event:` / `data:` / UTF-8 多字节 / JSON 中间分片；在此插保活注释会落进未闭合行损坏流。

方案：`anthropic_messages` surface 统一插一层 **bytes 安全 frame normalizer**:

- 以 `bytes` 缓冲、查找 ASCII delimiter，支持三种 `("\r\n\r\n", "\n\n", "\r\r")`（复用 `proxy_server.py:6902-6915` 的 **delimiter 查找逻辑**抽取为共享 helper；**不复用**其 `:7008-7011` 的逐 chunk `decode(errors="replace")`——那会把跨 chunk UTF-8 断成 replacement char、破坏字节等价）
- 累积到 delimiter 后按**完整 frame** 逐个交上层，末端未闭合残片暂不下发
- **注入不变量（单一化）**：只要 normalizer **尚未向下游 yield 半帧**，下游即处于 frame 边界，可安全注入 `: ping`；buffer 内是否已有半帧不影响（半帧尚未下发）
- 最大未闭合 frame 上限（防上游不闭合撑爆内存）；**EOF 残片策略冻结**：连接正常结束但残留非空残片 → 原样下发（保持与今日「逐字转发」的兼容），并 debug 日志记异常残片
- adapter 路径（Messages→Responses/chat capability 转换，`messages/handler.py:493-540`）已输出完整帧，经 normalizer 是**恒等变换**，故无需辨别 native vs adapter

chat / responses 面内层产物已是完整帧（`return_sse_chunk` 整帧化 dict；OpenAI generator 产整帧），**不加** normalizer。

### 资源所有权 — 唯一幂等 `StreamLease`（贯穿两面）

多层 wrapper（normalizer → real-frame generator → `_sse_keepalive`）+ 慢路径 pending task 的关闭若各自级联，会 `RuntimeError: aclose() already running` / 泄漏 task / 重复退款。冻结**唯一响应级 `StreamLease`**:

- 单一持有：pending `__anext__` task（若有）+ 最内层 producer（+ container-ownership wrapper 的持久化 finally 仍归 producer 自身单次取消路径）
- `lease.close()` **幂等**，锁定顺序：`task.cancel()` → shield 下 `await task`（确认取消进入 producer、吞异常）→ `await producer.aclose()`。顺序不可颠倒（pending anext 运行中关 producer 会 RuntimeError）
- normalizer / real-frame generator / `_sse_keepalive` / `combined_generator` / `_UpstreamClosingStreamingResponse`（含未启动 body 时）的 `finally` **只调同一个** `lease.close()`，不各自关不同层
- producer 的断连退款（`common_request_processing.py:2534-2549` 收到一次 `CancelledError`/`GeneratorExit`）+ Responses ownership 持久化（`:1799-1847`）仍由 producer 自身单次路径拥有；lease 只保证「取消恰好送达一次 + 关闭顺序」
- 验收：`lease.close()` 并发调两次 → 副作用（退款 / deferred logging / 上游关闭）仍恰一次；`asyncio.all_tasks()` 无孤儿

### 统一 real-frame 管线（消解慢路径矛盾）

所有真实帧（含慢路径 pending task 得到的首帧）经**同一个 real-frame generator**，它负责 `combined_generator` 现有的 DD span（`:528-541`）与 first-frame 处理；`_sse_keepalive` 包在其外层、只产合成 ping。SlowCommit body **不直接** yield 真实帧，只 yield keepalive generator 的输出。层次冻结:

```
producer（async_post_call_streaming_hook / cost injection / guardrail / spend logging / 按 surface 出 error 帧）
  → [anthropic surface: bytes-safe frame normalizer]
  → real-frame generator（DD span / first-frame）
  → _sse_keepalive（仅注入合成 ping）
  → Starlette
```

合成 ping **不得**进 `_process_chunk_with_cost_injection`（`:2502-2524`）/ guardrail / usage / DD per-model-chunk span——它在 `_sse_keepalive` 层产生、只是 ASCII 字符串，天然不进内层 hooks。

### 面 2 — chunk 间隙「保活组合子」

`_sse_keepalive(inner, strategy)`:

- 每次 `inner.__anext__()` 作**持久 task**（由 lease 持有），`asyncio.wait({task}, timeout=interval)` 竞速；**绝不** `wait_for(anext())`
- 超时 → `yield` 保活帧，**继续等同一 task**
- task 完成 → 取结果（`StopAsyncIteration` 结束）→ `strategy` 按已消费的真实帧推进阶段 → `yield frame` → 建下一个 task
- **同轮优先级冻结**：`wait` 超时返回后若 task 已 done，**先消费真实帧，不插 ping**
- close → `lease.close()`（不自己级联）

### 面 1 — TTFB「延迟提交」

首 chunk 竞速扩为三方，返回 typed tagged union:

```
FirstChunkRace =
  | Disconnected
  | FirstChunk(frame: SSEFrame)                    # interval 内到达（快路径）
  | SlowCommit(lease)                              # interval 先到，须提交流；lease 持 pending task + inner
```

- **同轮优先级冻结**：`disconnect > 已完成首 chunk > timer`（与 `:375-389` 一致，disconnect watcher 已消费 ASGI 消息须优先）
- **`FirstChunk` 快路径** → 完全保持现行为：错误首帧转 JSON、正常首帧走管线、断连转 499。下游 payload 字节不变
- **`SlowCommit` 慢路径**:
  - 交出响应前先 `cancel` 并完整 `await` 旧 `_wait_for_http_disconnect` task（`:381-386` 同款），避免与 Starlette `listen_for_disconnect` 抢 ASGI `receive`
  - 返回 `_UpstreamClosingStreamingResponse`，body：先 `yield` 面 1 保活帧，pending 首帧 task（lease 持有）以 interval 竞速边等边发保活，首真帧到达后经统一 real-frame 管线 + `_sse_keepalive` 续流
  - 关闭走 `lease.close()`（见上）；即使 Starlette 从未启动 body，响应对象自身可执行该清理
  - 慢路径**放弃**「首帧转 JSON 错误」（已提交 200）；后续错误按「错误契约」以 surface 可识别 SSE error 下发

### 错误契约 — producer 层统一（failure-once）

现状缺口：`HTTPException` 被 `async_streaming_data_generator:2566` / `proxy_server.py:7077` **重新抛出**（非序列化）→ 提交后 Starlette abort；Anthropic serializer（`:2603-2611`）产**无 `event: error`** 的 `data:{...}` → SDK 忽略（既有 latent bug，延迟提交放大触发面）。**注**：chat post-commit 普通异常实际只 yield 一帧 error、**不带 `[DONE]`**（`proxy_server.py:7100-7108`），初稿此处描述有误，已改。

冻结方案——**在 producer 层统一**，不靠外层 mapper 猜:

- producer 捕获异常 → 调 `post_call_failure_hook`（一次）→ 按 surface **直接 yield 客户端可识别 error 帧** → 结束。**提交后不再 re-raise `HTTPException`**（是否已提交由 `create_response` 层经 lease/flag 告知 producer；未提交的快路径保持 re-raise 以便转 JSON）
  - anthropic：`event: error\ndata: {"type":"error","error":{...}}\n\n`
  - chat：`data: {"error":...}\n\n`（OpenAI SDK 可识别；不追加 `[DONE]`，与现状一致）
  - responses：其 iterator 的 failure 形态（若用 typed `{"type":"error"}` 须补 `message`/`sequence_number` 等必填字段，`openai/types/responses/response_error_event.py:11-27`）
- typed outcome 标 `failure_recorded: bool`：producer 已 yield error 帧且已调 hook 的分支，外层**绝不**再调 hook、再补帧；仅 normalizer / tracing 等 **producer 外层**异常才由外层兜底并标 `failure_recorded=False`
- 验收覆盖三类来源（`HTTPException` / 普通异常 / producer 已序列化 error）× 每 surface 客户端可识别 × failure hook 每类恰一次 × 不双发

### 保活帧：格式感知策略（两阶段生成器）

- **通用保底（三面）**：SSE 注释 `: ping\n\n`。协议级注释、两 SDK 安全忽略、frame 边界注入安全
- **Anthropic 面 2 叠加**：额外 `event: ping\ndata: {"type": "ping"}\n\n`。协议层**防御纵深**（传输层保活由注释单独完成，非第二套 timeout 修复机制）
- **面 1（message_start 前）只发注释**：Anthropic 自身不在 message_start 前发 ping，严格客户端行为留 PoC
- **OpenAI chat / responses**：仅注释

**两阶段生成器（no-mutation，语义精确）**：`AnthropicKeepaliveStrategy` 空闲注入拆成阶段 1（未见 message_start，仅注释）→ 阶段 2（已见，注释 + ping），由**控制流**切换、不维护可变 `seen_message_start` flag。精确语义（冻结，防 off-by-one）:

- 识别 `message_start` 真帧 → **先正常 yield 该真帧**，`message_start` 本身**不伴随**合成 ping
- 阶段 2 从「等待**下一个**真帧」开始生效；只有该等待满一个 interval（idle）才发注释 + ping
- 阶段切换**不**取消 / 重建当前 pending task
- 验收断言：message_start 后**立即**到达下一真帧时两帧间**无**合成 ping；仅下一 idle interval 到期才有

#### record-not-adopted：不用空 `content_block_delta`

空 `content_block_delta` 的 `text_delta` 需当前开放 text 块 index；发在 `content_block_stop` 后 / tool_use / thinking 块内会破坏客户端组装（SDK accumulator 按 `event.index` 访问背书）。原生 `ping` 任意位置合法、更稳。

### 上限与兜底

无独立上限，**只跟随上游存活**（用户选择）。兜底:

- **当前**：既有上游 httpx 默认 read 超时 `COMPLETION_HTTP_FALLBACK_SECONDS=600`（`http_handler.py:126-137`，评审核实）——half-open 挂死到点抛异常 → 经错误契约以 error 帧下发、保活停。故**非无限**，上界约 600s
- **关联 upstream spec 实现后**：更细的 read gap + total deadline
- **发布门槛**：`enabled=true` 且检测不到有效上游 read/total 超时 → 启动 warning。BACKLOG 记依赖 + 可观测性指标（active keepalive streams / ping count / stream age / timeout termination）

## 配置（Override / Resolved 双类型 + 真实防泄漏）

```yaml
litellm_settings:
  stream_keepalive: { enabled: true, interval: 15 }
model_list:
  - model_name: github_copilot/claude-opus-4.8
    litellm_params:
      stream_keepalive: { interval: 15 }   # 部分覆盖
```

- `StreamKeepaliveOverride`（Pydantic `frozen`，`extra="forbid"`）：`enabled: bool | None = None`、`interval: float | None = None`（有限、`>0`、最小值如 1s 拒忙循环）。**全 Optional 无默认**
- **merge 语义冻结**：按 `model_fields_set`（deployment 显式设置的字段覆盖 global）；`interval: null` 显式清空语义 = 回退 global 未设则回退默认。选定此语义、写测试固定
- `resolve(merged) -> ResolvedStreamKeepaliveConfig`（frozen，完整）：默认 `enabled=True`/`interval=15` **只在 resolve 末端加一次**，故「deployment 只写 `enabled:false`」不重置 global interval
- **防泄漏（冻结完整通路）**：
  1. `GenericLiteLLMParams.stream_keepalive: StreamKeepaliveOverride | None` + `LiteLLMParamsTypedDict` 同步注册
  2. `"stream_keepalive"` **加入 `all_litellm_params`**（`types/utils.py:3054-3074`）——仅 Pydantic 注册**挡不住** `extra="allow"` 的展开
  3. Router **三条**展开路径（sync chat `router.py:1633-1677` / async chat `:2657-2694` / generic responses+messages `:4341-4372`）从 provider-visible local copy `pop`
  4. 选定 deployment 经 `hidden_params.model_id` + `common_request_processing.py:1431-1448` 取回，解析 override
  5. wire-body 回归：三面真实 Router 路径断言上游 body 无 `stream_keepalive`

## 组件设计

1. **配置** `stream_keepalive_config.py`：`StreamKeepaliveOverride` / `ResolvedStreamKeepaliveConfig` / `parse` / `merge`(fields_set) / `resolve`
2. **策略**：`KeepaliveStrategy` 基（frozen）；`AnthropicKeepaliveStrategy`（两阶段）；`CommentOnlyKeepaliveStrategy`。组合优于继承，注入
3. **SSE delimiter 共享 helper**（抽取 `proxy_server.py:6902-6915` 查找逻辑）+ **bytes-safe frame normalizer**
4. **`StreamLease`**：唯一幂等资源 owner
5. **统一 real-frame generator + `_sse_keepalive`**
6. **`create_response` 改造**：三方竞速 + `FirstChunkRace` + 慢路径提交 + lease + producer-层错误契约接线；新增 `downstream_sse_surface` / strategy 参数（None → 原路径）
7. **三调用点接线** + 配置注册/防泄漏

### 未采用现成库

`sse-starlette` 的 `EventSourceResponse(ping=N)` 只覆盖面 2、发不了原生 ping、接不进 litellm 首 chunk 缓冲/错误/断连机制，不采用，仅借鉴注释心跳。

## PoC（进 plan 前）

- **Claude Code 超时类型（门禁）**：真实 E2E——上游首字节延迟超原失败阈值，确认无 keepalive 断开、启用后成功；固定 total deadline 对照确认不在承诺内。**结果写回 spec 再进 plan**
- **面 1 原生 ping 兼容**（非门禁，默认只发注释）：验证「message_start 前发 `event: ping`」是否被 Claude Code 接受；安全则面 1 可升级注释 + ping

## 测试（能被 mutate 时失败，>90% kill）

注入 timer/clock 或事件屏障控制 producer，避免只靠墙钟 `≈interval` 的易抖测试。

- **组合子**：慢 gen（2×interval 无帧）→ 恰 N 帧、producer task 未 cancel；快 gen → 零保活帧
- **半帧注入（重点）**：Anthropic raw SSE 在 `event:`/`data:`/UTF-8 多字节/JSON 中间分片；停顿期 ping 不落半帧；下游只见完整帧且**字节等价**（跨 chunk UTF-8 不产 replacement char）；三种 delimiter 都识别；EOF 残片按策略下发
- **lease 幂等（重点）**：并发 `close()` 两次 → 退款/deferred logging/上游关闭各恰一次；未启动 body 取消 → 无孤儿 task
- **面 1 延迟提交**：首帧 2×interval 后到 → 已提交、头已发、保活帧先于首真帧；快路径错误流 → 仍走 JSON 错误
- **同轮优先级**：interval/首帧/disconnect 同 turn → `disconnect > chunk > timer`
- **错误契约（重点）**：producer 以 `HTTPException` / 普通异常 / 已序列化 error 结束 → 每 surface 客户端可识别、不双发、failure hook 每类恰一次；chat 不带 `[DONE]`
- **Anthropic 两阶段**：message_start 后立即到下一真帧 → 两帧间无 ping；下一 idle 到期 → 注释 + `event: ping`；之前 idle → 仅注释
- **OpenAI/responses**：空闲仅注释，绝不合成 content chunk
- **断连各资源恰一次**：首 ping 已发、首真帧未到时断连 → max-parallel / budget reservation / 499 metadata / deferred logging 各恰一次
- **tracing/cost 隔离**：DD 两路径只跟真实 chunk 不跟 ping；`include_cost_in_streaming_usage=True` ping 不进成本注入、终态 usage 不变
- **配置**：global + deployment 部分覆盖（`enabled:false` 不重置 global interval）、`extra=forbid` 未知键、`NaN`/`inf`/极小 interval 拒绝、`interval:null` 语义；三面 wire-body 无泄漏（含 `all_litellm_params` 生效）
- **background polling** 传 None → 该内部路径无保活注入、不受影响
- **`enabled=false`**：零保活帧、下游字节级等同现状（对拍）

## 落地顺序

0. **PoC**（Claude Code 超时类型）→ 结果写回 spec
1. 组件 1（Override/Resolved parse/merge(fields_set)/resolve）+ 组件 2（策略两阶段）+ 纯单测
2. 组件 3（delimiter helper + bytes-safe normalizer）+ normalizer 单测（半帧/多字节/三 delimiter/上限/EOF/字节等价）
3. 组件 4（`StreamLease`）+ lease 幂等/无孤儿测试
4. 组件 5（统一 real-frame generator + `_sse_keepalive`）+ producer-not-cancelled/同轮优先级/close 传播
5. 组件 6（`create_response` 三方竞速 + `FirstChunkRace` + 慢路径 + producer-层错误契约）+ 面 1/未启动 body/错误三来源/快路径回归
6. 组件 7 三调用点接线 + `all_litellm_params` 注册 + 三条 Router pop + 三面端到端 + wire-body + `enabled=false` 对拍 + 启动 warning
7. BACKLOG：upstream-timeout 依赖门槛 + 可观测性指标；面 1 native ping PoC（非门禁）
