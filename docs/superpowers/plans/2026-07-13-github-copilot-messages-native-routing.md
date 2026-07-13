# GitHub Copilot `/v1/messages` 原生端点路由 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让经 litellm `/v1/messages` 进来的 `github_copilot/<model>` 请求，按该模型真实 `supported_endpoints` 三向分流到 Copilot 原生端点（messages / responses / chat），主路读上游实时 `/models`，失败时回退到模块内权威端点表。

**Architecture:** 新增一个 `github_copilot/model_capabilities.py`，提供「模型 → 规范化端点集」的带缓存 resolver（依赖注入 fetcher + clock，主路拉 `/models`、fallback 读模块内权威端点表）。三个既有决策点（messages-native 选择、responses-vs-chat 兜底、responses config 选择）改为共用这个 resolver 的单一真相源。

**Tech Stack:** Python，litellm 既有 provider config 体系，`litellm.module_level_client`（同步 HTTP），`InMemoryCache` 风格的模块级 TTL 缓存，pytest。

## Global Constraints

- 不写任何注释（除非本任务步骤给出的代码里已含），遵循项目 CLAUDE.md
- Python 行宽上限 120（非 88）
- 强类型：禁止 `Any` / 裸 `dict` / `dict[str, Any]`；每个函数参数都要有精确类型。需要吃不可信外部数据（`/models` JSON）时，用 Pydantic 模型 / `TypeAdapter` 在边界处校验成 typed 值再传入
- 不 monkeypatch 做测试；用依赖注入把 fetcher / clock 传进去
- 函数式：不可变、不重赋值；用推导式 + `tuple()` / `frozenset()` 一次成形，避免先建空容器再 mutate（否则触发 LIT001/LIT002）
- 失败要么显式传播、要么显式处理并注明理由，不静默吞异常
- 提交遵循 conventional commits；提交信息不带任何 Claude / Co-authored-by 署名；分支名不含 `/`
- 每次提交前跑相关测试 + `make pre-commit`（有 staged 后端改动时），修掉所有报错
- 只 `git add` 本任务涉及的文件，绝不 `git add -A`（工作区尚有其它未提交改动：`adapters/transformation.py`、`utils.py`、`count_tokens.py`）
- 若修掉了 `ruff-strict-budget.json` / `type-discipline-budget.json` / `basedpyright-code-budget.json` 门控的违规，跑 `make lint-budget-update` 并提交下调后的上限

---

### Task 1: 端点规范化 + 模块骨架 + 权威 fallback 表

**Files:**
- Create: `litellm/llms/github_copilot/model_capabilities.py`
- Test: `tests/test_litellm/llms/github_copilot/test_model_capabilities.py`

**Interfaces:**
- Produces:
  - `CopilotEndpoint`：`Literal["messages", "responses", "chat"]`
  - `normalize_endpoints(raw: tuple[str, ...]) -> frozenset[CopilotEndpoint]` —— 把上游 / 注册表两套命名归一
  - `strip_provider_prefix(model: str) -> str` —— 去掉 `github_copilot/` 前缀
  - `FALLBACK_ENDPOINTS: Mapping[str, frozenset[CopilotEndpoint]]` —— 模块内权威端点表（来自上游 `/models`）

- [ ] **Step 1: Write the failing test**

