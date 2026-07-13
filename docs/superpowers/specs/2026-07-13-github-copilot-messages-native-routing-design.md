# GitHub Copilot `/v1/messages` 按模型原生端点路由

状态：设计已确认，待写实现计划
日期：2026-07-13
分支：`ghc`

## 背景与问题

litellm 代理接收 Anthropic `/v1/messages` 请求后，对 `github_copilot/<model>` 目前的处理是：

- claude 模型 → `GithubCopilotAnthropicMessagesConfig`，命中 Copilot 原生 `/v1/messages`。已实现
- 非 claude（gpt 等）模型 → `get_provider_anthropic_messages_config` 返回 `None`，落到 handler 兜底分支；由于 `_RESPONSES_API_PROVIDERS = {"openai"}` 不含 `github_copilot`，`_should_route_to_responses_api` 返回 False，于是走 chat/completions 桥

这带来两个问题。其一，gpt 模型本应尽量走 Copilot 原生能力，chat/completions 是多一层 Anthropic↔OpenAI 往返转换的降级路径。其二，也是决定性的：上游最新的 `gpt-5.5`、`gpt-5.6-*`、`gpt-5.4-mini` 等模型**只支持 `/responses`，没有 `/chat/completions`**，把它们打到 chat/completions 会直接失败。

判据的数据来源是根因。现状要么靠 `"claude" in model` 字符串判断，要么靠静态 `model_cost` 注册表里的 `supported_endpoints`。而静态注册表严重漂移：上游 `/models` 有的 `gpt-5.4 / 5.4-mini / 5.5 / 5.6-luna/sol/terra`、`gemini-3.1/3.5`、`claude-opus-4.6/4.7/4.8`、`sonnet-5` 在注册表里根本不存在；连 `gpt-5-mini` 的 endpoints 都标错（表里为 `None`，上游实际支持 responses）。更糟的是 `get_models` 拉到上游 `/models` 后**只保留 `model["id"]`，把 `supported_endpoints` 和全部能力信息丢弃**，导致路由拿不到权威数据。

## 目标

让 `/v1/messages` 进来的 `github_copilot/<model>` 请求，按该模型**真实 `supported_endpoints`** 三向分流到 Copilot 原生端点。

## 非目标（本次不做，记录以备后续）

- 「屏蔽 Copilot 不支持的功能」这条大目标（如 responses 路径下不支持的工具/参数的过滤与降级）。分流打通后遇到的具体特性不兼容问题作为后续项跟进
- 修改 openai / azure 等其它 provider 的既有路由行为

## 路由规则

判据是该模型规范化后的 `supported_endpoints`。对一条进入的 Anthropic `/v1/messages` 请求，按优先级：

1. 含 `messages` 端点 → `GithubCopilotAnthropicMessagesConfig`，打 Copilot 原生 `/v1/messages`
2. 否则含 `responses` 端点 → 标准 Responses API 桥（`LiteLLMMessagesToResponsesAPIHandler` → `GithubCopilotResponsesAPIConfig`），打 Copilot `/responses`
3. 否则 → chat/completions 桥（现状兜底）

天然覆盖：claude → messages；`gpt-5.5 / 5.6-*`（responses-only）→ responses（对它们是必须，否则失败）；`gpt-4o / gemini`（chat-only）→ chat。

注意端点命名差异：上游 `/models` 用 `/chat/completions`、`/v1/messages`、`/responses`、`ws:/responses`；静态注册表用 `/v1/chat/completions`、`/v1/messages`、`/v1/responses`。判据**不得硬编码某个字符串**，需归一成 `messages / responses / chat` 三个语义后再判断。Copilot 的 responses 端点是 `/responses`（标准 Responses API 用法），现有 `GithubCopilotResponsesAPIConfig.get_complete_url` 已返回 `{base}/responses`，无需改。

## 组件设计

### 1. 能力图（新模块，如 `litellm/llms/github_copilot/model_capabilities.py`）

带缓存（TTL）的 `{model -> 规范化端点集}`，分两层，主路优先：

