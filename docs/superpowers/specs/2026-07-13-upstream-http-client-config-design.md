# 上游 HTTP client 细粒度配置（github_copilot）

状态：设计修订中（已过一轮对抗性评审，待再评审 + 用户确认）
日期：2026-07-13 初稿；2026-07-14 依评审修订
分支：`ghc`

## 修订说明（本版依据）

初稿经 GPT reviewer 对抗性评审（装真实依赖 `openai==2.33.0`/`httpx==0.28.1`/`aiohttp==3.14.1` 跑实验证伪），核心方向可行但需修订。本版吸收其 5 个 major 结论:

1. `total` 若设为时长，会被 OpenAI SDK 重试放大为 `(重试数+1)×N`，不是逻辑请求硬上限 → 改为**绝对 deadline**
2. httpx 流式 `wait_for` 包 `CustomStreamWrapper.__anext__` 会以 `CancelledError`(BaseException) 绕过既有失败日志/成本回收，且不自动关连接 → deadline 下沉到底层流
3. `http_client` 嵌套 dict 现会被当未知参数塞进 `extra_body` 泄漏到上游 → 注册为正式 litellm 级参数并在 provider 映射前消费
4. contextvar 的采用理由（「插不进 extensions」）被证伪（httpx request hook 可改 extensions）→ 修正理由，机制保留
5. 加白名单只覆盖 `/chat/completions`；`/responses`、`/v1/messages` 各自管线独立，messages 面甚至现在不传 timeout → 用户已决定**三面全覆盖**

## 背景与问题

litellm 代理对 GHC 上游超时，yaml 只能配一个浮点 `timeout`（或全局 `request_timeout`），被当成 `httpx.Timeout(timeout=X)`（connect/read/write/pool 同值）。运维想表达「建连快、读慢」或「整个请求硬封顶」时表达不了。

已核实链路（含评审更正）:

GHC chat completion 经 `openai_compatible_providers` 分支进 `_complete_custom_openai`（`main.py:5456-5478`），默认用 `OpenAIChatCompletion`（设 `EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER` 才整体切 `BaseLLMHTTPHandler`）；两者默认都经 `AsyncHTTPHandler._create_async_transport()` → **`LiteLLMAiohttpTransport`**（`disable_aiohttp_transport=True` 换 httpx）。litellm 内部本就支持 `httpx.Timeout`（`CompletionTimeout.resolve` 透传），aiohttp transport 已把它拆成 `sock_connect`/`sock_read`/`connect(pool)`。

三个缺口:

- **缺口 A（细粒度 timeout）**：yaml 只能给 float；`supports_httpx_timeout()` 只认 `openai/azure/bedrock`，`github_copilot` 不在内，`httpx.Timeout` 到 GHC 会被 `completion_timeout.py:51-68` 降级成只取 `read` 浮点
- **缺口 B（整体硬超时）**：httpx 原生无 total；aiohttp 有 `ClientTimeout.total` 但 litellm 未设。且 httpx 塞进 `request.extensions["timeout"]` 的只有 connect/read/write/pool（`httpx/_config.py:132-138`），OpenAI 公开 `RequestOptions` 无 extensions 字段（`openai/_types.py:117-125`），故不能走 SDK 普通参数传 total
- **缺口 C（HTTP/2）**：全库无 `http2`，aiohttp 不支持 h2，httpx 那条也没开。独立 PoC

三面管线分裂（评审发现）:

- `/chat/completions`：走 `CompletionTimeout.resolve`，加白名单即让 `httpx.Timeout` 不降级
- `/responses`：不经 `CompletionTimeout.resolve`，handler 接受并向 `AsyncHTTPHandler.post()` 传 `httpx.Timeout`（`responses/main.py:1099-1117`、`llm_http_handler.py:2477-2508`），但本设计的 http_client 解析未接线到此
- `/v1/messages`：**现在根本不传 per-request timeout**（`anthropic/experimental_pass_through/messages/handler.py:565-579` → `llm_http_handler.py:1869-1896` 的 `post()` 无 `timeout=`），且 client 按 `LlmProviders.ANTHROPIC` 缓存（`llm_http_handler.py:1950-1952`）——既有 bug

