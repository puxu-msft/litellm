# 流式畸形/截断响应修复 / Ill-formed & Truncated Stream Fix

状态：**活文档**（hook 字节层修复已在线；fork direct Responses 状态机第一阶段已落地）
最近更新：2026-07-17

> 本文是 `stream_transform`（`hookpkg/stream.py`）如何处理**上游畸形/截断 Anthropic SSE 流**的活文档，随代码演进持续更新。它汇总各类畸形模式、对应的客户端症状、hook 的现状（已修/观测中/已知缺口），并详解本轮针对 `JSON Parse error: Unterminated string` 的修复。改动 hook 处理逻辑时，请同步更新本文的「畸形模式映射表」「已知缺口」「变更记录」三处。

## TL;DR

github_copilot 后端把 opus/gpt 请求做「Anthropic -> OpenAI -> Anthropic」两次格式往返，期间会引入多种畸形 SSE：孤儿 delta/stop、index 跳号、块未闭合、以及**中途断流留下的半截 JSON 帧**。不同畸形在客户端表现为不同报错。

本轮修复的是**半截 JSON 帧**：上游断流时最后一个 chunk 是形如 `data: {"type":"content_block_delta",...,"text":"hello wor` 的未闭合 JSON。`stream_transform` 旧逻辑把它与 `[DONE]`/ping 一样原样转发，客户端 SSE 的 JSON 解析器就报 `JSON Parse error: Unterminated string`。修复：新增判别函数 `is_truncated_json_frame`，在 `stream_transform` 里把这类帧**丢弃**（缓冲块由收尾逻辑补 `content_block_stop`），合法的非 JSON 帧照常透传。

## 背景：畸形从哪来

Claude Code 经本代理打到 copilot 后端时，`/v1/messages`（Anthropic 格式）先被 litellm 转成 OpenAI chat/completions，copilot 再把 opus 请求转回 Anthropic 发给真模型 —— **一次请求两次格式往返**。响应回来同样两次往返。`github_copilot` 是 OpenAI 系 provider，没有 `anthropic_messages_config`，所以流式响应要靠 `AnthropicStreamWrapper`（`litellm/llms/anthropic/experimental_pass_through/adapters/streaming_iterator.py`）把 OpenAI 增量重新拼成 Anthropic 的 `content_block_start / delta / stop` 事件序列。

这个重拼过程 + 上游连接的不稳定，会在多个环节产出不符合 Anthropic 协议的事件序列。客户端（Claude Code）对协议是严格的：它按 `index` 维护一个 content blocks 数组，并逐条把 SSE `data:` 解析为 JSON，任一环节不合法都会报错并可能回退非流式或直接失败。

## 畸形模式 -> 客户端症状 -> hook 现状（映射表）

`hookpkg/block_audit.py` 在 `stream_transform` 两侧各架一个 `content_block` 生命周期状态机，把畸形归类为几种 `violation`。下表是当前已知的畸形谱系与处置状态。

