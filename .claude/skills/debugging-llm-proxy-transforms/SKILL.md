---
name: debugging-llm-proxy-transforms
description: >-
  当你在 litellm 代理（尤其把非 Anthropic 模型/copilot 后端当作 Claude 运行的 github_copilot
  provider）上遇到「请求/响应在代理转换层被损坏」类问题时，触发本 skill。典型症状：Anthropic
  `/v1/messages` 经代理打到 OpenAI 系 provider 后报 `tool_use ids were found without tool_result
  blocks immediately after`、`tools are required when tool choice is specified`、工具调用参数缺字段
  被客户端 schema 校验拒绝、cache_control 字段不被上游接受、模型漏填必填工具参数、工具调用
  `<invoke name=...>` 泄漏成普通文本(客户端不执行)、thinking block 排列损坏(连续 thinking、
  空 signature)。核心能力：用
  litellm **官方 CustomLogger 全链路 hook**（pre_call/pre_call_deployment/post_call_success/
  post_call_failure/streaming_iterator）在「转换前/转换后/成功后/失败时/流式响应」五个点观测与
  改写请求响应，无需改 site-packages；必要时再对 site-packages 打幂等 patch。核心方法论：**先定位是哪一层损坏（请求侧？转换层？响应侧？chunk 是 dict 还是 SSE
  bytes？）——确定性转换先离线最小复现，活管线属性再用 CustomLogger 探针，最后才 patch；绝不凭结构推断盲写修复**。
---

# Debugging LLM Proxy Transforms / 调试 LLM 代理的请求响应转换

搞清楚一个经 litellm 代理（特别是 `github_copilot` provider，把 opus/gpt 等经 copilot 后端当作 Claude 运行）的请求/响应，**在代理的哪一层被损坏**，并用**官方 hook** 或 **site-packages patch** 修复。

本 skill 来自一次多轮实测排错：从「AskUserQuestion 报 `question is missing`」和「`tool_use ids ... without tool_result`」两个症状出发，逐层探针定位到 litellm 的 Anthropic↔OpenAI 转换层 bug，并建成全链路 hook 探针体系。

## 第一原则：先定位「哪一层」，再修

一个请求经 litellm 代理有多层，任何一层都可能损坏数据。**盲目在某一层写修复，极易白忙**——本 skill 最大的教训就是：花大力气写了请求侧孤儿修复（`async_pre_call_hook`），结果发现 bug 在**转换后**的载荷里，pre_call 层根本够不到。

排错顺序永远是：**先判断故障属于确定性单层转换、还是活管线属性（判据见下节）→ 确定性的先用最小离线复现，活管线属性才架探针 → 看是哪一层的数据坏了 → 在能触及那一层的 seam/hook/patch 修 → 复现验证**。核心不变：先取得该层一手输入输出，绝不盲修。

### 一个请求在 litellm 里的分层（github_copilot / anthropic_messages 路由）

```
客户端(Claude Code) --Anthropic /v1/messages 原始载荷-->
  [L1] async_pre_call_hook          <- 转换前, Anthropic content 块格式
  [L2] Anthropic→OpenAI 转换         <- translate_anthropic_messages_to_openai (BUG 高发区)
  [L3] async_pre_call_deployment_hook<- 转换后/发出前, OpenAI tool_calls 格式
       --发给 copilot 后端-->  copilot 再转回 Anthropic 发给真模型 (双重转换!)
  [L4] OpenAI→Anthropic 响应转换      <- 流式: 逐块 input_json_delta
  [L5] async_post_call_streaming_iterator_hook <- 流式响应, 发往客户端前
  [L6] async_post_call_failure_hook  <- 若失败
客户端做本地 schema 校验 (如 AskUserQuestion 的 questions[].question 必填)
```

**关键认知**：`github_copilot` 是 OpenAI 系 provider，**没有 `anthropic_messages_config`**，所以 `/v1/messages` 请求会在 L2 被转成 chat/completions；copilot 后端又把 opus 请求转回 Anthropic——**一次请求经历两次格式往返**，报错的 Anthropic 措辞可能来自最内层，索引号也和客户端看到的对不上（这本身是「问题在转换层」的信号）。

## 先在最小 seam 离线复现，再决定要不要架 hook

很多故障是**确定性的单层转换**，不用碰活 proxy——离线最小复现确定、无网络、可断点、可反复。判据不是「请求侧/响应侧」，而是「确定性单层转换」vs「只在活管线里才显形的属性（真实路由 / callback 顺序 / 真实 iterator 形态 / HTTP 分块 / 时序 / 部署配置 / 要证明线上真实 wire）」——前者优先离线，后者才上 hook。