### 能力矩阵（四个轴 × 两种 transport）

| 超时轴 | 含义 | aiohttp | httpx |
|---|---|---|---|
| connect | TCP 建连 | `sock_connect` | `connect` |
| read | 两段字节间隔(gap) | `sock_read` | `read` |
| pool | 从连接池取连接 | `connect` | `pool` |
| total | 整个请求(含流式全程)硬封顶 | `ClientTimeout.total`（原生，per-attempt） | 无原生，需 deadline 包裹 |

## 设计原则（关键决策）

- **配置传输中立，实现 transport-aware**：yaml 表达意图（connect/read/pool/total），落地按激活的 transport 分派。**不做 per-transport 配置**（YAGNI）
- **total 用绝对 deadline，不用时长**：request-scope 起点算 `deadline = loop.time() + total_timeout`；aiohttp transport 每次 attempt 传 `ClientTimeout(total = deadline - loop.time())`。这样才能覆盖 OpenAI SDK 重试、重定向与建连全过程，兑现「逻辑请求硬封顶」。httpx 路径同理用 deadline 兜底
- **total 双 transport 都兑现（方案乙，用户拍板）**：aiohttp 原生 `ClientTimeout.total`（socket 层清理最干净），httpx 走 deadline 包裹。已知丙方案（都用包裹）代码更少且等价，用户明确要 aiohttp 原生精度，故采乙
- **aiohttp 的 deadline 用 contextvar 侧通道**。理由（评审修正）：httpx request hook 虽可改 `request.extensions`，但 OpenAI SDK 公开 request options 不承载 litellm 自定义 transport metadata，我们不改 SDK API surface；contextvar 在同一 asyncio task 链传播且并发隔离（评审实测 caller/hook/transport `id(current_task())` 相同、并发两 task 各读各值不串扰），共享 session 安全（`ClientTimeout` 是每次 `session.request()` 单独传，不改共享 session 状态）
- **contextvar 必须在 await 该请求的 async task 内、进 executor 之前设置并 try/finally reset**（评审证伪：`acompletion` 先把同步 `completion` 丢 executor，若在 executor 内围绕「创建 coroutine」set 后立即 reset，coroutine 在调用方 task 执行时看不到该值）
- **三面全覆盖**：chat/responses/messages 共享同一套 http_client 解析、httpx.Timeout 应用与 deadline scope；顺带修 messages 面不传 timeout + 错缓存键
- **http_client 是正式 litellm 级参数，绝不泄漏上游**：进 `all_litellm_params`，在 provider 映射前消费掉，wire-level 测试断言上游 body 不含它
- **自定义 client 显式告警不静默**：注入了 `litellm.aclient_session`、直接传入 `AsyncOpenAI`、或自定义 `AsyncHTTPHandler` 时会绕过 `_create_async_transport`，total 不生效 → 明确 warning，不静默失效
- **向后兼容**：旧 `timeout: float` 行为不变

## 目标

让 `litellm_params`（及全局 `litellm_settings`）能为 GHC 表达 connect/read/pool 分段超时与整体硬超时（绝对 deadline），per-deployment 覆盖全局，覆盖 chat/responses/messages 三面，两种 transport 都正确兑现，且配置不泄漏上游。

## 非目标（本次不做，记录以备后续）

- **HTTP/2（缺口 C）**：单独建模，需先 PoC 验证 GHC 是否协商 h2、换 httpx+http2 后吞吐/稳定性。PoC 代码与结论留 `exp/http2-ghc/`，有结论后再定 `http_client.http2` 落法。本次仅在 schema 预留并校验 `http2` 键，不接线
- 连接池大小等 aiohttp 连接器参数（已有 `AIOHTTP_CONNECTOR_LIMIT*` 等 env）
- 改动 openai/azure 等其它 provider 的既有超时行为
- 重构 `supports_httpx_timeout` 硬编码白名单（记 `BACKLOG.md`）

