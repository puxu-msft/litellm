# 终端可观测、跨 worker 事件档案与请求档案实施计划

状态：已冻结；两轮独立评审完成（round-1：0 blocker/2 major，全部闭合；round-2：0 blocker/0 major，结论“可冻结”），可从 Phase 1 kick-off 实施

日期：2026-07-18

权威输入：

- [冻结 Spec](../specs/2026-07-18-terminal-observability-event-archive-design.md)
- [ADR-0002](../../ADR.md)
- [Phase 0 实测结论](../../../exp/terminal-observability-phase0/CONCLUSION.md)
- [在途请求/优雅关停 Spec](../specs/2026-07-14-in-flight-observability-graceful-shutdown-design.md)
- [项目 TRACKING](../../TRACKING.md)

本计划只决定如何实施已冻结设计，不重新决定产品行为。当前 `config/hookpkg/logline.py` PoC 必须继续拥有 TTY，直到 Phase 7 shadow gate 通过并一次切换；任何中间阶段都不得出现双 footer 或双完成记录。

## 0. 已完成门禁与不可回退合同

Phase 0 已完成，不再重复研究。实现必须保留三项实测合同：

1. **Uvicorn collector ownership**：direct、`Multiprocess(workers=2)`、`ChangeReload` 均可由 parent 唯一持有 collector。direct runner 必须在 `server.run()` 外层持有 SIGTERM handler，且到 `collector.close()` 后才恢复，否则 uvicorn graceful shutdown re-raise 会在 cleanup 前终止进程
2. **HTTPX observer ownership**：observer transport 创建的 request wrapper 必须在 inner `handle_async_request()` 返回或抛错后于 `finally` 关闭；response wrapper 由 `httpx.Response.aclose()` 关闭。observer 失败只降级 capture，不改变业务 bytes、chunk、异常或关闭传播
3. **DuckDB 离线依赖**：Python wheel 不内置 sqlite scanner。构建期必须准备与 DuckDB 版本/平台/架构匹配的官方 extension；运行时关闭 extension autoinstall/autoload，只显式加载固定本地 artifact。`SELECT * UNION ALL BY NAME` 的 additive schema 对齐已验证

实验资产保留在 `exp/terminal-observability-phase0/`，不是生产模块的复制来源；实现必须把合同重新编码成生产测试。

### Spec 与 Plan Phase 映射

本计划把冻结 Spec 的 Shadow archive 拆成离线 primitives 和运行时 shadow 两个可独立验收阶段，故编号有意不同。跨文档讨论必须使用下表，不按数字直接类比：

| 冻结 Spec | 本计划 |
|---|---|
| Phase 0：PoC 门禁 | Plan Phase 0：已完成，不重复实施 |
| Phase 1：Shadow event archive | Plan Phase 1：离线 primitives + Plan Phase 2：single-worker shadow runtime |
| Phase 2：Rich TUI | Plan Phase 3 |
| Phase 3：四边界 transport capture | Plan Phase 4 |
| Phase 4：多 worker IPC | Plan Phase 5 |
| Phase 5：Web/API 与离线 replay | Plan Phase 6 |
| Phase 6：真流量 shadow + 切换 | Plan Phase 7 |
| Phase 7：受控网络 replay | Plan Phase 8 |
| zip-transcripts TODO | Plan §10 TODO |

## 1. 模块布局与依赖方向

按能力出现时逐步创建文件，不在 Phase 1 一次铺满空模块。目标布局：

