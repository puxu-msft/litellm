# 在途请求可观测 + 优雅关停播报 + 关停时序加固

状态：设计经三轮 GPT reviewer 对抗性评审（round-1 3 blocker/6 major/2 minor；round-2 6 闭合/5 部分含 2 blocker/3 新 major；round-3 逐点核对真实调度拓扑后仅剩 1 blocker、本版补齐 LoggingWorker 联合 quiesce 契约，评审背书「可进入实施计划」），全程核对真实代码 + uvicorn/prisma SDK 源码 + 最小复现 + 跑现有测试，本版冻结全部契约，待用户复核 gate
日期：2026-07-14 初稿；同日 round-1 重写、round-2、round-3 三次修订
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

## 修订说明（round-2 评审吸收）

round-2 背书：SSE 鉴权/自我登记/背压、uvicorn 三分支注入、真 E2E、中间件事实均已闭合。`ContextVar` 在已核对的主成功路径上能传到首次 scheduling boundary（非流式 success 在 wrapper 返回前建、标准流式在 iterator 抛 `StopAsyncIteration` 前建、pass-through 在 generator `finally` enqueue 前建，`LoggingWorker.enqueue` 用 `contextvars.copy_context()`）——未闭合的是 lease ownership，不是 context 传播。

round-2 冻结的 5 项契约（本版落定）：

