# GPT reasoning ↔ Anthropic thinking 全保真转换 · 实施计划（block 1）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 gpt-* 模型（经 copilot 走 Responses API）的 reasoning，在 Anthropic `/v1/messages` 桥接下对 Claude Code 全保真——summary 以带私有载体的合法 thinking 块可见、`encrypted_content` 跨轮精确回放、载体 A/B 可配置。

**Architecture:** 在 fork 的 direct Responses adapter（`litellm/llms/anthropic/experimental_pass_through/responses_adapters/`）响应侧把 `ResponseReasoningItem` 编码进 Anthropic thinking/redacted_thinking 载体、请求侧解码还原成带原始 id 的 Responses reasoning item。载体编解码抽成独立纯模块。跨模型时私有载体绝不发往真 Claude 后端。

**Tech Stack:** Python 3、litellm（本 fork）、pytest/unittest（`tests/test_litellm/` 镜像树）、OpenAI SDK 的 `ResponseReasoningItem`/`ResponsesAPIResponse` 类型、Anthropic Messages SSE。

**冻结依据 spec:** `docs/superpowers/specs/2026-07-14-gpt-reasoning-thinking-fidelity-design.md`（v3，两轮对抗评审收敛，0 blocker）。**本计划各 task 的需求隐含包含下面 Global Constraints。**

## Global Constraints

- Python max line 120（非 88）。
- fork 主代码（非 hookpkg）编码约束: 强类型、无 `Any`/裸 `dict`；tagged union + `match`；失败以值建模、不吞错（一个 `raise_public` 式函数把错误 union 映射到公共异常）；无变异（`tuple`/`frozen dataclass(slots=True)`/comprehension，禁 `list.append` 累积——会触发 LIT001/LIT002）；composition over inheritance；early return。
- 载体命名空间/版本: `ghc-rsn:v1:`（NS + 版本 + base64(json)），带必填字段校验。
- 私有载体不变量: `ghc-rsn` 载体**绝不**抵达 Anthropic/Claude 后端（跨模型剥离，spec §4.6/§4.7）。
- 配置位置: deployment `model_info.github_copilot_reasoning: {carrier, summary}`；per-request 覆盖不在 block 1。默认 `carrier="signature"`、`summary="auto"`；unknown 值 → `InvalidConfig` fail loud。
- reasoning_summary wire 映射: `off -> 省略字段`；`auto|concise|detailed` 原样。优先级 deployment > global `reasoning_auto_summary`(→detailed) > 默认 auto。
- 测试: 有意义、能在代码被变异（丢 encrypted_content/丢 id/漏 summary/错载体/提前 stop）时变红；镜像树放 `tests/test_litellm/llms/anthropic/experimental_pass_through/responses_adapters/`，provider-specific 配置/affinity 测试留 `tests/test_litellm/llms/github_copilot/`。
- 每完成一个语义单元即 commit（conventional commits）；commit 前跑相关测试 + `make pre-commit`。

## 阶段总览与门禁

- **Phase 1 — 载体 codec（纯模块，TDD，无 live 依赖）**: 基础，先做、风险最低、PoC 也要用。
- **Phase 2 — 可行性 PoC（门禁，人机协同）**: 最小响应侧发射 + 真实 Claude Code gpt 会话验证 oracle 表。**GATE**: 不过则回到载体/编码选择，不进 Phase 3+。
- **Phase 3 — 响应侧完整（非流式 + 流式）+ 配置 plumbing**
- **Phase 4 — 请求侧（decode → reasoning item 还原 / NotOurCarrier 丢弃 / 跨模型剥离）**
- **Phase 5 — reasoning_summary 请求 + 配置 resolver 收尾**
- **Phase 6 — 跨模型矩阵 + 集成/e2e 测试收口**

> **Phase 3-6 的 step 级 TDD 代码在 Phase 2 门禁通过后、进入该 phase 时再补齐**（spike-first）。原因: 它们押在「Claude Code 原样回放载体」这一未证事实（spec R1）与流式 SSE 的确切 litellm 内部类型上；PoC 未过就写死数百行 step 代码，正是 PoC 要防的「plan the wrong thing」。本计划为这些 phase 冻结**文件、接口签名、交付物、测试意图**（planner 合同），足以在门禁后无歧义展开。Phase 1/2 为完整 step 级。