| 上游畸形 | block_audit 违规类型 | 客户端典型症状 | hook 现状 |
|---|---|---|---|
| 中途断流留下**半截 JSON 帧** | 帧不可解析、不计入 seq，通常伴随末尾 `unclosed` | `JSON Parse error: Unterminated string` | ✅ **本轮修复**：`stream_transform` 丢弃该帧 |
| 块从未 `start` 就来 `delta` | `orphan_delta` | `RangeError: Content block not found`（litellm#24765） | ⚠️ 观测中（block_audit 记录），未在 transform 侧改写 |
| 块从未 `start` 就来 `stop` | `orphan_stop` | 同上 | ⚠️ 观测中 |
| index 跳号（如 `start@1` 而无 `@0`） | `index_gap` | `Content block not found` | ⚠️ 观测中 |
| 上一块没 `stop` 就 `start` 下一块 | `start_without_stop` | 渲染错乱/块丢失 | ⚠️ 观测中 |
| open 块未闭合就断流 | `unclosed` | 悬挂 / `Content block not found` | 🟡 **部分**：被缓冲的块（text / 匹配工具）由收尾补 `stop`；未匹配透传的块**未补**（见「已知缺口」） |
| 工具调用参数字段缺失/截断/双编码 | （不体现在序列，体现在 input） | 客户端 schema 校验拒绝（如 AskUserQuestion 缺 `question`） | ✅ `stream_fix.tools` 四类修复 pipeline |
| `<invoke>` 泄漏进 text block | （text 块内容问题） | 工具不执行、被当普通文本显示 | ✅ `convert_text_invoke` |
| 转换层/上游把一次工具调用变成**两个**相同 tool_use block | `dup_tools_in`(block_audit 增强后可见) | 客户端连发两个格式正确、内容完全相同的工具调用（如 AskUserQuestion） | ✅ **已修**：hook 末端 `dedup_tool_use` 丢弃相邻字节相同的重复块（见「重复 tool_use 去重」） |
| 连续 thinking / 空 signature（请求侧历史回传） | （请求侧，非流式序列） | 后端 400 `thinking.signature: Field required`（**仅 claude 路**） | ✅ `fix_thinking`（`process`，请求侧）；**对 gpt 冗余**，见「thinking block 处置的 provider 差异」 |

图例：✅ 已修 / 🟡 部分修 / ⚠️ 仅观测未改写。

> 注意「半截 JSON 帧」与「orphan/index_gap/unclosed」是**不同症状**：前者是 JSON **语法**错误（`Unterminated string`），后者是 block **语义**错误（`Content block not found`）。排错时先看客户端报的是哪一类，别混。

## 本轮修复详解：半截 JSON 帧 -> `Unterminated string`

### 现象与历史锚点

- 报错文本：`API Error: JSON Parse error: Unterminated string`（客户端 `<synthetic>` 消息，`isApiErrorMessage: true`）。
- 历史记录：`~/.claude/projects/-home-xp-refs-ai-agents-litellm/c9533ffc-058a-4817-8a23-bbb184bf6604.jsonl`，报错时刻 `2026-07-13T15:08:49.895Z`，发生在一次普通助手回复的流式返回中（前一轮是 thinking + text + Bash 调用，模型继续生成时流被截断）。

### 根因定位（可复现的证据链，非结构推断）

1. **精确时间戳对齐**：在 `probe-logs/block-seq.jsonl` 里找到 `ts=1783955329.9` 的审计记录，与报错 epoch（`1783955329.895`）相差 <5ms。block_audit 在 `finally` 落盘，客户端报错并断连时正好触发，故 ts 与报错时刻吻合。该记录内容：
   - `in_seq = [message_start, content_block_start@1]`（`len_in=2`），`in_violations = [index_gap@1(expected 0), unclosed@1]` —— 上游只发了 `message_start` + 一个 `start@1` 就断流。
   - `out_seq = [message_start, start@1, stop@1]`（`len_out=3`），`out_violations = [index_gap@1]` —— 收尾逻辑补上了 `stop@1`。
   - **关键**：out_seq 三个事件都结构完好，客户端却仍报语法错误 —— 说明有**不被 block_audit 记录的不可解析字节**发给了客户端（`sse_parse` 返回 None 的 chunk 不计入 seq，但仍会被 yield）。
2. **确定性复现**（不依赖非确定的真实后端）：用合成截断流喂给 `stream_transform`，直接观察它吐出的每条 SSE `data:` 是否可解析。喂入
   ```
   message_start
   content_block_start@1 (text)
   b'event: content_block_delta\ndata: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"hello wor'   # 半截,无闭合
   ```
   输出里出现不可解析的 data 行，Python `json.loads` 报 `Unterminated string starting at: line 1 column 77` —— 与客户端报错**同一错误**。复现坐实。

### 机制

`stream_transform` 的 `ev is None` 分支（`hookpkg/stream.py`）原本把所有 `sse_parse` 失败的 chunk 都 `yield chunk` 原样转发。这个分支的本意是放行 `[DONE]`、ping、空行等**合法的非 JSON 帧**。但上游断流留下的**半截 JSON 帧**同样让 `sse_parse` 返回 None，于是被一并原样转发，直达客户端 -> `Unterminated string`。

