# 上游 HTTP client 细粒度配置（github_copilot）

状态：设计已确认（三轮对抗性评审，终审判「修正两项 spec 语义后可进实现」，本版已冻结）
日期：2026-07-13 初稿；2026-07-14 三轮评审后定稿
分支：`ghc`

## 修订说明（本版依据）

初稿经两轮 GPT reviewer 对抗性评审（装真实依赖 `openai==2.33.0`/`httpx==0.28.1`/`aiohttp==3.14.1` 跑实验证伪）。第二轮的决定性发现：**aiohttp 原生 `ClientTimeout.total` 只包住单次 socket 请求，盖不住 OpenAI SDK 在两次 attempt 之间的重试 backoff（`_sleep_for_retry` 的 `anyio.sleep`）**，因此单靠它兑现不了「逻辑请求硬封顶」，无论如何都要外套一层 asyncio 绝对 deadline。据此用户决定从方案乙转向 **丙-maximal**：total 统一用 asyncio 绝对 deadline（不碰 aiohttp transport、不用 contextvar），一套机制覆盖两 transport × 三面，顺带消解 aiohttp 的 ceil 取整坑。

累计吸收的关键结论:

1. total 用**绝对 deadline** 而非时长，且必须外套 asyncio 层覆盖 SDK 重试 backoff（round1+2）
2. deadline 过期后**立即抛超时**，绝不给 aiohttp 传 `total=0`（实测 `total<=0` 会禁用 timer 而非立即失败）
3. httpx/流式 deadline 下沉到**底层字节流迭代接缝**，让超时以普通异常进各面既有失败路径，复用日志/成本回收并显式关闭 `httpx.Response`；三面返回类型不同（`CustomStreamWrapper` / `ResponsesAPIStreamingIterator` / pass-through iterator），各自接缝或统一到 `aiter_bytes` 层
4. `http_client` 嵌套 dict 现会被当未知参数塞进 `extra_body` 泄漏上游 → 注册为正式 litellm 级参数、在 provider 映射前消费，且**内部 resolved 值也不得再进 provider 映射**
5. deadline scope 起点冻结在「Router 选定 deployment、merge 参数之后、调用各面 async 函数之前」，direct SDK 入口给同一 helper——proxy common 层建 task 前拿不到 per-deployment 配置，不能作 scope owner
6. 三面共用 `resolve()` 时，未配置的轴回退到**各面自己**的现有有效超时，不得统一套 chat 的 600s（否则改掉 responses 的 6000s 语义）
7. contextvar 采用理由（「插不进 extensions」）被证伪且丙不再用 contextvar，删除

## 背景与问题

litellm 代理对 GHC 上游超时，yaml 只能配一个浮点 `timeout`，被当成 `httpx.Timeout(timeout=X)`（connect/read/write/pool 同值）；想表达「建连快、读慢」或「整个请求硬封顶」时表达不了。

已核实链路:

GHC chat completion 经 `openai_compatible_providers` 进 `_complete_custom_openai`（`main.py:5456-5478`），默认用 `OpenAIChatCompletion`；两条 handler 默认都经 `AsyncHTTPHandler._create_async_transport()` → **`LiteLLMAiohttpTransport`**（`disable_aiohttp_transport=True` 换 httpx）。三面（chat/responses/messages）的 async HTTP 最终都用 `get_async_httpx_client()` → `_create_async_transport()`，故 connect/read/pool 的 `httpx.Timeout` 映射对三面一致有效。

三个缺口:

- **缺口 A（细粒度 timeout）**：yaml 只能给 float；`supports_httpx_timeout()` 只认 `openai/azure/bedrock`，`github_copilot` 不在内，`httpx.Timeout` 到 chat 会被 `completion_timeout.py:51-68` 降级成只取 `read` 浮点
- **缺口 B（整体硬超时）**：httpx 原生无整体截止；aiohttp 有 `ClientTimeout.total` 但只覆盖单次 attempt，盖不住 SDK 重试 backoff。故整体封顶须在 litellm 层用 asyncio 绝对 deadline 实现
- **缺口 C（HTTP/2）**：全库无 `http2`，独立 PoC

