# GitHub Copilot `/v1/messages` 动态原生端点路由 Implementation Plan

> 状态：**已实现（2026-07-13）**。9 个 Task 全部落地并 TDD 通过；相关测试 161 passed。实现细节相对本计划的偏差（均已核实、非降级）：① 定时刷新改用无条件的 `asyncio.create_task(periodic_capability_refresh_loop())`（不挂 prisma-gated 的 AsyncIOScheduler，DB 无关）；② 冷启动兜底读**原始 `litellm.model_cost`**（`_cached_get_model_info_helper` 会丢 supported_endpoints）；③ 缓存**直接存 tuple 对象**并 `delete+set` 续期 TTL（InMemoryCache.get_cache 会 json.loads，不能存 json 串）；④ HTTP client 直接类型标注为 `HTTPHandler`（Protocol 与其松签名结构不匹配），边界用 Pydantic/TypeAdapter 校验；⑤ 刷新写入键与路由读取键统一为 `copilot_api_base()`（避免 cache miss）。真实验证：用真实上游 `/models`（40 模型）确认 claude→messages、gpt-5.5/5.6→responses、gpt-5.4→responses、gpt-4o/gemini→chat 全部正确。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让经 litellm `/v1/messages` 进来的 `github_copilot/<model>` 请求，按该模型**动态获取的** `supported_endpoints`（定时拉上游 `/models` 并缓存）三向分流到 Copilot 原生端点，叠加 operator 的 `mode` 硬 override，无任何 hardcode 端点表。

**Architecture:** 新增 `github_copilot/model_capabilities.py`：动态拉 `/models`（保留 `supported_endpoints`）→ 按 api_base 的 TTL 缓存（镜像既有 `AvailableModelsCache`）→ 由 proxy `AsyncIOScheduler` 定时后台刷新 → 对外暴露 `resolve_endpoints` / 三态判据 resolver 作为单一真相源。三个既有决策点改用它；`mode` override 与 lru_cache 旁路都在接线层处理。

**Tech Stack:** Python，litellm provider config 体系，`InMemoryCache`（TTL 缓存底座），`litellm.module_level_client`（同步 HTTP，短 timeout），apscheduler（既有 proxy scheduler），Pydantic（`/models` 边界校验），pytest。

## Global Constraints

- 不写任何注释（除非本任务步骤给出的代码已含），遵循项目 CLAUDE.md
- Python 行宽上限 120
- 强类型：禁止 `Any` / 裸 `dict` / `dict[str, Any]`；每个函数参数精确类型。吃 `/models` JSON 用 Pydantic / `TypeAdapter` 在边界校验成 typed 值
- 依赖注入优先：HTTP client / clock / fetcher 作参数传入；**不 monkeypatch 类属性做测试**（既有静态入口无 DI 时，可 monkeypatch 模块函数作有限接缝，但核心 resolver 测试用 DI）
- 不可变数据：用 `tuple` of pairs + `frozenset(("a","b"))`（tuple 入参，非 set 字面量）+ 冻结 dataclass；**禁止 dict/set/list 字面量与 comprehension**（会触发 LIT002）。缓存单元复用 `InMemoryCache` 子类（库内实现，不受 LIT 约束）
- 不静默吞异常：fetch 失败记 debug 日志并返回空
- conventional commits；提交信息无任何 Claude / Co-authored-by 署名；分支名不含 `/`
- 提交前跑相关测试 + `make pre-commit`（有 staged 后端改动时）
- **精确 hunk staging**：工作区尚有其它未提交改动（`adapters/transformation.py`、`utils.py`、`count_tokens.py`）。改共享文件（尤其 `utils.py`）用 `git add -p` 只 stage 本任务 hunk，提交前核对 `git diff --cached`；绝不 `git add -A` 或整文件 add 有他人 hunk 的文件
- 若修掉 budget 门控违规，跑 `make lint-budget-update` 并提交下调后的上限；新增文件执行前先单独跑 `python scripts/check_type_discipline.py <file>` 确认零新违规

## 领域约定（贯穿全计划）

- 三态端点：`CopilotEndpoint = Literal["messages", "responses", "chat"]`
- `mode` override 语义（operator 在 config `model_info.mode` 声明，优先级高于动态端点）：
  - `"anthropic"` → 强制 messages
  - `"responses"` → 强制 responses
  - `"chat"` → responses 判据里视作「不走 responses」（既有 opt-out）；**不阻断 messages 判据**（兼容现有 `mode: chat` + endpoints 含 `/v1/messages` 的 claude 部署）
  - 未设 → 纯按动态 `supported_endpoints`
- 数据分层（单一真相源 resolver）：动态 `/models` 缓存（主）→ `model_info.supported_endpoints`（operator 配置兜底）→ 空集（保守当 chat）
- model_info 读取复用既有 `_cached_get_model_info_helper(model=bare, custom_llm_provider="github_copilot")`（含 config `model_list` 的 model_info，经 `register_model` 并入）

---

### Task 1: 端点归一化 + 前缀处理（纯函数骨架）

**Files:**
- Create: `litellm/llms/github_copilot/model_capabilities.py`
- Test: `tests/test_litellm/llms/github_copilot/test_model_capabilities.py`