与 block_audit 记录吻合：半截帧不可解析 -> `observe_in`/`observe_out` 都跳过它 -> in_seq/out_seq 都看不到它，但它确实被 yield 给了客户端。

### 判别依据：如何区分「半截 JSON 帧」与「合法非 JSON 帧」

关键事实：**Anthropic 事件恒为 JSON 对象**（`{...}`），从不是顶层数组或裸值。据此：

- 某 `data:` 行 payload 以 `{` 起头**却** `json.loads` 失败 => 截断/损坏的事件帧 => **丢弃**。
- `[DONE]`（以 `[` 起头）、ping、SSE 注释（`: ...`）、空行 => 一律不满足「`{` 起头且解析失败」 => **透传**。

这条判据精确、无副作用：`[DONE]` 以 `[` 起头被排除；合法的 ping（`data: {"type":"ping"}`）能解析被排除；真正的半截对象帧被命中。

### 实现

- `hookpkg/sse.py` 新增纯函数 `is_truncated_json_frame(chunk)`：
  - 解码用 `errors="replace"`，使尾部截断落在多字节 UTF-8 边界（如半个汉字）时仍能识别 `data: {` 前缀。
  - 逐 `data:` 行判断：`payload` 为空 / `== "[DONE]"` / 非 `{` 起头 => `False`；`{` 起头且 `json.loads` 抛异常 => `True`。
- `hookpkg/stream.py` 的 `ev is None` 分支最前面接入：
  ```python
  if is_truncated_json_frame(chunk):
      logger.warning("stream_fix: dropped truncated/corrupt upstream SSE frame")
      ctx.audit("dropped_truncated_frame", head=repr(chunk)[:200])
      continue
  ```
  丢弃后 `continue`，不触碰缓冲。缓冲中的块（`buffering` 工具 / `tbuf` 文本）由**流末尾/异常收尾逻辑**补 `content_block_stop`，不会悬挂。合法非 JSON 帧走原有透传路径不变。

### 为什么「丢弃」而非「修复」

半截帧是上游已经断裂的流的最后残片，尝试补齐闭合符去「修复」它需要知道该事件的完整结构，且救回的往往只是几个字符的尾巴（text 的最后半个词，或工具 input 的碎片 —— 后者本就由 `stream_fix` 的 `buf_partial` 单独缓冲、在 `content_block_stop` 由 `loads_lenient` 修复）。丢弃是正确且最小的处置：移除损坏字节、让块被干净收尾，代价可忽略。遵循「Simplicity First」与正确性优先。

## 收尾逻辑全景（流如何被闭合）

`stream_transform` 在三处保证块闭合，理解它们才能判断某类畸形是否已被兜住：

1. **正常流末尾**（`async for` 自然结束）：若仍 `buffering`（匹配工具），回放 `buf_start + buf_deltas` 并补 `content_block_stop`；若仍 `tbuf`（文本），`emit_plain_text_block` 发出（空文本也发 start+stop）；若 `injected` 了 tool_use 却从未见 `message_delta`，补一个 `message_delta(stop_reason=tool_use)`。
2. **异常收尾**（`except Exception`）：上游中途 raise（如 httpx 断流）时，同样尽力回放缓冲并补 `content_block_stop`，然后**不 raise**（薄壳无法重放已消费的上游）。
3. **本轮新增的丢弃**：半截帧不进入上面任何缓冲，直接被丢，由 1/2 补 stop。

## 已知缺口 / 待办（不静默漏报）

以下是本轮**未纳入**修复的相关畸形，症状不同，记录在此避免遗忘：