```python
# tests/test_litellm/llms/github_copilot/test_model_capabilities.py
from litellm.llms.github_copilot.model_capabilities import (
    normalize_endpoints,
    strip_provider_prefix,
    FALLBACK_ENDPOINTS,
)


def test_normalize_upstream_names():
    assert normalize_endpoints(("/responses", "ws:/responses")) == frozenset({"responses"})
    assert normalize_endpoints(("/v1/messages", "/chat/completions")) == frozenset({"messages", "chat"})


def test_normalize_registry_names():
    assert normalize_endpoints(("/v1/chat/completions", "/v1/messages")) == frozenset({"messages", "chat"})
    assert normalize_endpoints(("/v1/responses",)) == frozenset({"responses"})


def test_normalize_unknown_dropped():
    assert normalize_endpoints(("/foo", "ws:/responses")) == frozenset({"responses"})
    assert normalize_endpoints(()) == frozenset()


def test_strip_provider_prefix():
    assert strip_provider_prefix("github_copilot/gpt-5.6-sol") == "gpt-5.6-sol"
    assert strip_provider_prefix("claude-opus-4.8") == "claude-opus-4.8"


def test_fallback_table_matches_upstream_critical_models():
    assert FALLBACK_ENDPOINTS["gpt-5.5"] == frozenset({"responses"})
    assert FALLBACK_ENDPOINTS["gpt-5.6-sol"] == frozenset({"responses"})
    assert FALLBACK_ENDPOINTS["gpt-5.4"] == frozenset({"responses", "chat"})
    assert FALLBACK_ENDPOINTS["gpt-4o"] == frozenset({"chat"})
    assert FALLBACK_ENDPOINTS["claude-opus-4.8"] == frozenset({"messages", "chat"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_litellm/llms/github_copilot/test_model_capabilities.py -v`
Expected: FAIL（`ModuleNotFoundError` / ImportError：模块不存在）

- [ ] **Step 3: Write minimal implementation**

```python
# litellm/llms/github_copilot/model_capabilities.py
from typing import Literal, Mapping

CopilotEndpoint = Literal["messages", "responses", "chat"]

_ENDPOINT_ALIASES: Mapping[str, CopilotEndpoint] = {
    "/v1/messages": "messages",
    "messages": "messages",
    "/responses": "responses",
    "/v1/responses": "responses",
    "ws:/responses": "responses",
    "responses": "responses",
    "/chat/completions": "chat",
    "/v1/chat/completions": "chat",
    "chat": "chat",
}


def normalize_endpoints(raw: tuple[str, ...]) -> frozenset[CopilotEndpoint]:
    return frozenset(
        _ENDPOINT_ALIASES[name] for name in raw if name in _ENDPOINT_ALIASES
    )


def strip_provider_prefix(model: str) -> str:
    return model.split("/", 1)[1] if "/" in model else model


FALLBACK_ENDPOINTS: Mapping[str, frozenset[CopilotEndpoint]] = {
    "claude-opus-4.6": frozenset({"messages", "chat"}),
    "claude-opus-4.7": frozenset({"messages", "chat"}),
    "claude-opus-4.8": frozenset({"messages", "chat"}),
    "claude-opus-4.5": frozenset({"messages", "chat"}),
    "claude-sonnet-4.5": frozenset({"messages", "chat"}),
    "claude-sonnet-4.6": frozenset({"messages", "chat"}),
    "claude-sonnet-5": frozenset({"messages", "chat"}),
    "claude-haiku-4.5": frozenset({"messages", "chat"}),
    "gpt-5.3-codex": frozenset({"responses"}),
    "gpt-5.4": frozenset({"responses", "chat"}),
    "gpt-5.4-mini": frozenset({"responses"}),
    "gpt-5.5": frozenset({"responses"}),
    "gpt-5.6-luna": frozenset({"responses"}),
    "gpt-5.6-sol": frozenset({"responses"}),
    "gpt-5.6-terra": frozenset({"responses"}),
    "gpt-5-mini": frozenset({"responses", "chat"}),
    "mai-code-1-flash-picker": frozenset({"responses"}),
    "gemini-3.1-pro-preview": frozenset({"chat"}),
    "gemini-3.5-flash": frozenset({"chat"}),
    "gpt-4o": frozenset({"chat"}),
    "gpt-4.1": frozenset({"chat"}),
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_litellm/llms/github_copilot/test_model_capabilities.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add litellm/llms/github_copilot/model_capabilities.py tests/test_litellm/llms/github_copilot/test_model_capabilities.py
git commit -m "feat(github_copilot): endpoint normalization + fallback capability table"
```

---

### Task 2: 实时 `/models` fetch + TTL 缓存 resolver

