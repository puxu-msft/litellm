# 泄漏 invoke → tool_use 转换 / Text-Leaked Invoke Conversion

**最高风险的流式改写**：模型(opus 经 copilot)有时把本该是 tool_use 的调用**泄漏成普通文本**吐进 text block，客户端收到后当文本显示、工具永不执行。本功能识别该模式，把泄漏的 `<invoke>` 转成真正的 Anthropic tool_use block，并改 stop_reason 让客户端执行。

配置开关 `stream_fix.convert_text_invoke`（默认关）。改流结构 + 改消息语义，务必配白名单 + audit。

## 泄漏的真实形态（实测样本）

```
看起来上一条消息被截断了。我重新发送。      ← 合法叙述(保留)
court                                        ← 损坏残渣(用户要求原样保留,不剔除)
<invoke name="SendMessage">                  ← 裸 invoke,无 <function_calls> 外层
<parameter name="to">a0cda3d3971f42759</parameter>
<parameter name="summary">...</parameter>
<parameter name="message">...</parameter>
</invoke>
```
- 语法：裸 `<invoke name="X">` + `<parameter name="k">v</parameter>` + `</invoke>`。容错 `antml:` 前缀、可选 `<function_calls>` 外层。
- **参数值全部提取为字符串**——若目标工具参数本应是数字/布尔/数组，转换后是 str，客户端 schema 可能拒绝。留意非字符串参数的工具。

## 转换：一个 text block → [text + tool_use (+text)]

`_extract_invoke_from_text(full)` 把 text 切成有序片段序列 `[("text",s) | ("tool_use",name,input)]`（支持一个 block 里多个 invoke）。到 `content_block_stop` 时按片段依次发出 block。

三个硬骨头（都已实测解决）：

### 1. index 偏移传播（偏移量模型 B）
原 text block 占 1 个 index，拆成 N 个 block 后**多出 N-1 个**。维护 `index_shift`，注入后**所有后续** content_block_* 事件的 index 都要 `+index_shift`（含现有 tool_use 补全分支、passthrough 分支、异常/流末尾回放）。`_shift_ev(ev, template)` 统一处理；`index_shift==0` 时返回 None → 字节透传（**关闭功能时零回归**）。

### 2. stop_reason 改写
泄漏时 `message_delta.delta.stop_reason` 多是 `end_turn`，但客户端要 `tool_use` 才执行合成调用。注入后置 `injected=True`，到 message_delta 改写。**异常截断无 message_delta 时**：流末尾若 `injected and not saw_message_delta`，**补发** `message_delta(stop_reason=tool_use)`，否则客户端收到 tool_use 却不执行。

### 3. 唯一 tool_use id
同一 block 多个同名 invoke（如两个 SendMessage）必须生成**不同** id，否则客户端 tool_result 按 id 回配错乱。用 index 保证唯一：`toolu_synth_<name>_<index>`。

## 白名单（防误伤，支持 glob）

`convert_text_invoke_tools`：非空时**仅转名单内工具**，其余 `<invoke>` 当普通文本放行——防止模型在正常回复里合法提到 `<invoke>`（讲解/贴示例/写文档）被误转成**真实工具执行**。名单项支持 **glob 通配**（`fnmatch`）：
- 精确 `"SendMessage"`、前缀 `"mcp__plugin_*"`、单字符 `"Tool?"`。
- 匹配逻辑 `_name_in_whitelist(name, whitelist)`：任一 pat 精确等于或 `fnmatchcase` 命中即通过。
- **保守策略**：一个 block 里若有**任一**泄漏工具不在名单，**整块**当普通文本放行（不做部分转换）。
- 空名单 = 任意 invoke 都转（向后兼容）。

⚠️ 高危工具（Bash/Edit/Write）进白名单意味着其泄漏 invoke 会转成**真实副作用调用**——误转代价大。是否纳入由用户决定；纳入后靠 audit 观测非预期转换。

## 失败兜底

- 未闭合 `<invoke>`（流中途/截断）→ 不转换，原样回放该 text block。
- 解析失败 → 同上。
- 被白名单挡下 → 整块当普通文本发出。
- 三者殊途同归：**宁可不转，绝不发半个 invoke 或弄坏文本**。

## 配置与可观测

```json
"stream_fix": {
  "convert_text_invoke": true,
  "convert_text_invoke_tools": ["SendMessage","AskUserQuestion","Task","Agent","mcp__plugin_*"],
  "audit_file": "/home/xp/.config/litellm/probe-logs/stream-patched.jsonl"
}
```
- 转换发生时 audit 记 `{"_diag":"invoke_converted","n_tool":N}`。**唯一线上信号**——看有无非预期工具名（误伤）或高危工具被转（副作用）。
- `probe_only: true` 时用 `text_leak_partial`/`text_leak_full` 诊断先抓真实泄漏 wire 格式，再开转换。

## 验证要点（自测已覆盖）

真实样本端到端（拆分正确、court 保留、stop_reason 改写）、convert 关闭字节零回归、index 偏移传播（泄漏后 block 正确 shift）、未闭合透传、多同名 invoke id 唯一、无 message_delta 补发、message_delta 违约先到不丢块、glob 前缀匹配。