## 文件结构

- **Create** `litellm/llms/github_copilot/reasoning_carrier.py` — 载体 codec: `ReasoningReplayEnvelope` + `encode_carrier`/`decode_carrier` + tagged union 结果。纯函数、无 litellm 依赖。
- **Create** `litellm/llms/github_copilot/reasoning_config.py` — resolved config（frozen）+ 从 `model_info` 解析的 resolver。
- **Modify** `litellm/llms/anthropic/experimental_pass_through/responses_adapters/transformation.py` — 响应侧非流式 `~410-420`（reasoning item → 载体块）、请求侧 `~134-174`（载体块 → reasoning item / NotOurCarrier 丢弃）、请求组装 `~250-284`（summary）。
- **Modify** `litellm/llms/anthropic/experimental_pass_through/responses_adapters/streaming_iterator.py` — 流式 `~67-276`（output_item.done 发 signature_delta / redacted 块）。
- **Modify** `litellm/llms/anthropic/experimental_pass_through/responses_adapters/handler.py` — `~22-113`（把 resolved config plumb 进 request/response/stream 三处）。
- **Test（镜像）** `tests/test_litellm/llms/github_copilot/test_reasoning_carrier.py`、`test_reasoning_config.py`；`tests/test_litellm/llms/anthropic/experimental_pass_through/responses_adapters/test_reasoning_fidelity.py`（集成）。

---

## Phase 1 — 载体 codec（纯模块，TDD）

### Task 1: `ReasoningReplayEnvelope` + tagged union 结果类型

**Files:**
- Create: `litellm/llms/github_copilot/reasoning_carrier.py`
- Test: `tests/test_litellm/llms/github_copilot/test_reasoning_carrier.py`

**Interfaces:**
- Produces:
  - `ReasoningReplayEnvelope(reasoning_item_id: str, encrypted_content: str, summary_parts: tuple[str, ...], origin_model: str | None = None, version: int = 1)` — frozen, slots。
  - `DecodeResult = DecodedCarrier | NotOurCarrier | InvalidCarrier | UnsupportedCarrierVersion`（frozen dataclasses；`DecodedCarrier.envelope`、`InvalidCarrier.reason: str`、`UnsupportedCarrierVersion.version: int`）。

- [ ] **Step 1: 写失败测试**（构造 envelope、断言字段与不可变）

```python
# tests/test_litellm/llms/github_copilot/test_reasoning_carrier.py
from litellm.llms.github_copilot.reasoning_carrier import (
    ReasoningReplayEnvelope, DecodedCarrier, NotOurCarrier, InvalidCarrier, UnsupportedCarrierVersion,
)


def test_envelope_is_frozen_and_holds_fields():
    env = ReasoningReplayEnvelope(
        reasoning_item_id="rs_abc",
        encrypted_content="ENC==",
        summary_parts=("step one", "step two"),
        origin_model="gpt-5.6-sol",
    )
    assert env.reasoning_item_id == "rs_abc"
    assert env.encrypted_content == "ENC=="
    assert env.summary_parts == ("step one", "step two")
    assert env.version == 1
    import dataclasses
    try:
        env.encrypted_content = "x"  # type: ignore[misc]
        assert False, "should be frozen"
    except dataclasses.FrozenInstanceError:
        pass
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/xp/refs/ai-agents/litellm && python -m pytest tests/test_litellm/llms/github_copilot/test_reasoning_carrier.py::test_envelope_is_frozen_and_holds_fields -q`
Expected: FAIL（ImportError / module 不存在）

- [ ] **Step 3: 写最小实现**