- ~~**Chat 最终 SSE 未匹配透传块的 `unclosed`**~~：**已覆盖（2026-07-17）**。fork Chat/direct Responses adapter 统一补终止信封；解析前 hook 重组 raw provider chunks 并处理截断，callback 输出后 core normalizer 保护 keepalive 注入边界
- ~~**核心 adapter 缺失 `message_stop` / `message_delta`**~~：**已实现（2026-07-17）**。Chat sync/async 与 direct Responses 的自然 EOF、异常 EOF 均关闭 open block，并补 `message_delta(stop_reason=end_turn)` + `message_stop`；真实 finish/completed 提供的 stop_reason/usage 优先
- **`orphan_delta` / `orphan_stop` / `index_gap` 的主动改写**：direct Responses 已在 fork 核心按 item_id 规整 reasoning orphan delta、function arguments orphan 缓冲/重放、orphan done、duplicate added/done 和负 index；Chat 最终 SSE 继续由 block_audit 观测和 hook 边界恢复

## Fork 核心迁移状态（2026-07-17）

- `responses_adapters/streaming_iterator.py`：fallback 与 `response.created` 幂等，只发一个 `message_start`；所有 Responses item 通过统一生命周期建立连续 block index；completed/EOF/异常共用完整终止出口
- `adapters/streaming_iterator.py`：sync/async 共用终结队列，断流时不再留下 open block 或缺 message 终止事件
- 工具参数：normal delta、orphan 缓冲、done-only 完整 arguments 三个来源互斥；避免丢失、负 index 与 done 重复补发
- 保留在 hook：SSE bytes 跨 chunk 重组、尾部截断帧丢弃、具体工具 schema 补全、`<invoke>` 恢复、末端重复工具去重和生产审计
- 双边界结论：hook 位于 core keepalive normalizer 之前，不能删除解析前重组；core 位于 callback 输出之后，不能删除最终 frame normalizer。hook 现复用 core LF/CRLF/CR delimiter 与 8 MiB 上限 primitives

以上是否要一并做掉，取决于线上是否真的出现对应症状 —— 用 block_audit 的 `violation_only` 数据驱动决策，别凭空加工。

## 重复 tool_use 去重（`dedup_tool_use`）

现象：一次请求客户端连发两个格式正确、**内容完全相同**的工具调用（实测 AskUserQuestion）。

### 取证与一次被证伪的推断（记录教训）

- **方法论纠正**：一开始查 Claude transcript（3206 个）全空。原因：transcript 是**客户端渲染/去重后**的产物，AskUserQuestion 客户端一次只弹一个，重复的被吞掉。真相源在**代理侧**，不是 transcript。回溯历史请求应查 litellm 侧（postgres `LiteLLM_SpendLogs`）+ block_audit 流式记录。
- **一个被证伪的过度自信**：曾据「`LiteLLM_SpendLogs` 里 285 条 AskUserQuestion 响应精确解析后 0 条含 ≥2 个 tool_call」推断「排除模型 degeneration、必是转换层拆块」。**这个排除不成立**：litellm 的 `stream_chunk_builder` 按 `tool_calls[].index` 聚合，而这些调用常全为 `index=0`，所以「模型/copilot 真发了两个 index=0 的相同调用」在聚合里**同样只显示 1 个**。DB 聚合无法区分「转换层拆一个」与「上游发两个」。
- **转换层 patch 尝试后撤回**：曾试改 `AnthropicStreamWrapper._should_start_new_content_block`（见 `streaming_iterator.py`）——它对 tool_use「见 name 即起新块」，不校验 tool_call index/id。但既有测试 `test_parallel_tool_calls.py::...interleaved...` 证明仓库存在**合法契约**：两个 **id 相同、参数不同**（SF 紧跟 CHI）、全 `index=0` 的背靠背调用应是**两个独立块**。在 `content_block_start` 时刻**拿不到完整 input**，无法区分「同一调用的重复」与「参数不同的合法背靠背」，按 name/index/id 判定必误伤其一。故**撤回**（`streaming_iterator.py` 保持原状），改在 hook 末端用完整 input 判定。

### 修复：hook 末端按完整 input 去重（根因无关）

无论重复源自转换层拆块还是上游真发两个，客户端收到的都是两个「name + 完整 input 字节完全相同」的相邻 tool_use。`hookpkg/dedup.py` 的 `dedup_adjacent_tool_use` 在**发给客户端前的最后一层**丢弃与紧邻前一个 tool_use 逐字节相同的块，并把后续块 index 递减保持连续。

