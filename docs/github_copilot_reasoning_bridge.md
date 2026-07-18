# GitHub Copilot gpt reasoning ↔ Anthropic thinking bridge

状态: **已实现并线上验证**（block 1）。日期: 2026-07-15
Spec: `docs/superpowers/specs/2026-07-14-gpt-reasoning-thinking-fidelity-design.md`（v3）
Plan: `docs/superpowers/plans/2026-07-14-gpt-reasoning-thinking-fidelity.md`
门禁结果: `docs/superpowers/plans/2026-07-14-gpt-reasoning-poc-results.md`
延后块 2/3/4: `docs/BACKLOG.md`

本文是这套机制的**活文档**（answers "what/where/how it works now"）。改动实现时同步更新此文与 spec/plan。

## 1. 解决什么

gpt-* 经 GitHub Copilot 走 OpenAI **Responses API**，reasoning 状态是不透明加密的 `encrypted_content`。Claude Code 只说 Anthropic Messages。litellm 的 direct Responses adapter 把 `/v1/messages` 桥到 Responses，但**原本把 reasoning 的 `encrypted_content` 丢了**——客户端只收到空壳 thinking，gpt 跨轮推理连续性丢失。

本机制让 gpt reasoning 对 Claude Code **像原生 Anthropic 扩展思考**: 推理可见（thinking 文本）、`encrypted_content` 跨轮保留、gpt 拿回自己的推理状态。**仅作用于 github_copilot provider**；openai/azure 等其它 Responses provider 不受影响。

## 2. 载体机制（carrier）

把 Responses reasoning item 的 `id + encrypted_content + summary` 序列化成一个命名空间化、版本化的私有 token:

```
ghc-rsn:v1:<urlsafe_b64(json({"id","ec","sp","om"}))>
```

塞进 Anthropic 原生、**客户端会逐字节存储回放**的字段:
- **A 方案（默认，`carrier=signature`）**: `thinking` 块的 `signature`（thinking 文本 = summary，可见）
- **B 方案（`carrier=redacted_thinking`）**: 独立 `redacted_thinking` 块的 `data`（+ 可选并排 thinking 摘要块）

**codec**: `litellm/llms/github_copilot/reasoning_carrier.py`
- `serialize_envelope` / `encode_carrier(env, carrier)` / `decode_carrier(block) -> DecodeResult`
- `decode_carrier` 是**严格边界**: Pydantic 校验 payload（拒非 str `sp`/`om`、缺字段、空 id/ec、多余键）、**严格 base64**（`validate=True`，篡改一字节 → `InvalidCarrier`）。返回 tagged union `DecodedCarrier | NotOurCarrier | InvalidCarrier | UnsupportedCarrierVersion`，永不抛。
- `strip_carrier_thinking_blocks(messages)`: 跨模型安全，见 §5。

## 3. 数据流（落点）

**响应侧（gpt → Claude Code）· 发载体**
- 流式 `responses_adapters/streaming_iterator.py`: reasoning item 的 `output_item.done` 处，`content_block_stop` 前发 `signature_delta`（A）或 redacted 块（B）。仅在 item_id 映射到真 reasoning 块时发（不误注入其它块）。
- 非流式 `responses_adapters/transformation.py::translate_response(reasoning_carrier=...)`: 用 `_reasoning_carrier_blocks` 发载体；无有效 id/ec 时回退纯 summary thinking（向后兼容）。

**请求侧（Claude Code → gpt）· 还原**
- `responses_adapters/transformation.py::translate_messages_to_responses_input`: 对历史 thinking/redacted_thinking 块 `decode_carrier`；`DecodedCarrier` → 重建**带原始 id** 的 Responses `reasoning` input item（gpt 拿回 encrypted_content）；非载体（真 claude thinking）→ 保留 litellm 默认的 output_text（不丢弃，见 §7 偏离）。

**summary 请求**: `translate_thinking_to_reasoning` + handler 的 `_resolve_reasoning_summary`：按配置设 `reasoning.summary`（默认 `auto` → 可见推理）。

## 4. 配置

deployment `model_info.github_copilot_reasoning: {carrier, summary}`（元数据，不进 provider 请求体）:
- `carrier`: `signature`（默认）| `redacted_thinking`
- `summary`: `off`（省略字段）| `auto`（默认）| `concise` | `detailed`
- unknown 值 → fail-loud（记 warning + 用默认）

resolver: `litellm/llms/github_copilot/reasoning_config.py::resolve_reasoning_config`。

**Kill switch**: 环境变量 `GHC_REASONING_DISABLE=1` 全局关闭（默认开）。`reasoning_bridge_enabled()`。

## 5. 跨模型安全

gpt-origin 的 `ghc-rsn` 载体若被回放进 **claude 请求**，claude 后端会当非法签名 400。`messages/handler.py` 在非 Responses（completion / claude）路径前调 `strip_carrier_thinking_blocks` 剥离载体——**零拷贝快路**（无载体则原样返回，正常 claude 请求零影响）。

## 6. 测试

**单元/集成（CI，`tests/test_litellm/`）**: 60 个特性测试
- `github_copilot/test_reasoning_carrier.py`（codec、严格边界、篡改注入、unicode/超长、property-based、跨模型 strip）
- `github_copilot/test_reasoning_config.py`（resolver、kill switch、summary wire）
- `responses_adapters/test_reasoning_fidelity.py`（流式/非流式发载体、请求侧重建、**全链路往返回归**、篡改不重建、backend-reach、provider 门控）

**按需 e2e（`tests/e2e/github_copilot_reasoning/`，不进 CI）**:
- `anthropic_sdk` / `billed`（`LITELLM_RUN_BILLED=1`）: 真 anthropic SDK + 真后端差分（有效接受 / 篡改拒绝）+ 连续性
- `claude_cli`（`LITELLM_RUN_CLAUDE_CLI=1`）: 驱动真 claude CLI 验 R1（Claude Code 存载体）

## 7. 与 spec 的偏离（记录）

- §4.3: 非载体 thinking 当 gpt 目标时**保留为 output_text**（非丢弃）——避免回退 litellm 既有契约（`test_assistant_thinking_block_becomes_output_text`），且 gpt 容忍、跨模型安全另由 §5 strip 保证。
- §4.4: summary 默认 `auto`（可见），但仅 github_copilot；非 copilot no-op。

## 8. block 2 协议信封状态

2026-07-17 已修 direct Responses 双 `message_start`：fallback 已发 start 后，上游 `response.created` 为幂等 no-op。strict Anthropic SDK live 验证通过：显式 `summary=detailed` 能累积 thinking_delta，最终 carrier 可解码；默认 `summary=auto` 是 best-effort，不保证每次返回非空摘要。同期 direct Responses 与 Chat wrapper 均已补自然/异常 EOF 完整信封，真实 Claude CLI carrier 存储、Bash 工具配对和跨轮 replay/tamper 门禁均通过。其余 block 2/3/4 结果见 `docs/superpowers/specs/2026-07-17-anthropic-protocol-tool-stream-fidelity-design.md`。