**离线手段（不碰活 proxy）：**

- **introspection**：`get_llm_provider(model,…)` 返回规范化 `(model, provider, dynamic_api_key, api_base)`；`get_optional_params(…)` 给经支持性检查 + provider 映射后的 optional params **中间结果**（不是最终 HTTP payload——消息 / headers / URL / `transform_request` 还在后面）；`get_supported_openai_params(model)` 给该 model 声明支持的参数（无映射可能返 `None`）。「参数莫名丢了」多半这步现形。
- **看会发给 provider 的请求**（比 optional_params 完整）：`return_raw_request(endpoint, kwargs)`（`litellm/utils.py`，**BETA，主要测过 `/chat/completions`**）跑真实请求构造路径，从 `pre_call` 的 `raw_request_typed_dict` 取请求快照（provider-facing URL / headers / body）。它塞假 api key 并捕获随后的失败——但**可能真的对 provider 发起一次（被认证拒的）连接**，不是纯离线；快照也不保证所有路径下与最终字节完全一致（如流式在 `pre_call` 后才追加 `stream`）。proxy 也暴露了对应端点。
- **直调转换 seam**：provider 的 `transform_request` / `transform_response` 可脱离 proxy 调，litellm 测试套件大量这么做（`tests/llm_translation/test_anthropic_completion.py`、`tests/test_litellm/llms/github_copilot/test_github_copilot_transformation.py`）。但它们收**拆开的规范化参数**（optional_params/litellm_params/headers、`httpx.Response`+`ModelResponse`+logging stub），不是把原始 JSON 直接塞进去——照对应 provider 的既有测试构造最小参数。注意：Anthropic `/v1/messages`→OpenAI 的**外层** seam 是 Anthropic Messages adapter（`experimental_pass_through/adapters/handler.py`），不是目标 provider 的 `transform_request`（`GithubCopilotConfig` 根本没覆盖它）。连流式 SSE 序列化都能离线迭代（`adapters/streaming_iterator.py` 的 wrapper 有直接测试），劈帧也能人工切点离线回归。
- **`mock_response` / `mock_tool_calls`**：传给 `completion()` 会在 provider 调用**之前**直接造标准化 `ModelResponse`（`main.py` 的 `mock_completion`，dict→`ModelResponse(**d)` / str→`choices[0].message.content` / tool_calls 直接塞 message），**不经过 provider 的 `transform_response`**。所以它是跳过网络、测响应**周边**路径（logging / cache / router / callback / agentic loop）的，**不是**测 provider 响应解析、也分不清「litellm 转换 vs 上游」。要测响应解析，自己构造 `httpx.Response` 直接调 `transform_response`。

这些够不到时才回到下文 L1–L6 hook；hook 再够不到才 patch site-packages。

**原生日志能替代吗——看故障在哪**：内建标准日志 / OTEL / `log_raw_request_response` 给请求侧语义 `messages` + 转换后 `raw_request` + **聚合后的语义 ModelResponse**，**够不到发往客户端的 Anthropic SSE wire**（回转在 logging 之外，流式成功日志走聚合 `complete_streaming_response`）。要看最终 wire 或证明线上真实分块 / 时序，仍得用 `async_post_call_streaming_iterator_hook`（这属官方 hook，不是「原生被动日志」）；但确定性的 wire 序列化本身可离线测，不必事事上活代理。

## 官方 hook 是正道，不是野路子

litellm 官方 `CustomLogger` 暴露了全链路 hook（`callbacks: hooks.proxy_handler_instance` 注册，是文档机制）。**能用官方 hook 就别改 site-packages**。各点见 `reference/hook-points.md`。

| hook | 时机 | 能改数据 | 用途 |
|---|---|---|---|
| `async_pre_call_hook` | 转换前 | ✅ | 改原始 Anthropic 请求 |
| `async_pre_call_deployment_hook` | 转换后/发出前 | ✅ | **覆盖转换后盲区**（monkeypatch 曾经的观测目标） |
| `async_post_call_failure_hook` | 失败时 | 只观测/改错误 | 抓报错瞬间的载荷+异常 |
| `async_post_call_success_hook` | 成功后 | 只观测 | 非流式回归 |
| `async_post_call_streaming_iterator_hook` | 流式响应发出前 | ✅ 改流 | 改写流式 chunk（补工具参数等） |