```text
litellm/proxy/observability/terminal/
  events.py                 # versioned immutable envelopes + tagged payloads
  codec.py                  # length-prefixed JSON and schema validation
  config.py                 # terminal_logging typed config + dynamic/static split
  identity.py               # request/event/worker/session identities

  archive/
    schema.py               # public SQLite schema versions and migrations/read adapters
    content_pool.py         # zstd+BLAKE3 blob pool
    spool.py                # per-worker WAL, ranges, ack/compact
    segment.py              # active/draining/immutable segment lifecycle
    catalog.py              # non-rotating catalog + session_aliases
    recovery.py             # reconciliation, incomplete, orphan GC

  collector/
    protocol.py             # Unix stream handshake/range/ack messages
    client.py               # worker non-blocking sender + spool fallback
    server.py               # parent collector socket/runtime
    projection.py           # durable-event projection reducer
    runtime.py              # startup/shutdown/reconfigure ownership

  capture/
    headers.py              # fixed credential-header masking
    body.py                 # chunk manifests/timing/completeness
    client_http.py          # ASGI client ingress/final downstream observers
    upstream_httpx.py       # github_copilot-gated shared transport observer
    semantic.py             # final client tool/thinking facts

  render/
    logging_adapter.py      # known logger handler ownership
    model.py                # responsive completion/footer render models
    rich_renderer.py        # Console + one Live owner
    plain_renderer.py       # plain/JSON fail-open

  query/
    adapters.py             # segment schema adapters
    coordinator.py          # DuckDB catalog snapshot + logical views
    api.py                  # read-only SQL and structured request APIs

  replay/
    offline.py              # body/chunk reconstruction
    network.py              # later controlled replay using current authenticator
```

允许实施中合并只有几行且永远同生命周期的相邻文件；禁止把 event/storage/renderer/query 塞进一个 god file。导入层级固定如下：

- `events/identity/config` 是基础层，不导入 archive、collector、capture、render、query 或 replay
- `archive/collector/capture` 导入基础层；capture 不导入 renderer/query
- `projection` 导入 events/archive，不被 registry 或业务请求路径反向调用
- `render/query/replay` 导入 projection/archive/events，不反向影响业务生命周期、capture 或持久化提交

新增代码遵守 fully typed、frozen dataclass/slots、tagged union + exhaustive match、composition、dependency injection、无 `Any`、无局部可变累加器。需要解析未知 JSON 时用 Pydantic/TypeAdapter 在边界验证后再进入 typed core。

## 2. Phase 1 — Versioned events 与 SQLite primitive（无运行时接线）

目标：先建立可离线验证的事件、spool、segment、catalog 和内容池，不接 TTY、不接真实请求。

### Task 1.1：版本化事件 envelope — ✅ 已完成

实测：50 tests passed；Ruff、formatter、basedpyright（0 errors）通过；mutmut 对 `events.py`/`codec.py` 生成 553 mutants，528 killed、25 survived、0 timeout，kill rate 95.48%。独立 re-review 为 0 blocker/0 major，可进入 Task 1.2。

新增：

- `litellm/proxy/observability/terminal/events.py`
- `litellm/proxy/observability/terminal/codec.py`
- `tests/test_litellm/proxy/observability/terminal/test_events.py`
- `tests/test_litellm/proxy/observability/terminal/test_codec.py`

先写失败测试：

- 每个冻结 `event_type` 都能构造、编码、解码并保持字段
- `(worker_instance_id, worker_sequence)` 是幂等 identity；UUIDv4 worker ID 不接受 PID 代替
- terminal events 穷举映射五种 reason；未知 event type 返回 typed `UnsupportedSchema`，不静默转 dict
- 同 major schema 的未知字段 round-trip 保留
- length-prefix 处理半 header、半 payload、多个 frame、超上限 frame 和 EOF 截断

最小实现：frozen envelope、各 payload tagged dataclass、Pydantic boundary adapter、orjson codec。不要先引入 SQLite。

窄验证：

```bash
.venv/bin/python -m pytest tests/test_litellm/proxy/observability/terminal/test_events.py tests/test_litellm/proxy/observability/terminal/test_codec.py -q
.venv/bin/ruff check litellm/proxy/observability/terminal/events.py litellm/proxy/observability/terminal/codec.py tests/test_litellm/proxy/observability/terminal/test_events.py tests/test_litellm/proxy/observability/terminal/test_codec.py
```

完成条件：mutation testing 对 event type/terminal mapping/length checks 的 kill rate >90%；无 `dict[str, Any]` 泄入 core。

### Task 1.2：固定 header masking 与 session identity — ✅ 已完成