- **为什么这层、用完整 input**：区分「重复」与「合法背靠背」唯一可靠依据是**完整累积 input**（SF≠CHI 的 input 不同，永不误伤）；这正是转换层块起始时刻拿不到的。两个字节完全相同的相邻工具调用对用户从无价值（问同一问题两次、并行跑同一命令），故丢弃安全。
- **链路**：`response -> [block_audit 入站] -> stream_transform -> [block_audit 出站] -> dedup -> 客户端`（`hookpkg/__init__.py` 组合）。dedup 在 block_audit **之后**：block_audit 仍如实记录上游/转换后的重复（`dup_tools_in/out`），dedup 在末端丢重复并记审计 `dropped_duplicate_tool_use`（落 `stream-patched.jsonl`）。
- **配置**：`hooks.config.json -> dedup_tool_use.enabled`（现 `true`）。相邻性：只对**直接相邻**的相同 tool_use 去重（中间夹任何非 tool 块即打断），保守。
- **保留的观测**：block_audit 的 `dup_tools_in` 命中 = 上游/转换层就发了两个（区别于仅 `dup_tools_out` = stream_fix 注入）。要进一步坐实到底是转换层拆块还是上游 degeneration，仍需抓一次上游 OpenAI `tool_calls` 分片（id/index/arguments）——block_audit 增强 + `stream_fix.probe_only` 可捕获。

## thinking block 处置的 provider 差异（2026-07-14 live 探针实测）

`fix_thinking`（`hookpkg/thinking.py`）当初为「后端 400」建，但 live 探针（直打 `127.0.0.1:4143/v1/messages`，原始 SSE + 请求回放，raw 数据存 `probe-logs/live-probe-t1.sse`/`live-probe-t2.json`）证明它**只对 claude 路承重，对 gpt 冗余**。方法论遵循 skill「先探针定位哪一层，别凭结构推断」。

### 实测事实（非推断）

- **gpt 的 thinking 是空壳**：gpt-5.6-sol 走 Responses API，流式回来的 thinking 块恒为 `{"type":"thinking","thinking":""}` —— **无 `signature` 字段、无 delta、无 `encrypted_content`**。真正的推理当作**普通 text 块**吐出。gpt 的 `encrypted_content` 只藏在 Responses 响应 `id`（base64 包 `model_id`/`response_id` 的 affinity 机制），而 Anthropic 客户端不回传 assistant message id，故它到不了下一轮。
- **fix ON/OFF × gpt/claude 的 4 格实测**（回放畸形 thinking 排列，非流式看 http_code）：

  | 回放的畸形排列 | gpt（fix 关） | gpt（fix 开） | claude/opus（fix 关） |
  |---|---|---|---|
  | 连续两个 thinking | 200 | 200 | **400** `thinking.signature: Field required` |
  | 非空 thinking 无签名 | 200 | 200 | **400** `thinking.signature: Field required` |

- **结论**：`thinking.signature: Field required` 的 400 是 **claude（opus/sonnet 经 copilot 回 Anthropic-native 后端）**的严格校验报的。gpt 走 Responses，无有效 encrypted_content 的 thinking 在转换层被丢/被容忍，到不了报错 —— 开不开 `fix_thinking` 都是 200。日志里的 `fixed N thinking block issue(s) on model='gpt'` 是**触发了但对该 gpt 请求非必需**的兜底（无害但多余）。

### 「需要 reasoning ↔ thinking 双向转换吗」——不需要（就正确性而言）

- gpt 路：thinking 是空壳，没有推理/签名/encrypted_content 可转；推理已以 text 保留；畸形 thinking 被容忍。**没有断掉的往返要修**。
- claude 路：thinking 靠自带真实签名原生往返；`fix_thinking` 只是给流式截断/排列打乱做清理，避 400 —— 这是「修复损坏的 thinking 排列」，不是「reasoning↔thinking 转换」。
- 「把 encrypted_content 塞进 signature 双向搬」唯一收益是给 gpt 增加跨轮推理连续性，但 encrypted_content 在 L4 就被丢、官方 hook（L5）碰不到（SSE 已是空壳），要抓需 site-packages patch；且 gpt 现在工作正常。**收益未证的质量优化，进 backlog + 先 PoC，非需求**。