**易错点**：`async_post_call_streaming_iterator_hook` 与 `async_post_call_response_headers_hook` 靠 `vars(cls)` 检测覆写——**必须直接定义在 hook 类上**（继承无效），否则 litellm 走 fast-path 跳过整条链。其余 hook 无条件遍历 `litellm.callbacks`，签名对上即触发。

## 实测定位的真实 bug（案例）

1. **`tool_use` 孤儿（转换层吞 tool_result）**：当某 `tool_result` 的内层 `content` 全是**未知块类型**（实测 `tool_reference`，来自 Claude Code 的 `ToolSearch`/deferred tools），L2 转换器的 text/image 分支都不命中，`combined_content_parts` 为空 → **整个 tool message 不被创建 → tool_result 被静默吞掉 → 配对的 tool_use 变孤儿**。修复：保证每个 tool_result 至少产出一个 OpenAI tool message（未知块降级空内容）。详见 `reference/case-tool-result-orphan.md`。

2. **流式工具参数缺字段**：模型经代理生成工具调用时漏填必填字段（如 AskUserQuestion 的 `questions[].question`）。修复：L5 流式 hook 做状态机改写，缓冲 tool_use 块 → 重组分片 JSON → 缺字段从同级字段补（`question ← header`）→ 重发。详见 `reference/case-stream-tool-fix.md` 与 `reference/streaming-rewrite.md`。

3. **工具调用泄漏成文本**：模型把本该是 tool_use 的 `<invoke name=...>` 吐进 text block，客户端当文本显示、工具永不执行。修复：L5 识别泄漏，把 text block 拆成 `[text + tool_use]`、传播 index 偏移、改 stop_reason 让客户端执行、白名单(glob)防误伤。详见 `reference/invoke-conversion.md`。

4. **thinking block 排列损坏**：客户端把损坏的 thinking block 排列作为历史发回(连续 thinking 相邻、signature 为空)，后端报错。修复(请求侧 `process`)：连续 thinking 间插空格文本块；空 signature 转文本(内容空则删,删后 content 空则空格占位)；可选 strip_all 一键剥离。**关键区分**：signature 空是错误(修)，thinking 内容空是新版常态(不动)。见 `hookpkg/thinking.py`。

5. **一次调用变成两个相同 tool_use(客户端连发两个内容完全相同的工具,如 AskUserQuestion)**：可能是转换层把一个 tool_call 拆成两块,也可能上游真发了两个——**从聚合数据分不清**(`stream_chunk_builder` 按 `tool_calls[].index` 合并,俩 index=0 的相同调用聚合后也只剩 1 个,别据此排除某机制)。转换层在 `content_block_start` 时刻**拿不到完整 input**,无法区分「重复」与「参数不同的合法背靠背」(既有 `test_parallel_tool_calls.py::interleaved` 就是同 id、不同参数、全 index=0 的合法两块),故转换层 patch 不安全、已撤回。改在 **L5 末端**用**完整累积 input** 去重:丢弃与紧邻前一个 tool_use「name+完整 input 逐字节相同」的块、后续 index 递减。根因无关、构造上安全(input 不同永不误伤)。观测:block_audit 增强的 `dup_tools_in`(按 name,弱信号,合法并行也命中) vs 审计事件 `dropped_duplicate_tool_use`(真去重)。见 `hookpkg/dedup.py`。

6. **SSE 帧被 chunk 边界劈开(半截 `event:` 泄漏 → `JSON Parse error: Unexpected identifier "event"`)**：上游(copilot 双重转换 + httpx 分块)不保证 chunk 对齐 SSE 帧边界——一个 `event: X\ndata: {...}\n\n` 帧可能被劈到相邻两个 chunk,或多帧挤进一个 chunk。逐 chunk 的 `sse_parse` 只认单帧、且**只取第一个 `data:` 行** → 半截帧丢内容、多帧丢后续;半截 `event:` 行透传给客户端,其 `JSON.parse` 报 `Unexpected identifier "event"`(**区别于**截断半截 JSON 串报的 `Unterminated string`——见血泪陷阱段)。**block/事件层缓冲救不了**:劈帧根本 parse 不出事件,够不到那层。修复:流入口做**字节级帧重组**——carry 累积到完整帧(`\n\n` 结尾)才逐帧下发,尾部半截留到下个 chunk 拼接;真·流末尾截断仍交 `is_truncated_json_frame` 丢弃。正样本对照(遍历每字节切点不丢文本)+ 正向审计 `reassembled_split_frame`(n=拼接次数,真实流量出现即证 live)。详见 `reference/case-sse-frame-reassembly.md`。

## 方法论硬教训（本轮血泪，务必内化）