实测：Task 1.1+1.2 合并 74 tests passed；Ruff、formatter、basedpyright（0 errors）通过；mutmut 对 `identity.py`/`capture/headers.py` 生成 83 mutants，78 killed、5 survived、0 timeout，kill rate 93.98%。独立 review 为 0 blocker/0 major；固定 Crockford 向量、authorization 无 scheme 和典型 5 位碰撞均已补证。

新增：

- `litellm/proxy/observability/terminal/identity.py`
- `litellm/proxy/observability/terminal/capture/headers.py`
- 对应 `test_identity.py`、`test_headers.py`

先写失败测试：

- 现有 `X-Claude-Code-Session-Id` 生成稳定 keyed BLAKE2s/Crockford 短 hash
- 碰撞时扩展 5–6 位且 catalog alias 决策可持久恢复
- 无 session 输出 `□ ----`
- 固定 credential header 集合只保留掩码前后缀；普通 header 原值保持
- 原 secret 不出现在 encoded event、repr、日志异常或测试快照

独立 oracle：用标准库 `hashlib.blake2s` 直接计算固定向量，不用实现自己的 encode/decode 相互自证。

### Task 1.3：Worker spool schema 与幂等 range/ack — ✅ 已完成

实测：Task 1.1–1.3 合并 94 tests passed；Ruff、formatter、basedpyright（0 errors）通过；真实 SQLite trigger/drop-table 覆盖 store/ack/compact/load 运行时错误，损坏 frame 值化为 `CORRUPT_FRAME`。独立 re-review 为 0 blocker/0 major。mutmut 生成 387 mutants，299 killed、88 survived、0 timeout，kill rate 77.26%；存活项主要是 SQL/异常消息与 I/O 分支，未达到纯逻辑 >90% 目标，已如实保留，不作为本 I/O repository task 的虚假绿灯。

新增：

- `archive/schema.py`
- `archive/spool.py`
- `test_spool.py`

先写失败测试：

- WAL/0600 文件创建、schema version、worker UUID metadata
- append event/chunk batch 后 sequence 连续
- 重复 range import identity 幂等
- ack 丢失后 replay 不重复；只在 durable ack 后 compact
- worker restart 用新 UUID，不复用旧 sequence namespace
- SQLite busy/full/I/O failure 返回 tagged failure，不阻塞/不伪装 committed

独立 oracle：所有 spool sequence/range/ack/compact 断言同时用标准 `sqlite3` 直接查询 rows、schema 与 transaction state，不只经 spool repository API 读取。

实现使用注入的 SQLite connection factory/clock/UUID source，测试不 monkeypatch class attributes。

### Task 1.4：Content pool、chunk manifest 与 completeness — ✅ 已完成

实测：Task 1.1–1.4 合并 110 tests passed；Ruff、formatter、basedpyright（0 errors）通过；固定 seed 的 64KiB payload 经 127 cuts/128 chunks 完成 store->manifest->load->reassemble 独立 oracle。独立 re-review 为 0 blocker/0 major。mutmut 生成 83 mutants，67 killed、16 survived、0 timeout，kill rate 80.72%，如实保留未达纯逻辑 >90% 的结果。

新增：

- `archive/content_pool.py`
- `capture/body.py`
- `test_content_pool.py`、`test_body_capture.py`

先写失败测试：

- BLAKE3 digest + zstd blob round-trip，重复 bytes 去重
- chunk sequence、UTC timestamp、monotonic offset 重建原 body
- `complete` 与 `incomplete:overflow|crash_tail|spool_failure` 穷举
- orphan blob 可检测；manifest 缺 blob不能声明 complete
- 大 blob 流式压缩路径不一次物化整个 body

独立 oracle：BLAKE3/zstd 官方库直接解码；随机 chunk cuts 拼接必须等于原 payload。

### Task 1.5：Central segment 与非轮转 catalog — ✅ 已完成

