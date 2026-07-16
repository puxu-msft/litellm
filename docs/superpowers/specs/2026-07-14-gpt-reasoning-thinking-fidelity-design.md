# GPT reasoning ↔ Anthropic thinking 全保真转换 · 设计/规格

状态: **已实现并线上验证**（2026-07-15）。当前实现的活文档见 `docs/github_copilot_reasoning_bridge.md`（含与本 spec 的偏离记录 §7）。两轮 GPT 对抗评审（轮1 的 2 blocker + 8 major + 3 minor、轮2 的 4 major + 2 minor）已全部吸收冻结。
日期: 2026-07-14
范围: Anthropic ↔ GPT 全保真格式转换的 **block 1**（reasoning↔thinking）。block 2/3/4 见 `docs/BACKLOG.md`。
适用: 私有 fork 的 `github_copilot` provider，gpt-* 模型经 `/v1/messages` 路由到 Responses API。

> v2 变更提要（评审驱动，证据见 §10）: ①主技术落点纠正为 **direct Responses adapter（`responses_adapters/`）**，旧 chat bridge 降为兼容路径; ②carrier 从裸 `encrypted_content` 升级为**强类型 replay envelope**（含原始 reasoning item `id` + `encrypted_content` + `summary`）; ③新增 gpt-origin→Claude 的**跨模型 carrier 剥离合同**; ④配置解析/传播合同显式化; ⑤`decode` 返回 tagged union; ⑥phase 0 PoC 补 oracle; ⑦测试主 seam 移到 `responses_adapters/`; ⑧双 `message_start` 已证实，纳入 phase 0 归因。

## 1. 背景与问题（why）

Claude Code 只说 Anthropic Messages 协议。gpt-5.x 是 reasoning 模型、经 copilot 走 Responses API，其推理状态是 `encrypted_content`（不透明、加密）。gpt-* 的 `/v1/messages` 请求经 `_should_route_to_responses_api` 判定后，走 **direct Responses adapter**（`LiteLLMMessagesToResponsesAPIHandler`，落在 `litellm/llms/anthropic/experimental_pass_through/responses_adapters/`），而非 chat completion bridge。

2026-07-14 live 探针（原始数据 `~/.config/litellm/probe-logs/live-probe-t1.sse`/`t2.json`；方法与结论见 `~/.config/litellm/docs/illformed-fix.md`「thinking block 处置的 provider 差异」）+ GPT 评审代码核实确证:

- gpt 回来的 thinking 块是**空壳**: `{"type":"thinking","thinking":""}`，无 `signature`/无 `encrypted_content`。
- 丢失点在 direct Responses adapter 非流式 [responses_adapters/transformation.py:410-420](litellm/llms/anthropic/experimental_pass_through/responses_adapters/transformation.py#L410-L420): 对每个 `ResponseReasoningItem` 只遍历 `item.summary` 输出 `thinking(signature=None)`，**完全不读 `item.id`/`item.encrypted_content`**。流式路径 [streaming_iterator.py](litellm/llms/anthropic/experimental_pass_through/responses_adapters/streaming_iterator.py) 在 `output_item.added` 开 thinking 块、发 summary delta、`output_item.done` 直接 stop，同样不读 `encrypted_content`。
- 空壳无文本的另一因: 当前默认**不请求 reasoning summary**（仅 global `litellm.reasoning_auto_summary`/env 打开时注入 `summary="detailed"`），故 `item.summary` 常为空 → thinking 无文本。
- litellm 自带的 encrypted_content 往返（affinity，`responses/utils.py`）靠 Responses **item-id** 传递，Anthropic 客户端不回传 assistant message id、只存 thinking 的 `{thinking, signature}`，故 encrypted_content 在 Anthropic 边界结构性丢失。
- 好消息（评审核实）: reasoning item 的 `id`/`encrypted_content`/`summary` 在 direct adapter 这层**都拿得到**，只是被丢——修复落点明确、数据可得。

后果: gpt 对 Claude Code 不像原生 Anthropic 模型（推理不可见、跨轮不连续）。这是全保真的核心缺口。

> `fix_thinking`（hookpkg 请求侧）与本问题无关（只为 claude 路承重），保持现状。

## 2. 目标与非目标

### 目标（block 1）

1. gpt 的 reasoning summary 以**符合 Anthropic wire schema、可被 Claude Code 存储回放的私有 carrier thinking 块**呈现（可见）。
2. gpt 的 `id + encrypted_content + summary` 通过该 carrier **跨轮保留**，请求侧还原成后端可接受的 Responses reasoning item，使 gpt 保持推理连续性。
3. 覆盖 direct Responses adapter 的流式与非流式响应路径、请求侧回放路径。
4. 载体策略 A/B **可配置切换**，默认 A。
5. **跨模型安全**: 私有 carrier 绝不抵达真正的 Anthropic/Claude 后端。

### 非目标（见 BACKLOG）

- 协议信封问题（block 2），除非双 `message_start` 经 phase 0 证实会阻断 block 1（见 §7、§9-R）。
- 工具保真（block 3）、流式畸形主动改写（block 4）、claude 路 `fix_thinking`。
- **强制 chat bridge 路径**（`litellm.use_chat_completions_url_for_anthropic_messages=True`，[handler.py:61-65](litellm/llms/anthropic/experimental_pass_through/messages/handler.py#L61-L65)）**不承诺全保真**: 该开关关闭 direct Responses 路由、退回 chat bridge 时，carrier **不发射**、退化为当前行为，并打一条可观测 warning（`reasoning fidelity disabled: forced chat bridge`）。全保真只保证 direct Responses route。
- **per-request 覆盖**（top-level `reasoning_carrier`/`reasoning_summary` 直接进 `/v1/messages` 请求体）不在 block 1，延后（见 §4.5）。

### 术语澄清（评审 #12）

「合法 thinking 块」在本 spec 指: **符合 Anthropic Messages wire schema、可被 Claude Code 存储与回放、但仅由本 fork 解释的私有 carrier**。它**不是** Anthropic 服务端可验签的真 signature。不变量: 私有 carrier 绝不允许发往 Anthropic/Claude 后端（§4.7）。

## 3. 验收标准（可测）

1. **响应侧可见**: 触发 reasoning 的 gpt 流式请求，客户端收到的 thinking 块: 当 `reasoning_summary ∈ {auto,concise,detailed}` 时含**非空** `thinking` 文本（=summary）且 carrier 非空; 当 `reasoning_summary=off` 时允许无 thinking 文本，但 carrier **必须非空且跨轮精确回放**（评审 v2-#6）。两种都不再是「空壳且无 carrier」。
2. **envelope 往返精确**: carrier decode 出的 `id`/`encrypted_content`/`summary` 与响应侧 encode 进去的**逐字节一致**（单测）。仅保 encrypted_content、丢 id 视为不达标。
3. **跨轮连续性**: 两轮真实 gpt 会话，第二轮请求侧把回放 carrier 还原成**带原始 id** 的 Responses reasoning item、后端 200 接受（live）。
4. **配置切换**: `reasoning_carrier` 切 A/B，响应产对应块、请求对应 decode，往返测试通过。
5. **跨模型矩阵**（§4.6）: 四组合 × A/B 全部符合预期; 尤其 gpt-origin→Claude 时 carrier 被剥离/降级，不把伪签名发往 Claude 后端。
6. **失败即值**: decode 返回 tagged union; `InvalidCarrier`/`UnsupportedVersion` 安全丢弃 + 观测计数、不吞错、不泄露密文正文; 仅 `NotOurCarrier` 进既有逻辑。
7. **mutation oracle**: 删 encrypted_content、删 id、漏 summary 请求、错载体、提前 `content_block_stop`、篡改 envelope 一字节——对应测试必须变红。

## 4. 设计

### 4.1 replay envelope + carrier codec + block renderer（三层，评审建议）

**ReasoningReplayEnvelope**（强类型，frozen dataclass/Pydantic）: `{ version, reasoning_item_id, encrypted_content, summary_parts, origin_model? }`。保留 Responses 要求的 `summary` 空数组语义（`summary_parts` 可空但非缺失）。

**carrier codec**（两实现，配置选）: 把 envelope 序列化进 Anthropic 块字段:
- A（`signature`）: `thinking{thinking: summary_text, signature: NS(envelope)}`。
- B（`redacted_thinking`）: **两个独立 content block**——若有 summary，先一个 `thinking{thinking: summary_text}` **展示块**，再一个独立 `redacted_thinking{data: NS(envelope)}` **载体块**; 无 summary 则只发 redacted 载体块。`redacted_thinking` 的 `data` 只能出现在它自己的 `content_block_start` 里，不能作为 delta 注入已打开的 thinking 块（评审 v2-新1，见 §4.2 流式序列）。
- `NS(...)` = 命名空间化、版本化、带必填校验（可选 checksum）的编码，**非**「前缀 + 裸 base64」（评审 #11）。

**block renderer**: 把 codec 产物拼成 Anthropic content 块 / 流式 SSE 事件序列。

`decode(block) -> DecodeResult`（tagged union，评审 #10）: `DecodedCarrier(envelope) | NotOurCarrier | InvalidCarrier(reason) | UnsupportedCarrierVersion`。

> 三层拆分有 A/B 两个真实 codec 消费者，非空泛化; 不为单实现加无消费者的抽象。

### 4.2 响应侧（gpt → Claude Code）· direct adapter

- **非流式** [responses_adapters/transformation.py:~392-491](litellm/llms/anthropic/experimental_pass_through/responses_adapters/transformation.py)（丢失点 410-420）: 对 `ResponseReasoningItem`，用 `item.id`/`item.encrypted_content`/`item.summary` 构 envelope → codec/renderer 产 carrier 块，替换现在的空壳。
- **流式** [responses_adapters/streaming_iterator.py:~67-276](litellm/llms/anthropic/experimental_pass_through/responses_adapters/streaming_iterator.py): `encrypted_content` 在 `output_item.done` 才齐（评审 #3、#8），故冻结每载体的完整 SSE 序列与 index 递增:
  - **A**: `content_block_start(thinking)@i` → `reasoning_summary_text.delta*` 映射为 thinking 文本 delta → `output_item.done` 时发 `signature_delta`（signature = NS(envelope)）→ `content_block_stop@i`。单块。
  - **B**: 若有 summary，先 `content_block_start(thinking)@i` → summary delta* → `content_block_stop@i`; 再**新起** `content_block_start(redacted_thinking, data=NS(envelope))@i+1` → `content_block_stop@i+1`（data 在 start 事件里，无 delta）。无 summary 则只发 redacted 块（占一个 index）。后续块 index 相应顺延。
  - 参照既有 thinking+signature_delta 机制 [agentic_streaming_iterator.py:59-102](litellm/llms/anthropic/experimental_pass_through/messages/agentic_streaming_iterator.py#L59-L102)。注意 affinity `litellm_enc:` 包装在 [responses/streaming_iterator.py:198-219](litellm/responses/streaming_iterator.py#L198-L219)，envelope 搬**包装后**上游实际给的 encrypted_content 值。

### 4.3 请求侧（Claude Code → gpt）· direct adapter

落点 [responses_adapters/transformation.py:~134-174](litellm/llms/anthropic/experimental_pass_through/responses_adapters/transformation.py#L134-L174)（现状 161-164 把 `thinking.thinking` 当普通 assistant `output_text`、无 `redacted_thinking` 分支，评审 #4）。冻结规则:

- 识别到的 carrier 块 → **只**生成 Responses `reasoning` item（`id`+`encrypted_content`+`summary` 从 envelope 还原），**不生成 `output_text`**。
- B 版与 carrier 配对的 summary thinking 块是**展示副本**，也不生成 `output_text`（消费/去重，评审 #4）。
- 多 reasoning item / 多 summary part / reasoning 与 tool call 交错时，保持原始 item 顺序。
- **未识别的真 claude thinking（`NotOurCarrier`）当前目标为 gpt 时: 冻结为「丢弃该块 + 观测计数」**（评审 v2-新4）。三样例都丢: 有文本 thinking、空 thinking、纯 redacted_thinking。理由: claude 的不透明推理无有效 gpt reasoning-item 表示，且 assistant 的**实际回复文本另在 text 块**、丢 thinking 不损用户可见内容; 这正是正常跨模型会话里 gpt 看前序 assistant 轮的样子（不含他模型内部推理）。**未采纳**「降级为 output_text」——那会把他模型内部推理当可见 assistant 文本注入、污染 gpt 上下文。

### 4.4 Responses 请求补 summary

落点 [responses_adapters/transformation.py:~250-284](litellm/llms/anthropic/experimental_pass_through/responses_adapters/transformation.py#L250-L284) + [handler.py:~22-113](litellm/llms/anthropic/experimental_pass_through/responses_adapters/handler.py)（**不是** `github_copilot/responses/transformation.py`，评审 #6）。两层枚举:
- 部署配置 `reasoning_summary: off | auto | concise | detailed`。
- wire 映射: `off -> 省略 summary 字段`; 其余原样进 `reasoning.summary`（wire 合法值只有 `auto|concise|detailed`，无 `off`）。
- **冻结优先级**（评审 v2-新2，block 1 direct `/v1/messages` 路径）: ①deployment resolved config `reasoning_summary`（控制项）> ②global `litellm.reasoning_auto_summary=True` 映射为 `detailed`（deployment 未设时的回退）> ③默认 `auto`。
- 现有 per-request alias `reasoning_summary`/`reasoningSummary`（[utils.py:9157-9190](litellm/utils.py#L9157-L9190)、[main.py:5310-5343](litellm/main.py#L5310-L5343)）是 chat surface 的机制; block 1 在 `/v1/messages` **不支持**它作为控制项——若出现则忽略（文档说明），per-request 覆盖延后（§4.5、BACKLOG）。避免两 surface 语义分叉。

### 4.5 配置解析与传播合同（评审 #7）

现状: `_ADAPTER` 是 module-global 实例，`translate_response()` 只收 response，`AnthropicResponsesStreamWrapper` 只收 stream+model——**没有配置参数入口**。故不能只说「走已有通道」。**冻结如下**（评审 v2-新3）:

- **字段位置**: deployment `model_info` 下的命名空间键 `github_copilot_reasoning: {carrier, summary}`。选 `model_info` 而非 `litellm_params`: 它是**元数据**、不是会被转发进 provider 请求体的调用参数，已由 [handler.py:~495-533](litellm/llms/anthropic/experimental_pass_through/messages/handler.py) 按 deployment 读到，且 `ModelInfo` 允许 extra 字段（[types/router.py:124-155](litellm/types/router.py#L124-L155)）。
- **入口与优先级**: deployment `model_info` config > 全局内置默认（`carrier="signature"`, `summary="auto"`）。model group 经 alias 指向的 deployment 的 `model_info` 承载（同机制，无独立 group 层）。**per-request 覆盖 block 1 不支持**（延后），从而彻底规避 top-level 未注册参数泄漏进 copilot `extra_body` 的风险（[types/utils.py:3054-3072](litellm/types/utils.py#L3054-L3072)）。
- **null/unset**: deployment 未设 → 继承全局默认。
- **unknown 值**: `InvalidConfig` tagged error，resolve 时**fail loud**，不静默回退。
- **非 github_copilot provider**: resolver 返回 no-op 默认，忽略。
- **防泄漏**: config 只经 model_info 读取、产出 frozen config，**绝不注入** Responses 请求体。
- 用 **frozen dataclass/Pydantic** 表示 resolved config，**同时**传入 request adapter、non-stream response adapter、stream wrapper（需新增 plumbing，不再传裸 `dict[str, Any]`）。

### 4.6 跨模型矩阵（评审 #5，新增）

| 历史 origin | 当前目标 | 要求 |
|---|---|---|
| claude-origin | claude | 原样透传（现状） |
| claude-origin | gpt | 真 claude 签名无我们 NS → `NotOurCarrier` → **丢弃该 thinking 块 + 观测**（§4.3 冻结） |
| gpt-origin | gpt | carrier decode → 重建 reasoning item（核心正路） |
| **gpt-origin** | **claude** | **进入原生 Anthropic 路由前，剥离/降级私有 carrier**——绝不把伪签名发往 Claude 后端（否则 400）。若保 summary 可见，降级为普通 text，且不把伪 thinking 块留在 latest assistant message |

每组合再 × A/B 各验。

### 4.7 错误处理与不变量

- `decode` tagged union; 仅 `NotOurCarrier` 进既有逻辑; `InvalidCarrier`/`UnsupportedCarrierVersion` → 不含密文正文的 warning + 可观测计数，按冻结策略丢弃。
- **不变量**: 私有 carrier（`ghc-rsn` NS）绝不抵达 Anthropic/Claude 后端（§4.6 末行保证）。
- 编解码异常收敛为返回值，不向上抛破坏请求。

## 5. 前置 PoC（phase 0，带 oracle 表，评审 #8）

必须用**真实 Claude Code gpt 会话**（客户端存储行为不可 curl 替代）。A、B **各**跑，每轮记录并断言:

1. 抓 direct wrapper 实际收到的原始 `output_item.done`，记 `id` 与 `encrypted_content`（不从 transcript 反推）。
2. Claude Code transcript 里 carrier **逐字节原样存在**。
3. 下一轮抓代理构造的 Responses input，**精确断言** replay item 的 `id`/`encrypted_content`/`summary` 与原值一致。
4. 后端 200。
5. **重启 Claude Code** 后重复下一轮（验从 transcript 恢复）。
6. **切到 Claude** 验证不发送伪签名（§4.6）。
7. 篡改 carrier 一字节 → 明确失败行为。
8. 记录**完整 SSE 事件顺序**，确认双 `message_start`（§7）对 Claude Code 的实际影响。
9. 量 encrypted_content 大小 p50/p95/max（评审 R2，供 §4.1 是否需压缩 codec 决策）。

「gpt 用到上一轮推理」只作辅助观察，不替代结构断言。

## 6. 测试策略（主 seam 在 responses_adapters，评审 #9）

- **单元**: envelope encode/decode 往返字节一致、NS 识别、真 claude 签名不误伤（property-based: 真样本/随机串/合法 b64/伪前缀/未知版本）、summary 有无两分支、decode tagged union 各分支。
- **集成（端到端单元链，放 `tests/test_litellm/llms/anthropic/experimental_pass_through/responses_adapters/`）**: `_should_route_to_responses_api` 确认 gpt 进 direct handler; 非流式 `ResponsesAPIResponse.output → Anthropic carrier`; 流式 raw Responses SSE（`output_item.added → summary delta* → output_item.done` 带 encrypted_content）→ 完整 Anthropic SSE 序列; Claude Code 回放形状 → direct request adapter → Responses input（带原始 id）; 目标切 Claude 时 carrier stripping。provider 目录保留 affinity/config 测试。
- **mutation oracle**: 删 encrypted_content / 删 id / 漏 summary 请求 / 错载体 / 提前 stop → 变红。
- **e2e/live**: 沿用探针，A/B 各跑真实 gpt 两轮。

## 7. 双 message_start（评审 #13，已证实）

已复现（`streaming_iterator.py:~281-290` fallback 先发一次 + `~76-80` 上游 `response.created` 又发一次）。当前两个 start 通常都在 reasoning 块前，尚无证据证明它吞掉 reasoning 块，但可能让严格客户端重置/拒绝整条 stream，或污染 phase 0 归因。处置: phase 0 §5-8 记录并确认 Claude Code 行为; **若 Claude Code 在第二个 start 处重置/拒绝 → 它是 block 1 前置 blocker，一并修**; 若容忍且 carrier 稳定落盘 → 留 block 2。

## 8. 风险与未决

- **R1（最高）**: Claude Code 是否原样存储/回放 carrier（signature / redacted_thinking.data）。评审核实: Anthropic wire schema 只把二者定义为 string，未见客户端本地验签的必然证据，故 PoC 有实际价值、非已知必然失败; 但 Anthropic **服务端**会验真签名——故 §4.7 不变量必须成立。A/B 可配置切换对冲。
- **R2**: encrypted_content 体积 → phase 0 量 p50/p95/max; 若需压缩，在 envelope version 显式表达 codec，不静默压缩。
- **R3**: 上游 rebase 冲突 → 私有 fork、focus copilot，可接受; 改动集中 + 充分测试。
- **U1**: summary token/成本 → phase 0 一并观测。
- **U2**: 双 message_start 影响 → 见 §7，phase 0 定归属。

## 9. 相关

- `docs/BACKLOG.md` —— block 2/3/4 延后项。
- `~/.config/litellm/docs/illformed-fix.md` —— live 探针方法与 provider 差异结论。
- `~/.claude/skills/debugging-llm-proxy-transforms/` —— 代理转换排错方法论。

## 10. 评审吸收记录（2026-07-14 GPT 对抗评审）

代码核实的 2 blocker + 8 major + 3 minor 全部吸收（见 v2 变更提要与各节内嵌）。本人复核关键 blocker: [handler.py:532](litellm/llms/anthropic/experimental_pass_through/messages/handler.py#L532) 确走 `LiteLLMMessagesToResponsesAPIHandler`; [responses_adapters/transformation.py:410-420](litellm/llms/anthropic/experimental_pass_through/responses_adapters/transformation.py#L410-L420) 确只用 summary、丢 id/encrypted_content。**未采纳/降级**: 无——评审均为事实性或与用户价值一致的加固，全采。评审建议的 encode 三层拆分采纳但保持最小（A/B 两真实消费者）。

**轮2 复审（v2→v3）**: 0 blocker（两 blocker 确认 resolved），提出 4 major + 2 minor，均为「spec 把公开行为/配置合同留成实现时再定」+ 一个 B 流式序列技术错误。全部冻结:
1. B 流式序列改为**两个独立 content block**（summary thinking 块 + 独立 redacted_thinking 载体块），§4.1/§4.2 已改（原写法把 redacted 注入已开 thinking 块，Anthropic 无此事件）。
2. `NotOurCarrier`（claude-origin→gpt）冻结为**丢弃 + 观测**，§4.3；未采纳「降级 output_text」（污染上下文）。
3. 配置字段位置冻结为 deployment `model_info.github_copilot_reasoning`、per-request 延后、unknown→InvalidConfig fail loud、防泄漏，§4.5。
4. `reasoning_summary` 优先级冻结（deployment > global auto_summary > 默认 auto），既有 per-request alias 在 `/v1/messages` 不支持，§4.4。
5. 验收标准 1 改为区分 `off`（允许无文本、carrier 必非空），§3。
6. 强制 chat bridge 明确列为**非目标**（不发 carrier + warning），§2。