### 待办（backlog，非阻塞）

- **把 `fix_thinking` 按路由收窄到 claude 路**（gpt 上跳过）：省无谓转换，并消除一个潜在语义错配 —— `empty_signature: to_text` 会把「非空但无签名的 thinking」转成可见 text 块，对 gpt/claude 一视同仁；当前因 gpt thinking 恰为空壳（to_text 遇空走删除）而未出事，但是埋着的错配。落地时补回归测试锁「gpt 请求不被 fix_thinking 改动」。
- **（可选，需 PoC）gpt 跨轮推理连续性**：L4 site-packages patch 抓 `encrypted_content` → 塞进 thinking.signature（响应侧）→ 请求侧读回还原为 Responses reasoning item。先量收益再决定是否值得。



- **block_audit**（`hooks.config.json -> block_audit`，现 `enabled=true`）：双侧记录 `in_seq/out_seq/in_violations/out_violations` 到 `probe-logs/block-seq.jsonl`。`regressed=true`（入站干净、出站脏）是 `stream_fix` 自己改断的铁证；`violation_only=true` 可只落畸形流。**排查客户端流式报错，第一步就是按报错时间戳去 block-seq.jsonl 找对齐记录。**
- **block_audit 已增强（记 tool 身份）**：每条记录含 `in_blocks`/`out_blocks`（每个 `content_block_start` 的 `[index, type, tool_name]`）与 `dup_tools_in`/`dup_tools_out`（同名 tool_use ≥2 的观测信号，非违规——合法 parallel 也会命中，需人工核对 input）。用于定位「连发两个相同工具调用」类现象，并区分成因（in 侧 = 上游/转换层；仅 out 侧 = stream_fix 注入）。
- **litellm 侧回溯（不是 Claude transcript）**：客户端 transcript 经过渲染/去重，未必反映代理实发字节。回溯历史请求优先查 postgres `LiteLLM_SpendLogs`（含 `messages`/`response`，`store_prompts_in_spend_logs: true`）；注意 `response` 是非流式聚合（OpenAI 格式），流式独有的拆块/畸形不体现在其中，那类要靠 block_audit 的流式记录。
- **本轮新增 audit 事件**：`dropped_truncated_frame`（丢弃截断帧）与 `dropped_duplicate_tool_use`（丢弃相邻重复 tool_use）均落 `stream_fix.audit_file`（`probe-logs/stream-patched.jsonl`），生产也记。线上遇到对应畸形时这里各有一行，而不是客户端报错/看到重复 —— 修复生效的正向信号。
- **抓真实 wire 格式**：置 `stream_fix.probe_only=true` -> `./reload.sh` -> 触发请求 -> 看 `probe-logs/` 的 `chunk_shape`/`block_start`/`tool_input` 等诊断事件确认真实字节形态，再写针对性改写。切记：chunk 在 anthropic_messages 流式路径下是 **SSE 序列化后的 bytes**（hook 在序列化之后），不是事件 dict。

## 测试

`hookpkg/tests/test_stream_degen.py`，从 litellm 根目录跑：

```sh
python3 -m unittest hookpkg.tests.test_stream_degen -v
```

本轮新增两组，共 8 个：

- `TestTruncatedFrameDrop`（集成，喂 `stream_transform`）：
  - `test_truncated_trailing_frame_not_forwarded` —— 复现真实故障（Variant C），断言输出无任何不可解析 data 行、半截帧字节整体不出现、`hello wor` 不泄漏、缓冲 text 块仍有 start+stop。
  - `test_truncated_frame_during_tool_buffer_dropped` —— 缓冲匹配工具期间断流，半截帧丢弃、块被补 stop。
  - `test_done_and_ping_still_pass_through` —— `[DONE]` 与以 `[DONE]` 为 data 的 ping 仍原样透传，不被误丢。