- **验证部署要看代码级产物,不看「跑过部署命令」**：config 热读 ≠ 代码 reload。双重确认:stdout 的 `reloaded N modules via SIGUSR2` 日志(机制层)+ 新代码独有的落盘字段/审计事件在真实流量出现(行为层)。本次曾据一个间接推断误判「reload 坏了」,被 stdout 日志推翻——**别拿间接推断否定可直接观测的机制,先找一手信号**。详见 `reference/hot-reload-verification.md`。
- **回溯请求查代理侧,别查客户端 transcript**：Claude Code 的 transcript 是**客户端渲染/去重后**的产物,会以「沉默的省略」骗你(两个相同 AskUserQuestion 被客户端吞成看不见 → transcript 全空 ≠ 没发生)。真相源:block_audit 流式记录、postgres `LiteLLM_SpendLogs`(`store_prompts_in_spend_logs`)。但注意 `LiteLLM_SpendLogs.response` 是**非流式聚合**(OpenAI 格式),流式独有的拆块/畸形不体现其中。
- **聚合/摘要无法区分「会被合并的机制」**：见上「两个相同 tool_use」——聚合按 index 合并,故不能用「聚合只有 1 个」排除「上游发了两个」。别让摘要数据支撑一个过度自信的排除。
- **在「有区分信号的那一层」修,而非最靠近症状处**：区分「重复 vs 合法背靠背」的唯一信号是完整 input,只有 L5 末端拿得到 → 修复就得放那,转换层结构上做不到。
- **改共享/核心代码前,先跑全套既有测试**：既有测试编码着你不知道的契约(`interleaved` 用例替我挡下了不安全的转换层 patch)。
- **根因未定但症状明确且修复「构造上安全」时,可在边界做根因无关的症状修复**:按完整 input 逐字节去重,字节相同才丢——无论重复来自转换层还是上游,都在发客户端前的最后一层修掉,且合法并行永不误伤。

## 血泪陷阱：先确认 chunk 的真实 wire 形态（SSE bytes vs dict）

排查流式工具参数问题时，我一度反复报 `question is missing`，写了完整的流式修复却「不生效」。诊断走了**两轮**，第一轮结论错了：

- **错误的第一轮**：看到 `stream_transform_entered>0` 但 `block_start` 统计空，就归因为「当前会话不走代理 / AskUserQuestion 是客户端工具不走流」。**错。** 犯了没检查 chunk 实际类型就归因的毛病。而且这与「`request_seen` 有记录（证明请求走了代理）」自相矛盾，本应当场察觉。
- **正确的根因**：加一级 `chunk_shape` 诊断（记 `type(chunk).__name__` + repr）后发现——**chunk 全是 SSE 序列化后的 `bytes`**（`b"event: <type>\ndata: <json>\n\n"`），不是 Anthropic 事件 dict，且里面**明确有 AskUserQuestion 的 tool_use 块**（一直经过 hook）。原因：`adapters/transformation.py:303` 的 `async_anthropic_sse_wrapper` 在 hook **之前**就 `yield payload.encode()`。我的状态机用 `_chunk_get(chunk,"type")`（=`getattr(bytes,"type",None)`）判断，对 bytes 恒 None → 全部透传 → 补全永不触发。

**教训**：
1. 修 L5 流式改写前，**先加 `chunk_shape` 诊断确认 chunk 是 dict 还是 bytes/str**。SSE 序列化常在 hook 之前，hook 多半拿到 bytes。
2. `stream_transform_entered>0` + `block_start` 空 **不代表工具调用不走流**——可能只是你按错误的类型解析。
3. `request_seen` 有记录本身就证明请求经过代理（`async_pre_call_hook` 只在请求经 litellm 时调用），别忽视这个反证。
4. **逐 chunk 解析器天生拼不回被劈开的帧**：SSE 帧不保证对齐 chunk 边界，`sse_parse(chunk)` 逐 chunk 且常只取首帧 → 半截丢内容、多帧丢后续、半截 `event:` 泄漏。所以流入口要**先做字节级帧重组**（carry 到 `\n\n` 再逐帧下发），再进状态机——事件/block 层缓冲在这层之上，救不了。见案例 6 / `reference/case-sse-frame-reassembly.md`。

修复：`_sse_parse` 解析 bytes→dict、状态机在 dict 上跑、`_sse_serialize` 按原形态重序列化。已用审计探针（`stream_fix.audit_file` 记 `patched` 事件）验收：真实 AskUserQuestion 触发补全、调用成功。

