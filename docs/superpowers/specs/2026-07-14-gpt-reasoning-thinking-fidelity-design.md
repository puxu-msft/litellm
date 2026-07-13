# GPT reasoning ↔ Anthropic thinking 全保真转换 · 设计/规格

状态: **草案，待评审**
日期: 2026-07-14
范围: Anthropic ↔ GPT 全保真格式转换的 **block 1**（reasoning↔thinking）。block 2/3/4 见 `docs/BACKLOG.md`，本 spec 不覆盖。
适用: 私有 fork 的 `github_copilot` provider（focus provider），gpt-* 模型经 `/v1/messages` 路由到 Responses API。

## 1. 背景与问题（why）

Claude Code 只说 Anthropic Messages 协议。gpt-5.x 是 reasoning 模型、经 copilot 走 Responses API，其推理状态是 `encrypted_content`（不透明、加密）。litellm 在中间做 Anthropic ↔ OpenAI/Responses 双向翻译。

2026-07-14 live 探针（原始数据存 `~/.claude/litellm/probe-logs/live-probe-t1.sse`/`t2.json`，方法与结论另见 `~/.claude/litellm/docs/illformed-fix.md` 的「thinking block 处置的 provider 差异」一节）确证:

- gpt 经此桥回来的 thinking 块是**空壳**: `{"type":"thinking","thinking":""}`，无 `signature`、无 delta、无 `encrypted_content`。真正的推理当普通 text 块吐出。
- 根因两条: ①Responses 请求**没设 `reasoning.summary`**，故 gpt 不回可见摘要 → thinking 无文本; ②响应侧 chat→Anthropic 转换（`litellm/llms/anthropic/experimental_pass_through/adapters/transformation.py` 的 `~1165-1190` 非流式、`~1412+` 流式）**只读 `thinking_blocks`/`reasoning_content`，丢弃 `reasoning_items`/`encrypted_content`**。
- litellm 自带的 encrypted_content 往返（`_build_reasoning_item` + item-id affinity，`litellm/responses/utils.py` 的 `_wrap_encrypted_content_with_model_id`）**只对原生 Responses 客户端有效**: 它靠 Responses item-id 传递，而 Anthropic 客户端只存 thinking 块的 `{thinking, signature}`、不回传 assistant message id，故 encrypted_content 在 Anthropic 边界结构性丢失。
- 全量存量 transcript（463 文件、3.7 万+ thinking 块）无一个 gpt-origin thinking 块，佐证 gpt 推理从未以可回放形式落地。

后果: gpt 对 Claude Code **不像原生 Anthropic 模型**——推理不可见、跨轮不连续。这是「全保真」的核心缺口。

> 注: `fix_thinking`（hookpkg 请求侧）与本问题**无关**。探针证明它只为 claude 路承重（claude 后端严格要求 `thinking.signature: Field required`，gpt 路对畸形 thinking 容忍/丢弃、开不开都 200）。它是「修复损坏 thinking 排列」的补丁，不是「reasoning↔thinking 转换」。

## 2. 目标与非目标

### 目标（block 1）

让 gpt 的 reasoning 在经此代理时对 Claude Code **像原生 Anthropic 扩展思考一样**:

1. gpt 的 reasoning summary 以**带载体的合法 thinking 块**呈现给客户端（可见）。
2. gpt 的 `encrypted_content` 通过 Anthropic 原生载体**跨轮保留**，请求侧还原回 Responses reasoning item，使 gpt 保持推理连续性。
3. 覆盖流式与非流式两条响应路径、请求侧回放路径。
4. 载体策略 A/B **可配置切换**，默认 A。

### 非目标（本 spec 不做，见 BACKLOG）

- 双 `message_start`、空壳收尾、断流信封补全等协议信封问题（block 2）——除非它直接挡住 block 1 的 reasoning 块发射。
- 工具调用保真（block 3）、流式畸形谱系主动改写（block 4）。
- claude 路的 `fix_thinking` 行为（已证与本问题无关，保持现状）。

