# 请求处理时间性能体检 / Request-processing latency health-check

日期:2026-07-15  ·  范围:litellm 代理 + 自研 hookpkg(GitHub Copilot provider,本地 standalone)

主动体检,非因观察到变慢。测三层:代理+hook 请求侧开销、响应侧逐 chunk CPU、端到端总耗时占比。方法:确定性纯函数转换离线精测(合成小/中/大 payload),活管线属性用 `log_success` 落盘 sink 从真实流量聚合。复现见 `exp/perf/bench.py`。

## 结论先行

**hook 的 CPU 开销相对模型/网络时间可以忽略。** 真实 opus 请求(~400KB / 16-19 万 token 上下文)端到端 3-10s,而请求侧 hook `process()` 只花 ~3ms(≈0.03-0.1%);响应侧逐 chunk 7µs 分摊在秒级流上、被 chunk 间网络间隔淹没。体检的价值不在"发现卡顿",而在定位了一处**纯浪费**和一处**冗余计算**——都值得按 long-termism-wins 清掉,但都不是延迟瓶颈。

## 一、请求侧 `process()` —— 每请求一次(离线,生产配置)

| payload | 大小 | messages | `process()` 总 | 快照占比 | walk 占比 |
|---|---|---|---|---|---|
| small | ~5 KB | 8 | 0.063 ms | 37% | 30% |
| medium | ~59 KB | 82 | 0.571 ms | 58% | 22% |
| large | ~606 KB | 402 | 3.902 ms | **71%** | 14% |

逐子步(large,median):

- **messages JSON round-trip 快照 2.786 ms(71%)** —— 最大头,且**纯浪费**。`__init__.py:process()` 里只要 `probe.dump_orphans_only` 为真(当前生产配置正是 `true`),每个请求都 `json.loads(json.dumps(messages))` 把整条历史序列化+反序列化一遍,只为"万一检测到孤儿 tool_use"时留一份修复前快照。而孤儿极罕见,99%+ 的请求这份 600KB 的深拷贝当场丢弃(还带一次全量内存分配)。
- `_strip_cache_control_scope` 递归 walk 0.562 ms(14%) —— 每请求遍历整棵 messages 树剥 `cache_control.scope`。
- `_fix_orphan_tool_use` 扫描 0.214 ms(5.5%)、`fix_thinking_blocks` 0.228 ms(5.9%)、`_strip_beta_by_model` ~0。

cProfile 交叉验证一致(注:cProfile 因 `walk` 递归调用极多而高估 `_strip_cache_control_scope` 的绝对值,perf_counter 的墙钟数才是准的)。

## 二、响应侧流式管线 —— 逐 chunk CPU(离线,415-chunk 流)

| 层 | 每流 median | 每 chunk |
|---|---|---|
| 纯状态机 `stream.stream_transform` | 1.68 ms | 4.0 µs |
| +block_audit(双侧观测+每流一次落盘) | 3.18 ms | 7.7 µs |
| 全链路(+dedup) | 3.06 ms | 7.4 µs |

- **block_audit 让每 chunk CPU 翻倍**(+1.5ms/流):它在 `stream_transform` 两侧各 `sse_parse` 一次,而状态机内部已经 parse 过一次 —— 于是每个 chunk 被 JSON 反序列化 **3 次**(observe_in + 状态机 + observe_out),dedup 再加 1 次 = 最多 4 次。block_audit 当前 `violation_only:false`(每条流都落盘,block-seq.jsonl 因此涨到 3.6M)。
- dedup 边际在噪声内(±0.1ms)。
- 7µs/chunk 分摊在秒级流上,相对 chunk 间网络间隔可忽略 —— 响应侧 hook **不是**墙钟瓶颈。

另注(不在本次 TTFB 范围,但属端到端一段):`stream_fix.enabled` + text buffer 无条件开 → 所有 text block 缓冲到 `content_block_stop` 才外发,等于全局按 block 成段(非流式手感)。这影响的是首 token/流式观感,不是 CPU。

## 三、端到端总耗时 —— 真实流量(`log_success` 落盘 sink)

sink 每条成功请求追加一行 `total_s / ttft_s / gen_s / tokens` 到 `probe-logs/request-timing.jsonl`(配置 `request_log.timing_file` 开关,`logline.build_timing_record`)。真实样本:

| model | stream | total_s | in_tok | out_tok | req |
|---|---|---|---|---|---|
| claude-opus-4.8 | ✓ | 5.41 | 162k | 2035 | 391 KB |
| claude-opus-4.8 | ✓ | 10.17 | 191k | 1497 | 445 KB |
| claude-opus-4.8 | ✓ | 2.99 | 193k | 983 | 451 KB |
| claude-haiku-4.5 | ✗ | 0.71 | 12 | 4 | — |