实测：Phase 1 合并 132 tests passed；Ruff、formatter、basedpyright（0 errors）通过。状态机、两个 publish crash windows、安全 orphan GC、missing/corrupt blob closure、dead/live worker recovery均有反向测试；独立终审关闭 1 blocker/3 major，最终 0 blocker/0 major。

新增：

- `archive/segment.py`
- `archive/catalog.py`
- `archive/recovery.py`
- `test_segment.py`、`test_catalog.py`、`test_recovery.py`

先写 crash-point 测试：

- request accepted 绑定 owner segment，后续内容不能跨段
- 2d/1GiB threshold 后旧段 draining、新 active 接新请求
- owner 未 terminal 时不能 seal；stale 只告警
- `publishing -> published` 每个 fsync/rename/catalog crash point 可 reconciliation
- recovery 只能生成 `shutdown_dropped/incomplete`，不能推断 completed/failed/timed_out
- seal 前引用闭合和 orphan GC
- alias map 在非轮转 catalog 中跨 rotation/restart 保持

不要用只测“写后由同一实现读取”的 round-trip 作为唯一 oracle；用 sqlite3 直接检查 schema/rows、文件系统目录状态和故障注入后的 manifest closure。

### Phase 1 合并验收

状态：✅ 已完成。Task 1.1–1.5 全部通过各自 TDD/review 门禁；当前未接 proxy runtime/TTY。

- 仅离线库和测试，不修改 proxy startup/callback/TTY
- `make pre-commit` 前只 stage 本阶段文件；若预算文件下降，运行 `make lint-budget-update`
- 独立 reviewer 检查 public schema、crash protocol、幂等与 mutation score
- 更新 Spec/Tracking 实际完成状态和 schema 文档

## 3. Phase 2 — Single-worker shadow archive + JSONL — ✅ 已完成

实测：Phase 1+2 合并 141 tests passed；Ruff、formatter、basedpyright（0 errors）通过。durable-first projection、restart owner重建、terminal不复活、typed config/PoC互斥、plain/JSONL sinks均有测试；独立review的2 major已闭合。未接生产callback/TTY。

目标：在单 worker 下接真实事件，但当前 PoC 仍是唯一 TTY owner。新系统只写 shadow SQLite/JSONL。

### Gate 2.0：并行依赖检查

读取当前工作树和 [in-flight Spec](../specs/2026-07-14-in-flight-observability-graceful-shutdown-design.md)。若 `InFlightRegistry` 尚未实现：

- 允许用 CustomLogger pre-call/success/failure + transport facts 建 shadow adapter
- adapter 文件必须命名/文档标明 `transitional`
- 不允许把 transitional lifecycle 宣布为最终真相源
- Phase 7 切换 gate 必须等待 registry 完成

若 registry 已实现：直接订阅 registry mutation，不再新增 transitional adapter。

### Task 2.1：Typed `terminal_logging` config

新增：

- `terminal/config.py`
- proxy config validation/tests
- `config.yaml` 示例，但默认 `shadow_enabled=false`

先写失败测试：静态/dynamic 字段、非法 mode/threshold/path/refresh fail-fast、SIGHUP reconfigure 只应用动态项。现有 `hooks.config.json request_log` 不迁移、不关闭。显式测试 `request_log.live_status=true` 与 `terminal_logging.mode=interactive-experimental` 同时出现时配置 fail-fast，防止双 footer/双完成记录。

### Task 2.2：Single-process collector runtime

新增：

- `collector/projection.py`
- `collector/runtime.py`
- `test_projection.py`、`test_runtime.py`

先写失败测试：committed event 才进入 projection；duplicate/gap/terminal-after-terminal anomaly；startup 从 segments/catalog 重建；shutdown flush/checkpoint；collector/SQLite failure返回 degraded 状态。

### Task 2.3：Shadow lifecycle adapter

接线位置以当时 registry 状态决定；测试必须从真实 callback/registry event 到 SQLite row，不直接调用内部 writer伪装 integration。

先写失败测试：stream/non-stream success、failure、cancel、timeout、shutdown_dropped；session ID 传播；attempt/retry sequence；无双 completion event。