**Files:**
- Modify: `litellm/llms/github_copilot/model_capabilities.py`
- Test: `tests/test_litellm/llms/github_copilot/test_model_capabilities.py`

**Interfaces:**
- Consumes: `normalize_endpoints`, `CopilotEndpoint`（Task 1）；`Authenticator`、`get_copilot_default_headers`、`DEFAULT_GITHUB_COPILOT_API_BASE`（既有 common_utils / authenticator）
- Produces:
  - `EndpointMap = Mapping[str, frozenset[CopilotEndpoint]]`
  - `fetch_live_endpoints() -> EndpointMap` —— 同步拉 `{api_base}/models`，用 Pydantic 校验后归一
  - `get_live_endpoint_map(fetch: Callable[[], EndpointMap] = fetch_live_endpoints, now: Callable[[], float] = time.monotonic, ttl: float = 600.0) -> EndpointMap` —— 带模块级 TTL 缓存；fetch 抛错时返回空 map（记 debug 日志，不抛）

- [ ] **Step 1: Write the failing test**

```python
# 追加到 test_model_capabilities.py
import litellm.llms.github_copilot.model_capabilities as mc


def test_live_map_caches_within_ttl():
    calls = []

    def fake_fetch():
        calls.append(1)
        return {"gpt-5.5": frozenset({"responses"})}

    clock = {"t": 1000.0}
    mc._reset_live_cache()
    m1 = mc.get_live_endpoint_map(fetch=fake_fetch, now=lambda: clock["t"], ttl=600.0)
    m2 = mc.get_live_endpoint_map(fetch=fake_fetch, now=lambda: clock["t"], ttl=600.0)
    assert m1 == {"gpt-5.5": frozenset({"responses"})}
    assert m2 == m1
    assert len(calls) == 1


def test_live_map_refetches_after_ttl():
    calls = []

    def fake_fetch():
        calls.append(1)
        return {"gpt-5.5": frozenset({"responses"})}

    clock = {"t": 1000.0}
    mc._reset_live_cache()
    mc.get_live_endpoint_map(fetch=fake_fetch, now=lambda: clock["t"], ttl=600.0)
    clock["t"] = 1000.0 + 601.0
    mc.get_live_endpoint_map(fetch=fake_fetch, now=lambda: clock["t"], ttl=600.0)
    assert len(calls) == 2


def test_live_map_fetch_failure_returns_empty():
    def boom():
        raise RuntimeError("no auth")

    mc._reset_live_cache()
    assert mc.get_live_endpoint_map(fetch=boom, now=lambda: 1.0, ttl=600.0) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_litellm/llms/github_copilot/test_model_capabilities.py -k live -v`
Expected: FAIL（`AttributeError`：`get_live_endpoint_map` / `_reset_live_cache` 未定义）

- [ ] **Step 3: Write minimal implementation**

在 `model_capabilities.py` 追加（顶部补 imports）：