- 主路：复用 `Authenticator` + `get_copilot_default_headers` 拉 Copilot `/models`，解析每个模型的 `supported_endpoints`。这要求**不再丢弃 `supported_endpoints`**——修 `get_models` 只留 id 的反模式，或在此模块单独发请求并保留完整能力。倾向后者，避免动 `get_models` 的既有返回契约（`List[str]`）
- Fallback：主路失败/离线/未认证时，回退到补齐后的静态注册表的 `supported_endpoints`
- 端点名规范化器：把上游 `/responses`、`ws:/responses`、`/v1/messages`，与注册表 `/v1/responses`、`/v1/chat/completions` 等，归一成 `messages / responses / chat` 三态

对外暴露一个 resolver，作为下方所有决策点的**单一真相源**。fetcher 通过依赖注入传入，便于单测传假实现。

### 2. 接线三个决策点（共用同一 resolver）

- `get_provider_anthropic_messages_config`（`litellm/utils.py:8055`）：由「`"claude" in model`」升级为「模型支持 `messages` 端点」。主路 fetch 失败时回退到 `"claude" in model` 字符串判断，保证 claude 侧永不退化
- `_should_route_to_responses_api`（`litellm/llms/anthropic/experimental_pass_through/messages/handler.py:53`）：改为模型感知。`github_copilot` 时查能力图是否支持 `responses`；openai 行为不变；全局开关 `use_chat_completions_url_for_anthropic_messages` 顺带对 copilot 生效（强制回退 chat）
- responses config 选择处（`litellm/utils.py:8215` 的 `github_copilot_supports_responses_api`）：改走同一 resolver，消除 `/v1/responses` vs `/responses` 命名分歧，保证 `/v1/responses`（标准 Responses API）直接请求 copilot 模型时也选对 config

### 3. 补齐静态注册表

把上游 `/models` 有、注册表缺/错的模型补进 `model_prices_and_context_window.json`（及 backup），`supported_endpoints` 与上游对齐，作为可靠 fallback：

- 新增：`gpt-5.4`、`gpt-5.4-mini`、`gpt-5.5`、`gpt-5.6-luna/sol/terra`、`gemini-3.1-pro-preview`、`gemini-3.5-flash`、`gemini-3-flash-preview`、`claude-opus-4.6`、`claude-opus-4.7`、`claude-opus-4.8`、`claude-sonnet-5`、`mai-code-1-flash-picker`
- 修正：`gpt-5-mini` 的 endpoints（应含 responses）

待定实现细节：注册表条目除 `supported_endpoints` 外还需定价/上下文数据。上下文与 token 上限可从上游 `/models` 取到，但**每 token 定价上游不提供**。倾向按同族现有条目沿用定价；写实现计划时定稿，先不阻塞。

## 测试

对齐项目 CLAUDE.md 的测试要求（能杀掉变异、可作回归）：

- 能力 resolver（注入假 fetcher，**不 monkeypatch**）：端点名规范化（`/responses`、`ws:/responses`、`/v1/responses` 都归一到 `responses`；messages / chat 同理）；live → registry 分层回退；unknown 模型的处理
- 路由（注入能力图）：claude → 命中 messages config；responses-only 的 `gpt-5.6-*` → 命中 responses 桥；chat-only 的 `gpt-4o` → 命中 chat 桥；openai 行为不回归；全局开关强制回退 chat
- 注册表补齐的回归断言：断言最新模型存在且 `supported_endpoints` 与上游一致，防再次漂移

测试落点：路由回归进 `tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_anthropic_experimental_pass_through_messages_handler.py`；能力 resolver 与注册表断言进 `tests/test_litellm/llms/github_copilot/`（新建 `test_model_capabilities.py` 或就近扩展）。

## 验收

- claude 模型经 `/v1/messages` → Copilot 原生 `/v1/messages`（现状保持）
- responses-only 的最新 gpt 模型经 `/v1/messages` → Copilot `/responses`，不再落 chat/completions 而失败
- chat-only 模型经 `/v1/messages` → chat/completions（兜底保持）
- 上游新增模型无需改代码即自动分流正确（主路生效时）
- 主路不可用时，补齐后的注册表给出一致分流

真实验证（按项目约定，非 pytest 截图）：起本地 proxy，用 curl 打 `/v1/messages`，分别用一个 claude 模型、一个 responses-only 的 gpt 模型、一个 chat-only 模型，观察实际命中的上游端点与成功响应。
