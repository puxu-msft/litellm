# 上游 HTTP client 细粒度配置（github_copilot）

状态：设计已确认
日期：2026-07-13
分支：`ghc`

## 背景与问题

litellm 代理对 github_copilot（GHC）上游的超时，目前 yaml 只能配一个浮点数 `timeout`（或全局 `litellm_settings.request_timeout`，默认 6000s），它会被当成 `httpx.Timeout(timeout=X)`，即 connect/read/write/pool 四段同值。运维想表达「建连快超时、流式读慢超时」或「给整个请求一个硬上限」时，现有 yaml 表达不了。

已核实的实际链路与三个缺口:

GHC 的 completion 走 `custom_llm_provider == "github_copilot"`，落到 openai-compatible handler，默认经 **aiohttp transport**（`LiteLLMAiohttpTransport`）打上游；`disable_aiohttp_transport=True` 才换回 httpx transport。litellm 内部**本就支持 `httpx.Timeout` 对象**（`CompletionTimeout.resolve` 会透传），aiohttp transport 已把它拆成 `sock_connect`/`sock_read`/`connect(pool)`（`aiohttp_transport.py` `_make_aiohttp_request`）。真正缺的:

- **缺口 A（细粒度 timeout）**：yaml 只能给 float；且 `supports_httpx_timeout()` 只认 `openai/azure/bedrock`，`github_copilot` 不在内（`utils.py`），所以即便构造出 `httpx.Timeout`，到 GHC 也会被 `completion_timeout.py` 降级成只取 `read` 的浮点数
- **缺口 B（流式整体硬超时）**：httpx 原生**没有** total/整体截止的概念（只有每段操作超时）；aiohttp 有 `ClientTimeout.total`，但 litellm 的 transport 当前只设 `sock_connect`/`sock_read`/`connect`，没设 total。而 httpx 塞进 `request.extensions["timeout"]` 的只有 `connect/read/write/pool` 四键，没有 total 通道，且该 extensions 由 OpenAI SDK 内部构造、litellm 插不进手。所以整体硬上限现在无处表达
- **缺口 C（HTTP/2）**：全代码库无 `http2`，aiohttp 不支持 h2，httpx 那条也没开 `http2=True`。独立处理

### 能力矩阵（四个轴 × 两种 transport）

| 超时轴 | 含义 | aiohttp | httpx |
|---|---|---|---|
| connect | TCP 建连 | `sock_connect` | `connect` |
| read | 两段字节之间的最大间隔(gap) | `sock_read` | `read` |
| pool | 从连接池取连接 | `connect` | `pool` |
| total | 整个请求(含流式全程)硬封顶 | `ClientTimeout.total`（原生） | 无原生，需 `asyncio.wait_for` 模拟 |

connect/read/pool 两种 transport 都能落；唯 total 存在能力不对称。

## 设计原则（关键决策）

- **配置传输中立，实现 transport-aware**：用户选 aiohttp 还是 httpx 是性能/兼容取舍，不应被迫为两种 transport 写两套超时。yaml 表达的是意图（connect/read/pool/total），落地时按当前激活的 transport 分派。**不做 per-transport 配置**（YAGNI）
- **total 双 transport 都兑现（方案乙，用户拍板）**：aiohttp 走**原生** `ClientTimeout.total`（socket 层清理最干净），httpx 走 `asyncio.wait_for`（覆盖非默认路径与未来 http2/httpx）。已知丙方案（两条都用 wait_for）代码更少且行为等价，但用户明确要 aiohttp 原生精度，故采乙
- **aiohttp 的 total 用 contextvar 侧通道**，不用自定义 header——避免超时值泄漏到 GHC 上游
- **向后兼容**：旧写法 `timeout: 600`（float）行为完全不变
- **不 hardcode、不静默吞**：httpx transport 侧 total 通过 wait_for 真实兑现；任何不兑现的路径必须显式 warning，不静默忽略

## 目标

让 `litellm_params`（及全局 `litellm_settings`）能表达 GHC 上游的 connect/read/pool 分段超时与整体硬超时，per-deployment 覆盖全局，两种 transport 都正确兑现。

## 非目标（本次不做，记录以备后续）

- **HTTP/2（缺口 C）**：因能力不同单独建模，且需先 PoC 验证 GHC endpoint 是否协商 h2、绕过 aiohttp 换 httpx+http2 后吞吐/稳定性是否可接受。PoC 代码与结论文档留在 `exp/http2-ghc/`，有结论后再定 `http_client.http2` 的落法（预期：设 http2 即为该 deployment 注入 `httpx.AsyncClient(http2=True)` 走 httpx）。本次仅在 yaml schema 预留 `http2` 键位并校验，不接线
- 连接池大小等 aiohttp 连接器参数（`AIOHTTP_CONNECTOR_LIMIT*` 等已有 env 覆盖，不进本 yaml 块）
- 改动 openai/azure 等其它 provider 的既有超时行为
- 重构 `supports_httpx_timeout` 的硬编码白名单（见「已知 code smell」）

