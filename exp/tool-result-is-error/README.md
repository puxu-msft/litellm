# `tool_result.is_error` 跨协议载体 PoC

日期：2026-07-17

状态：完成

## 问题

Anthropic `tool_result` 有可选布尔字段 `is_error`。OpenAI Chat tool message 与 Responses `function_call_output` 没有标准等价字段。本 PoC 判断 GitHub Copilot 是否允许用同名私有字段保留错误语义

## Oracle

每条路径先让真实 Copilot backend 生成合法 function call，再回放对应 tool result。标准请求与仅增加 `is_error: true` 的请求共享同一结构。记录 HTTP 状态和模型行为，不记录 response id、call id、encrypted reasoning、prompt 或凭据

Responses 使用显式 item replay，而不是 `previous_response_id`。后者在本部署对标准与 private 请求都返回 400，不能区分载体是否有效

## 结果

### Responses

- 标准 `function_call_output`：HTTP 200
- 同一 item 增加 `is_error: true`：HTTP 400

结论：Copilot Responses 不接受该私有字段，不能作为载体

### Chat

- 标准 tool message：HTTP 200
- 同一 message 增加 `is_error: true`：HTTP 200
- GitHub Copilot Chat request transformation 原样保留 message keys，spend log 也确认 private 请求在 proxy 入口仍带该字段
- Sonnet 行为差分使用中性/成功 tool output，并要求只依据 error marker 返回分类；private 与 standard 均返回 `OK`

结论：Copilot Chat 容忍该字段，但目标模型忽略其错误语义；响应也不回显 tool result，无法恢复原始 bit

## 决策

- 不实现私有字段载体
- 不用文本前缀或 JSON 包装污染模型可见 tool output
- Chat 与 Responses 都保留 tool result content 和配对 id
- `is_error` bit 明确记录为协议不可表示的已知有损
- 转换测试固化上述边界，防止未来把“字段被接受”误报为“语义已保真”

## 相关

- `docs/superpowers/specs/2026-07-17-anthropic-protocol-tool-stream-fidelity-design.md`
- `docs/superpowers/plans/2026-07-17-anthropic-protocol-tool-stream-fidelity.md`
- `tests/test_litellm/llms/anthropic/experimental_pass_through/adapters/test_anthropic_experimental_pass_through_adapters_transformation.py`
- `tests/test_litellm/llms/anthropic/experimental_pass_through/responses_adapters/test_responses_adapters_transformation.py`