```python
# litellm/llms/github_copilot/reasoning_carrier.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Union

_NS = "ghc-rsn"
_VERSION = 1


@dataclass(frozen=True, slots=True)
class ReasoningReplayEnvelope:
    reasoning_item_id: str
    encrypted_content: str
    summary_parts: tuple[str, ...]
    origin_model: Union[str, None] = None
    version: int = _VERSION


@dataclass(frozen=True, slots=True)
class DecodedCarrier:
    envelope: ReasoningReplayEnvelope


@dataclass(frozen=True, slots=True)
class NotOurCarrier:
    pass


@dataclass(frozen=True, slots=True)
class InvalidCarrier:
    reason: str


@dataclass(frozen=True, slots=True)
class UnsupportedCarrierVersion:
    version: int


DecodeResult = Union[DecodedCarrier, NotOurCarrier, InvalidCarrier, UnsupportedCarrierVersion]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_litellm/llms/github_copilot/test_reasoning_carrier.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add litellm/llms/github_copilot/reasoning_carrier.py tests/test_litellm/llms/github_copilot/test_reasoning_carrier.py
git commit -m "feat(github_copilot): reasoning replay envelope + decode result types"
```

### Task 2: `encode_carrier`（A/B 两载体，含 B 双块）

**Files:**
- Modify: `litellm/llms/github_copilot/reasoning_carrier.py`
- Test: `tests/test_litellm/llms/github_copilot/test_reasoning_carrier.py`

**Interfaces:**
- Produces: `encode_carrier(env: ReasoningReplayEnvelope, carrier: Literal["signature","redacted_thinking"]) -> tuple[dict, ...]`
  - `"signature"` → `({"type":"thinking","thinking":<summary>,"signature":<token>},)`（单块）。
  - `"redacted_thinking"` → 有 summary: `({"type":"thinking","thinking":<summary>}, {"type":"redacted_thinking","data":<token>})`；无 summary: `({"type":"redacted_thinking","data":<token>},)`。
  - `<summary>` = `" ".join(env.summary_parts).strip()`；`<token>` = `ghc-rsn:v1:<b64(json)>`。

- [ ] **Step 1: 写失败测试**（A 单块带 signature；B 有/无 summary 的块数与顺序；token 前缀）

```python
from litellm.llms.github_copilot.reasoning_carrier import ReasoningReplayEnvelope, encode_carrier

def _env(summary=("s1","s2")):
    return ReasoningReplayEnvelope("rs_1", "ENC==", summary, "gpt-5.6-sol")

def test_encode_signature_single_block():
    blocks = encode_carrier(_env(), "signature")
    assert len(blocks) == 1
    b = blocks[0]
    assert b["type"] == "thinking"
    assert b["thinking"] == "s1 s2"
    assert b["signature"].startswith("ghc-rsn:v1:")

def test_encode_redacted_with_summary_two_blocks_in_order():
    blocks = encode_carrier(_env(), "redacted_thinking")
    assert [b["type"] for b in blocks] == ["thinking", "redacted_thinking"]
    assert blocks[0]["thinking"] == "s1 s2"
    assert blocks[1]["data"].startswith("ghc-rsn:v1:")
    assert "signature" not in blocks[1]

def test_encode_redacted_without_summary_single_redacted_block():
    blocks = encode_carrier(_env(summary=()), "redacted_thinking")
    assert [b["type"] for b in blocks] == ["redacted_thinking"]
    assert blocks[0]["data"].startswith("ghc-rsn:v1:")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_litellm/llms/github_copilot/test_reasoning_carrier.py -q -k encode`
Expected: FAIL（`encode_carrier` 未定义）

- [ ] **Step 3: 写最小实现**（追加到模块；注意无 `list.append` 累积）

```python
import base64
import json
from typing import Literal

_Carrier = Literal["signature", "redacted_thinking"]


def _serialize(env: ReasoningReplayEnvelope) -> str:
    payload = {
        "id": env.reasoning_item_id,
        "ec": env.encrypted_content,
        "sp": list(env.summary_parts),
        "om": env.origin_model,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).decode("ascii")
    return f"{_NS}:v{env.version}:{b64}"


def _summary_text(env: ReasoningReplayEnvelope) -> str:
    return " ".join(env.summary_parts).strip()


def encode_carrier(env: ReasoningReplayEnvelope, carrier: _Carrier) -> tuple[dict, ...]:
    token = _serialize(env)
    summary = _summary_text(env)
    if carrier == "signature":
        return ({"type": "thinking", "thinking": summary, "signature": token},)
    summary_block = ({"type": "thinking", "thinking": summary},) if summary else ()
    return (*summary_block, {"type": "redacted_thinking", "data": token})
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_litellm/llms/github_copilot/test_reasoning_carrier.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add litellm/llms/github_copilot/reasoning_carrier.py tests/test_litellm/llms/github_copilot/test_reasoning_carrier.py
git commit -m "feat(github_copilot): encode reasoning carrier (signature + redacted_thinking)"
```