**Interfaces:**
- Produces:
  - `CopilotEndpoint = Literal["messages", "responses", "chat"]`
  - `EndpointSet = frozenset[CopilotEndpoint]`
  - `normalize_endpoints(raw: tuple[str, ...]) -> EndpointSet`
  - `strip_copilot_prefix(model: str) -> str`（仅去 `github_copilot/` 精确前缀）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_litellm/llms/github_copilot/test_model_capabilities.py
from litellm.llms.github_copilot.model_capabilities import (
    normalize_endpoints,
    strip_copilot_prefix,
)


def test_normalize_upstream_and_config_names():
    assert normalize_endpoints(("/responses", "ws:/responses")) == frozenset({"responses"})
    assert normalize_endpoints(("/v1/messages", "/chat/completions")) == frozenset({"messages", "chat"})
    assert normalize_endpoints(("/v1/chat/completions", "/v1/responses")) == frozenset({"chat", "responses"})


def test_normalize_drops_unknown_and_empty():
    assert normalize_endpoints(("/foo", "ws:/responses")) == frozenset({"responses"})
    assert normalize_endpoints(()) == frozenset()


def test_strip_only_copilot_prefix():
    assert strip_copilot_prefix("github_copilot/gpt-5.6-sol") == "gpt-5.6-sol"
    assert strip_copilot_prefix("claude-opus-4.8") == "claude-opus-4.8"
    assert strip_copilot_prefix("vendor/weird/model") == "vendor/weird/model"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_litellm/llms/github_copilot/test_model_capabilities.py -v`
Expected: FAIL（ImportError：模块不存在）

- [ ] **Step 3: 最小实现**

```python
# litellm/llms/github_copilot/model_capabilities.py
from typing import Literal

CopilotEndpoint = Literal["messages", "responses", "chat"]
EndpointSet = frozenset

_ALIASES: tuple[tuple[str, CopilotEndpoint], ...] = (
    ("/v1/messages", "messages"),
    ("messages", "messages"),
    ("/responses", "responses"),
    ("/v1/responses", "responses"),
    ("ws:/responses", "responses"),
    ("responses", "responses"),
    ("/chat/completions", "chat"),
    ("/v1/chat/completions", "chat"),
    ("chat", "chat"),
)


def normalize_endpoints(raw: tuple[str, ...]) -> "frozenset[CopilotEndpoint]":
    return frozenset(
        canon for name in raw for alias, canon in _ALIASES if alias == name
    )


def strip_copilot_prefix(model: str) -> str:
    prefix = "github_copilot/"
    return model[len(prefix):] if model.startswith(prefix) else model
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_litellm/llms/github_copilot/test_model_capabilities.py -v`
Expected: PASS

- [ ] **Step 5: 类型纪律 + 提交**

```bash
python scripts/check_type_discipline.py litellm/llms/github_copilot/model_capabilities.py
git add litellm/llms/github_copilot/model_capabilities.py tests/test_litellm/llms/github_copilot/test_model_capabilities.py
git commit -m "feat(github_copilot): endpoint normalization + prefix helpers"
```

---

### Task 2: 动态 `/models` 取值（保留 supported_endpoints，DI client）

**Files:**
- Modify: `litellm/llms/github_copilot/model_capabilities.py`
- Test: `tests/test_litellm/llms/github_copilot/test_model_capabilities.py`

**Interfaces:**
- Consumes: `normalize_endpoints`（Task 1）；`get_copilot_default_headers`、`DEFAULT_GITHUB_COPILOT_API_BASE`（common_utils）
- Produces:
  - `EndpointPairs = tuple[tuple[str, frozenset[CopilotEndpoint]], ...]`
  - `fetch_endpoint_pairs(api_key: str, api_base: str, client: HTTPHandler, timeout: float = 5.0) -> EndpointPairs` —— 同步 GET `{api_base}/models`，Pydantic 校验后归一；非 200 抛 `RuntimeError`；client 依赖注入

前置 PoC（执行 Task 2 前）：用一次真实 token 确认 `{api_base}/models` 的 URL 与鉴权 header 正确（既有 `get_models` 继承 OpenAIConfig 打 `/v1/models`，两者需对齐；用户提供的 `AVAILABLE_MODELS.json` 来自 `/models`）。把结论写回本任务，再实现。

- [ ] **Step 1: 写失败测试（DI 假 client）**

```python
# 追加到 test_model_capabilities.py
from litellm.llms.github_copilot.model_capabilities import fetch_endpoint_pairs


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, timeout))
        return self._resp


_MODELS_PAYLOAD = {
    "data": [
        {"id": "claude-opus-4.8", "supported_endpoints": ["/v1/messages", "/chat/completions"]},
        {"id": "gpt-5.5", "supported_endpoints": ["/responses", "ws:/responses"]},
        {"id": "gpt-4o"},
    ]
}


def test_fetch_endpoint_pairs_preserves_endpoints():
    client = _FakeClient(_FakeResp(200, _MODELS_PAYLOAD))
    pairs = fetch_endpoint_pairs("k", "https://api.githubcopilot.com", client)
    as_map = dict(pairs)
    assert as_map["claude-opus-4.8"] == frozenset({"messages", "chat"})
    assert as_map["gpt-5.5"] == frozenset({"responses"})
    assert as_map["gpt-4o"] == frozenset()
    assert client.calls[0][0] == "https://api.githubcopilot.com/models"