### Task 2.4：非 TTY JSONL sink

新增 plain/JSON sink，输出所有元数据事件与 blob digest，不输出 body。测试 `isatty=false`、`mode=json|plain|off|auto`、logger exception、unknown fields。当前 PoC stdout 行保持不变；shadow JSONL 写独立文件/测试 sink，不能争 stdout。

### Phase 2 独立验收

- 用本地 mock provider + 真实 proxy subprocess 生成请求
- 直接 sqlite3 查询 event rows，与 HTTP 客户端观测的 status/timing/session对照
- 故意杀进程，验证 committed/uncommitted/incomplete 区分
- 现有 hookpkg 181+ 回归保持
- reviewer 通过后才进入 renderer，不切换 TTY

## 4. Phase 3 — Rich renderer（显式实验开关，PoC 默认 owner）

状态：✅ primitives 已完成，未接生产 owner。Golden completion/footer、单 Console Live 与 handler restore 测试通过；PoC 仍唯一 TTY owner。

### Task 3.1：Render model 与响应式布局

新增 `render/model.py` 与纯函数 tests。覆盖冻结完成行/footer：会话优先、四 marker、HTTP/retry、固定两位时长、TTFT、上游 bytes、token 三段/百分比未知、工具重复完整顺序、thinking carrier、`(non-stream)`、最久组优先与 `+N groups`。

独立 oracle：golden plain text fixture，不由 Rich renderer 自己生成 expected。

### Task 3.2：Known logger adapters

实现 worker 与主进程不同 ownership：worker logger 产生 typed event；main uvicorn logger 产生 collector local event。保存/恢复原 handlers，不劫持 stdout/stderr。测试 handler install 幂等、reload/reconfigure、exception traceback、模型 endpoint access 去重与其他 endpoint access 保留。

### Task 3.3：Rich Live 与 fail-open

实现一个 Console/Live owner。先写 `_MemoryTTY` 单测，再用 PTY+pyte：

- footer 最后一行、4Hz elapsed
- INFO、完整 ERROR traceback、多行工具记录不会覆盖 footer
- resize/80/120/宽屏/Unicode/NO_COLOR
- renderer 失败重建一次，再失败 plain并恢复 handlers/光标
- direct SIGTERM 外层 handler保持到 renderer/collector close
- SIGHUP reconfigure 后 projection state、在途 requests 和 footer 连续存在，不重建/清空 reducer

必须做正样本对照：临时坏 renderer 应导致 PTY 吞行/覆盖测试红，再恢复变绿。连续 8–25 次验证时序稳定。

### Phase 3 验收

`terminal_logging.mode=interactive-experimental` 才启用 Rich；默认仍由 hookpkg PoC 画 TTY。不能同时启用两者，配置 validator fail-fast。通过后仍不切默认。

## 5. Phase 4 — 四边界 capture 与 GitHub Copilot httpx observer

状态：✅ capture primitives 已完成，未接 shared production transport。ASGI client边界、github_copilot-gated AsyncBaseTransport、observer fail-open与最终语义工具/thinking摘要均有typed测试。

### Task 4.1：客户端 ingress/final downstream observer

在 ASGI/StreamingResponse 的最小 owning seam 捕获原始 request body 与最终 response chunks。测试必须覆盖不读双份 body、不改变 downstream chunk、客户端取消、错误 response、SSE keepalive 与 backpressure。

### Task 4.2：Shared httpx observer protocol

把 Phase 0 primitive转成 typed、DI 的 production observer。仅 `custom_llm_provider=github_copilot` 且 capture 开启时安装。

生产测试必须覆盖：

- `LiteLLMAiohttpTransport` 与 `AsyncHTTPTransport`
- retry-created clients/single-connection retry
- request wrapper `finally` close、response `Response.aclose` close
- observer callback异常 fail-open
- sync handler或明确证明 GitHub Copilot目标面不走 sync；不能默默漏面
- 关闭 observer前后真实 mock HTTP server看到相同 bytes/chunks/errors