```python
import time
from typing import Callable, Optional

from pydantic import BaseModel, TypeAdapter

import litellm
from litellm._logging import verbose_logger
from litellm.llms.github_copilot.authenticator import Authenticator
from litellm.llms.github_copilot.common_utils import (
    DEFAULT_GITHUB_COPILOT_API_BASE,
    get_copilot_default_headers,
)

EndpointMap = Mapping[str, "frozenset[CopilotEndpoint]"]

_authenticator = Authenticator()


class _ModelEntry(BaseModel):
    id: str
    supported_endpoints: tuple[str, ...] = ()


class _ModelsResponse(BaseModel):
    data: tuple[_ModelEntry, ...]


_MODELS_ADAPTER = TypeAdapter(_ModelsResponse)


def fetch_live_endpoints() -> EndpointMap:
    api_key = _authenticator.get_api_key()
    api_base = (_authenticator.get_api_base() or DEFAULT_GITHUB_COPILOT_API_BASE).rstrip("/")
    headers = get_copilot_default_headers(api_key)
    resp = litellm.module_level_client.get(url=f"{api_base}/models", headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"github_copilot /models HTTP {resp.status_code}: {resp.text[:200]}")
    parsed = _MODELS_ADAPTER.validate_python(resp.json())
    return {
        entry.id: normalize_endpoints(entry.supported_endpoints)
        for entry in parsed.data
        if normalize_endpoints(entry.supported_endpoints)
    }


_live_cache: dict[str, object] = {}


def _reset_live_cache() -> None:
    _live_cache.clear()


def get_live_endpoint_map(
    fetch: Callable[[], EndpointMap] = fetch_live_endpoints,
    now: Callable[[], float] = time.monotonic,
    ttl: float = 600.0,
) -> EndpointMap:
    cached = _live_cache.get("map")
    expiry = _live_cache.get("expiry")
    if isinstance(cached, dict) and isinstance(expiry, float) and now() < expiry:
        return cached
    try:
        fresh = dict(fetch())
    except Exception as e:
        verbose_logger.debug("github_copilot get_live_endpoint_map fetch failed: %s", e)
        fresh = {}
    _live_cache["map"] = fresh
    _live_cache["expiry"] = now() + ttl
    return fresh
```

注：`_live_cache` 是模块级缓存，测试用 `_reset_live_cache()` 清空以隔离；这是缓存本身的一次性状态刷新，非业务可变数据结构，若被 LIT001/LIT002 命中则本处以 `# mutable-ok`（附「模块级 TTL 缓存刷新」理由）豁免。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_litellm/llms/github_copilot/test_model_capabilities.py -k live -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add litellm/llms/github_copilot/model_capabilities.py tests/test_litellm/llms/github_copilot/test_model_capabilities.py
git commit -m "feat(github_copilot): cached live /models endpoint map with injectable fetcher"
```

---

### Task 3: 分层的按模型能力 API

**Files:**
- Modify: `litellm/llms/github_copilot/model_capabilities.py`
- Test: `tests/test_litellm/llms/github_copilot/test_model_capabilities.py`

**Interfaces:**
- Consumes: `get_live_endpoint_map`、`FALLBACK_ENDPOINTS`、`strip_provider_prefix`、`normalize_endpoints`（Task 1/2）；`litellm.model_cost`（既有全局注册表）
- Produces:
  - `resolve_endpoints(model: str, *, live_map: Optional[EndpointMap] = None) -> frozenset[CopilotEndpoint]` —— 分层：live map → 注册表 `supported_endpoints` → 模块内 `FALLBACK_ENDPOINTS` → 空集
  - `supports_endpoint(model: str, endpoint: CopilotEndpoint, *, live_map: Optional[EndpointMap] = None) -> bool`

- [ ] **Step 1: Write the failing test**

```python
# 追加到 test_model_capabilities.py
def test_resolve_prefers_live_map():
    live = {"gpt-5.5": frozenset({"responses"})}
    assert mc.resolve_endpoints("github_copilot/gpt-5.5", live_map=live) == frozenset({"responses"})


def test_resolve_falls_back_to_module_table_when_live_empty():
    assert mc.resolve_endpoints("gpt-5.6-sol", live_map={}) == frozenset({"responses"})


def test_resolve_unknown_model_is_empty():
    assert mc.resolve_endpoints("totally-unknown-model", live_map={}) == frozenset()


def test_supports_endpoint():
    live = {"claude-opus-4.8": frozenset({"messages", "chat"})}
    assert mc.supports_endpoint("github_copilot/claude-opus-4.8", "messages", live_map=live) is True
    assert mc.supports_endpoint("github_copilot/claude-opus-4.8", "responses", live_map=live) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_litellm/llms/github_copilot/test_model_capabilities.py -k "resolve or supports" -v`
Expected: FAIL（`AttributeError`：`resolve_endpoints` 未定义）

- [ ] **Step 3: Write minimal implementation**

```python
# model_capabilities.py 追加
def _registry_endpoints(bare_model: str) -> frozenset[CopilotEndpoint]:
    entry = litellm.model_cost.get(f"github_copilot/{bare_model}")
    if not isinstance(entry, dict):
        return frozenset()
    raw = entry.get("supported_endpoints")
    return normalize_endpoints(tuple(raw)) if isinstance(raw, (list, tuple)) else frozenset()