## 3. 验收标准（可测）

1. **响应侧可见**: 触发 reasoning 的 gpt 流式请求，客户端收到的 thinking 块含非空 `thinking` 文本（= summary）与非空载体（signature 或 redacted_thinking.data），不再是空壳。
2. **encrypted_content 往返**: 载体里 decode 出的 encrypted_content 与响应侧 encode 进去的**逐字节一致**（单测保证）。
3. **跨轮连续性**: 两轮真实 gpt 会话，第二轮请求侧成功把回放载体还原成 Responses reasoning item、后端接受（200）、且 gpt 行为体现出用到了上一轮推理（live 验证）。
4. **配置切换**: `reasoning_carrier` 切 A/B，响应侧产出对应块类型、请求侧对应 decode，均通过往返测试。
5. **不误伤 claude**: 混用历史里的真 claude thinking（无我们标记）原样透传，不被当 encrypted_content 解析；claude 路请求行为不变。
6. **失败即值**: decode 遇未知/损坏载体返回 None、安全丢弃，不抛错、不污染请求。

## 4. 设计

### 4.1 载体抽象（单一职责、可测、可配）

新增小模块（建议 `litellm/llms/github_copilot/reasoning_carrier.py`，最终位置实现时定），接口:

- `encode(reasoning_item, *, carrier, summary_text) -> list[AnthropicBlock]`
  - `carrier="signature"`（A）: 产 `thinking{thinking: summary_text, signature: MARK + b64(encrypted_content)}`。
  - `carrier="redacted_thinking"`（B）: 产 `redacted_thinking{data: MARK + b64(encrypted_content)}`；若有 `summary_text`，并排追加一个 `thinking{thinking: summary_text}` 块（无独立签名）。
- `decode(block) -> str | None`: 若块的载体字段以 `MARK` 起头，剥离并解码返回原始 `encrypted_content`；否则返回 None（含真 claude 签名 → None，原样透传）。

**标记**: `MARK = "ghc-rsn:v1:"`（版本化）。作用: 请求侧区分「我们的载体」与「真 claude 签名」; 版本便于演进。编码用 base64 保证载体字段是安全字符串、往返字节精确。

**encrypted_content 形态**: 直接搬 copilot Responses 返回的 `encrypted_content` 原值（含 litellm 若已做的 `litellm_enc:` 包装则一并搬），请求侧原样还原，不自行拆解其内部结构。

### 4.2 响应侧（gpt → Claude Code）

在 `adapters/transformation.py` 的 chat→Anthropic 转换（非流式 `~1165-1190`、流式 `~1412+`）接线: 当 message 带 `reasoning_items`（其中含 `encrypted_content`）时，调 `encode(...)` 产出 thinking/redacted_thinking 块，替换/补齐现在产出的空壳。summary 文本取自 reasoning item 的 summary（见 4.4）。流式路径需把 encode 结果拆成 `content_block_start/delta/stop`（signature 经 `signature_delta` 事件下发，遵循既有 `agentic_streaming_iterator.py`/`fake_stream_iterator.py` 的 thinking+signature_delta 机制）。

### 4.3 请求侧（Claude Code → gpt）

在 Anthropic→Responses 的请求转换里，遍历历史 assistant message 的 content 块: 对每个 thinking/redacted_thinking 块调 `decode(block)`; 非 None 则据此重建一个 Responses `reasoning` item（`encrypted_content` = decode 结果），插入 Responses input 的对应位置; None 则按现状处理（真 claude 签名透传 / 无载体 thinking 交由既有逻辑）。

### 4.4 Responses 请求补 summary

在 `github_copilot/responses/transformation.py` 组装 Responses 请求时，按 `reasoning_summary` 配置设 `reasoning.summary`（`"auto"` 默认 / `"off"` 关闭）。关闭时 reasoning 无可见摘要 → A 版退化为「空 thinking + 有效载体」、B 版为「纯 redacted_thinking」，仍保往返连续性（等价方案 C）。

