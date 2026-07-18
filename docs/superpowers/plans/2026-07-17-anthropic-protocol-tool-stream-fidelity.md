# Anthropic 协议、工具与流式保真实施计划

状态：全部完成并 live 验证

关联 spec：`docs/superpowers/specs/2026-07-17-anthropic-protocol-tool-stream-fidelity-design.md`

## Phase 1：核心事件状态机

状态：已完成

1. 为 Chat wrapper 增加 sync/async、自然/异常 EOF 回归，并统一终结事件队列
2. 修 direct Responses fallback + `response.created` 双 `message_start`
3. 为 direct Responses 跟踪 open block，统一 completed/EOF/异常终结
4. 规整 reasoning orphan delta、function argument orphan、orphan done 与 duplicate added
5. 锁定 function arguments 的正常 delta、orphan 缓冲、done-only 三种互斥来源
6. 映射 `disable_parallel_tool_use` 到 `parallel_tool_calls`
7. 重跑 Chat 与 direct Responses adapter 子树

## Phase 2：严格客户端与 live 验收

状态：已完成

1. 运行 `tests/e2e/github_copilot_reasoning/test_anthropic_sdk_reasoning.py` 的 billed Anthropic SDK 组
2. 保存原始 SSE 事件序列，断言 `message_start == 1`、所有块闭合、thinking_delta 可见、最终 carrier 可解码
3. 驱动真实 Claude Code 两轮工具调用，核对 tool_use id/name/input 与下一轮 tool_result 配对
4. 若 live 与离线 oracle 冲突，优先以最终 wire 与严格 SDK 为准，回到对应 adapter 修正

结果：strict Anthropic SDK 的非流式 carrier、显式 detailed thinking summary、流式最终 carrier 全部通过；默认 `summary=auto` 是 best-effort，测试已改为显式 `detailed`，不再错误要求 auto 每次非空。真实 Claude CLI 存储可解码 carrier，并在同一 transcript 中保留 `Bash` tool_use 的 name、非空 input.command、id 与匹配 tool_result。valid carrier replay、tampered carrier rejection、跨轮 reasoning continuity 全部 live 通过

## Phase 3：SSE 字节帧迁移 PoC

状态：已完成，结论为双边界共享 primitive，不删除任一边界

1. 用真实 chunk shape 探针确认 Chat 与 direct Responses 两条路径首次持有最终 SSE bytes 的核心边界
2. 将 hookpkg `_reassemble_sse_frames` 的确定性逻辑抽成候选核心 normalizer，不先删除 hook 实现
3. 对每个字节切点验证帧重组等价；覆盖多帧同 chunk、UTF-8 切点、尾部截断与合法 ping/comment
4. live 对照核心 normalizer 与 hook 审计；确认无双重缓冲后再裁决迁移和删除顺序

结果：hook `async_post_call_streaming_iterator_hook` 位于最终 `create_response`/keepalive normalizer 之前，必须先重组 raw provider chunks 才能解析和改写；core normalizer 位于 callback 输出之后，必须保证 keepalive 只在完整 frame 之间注入。两者职责不同。hook 已改为复用 core `find_frame_delimiter` 与 `DEFAULT_MAX_UNTERMINATED_BYTES`，保留 mixed bytes/str/dict 与 `reassembled_split_frame` 审计。core 每字节切点、UTF-8、CRLF、多帧、超限测试与 hook 38 个流式测试通过

## Phase 4：剩余工具载体 PoC

状态：已完成，结论为 `tool_result.is_error` 无可用无损载体

1. 构造 `tool_result.is_error=true` 的 Anthropic 请求，分别观察 Chat 与 direct Responses 发往 Copilot 的最终载荷及回转结果
2. 评估标准字段、provider-specific 字段和显式版本化 carrier；拒绝会污染模型可见内容的隐式前缀
3. 冻结载体后先写双向往返与篡改/未知版本测试，再实现
4. 盘点 hookpkg 的 schema 补全规则；只迁移纯协议规则，业务工具规则继续配置化

结果：Responses 标准 function_call_output replay 为 200，增加私有 `is_error` 后为 400；Chat 私有字段被 Copilot 接受并确实离开 proxy schema，但 Sonnet 行为差分为 private=OK、standard=OK，字段被忽略且无法从响应恢复。两条标准协议都没有 error bit 等价槽位。保留 content/call id，明确记录 error bit 有损；拒绝内容前缀、JSON 包装或未经验证的 provider-specific carrier

## Phase 5：收尾

状态：已完成

1. 运行相关单元、类型检查与 pre-commit
2. 由独立 reviewer 检查协议序列、工具参数单次性、文档与代码一致性
3. 更新 BACKLOG、TRACKING、reasoning bridge 与 `config/docs/illformed-fix.md`
4. 将完成项从 BACKLOG 移入稳定 live doc，保留仍需 live/PoC 的具体门禁

结果：相关单元、Ruff、editor diagnostics、diff check 全绿；独立终审无 blocker/major。Claude CLI e2e 的 transcript oracle 已按本次唯一 `tmp_path` project 目录隔离，消除并行 Claude 会话造成 carrier/tool 配对假绿的可能