def resolve_endpoints(
    model: str,
    *,
    live_map: Optional[EndpointMap] = None,
) -> frozenset[CopilotEndpoint]:
    bare = strip_provider_prefix(model)
    effective_live = get_live_endpoint_map() if live_map is None else live_map
    from_live = effective_live.get(bare)
    if from_live:
        return from_live
    from_registry = _registry_endpoints(bare)
    if from_registry:
        return from_registry
    return FALLBACK_ENDPOINTS.get(bare, frozenset())


def supports_endpoint(
    model: str,
    endpoint: CopilotEndpoint,
    *,
    live_map: Optional[EndpointMap] = None,
) -> bool:
    return endpoint in resolve_endpoints(model, live_map=live_map)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_litellm/llms/github_copilot/test_model_capabilities.py -v`
Expected: PASS（全绿）

- [ ] **Step 5: Commit**

```bash
git add litellm/llms/github_copilot/model_capabilities.py tests/test_litellm/llms/github_copilot/test_model_capabilities.py
git commit -m "feat(github_copilot): layered per-model endpoint capability resolver"
```

---

### Task 4: messages-native 选择接入能力 API

**Files:**
- Modify: `litellm/utils.py:8055-8061`（`get_provider_anthropic_messages_config` 的 github_copilot 分支）
- Test: `tests/test_litellm/llms/github_copilot/messages/test_github_copilot_messages_transformation.py`

**Interfaces:**
- Consumes: `supports_endpoint`（Task 3）
- Produces: 行为变更 —— github_copilot 且模型支持 `messages` 端点时返回 `GithubCopilotAnthropicMessagesConfig`；否则返回 `None`（继续沿用后续 JSON provider / None 逻辑）。fetch 不可用时 `resolve_endpoints` 已能回退到 `FALLBACK_ENDPOINTS`（claude 全在表内），claude 侧不退化

- [ ] **Step 1: Write the failing test**

```python
# test_github_copilot_messages_transformation.py 追加
from litellm.utils import ProviderConfigManager
from litellm.types.utils import LlmProviders
from litellm.llms.github_copilot.messages.transformation import (
    GithubCopilotAnthropicMessagesConfig,
)


def test_messages_config_selected_for_messages_capable_model(monkeypatch):
    import litellm.llms.github_copilot.model_capabilities as mc
    monkeypatch.setattr(mc, "get_live_endpoint_map", lambda *a, **k: {"claude-opus-4.8": frozenset({"messages", "chat"})})
    cfg = ProviderConfigManager.get_provider_anthropic_messages_config(
        model="claude-opus-4.8", provider=LlmProviders.GITHUB_COPILOT
    )
    assert isinstance(cfg, GithubCopilotAnthropicMessagesConfig)


def test_messages_config_not_selected_for_responses_only_model(monkeypatch):
    import litellm.llms.github_copilot.model_capabilities as mc
    monkeypatch.setattr(mc, "get_live_endpoint_map", lambda *a, **k: {"gpt-5.5": frozenset({"responses"})})
    cfg = ProviderConfigManager.get_provider_anthropic_messages_config(
        model="gpt-5.5", provider=LlmProviders.GITHUB_COPILOT
    )
    assert not isinstance(cfg, GithubCopilotAnthropicMessagesConfig)
```

注：此处用 `monkeypatch.setattr` 替换的是**模块级函数依赖**（等价于在无 DI 入口时注入 fetcher），非「monkeypatch 类属性做测试」那种反模式；`get_provider_anthropic_messages_config` 是静态方法、无实例可注入，故在模块函数边界注入。若后续该函数支持传入 live_map，则改为 DI。

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_litellm/llms/github_copilot/messages/test_github_copilot_messages_transformation.py -k "messages_config" -v`
Expected: FAIL（`test_messages_config_not_selected_for_responses_only_model` 失败：当前逻辑只看 `"claude" in model`，gpt-5.5 走到 JSON provider/None，但断言可能因缓存或旧逻辑不符；真正驱动实现的是它与新逻辑的差异）