### Task 3: `decode_carrier`（tagged union，往返 + 不误伤 claude + 损坏分类）

**Files:**
- Modify: `litellm/llms/github_copilot/reasoning_carrier.py`
- Test: `tests/test_litellm/llms/github_copilot/test_reasoning_carrier.py`

**Interfaces:**
- Produces: `decode_carrier(block: dict) -> DecodeResult`。读 `thinking.signature` 或 `redacted_thinking.data`；非 `ghc-rsn:` 起头 → `NotOurCarrier`；版本不符 → `UnsupportedCarrierVersion`；结构/base64/json/必填字段坏 → `InvalidCarrier(reason)`；成功 → `DecodedCarrier(envelope)`（`encode`→`decode` 往返字段逐一相等）。

- [ ] **Step 1: 写失败测试**（往返一致；真 claude 签名→NotOurCarrier；随机串→NotOurCarrier；伪前缀+坏 b64→InvalidCarrier；未知版本→UnsupportedCarrierVersion；缺 id→InvalidCarrier）

```python
from litellm.llms.github_copilot.reasoning_carrier import (
    ReasoningReplayEnvelope, encode_carrier, decode_carrier,
    DecodedCarrier, NotOurCarrier, InvalidCarrier, UnsupportedCarrierVersion,
)

def test_roundtrip_signature():
    env = ReasoningReplayEnvelope("rs_1", "ENC==", ("s1", "s2"), "gpt-5.6-sol")
    (block,) = encode_carrier(env, "signature")
    res = decode_carrier(block)
    assert isinstance(res, DecodedCarrier)
    assert res.envelope == env

def test_roundtrip_redacted():
    env = ReasoningReplayEnvelope("rs_2", "ENC2", (), None)
    blocks = encode_carrier(env, "redacted_thinking")
    res = decode_carrier(blocks[-1])  # the redacted block carries it
    assert isinstance(res, DecodedCarrier)
    assert res.envelope == env

def test_real_claude_signature_is_not_our_carrier():
    block = {"type": "thinking", "thinking": "x", "signature": "EqoBCkYIB..."}  # 真 claude 风格
    assert isinstance(decode_carrier(block), NotOurCarrier)

def test_random_string_not_our_carrier():
    assert isinstance(decode_carrier({"type": "redacted_thinking", "data": "just-random"}), NotOurCarrier)

def test_corrupt_base64_is_invalid():
    block = {"type": "thinking", "thinking": "", "signature": "ghc-rsn:v1:!!!notb64!!!"}
    assert isinstance(decode_carrier(block), InvalidCarrier)

def test_unknown_version():
    block = {"type": "thinking", "thinking": "", "signature": "ghc-rsn:v9:YWJj"}
    assert isinstance(decode_carrier(block), UnsupportedCarrierVersion)

def test_missing_required_field_is_invalid():
    import base64, json
    b64 = base64.urlsafe_b64encode(json.dumps({"ec": "E", "sp": []}).encode()).decode()  # 缺 id
    block = {"type": "thinking", "thinking": "", "signature": f"ghc-rsn:v1:{b64}"}
    assert isinstance(decode_carrier(block), InvalidCarrier)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_litellm/llms/github_copilot/test_reasoning_carrier.py -q -k decode or roundtrip or claude or version or invalid`
Expected: FAIL（`decode_carrier` 未定义）

- [ ] **Step 3: 写最小实现**（追加；用 tagged 返回、不 raise 到外层）

