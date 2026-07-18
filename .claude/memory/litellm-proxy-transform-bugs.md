---
name: litellm-proxy-transform-bugs
description: litellm(4143) 的 Anthropic↔OpenAI 转换层会损坏请求/响应；用官方 CustomLogger 全链路 hook(转换前/后/失败/流式)探针定位+修复，够不到才 patch site-packages。已修 5 类 bug(孤儿 tool_result/缺字段/invoke 泄漏/thinking 损坏/退化重复裁剪)。落点已迁 ~/.claude/litellm/(模块化 hookpkg 包)。区别于 copilot-api(4141) 的模型解码侧污染
metadata: 
  node_type: memory
  type: project
  originSessionId: afd50054-2ea7-48c7-97c2-e9163f464847
---

排查经 litellm 代理（`localhost:4143`，`github_copilot` provider 把 opus 经 copilot 后端当 Claude 跑）的请求/响应损坏问题。**方法论与完整案例已沉淀为 skill [[debugging-llm-proxy-transforms]]**（`~/.claude/skills/`），此处仅留触发指针，不重复详情。

**核心认知（够用即止，详情看 skill）：**
- `github_copilot` 无 `anthropic_messages_config` → `/v1/messages` 请求经 L2 转成 OpenAI chat/completions，copilot 后端再转回 Anthropic（**双重转换**）。报错的 Anthropic 措辞/索引可能来自最内层，与客户端看到的对不上——这是「问题在转换层」的信号。
- **先探针定位是哪一层坏**（请求前/转换后/失败/流式），再修。血泪：曾在请求侧 `async_pre_call_hook` 写孤儿修复，结果 bug 在转换后，白忙。
- 用官方 `CustomLogger` 全链路 hook（`async_pre_call_hook`/`async_pre_call_deployment_hook`/`async_post_call_failure_hook`/`async_post_call_streaming_iterator_hook`）观测改写，**不改 site-packages**；够不到才打幂等 patch。
- **陷阱**：`streaming_iterator_hook` 靠 `vars(cls)` 检测，必须定义在薄壳类上且**重启** litellm 才生效。改 `hook_impl.py` 逻辑则热重载。
- **陷阱（血泪）**：改 L5 流式前先加 `chunk_shape` 诊断确认 chunk 真实类型——`anthropic_messages` 流式路径 SSE 序列化在 hook **之前**（`transformation.py:303` async_anthropic_sse_wrapper），hook 拿到的是 **SSE bytes 不是 dict**，按 dict 解析会全部落空、修复静默失效。曾误判为「AskUserQuestion 不走代理」，实为没解析 bytes；`request_seen` 有记录已证明请求走了代理。修法：`_sse_parse`→dict→状态机→`_sse_serialize` 回 bytes。已用 `stream_fix.audit_file` 记 `patched` 事件验收生效。

**已修的真实 bug**：
1. litellm 转换器把内层全是 `tool_reference` 块的 `tool_result` 静默吞掉 → tool_use 孤儿 → `tool_use ids ... without tool_result`。patch 在 `~/.config/litellm/patches/`。
2. 流式响应里工具调用漏填必填字段（AskUserQuestion 的 `questions[].question`）→ L5 `stream_fix` 解析 SSE bytes、缺字段从 `header` 补 `question`、重序列化。已验收。
3. 工具调用 `<invoke name=...>` 泄漏成 text（客户端不执行）→ `stream_fix.convert_text_invoke` 把 text block 拆成 `[text+tool_use]`、传播 index 偏移、改 `message_delta.stop_reason=tool_use`、白名单(glob,如 `mcp__plugin_*`)防误伤。转换记 `invoke_converted` 审计。已上线。
4. thinking block 排列损坏（客户端发回连续 thinking、空 signature）→ 请求侧 `process` 修复(`hookpkg/thinking.py`,配置 `fix_thinking`)：连续插空格、空 signature 转文本/删除、可选 strip_all。区分 signature 空(错误,修) vs 内容空(新版常态,不动)。
5. 上游退化重复输出（一个 text block 连吐 ≥4 段短小完全相同片段，如空行分隔的 court×N）→ `stream_fix.degen_trim` 折叠为「首段+notice」。纯函数 `hookpkg/degen.py`(groupby 游程,**红线:先滤空段再 groupby**,否则 `\n\n\n\n` 真空段夹入打断游程漏检;两级分段 `\n\n` 默认+`\n` 严阈值回退)。**关键设计**:文本块缓冲**无条件化**(不再由 convert_invoke 门控)→ stream_fix 常开即全局按 block 成段(非流式,用户明确接受);折叠顺序先 degen 后 invoke 提取(硬门控 `if convert_invoke`)。**合并态评审抓到丢内容 bug**:先清 tbuf 再折叠,折叠抛异常→except 时 tbuf 已空→整块文本蒸发;修=折叠/提取包局部 try 降级发原文 + `_as_pos_int` 阈值兜底。25 测试。计划 `docs/plan/degeneration-trim.md`。

**实现落点(现)**：`~/.claude/litellm/`（**注意已从 `~/.config/litellm` 迁移**）。已模块化为 `hookpkg/` 包(薄壳 `hooks.py`→`hookpkg/__init__.py` 五个官方 hook 入口;`stream.py` 流式状态机;`config.py` 热读+`default_degen_trim()`;`sse.py`/`invoke_convert.py`/`degen.py`/`fixes/` 工具;`thinking.py`);`hook_impl.py` 已删。配置 `hooks.config.json`,探针 `probe-logs/`,热重载 `./reload.sh`(SIGUSR2,新模块需进 `reload.py` 的 `RELOAD_ORDER`),测试 `python3 -m unittest hookpkg.tests.*`(标准库 unittest,无 pytest)。

区别于 [[tool-output-corruption-via-copilot-proxy]]（那是 `localhost:4141` copilot-api，模型**解码侧**误读干净输入，非转换层结构 bug）。相关 [[zipfs-project-goal]]。