独立 oracle：本地 raw HTTP/SSE server记录 socket收到的 body/chunks，不只比较 wrapper两端。

### Task 4.3：Final semantic observer

在发客户端前的最终 Anthropic语义边界记录工具列表（顺序/重复）和规范化 thinking carrier。测试复用协议 fidelity fixtures，并与 strict Anthropic SDK解析结果对照，不从聚合 ModelResponse推断。

### Task 4.4：准确 upstream bytes/TTFT

`↑/↓` 来自实际 observed body chunk总和；TTFT在 downstream首次真实 yield计时。测试故意让 semantic payload estimate与wire bytes不同，防止回退旧近似值仍假绿。

### Phase 4 验收

四边界 digest/bytes/chunk timeline在 mock/live shadow样本闭合；capture overflow注入后业务流成功且archive明确incomplete。

## 6. Phase 5 — Uvicorn main-process collector、Unix IPC 与 worker spool

状态：✅ IPC/spool replay primitives 已完成，生产uvicorn runner接线留到最终cutover。Unix range/ack、durable ack后compact、断连保留pending均有真实socket测试；Phase0 runner三拓扑合同保留。

### Task 5.1：把 Phase 0 ownership接入正式 runner

在 `uvicorn_runner.py`/新 runtime factory 组合 collector，不复制 supervisor dispatch。先扩现有 `test_uvicorn_runner.py`，再真实 subprocess测试 direct/multiprocess/reload。

必须断言 direct外层 SIGTERM handler直到 `collector.close` 后才恢复；第二信号、startup failure、limit_max_requests、collector startup failure均有明确路径。

### Task 5.2：Unix protocol/client/server

实现 handshake、schema negotiation、range notification、durable ack。用真实 Unix socket测试 partial frames、disconnect、duplicate range、collector restart、worker restart。

### Task 5.3：Spool replay 与 overflow

测试 queue满不做同步SQLite回压业务；记录 `incomplete:overflow`。spool不可写、anomaly通道不可写、overflow bit补写、ack丢失、worker死亡后parent导入。

### Task 5.4：多进程故障矩阵

真 subprocess：workers=2，杀worker、杀collector模拟组件、reload、SIGTERM/第二信号、残留PID扫描。collector必须是唯一stdout owner；unsupported topology自动JSON/plain。

### Phase 5 验收

Phase 0九-run oracle迁入正式测试；连续运行；无 orphan process/socket/spool。仍不切默认TTY。

增加 Phase 4+5 合并态测试：workers=2 的四边界 capture 经 IPC/spool 汇入 central segment，再由标准 SQLite reader 验证 event/body/chunk closure；不得以 capture 与 IPC 各自单测绿代替整链路。

## 7. Phase 6 — DuckDB query coordinator、公开 SQL 与 Web inspector

### Task 6.1：依赖与 extension artifact

实施时查询最新稳定 DuckDB，不凭记忆写版本；固定 Python package与官方 sqlite extension的版本/平台artifact。新增构建/安装脚本和 checksum manifest。测试在干净 HOME、网络不可用、autoinstall/autoload关闭时显式加载。

若目标环境加载失败，停止并回ADR；不能运行时下载或Python手拼任意SQL。

### Task 6.2：Schema adapters + logical views

实现 catalog snapshot、segment pruning、SQLite read txn/immutable RO、DuckDB relations、`UNION ALL BY NAME`。测试 additive/major schema、NULL补列、类型变化adapter、多segment时间范围、active WAL一致读取。

### Task 6.3：Read-only SQL endpoint

单 statement、只读逻辑表、streaming rows、cancel/resource limits。底层表公开并有schema docs。使用真实DuckDB parser/plan判断，不用字符串contains决定只读。

### Task 6.4：Web inspector

API + 内置界面：request list/filter、四边界structured diff/raw、headers掩码、chunk timeline、usage/tools/thinking/retry/anomaly。遵循现有 UI设计系统；用Playwright桌面/移动截图检查文字不重叠。不要做营销页面。

### Task 6.5：Offline reconstruct