三面管线分裂:

- `/chat/completions`：走 `CompletionTimeout.resolve`，加白名单让 `httpx.Timeout` 不降级
- `/responses`：native handler 已接受并向 `post()` 传 `httpx.Timeout`（`responses/main.py:1099-1117`），但当 provider config 缺失或 `use_chat_completions_api=True` 时会转 completion bridge（`responses/main.py:1054-1066`），该 bridge 也须带上 resolved 配置
- `/v1/messages`：现在**根本不传 per-request timeout**（`.../messages/handler.py:565-579` → `llm_http_handler.py:1869-1896` 的 `post()` 无 `timeout=`），且 client 按 `LlmProviders.ANTHROPIC` 缓存（既有 bug）

### 能力矩阵（四个轴 × 两种 transport）

| 超时轴 | 含义 | aiohttp | httpx | 本设计落法 |
|---|---|---|---|---|
| connect | TCP 建连 | `sock_connect` | `connect` | `httpx.Timeout`，两 transport 通吃 |
| read | 两段字节间隔(gap) | `sock_read` | `read` | 同上 |
| pool | 从连接池取连接 | `connect` | `pool` | 同上 |
| total | 整个逻辑请求(含重试、含流式)硬封顶 | 原生仅单 attempt | 无原生 | **litellm 层 asyncio 绝对 deadline**（丙） |

## 设计原则（关键决策）

- **配置传输中立**：yaml 表达意图，connect/read/pool 经 `httpx.Timeout` 两 transport 通吃；total 用 litellm 层 asyncio deadline，与 transport 无关。不做 per-transport 配置（YAGNI）
- **total = 单一 asyncio 绝对 deadline（丙-maximal，用户拍板）**：request-scope 起点算 `deadline = loop.time() + total_timeout`
  - 非流式：`asyncio.timeout_at(deadline)`（或等价 deadline-aware wrapper，见下 Python 兼容）包住整个 SDK 调用——覆盖 SDK 重试 backoff 与建连全过程
  - 流式（**同一 deadline 两阶段**）：① `asyncio.timeout_at(deadline)` 包住「建立 stream / 取得外层 iterator」的初始 await——覆盖建连、等响应头、以及返回 iterator **之前**的 SDK 重试 backoff；② 初始 await 成功后，把**同一个** deadline 交给底层字节流迭代包裹（各面 iterator 之下），超时以普通 `TimeoutError` 进各面既有 `__anext__` 失败路径，复用 partial usage/成本回收/失败回调/异常映射；包裹层 shielded `aclose()` 显式关闭 `httpx.Response`；**不替换各面返回类型**（chat 靠 `isinstance(resp, CustomStreamWrapper)` 设 logging loop，见 `main.py:685-688`）。仅测慢 chunk 不足以证明整体封顶，须补「stream=True、首 attempt 失败、deadline 在 backoff 中到期」场景
  - `remaining = deadline - loop.time()` **`<= 0` 立即抛超时**，不依赖底层 timer
  - **不碰 aiohttp transport、不引 contextvar**——一套机制覆盖两 transport × 三面
  - **Python 兼容**：`asyncio.timeout_at()` 为 3.11+，项目声明支持 3.10（`pyproject.toml`）→ 实现须提供版本兼容的统一 deadline helper（3.10 上用 `asyncio.wait_for(awaitable, remaining)`），并统一转换为同一公开 timeout 异常