## yaml 表面（单独命名空间，用户已选）

```yaml
litellm_settings:
  http_client:            # 全局兜底
    connect_timeout: 5
    read_timeout: 300

model_list:
  - model_name: github_copilot/claude-opus-4.8
    litellm_params:
      model: github_copilot/claude-opus-4.8
      http_client:        # 覆盖全局
        connect_timeout: 5      # -> httpx connect -> aiohttp sock_connect
        read_timeout: 300       # -> httpx read    -> aiohttp sock_read（gap）
        pool_timeout: 5         # -> httpx pool    -> aiohttp connect(取连接)
        total_timeout: 1800     # 缺口 B：整个逻辑请求(含流式、含重试)硬上限，可选，默认不设
        # http2: true           # 缺口 C，本次仅校验不接线
```

各键可选。缺省回退（评审修正，明确区分）:

- `connect_timeout` 未设 → `fallback_connect = HTTP_HANDLER_CONNECT_TIMEOUT_SECONDS`（5s）
- `read_timeout` / `pool_timeout` 未设 → `fallback_io`（沿用 chat 现有：未显式配置 global timeout 时为 600s；显式配了 `request_timeout` 则用该值）
- `write` 轴 yaml 不暴露，但 `httpx.Timeout` 必须给 write 值 → 取 `fallback_io`
- `total_timeout` 未设 → `None`（不封顶，维持现状仅 `sock_read` gap 保护）

**语义提醒**：`total_timeout` 对流式意味「整段生成必须在 N 秒内跑完」，长 completion 可能误杀，故默认关闭。**默认值澄清**：包级 sentinel 是 6000s，但 chat completion 未显式配置时实际用 600s；只有显式 `request_timeout: 6000` 才保留 6000。

旧 `timeout: float` 与 `http_client` 并存时 `http_client` 优先，加载时 warning。

## 组件设计

### 1. 配置模型与解析（新建 `litellm/litellm_core_utils/http_client_config.py`）

- `HttpClientConfig`（Pydantic，`frozen`）：`connect_timeout`/`read_timeout`/`pool_timeout`/`total_timeout: Optional[float]`（>0 校验），`http2: Optional[bool]`
- `parse(raw: Mapping) -> HttpClientConfig`：边界校验（负值/非法类型/未知键报错，不放 `Any` 下游）
- `merge(global_cfg, deployment_cfg) -> HttpClientConfig`：deployment 非 None 键覆盖全局
- `resolve(cfg) -> ResolvedHttpClient`：产出 `httpx.Timeout(connect, read, write, pool)`（各段带上述明确 fallback）+ `total_timeout: Optional[float]`（供上层算 deadline）

### 2. http_client 注册为 litellm 级参数（杜绝上游泄漏）

- 加入 `all_litellm_params`（`types/utils.py`），使其不进 `get_non_default_completion_params()` / `extra_body`
- 在三面各自进入 provider 映射**之前**解析并从 provider-visible kwargs 中 pop 掉，只把 `ResolvedHttpClient` 放进内部 request context
- 全局 `litellm_settings.http_client` 在 proxy 配置加载边界立即校验（不能只靠 `setattr(litellm, key, value)`）
- 回归：断言最终上游 JSON 不含 `http_client`

### 3. 三面的 httpx.Timeout 应用

- **chat**：`supports_httpx_timeout()` 加 `"github_copilot"`，使 `httpx.Timeout` 不被 `completion_timeout.py` 降级；把 resolved timeout 送入现链路
- **responses**：把 resolved `httpx.Timeout` 接入 `responses/main.py` → handler `post()` 的 timeout 参数
- **messages**：修既有 bug——向 `anthropic .../messages/handler.py` 与 `llm_http_handler.py` 的 `post()` 传入 resolved `httpx.Timeout`；并修正 client 缓存键（当前误用 `ANTHROPIC`，应按 GHC deployment 键）