对照:请求侧 hook 在这种 ~400KB 载荷上 ~2.5-3ms,即端到端的 ~0.03-0.1%。

**instrumentation 限制**:anthropic_messages 透传的流式路径上,litellm 把 `completionStartTime` 设成 ≈`endTime`,故 `ttft_s == total_s`、`gen_s == 0`,首 token/生成时长拆不开。`total_s` 可靠;ttft/gen 列在这条路上退化(其他路径如 responses API 可能不同)。要真 TTFT 需在 hook 首个 yield 处自计时(本次 TTFB 不在范围,未做)。

## 四、TUI 请求日志（2026-07-18）

`request_log` 的完成行采用访问日志式布局：`[ OK ] 17:18:53 200 github_copilot/claude-opus-4.8 27.3s ■ ↑1.5MB ↓17.6KB ↑2+567.3k+4.7k ↻99%+1% ↓1.8k tool_use(Bash) think:enc(1) (thinking:adaptive)`。工具名来自聚合 `ModelResponse.choices[].message.tool_calls`；`think:enc(N)` 统计带不透明 signature、data 或 encrypted_content 的 thinking block；请求 thinking 模式来自 `standard_logging_object.model_parameters.thinking.type`。字段缺失时直接省略，不输出诊断占位。

真实 TTY 下，`async_pre_call_hook` 按 `litellm_call_id` 登记请求，最后一行每 200ms 实时刷新在途模型与耗时；同模型并发合并为 `model ×N elapsed`，其中 elapsed 是该组最早请求的耗时。成功或失败日志回调按同一 call ID 注销请求，并把完成行写入上方滚动区。footer 使用 DECSTBM 预留终端最后一行，刷新时保存并恢复光标；最后一个请求结束后清空 footer、恢复全屏滚动区。非 TTY 输出继续走普通逐行 logging，不写 ANSI 控制序列。

配置入口为 `request_log.live_status`，默认开启；`request_log.diagnose` 已默认关闭。纯 formatter、终端生命周期和 logging handler 由 `hookpkg/tests/test_logline.py` 覆盖，并用 PTY + pyte 抓屏验证了双请求 `×2`、单请求、普通日志插入和全部完成后的 footer 清理。

## 建议(按 long-termism-wins,均非延迟急件)

1. **[已实施 ✅]** 干掉每请求的整份 messages JSON round-trip 快照。`_orphan_pre_fix_snapshot` 先用只读的 `orphans.find_orphans_any_format` 检测,**仅当确有孤儿**才取快照再修复(语义不变:快照本就只在有孤儿时用)。效果:large `process()` **3.9ms → 1.24ms(-68%)**,常态省掉 2.8ms + 一次 600KB 全量分配。回归测试 `tests/test_init.py::TestOrphanPreFixSnapshot`(两向锁定:干净不快照、有孤儿必快照)。已 reload 上线。
2. **[已实施 ✅]** block_audit 稳态设 `violation_only:true`——干净流不再落盘(block-seq 只在真有违规时写),每流也不再为审计重复 parse。已线上生效(config 热读),流式 smoke 实测干净流零落盘。更彻底的消除重复 parse(状态机把已 parse 事件传给审计层复用)需改接口,列入 backlog。
3. **[可选]** `_strip_cache_control_scope` 的全树递归 walk 可在剥离 0 个时快速跳过,收益小(0.5ms 级),优先级低。

## 日志落盘现状(2026-07-15,治理后)

删除历史垃圾 `stream-tool-input.jsonl`(3.8M,probe_only 早关)后 probe-logs 从 7.8M 降到 4.2M。block_audit 转 `violation_only:true` 后 `block-seq.jsonl`(3.6M)停止增长(仅违规时写)。剩余活跃增长仅 `stream-patched.jsonl`(audit 动作)与 `request-timing.jsonl`(新 sink,~280 B/请求)。

## 复现与资产

- `exp/perf/synth.py` —— 合成小/中/大 payload + 真实形态 SSE 流
- `exp/perf/bench.py` —— 请求侧/响应侧/cProfile 三段基准,`.venv/bin/python exp/perf/bench.py`
- `hookpkg/logline.py::build_timing_record` + `tests/test_logline.py::TestBuildTimingRecord` —— 端到端计时 sink(6 单测)
- `probe-logs/request-timing.jsonl` —— 真实流量持续累积,后续可聚合分布