- `TestIsTruncatedJsonFrame`（判别函数单测，守边界）：截断对象帧 / `[DONE]` / ping / 合法对象帧 / 无 data 行与注释 / UTF-8 边界截断。

**守 bug 有效性已验证**：把 `is_truncated_json_frame` 临时改为恒返回 `False`（模拟修复前透传行为），两个截断集成测试**如期失败**（半截 JSON 泄漏），恢复修复后 50 测试全绿、无回归。这保证了测试不是「摆设绿」，代码被回退/变异会立即变红。

## 维护指引

- 改 `hookpkg/` 代码后：`./reload.sh`（SIGUSR2，进行中的流不受影响），下次请求生效；改 `hooks.py` 薄壳或 `hookpkg/reload.py` 自身需**重启** litellm。
- **验证 reload 真的生效**（别只看 `./reload.sh` 打了 "已发 SIGUSR2"）：看 `probe-logs/reload-audit.jsonl` 是否新增一条 `reload_ok`（`pid` 对得上、`n_reloaded`=`expected`）。埋点按三段落痕，缺哪段病在哪段：① `handler_installed`（`is_main` 是否 True——非主线程 `signal.signal` 必失败并记 `handler_install_failed`；`installed_disposition` 判 handler 是否被上游框架覆盖）；② `_signal_count`（信号到没到，与 `reload-sends.jsonl` 发送侧对账）+ `maybe_reload_first_poll`（hook 入口在不在轮询）；③ `reload_ok`（真跑没跑，带 `sig_latency`）。2026-07-14 本会话实测当前部署三段全绿（`is_main=True`、`reload_ok 15/15`），机制健康——曾疑「SIGUSR2 静默失效」是旧代理跑的是**无审计**旧 `reload.py`、看不到落痕所致的观测缺口，非机制损坏。
- 本会话/客户端就走本代理，改完热重载后，下一次请求即是线上实测。
- **何时更新本文档**：新增/改动任一畸形的处置逻辑时，同步更新「畸形模式映射表」；补掉某个「已知缺口」时把它从待办移入映射表并标 ✅；每次实质修复追加「变更记录」一行。

## 变更记录

- 2026-07-13 —— 新增 `is_truncated_json_frame`（`hookpkg/sse.py`）+ `stream_transform` 丢弃截断帧（`hookpkg/stream.py`），修复 `JSON Parse error: Unterminated string`；新增 8 个回归/单测（`hookpkg/tests/test_stream_degen.py`）；线上已热重载启用。根因定位见上「确定性复现」。
- 2026-07-14 —— 调查「连发两个相同 AskUserQuestion」：先增强 `block_audit` 记 `in_blocks`/`out_blocks` + `dup_tools_in/out`（`hookpkg/block_audit.py`，+4 测试）。转换层 patch 尝试后**撤回**（既有 `interleaved` 测试证明「同 id、参数不同、全 index=0」的合法背靠背需保持两个块，块起始时刻无完整 input 无法区分重复与合法）。改为 hook 末端 `dedup_tool_use`（`hookpkg/dedup.py`，按 name+完整 input 去重相邻重复，+8 测试），组合进链路末端（`__init__.py`），`hooks.config.json` 启用，线上已热重载。同时修正前一条「DB 聚合排除 degeneration」的过度推断（聚合按 index 合并，无法区分）。
- 2026-07-14 —— 增强 `fix_thinking` 日志：由「只有总数」改为「总数 + 逐类动作 + 结果形态（转文本字符数/删除原因/剥离类型细分）+ 涉及 msg 下标」，`fix_thinking_blocks` 返回值从 `int` 计数升级为结构化 action 元组（`hookpkg/thinking.py`）；新增 `hookpkg/tests/test_thinking.py`（此模块原先无测试，14 用例，含日志摘要断言）。
- 2026-07-14 —— live 探针实测 thinking block 的 provider 差异（见「thinking block 处置的 provider 差异」一节）：证明 gpt 的 thinking 恒为空壳（无签名/无 encrypted_content，推理走 text 块）、`fix_thinking` 只对 claude 路承重（`thinking.signature: Field required` 400 仅 claude），对 gpt 冗余；否定「需要 reasoning↔thinking 双向转换」的正确性动机；两条 backlog（收窄 fix_thinking 到 claude 路、可选 gpt 跨轮推理连续性 PoC）。
- 2026-07-14 —— reload 机制加全链路诊断埋点（`hookpkg/reload.py` + `reload.sh`，落 `reload-audit.jsonl` 带 PID/TID/`is_main`/`signal_count`/`poll_count`，`reload.sh` 发送侧落 `reload-sends.jsonl`）：实测当前部署三段全绿、机制健康，纠正此前「SIGUSR2 静默失效」的误判（实为旧代理跑无审计旧 reload.py 的观测缺口）。详见「维护指引」。埋点当前未提交（用户要求）但已在磁盘/线上生效。同时补「缓解边界：downstream keepalive 不治上游半截帧」与「现状快照」两节。未改任何 hook 处置逻辑，纯诊断/文档。