### 4.5 配置

挂在 github_copilot 部署的 litellm 配置（与现有 copilot 参数同处），全局默认 + 可按模型覆盖:

- `reasoning_carrier: "signature" | "redacted_thinking"`，默认 `"signature"`（A）。
- `reasoning_summary: "auto" | "off"`，默认 `"auto"`。

配置读取走 fork 已有的 capability/deployment 参数通道（近期提交已有 per-request deployment model_info 线索，实现时对齐），不新造 hookpkg 开关。

### 4.6 错误处理

- decode 未知/损坏载体 → None → 安全丢弃该 reasoning，不抛错。
- 真 claude 签名（无 `MARK`）→ decode 返回 None → 原样透传，绝不误解。
- gpt 未给 summary → 按 4.4 退化，不报错。
- 编解码异常收敛为「返回值/None」，不向上抛破坏请求（遵循 never-swallow 的同时以值建模失败: 记一条 warning 便于观测）。

## 5. 前置 PoC（phase 0，设计押在其上）

**验证 Claude Code 是否忠实存储并原样回放两种载体**（signature / redacted_thinking.data）。这是全设计的可行性根基，必须在正式实现前跑出结论。

做法: 最小实现响应侧 `encode`（先只 A，或 A/B 各一）→ 在 Claude Code 里用 gpt 跑一次真实会话 → 查存下的 transcript，确认载体字段原样存在 → 触发下一轮，抓代理侧请求，确认载体被原样回放、请求侧 `decode` 能还原、后端 200 接受。两载体各验一遍。

PoC 结论用于: 定默认载体; 若某载体被 Claude Code 篡改/校验拒绝，则调整标记/编码或改默认到另一载体。**客户端存储行为只能用真实 Claude Code gpt 会话测**，非 curl 可替代——此步需要人工在 Claude Code 侧驱动 gpt。

## 6. 测试策略

- **单元**: `encode`/`decode` 两载体往返字节一致; 标记识别; 真 claude 签名不误伤; summary 有/无两分支。
- **集成**（转换层，走既有 `tests/test_litellm/llms/github_copilot/`）: 「带载体的 Anthropic 请求 → 正确 Responses reasoning item（encrypted_content 精确）」; 「带 encrypted_content 的 Responses reasoning item → 正确 Anthropic 块」; 流式/非流式各一。断言要能在代码被变异（丢 encrypted_content、错载体、漏 summary）时变红。
- **e2e/live**: 沿用探针，两载体各跑真实 gpt 两轮，验可见性 + 往返连续 + 协议合规（无新增畸形）。

## 7. 风险与未决

- **R1（最高）**: Claude Code 可能不原样回放 signature（校验/丢弃）或 redacted_thinking.data。→ 由 phase 0 PoC 先证; A/B 可配置切换即为对冲。
- **R2**: encrypted_content 体积可能较大，signature/data 膨胀。→ 探针量实际大小，必要时评估是否压缩（保持往返字节可还原）。
- **R3**: 上游 litellm rebase 可能冲突转换层改动。→ 私有 fork、focus copilot，可接受; 改动尽量集中、带充分测试。
- **U1**: summary `"auto"` 的 token/成本影响未量化。→ 可在 PoC 时一并观测，必要时默认调整。
- **U2**: 双 `message_start` 是否会干扰流式 reasoning 块发射，待实现时确认; 若挡路则纳入本块顺带修，否则留 block 2。

## 8. 相关

- `docs/BACKLOG.md` —— block 2/3/4 延后项。
- `~/.claude/litellm/docs/illformed-fix.md` —— live 探针方法与 provider 差异结论、hookpkg 现状。
- `~/.claude/skills/debugging-llm-proxy-transforms/` —— 代理转换排错方法论（先探针定位哪一层，别凭结构盲改）。