- **scope 起点冻结**：global/request 级配置在 API async 入口解析；per-deployment 配置在 **Router 选定 deployment、merge 参数之后**解析；deadline scope 在**同一 Router attempt 内、调用三面实际 async 函数之前**建立；direct SDK 调用绕过 Router 时由各面 async 入口用**同一 helper** 建立。跨 Router fallback 的预算语义：**每个 deployment attempt 独立起算 deadline**（fallback 不继承首个 deadline；如需全局封顶另行显式建模，记 BACKLOG）
- **三面全覆盖**：chat/responses（含 completion bridge 分支）/messages 共享 http_client 解析、httpx.Timeout 应用与 deadline scope；顺带修 messages 不传 timeout + 错缓存键
- **http_client 是正式 litellm 级参数，绝不泄漏上游**：进 `all_litellm_params`，provider 映射前消费；内部 resolved 值走不会再参与 provider 映射的 typed 通道；wire-level 测试断言上游 body 无 `http_client`
- **未配置轴回退各面原语义**：`resolve()` 接收各面已解析的 legacy effective timeout，未配置轴回退到该面原行为（chat 未显式配置为 600s；responses 为 `timeout or request_timeout`，package default 6000s），不统一套 600
- **自定义 client 显式告警**：注入 `litellm.aclient_session` / 传入 `AsyncOpenAI` / 自定义 `AsyncHTTPHandler` 绕过 `_create_async_transport` 时，connect/read/pool 仍可能不生效 → 明确 warning，不静默
- **向后兼容**：旧 `timeout: float` 行为不变

## 目标

让 `litellm_params`（及全局 `litellm_settings`）为 GHC 表达 connect/read/pool 分段超时与整体硬超时（asyncio 绝对 deadline，覆盖重试与流式），per-deployment 覆盖全局，覆盖 chat/responses/messages 三面，两 transport 都兑现，配置不泄漏上游。

## 非目标（本次不做，记录以备后续）

- **HTTP/2（缺口 C）**：单独 PoC（GHC 是否协商 h2、换 httpx+http2 后吞吐/稳定性），代码与结论留 `exp/http2-ghc/`。本次仅 schema 预留并校验 `http2` 键，不接线
- 连接池大小等 aiohttp 连接器参数（已有 `AIOHTTP_CONNECTOR_LIMIT*` 等 env）
- 全局跨 Router fallback 的统一预算封顶（本次每 attempt 独立起算，记 BACKLOG）
- 改变**未配置 `http_client`** 的任何 provider 的既有超时行为——本功能为 **opt-in（用户已确认通用 opt-in，不做 GHC-only gate）**：配置了 `http_client` 的任意 provider 生效（含全局 `litellm_settings.http_client` 对所有 provider 生效），未配置则行为完全不变。因此「不改动 openai/azure」的语义是「不改动其未配置时的既有行为」，而非把功能限制到 GHC。为兑现通用 opt-in，`http_client` 解析出的 `httpx.Timeout` 须对其被配置的 provider 生效，chat 的 `supports_httpx_timeout` 降级不得把它剥掉（实践上把 `github_copilot` 加进白名单覆盖用户实际用量，并确保 http_client 来源的 `httpx.Timeout` 不被降级）
- 重构 `supports_httpx_timeout` 硬编码白名单（记 BACKLOG）

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
        total_timeout: 1800     # 整个逻辑请求(含重试、含流式)硬上限，可选，默认不设
        # http2: true           # 缺口 C，本次仅校验不接线
