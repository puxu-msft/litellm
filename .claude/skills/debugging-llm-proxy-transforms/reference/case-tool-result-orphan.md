# 案例：tool_result 孤儿（转换层吞块）

## 症状
经 `github_copilot` provider 打 opus，请求报：
```
messages.N: `tool_use` ids were found without `tool_result` blocks immediately after: toolu_xxx.
Each `tool_use` block must have a corresponding `tool_result` block in the next message.
```

## 误判与教训
一开始以为是**请求输入**里就有孤儿，在 `async_pre_call_hook`（L1）写了孤儿修复。**白忙**——探针证明 L1 拿到的输入干净、tool_use 与 tool_result 相邻正确。而且报错的 `messages.N` 索引和真实位置对不上（N=236 vs 真实 262），这本身就是「问题在转换层、消息被重排」的信号。

## 定位（关键步骤）
1. **确认 provider 走哪条路**：`github_copilot` 无 `anthropic_messages_config`（查 `utils.py:_get_provider_anthropic_messages_config_cached`，只有 ANTHROPIC/BEDROCK/VERTEX_AI/AZURE_AI/MINIMAX/DEEPSEEK 有）→ 走 `LiteLLMMessagesToCompletionTransformationHandler`，L2 把 Anthropic 转成 OpenAI chat/completions。
2. **在 L2 转换函数返回前打只读探针**（对比 src/out messages，检测转换后是否出现孤儿）。site-packages patch，因为官方 hook 拿不到「转换后 OpenAI 格式且能对比转换前」的点。
3. 探针抓到：转换后 `assistant_index=235` 的 tool_call 无紧邻 tool 回应；且全局搜不到该 id 的 tool message → **tool_result 被转换吞了**。
4. 解剖那个 tool_result：内层 `content` 是 10 个 `{"type":"tool_reference","tool_name":...}` 块（来自 Claude Code 的 `ToolSearch`/deferred tools 特性）。

## 根因
`translate_anthropic_messages_to_openai`（`adapters/transformation.py`）处理 `tool_result` 的多项 content 分支：
```python
combined_content_parts = []
for c in content_items:
    if c.get("type") == "text": combined_content_parts.append(...)
    elif c.get("type") == "image": combined_content_parts.append(...)
    # ← 没有 else！tool_reference 落空
if combined_content_parts:   # ← 空列表，永不进入
    tool_result = ChatCompletionToolMessage(...)
    tool_message_list.append(tool_result)
```
内层全是未知块类型 → `combined_content_parts` 空 → **tool message 从不创建 → tool_result 静默丢失 → 配对 tool_use 变孤儿**。单项分支（`len==1`）同理，dict 块非 text/image 时也落空。

## 修复
保证**每个 tool_result 至少产出一个** OpenAI tool message，未知块降级为空内容：
```python
# 多项分支：去掉 if combined_content_parts 门槛
tool_result = ChatCompletionToolMessage(
    role="tool", tool_call_id=content.get("tool_use_id",""),
    content=combined_content_parts if combined_content_parts else "")
tool_message_list.append(tool_result)
# 单项分支：dict 块非 text/image 时加 else 兜底，产出 content="" 的 tool message
```

## 验证
用真实 265 条 payload 离线跑 `LiteLLMAnthropicMessagesAdapter().translate_anthropic_messages_to_openai(src, model)`，断言 `orphans == []`、目标 id 有对应 tool message。

## 备选修复位置
也可用 `async_pre_call_deployment_hook`（L3，官方，不改 site-packages）在转换后补孤儿 tool message（`deployment_probe.fix_orphans`）。本案选 patch 根治（更接近根因、适合上报上游），deployment hook 仅作观测+备用。这是 litellm 真实 bug，值得上报 BerriAI/litellm。