```python
def _carrier_field(block: dict) -> Union[str, None]:
    t = block.get("type")
    if t == "thinking":
        sig = block.get("signature")
        return sig if isinstance(sig, str) else None
    if t == "redacted_thinking":
        data = block.get("data")
        return data if isinstance(data, str) else None
    return None


def _req_str(v: object) -> str:
    if not isinstance(v, str):
        raise ValueError("expected str")
    return v


def decode_carrier(block: dict) -> DecodeResult:
    field = _carrier_field(block)
    if field is None or not field.startswith(f"{_NS}:"):
        return NotOurCarrier()
    parts = field.split(":", 2)
    if len(parts) != 3 or not parts[1].startswith("v"):
        return InvalidCarrier("malformed-structure")
    try:
        version = int(parts[1][1:])
    except ValueError:
        return InvalidCarrier("malformed-version")
    if version != _VERSION:
        return UnsupportedCarrierVersion(version)
    try:
        raw = base64.urlsafe_b64decode(parts[2].encode("ascii"))
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return InvalidCarrier("malformed-base64-or-json")
    if not isinstance(payload, dict):
        return InvalidCarrier("payload-not-object")
    try:
        env = ReasoningReplayEnvelope(
            reasoning_item_id=_req_str(payload["id"]),
            encrypted_content=_req_str(payload["ec"]),
            summary_parts=tuple(payload.get("sp") or ()),
            origin_model=payload.get("om"),
            version=version,
        )
    except (KeyError, ValueError, TypeError) as e:
        return InvalidCarrier(f"missing-or-invalid-field:{type(e).__name__}")
    return DecodedCarrier(env)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_litellm/llms/github_copilot/test_reasoning_carrier.py -q`
Expected: PASS（全部）

- [ ] **Step 5: 变异验证（守测有效性）**

临时把 `_serialize` 里 `"id": env.reasoning_item_id` 改成 `"id": "WRONG"`，跑 `test_roundtrip_signature` 应变红；恢复。记录一句「往返测试对 id 变异敏感」。

- [ ] **Step 6: Commit**

```bash
git add litellm/llms/github_copilot/reasoning_carrier.py tests/test_litellm/llms/github_copilot/test_reasoning_carrier.py
git commit -m "feat(github_copilot): decode reasoning carrier as tagged union (roundtrip, claude-safe, corruption-classified)"
```

### Task 4: property-based 不误伤 claude + `make pre-commit`

**Files:**
- Modify: `tests/test_litellm/llms/github_copilot/test_reasoning_carrier.py`

- [ ] **Step 1: 写 property 测试**（对随机字符串、合法 b64、伪 `ghc-rsn` 前缀但坏结构，`decode` 只返回 `NotOurCarrier`/`InvalidCarrier`/`UnsupportedCarrierVersion`，绝不 `DecodedCarrier`，也绝不抛异常）

```python
import base64, string, random as _r
from litellm.llms.github_copilot.reasoning_carrier import decode_carrier, DecodedCarrier

def test_never_decodes_non_envelope_and_never_raises():
    seeds = ["", "sig", "EqoBabc==", base64.urlsafe_b64encode(b"{}").decode(),
             "ghc-rsn", "ghc-rsn:", "ghc-rsn:v1", "ghc-rsn:v1:", "ghc-rsn:vX:YWJj",
             "ghc-rsn:v1:" + base64.urlsafe_b64encode(b"[1,2,3]").decode()]
    corpus = seeds + ["".join(_r.choice(string.printable) for _ in range(_r.randint(0, 40))) for _ in range(200)]
    for s in corpus:
        for block in ({"type": "thinking", "thinking": "", "signature": s},
                      {"type": "redacted_thinking", "data": s}):
            res = decode_carrier(block)  # 不得抛
            assert not isinstance(res, DecodedCarrier) or s.startswith("ghc-rsn:v1:")
```

- [ ] **Step 2: 跑 + 确认通过**

Run: `python -m pytest tests/test_litellm/llms/github_copilot/test_reasoning_carrier.py -q`
Expected: PASS

- [ ] **Step 3: pre-commit**