按chunk sequence/monotonic interval重建body与本地consumer replay；对split-frame已知案例做独立oracle。此阶段不发网络。

## 8. Phase 7 — InFlightRegistry gate、真流量 shadow 与一次切换

前置硬门：既有 in-flight Spec 的 `InFlightRegistry`、`timed_out`、shutdown quiesce必须完成。若并行分支尚未落地，等待/合并，不在本功能中另造registry。

### Task 7.1：替换 transitional lifecycle source

影子事件改订阅registry mutation。对每个 transitional/registry事件逐request比对，无gap/双terminal后删除transitional adapter。

### Task 7.2：真流量 shadow矩阵

覆盖 Spec §16.5：Claude stream/non-stream、GPT Responses tool/reasoning、双session并发、cache三段、重复工具、thinking enc/redacted、retry/429/timeout/cancel/shutdown。

独立oracle：HTTP客户端raw bytes、strict SDK、SpendLogs、httpx mock server、registry snapshot；禁止只拿新archive对新renderer自证。

### Task 7.3：切换门禁

逐request比较 model/surface/provider/status/duration/TTFT/upstream bytes/tokens/tools/thinking/session/terminal。任何gap/body不闭合/双记录阻塞切换。

一次配置切换让新Rich renderer成为唯一TTY owner；关闭PoC `request_log.live_status`和重叠完成行。保留PoC代码一个回退周期，确认稳定后另任务删除；不在切换提交同时大删代码。显式测试 cutover 后 `isatty()=false` 且 `mode=auto` 时，版本化 JSONL 元数据事件输出到 stdout，PoC plain-text 完成行不再出现。

## 9. Phase 8 — 受控网络 replay

在 offline reconstruct通过后新增。默认dry-run、明确目标、使用当前GitHub Copilot authenticator，不从archive恢复旧凭据。每次replay写source linkage和结果事件。测试本地mock endpoint先行；live smoke 是额外环境验收，不替代本地确定性门禁。

## 10. TODO — zip-transcripts 说明书

仅在 Plan Phase 6 Task 6.5 offline reconstruct 完成、immutable segment manifest/checksum/receipt 已稳定后写交接说明书；不修改 `/home/xp/src/zip-transcripts`。说明书定义segment发现、闭合判据、archive receipt和恢复。旧segments当前不删，只告警。

## 11. 每阶段统一收尾

1. 运行阶段窄测试、相关子树回归、真实oracle
2. 运行 Ruff/pyright/编辑器诊断；修预算时执行 `make lint-budget-update`
3. stage本阶段文件后运行 `make pre-commit`；不 stash/delete 他人改动，不自动commit。若命令因本阶段之外的脏工作树文件失败，保存完整失败输出，运行本阶段路径的 Ruff/basedpyright/测试并在阶段报告中明确标记全局 pre-commit 被外部改动阻塞；最终合并前仍必须在隔离 worktree 或协调后的干净工作树补过 `make pre-commit`，不能把 scoped checks 冒充全局通过
4. mutation testing覆盖新增核心纯逻辑，目标 >90% kill；等价mutant需文档证明
5. 更新 Spec/TRACKING/ARCH/DESIGN和public schema/API文档
6. 独立 reviewer评代码；处理后re-review。涉及wire/恢复/切换的阶段再由 verifier做黑盒验收
7. 阶段合并态检查，不能以各自单测绿代替完整链路

## 12. 风险与停止条件

- Phase 1发现SQLite协议无法满足幂等/闭合：停止并回Spec/ADR，不先接运行时
- Phase 4 observer改变任意业务bytes/chunk/error/close：停止，不以“通常正常”灰度
- Phase 5主进程collector在正式runner拓扑不满足Phase 0合同：停止并回ADR裁决专用子进程
- Phase 6 vendored DuckDB extension无法离线加载：停止并回ADR选query engine
- Phase 7 registry未完成或shadow不闭合：继续PoC owner，不切换
- 任意阶段测试需要覆盖/回退他人未提交 graceful-shutdown改动：停止协调，不能强行覆盖