1. **记账 lease 必须绑到真实 `GLOBAL_LOGGING_WORKER` 队列项**，不是替换表面的 `asyncio.create_task`。真实链是两级：wrapper 建短命 task → `_client_async_logging_helper` 把 `async_success_handler` enqueue 到 `GLOBAL_LOGGING_WORKER`（[utils.py:1071-1091/1738-1767](../../../litellm/utils.py#L1071)）→ worker 稍后 dequeue 建 processing task（[logging_worker.py:82-151](../../../litellm/litellm_core_utils/logging_worker.py#L82)）→ `_ProxyDBLogger.async_log_success_event` 内再派生 `update_cache` + `_batch_database_updates`（[db_spend_update_writer.py:121-224](../../../litellm/proxy/db/db_spend_update_writer.py#L121)）。若只换外层 task，lease 在 enqueue 后即释放、真回调仍在队列里，`drain()` 的 running set 为空、teardown 照断 DB
2. **quiesce 时序**：`drain` 与 flush 的方向必须是「先 flush 日志队列产出记账 work，再 fixed-point 排空 managed child」，不是反过来；deadline 到期必须 **cancel + `gather(return_exceptions=True)` 等终止**，再写 `shutdown_dropped`、释放 lease，最后才断 aiohttp/prisma/redis
3. **IAM token refresh 的第二条 recreate 路径**（`PrismaWrapper._safe_refresh_token → _recreate_prisma_client_locked`，[prisma_client.py:410-525](../../../litellm/proxy/db/prisma_client.py#L410)，走独立 `_reconnection_lock`）**不经** `attempt_db_reconnect`，需单独注入 shutdown policy 并在 loop 醒来/拿锁后/recreate 前三处 guard；lifespan 一进入优先 cancel/await refresh
4. **redis 单行降级**：底层 `async_increment` 在 raise 前已 `verbose_logger.error` + 派生 failure-hook task（[redis_cache.py:896-916](../../../litellm/caching/redis_cache.py#L896)），故须**删底层那句重复 error**（由调用边界按语境记）或注入 typed error reporter，且 failure-hook 纳入 managed task 或关停预期关闭时不派生——只改 proxy caller 达不到「单行」
5. **registry 改为内部原子 reducer**（单事件循环、方法内同步无 await）：调用者只传 typed intent、不传 expected version；`advance_stage` 用有序 transition/`max`、terminal 后拒更新；`set_llm_context` 只替换自有字段；每次 mutation 递增 record version + global sequence；mutation 与向 subscriber 发布之间不 await。不暴露 CAS 给调用者（暴露则须返回 `Applied|Conflict|Missing|Terminal` tagged union 由上层重试，不静默返 None）
6. **`wait_for_drain` 计数口径改为 transport lease 数**，不含 accounting lease，否则它先按完整 deadline 等记账、留给 flush/drain 的时间为 0

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

单一 deadline：复用现有 `GRACEFUL_SHUTDOWN_TIMEOUT` 作唯一 operator-facing 配置（不新增第二个 timeout）。`start_shutdown()` 首次调用冻结 `deadline = monotonic() + validated_timeout`，后续环境变化/重复调用不重置；新增 `deadline_remaining()`。`DrainingServer.shutdown()` 在 `super().shutdown()` 前把 `config.timeout_graceful_shutdown` 设为当刻 `max(0, deadline_remaining())`；lifespan 各 drain 读同一绝对 deadline，不收相对 timeout。deadline 到期进入 cancel-and-settle（非再开 cleanup 窗口）。第二次 SIGINT 立即 force-exit（取消 broadcaster/subscriber/logging queue item/managed task）。

### B. Managed task supervisor + work-lease（Phase 1，记账生命周期）

**lease 绑到真实调度拓扑，不是替换表面 `asyncio.create_task`。** 记账主链是两级队列：wrapper 短命 task → `_client_async_logging_helper` enqueue 到 `GLOBAL_LOGGING_WORKER` → worker dequeue 建 processing task → `_ProxyDBLogger.async_log_success_event` 派生 `update_cache` + `_batch_database_updates`。故 lease 在**最早的 logging enqueue 边界**同步 acquire，把 `inflight_id` + once-only lease token 存入 `LoggingTask` queue item；该 item 被处理/拒收/丢弃/取消/loop-rebind 时统一 release，`LoggingWorker` 的关停 quiesce（见 C）因而成为 accounting drain 的一环。

**不让通用 `LoggingWorker` 依赖 proxy 类型。** `LoggingTask` 从 mutable `TypedDict` 改为 `frozen dataclass(slots=True)`，新增可选中立字段 `token: CompletionToken | None`（`CompletionToken` 是中立 protocol，`token.settle(outcome)` 幂等；proxy 的 `AccountingLease` 实现它，纯 SDK 传 `None`）。`ensure_initialized_and_enqueue` 加 keyword-only `token=None`。worker 在唯一 `finally` settle 非空 token；`_schedule_delayed_enqueue_retry` 无 loop 直接 drop、`_retry_enqueue_task` 重试放弃、`_aggressively_clear_queue_async`、`_ensure_queue` 换 loop 丢旧 queue 等**每一条 drop/rebind 路径都必须 settle token**，否则 record 泄漏。SDK（`token=None`）路径行为完全不变，加纯 SDK 回归测试守此。

`ManagedTaskSet`（抽出的极小共享件，组合非继承；标准 asyncio 语义：强引用 task 集 + done-callback 移除 + cancel-all + await-settlement + fixed-point empty 检查）被 `LoggingWorker` 与新 supervisor 共用；**不**合并二者高层 drain 语义（LoggingWorker 的 bounded queue/best-effort overflow 与 accounting 的 lease/admission/deadline outcome 各自持有）。

**admission 授权用不可伪造 scope，不靠 `ContextVar != None`。** 已登记 parent 持 `AccountingScope`；`spawn_child(scope, ...)` 同步临界段先 `admissions_in_progress += 1`、acquire child lease、入 set、末尾 `-= 1`。root admission 关闭后，只有携带**仍有效** parent scope 的 child 被接受（custom callback 即使 `current_inflight_id` 仍在、无有效 scope 也拒绝作新 root 并 settle/close coroutine）；parent 完成后其 scope 失效，防止 callback 在任意未来时刻再派生。

`ManagedTaskSupervisor`（process-scoped）：
- `spawn_child(scope, coro, *, name) -> None`——建 task 入集合，`add_done_callback` 移除并 once-only settle lease；task creation 失败须回滚已 acquire 的 lease、关闭未调度 coroutine
- `async drain() -> DrainOutcome`——读单一 deadline，fixed-point 稳定空判定 `root_queue_unfinished == 0 && accounting_tasks == 0 && admissions_in_progress == 0`（每轮从当前 snapshot await 后重读，让事件循环至少推进一轮）；到期 cancel 全部 remaining 并 `gather(return_exceptions=True)` 等终止

**child 分类**：并非所有派生都是必须在 teardown 前完成的 accounting child。`update_cache` / `_batch_database_updates` 属 accounting（必须 drain）；`budget_alerts` / `async_set_cache_pipeline` / `failed_tracking_alert` / service logging hooks 属 telemetry（deadline 可直接取消）。规格按此分类，实现计划逐一落表。

work-lease 让 record 活过 ACCOUNTING：`InFlightRegistry` 每条持 transport + accounting 两类 lease（引用计数为登记表内唯一可变单元，封在方法内）。中间件入口 acquire transport lease、响应完成 release；**每个脱离父 coroutine 的 ownership boundary**（success / failure / pass-through logging / `update_cache` / `_batch_database_updates`）acquire 一个 accounting lease，awaited 的子调用（如 `_update_database_and_spend_counters`）不另取、由父 lease 覆盖。transport 已 release 而 accounting lease>0 时 registry 内部原子推进到 `ACCOUNTING`；全部 lease 为 0 才写 terminal reason 并 deregister。`ContextVar current_inflight_id` 用于入口，但 queue item **显式保存 `inflight_id` + token**，关键生命周期不只靠隐式 context。release 重复调用返回 typed invariant error，不把计数减成负。

关停记账边界返回 tagged union：`AccountingCompleted | AccountingSkippedDuringShutdown | AccountingFailed`，由边界 `match` 后单行记录，**不**在各 DB/redis primitive 里各自静默短路。

### C. 关停 quiesce 时序契约（Phase 1，核心）

现有 lifespan（[proxy_server.py:1071-1102](../../../litellm/proxy/proxy_server.py#L1071)）顺序为 `wait_for_drain → close aiohttp → stop token refresh → stop watchdog → proxy_shutdown_event(断 prisma/cache)`。改为冻结的 quiesce 协议（顺序即正确性）：

1. uvicorn 停接入、排空 HTTP transport
2. lifespan 进入后**停非请求后台 producer 但暂不关其依赖**：cancel/await IAM token refresh loop、stop watchdog 周期 loop 与 engine watcher
3. 封闭新的 root accounting admission，但**允许持有效 scope 的已登记 accounting task 派生 child**
4. **`LoggingWorker.quiesce()` flush 仍持 accounting lease 的 logging queue**，让顶层 success/failure callback 真正开始并完成或进入 supervisor
5. supervisor **fixed-point** drain（先 flush 产出 work、再排空 child，方向不可反）
6. deadline 到期 **联合** cancel：**LoggingWorker 的 queue item + 运行中 processing task + retry/aggressive-clear helper + supervisor child 全部**，再 `gather(return_exceptions=True)` 等 settlement
7. 对每条未完 record 原子写 `shutdown_dropped`、settle remaining lease
8. stop logging worker
9. 关 shared aiohttp（须在 callback drain 之后，不能停在现位）、prisma、redis

**LoggingWorker 加独立 `quiesce(deadline, admission_policy) -> LoggingDrainOutcome`，不复用 `flush()`/`stop()`**（现有 `flush()` 只 `await queue.join()`、不冻结 admission、不取消；`stop()` 的取消路径会调 `clear_queue()`，可能在 deadline 后仍执行 queued coroutine，正是 round-2 blocker 复发点）。`quiesce` 契约：关闭 proxy root logging admission（SDK 非 proxy admission 是否续由调用方决定）；正常阶段等 queue join 让已登记 item 执行；deadline 后**不再执行 queued coroutine**，而是逐项 close coroutine、settle token、`task_done`；cancel worker processing/retry/aggressive-clear 并 await settlement；**禁止取消路径再调 `clear_queue()` 执行业务 callback**；fixed-point **联合**检查 LoggingWorker 与 supervisor（不只查 supervisor）。现有 `GLOBAL_LOGGING_WORKER.flush()` 全仓仅 3 处测试调用，故普通 `flush()` 语义保持不变、只新增 proxy quiesce 路径。

`drain()`/`quiesce()` 返回冻结 tagged outcome：`Drained | DeadlineExceeded(cancelled, cancellation_failed) | ForcedExit`——仅返回整数不足以让 teardown 判断能否安全继续。`wait_for_drain` 计数口径改为 **transport lease 数**（不含 accounting）。

### C2. watchdog + IAM refresh + redis（Phase 1）

**watchdog**：所有 engine 死亡 detector（waitpid 线程、watcher 启动自检、pidfd、`os.kill` 轮询、周期探测、auth/exception-handler 直调、`_handle_writer_engine_replaced`）统一走同步 `_handle_engine_stopped(pid, cause)`；`is_shutting_down()` 分支只 cleanup watcher、**不** reconnect、**不**写 `_expected_engine_deaths`（无替身，语义不符）。`attempt_db_reconnect` 取得 `_db_reconnect_lock` 后、recreate 前再 guard，封死已排队 task。经 `PrismaClient` 构造参数注入 `Callable[[], bool]`（DI）。

**IAM token refresh（第二条 recreate 路径）**：`PrismaWrapper._safe_refresh_token → _recreate_prisma_client_locked`（走独立 `_reconnection_lock`、不经 `attempt_db_reconnect`）单独注入同一 shutdown policy——`_token_refresh_loop` 醒来后、`_safe_refresh_token` 取 `_reconnection_lock` 后、`_recreate_prisma_client_locked` kill/spawn 前三处 guard。**不**把 IAM refresh 硬塞进 `_handle_engine_stopped`（它不是死亡 detector），共享的是「是否允许 recreate」policy。lifespan 第 2 步优先 cancel/await refresh task。

**redis**：`RedisCache.async_increment` 保持 `float or raise`。为达「单行降级」须**删底层重复的 `verbose_logger.error`**（由调用边界按语境记）或注入 typed error reporter/policy；service failure-hook 纳入 managed task 或关停预期关闭时不派生。proxy 记账边界（`_increment_spend_counter_cache`）精确捕 redis `ConnectionError`，关停中映射为 `AccountingSkippedDuringShutdown` 单行记录、不重抛。

### D. 在途登记表（Phase 2）

`RequestRecord`（`frozen dataclass(slots=True)`）：`id`(入口 uuid) / `method` / `path` / `client_ip` / `started_at_monotonic` / `started_at_wall` / `model?` / `call_type?` / `provider?` / `streaming?` / `stage`(枚举 `RECEIVED/AUTH/UPSTREAM/STREAMING/ACCOUNTING`) / `version`。

`InFlightRegistry` 为**内部原子 reducer**（单事件循环、方法内同步无 await，mutation 与向 subscriber 发布之间不 await）：调用者只传 typed intent、不传 expected version。`register` / `advance_stage`（有序 transition 或 `max(current, requested)`，terminal 后拒更新）/ `set_llm_context`（只替换自有字段）/ `acquire_lease` / `release_lease` / `snapshot() -> tuple[RequestRecord, ...]`；每次成功 mutation 递增 record `version` + global event sequence。terminal reason 明确 `completed | failed | cancelled | shutdown_dropped`。**不**暴露 CAS 给调用者（若确需跨线程乐观并发，CAS API 须返回 `Applied | Conflict | Missing | Terminal` tagged union 由上层重试，不返 None 静默丢）。

中间件改为 register/transport-lease，并注入**同步纯函数 `RequestTrackingPolicy`** 排除 control-plane 请求——只依赖入口已有的 `scope["type"]`/规范化 `scope["path"]`/method（route 尚未 resolved，**不**读 route tag、**不**对原始 URL 做 substring）：`/metrics` 与新 in-flight 端点用精确路径、对整个 `/health` namespace（含 `/health/drain` 与其既有 `exclude_self` 自计数语义、readiness/liveness/backlog）作显式决定并测试、兼容 `root_path`、非 HTTP scope 一律不登记、exclusion reason 入 typed enum。gauge 从业务 record 数派生；入口设 `ContextVar current_inflight_id`；内部 record 经独立 response DTO 输出。

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
- **记账生命周期**（core）：流式 + 非流式 + pass-through 请求响应结束后 success/failure callback 仍持 accounting lease → `stage=ACCOUNTING` 可见；lease 在最早 enqueue 边界 acquire、经 `LoggingWorker.flush()` 与 supervisor drain 后才全 release；漏 release（record 永不移除）与早 release（teardown 提前断 DB）各设反向断言；`drain()` 返回 `Drained | DeadlineExceeded | ForcedExit`，deadline 到期 cancel + settle，abandoned record 写 `shutdown_dropped`；**quiesce 顺序断言**：flush 日志队列在 supervisor drain 之前、断 aiohttp/prisma/redis 在记账 drain 之后
- **watchdog 竞态**：reconnect task 已创建阻塞在 `_db_reconnect_lock`，随后置 shutdown，释放 lock 后断言无 probe/recreate/rearm；各死亡入口在 shutdown 下均只 cleanup 不 reconnect
- **IAM refresh 竞态**：refresh task 已阻塞在 `_reconnection_lock`，随后 shutdown，释放 lock 后断言不调用 `_recreate_prisma_client_locked`
- **redis**：注入 `ConnectionError` + shutdown，断言边界返回 `AccountingSkippedDuringShutdown`、单行日志（底层不再重复 error、failure-hook 不脱离）、`async_increment` 契约不变、不重抛
- **control-plane 排除**：`/metrics`、`/health/in-flight`、`/health/in-flight/stream`、`/health/drain`、readiness/liveness/backlog 各断言是否登记；`root_path` 部署下路径规范化；非 HTTP scope 不登记
- **registry 并发**：同一 record 上 `set_llm_context` 与 `advance_stage` 并发，断言无 lost-update、stage 不倒退、version/sequence 单调
- **LoggingWorker quiesce**（core）：deadline 到期后，联合断言 queue 未 dequeue item / 运行中 processing task / retry / aggressive-clear helper **全部被取消并 settle token**，且取消路径**不**再执行 queued 业务 callback（不触 DB）；fixed-point 联合 LoggingWorker + supervisor 才判空
- **纯 SDK 无侵入**：不设 registry/`inflight_id`（`token=None`）时 enqueue/flush/stop 行为与改动前完全一致；drop/rebind/retry 路径对 None token 不崩、对非空 token 必 settle
- **admission scope**：root admission 关闭后，持有效 `AccountingScope` 的 child 被接受、无效 scope（含 `current_inflight_id` 仍在但 parent 已完成）作新 root 被拒并 settle/close
- **SSE**：无 credential / 普通 key / admin viewer / proxy admin 四类身份 + 自定义 auth header；慢消费/队列溢出/GET+SSE 建连竞态/断线重连/shutdown 关闭；**保持一个 SSE client 打开 + 一条长业务请求 + SIGINT，断言 SSE 自动关闭、业务请求继续被播报、不被拖到超时**
- **uvicorn 入口**（Phase 1 必做，真 subprocess signal E2E）：direct/reload/workers=2/`limit_max_requests` 四入口；捕获日志断言顺序 `graceful_shutdown_started → Waiting for connections to close → ≥1 条带明细 drain log → 请求结束或 deadline → 记账 settled/skipped → teardown`，且**不**出现 reconnect/`ClientNotConnectedError`/redis traceback；第二次 SIGINT force-exit 生效

## 分期

1. **Phase 1 — 关停正确性（先修 bug）**：A（`DrainingServer` + 单一 deadline + 原生三分支 runner）+ B（`ManagedTaskSet` + supervisor + work-lease 绑 logging queue item + 记账 tagged 结果）+ C（quiesce 时序契约）+ C2（watchdog 统一入口/锁后 guard + IAM refresh guard + redis 边界归位，全 DI）+ 真 subprocess E2E。**独立可交付**
2. **Phase 2 — 登记表 + 可观测后端**：D + E（端点 / SSE 协议 / LOG）
3. **Phase 3 — 关停播报明细化**：F
4. **Phase 4 — WebUI 实时面板**：G

依赖：A/B/C 的 lease、supervisor 与 quiesce 序是 ACCOUNTING 可观测（D/E）与关停排空的共同底座，故 Phase 1 先立；F 复用 A 的 Server + D 的登记表；G 依赖 E 的 SSE 协议。

## record-not-adopted / 已记 backlog

- 跨 worker / 跨实例在途聚合视图——本轮仅 per-worker + `worker_pid` 标注，聚合面需另设共享事件面，记 backlog
- gunicorn/hypercorn/granian 的 early-shutdown hook——本轮验收不含，记 backlog（各自 lifecycle 单列）
- 关停期 spend 零丢失（同步强制 flush 全部记账）——超 deadline 放弃 + tagged 记录，零丢失记 backlog

## round-2 待评审点裁决（已落定）

- **work-lease 配平**：采用显式 once-only `AccountingLease` token，绑 `inflight_id` + logging queue item；每个 detached ownership boundary 各取一 lease、awaited 子调用不取；task 创建失败回滚、release 幂等 once-only（见 B）
- **`ManagedTaskSet`**：抽极小共享件、组合非继承、纯标准 asyncio；不合并 LoggingWorker 与 supervisor 的高层 drain 语义（见 B）
- **单一 deadline**：复用 `GRACEFUL_SHUTDOWN_TIMEOUT`，首次 `start_shutdown` 冻结绝对 deadline，`DrainingServer.shutdown` 前映射到 `config.timeout_graceful_shutdown`，`wait_for_drain` 只数 transport lease（见 A/C）

## round-3 待评审点裁决（已落定，评审背书可进入实施计划）

- **fixed-point 收敛**：核对真实派生链（`_ProxyDBLogger` → `update_cache`/`_batch_database_updates` → 各 helper，深度有限、无递归重入 accounting root），显式 `admissions_in_progress` 计数 + `AccountingScope` + 单一绝对 deadline 下会收敛，无结构性活锁（见 B）
- **`LoggingWorker` 对纯 SDK 无副作用**：`LoggingTask` 加中立可选 `CompletionToken`（SDK 传 None、行为不变），proxy 走**新增 `quiesce()`** 而非改动普通 `flush()`；全仓 `flush()` 仅 3 处测试调用（[test_openai_batches_and_files.py:55](../../../tests/batches_tests/test_openai_batches_and_files.py#L55) / [test_tpm_rpm_routing_v2.py:562](../../../tests/local_testing/test_tpm_rpm_routing_v2.py#L562) / [test_no_duplicate_spend_logs.py:116](../../../tests/test_litellm/responses/test_no_duplicate_spend_logs.py#L116)），语义保持（见 C）

