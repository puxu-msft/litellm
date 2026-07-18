# 案例：AskUserQuestion 缺 question 的流式修复

## 症状
Claude Code 调用 AskUserQuestion 报本地 schema 校验错误：
```
InputValidationError: questions[0].question is missing
```
每个 `questions[]` 元素有 `header`(≤12 字符短标签) 和 `question`(完整问句)；`question` 必填却缺失。

## 目标修复
经代理运行的模型若漏填 `question`，在 L5 流式响应 hook 里从同元素的 `header` 补上（`question ← header`）。实现见 `streaming-rewrite.md`。

## 本案最大教训：先确认 chunk 的真实 wire 形态，别按假设的 dict 解析

写完流式修复后「不生效」。诊断经历了**两次结论**，第一次是错的，记录下来警示：

### 错误的第一轮结论（已被推翻）
诊断探针显示 `stream_transform_entered>0` 但 `block_start` 统计空，我据此下结论「当前会话不走代理 / AskUserQuestion 是客户端工具不走流」。**这是错的**——犯了同一个毛病：**没检查 chunk 的实际类型就归因**。

### 正确的根因（chunk 是 SSE bytes，不是 dict）
加一级 `chunk_shape` 诊断（记录 `type(chunk).__name__` + repr 前缀）后真相大白：
```
py_type=bytes  repr_head=b'event: content_block_start\ndata: {"type":...,"content_block":{"type":"tool_use","name":"AskUserQuestion"...
```
- chunk 全是 **SSE 序列化后的 `bytes`**（`b"event: <type>\ndata: <json>\n\n"`），不是 Anthropic 事件 dict。
- 而且**明确看到了 AskUserQuestion 的 tool_use 块**——它**一直经过 hook**！

**为什么是 bytes**：`anthropic_messages` 流式路径里，`adapters/transformation.py:303` 返回 `anthropic_wrapper.async_anthropic_sse_wrapper()`，该 wrapper（`streaming_iterator.py:~798`）`yield payload.encode()` —— **SSE 序列化发生在 iterator hook 之前**。所以 hook 拿到的是 bytes。

**我的 bug**：`stream_transform` 用 `_chunk_get(chunk,"type")`（即 `getattr(bytes,"type",None)`）判断事件类型 → 对 bytes 恒返回 None → 所有 chunk 落到「原样透传」→ `block_start` 恒空、补全永不触发。

### 关键教训
- `stream_transform_entered>0` + `block_start` 空 **不代表工具调用不走这条流**——先加 `chunk_shape` 诊断确认 chunk 是 dict 还是 bytes/str，别急着归因于路由。
- **request_seen 有记录本身就证明请求走了代理**（`async_pre_call_hook` 只在请求经 litellm 时调用）。第一轮结论与这个证据自相矛盾，本应当场察觉。

## 修复：解析 SSE bytes → dict → 状态机 → 重新序列化
`_sse_parse(chunk)` 把 bytes/str 的 `data: <json>` 行解析成事件 dict（不可解析返回 None→透传）；状态机在 dict 上判断/补全；`_sse_serialize(ev, template_chunk)` 按原 chunk 形态（bytes/str/dict）重新序列化。详见 `streaming-rewrite.md`。

## 已验收生效
清空审计，发一个 AskUserQuestion，`stream_fix.audit_file`（`probe-logs/stream-patched.jsonl`）出现 `{"_diag":"patched","tool":"AskUserQuestion"}` → 补全真实触发、调用成功。多 question 场景同样触发。**这也终结了「AskUserQuestion 不走代理」的错误说法——本会话的调用确实经此 hook。**

## 扩展到其他工具/字段
`stream_fix.tools` 是通用规则表，可加任意工具：
```json
"SomeTool": {"items_key": "items", "copy_within_items": [{"src":"a","dst":"b"}]}
```
`items_key` 为 null 时可扩展成对顶层字段补全（当前实现聚焦 items 内补全，需要顶层规则时扩 `_apply_item_fixes`）。

## 复发（2026-07）：两条路径补全能力不对等 + coerce/fields 顺序是正确性硬依赖

同样的 `questions[0].question is missing` 又出现。上面修的是**缓冲路径**（正常 tool_use 块 → 缓冲 partial_json → `apply_item_fixes` 补全）。但 AskUserQuestion 还有**第二条路径**：模型（gpt 尤甚）把 `<invoke name="AskUserQuestion">` 泄漏进 text block，走 `convert_text_invoke` 的 `synth_tool_use_events` 合成 tool_use。这条路径**完全绕过了 `apply_item_fixes`**。

### 两个叠加的坑
1. **泄漏路径不跑补全**：`extract_invoke_from_text` 把 `<parameter name="questions">` 的值原样塞进 `input_dict["questions"]`，那是个**字符串**（parameter 值本就是文本），synth 直接发出 → 客户端拿到 `questions` 是字符串、且元素缺 `question`。
2. **修复顺序是正确性硬依赖，不是偏好**：`apply_item_fixes` 内部必须 `coerce(string->array)` **先于** `fields(header->question)`。因为 `fields.apply_field_fixes` 用 `isinstance(items, list)` 判断，若 questions 还是字符串就**静默跳过、什么都不补**（实测反序时 `question=None`）。必须先 coerce 把字符串还原成 list，fields 才遍历得到 items。

### 修复
- 泄漏路径的 tool_use 段接上 `apply_item_fixes`（仅对配了补全规则的工具、非 probe_only），与缓冲路径对齐；补全生效记 `patched via=leaked`。
- 顺序契约在代码里显式标记（`fixes/__init__.py` + `fields.py` docstring 写明「正确性硬依赖」及反了的后果），并用 `tests/test_fixes.py` 锁死：`test_stringified_questions_get_question_filled` 用真产品码守契约，谁把两步对调该断言即红（已做 mutation 验证：动态构造反序版本，`question` 果然变 `None`）。

### catch-all 审计补上「失败静默不留痕」的盲区
原来只有补全**成功**才记 `patched`，泄漏路径只记 `invoke_converted`（不含工具名/内容）——AskUserQuestion 缺字段静默漏到客户端时**零审计**，无法从历史日志实锤走哪条路。新增 `tool_out_integrity` 事件：缓冲/泄漏两路径**出站前都记一次**结构完整性（`items_type`、`n_items`、哪些条目仍缺必填字段）。`missing` 非空 = 发给客户端时仍缺字段 → 回归警报；健康时恒空。生产 smoke 实测：真实 AskUserQuestion 请求产生 `tool_out_integrity path=buffered items_type=list missing=[]`，坐实探针在线激活。

### 仍存的盲点（deferred）
`header` 也缺时 `question<-header` 补不出（源字段为空）。没加占位兜底（塞垃圾问题给用户更糟），靠 `tool_out_integrity` 的 `missing` 暴露该 case，遇到再针对性决策。

### 部署确认教训
排查末尾发现 hook 部署已从 `~/.claude/litellm/` 迁到 `~/.config/litellm/`（与 `refs/ai-agents/litellm/config/hookpkg` 同一 inode）。旧位置被清空只剩 config symlink，一度以为改动丢了。**核实而非假设**：`ls -lid` 比对 inode + `reload-audit.jsonl` 的 `reload_ok n_reloaded` + 端到端探针激活，三重坐实运行进程真加载了修复。见 `hot-reload-verification.md`。