## 缓解边界：downstream keepalive 不治「上游半截帧」（2026-07-14 本会话核实）

读到「`Unterminated string` 来自中途断流」时，容易顺手去开 litellm 的 `stream_keepalive`（`litellm/proxy/common_utils/sse_keepalive.py` + `common_request_processing.py`，已接进运行路径）当解药。**这是误区，先分清它治什么：**

- keepalive 是**下游**保活：上游空闲时向客户端注入 `: ping` 注释帧（见到 `message_start` 后追加原生 `event: ping`，two-phase），用来**重置下游客户端 / 中间层（如 Caddy）的 inactivity 读超时**，避免它们在上游长时间沉默（如长 thinking）时先把连接掐了。它**不改上游发来的字节**。
- 因此：copilot 双转换**自己**发的半截 JSON 帧（`is_truncated_json_frame` 丢弃的那类）与 keepalive **无关**，开它不治此症；真正兜住这类的是 `stream_transform` 的丢弃 + `_reassemble_sse_frames` 的重组。
- 但**若**某次截断的成因是「下游 inactivity 超时把连接掐了」（客户端/Caddy 在上游沉默期放弃），keepalive **能**防住那一类。两者是不同成因，分清得靠按报错时刻抓 `block-seq.jsonl` + 逃逸帧字节，别凭症状名反推。
- 配置（供需要时启用，治的是下游超时那类）：全局 `litellm_settings.stream_keepalive`，或按 deployment 的 `litellm_params.stream_keepalive`（后者按已设字段覆盖全局）；字段 `enabled`（默认 true）、`interval`（秒，默认 15，下限 1，须明显小于下游最短 inactivity 超时才有效）。

## 现状快照（2026-07-14）

截至 2026-07-14 ~20:52Z，过去约 1h44m 内全项目 transcript 零 `Unterminated string` / `Unexpected identifier` / `Content block not found` —— 三类流损坏客户端报错均停在 ~19:08Z（本地 03:08）。此前那一波（本地 02:56–03:08）随 `_reassemble_sse_frames` 上线 + 多次整进程重启已消停。**当前无可复现的活 bug**；「重组器上线导致截断帧逃逸」的回归假设未被证据支持（错误是归零、非增多）。上游截断本身仍会偶发，靠 `is_truncated_json_frame` 丢弃 + 重组兜底，属既有防线覆盖范围。

## 相关文档

- `README.md` —— hook 系统总览、配置项、热重载、可观测。
- `docs/plan/degeneration-trim.md` —— 文本块全缓冲机制（本轮修复所在状态机的另一消费者）。
- `~/.claude/skills/debugging-llm-proxy-transforms/` —— 调试方法论与官方 hook 机制沉淀，尤其 `reference/streaming-rewrite.md`（流式改写状态机）与「先确认 chunk 真实 wire 形态」的血泪教训。
