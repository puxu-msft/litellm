# GitHub Copilot `/v1/messages` 按模型原生端点路由

状态：设计已确认（动态取值版），待更新实现计划
日期：2026-07-13（2026-07-13 修订为动态取值版）
分支：`ghc`

## 背景与问题

litellm 代理接收 Anthropic `/v1/messages` 请求后，对 `github_copilot/<model>` 目前的处理是：

- claude 模型 → `GithubCopilotAnthropicMessagesConfig`，命中 Copilot 原生 `/v1/messages`。已实现
- 非 claude（gpt 等）模型 → `get_provider_anthropic_messages_config` 返回 `None`，落到 handler 兜底分支；由于 `_RESPONSES_API_PROVIDERS = {"openai"}` 不含 `github_copilot`，`_should_route_to_responses_api` 返回 False，于是走 chat/completions 桥

这带来两个问题。其一，gpt 模型本应尽量走 Copilot 原生能力，chat/completions 是多一层 Anthropic↔OpenAI 往返转换的降级路径。其二，也是决定性的：上游最新的 `gpt-5.5`、`gpt-5.6-*`、`gpt-5.4-mini` 等模型**只支持 `/responses`，没有 `/chat/completions`**，把它们打到 chat/completions 会直接失败。

判据的数据来源是根因。litellm 拉取 Copilot `/models` 时（`get_models` 继承自 OpenAIConfig）**只保留 `model["id"]`，把 `supported_endpoints` 和全部能力信息丢弃**，导致路由拿不到权威数据，只能退回 `"claude" in model` 字符串判断或静态 `model_cost` 注册表。而静态注册表既严重漂移（`gpt-5.4/5.5/5.6-*` 等新模型缺失），又对端点标注不可靠（实测根目录 `model_prices_and_context_window.json` 里 `claude-haiku-4.5` 只有 `chat`、漏了 `messages`，且与 backup 文件自相矛盾）。

## 设计原则（关键决策）

**判据来源必须是动态取值：定时拉取上游 `/models` 并缓存，不引入任何 hardcode 端点表。** 上游模型（尤其 gpt-5.x 家族）快速迭代，任何静态表都会漂移；上游 `/models` 是唯一权威真相源。这是本设计的核心约束。

## 目标

让 `/v1/messages` 进来的 `github_copilot/<model>` 请求，按该模型**真实 `supported_endpoints`** 三向分流到 Copilot 原生端点，判据来自定时刷新的上游 `/models` 缓存。

## 非目标（本次不做，记录以备后续）

- 「屏蔽 Copilot 不支持的功能」（如 responses 路径下不支持的工具/参数的过滤与降级）。分流打通后遇到的具体特性不兼容问题作为后续项跟进
- 修改 openai / azure 等其它 provider 的既有路由行为
- 补齐 `model_prices_and_context_window.json` 的定价/条目（端点判据改由动态 `/models` 提供，不再依赖该注册表；定价数据维护是独立工作）

## 路由规则

判据是该模型规范化后的 `supported_endpoints`，叠加 operator 的 `mode` 硬 override（见组件 1）。对一条进入的 Anthropic `/v1/messages` 请求，按优先级：

0. `mode == "anthropic"` → 强制 messages；`mode == "responses"` → 强制 responses（这两个是 operator 显式强制，短路后续）
1. 含 `messages` 端点 → `GithubCopilotAnthropicMessagesConfig`，打 Copilot 原生 `/v1/messages`
2. 否则含 `responses` 端点（且非 `mode == "chat"` opt-out）→ 标准 Responses API 桥（`LiteLLMMessagesToResponsesAPIHandler` → `GithubCopilotResponsesAPIConfig`），打 Copilot `/responses`
3. 否则 → chat/completions 桥（现状兜底）

天然覆盖：claude → messages；`gpt-5.5 / 5.6-*`（responses-only）→ responses（对它们是必须，否则失败）；`gpt-4o / gemini`（chat-only）→ chat。

端点命名差异：上游 `/models` 用 `/chat/completions`、`/v1/messages`、`/responses`、`ws:/responses`；deployment `model_info` 里 operator 可能写 `/v1/chat/completions`、`/v1/responses`。判据**不得硬编码某个字符串**，需归一成 `messages / responses / chat` 三态。Copilot 的 responses 端点是 `/responses`（标准 Responses API 用法），现有 `GithubCopilotResponsesAPIConfig.get_complete_url` 已返回 `{base}/responses`，无需改。

## 组件设计

### 1. 能力模块（新建 `litellm/llms/github_copilot/model_capabilities.py`）

对外暴露一个 resolver，作为下方所有决策点的**单一真相源**。数据来源分层，主路是动态取值：

