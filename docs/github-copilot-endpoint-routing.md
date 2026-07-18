# GitHub Copilot `/v1/messages` 端点路由设计

> 状态：已实现，在 `ghc` 分支本地（按约定不进上游）。判据落点 `litellm/llms/github_copilot/model_capabilities.py`。
> 关联：决策见 [ADR](./ADR.md)；实现计划与规格见 [`superpowers/`](./superpowers/)。

## 问题

Claude Code / Anthropic 客户端经 litellm 打 `/v1/messages` 到 `github_copilot/<model>`。Copilot 后端对不同模型开放不同端点：

- claude 系 → `/v1/messages`(原生 Anthropic) + `/chat/completions`
- 新 gpt 系(`gpt-5.5`/`gpt-5.6-*`/`gpt-5.4-mini`) → **只有 `/responses`**(标准 Responses API),无 `/chat/completions`
- gpt-4o / gemini 等 → 只有 `/chat/completions`

历史实现只把 claude 路由到原生 messages,其余 gpt 落到 chat/completions 桥。**决定性问题**:responses-only 的新 gpt 打 chat/completions 会直接失败。根因是 litellm 拉 Copilot `/models` 时 `get_models` 只留 `model["id"]`、丢弃 `supported_endpoints`,导致路由没有权威判据,只能退回 `"claude" in model` 字符串或漂移的静态 `model_cost` 注册表。

## 决策（why）

**判据来源必须是动态取值:定时拉上游 `/models` 并缓存,不引入任何 hardcode 端点表。** 上游模型(尤其 gpt-5.x)快速迭代,任何静态表都会漂移(实测注册表连 `claude-haiku-4.5` 的端点都标错、root/backup 自相矛盾);上游 `/models` 是唯一权威真相源。详见 ADR。

## 路由规则（how）

对进入的 `/v1/messages`,按该模型规范化后的 `supported_endpoints` + operator 的 `mode` 硬 override 三向分流:

0. `mode == "anthropic"` → 强制 messages;`mode == "responses"` → 强制 responses(operator 显式短路)
1. 含 `messages` 端点 → `GithubCopilotAnthropicMessagesConfig` → Copilot 原生 `/v1/messages`
2. 否则含 `responses` 端点(且非 `mode == "chat"` opt-out) → 标准 Responses API 桥(`LiteLLMMessagesToResponsesAPIHandler` → `GithubCopilotResponsesAPIConfig`) → Copilot `/responses`
3. 否则 → chat/completions 桥(兜底)

天然覆盖:claude→messages;responses-only 的 gpt→responses(对它们是必须);chat-only→chat。

### `mode` 真值表（operator 在 `model_info.mode` 声明,优先级高于动态端点）

| mode | messages 判据 | responses 判据 |
|---|---|---|
| `anthropic`(新增) | True(强制) | False |
| `responses` | False | True(强制) |
| `chat` | 由端点决定(**不阻断**) | False(既有 opt-out) |
| 未设 | 由端点决定 | 由端点决定 |

`mode: chat` **不阻断 messages** 是为兼容现有「claude 部署写 `mode: chat` + `supported_endpoints` 含 `/v1/messages`」的配置。

### 端点名归一化

上游 `/models` 用 `/chat/completions`、`/v1/messages`、`/responses`、`ws:/responses`;config `model_info` 里 operator 可能写 `/v1/responses`、`/v1/chat/completions`。判据**不硬编码字符串**,归一成 `messages / responses / chat` 三态。Copilot 的 responses 端点是 `/responses`(标准用法),`GithubCopilotResponsesAPIConfig.get_complete_url` 已返回 `{base}/responses`。

## 数据流（单一真相源 resolver）

`model_capabilities.py` 暴露 resolver,分层:

1. **主路(动态、定时缓存)**:拉 Copilot `/models` 保留每模型 `supported_endpoints`,按 api_base 做 TTL 缓存(TTL 1800s > 刷新周期 120s,刷新前不过期)。由 proxy 启动时的**无条件 asyncio 后台循环**(不依赖 prisma/DB)定时刷新;路由只读缓存,不在热路径同步拉取。取 token 走**非交互**守卫(OAuth `access-token` 文件非空才取,杜绝 device flow),失败保留 last-good。刷新周期取 120s(原 300s)是为了 ≤ Copilot api-key 的刷新窗口(约 5 分钟),让后台循环在 key 过期前稳稳提前刷新;详见 [token 提前刷新](./github-copilot-token-refresh.md)。
2. **冷启动/失败兜底**:读**原始** `litellm.model_cost` 条目的 `supported_endpoints`(注意 `_cached_get_model_info_helper` 会丢该字段,故读 raw)。
3. **空集**:保守当 chat。

`mode` override 叠加在端点判据之上。所有外部数据(`/models` JSON、model_cost 条目)在边界用 Pydantic/`TypeAdapter` 校验成 typed 值。

## 代码地图

- `litellm/llms/github_copilot/model_capabilities.py`(新增):`fetch_endpoint_pairs` / `refresh_capabilities` / `get_cached_pairs` / `resolve_endpoints` / `forced_mode` / `route_supports_messages` / `route_supports_responses` / `raw_model_info` / `copilot_api_base` / `refresh_default_capabilities` / `periodic_capability_refresh_loop`
- `litellm/utils.py::get_provider_anthropic_messages_config`:copilot 分支放在 `@lru_cache` 的 `_get_provider_anthropic_messages_config_cached` **之外**(否则动态判据被永久 memoize),接受 per-request `model_info`
- `litellm/llms/anthropic/experimental_pass_through/messages/handler.py::_should_route_to_responses_api`:模型感知,传 `model` + `model_info`
- `litellm/llms/github_copilot/responses/transformation.py::github_copilot_supports_responses_api`:走同一 resolver,**保留** `mode` opt-out 契约与既有测试
- `litellm/proxy/proxy_server.py`:启动时按需挂后台刷新任务
- `litellm/types/utils.py`:`mode` Literal 加 `anthropic`

## 测试地图

- `tests/test_litellm/llms/github_copilot/test_model_capabilities.py`:归一化、缓存、resolver 分层、mode 真值表、非交互守卫、空集、deployment-aware 刷新
- `.../messages/test_github_copilot_messages_transformation.py`:messages config 选择(含杀「退回字符串判断」变异、per-deployment mode override)
- `.../messages/test_anthropic_experimental_pass_through_messages_handler.py`:`_should_route_to_responses_api` + handler 级**合并态**(真正调 `anthropic_messages_handler` 断言三终点各命中)
- `.../responses/test_github_copilot_responses_transformation.py`:responses gate + 保留 mode 回归

## 已知限制（多租户/共享 backend 别名;单账号单 base 部署不受影响）

1. **多个不同 api_base**:路由读 `copilot_api_base()`(Authenticator base)作缓存键。deployment 显式配了不同 api_base 时其动态缓存不被命中(后台按各 base 刷新,但读路径未接 per-request deployment api_base)。单账号单 base 时一致,无影响。
2. **共享 backend 别名的 responses 二次判据**:`mode: responses` 在 messages 层已按 per-request `model_info` 正确放行到 Responses 桥,但桥内 `litellm.responses()` 会再经 `get_provider_responses_api_config` 读**共享** `model_cost` 的 mode;两个别名映射同一 `github_copilot/<model>` 且 mode 冲突时下游可能按共享 mode 重判。每 deployment 唯一模型时不发生。
3. **非交互认证**:后台刷新仅在 OAuth `access-token` 文件非空时取 token;罕见「api-key.json 仍有效但 access-token 缺失」状态会跳过该轮刷新。

彻底修复方向:把选中 deployment 的 effective `api_base` + `model_info` 一路贯穿到 `get_provider_anthropic_messages_config` / `_should_route_to_responses_api` / `litellm.responses()` 的 config 选择。