Run: `git add -A && make pre-commit`（修掉 lint/type；若动 budget 文件按 CLAUDE.md 跑 `make lint-budget-update`）
Expected: 通过

- [ ] **Step 4: Commit**

```bash
git add tests/test_litellm/llms/github_copilot/test_reasoning_carrier.py
git commit -m "test(github_copilot): property-based carrier decode safety (never mis-decode, never raise)"
```

---

## Phase 2 — 可行性 PoC（门禁，人机协同）

> 目的: 用**真实 Claude Code gpt 会话**证伪/证实 spec R1——Claude Code 是否原样存储并回放载体（A 与 B），后端是否接受还原的 reasoning item。**未过则停，回到载体/编码决策，不进 Phase 3。**

### Task 5: 最小响应侧发射（非流式，插桩，配置 flag 硬开）

**Files:**
- Modify: `litellm/llms/anthropic/experimental_pass_through/responses_adapters/transformation.py:~410-420`（非流式 reasoning item 分支）
- Test: `tests/test_litellm/llms/anthropic/experimental_pass_through/responses_adapters/test_reasoning_fidelity.py`

**Interfaces:**
- Consumes: `encode_carrier`（Task 2）、`ReasoningReplayEnvelope`（Task 1）。
- Produces: 非流式 `translate_response` 对每个 `ResponseReasoningItem`，当环境 flag `GHC_REASONING_POC=1` 时，用 `item.id`/`item.encrypted_content`/`item.summary` 构 envelope、`encode_carrier(env,"signature")` 产块替换现有空壳；flag 未开则保持现状（零风险）。

- [ ] **Step 1: 写失败集成测试**（构造带 `ResponseReasoningItem(id, encrypted_content, summary=[...])` 的 `ResponsesAPIResponse`，flag 开时 `translate_response` 产出的 Anthropic content 里出现 `thinking{signature: ghc-rsn:v1:...}`，且 `decode_carrier` 还原的 `encrypted_content`/`id`/`summary_parts` 与输入精确一致）

```python
# 测试骨架（实现时按 responses_adapters 的实际构造/入口补全 import 与 fixture）
import os
from litellm.llms.github_copilot.reasoning_carrier import decode_carrier, DecodedCarrier
# from ... import LiteLLMAnthropicToResponsesAPIAdapter, 构造 ResponsesAPIResponse 的 helper

def test_poc_nonstream_reasoning_item_becomes_carrier(monkeypatch):
    monkeypatch.setenv("GHC_REASONING_POC", "1")
    resp = _make_responses_api_response_with_reasoning(
        item_id="rs_poc", encrypted="ENC-POC==", summary_texts=["think a", "think b"])
    anthropic = _adapter().translate_response(resp)
    thinking_blocks = [b for b in anthropic["content"] if b.get("type") == "thinking" and b.get("signature")]
    assert thinking_blocks, "expected a carrier thinking block"
    res = decode_carrier(thinking_blocks[0])
    assert isinstance(res, DecodedCarrier)
    assert res.envelope.reasoning_item_id == "rs_poc"
    assert res.envelope.encrypted_content == "ENC-POC=="
    assert res.envelope.summary_parts == ("think a", "think b")
```

- [ ] **Step 2: 跑确认失败** → `python -m pytest .../test_reasoning_fidelity.py -q`（FAIL）
- [ ] **Step 3: 实现**（读 `responses_adapters/transformation.py:392-491`，在 `ResponseReasoningItem` 分支 flag-gated 接入 `encode_carrier`；summary 从 `item.summary[*].text` 组 `summary_parts`；无 `Any`、tuple 构造）
- [ ] **Step 4: 跑确认通过**
- [ ] **Step 5: Commit** `feat(github_copilot): [POC] emit reasoning carrier on non-stream responses behind GHC_REASONING_POC`

### Task 6: 人机协同 live 验证（oracle 表，A 与 B 各一）

> 无自动化断言可替代——需人在 Claude Code 里真实驱动 gpt。执行者按下表逐格记录，产出一份 `docs/superpowers/plans/2026-07-14-gpt-reasoning-poc-results.md`。