- [ ] **Step 3: Write minimal implementation**

把 utils.py:8055-8061 改为：

```python
        elif litellm.LlmProviders.GITHUB_COPILOT == provider:
            from litellm.llms.github_copilot.model_capabilities import supports_endpoint

            if supports_endpoint(model, "messages"):
                from litellm.llms.github_copilot.messages.transformation import (
                    GithubCopilotAnthropicMessagesConfig,
                )

                return GithubCopilotAnthropicMessagesConfig()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_litellm/llms/github_copilot/messages/test_github_copilot_messages_transformation.py -k "messages_config" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add litellm/utils.py tests/test_litellm/llms/github_copilot/messages/test_github_copilot_messages_transformation.py
git commit -m "feat(github_copilot): select native messages config by model capability"
```

---

### Task 5: responses-vs-chat 兜底改为模型感知

**Files:**
- Modify: `litellm/llms/anthropic/experimental_pass_through/messages/handler.py:53-61`（`_should_route_to_responses_api`）与 `:515` 调用点
- Test: `tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_anthropic_experimental_pass_through_messages_handler.py`

**Interfaces:**
- Consumes: `supports_endpoint`（Task 3）
- Produces: `_should_route_to_responses_api(custom_llm_provider: Optional[str], model: Optional[str]) -> bool` —— openai 保持原语义（provider 命中即 True）；github_copilot 时以「模型支持 responses 端点」为判据；全局开关 `use_chat_completions_url_for_anthropic_messages` 仍优先返回 False

- [ ] **Step 1: Write the failing test**

```python
# test_anthropic_experimental_pass_through_messages_handler.py 追加
from litellm.llms.anthropic.experimental_pass_through.messages.handler import (
    _should_route_to_responses_api,
)


def test_route_openai_still_responses():
    assert _should_route_to_responses_api("openai", model="gpt-5.5") is True


def test_route_copilot_responses_only_model(monkeypatch):
    import litellm.llms.github_copilot.model_capabilities as mc
    monkeypatch.setattr(mc, "get_live_endpoint_map", lambda *a, **k: {"gpt-5.5": frozenset({"responses"})})
    assert _should_route_to_responses_api("github_copilot", model="gpt-5.5") is True


def test_route_copilot_chat_only_model(monkeypatch):
    import litellm.llms.github_copilot.model_capabilities as mc
    monkeypatch.setattr(mc, "get_live_endpoint_map", lambda *a, **k: {"gpt-4o": frozenset({"chat"})})
    assert _should_route_to_responses_api("github_copilot", model="gpt-4o") is False


def test_route_global_flag_forces_chat(monkeypatch):
    import litellm
    monkeypatch.setattr(litellm, "use_chat_completions_url_for_anthropic_messages", True)
    assert _should_route_to_responses_api("github_copilot", model="gpt-5.5") is False
    assert _should_route_to_responses_api("openai", model="gpt-5.5") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_anthropic_experimental_pass_through_messages_handler.py -k route -v`
Expected: FAIL（`TypeError`：`_should_route_to_responses_api` 尚不接受 `model`）

- [ ] **Step 3: Write minimal implementation**

改 `_should_route_to_responses_api`：

```python
def _should_route_to_responses_api(
    custom_llm_provider: Optional[str],
    model: Optional[str],
) -> bool:
    if litellm.use_chat_completions_url_for_anthropic_messages:
        return False
    if custom_llm_provider == "github_copilot":
        if model is None:
            return False
        from litellm.llms.github_copilot.model_capabilities import supports_endpoint

        return supports_endpoint(model, "responses")
    return custom_llm_provider in _RESPONSES_API_PROVIDERS
```

改调用点（handler.py:515）：