### 4. total → 绝对 deadline 的双 transport 兑现（方案乙）

- **request-scope 起点**：在覆盖三面的共同高层（候选：proxy `common_request_processing`，或各面 async 入口）设置 contextvar `_http_deadline_ctx = loop.time() + total_timeout`，`try/finally reset`；**须在 await 请求的 async task 内、进 executor 前设置**
- **aiohttp 路径（默认）**：`LiteLLMAiohttpTransport._make_aiohttp_request` 读 `_http_deadline_ctx`，非 None 则 `ClientTimeout(total = max(0, deadline - loop.time()))` 每次 attempt 重算——扛住 SDK 重试
- **httpx 路径（`disable_aiohttp_transport` 或未来 http2）**：
  - 非流式：`asyncio.wait_for(call, remaining)`
  - 流式：deadline 包裹放在**底层 `completion_stream` 与 `CustomStreamWrapper` 之间**，超时以普通 `TimeoutError` 进 `CustomStreamWrapper.__anext__`，复用既有失败日志/部分用量/异常映射；包裹层实现 shielded `aclose()`，timeout 分支显式关闭被包装流；**不替换返回类型**（`acompletion` 靠 `isinstance(resp, CustomStreamWrapper)` 设 logging loop，见 `main.py:685-688`）

### 5. 自定义 client 告警

注入 `litellm.aclient_session` / 传入 `AsyncOpenAI` / 自定义 `AsyncHTTPHandler` 时，deadline 与部分 timeout 语义不保证 → 检测到 http_client 配置 + 自定义 client 时 warning，说明哪些不兑现。

依赖注入优先（HTTP client / clock / total 值以参数或 request context 传入），便于单测传假实现，不 monkeypatch。数据结构不可变（frozen dataclass / tuple / frozenset），满足 LIT001/LIT002 与强类型；吃 yaml/JSON 用 Pydantic/`TypeAdapter` 边界校验。

## 测试（对齐 CLAUDE.md：能被 mutate 时失败，>90% kill）

- **桥接纯单测**：`parse` 边界（缺字段/负值/非法类型/未知键）；`resolve` 各段 fallback（connect=5、io、write=io）正确；`merge` per-deployment 覆盖全局
- **回归**：`supports_httpx_timeout("github_copilot") is True`
- **wire-body 不泄漏（重点）**：mock transport 抓最终上游 JSON，断言不含 `http_client`（三面各一条）
- **deadline 扛重试（重点）**：注入 clock + 至少一次 SDK retry，断言 aiohttp transport 每次 attempt 拿到递减 remaining、总耗时不超一个 total budget（mutate 掉「重算 remaining」应失败）
- **并发隔离**：两并发请求不同 total，各自 deadline 不串扰
- **httpx 流式 deadline（重点）**：强制 `disable_aiohttp_transport=True`，伪慢流断言到点抛 `Timeout`、失败日志/部分成本回收**仍执行**、底层连接被 `aclose()`；快流不误杀；Router mid-stream fallback 行为
- **三面端到端**：chat/responses/messages 各配 http_client，断言传给底层 client 的分段 timeout 与 deadline 符合预期；messages 面回归其原本不传 timeout 的 bug
- **自定义 client 告警**：注入 `aclient_session` + http_client，断言 warning

## 落地顺序

1. 组件 1（配置模型/parse/merge/resolve）+ 组件 2（参数注册、防泄漏）+ 其单测与 wire-body 回归
2. 组件 3 三面 httpx.Timeout 应用（含 messages bug 修复）+ 端到端
3. 组件 4 aiohttp 绝对 deadline（contextvar + transport 重算 remaining）+ 扛重试/并发测试
4. 组件 4 httpx 流式 deadline 下沉 + shielded aclose + 聚焦回归
5. 组件 5 自定义 client 告警；`BACKLOG.md` 记 `supports_httpx_timeout` smell；HTTP/2 PoC 另起