- [ ] **Step 1: 起代理**（`~/.claude/litellm/start-ghc-api.sh` 或确认在跑）、`GHC_REASONING_POC=1`，开 `stream_fix.probe_only` 抓 wire。
- [ ] **Step 2: A 载体** — 在 Claude Code 用 model=gpt 跑一轮触发推理的对话，逐格填 oracle:

  | # | oracle | 期望 | 实测 |
  |---|---|---|---|
  | 1 | 抓 direct wrapper 收到的原始 `output_item.done` 的 `id`/`encrypted_content` | 记录到值 | |
  | 2 | Claude Code transcript 里载体 `signature` **逐字节原样** | 存在且一致 | |
  | 3 | 触发下一轮，抓代理构造的 Responses input 的 reasoning item | `id`/`encrypted_content`/`summary` 与轮1一致 | |
  | 4 | 后端响应码 | 200 | |
  | 5 | 重启 Claude Code 再触发下一轮 | 载体从 transcript 恢复、仍 200 | |
  | 6 | 切 model=opus 再发一轮 | 不把 `ghc-rsn` 伪签名发往 claude 后端（§4.6） | |
  | 7 | 手改 transcript 里载体一字节再回放 | 明确失败/被丢，不静默错乱 | |
  | 8 | 完整 SSE 事件顺序 | 记录；确认双 `message_start` 是否致客户端重置/拒绝（spec §7） | |
  | 9 | encrypted_content 大小 p50/p95/max | 记录（R2） | |

- [ ] **Step 3: B 载体** — 临时把 Task 5 的 `encode_carrier(...,"redacted_thinking")`，重复整表。
- [ ] **Step 4: 门禁判定**（写进 results 文档）:
  - **PASS**（oracle 2/3/4/5 至少一种载体全绿，6 不违规）→ 记录默认载体（哪种全绿选哪个；都绿默认 A），进 Phase 3。
  - **FAIL**（载体不回放/被篡改/后端拒绝）→ 停。按失败点决策: 载体被 Claude Code 规范化 → 调编码（如纯 ASCII/更短）；signature 被校验 → 切 B 默认；两者皆败 → 上报，重议 spec（可能需 block 2 双 start 先修，或换机制）。
  - oracle 8 若显示双 `message_start` 致客户端重置 → 把 spec §7 该项升为 block 1 前置，插入 Phase 3 之前。
- [ ] **Step 5: Commit** results 文档 + 撤除或保留 POC flag 决定。

---

## Phase 3 — 响应侧完整（门禁后 step 级展开）

**交付物:** 非流式 + 流式响应侧按选定/可配置载体产出，覆盖 spec §4.2、§4.1 的 A/B SSE 序列。

**Files:** Modify `responses_adapters/transformation.py:~392-491`（非流式，去掉 POC flag、接 config）；`responses_adapters/streaming_iterator.py:~67-276`（流式）；`handler.py:~22-113`（plumb config）。

**Interfaces:**
- Consumes: `encode_carrier`、`ResolvedReasoningConfig`（Phase 5 Task）、`ResponseReasoningItem`。
- Produces: 非流式 content 含载体块；流式按 §4.2 冻结序列——**A**: `content_block_start(thinking)@i`→summary delta*→`output_item.done` 发 `signature_delta`→`stop@i`；**B**: `thinking@i`(有 summary 时) start/delta/stop，再 `redacted_thinking@i+1` start(data)/stop，index 顺延。

**测试意图:** 集成测试喂构造的非流式 `ResponsesAPIResponse` 与流式 raw Responses SSE（`output_item.added`→`reasoning_summary_text.delta*`→`output_item.done(encrypted_content)`），断言输出 Anthropic 块/SSE 序列 + `decode_carrier` 往返；mutation oracle: 丢 encrypted_content、B 把 redacted 塞进 thinking 块或复用 index、提前 stop → 变红。

**step 级 TDD 在进入本 phase 时按上述接口与 Phase 2 抓到的真实 SSE 形态补齐。**

## Phase 4 — 请求侧（门禁后 step 级展开）