```

各键可选。缺省回退（每面独立）:

- `connect_timeout` 未设 → `fallback_connect = HTTP_HANDLER_CONNECT_TIMEOUT_SECONDS`（5s）
- `read_timeout` / `pool_timeout` / `write` 未设 → **该面 legacy effective timeout**（chat 未显式配置为 600s；responses 为 `timeout or request_timeout`；messages 取其应有默认）。`write` 轴 yaml 不暴露但 `httpx.Timeout` 需给值，取同一 legacy io fallback
- `total_timeout` 未设 → `None`（不封顶，维持现状仅 gap 保护）

**语义提醒**：`total_timeout` 对流式意味「整段生成须在 N 秒内跑完」，长 completion 可能误杀，默认关闭。**默认值澄清**：包级 sentinel 6000s，但 chat 未显式配置时实际用 600s；responses 等其它面按各自现状。

旧 `timeout: float` 与 `http_client` 并存时 `http_client` 优先，加载时 warning。

## 组件设计

### 1. 配置模型与解析（新建 `litellm/litellm_core_utils/http_client_config.py`）

- `HttpClientConfig`（Pydantic `frozen`）：`connect_timeout`/`read_timeout`/`pool_timeout`/`total_timeout: Optional[float]`（>0 校验），`http2: Optional[bool]`
- `parse(raw) -> HttpClientConfig`：边界校验（负值/非法类型/未知键报错，不放 `Any` 下游）
- `merge(global_cfg, deployment_cfg) -> HttpClientConfig`：deployment **已设置且非 None** 的键覆盖全局；**显式 `null` 视同未设**（回退全局，不引入「清除全局」语义），键缺失同样回退全局
- `resolve(cfg, legacy_effective_timeout) -> ResolvedHttpClient`：产出 `httpx.Timeout(connect, read, write, pool)`（未配置轴用该面 `legacy_effective_timeout`，connect 用 5s）+ `total_timeout: Optional[float]`

### 2. http_client 注册为 litellm 级参数（杜绝上游泄漏）

- 加入 `all_litellm_params`（`types/utils.py`），使其不进 `get_non_default_completion_params()` / `extra_body`（现泄漏入口 `utils.py:9147-9154`、`4352-4383`）
- 三面各自在 **deployment merge 之后、provider optional-param 映射之前**解析并从 provider-visible kwargs pop 掉；resolved 结果走 typed 内部通道（不再参与 provider 映射，避免换名再泄漏）
- 全局 `litellm_settings.http_client` 在 proxy 加载边界立即校验（不能只靠 `setattr`，`proxy_server.py:4315-4321`）；per-deployment `litellm_params.http_client` 在 model-list/Deployment 构造边界校验（使「加载时 warning」可稳定实现）
- 回归：mock transport 抓上游 JSON，断言不含 `http_client`（三面各一条）

### 3. 三面的 httpx.Timeout 应用

- **chat**：`supports_httpx_timeout()` 加 `"github_copilot"`，resolved timeout 送入现链路
- **responses**：resolved `httpx.Timeout` 接入 native handler `post()`；**并覆盖 completion bridge 分支**（`responses/main.py:1054-1066`），使非 native-responses 模型也不丢配置
- **messages**：修 bug——向 `.../messages/handler.py` 与 `llm_http_handler.py` 的 `post()` 传 resolved `httpx.Timeout`；缓存键按**实际 `custom_llm_provider`** 构造（`LlmProviders(custom_llm_provider)`：GHC 路径为 `GITHUB_COPILOT`，真正的 Anthropic provider 仍为 `ANTHROPIC`，不无条件替换以免改动其它 provider 行为）。messages 的 `legacy_effective_timeout` 取现有 `_default_cached_client_timeout()` 语义（未显式配置 600s，显式 global timeout 则用该值）

### 4. total → 单一 asyncio 绝对 deadline（丙-maximal）

- **scope 建立**：在 Router 选定 deployment + merge 后（direct 入口用同一 helper）算 `deadline = loop.time() + total_timeout`，随 request context 流入下面两处
- **非流式**：`asyncio.timeout_at(deadline)` 包住 SDK 调用，覆盖重试 backoff
- **流式（同一 deadline 两阶段）**：① `timeout_at(deadline)` 包住「取得外层 iterator」的初始 await（覆盖建连、响应头、返回前的 backoff）；② 同一 deadline 交给底层字节流迭代包裹（各面 iterator 之下，或统一到 `response.aiter_bytes()` 层）:
  - chat：OpenAI `AsyncStream` 与 `CustomStreamWrapper` 之间，超时以 `TimeoutError` 进 `__anext__`（`streaming_handler.py:1877-1952/2068-2129`）
  - responses（native）：`response.aiter_bytes()` 与 SSE decoder 之间，仍触发 `ResponsesAPIStreamingIterator._handle_failure()`
  - responses（completion bridge）：内部持 `CustomStreamWrapper`，**复用 chat 的底层 wrapper**，不套 native byte-stream 接缝
  - messages：`response.aiter_bytes()` 与 `PassThroughStreamingHandler.chunk_processor()` 之间，保其 `finally` 记录 partial chunks。**failure hook 归属（已冻结，方案 b）**：direct SDK 消费 `litellm.anthropic_messages()` 返回 iterator 时**只记 partial spend、不触发 `failure_handler`**（与现有 `chunk_processor` 行为一致，不引入重复触发）；完整 failure hook 仍由 proxy 外层负责（`common_request_processing.py:2551-2577`）。验收断言「direct SDK 路径 failure_handler 触发 0 次、partial spend logging 触发 1 次」
  - 三面均：`remaining<=0` 立即抛；shielded `aclose()` 显式关闭 `httpx.Response`；不替换返回类型
- **不修改 aiohttp transport，不引 contextvar**

### 5. 自定义 client 告警

注入 `aclient_session` / 传 `AsyncOpenAI` / 自定义 `AsyncHTTPHandler` 且配了 http_client 时 warning，说明哪些不兑现。

依赖注入优先（HTTP client / clock / deadline 以参数或 request context 传入），便于单测传假实现，不 monkeypatch。数据结构不可变（frozen dataclass / tuple / frozenset），满足 LIT001/LIT002 与强类型；吃 yaml/JSON 用 Pydantic/`TypeAdapter` 边界校验。

## 测试（对齐 CLAUDE.md：能被 mutate 时失败，>90% kill）

- **配置纯单测**：`parse` 边界；`resolve` 各面 fallback（connect=5、未配置轴取该面 legacy timeout）；`merge` 覆盖
- **回归**：`supports_httpx_timeout("github_copilot") is True`
- **wire-body 不泄漏（重点）**：mock transport 抓上游 JSON，三面各断言不含 `http_client`
- **deadline 扛重试（重点）**：注入 clock + 至少一次 SDK retry（含 backoff sleep），断言总耗时不超一个 total budget、超时确实在 backoff 期间触发（mutate 掉外层 `timeout_at` 应失败）
- **deadline 过期立即失败**：`remaining<=0` 路径断言立即抛，不被底层 timer 吞
- **httpx 流式 deadline（重点，三面各一）**：伪慢流断言到点抛 `Timeout`、各面失败日志/部分成本回收**仍执行**、`httpx.Response` 被关闭；快流不误杀
- **流式建立阶段封顶（重点）**：`stream=True`、首 attempt 失败、deadline 在 SDK retry backoff 中到期，断言初始 await 被 `timeout_at` 覆盖并抛超时（仅测慢 chunk 证不出这点）
- **未配置轴保各面语义**：responses 未配 read 时仍用其 6000s 语义而非 600
- **三面端到端**：各配 http_client，断言分段 timeout 与 deadline 生效；messages 回归其原不传 timeout 的 bug
- **自定义 client 告警**：注入 `aclient_session` + http_client 断言 warning

## 落地顺序

1. 组件 1（模型/parse/merge/resolve）+ 组件 2（参数注册、防泄漏、两级校验）+ 单测与 wire-body 回归
2. 组件 3 三面 httpx.Timeout 应用（含 responses bridge 分支、messages bug 修复）+ 端到端
3. 组件 4 非流式 `asyncio.timeout_at` deadline + scope 起点冻结在 Router attempt + 扛重试/过期立即失败测试
4. 组件 4 三面流式 deadline 下沉 + shielded aclose + 聚焦回归
5. 组件 5 自定义 client 告警；BACKLOG 记 `supports_httpx_timeout` smell、跨 fallback 统一预算；HTTP/2 PoC 另起