- **主路（动态、定时缓存）**：拉 Copilot `/models`，保留每模型 `supported_endpoints`（补上 `get_models` 丢弃能力信息的反模式，用独立取值路径而非改 `get_models` 的 `List[str]` 契约）。结果按 api_base 缓存（TTL，单账号），复用 litellm 既有的 provider-models 缓存模式（参见 `utils.py` 的 `_model_cache` / `_get_valid_models_from_provider_api`，TTL 300s、按 provider+litellm_params 键）。缓存由 proxy 的 `AsyncIOScheduler` **定时后台刷新**，路由只读缓存、不在热路径同步拉取——避免阻塞与 device-flow 触发。取值用 deployment 的 `api_key/api_base`（非交互）+ 短 timeout，失败返回空并记 debug 日志
- **冷启动 / 拉取失败兜底（非 hardcode）**：缓存未热或上游暂不可达时，回退到该 deployment 的 `model_info.supported_endpoints` + `mode`（operator 在 config 声明，属配置数据非源码 hardcode）；再无则保守当 chat
- **`mode` 硬 override（operator 显式强制，优先级高于动态端点）**：
  - `mode == "anthropic"` → 强制 native `/v1/messages`（新增）
  - `mode == "responses"` → 强制 `/responses`（既有契约）
  - `mode == "chat"` → 在 **responses 判据**里表示「不走 responses」（既有 opt-out，不得回归）；但**不阻断 messages 判据**，以兼容现有「`mode: chat` + `supported_endpoints` 含 `/v1/messages`」的 claude 部署
  - 未设 mode → 完全按动态 `supported_endpoints` 三向（messages > responses > chat）
- **端点名规范化器**：上游 `/responses`、`ws:/responses`、`/v1/messages`，与 config 的 `/v1/responses`、`/v1/chat/completions` 等，归一成 `messages / responses / chat` 三态

fetcher 与缓存均通过依赖注入传入（HTTP client / clock），便于单测传假实现，不 monkeypatch 类属性。所有数据结构用不可变形态（`tuple` of pairs + `frozenset`，冻结 dataclass 缓存单元），满足项目 LIT001/LIT002 与强类型约束；吃 `/models` JSON 用 Pydantic/`TypeAdapter` 在边界校验。

### 2. 接线三个决策点（共用同一 resolver）

- `get_provider_anthropic_messages_config`（`litellm/utils.py:8055`）：由「`"claude" in model`」升级为「`mode == "anthropic"` 或模型支持 `messages` 端点」（`mode: chat` 不阻断）。**注意**该函数委托给 `@lru_cache` 的 `_get_provider_anthropic_messages_config_cached`；copilot 的动态判据必须放在缓存函数**之外**（在公开方法里先处理 GITHUB_COPILOT），否则首次判据会被永久 memoize、定时刷新失效
- `_should_route_to_responses_api`（`litellm/llms/anthropic/experimental_pass_through/messages/handler.py:53`）：改为模型感知，签名加 `model`。`github_copilot` 时查能力 resolver 是否支持 `responses`；openai 行为不变；全局开关 `use_chat_completions_url_for_anthropic_messages` 顺带对 copilot 生效（强制回退 chat）
- responses config 选择处（`litellm/utils.py:8215` 的 `github_copilot_supports_responses_api`）：改走同一 resolver。语义：`mode == "responses"` → True；`mode in {"chat", "anthropic"}` → False（chat 是既有 opt-out，anthropic 强制走 messages 故不走 responses）；否则动态端点是否含 `responses`。消除 `/v1/responses` vs `/responses` 命名分歧，且**保留** `mode` opt-out 契约与既有测试

## 测试

对齐项目 CLAUDE.md 的测试要求（能杀掉变异、可作回归）：

- 能力模块（DI 注入假 fetcher / 假 client / 假 clock，**不 monkeypatch 类属性**）：`/models` 响应解析 + Pydantic 校验 + 端点归一化（`/responses`、`ws:/responses`、`/v1/responses` 都归一到 `responses`；messages / chat 同理）；TTL 缓存命中与过期刷新；fetch 失败返回空并触发 model_info 兜底；`mode` 硬 override 优先于端点集；HTTP 非 200 处理
- 路由决策（注入能力 resolver 或其依赖）：claude → 命中 messages config；responses-only 的 `gpt-5.6-*` → 命中 responses 桥；chat-only 的 `gpt-4o` → 命中 chat 桥；openai 行为不回归；全局开关强制回退；**且需覆盖「旧逻辑会判错」的用例**（如非 claude 但支持 messages 的注入模型 → messages config；名字含 claude 但只 responses 的注入模型 → 不选 messages config），确保能杀死「退回字符串判断」变异
- 合并态（merged-state）：在 handler 层 stub 三个终点（native messages / responses dispatch / chat bridge），断言三类模型分别只命中对应终点，覆盖 `get_llm_provider` 去前缀 → config 选择 → `_should_route_to_responses_api` → responses 桥 → `GithubCopilotResponsesAPIConfig` 的完整链
- `mode` override 回归：既有 `test_github_copilot_responses_transformation.py` 中锁定 `mode=chat/responses`、`register_model` 用户 override 的用例必须继续通过

## 验收

- claude 模型经 `/v1/messages` → Copilot 原生 `/v1/messages`（现状保持）
- responses-only 的最新 gpt 模型经 `/v1/messages` → Copilot `/responses`，不再落 chat/completions 而失败
- chat-only 模型经 `/v1/messages` → chat/completions（兜底保持）
- 上游新增模型无需改代码即自动分流正确（定时刷新生效后）
- 缓存未热 / 上游不可达时，按 deployment `model_info` 兜底，claude 仍走 messages
- 路由热路径不因能力取值而阻塞或触发 device flow

真实验证（按项目约定，非 pytest 截图）：起本地 proxy，用 curl 打 `/v1/messages`，分别用一个 claude 模型、一个 responses-only 的 gpt 模型、一个 chat-only 模型，观察实际命中的上游端点与成功响应。