```python
        if _should_route_to_responses_api(custom_llm_provider, model=model):
            return LiteLLMMessagesToResponsesAPIHandler.anthropic_messages_handler(**_shared_kwargs)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_anthropic_experimental_pass_through_messages_handler.py -k route -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add litellm/llms/anthropic/experimental_pass_through/messages/handler.py tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_anthropic_experimental_pass_through_messages_handler.py
git commit -m "feat(github_copilot): route /v1/messages gpt models to responses by capability"
```

---

### Task 6: responses config 选择走同一 resolver

**Files:**
- Modify: `litellm/llms/github_copilot/responses/transformation.py`（`github_copilot_supports_responses_api`）
- Modify: `litellm/utils.py:8210-8217`（如签名/语义需要）
- Test: `tests/test_litellm/llms/github_copilot/responses/test_github_copilot_responses_transformation.py`

**Interfaces:**
- Consumes: `supports_endpoint`（Task 3）
- Produces: `github_copilot_supports_responses_api(model: str) -> bool` 改为 `return supports_endpoint(model, "responses")`，消除 `/v1/responses` vs `/responses` 命名分歧，与 messages 路由用同一真相源

- [ ] **Step 1: Write the failing test**

```python
# test_github_copilot_responses_transformation.py 追加
from litellm.llms.github_copilot.responses.transformation import (
    github_copilot_supports_responses_api,
)


def test_supports_responses_via_capability(monkeypatch):
    import litellm.llms.github_copilot.model_capabilities as mc
    monkeypatch.setattr(mc, "get_live_endpoint_map", lambda *a, **k: {"gpt-5.6-sol": frozenset({"responses"})})
    assert github_copilot_supports_responses_api("gpt-5.6-sol") is True


def test_not_supports_responses_for_chat_only(monkeypatch):
    import litellm.llms.github_copilot.model_capabilities as mc
    monkeypatch.setattr(mc, "get_live_endpoint_map", lambda *a, **k: {"gpt-4o": frozenset({"chat"})})
    assert github_copilot_supports_responses_api("gpt-4o") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_litellm/llms/github_copilot/responses/test_github_copilot_responses_transformation.py -k "supports_responses_via_capability or chat_only" -v`
Expected: 视旧实现而定；`gpt-5.6-sol`（不在旧注册表）在旧逻辑下返回 False → `test_supports_responses_via_capability` FAIL

- [ ] **Step 3: Write minimal implementation**

把 `github_copilot_supports_responses_api` 整体替换为：

```python
def github_copilot_supports_responses_api(model: str) -> bool:
    from litellm.llms.github_copilot.model_capabilities import supports_endpoint

    return supports_endpoint(model, "responses")
```

同时删除该文件中不再被引用的 `_cached_get_model_info_helper` import 等死代码（若有），并确认 utils.py:8215 调用签名 `github_copilot_supports_responses_api(model=model)` 仍匹配。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_litellm/llms/github_copilot/responses/test_github_copilot_responses_transformation.py -v`
Expected: PASS（含既有用例不回归）

- [ ] **Step 5: Commit**

```bash
git add litellm/llms/github_copilot/responses/transformation.py litellm/utils.py tests/test_litellm/llms/github_copilot/responses/test_github_copilot_responses_transformation.py
git commit -m "refactor(github_copilot): unify responses capability gate on shared resolver"
```

---

### Task 7: 合并态回归 + 真实验证

**Files:**
- Test: `tests/test_litellm/llms/github_copilot/test_model_capabilities.py`（补一条端到端路由决策的表驱动断言）

**Interfaces:**
- Consumes: 前六个任务的全部产物

- [ ] **Step 1: Write the failing test（表驱动，锁死三向分流）**

```python
import pytest