**交付物:** 历史 assistant thinking/redacted_thinking 块 → `decode_carrier` → 重建带原始 id 的 Responses reasoning item；`NotOurCarrier`（claude-origin→gpt）**丢弃 + 计数**；gpt-origin→claude 路径**剥离** `ghc-rsn` 载体（spec §4.6）。载体块只产 reasoning item、**不产 `output_text`**（修 `~161-164` 现状）。

**Files:** Modify `responses_adapters/transformation.py:~134-174`；跨模型剥离落在原生 Anthropic 路由入口前（`messages/handler.py` 分流处，实现时定位）。

**Interfaces:**
- Consumes: `decode_carrier` → `match DecodeResult`。
- Produces: Responses input items 顺序保持；reasoning item 含 `id`/`encrypted_content`/`summary`。

**测试意图:** 「带载体的 Anthropic 请求 → 正确 Responses reasoning item（id 精确）」；`NotOurCarrier` 三样例（有文本/空/纯 redacted thinking）全丢弃并计数；gpt-origin 载体在 target=claude 时被剥离、不出现在发往 claude 的 latest assistant message；mutation: 删 id、错还原顺序、漏剥离 → 变红。

## Phase 5 — summary 请求 + 配置 resolver（门禁后 step 级展开）

**交付物:** `reasoning_config.py`（`ResolvedReasoningConfig(carrier, summary)` frozen + `resolve_from_model_info(model_info) -> ResolvedReasoningConfig | InvalidConfig`）；请求组装按 config 设 `reasoning.summary`（`off→省略`）；config plumb 进 request/response/stream 三处（替 module-global `_ADAPTER`）。

**Files:** Create `reasoning_config.py` + test；Modify `responses_adapters/transformation.py:~250-284`、`handler.py:~22-113`。

**Interfaces:**
- Produces: `resolve_from_model_info`；优先级 deployment > global `reasoning_auto_summary`(→detailed) > 默认 auto；unknown→`InvalidConfig` fail loud；非 copilot→默认 no-op。
- Consumes: deployment `model_info.github_copilot_reasoning`。

**测试意图:** resolver 各优先级/默认/unknown-fail-loud/非 copilot 忽略；`off→省略 summary 字段`、其余原样进 wire；config 三处到达（用注入断言，不 monkeypatch）；防泄漏: config 不出现在发往 copilot 的请求体。

## Phase 6 — 跨模型矩阵 + 集成/e2e 收口（门禁后 step 级展开）

**交付物:** spec §4.6 四象限 × A/B 集成测试全绿；`_should_route_to_responses_api` 路由确认测试；e2e live A/B 各两轮（沿用探针）；illformed-fix.md / spec 状态更新为已实现。

**测试意图:** 端到端单元链（路由→raw SSE/Responses input）；跨模型矩阵每格断言；e2e 连续性 + 协议合规（无新增畸形，双 start 已按 §7 结论处置）。

---

## Self-Review（planner 自查）

- **Spec 覆盖**: §3 验收 1↔Phase2/3、验收 2↔Task3+Phase3/4、验收 3↔Phase4/6+PoC、验收 4↔Phase5、验收 5(跨模型)↔Phase4/6、验收 6(tagged union)↔Task3、验收 7(mutation)↔各 phase mutation oracle。§4.1 codec↔Phase1；§4.2 响应↔Phase3；§4.3 请求↔Phase4；§4.4 summary↔Phase5；§4.5 config↔Phase5；§4.6 矩阵↔Phase4/6；§5 PoC↔Phase2；§7 双 start↔Phase2-oracle8 + 门禁分支。无未覆盖 spec 项。
- **占位扫描**: Phase 1/2 无占位、含真实代码与命令。Phase 3-6 为**有意 gated 的 task/接口级合同**（非 TODO 占位），已注明门禁后展开的依据与接口签名。
- **类型一致**: `ReasoningReplayEnvelope`/`encode_carrier`/`decode_carrier`/`DecodeResult`/`ResolvedReasoningConfig` 跨 task 命名一致；`carrier` 取值 `"signature"|"redacted_thinking"` 全程一致；`summary` 取值 `off|auto|concise|detailed` 全程一致。