def test_fetch_endpoint_pairs_non_200_raises():
    import pytest
    client = _FakeClient(_FakeResp(401, {"error": "no auth"}))
    with pytest.raises(RuntimeError):
        fetch_endpoint_pairs("k", "https://api.githubcopilot.com", client)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_litellm/llms/github_copilot/test_model_capabilities.py -k fetch -v`
Expected: FAIL（ImportError：`fetch_endpoint_pairs` 未定义）

- [ ] **Step 3: 实现**

```python
# model_capabilities.py 追加（顶部补 imports）
from pydantic import BaseModel, TypeAdapter

from litellm.llms.custom_httpx.http_handler import HTTPHandler
from litellm.llms.github_copilot.common_utils import get_copilot_default_headers


class _ModelEntry(BaseModel):
    id: str
    supported_endpoints: tuple[str, ...] = ()


class _ModelsResponse(BaseModel):
    data: tuple[_ModelEntry, ...]


_MODELS_ADAPTER = TypeAdapter(_ModelsResponse)


def fetch_endpoint_pairs(
    api_key: str,
    api_base: str,
    client: HTTPHandler,
    timeout: float = 5.0,
) -> "tuple[tuple[str, frozenset[CopilotEndpoint]], ...]":
    url = f"{api_base.rstrip('/')}/models"
    resp = client.get(url, headers=get_copilot_default_headers(api_key), timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"github_copilot /models HTTP {resp.status_code}: {resp.text[:200]}")
    parsed = _MODELS_ADAPTER.validate_python(resp.json())
    return tuple(
        (entry.id, normalize_endpoints(entry.supported_endpoints)) for entry in parsed.data
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_litellm/llms/github_copilot/test_model_capabilities.py -k fetch -v`
Expected: PASS

- [ ] **Step 5: 类型纪律 + 提交**

```bash
python scripts/check_type_discipline.py litellm/llms/github_copilot/model_capabilities.py
git add litellm/llms/github_copilot/model_capabilities.py tests/test_litellm/llms/github_copilot/test_model_capabilities.py
git commit -m "feat(github_copilot): fetch /models preserving per-model supported_endpoints"
```

---

### Task 3: 按 api_base 的 TTL 缓存（镜像 AvailableModelsCache）

**Files:**
- Modify: `litellm/llms/github_copilot/model_capabilities.py`
- Test: `tests/test_litellm/llms/github_copilot/test_model_capabilities.py`

**Interfaces:**
- Consumes: `fetch_endpoint_pairs`（Task 2）；`InMemoryCache`
- Produces:
  - `refresh_capabilities(api_key: str, api_base: str, client: HTTPHandler) -> EndpointPairs` —— 拉取并写缓存（键为 api_base），失败记 debug、返回空 tuple、不抛
  - `get_cached_pairs(api_base: str) -> Optional[EndpointPairs]` —— 只读缓存，未命中返回 None

缓存值存 JSON 可序列化形态（`tuple[[id, sorted endpoints], ...]`），读时用生成器重建 `frozenset`，避免 dict 字面量/comprehension。

- [ ] **Step 1: 写失败测试**

```python
# 追加
import litellm.llms.github_copilot.model_capabilities as mc


def test_refresh_and_get_cached():
    mc._CAP_CACHE.cache_dict.clear()
    client = _FakeClient(_FakeResp(200, _MODELS_PAYLOAD))
    pairs = mc.refresh_capabilities("k", "https://api.githubcopilot.com", client)
    assert dict(pairs)["gpt-5.5"] == frozenset({"responses"})
    cached = mc.get_cached_pairs("https://api.githubcopilot.com")
    assert cached is not None
    assert dict(cached)["gpt-5.5"] == frozenset({"responses"})


def test_refresh_failure_returns_empty_no_raise():
    mc._CAP_CACHE.cache_dict.clear()
    client = _FakeClient(_FakeResp(500, {"e": 1}))
    assert mc.refresh_capabilities("k", "https://api.githubcopilot.com", client) == ()


def test_get_cached_miss_returns_none():
    mc._CAP_CACHE.cache_dict.clear()
    assert mc.get_cached_pairs("https://unseen.example") is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_litellm/llms/github_copilot/test_model_capabilities.py -k "cached or refresh" -v`
Expected: FAIL（AttributeError：`_CAP_CACHE` / `refresh_capabilities` 未定义）

- [ ] **Step 3: 实现**

```python
# model_capabilities.py 追加
import json
from typing import Optional

from litellm._logging import verbose_logger
from litellm.caching.in_memory_cache import InMemoryCache

_CAP_CACHE = InMemoryCache(ttl_seconds=300, max_size=64)


def _encode_pairs(pairs: "tuple[tuple[str, frozenset[CopilotEndpoint]], ...]") -> str:
    return json.dumps([[model, sorted(eps)] for model, eps in pairs])


def _decode_pairs(raw: str) -> "tuple[tuple[str, frozenset[CopilotEndpoint]], ...]":
    return tuple((model, frozenset(eps)) for model, eps in json.loads(raw))


def refresh_capabilities(
    api_key: str,
    api_base: str,
    client: HTTPHandler,
) -> "tuple[tuple[str, frozenset[CopilotEndpoint]], ...]":
    try:
        pairs = fetch_endpoint_pairs(api_key=api_key, api_base=api_base, client=client)
    except Exception as e:
        verbose_logger.debug("github_copilot refresh_capabilities failed for %s: %s", api_base, e)
        return ()
    _CAP_CACHE.set_cache(api_base, _encode_pairs(pairs))
    return pairs


def get_cached_pairs(api_base: str) -> Optional["tuple[tuple[str, frozenset[CopilotEndpoint]], ...]"]:
    raw = _CAP_CACHE.get_cache(api_base)
    if not isinstance(raw, str):
        return None
    return _decode_pairs(raw)
```

注：`_CAP_CACHE` 是 `InMemoryCache` 实例（库内可变实现，不受 LIT 约束），测试用 `.cache_dict.clear()` 隔离。列表推导仅出现在 encode/decode 内部对 JSON 的一次成形，返回 `tuple`；`_encode_pairs` 的 `[[...]]` 是 json 序列化输入，若 LIT002 命中则以 `# mutable-ok: json 序列化载荷` 豁免该行。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_litellm/llms/github_copilot/test_model_capabilities.py -k "cached or refresh" -v`
Expected: PASS

- [ ] **Step 5: 类型纪律 + 提交**

```bash
python scripts/check_type_discipline.py litellm/llms/github_copilot/model_capabilities.py
git add litellm/llms/github_copilot/model_capabilities.py tests/test_litellm/llms/github_copilot/test_model_capabilities.py
git commit -m "feat(github_copilot): TTL-cached capability map keyed by api_base"
```

---

### Task 4: 单一真相源 resolver（动态 → model_info 兜底 → mode override）

**Files:**
- Modify: `litellm/llms/github_copilot/model_capabilities.py`
- Test: `tests/test_litellm/llms/github_copilot/test_model_capabilities.py`

**Interfaces:**
- Consumes: `get_cached_pairs`、`strip_copilot_prefix`、`normalize_endpoints`；`_cached_get_model_info_helper`（utils）
- Produces:
  - `resolve_endpoints(model: str, *, model_info: Mapping[str, object], api_base: Optional[str]) -> EndpointSet` —— 动态缓存优先，缺失则用 `model_info.supported_endpoints`，再无返回空集（不含 mode）
  - `forced_mode(model_info: Mapping[str, object]) -> Optional[Literal["anthropic", "responses", "chat"]]`
  - `route_supports_messages(model, *, model_info, api_base) -> bool` —— `mode=="anthropic"` 或端点含 messages
  - `route_supports_responses(model, *, model_info, api_base) -> bool` —— `mode=="responses"`→True；`mode in {chat, anthropic}`→False；否则端点含 responses

`model_info` 由调用方传入（DI），生产代码传 `_cached_get_model_info_helper(...)` 的结果；测试直接传 dict。

- [ ] **Step 1: 写失败测试**

```python
# 追加
def test_resolve_prefers_dynamic_cache():
    mc._CAP_CACHE.cache_dict.clear()
    mc._CAP_CACHE.set_cache("https://b", mc._encode_pairs((("gpt-5.5", frozenset({"responses"})),)))
    eps = mc.resolve_endpoints("github_copilot/gpt-5.5", model_info={}, api_base="https://b")
    assert eps == frozenset({"responses"})


def test_resolve_falls_back_to_model_info():
    mc._CAP_CACHE.cache_dict.clear()
    eps = mc.resolve_endpoints(
        "gpt-x", model_info={"supported_endpoints": ["/responses"]}, api_base="https://b"
    )
    assert eps == frozenset({"responses"})


def test_route_supports_messages_mode_anthropic_forces():
    mc._CAP_CACHE.cache_dict.clear()
    assert mc.route_supports_messages("m", model_info={"mode": "anthropic"}, api_base=None) is True


def test_mode_chat_does_not_block_messages():
    mc._CAP_CACHE.cache_dict.clear()
    info = {"mode": "chat", "supported_endpoints": ["/v1/chat/completions", "/v1/messages"]}
    assert mc.route_supports_messages("claude-x", model_info=info, api_base=None) is True
    assert mc.route_supports_responses("claude-x", model_info=info, api_base=None) is False


def test_mode_responses_forces_responses():
    mc._CAP_CACHE.cache_dict.clear()
    assert mc.route_supports_responses("m", model_info={"mode": "responses"}, api_base=None) is True
    assert mc.route_supports_messages("m", model_info={"mode": "responses"}, api_base=None) is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_litellm/llms/github_copilot/test_model_capabilities.py -k "resolve or route or mode" -v`
Expected: FAIL（AttributeError：resolver 函数未定义）

- [ ] **Step 3: 实现**

```python
# model_capabilities.py 追加
from typing import Mapping


def resolve_endpoints(
    model: str,
    *,
    model_info: Mapping[str, object],
    api_base: Optional[str],
) -> "frozenset[CopilotEndpoint]":
    bare = strip_copilot_prefix(model)
    if api_base is not None:
        cached = get_cached_pairs(api_base)
        if cached is not None:
            hit = next((eps for m, eps in cached if m == bare), None)
            if hit:
                return hit
    raw = model_info.get("supported_endpoints")
    if isinstance(raw, (list, tuple)):
        return normalize_endpoints(tuple(str(x) for x in raw))
    return frozenset()


def forced_mode(
    model_info: Mapping[str, object],
) -> "Optional[Literal['anthropic', 'responses', 'chat']]":
    mode = model_info.get("mode")
    return mode if mode in ("anthropic", "responses", "chat") else None


def route_supports_messages(
    model: str,
    *,
    model_info: Mapping[str, object],
    api_base: Optional[str],
) -> bool:
    mode = forced_mode(model_info)
    if mode == "anthropic":
        return True
    if mode in ("responses",):
        return False
    return "messages" in resolve_endpoints(model, model_info=model_info, api_base=api_base)


def route_supports_responses(
    model: str,
    *,
    model_info: Mapping[str, object],
    api_base: Optional[str],
) -> bool:
    mode = forced_mode(model_info)
    if mode == "responses":
        return True
    if mode in ("chat", "anthropic"):
        return False
    return "responses" in resolve_endpoints(model, model_info=model_info, api_base=api_base)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_litellm/llms/github_copilot/test_model_capabilities.py -v`
Expected: PASS（全绿）

- [ ] **Step 5: 类型纪律 + 提交**

```bash
python scripts/check_type_discipline.py litellm/llms/github_copilot/model_capabilities.py
git add litellm/llms/github_copilot/model_capabilities.py tests/test_litellm/llms/github_copilot/test_model_capabilities.py
git commit -m "feat(github_copilot): capability resolver with mode override + model_info fallback"
```

---

### Task 5: 定时后台刷新（挂 proxy scheduler）

**Files:**
- Modify: `litellm/llms/github_copilot/model_capabilities.py`（加一个可被调度的刷新入口）
- Modify: `litellm/proxy/proxy_server.py`（在既有 `AsyncIOScheduler` 上 `add_job`）
- Test: `tests/test_litellm/llms/github_copilot/test_model_capabilities.py`

**Interfaces:**
- Produces: `refresh_all_copilot_deployments(router) -> None` —— 遍历 router 里 github_copilot deployment 的 (api_key, api_base)，逐个 `refresh_capabilities`；无 router / 无 copilot 部署时直接返回

- [ ] **Step 1: 写失败测试（DI 假 router + 假 client 工厂）**

```python
# 追加
def test_refresh_all_iterates_copilot_deployments():
    mc._CAP_CACHE.cache_dict.clear()
    calls = []

    def fake_refresh(api_key, api_base, client):
        calls.append((api_key, api_base))
        return ()

    class _Router:
        def get_model_list(self):
            return [
                {"litellm_params": {"model": "github_copilot/gpt-5.5", "api_base": "https://b1"}},
                {"litellm_params": {"model": "openai/gpt-4o", "api_base": "https://x"}},
            ]

    mc.refresh_all_copilot_deployments(_Router(), _refresh=fake_refresh, _client=object())
    assert ("", "https://b1") in [(k, b) for k, b in calls] or any(b == "https://b1" for _, b in calls)
    assert all(b != "https://x" for _, b in calls)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_litellm/llms/github_copilot/test_model_capabilities.py -k refresh_all -v`
Expected: FAIL（AttributeError：`refresh_all_copilot_deployments` 未定义）

- [ ] **Step 3: 实现**

```python
# model_capabilities.py 追加
from litellm.llms.github_copilot.authenticator import Authenticator
from litellm.llms.github_copilot.common_utils import DEFAULT_GITHUB_COPILOT_API_BASE

_authenticator = Authenticator()


def _copilot_bases_from_router(router: object) -> "tuple[tuple[str, str], ...]":
    get_list = getattr(router, "get_model_list", None)
    if get_list is None:
        return ()
    return tuple(
        ("", str(p.get("api_base") or DEFAULT_GITHUB_COPILOT_API_BASE))
        for d in (get_list() or ())
        for p in (d.get("litellm_params", {}),)
        if str(p.get("model", "")).startswith("github_copilot/")
    )


def refresh_all_copilot_deployments(router, _refresh=refresh_capabilities, _client=None) -> None:
    from litellm.llms.custom_httpx.http_handler import HTTPHandler

    client = _client if _client is not None else HTTPHandler()
    try:
        api_key = _authenticator.get_api_key()
    except Exception as e:
        verbose_logger.debug("github_copilot refresh_all: no api key (%s)", e)
        return
    for _, api_base in frozenset(_copilot_bases_from_router(router)):
        _refresh(api_key, api_base, client)
```

在 `proxy_server.py` 既有 scheduler（约 7437 起）添加：

```python
        scheduler.add_job(
            refresh_all_copilot_deployments,
            "interval",
            seconds=300,
            args=[llm_router],
        )
```

（import：`from litellm.llms.github_copilot.model_capabilities import refresh_all_copilot_deployments`；`llm_router` 用该作用域内既有的 router 变量名）

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_litellm/llms/github_copilot/test_model_capabilities.py -k refresh_all -v`
Expected: PASS

- [ ] **Step 5: 类型纪律 + 提交（精确 staging）**

```bash
python scripts/check_type_discipline.py litellm/llms/github_copilot/model_capabilities.py
git add litellm/llms/github_copilot/model_capabilities.py tests/test_litellm/llms/github_copilot/test_model_capabilities.py
git add -p litellm/proxy/proxy_server.py   # 只 stage 本任务的 add_job + import hunk
git diff --cached --stat
git commit -m "feat(github_copilot): periodic background refresh of capability cache"
```

---

### Task 6: messages-native 选择接入 resolver（lru_cache 旁路）

**Files:**
- Modify: `litellm/utils.py`（`get_provider_anthropic_messages_config` 公开方法，`:8005` 附近；github_copilot 分支从 lru_cached 内挪到公开方法）
- Test: `tests/test_litellm/llms/github_copilot/messages/test_github_copilot_messages_transformation.py`

**Interfaces:**
- Consumes: `route_supports_messages`（Task 4）
- Produces: 公开方法在委托给 `@lru_cache` 的 `_get_provider_anthropic_messages_config_cached` **之前**处理 GITHUB_COPILOT；`route_supports_messages` 为真返回 `GithubCopilotAnthropicMessagesConfig()`，否则继续原逻辑（返回 None / JSON provider）

- [ ] **Step 1: 写失败测试（含杀变异用例）**

```python
# test_github_copilot_messages_transformation.py 追加
from litellm.utils import ProviderConfigManager
from litellm.types.utils import LlmProviders
from litellm.llms.github_copilot.messages.transformation import (
    GithubCopilotAnthropicMessagesConfig,
)


def _patch_caps(monkeypatch, supports_messages: bool):
    import litellm.utils as u
    monkeypatch.setattr(u, "_cached_get_model_info_helper", lambda **k: {})
    import litellm.llms.github_copilot.model_capabilities as mc
    monkeypatch.setattr(mc, "route_supports_messages", lambda *a, **k: supports_messages)


def test_non_claude_but_messages_capable_selects_messages_config(monkeypatch):
    _patch_caps(monkeypatch, supports_messages=True)
    cfg = ProviderConfigManager.get_provider_anthropic_messages_config(
        model="gpt-weird", provider=LlmProviders.GITHUB_COPILOT
    )
    assert isinstance(cfg, GithubCopilotAnthropicMessagesConfig)


def test_claude_named_but_not_messages_capable_not_selected(monkeypatch):
    _patch_caps(monkeypatch, supports_messages=False)
    cfg = ProviderConfigManager.get_provider_anthropic_messages_config(
        model="claude-responses-only", provider=LlmProviders.GITHUB_COPILOT
    )
    assert not isinstance(cfg, GithubCopilotAnthropicMessagesConfig)
```

注：这两个用例专为杀「退回 `"claude" in model` 字符串判断」变异——非 claude 但 messages-capable 必须选中、名字含 claude 但不 capable 必须不选。旧逻辑两者都判反。为绕开 `_get_provider_anthropic_messages_config_cached` 的 lru_cache，实现须把 copilot 分支放在公开方法（未缓存），测试用未见过的 model 名避免命中残留缓存。

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_litellm/llms/github_copilot/messages/test_github_copilot_messages_transformation.py -k "messages_capable or not_selected" -v`
Expected: FAIL（旧逻辑：`gpt-weird` 无 claude → None → 第一个断言失败）

- [ ] **Step 3: 实现**

在 `get_provider_anthropic_messages_config` 公开方法（委托 `_get_provider_anthropic_messages_config_cached` 之前）插入：

```python
    @staticmethod
    def get_provider_anthropic_messages_config(
        model: str,
        provider: LlmProviders,
    ) -> Optional[BaseAnthropicMessagesConfig]:
        if litellm.LlmProviders.GITHUB_COPILOT == provider:
            from litellm.llms.github_copilot.model_capabilities import route_supports_messages

            model_info = _cached_get_model_info_helper(model=model, custom_llm_provider="github_copilot")
            api_base = _authenticator_api_base()
            if route_supports_messages(model, model_info=model_info, api_base=api_base):
                from litellm.llms.github_copilot.messages.transformation import (
                    GithubCopilotAnthropicMessagesConfig,
                )

                return GithubCopilotAnthropicMessagesConfig()
            return None
        return ProviderConfigManager._get_provider_anthropic_messages_config_cached(model=model, provider=provider)
```

并删除 `_get_provider_anthropic_messages_config_cached` 里原 github_copilot 的 `"claude" in model_lower` 分支（已上移）。`_authenticator_api_base()` 为一小助手（读 `Authenticator().get_api_base()`，异常返回 None），可置于 utils 或复用 model_capabilities 暴露的读取器。`_cached_get_model_info_helper` 若对未知模型抛异常，用 try 包裹并降级为 `{}`（在 caller 处 typed 处理，不吞语义）。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_litellm/llms/github_copilot/messages/test_github_copilot_messages_transformation.py -v`
Expected: PASS

- [ ] **Step 5: 类型纪律 + 精确提交**

```bash
git add -p litellm/utils.py
git diff --cached
git add tests/test_litellm/llms/github_copilot/messages/test_github_copilot_messages_transformation.py
git commit -m "feat(github_copilot): select native messages config by dynamic capability + mode"
```

---

### Task 7: responses-vs-chat 兜底改模型感知

**Files:**
- Modify: `litellm/llms/anthropic/experimental_pass_through/messages/handler.py`（`_should_route_to_responses_api` `:53` 与调用点 `:515`）
- Test: `tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_anthropic_experimental_pass_through_messages_handler.py`

**Interfaces:**
- Consumes: `route_supports_responses`（Task 4）
- Produces: `_should_route_to_responses_api(custom_llm_provider: Optional[str], model: Optional[str], model_info: Optional[Mapping[str, object]]) -> bool` —— openai 保持 provider 命中即 True；github_copilot 用 `route_supports_responses`；全局开关优先 False

- [ ] **Step 1: 写失败测试**

```python
# 追加
from litellm.llms.anthropic.experimental_pass_through.messages.handler import (
    _should_route_to_responses_api,
)


def test_openai_still_responses():
    assert _should_route_to_responses_api("openai", model="gpt-5.5", model_info=None) is True


def test_copilot_responses_only(monkeypatch):
    import litellm.llms.github_copilot.model_capabilities as mc
    monkeypatch.setattr(mc, "route_supports_responses", lambda *a, **k: True)
    assert _should_route_to_responses_api("github_copilot", model="gpt-5.5", model_info={}) is True


def test_copilot_chat_only(monkeypatch):
    import litellm.llms.github_copilot.model_capabilities as mc
    monkeypatch.setattr(mc, "route_supports_responses", lambda *a, **k: False)
    assert _should_route_to_responses_api("github_copilot", model="gpt-4o", model_info={}) is False


def test_global_flag_forces_chat(monkeypatch):
    import litellm
    monkeypatch.setattr(litellm, "use_chat_completions_url_for_anthropic_messages", True)
    assert _should_route_to_responses_api("github_copilot", model="gpt-5.5", model_info={}) is False
    assert _should_route_to_responses_api("openai", model="gpt-5.5", model_info=None) is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_anthropic_experimental_pass_through_messages_handler.py -k "responses_only or chat_only or forces_chat or still_responses" -v`
Expected: FAIL（`TypeError`：旧签名不接受 `model` / `model_info`）

- [ ] **Step 3: 实现**

```python
def _should_route_to_responses_api(
    custom_llm_provider: Optional[str],
    model: Optional[str],
    model_info: Optional[Mapping[str, object]],
) -> bool:
    if litellm.use_chat_completions_url_for_anthropic_messages:
        return False
    if custom_llm_provider == "github_copilot":
        if model is None:
            return False
        from litellm.llms.github_copilot.model_capabilities import route_supports_responses

        api_base = _copilot_api_base_or_none()
        return route_supports_responses(model, model_info=model_info or {}, api_base=api_base)
    return custom_llm_provider in _RESPONSES_API_PROVIDERS
```

调用点（`:515`）改为传 `model` 与 `kwargs.get("model_info")`：

```python
        if _should_route_to_responses_api(custom_llm_provider, model=model, model_info=kwargs.get("model_info")):
            return LiteLLMMessagesToResponsesAPIHandler.anthropic_messages_handler(**_shared_kwargs)
```

`_copilot_api_base_or_none()` 复用 model_capabilities 暴露的 api_base 读取器（异常→None）。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_anthropic_experimental_pass_through_messages_handler.py -k "responses_only or chat_only or forces_chat or still_responses" -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add litellm/llms/anthropic/experimental_pass_through/messages/handler.py tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_anthropic_experimental_pass_through_messages_handler.py
git commit -m "feat(github_copilot): route /v1/messages gpt models to responses by dynamic capability"
```

---

### Task 8: responses config 选择统一到 resolver（保留 mode 契约）

**Files:**
- Modify: `litellm/llms/github_copilot/responses/transformation.py`（`github_copilot_supports_responses_api`）
- Modify: `litellm/utils.py:8215`（如调用签名需要）
- Test: `tests/test_litellm/llms/github_copilot/responses/test_github_copilot_responses_transformation.py`

**Interfaces:**
- Consumes: `route_supports_responses`（Task 4）
- Produces: `github_copilot_supports_responses_api(model: str) -> bool` 改为读 model_info + `route_supports_responses`；**保留** `mode=responses`→True、`mode in {chat, anthropic}`→False 的语义，既有 mode/register_model 测试不回归

- [ ] **Step 1: 写失败测试（新增动态用例 + 保留 mode 回归）**

```python
# test_github_copilot_responses_transformation.py 追加
from litellm.llms.github_copilot.responses.transformation import (
    github_copilot_supports_responses_api,
)


def test_dynamic_responses_only_model(monkeypatch):
    import litellm.llms.github_copilot.model_capabilities as mc
    monkeypatch.setattr(mc, "route_supports_responses", lambda *a, **k: True)
    import litellm.llms.github_copilot.responses.transformation as t
    monkeypatch.setattr(t, "_cached_get_model_info_helper", lambda **k: {}, raising=False)
    assert github_copilot_supports_responses_api("gpt-5.6-sol") is True
```

保留既有 `test_returns_none_when_mode_is_chat` / `test_mode_chat_overrides_endpoints_with_responses` / `test_user_override_via_register_model` 全部通过（不改这些用例；实现须继续满足）。

- [ ] **Step 2: 跑测试确认失败/回归基线**

Run: `pytest tests/test_litellm/llms/github_copilot/responses/test_github_copilot_responses_transformation.py -v`
Expected: `test_dynamic_responses_only_model` FAIL（旧逻辑对不在注册表的 `gpt-5.6-sol` 返回 False），既有 mode 用例仍 PASS

- [ ] **Step 3: 实现**

```python
def github_copilot_supports_responses_api(model: str) -> bool:
    from litellm.llms.github_copilot.model_capabilities import route_supports_responses
    from litellm.utils import _cached_get_model_info_helper

    try:
        info = _cached_get_model_info_helper(model=model, custom_llm_provider="github_copilot")
    except Exception:
        info = {}
    return route_supports_responses(model, model_info=info, api_base=_copilot_api_base_or_none())
```

`route_supports_responses` 已内含 `mode` 语义，故 `mode=chat`→False、`mode=responses`→True 保持。删除该文件里因此不再引用的死代码（如原 `_cached_get_model_info_helper` 直接判 mode 的逻辑并入 resolver）。**注意**若既有测试用 `@patch(...responses.transformation._cached_get_model_info_helper)`，保持该名可被 patch（本任务仍从该模块引用它）。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_litellm/llms/github_copilot/responses/test_github_copilot_responses_transformation.py -v`
Expected: PASS（新用例 + 既有 mode 回归全绿）

- [ ] **Step 5: 精确提交**

```bash
git add litellm/llms/github_copilot/responses/transformation.py tests/test_litellm/llms/github_copilot/responses/test_github_copilot_responses_transformation.py
git add -p litellm/utils.py   # 若有签名调整
git diff --cached
git commit -m "refactor(github_copilot): unify responses gate on dynamic resolver, keep mode contract"
```

---

### Task 9: 合并态路由测试 + 真实验证

**Files:**
- Test: `tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_anthropic_experimental_pass_through_messages_handler.py`（handler 级合并态）

**Interfaces:**
- Consumes: 前八个任务的全部产物

- [ ] **Step 1: 写 handler 级合并态测试（stub 三终点）**

```python
def test_merged_state_routing(monkeypatch):
    import litellm.llms.github_copilot.model_capabilities as mc

    def fake_msg(model, *, model_info, api_base):
        return "claude" in model

    def fake_resp(model, *, model_info, api_base):
        return model.startswith("gpt-5.")

    monkeypatch.setattr(mc, "route_supports_messages", fake_msg)
    monkeypatch.setattr(mc, "route_supports_responses", fake_resp)

    assert _should_route_to_responses_api("github_copilot", model="gpt-5.6-sol", model_info={}) is True
    assert _should_route_to_responses_api("github_copilot", model="gpt-4o", model_info={}) is False

    from litellm.utils import ProviderConfigManager
    from litellm.types.utils import LlmProviders
    from litellm.llms.github_copilot.messages.transformation import GithubCopilotAnthropicMessagesConfig
    monkeypatch.setattr("litellm.utils._cached_get_model_info_helper", lambda **k: {})
    cfg = ProviderConfigManager.get_provider_anthropic_messages_config(
        model="claude-opus-4.8", provider=LlmProviders.GITHUB_COPILOT
    )
    assert isinstance(cfg, GithubCopilotAnthropicMessagesConfig)
```

- [ ] **Step 2: 跑测试**

Run: `pytest tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_anthropic_experimental_pass_through_messages_handler.py -k merged_state -v`
Expected: PASS（回归锁）

- [ ] **Step 3: `make pre-commit` 全量把关**

Run: `make pre-commit`
Expected: 无 lint/type/format 报错；触及 budget 则 `make lint-budget-update` 并一并提交

- [ ] **Step 4: 真实验证（起本地 proxy + curl，非 pytest 截图）**

```bash
python litellm/proxy/proxy_cli.py --config /home/xp/.config/litellm/config.yaml --detailed_debug --reload --use_v2_migration_resolver 2>&1 | tee litellm.log
```

对 `/v1/messages` 各发一条 curl：claude 模型、responses-only 的 gpt（如 `gpt-5.6-sol`）、chat-only 模型，观察 `litellm.log` 中实际命中的上游端点（`/v1/messages` vs `/responses` vs `/chat/completions`）与成功响应，整理进 PR 的 Proof of Fix。同时确认冷启动首个请求不因能力取值阻塞（定时刷新在后台）。

- [ ] **Step 5: 提交**

```bash
git add tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_anthropic_experimental_pass_through_messages_handler.py
git commit -m "test(github_copilot): merged-state /v1/messages routing regression"
```

---

## Self-Review

- **Spec coverage**：动态取值主路（Task 2/3/5）、resolver 单一真相源 + mode override（Task 4）、messages 接线含 lru_cache 旁路（Task 6）、responses 兜底模型感知（Task 7）、responses config 统一保留 mode 契约（Task 8）、合并态 + 真实验证（Task 9）、无 hardcode 表（全程未引入）、`mode: anthropic` 新增（Task 4/6）——均覆盖。非目标（特性屏蔽、定价注册表）未纳入，符合 spec
- **Placeholder scan**：无 TBD/TODO；每个代码步骤给出完整代码与预期。Task 2 的 `/models` URL 有显式前置 PoC 步骤
- **Type consistency**：`CopilotEndpoint` / `EndpointPairs` / `resolve_endpoints` / `route_supports_messages` / `route_supports_responses` / `refresh_capabilities` / `get_cached_pairs` / `_should_route_to_responses_api(custom_llm_provider, model, model_info)` / `github_copilot_supports_responses_api(model)` 跨 Task 一致
- **Review 回填**：Task 6 假 red 已换成杀变异用例；lru_cache 旁路（Task 6）；mode 契约保留（Task 8）；handler 合并态（Task 9）；LIT 不可变结构（全程 tuple/frozenset/InMemoryCache）；fetch DI + 短 timeout + 非交互（Task 2/5）；精确 hunk staging（Task 5/6/8）；strip 只去精确前缀（Task 1）