@pytest.mark.parametrize(
    "model, endpoint, expected",
    [
        ("github_copilot/claude-opus-4.8", "messages", True),
        ("github_copilot/gpt-5.5", "responses", True),
        ("github_copilot/gpt-5.5", "messages", False),
        ("github_copilot/gpt-4o", "chat", True),
        ("github_copilot/gpt-4o", "responses", False),
    ],
)
def test_three_way_routing_matrix(model, endpoint, expected):
    assert mc.supports_endpoint(model, endpoint, live_map={
        "claude-opus-4.8": frozenset({"messages", "chat"}),
        "gpt-5.5": frozenset({"responses"}),
        "gpt-4o": frozenset({"chat"}),
    }) is expected
```

- [ ] **Step 2: Run test to verify it fails / passes**

Run: `pytest tests/test_litellm/llms/github_copilot/ -v`
Expected: PASS（若前序任务无误，此为回归锁）

- [ ] **Step 3: `make pre-commit` 全量把关**

Run: `make pre-commit`
Expected: 无 lint/type/format 报错；如触及 budget，跑 `make lint-budget-update` 并把下调后的上限一起提交

- [ ] **Step 4: 真实验证（按项目约定，非 pytest 截图）**

起本地 proxy：

```bash
python litellm/proxy/proxy_cli.py --config litellm/proxy/dev_config.yaml --detailed_debug --reload --use_v2_migration_resolver 2>&1 | tee litellm.log
```

对 `/v1/messages` 分别用一个 claude 模型、一个 responses-only 的 gpt 模型、一个 chat-only 模型各发一条 curl（真实打 Copilot、真实计费），观察 `litellm.log` 中实际命中的上游端点（`/v1/messages` vs `/responses` vs `/chat/completions`）与成功响应。把命令与输出整理进 PR 的 Proof of Fix。

- [ ] **Step 5: Commit**

```bash
git add tests/test_litellm/llms/github_copilot/test_model_capabilities.py
git commit -m "test(github_copilot): three-way /v1/messages routing regression matrix"
```

---

## 待确认项（执行前请拍板）

1. **fallback 载体**：本计划把路由 fallback 做成 capability 模块内的 `FALLBACK_ENDPOINTS`（只含端点、来自上游 `/models`），而非往 `model_prices_and_context_window.json` 塞条目。原因：上游只提供端点与上限、**不提供每 token 定价**，向定价注册表塞入无定价/臆造定价条目违反 CLAUDE.md「禁止臆造数据」。这对「补齐注册表」是一处 refine。若你确实要同时补齐定价注册表（用于计费/`model_info`），那是一条独立的数据维护工作，建议单开一个 plan，且需要官方定价来源。请确认是否接受这个 refine
2. **copilot `/models` URL**：计划里 fetcher 打 `{api_base}/models`（与你给的 `AVAILABLE_MODELS.json` 来源一致）。执行 Task 2 前用一次真实 token 确认该 URL 与鉴权 headers 无误（既有 `get_models` 继承自 OpenAIConfig 打的是 `/v1/models`，两者需对齐）
3. **`gpt-5-mini` 归属**：上游 `AVAILABLE_MODELS.json` 里 `gpt-5-mini` vendor 是 Azure OpenAI，`supported_endpoints` 含 responses；已按此放进 `FALLBACK_ENDPOINTS`。若你的 Copilot 租户看到的能力不同，以真实 `/models` 为准（主路会覆盖 fallback）

## Self-Review

- **Spec coverage**：路由规则（Task 4/5）、能力图 + 归一化 + live 主路 + fallback（Task 1/2/3）、responses config 统一（Task 6）、注册表 fallback（Task 1 的 `FALLBACK_ENDPOINTS`，见待确认项 1 的 refine）、测试（各 Task + Task 7）、真实验证（Task 7 Step 4）均覆盖。非目标「特性屏蔽」未纳入，符合 spec
- **Placeholder scan**：无 TBD/TODO；每个代码步骤给出完整代码与预期
- **Type consistency**：`CopilotEndpoint` / `EndpointMap` / `resolve_endpoints` / `supports_endpoint` / `get_live_endpoint_map` / `_should_route_to_responses_api(custom_llm_provider, model)` 在各 Task 间签名一致