## yaml 表面（单独命名空间，用户已选）

`litellm_params` 下新增 `http_client` 块；全局 `litellm_settings.http_client` 作兜底，per-deployment 覆盖全局:

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
        total_timeout: 1800     # 缺口 B：整个请求(含流式)硬上限，可选，默认不设
        # http2: true           # 缺口 C，本次仅校验不接线
```

- 三个 timeout 键均可选；未设的段回退到现有默认（连接默认 `HTTP_HANDLER_CONNECT_TIMEOUT_SECONDS=5s`，read/整体回退到 `request_timeout`/6000s 语义）
- `total_timeout` 默认不设。**语义提醒**：对流式请求它意味着「整段生成必须在 N 秒内跑完」，对长 completion 可能误杀，故显式可选、默认关闭
- 旧 `timeout: 600` 与 `http_client` 并存时，`http_client` 优先，并在加载时 warning 提示重复

## 组件设计

### 1. 配置模型与桥接（新建 `litellm/litellm_core_utils/http_client_config.py`）

一个 Pydantic 模型 `HttpClientConfig`（`connect_timeout`/`read_timeout`/`pool_timeout`/`total_timeout` 均 `Optional[float]`，`http2: Optional[bool]`），加两个纯函数:

- `parse(raw: Mapping) -> HttpClientConfig`：用 Pydantic 在边界校验 yaml 传入的 dict（非法值/负数/未知键报错，不放 `Any` 进下游）
- `to_httpx_timeout(cfg, fallback_read) -> httpx.Timeout`：把 connect/read/pool 组装成 `httpx.Timeout`（缺省段用 fallback）。total 不进 httpx.Timeout（httpx 无此概念），单独返回/透传

per-deployment 覆盖全局的合并也在此（deployment 段非 None 的键覆盖全局同名键）。

### 2. github_copilot 纳入 httpx.Timeout 支持

`supports_httpx_timeout()` 白名单加 `"github_copilot"`，使 `httpx.Timeout` 不被 `completion_timeout.py` 降级成单浮点。

**已知 code smell（记录，本次不改）**：`supports_httpx_timeout` 是硬编码三元素白名单，长期应改为「按 provider 是否走 honor-httpx-timeout 的 handler 判定」，而非手工点名。本次只加一项，符合「三行修改不触发大重构」。记入 `BACKLOG.md`。

### 3. total 的双 transport 兑现（方案乙）

- **aiohttp 路径（默认）**：新增一个 contextvar（如 `_aiohttp_total_timeout_ctx`）。litellm 在发起 GHC 调用前 `set(total)`；`LiteLLMAiohttpTransport._make_aiohttp_request` 构造 `ClientTimeout` 时读取该 contextvar，非 None 就加 `total=`。contextvar 在同一 asyncio task 链内传播，请求结束 `reset`。**不经 header，不泄漏上游**
- **httpx 路径（`disable_aiohttp_transport=True` 或未来 http2）**：在 litellm 调用层用 `asyncio.wait_for` 兜整体超时。非流式包住 await；流式在调用开始算 `deadline = now + total`，包住 stream 迭代（每次 `__anext__` 用 `wait_for(remaining)`），到点抛 `litellm.Timeout`，并确保底层连接经 async context 关闭

注入点：total 值从 resolved 配置取，随调用上下文流入上述两处；具体接线（在 `main.py`/handler 的哪一层 set contextvar 与包 wait_for）在实施计划细化。

依赖注入优先，clock 与 total 值以参数/上下文传入，便于单测传假实现，不 monkeypatch。

## 测试（对齐 CLAUDE.md：能被 mutate 时失败，>90% kill）

- **桥接纯单测**：`parse` 边界（缺字段、负值、非法类型、未知键）；`to_httpx_timeout` 各段缺省回退正确；per-deployment 覆盖全局的合并
- **回归**：`supports_httpx_timeout("github_copilot") is True`
- **aiohttp total（侧通道）**：注入伪 session/clock，断言设了 `total_timeout` 时 transport 产出 `ClientTimeout(total=N)`、未设时 total 为 None（mutate 掉 contextvar 读取应失败）
- **httpx total（wait_for，重点）**：强制 `disable_aiohttp_transport=True`，用伪慢流断言 total 到点抛 `litellm.Timeout` 且底层连接被关闭；快流不误杀。这是最不显然的一处，单独聚焦回归
- **端到端**：一条 GHC deployment 配 `http_client`，断言实际传给底层 client 的 timeout 分段与整体值符合预期

## 落地顺序

1. 组件 1（配置模型 + 桥接）+ 组件 2（白名单）+ 其单测
2. 组件 3 aiohttp contextvar total + 单测
3. 组件 3 httpx wait_for total + 聚焦回归
4. 端到端串联测试；`BACKLOG.md` 记 `supports_httpx_timeout` smell；HTTP/2 PoC 另起