> 能力保留价值：`stream_fix` 对任何经此代理流式路径的工具调用自动补全漏填字段，`enabled=true`。触发边界是「被路由到本代理的流式工具调用」——已实测本会话的 AskUserQuestion 就在其内。

## 工程化手法

- **模块化包 + signal 热重载**：`hooks.py`(稳定薄壳) + `hookpkg/`(多文件包)。改包内代码 → `./reload.sh`(SIGUSR2 拓扑 reload)→ 下次请求生效,进行中的流不受影响;改 `hooks.config.json` → 按 mtime 即时热读;改 `reload.py` 本身(RELOAD_ORDER)或薄壳(新增 hook 方法)→ **必须重启** litellm。见 `reference/package-architecture.md`。**部署纪律(实测教训)**：`./reload.sh` 跑过 ≠ 新代码在跑。要**双重确认**:① litellm stdout 出现 `hookpkg: reloaded N modules via SIGUSR2`(N 对得上 RELOAD_ORDER;有 `reload failed at X` = 中途失败);② 一个「新代码才产生的落盘字段/审计事件」在真实流量上确认出现。**别拿「离线单测绿 + 跑过 reload」当线上已生效**(本次就因此误判)。详见 `reference/hot-reload-verification.md`。
- **配置驱动探针**：所有开关放 `hooks.config.json`（热读），各探针独立 enable/probe_only。孤儿类探针在修复后应**永远为空**，一旦有新行即回归警报。
- **site-packages patch**：官方 hook 够不到时（如 L2 转换层的对比观测），才改 site-packages，并生成**幂等 apply 脚本 + 分离的 fix/debug patch**，重装后可重打。见 `reference/patching-and-probes.md`。
- **探针输出应私有化**：对话可能含敏感内容，探针应落 `~/.config/litellm/probe-logs/`（`chmod 700`），别写 world-readable 的 `/tmp`。**注意**：`_DEFAULT_CONFIG` 里多数探针（probe/deployment_probe/failure_probe/success_probe 及 orphan/toolref dump）的**默认路径仍在 `/tmp`**，只有 `stream_fix.probe_file` 默认落 `probe-logs/`。启用探针时**手动把 file 路径改到 `probe-logs/`**（`hooks.config.json` 已示范）。

## 参考文件

- `reference/hook-points.md` —— 五个官方 hook 的签名、触发点源码位置、返回值契约、`vars(cls)` 陷阱
- `reference/package-architecture.md` —— hookpkg 模块结构、SIGUSR2 拓扑热重载、ProbeContext 统一可观测、sed 拆包搬运缺陷教训
- `reference/streaming-rewrite.md` —— 流式 SSE 改写状态机、分片 JSON 重组、异常必闭合块、触达诊断;**+ 给流式路径新增 text-block 消费者的四条铁律**(每路径必闭合块、disable≠块已关、feed/flush 事务化+drain_raw 兜底、每 block 一实例;三轮评审血泪)
- `reference/invoke-conversion.md` —— 泄漏进 text 的 `<invoke>` 转 tool_use：block 拆分、index 偏移、stop_reason 改写、白名单(glob)
- `reference/patching-and-probes.md` —— site-packages 打 patch、幂等脚本、全链路探针体系配置
- `reference/case-tool-result-orphan.md` —— tool_result 孤儿 bug 的完整定位与修复
- `reference/case-stream-tool-fix.md` —— AskUserQuestion 缺 question 的流式修复案例
- `reference/case-sse-frame-reassembly.md` —— SSE 帧被 chunk 边界劈开(半截 `event:` 泄漏 → `Unexpected identifier "event"`)的定位与字节级重组修复
- `reference/hot-reload-verification.md` —— **验证部署真的生效**:config 热读 ≠ 代码 reload、SIGUSR2 静默失效、用代码级可观测量在真实流量上确认
- `reference/stream-cancellation-and-abort.md` —— **hook 层掐不断上游流**:aclose 不级联、owner 是 httpx.Response、真掐断需动引擎、TCP 背压、客户端 EOF vs 上游 teardown。PoC 坐实,任何「取消/超时/按预算截断流」前必读
- `reference/reasoning-carrier-round-trip.md` —— **把 provider 不透明状态（reasoning encrypted_content）跨有损协议桥往返回客户端的载体模式**（命名空间 token 塞进 signature/data、严格 decode 边界、跨模型剥离、provider 门控、kill switch）+ **只能 live 验的门禁方法论**（subagent 驱动真客户端验 R1 存储、subagent transcript 路径深一层陷阱、valid/篡改差分证「重建真到后端」、严格 SDK vs 宽松客户端分离协议信封缺陷）
