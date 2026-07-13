# 在途请求可观测 + 优雅关停播报 + 关停时序加固

状态：设计经一轮 GPT reviewer 对抗性评审（3 blocker / 6 major / 2 minor，全程核对真实代码 + uvicorn/prisma SDK 源码 + 最小复现 + 跑现有测试），本版据评审重写并冻结契约，待用户复核 gate
日期：2026-07-14 初稿；同日 round-1 评审后重写
分支：`ghc`
关联：现有 `GracefulShutdownManager`（[graceful_shutdown_manager.py](../../../litellm/proxy/shutdown/graceful_shutdown_manager.py)）、`InFlightRequestsMiddleware`（[in_flight_requests_middleware.py](../../../litellm/proxy/middleware/in_flight_requests_middleware.py)）、`LoggingWorker`（[logging_worker.py](../../../litellm/litellm_core_utils/logging_worker.py)，供 task supervisor 复用其 bounded-queue/running-task-set/flush-stop 模式）

## 修订说明（round-1 评审吸收）

评审背书成立的原判断（核对代码后确认）：① uvicorn 两阶段顺序（先等 `server_state.connections/tasks`、后 `lifespan.shutdown()`），故 `wait_for_drain` 挂 lifespan 在交互式长连接场景空转、日志无 `drain_waiting`；② 根因归因正确——prisma engine 随终端 SIGINT 死是 prisma-client-py 设计意图（[query.py:100/195-196](../../../.venv/lib/python3.13/site-packages/prisma/engine/query.py#L100)），litellm 关停期把它当 crash 重连才是 bug；③ `Server.handle_exit` 是同步且足够早的覆写点，`start_shutdown()` 幂等使 lifespan 那次成 no-op；④ pure ASGI 中间件设的 `ContextVar` 能被 FastAPI handler 及其 `asyncio.create_task` 子任务读取。

评审命中并已吸收的问题：

1. **【最重】记账生命周期越过 HTTP 请求**：中间件在 `await self.app()` 返回即注销，但流式成功日志、spend 更新是脱离请求的 `asyncio.create_task`（fire-and-forget），uvicorn 只跟踪 ASGI request task、不跟踪这些 detached task。后果——`stage=ACCOUNTING` 观测不到（record 早没了）；teardown 断 DB/redis 时记账 task 仍在跑，**这才是 `ClientNotConnectedError` 出现在「请求已排空之后」的结构性根因**。**用户已定：走完整生命周期修复**（managed task supervisor + work-lease），非仅在 `from_db` 加 guard
2. **SSE 鉴权**：浏览器原生 `EventSource` 无法带 `Authorization` header，「`EventSource` 直连 + 复用 `/health/*` 鉴权」必 401；且在途明细含 client IP/model，须收紧到 admin
3. **SSE 自我登记成永久在途**：`/health/in-flight/stream` 走中间件、永不返回，永远留在登记表让 `wait_for_drain` 看不到 0，开着面板即可把 Ctrl+C 拖到超时
4. **watchdog guard 面不足 + reconnect-task 竞态**：死亡触发重连有多入口，已创建的 reconnect task 会越过晚到的 flag
5. **redis 降级放错层**：`async_increment` 返回 `float`，吞异常破坏契约；`RedisCache` 是 SDK/router 共享基础设施，注入 proxy 的关停标志是层级污染
6. **SSE pub/sub 无背压/无原子初始快照/无重连协议**：`asyncio.Queue()` 默认无界，慢订阅者内存爆
7. **`update(id, **fields)` 动态 patch 违反 fully-typed；stage 无单调约束**，并发子任务可能把 stage 倒退
8. **uvicorn 注入判断纠正**：`workers>1` 与 `reload` **可**注入自建 Server（`Multiprocess/ChangeReload(target=server.run)`），原「单 worker 直接 `server.run()`」还会破坏热重载；`limit_max_requests` 等非信号退出不经 `handle_exit`
9. 真 SIGINT E2E 从「可选」提为 **Phase 1 必做**；中间件非最外层（`SecurityHeaders`/`RequestSizeLimit` 在其外）；定义**从信号时刻起算的单一关停 deadline** 供各层共享，第二次 SIGINT 立即 force-exit

## 背景与问题

单进程 litellm proxy，交互式 `Ctrl+C`（SIGINT）关停，一段真实日志同时暴露两类痛点：

```
^CINFO:     Shutting down
INFO:     Waiting for connections to close. (CTRL+C to force quit)
02:00:33 - prisma-query-engine PID 3113457 exited (waitpid thread); triggering reconnect.
02:00:33 - Attempting Prisma DB reconnect. reason=engine_process_death
（静默约 3 分钟）
INFO:     Waiting for application shutdown.
02:03:54 - SpendCounterReseed.from_db: failed ... prisma.errors.ClientNotConnectedError
02:03:54 - LiteLLM Redis Caching: async async_increment() ... Connection closed by server.
INFO:     Application shutdown complete.
```

三件事（用户确认全做、关停 bug 先修）：① 在途请求明细不可观测（LOG + WebUI）；② 优雅关停不播报在途请求与各自 elapsed；③ 关停时序 bug（watchdog thrash + 记账撞已断 DB/redis 刷屏）。

### 已核实的现状

- 在途追踪已存在但**只是整数计数器**（[in_flight_requests_middleware.py](../../../litellm/proxy/middleware/in_flight_requests_middleware.py) 的 `_in_flight`），只在 `/health/backlog`（[_health_endpoints.py:1582-1595](../../../litellm/proxy/health_endpoints/_health_endpoints.py#L1582)）与 Prometheus gauge 露出，WebUI 无面
- 排空骨架 `wait_for_drain` 挂 lifespan（[proxy_server.py:1073-1074](../../../litellm/proxy/proxy_server.py#L1073)），晚于 uvicorn 连接排空，交互式场景空转
- 记账/日志是脱离请求的 detached task：流式成功日志 [common_request_processing.py:1686-1707](../../../litellm/proxy/common_request_processing.py#L1686)、`update_cache` fire-and-forget [:2238-2253](../../../litellm/proxy/common_request_processing.py#L2238)、spend 更新 [proxy_track_cost_callback.py:228-258](../../../litellm/proxy/hooks/proxy_track_cost_callback.py#L228)、`_batch_database_updates` [db_spend_update_writer.py:188-202](../../../litellm/proxy/db/db_spend_update_writer.py#L188)
- watchdog 死亡→重连多入口（[utils.py:4166-4368/4432-4446/4554-4625/4716-4738](../../../litellm/proxy/utils.py#L4166)），`_consume_expected_death`/`_expected_engine_deaths`（[:4222](../../../litellm/proxy/utils.py#L4222)）语义是「计划替换旧 engine、已有替身」
- `SpendCounterReseed.from_db` 已 try/except+return None（[spend_counter_reseed.py:118-120](../../../litellm/proxy/db/spend_counter_reseed.py#L118)），刷屏来自 `.exception`；另有 `coalesced`/`coalesced_window`/`window_from_spend_logs` 独立 DB/redis 入口（[proxy_server.py:2183/2200/2253/2583/2620](../../../litellm/proxy/proxy_server.py#L2183)）
- `RedisCache.async_increment` 返回 `float`（[redis_cache.py:857-916](../../../litellm/caching/redis_cache.py#L857)），except 内还建 `async_service_failure_hook` task；调用方 `_increment_spend_counter_cache`（[proxy_server.py:2660-2675](../../../litellm/proxy/proxy_server.py#L2660)）会 invalidate 并重抛
- uvicorn 注入点：`uvicorn.run` 内部 `server=Server(config)` 后按 reload/workers/direct 分派 `target=server.run`（[uvicorn/main.py:516-579](../../../.venv/lib/python3.13/site-packages/uvicorn/main.py#L516)）；`Server.handle_exit` [server.py:334](../../../.venv/lib/python3.13/site-packages/uvicorn/server.py#L334)、async `shutdown` [server.py:261-309](../../../.venv/lib/python3.13/site-packages/uvicorn/server.py#L261)、`limit_max_requests` 经 `on_tick` 不触发 `handle_exit`
- 中间件顺序（[proxy_server.py:1769-1780/15683-15688](../../../litellm/proxy/proxy_server.py#L1769)）：外→内 `RequestSizeLimit` / `SecurityHeaders` / `InFlightRequests` / `PrometheusAuth`
- WebUI 用 Bearer header（`AuthContext.accessToken`，[client.ts:103-127](../../../ui/litellm-dashboard/src/lib/http/client.ts#L103)）；`user_api_key_auth` 主读 header，query `key` 仅 Google route 特判（[user_api_key_auth.py:557-614](../../../litellm/proxy/auth/user_api_key_auth.py#L557)）

**根因**：litellm 直到 lifespan 才知道在关停（晚于 uvicorn 连接排空），且**把「HTTP 响应完成」误当「请求相关工作全部完成」**，脱离的记账 task 越过 teardown 与 DB/redis 断开竞跑。

## 目标

- 每条在途请求可查 HTTP 基础 + LLM 上下文 + 阶段状态（含 `ACCOUNTING`）：LOG、`/health/in-flight`(JSON)、`/health/in-flight/stream`(SSE)、WebUI 实时面板
- 优雅关停在 uvicorn 连接排空阶段就周期播报在途请求与各自 elapsed、卡在哪个 stage
- 关停按序：停接入 → 等 HTTP transport → 按 deadline 排空 managed 记账 task → flush/stop 日志 worker → 断 DB/redis；期间无 watchdog 重连、无 `ClientNotConnectedError`/redis traceback
- 全部改动在 litellm 内，不碰 `prisma-client-py`、不改 site-packages

## 非目标

- gunicorn/hypercorn/granian 的 early-shutdown（各自 lifecycle 不同，本轮**明确不承诺已修**，验收范围限定 uvicorn direct/reload/multiprocess；其它 server 记 backlog）
- 鉴权上下文字段（key/team/user）——用户未选
- 关停期 spend **零丢失**保证——超 deadline 未完成的记账 task 允许放弃，返回明确 tagged 结果并单行记录
- 跨 worker / 跨实例的在途聚合视图——本轮 per-worker 作用域 + 响应标注 `worker_pid`，聚合面记 backlog

## 设计

### A. 早停缝 + 单一关停 deadline（Phase 1 基础）

`DrainingServer(uvicorn.Server)` 覆写两处，覆盖信号与非信号退出：

```python
class DrainingServer(uvicorn.Server):
    def handle_exit(self, sig, frame):          # 信号：瞬间置 flag
        GracefulShutdownManager.start_shutdown()
        super().handle_exit(sig, frame)
    async def shutdown(self, sockets=None):     # 覆盖 limit_max_requests / 程序化 should_exit
        GracefulShutdownManager.start_shutdown()  # 幂等
        # Phase 3 在此/handle_exit 起在途明细播报任务，shutdown 结束取消
        await super().shutdown(sockets)
```

接线：抽出一个保持 uvicorn 原生分支语义的 runner，构造 `Config` + `DrainingServer`，再分派 `ChangeReload(config, target=server.run)`（reload）/ `Multiprocess(config, target=server.run)`（workers>1）/ `server.run()`（direct）三条路径，取代直接 `uvicorn.run`（[proxy_cli.py:1243-1267](../../../litellm/proxy/proxy_cli.py#L1243)）。

单一 deadline：`GracefulShutdownManager` 已存 `_shutdown_started_at`，新增 `deadline_remaining()` 从信号时刻起算。uvicorn connection wait（`timeout_graceful_shutdown`，可配名待定）、记账排空、lifespan teardown 都读 remaining，不各自重开完整窗口。第二次 SIGINT 立即 force-exit（取消 broadcaster/subscriber/managed task）。

### B. Managed task supervisor + work-lease（Phase 1，记账生命周期）

引入 process-scoped `ManagedTaskSupervisor`（复用 `logging_worker` 的 running-task-set/`flush()`/`stop()` 模式，**不**塞进 best-effort 日志 worker）：

- `spawn(coro, *, name, inflight_id: str | None) -> None`——建 task、入 running set、`add_done_callback` 移除并释放对应 work-lease
- `async drain(deadline) -> DrainOutcome`——await running set 至 deadline，返回 `DrainOutcome(drained=int, abandoned=int)`
- 替换裸 `asyncio.create_task` 的记账/日志创建点（流式成功日志 / `update_cache` / spend 更新 / `_batch_database_updates`）

work-lease 让 record 活过 ACCOUNTING：`InFlightRegistry` 每条持 transport + accounting 两类 lease（引用计数为登记表内唯一可变单元，封在方法内）。中间件入口 acquire transport lease、响应完成 release；每个记账 task 经 supervisor 创建时 acquire accounting lease、完成 release。transport 已 release 而 accounting 未清时 `stage=ACCOUNTING`；全部 release 才移除 record。记账 task 通过 `ContextVar current_inflight_id`（评审确认 `create_task` 会复制 context）找回自己的 record。

关停记账边界返回 tagged union：`AccountingCompleted | AccountingSkippedDuringShutdown | AccountingFailed`，由边界 `match` 后单行记录，**不**在各 DB/redis primitive 里各自静默短路。

### C. 关停时序加固：watchdog + redis（Phase 1）

**watchdog**：所有死亡 detector（waitpid 线程、watcher 启动自检、pidfd、`os.kill` 轮询、周期探测、auth/exception-handler 直调、`_handle_writer_engine_replaced`）统一走同步 `_handle_engine_stopped(pid, cause)`；`is_shutting_down()` 分支只 cleanup watcher、**不** reconnect、**不**写 `_expected_engine_deaths`（无替身，语义不符）。在 `attempt_db_reconnect` 取得 `_db_reconnect_lock` 后、进入 recreate 前再 guard 一道，封死已排队 task。`_start_engine_watcher`/周期探测/`_handle_writer_engine_replaced` 关停中拒绝启动/rearm。经 `PrismaClient` 构造参数注入 `Callable[[], bool]`（DI，非 monkeypatch）。

**redis**：`RedisCache.async_increment` 保持 `float or raise`，**不**在底层吞异常。在 proxy 记账边界（`_increment_spend_counter_cache` / `proxy_track_cost_callback`）精确捕 redis `ConnectionError`，关停中映射为 `AccountingSkippedDuringShutdown`，边界单行记录、不重抛。

### D. 在途登记表（Phase 2）

`RequestRecord`（`frozen dataclass(slots=True)`）：`id`(入口 uuid) / `method` / `path` / `client_ip` / `started_at_monotonic` / `started_at_wall` / `model?` / `call_type?` / `provider?` / `streaming?` / `stage`(枚举 `RECEIVED/AUTH/UPSTREAM/STREAMING/ACCOUNTING`)。

`InFlightRegistry` 变更面封装、typed 操作（不用 `**fields` 动态 patch）：`register` / `advance_stage`（单调状态机 + version/sequence compare-and-replace，拒绝倒退）/ `set_llm_context(SetUpstreamContext)` / `acquire_lease` / `release_lease` / `snapshot() -> tuple[RequestRecord, ...]`。terminal reason 明确 `completed | failed | cancelled | shutdown_dropped`。中间件改为 register/lease + 排除 control-plane 路由（in-flight/metrics/health，见 E）；gauge 从业务 record 数派生；入口设 `ContextVar current_inflight_id`。内部 record 经独立 response DTO 输出（不裸序列化 monotonic/内部字段）。

### E. 可观测面：端点 + SSE 协议 + LOG（Phase 2 后端）

- `GET /health/in-flight`——snapshot DTO（含各自 elapsed、`worker_pid`、`scope="worker"`）
- `GET /health/in-flight/stream`——**fetch-based SSE**（后端仍走 Bearer header 的 `user_api_key_auth`，**不**入 URL query），限 `proxy_admin` / 只读 `proxy_admin_viewer`；**排除出业务在途登记表**；generator 同时等 registry event / client 断开 / shutdown 事件，收 shutdown 发 terminal event 即结束，`finally` 里 unsubscribe
- pub/sub 契约：`subscribe()` 原子返回 `{snapshot, sequence, receiver}`，首条发 snapshot、后续事件带单调 sequence；**bounded** 队列，溢出丢中间态发 `resync_required`（服务端推新 snapshot）或断开慢订阅促重连；heartbeat comment 防代理 idle；事件冻结 tagged union `snapshot | registered | updated | deregistered | resync_required | shutdown`，序列化与前端 reducer 均 `match` 穷举
- 周期 LOG 默认关，开关打开每 N 秒在 snapshot 非空时打明细表；关停播报走 F 始终开

### F. 关停播报明细化（Phase 3）

复用 `DrainingServer`，在 `handle_exit`/`shutdown` 起后台任务：uvicorn 排空连接整段每 N 秒 `registry.snapshot()` 打明细（哪些请求、elapsed、stage），结束取消。`GracefulShutdownManager.wait_for_drain` 注入 `detail_fn`（默认 `registry.snapshot`），`drain_waiting`/`timeout` 日志从计数升级为明细，惠及 k8s 路径。

### G. WebUI 实时面板（Phase 4）

新增「Active Requests」页，挂现有 admin 鉴权；用支持 Authorization header 的 **fetch-based SSE 客户端**（优先成熟库，不手搓换行/framing/重连状态机），消费 tagged-union 事件、按 sequence 合并、断线重连拉新 snapshot；表格实时 + elapsed 前端 tick。

## 测试

- **登记表**：`register/advance_stage/lease/snapshot`+pub/sub 单测；`advance_stage` 拒绝倒退；lease 计数到 0 才移除；变异测试
- **记账生命周期**（core）：流式请求响应结束后 success callback 仍持 accounting lease → `stage=ACCOUNTING` 可见；`drain(deadline)` 等到记账完成或到点放弃并返回 `DrainOutcome`；teardown 在 drain 之后
- **watchdog 竞态**：reconnect task 已创建阻塞在 lock，随后置 shutdown，释放 lock 后断言无 probe/recreate/rearm；各死亡入口在 shutdown 下均只 cleanup 不 reconnect
- **redis**：注入 `ConnectionError` + shutdown，断言边界返回 `AccountingSkippedDuringShutdown`、单行日志、`async_increment` 契约不变、不重抛
- **SSE**：无 credential / 普通 key / admin viewer / proxy admin 四类身份 + 自定义 auth header；慢消费/队列溢出/GET+SSE 建连竞态/断线重连/shutdown 关闭；**保持一个 SSE client 打开 + 一条长业务请求 + SIGINT，断言 SSE 自动关闭、业务请求继续被播报、不被拖到超时**
- **uvicorn 入口**（Phase 1 必做，真 subprocess signal E2E）：direct/reload/workers=2/`limit_max_requests` 四入口；捕获日志断言顺序 `graceful_shutdown_started → Waiting for connections to close → ≥1 条带明细 drain log → 请求结束或 deadline → 记账 settled/skipped → teardown`，且**不**出现 reconnect/`ClientNotConnectedError`/redis traceback；第二次 SIGINT force-exit 生效

## 分期

1. **Phase 1 — 关停正确性（先修 bug）**：A（`DrainingServer` + 单一 deadline + 原生三分支 runner）+ B（task supervisor + work-lease + 记账 tagged 结果）+ C（watchdog 统一入口/锁后 guard/DI + redis 边界归位）+ 真 subprocess E2E。**独立可交付**
2. **Phase 2 — 登记表 + 可观测后端**：D + E（端点 / SSE 协议 / LOG）
3. **Phase 3 — 关停播报明细化**：F
4. **Phase 4 — WebUI 实时面板**：G

依赖：A/B 的 lease 与 supervisor 是 ACCOUNTING 可观测（D/E）与关停排空的共同底座，故 Phase 1 先立；F 复用 A 的 Server + D 的登记表；G 依赖 E 的 SSE 协议。

## record-not-adopted / 已记 backlog

- 跨 worker / 跨实例在途聚合视图——本轮仅 per-worker + `worker_pid` 标注，聚合面需另设共享事件面，记 backlog
- gunicorn/hypercorn/granian 的 early-shutdown hook——本轮验收不含，记 backlog（各自 lifecycle 单列）
- 关停期 spend 零丢失（同步强制 flush 全部记账）——超 deadline 放弃 + tagged 记录，零丢失记 backlog

## 待评审重点（round-2）

- work-lease 引用计数与 `ContextVar` 在「流式 + 多子任务并发记账」下的 acquire/release 配平边界是否有遗漏路径
- `ManagedTaskSupervisor` 与 `logging_worker` 是否抽出共享 `ManagedTaskSet` 工具，还是各自持有（避免过度抽象 vs 避免重复）
- 单一关停 deadline 的配置名与 uvicorn `timeout_graceful_shutdown` 的映射
