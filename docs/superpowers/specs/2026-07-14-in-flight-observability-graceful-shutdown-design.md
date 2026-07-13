# 在途请求可观测 + 优雅关停播报 + 关停时序加固

状态：设计初稿，待用户复核 gate（尚未评审、尚未实现）
日期：2026-07-14 初稿
分支：`ghc`
关联：现有 `GracefulShutdownManager`（[graceful_shutdown_manager.py](../../../litellm/proxy/shutdown/graceful_shutdown_manager.py)）、`InFlightRequestsMiddleware`（[in_flight_requests_middleware.py](../../../litellm/proxy/middleware/in_flight_requests_middleware.py)）

## 背景与问题

单进程运行的 litellm proxy，交互式 `Ctrl+C`（SIGINT）关停时出现两类痛点，一段真实日志同时暴露了它们：

```
^CINFO:     Shutting down
INFO:     Waiting for connections to close. (CTRL+C to force quit)
02:00:33 - prisma-query-engine PID 3113457 exited (waitpid thread); triggering reconnect.
02:00:33 - Attempting Prisma DB reconnect. reason=engine_process_death
（静默约 3 分钟，用户按了上箭头）
INFO:     Waiting for application shutdown.
02:03:54 - SpendCounterReseed.from_db: failed ... prisma.errors.ClientNotConnectedError
02:03:54 - LiteLLM Redis Caching: async async_increment() - Got exception ... Connection closed by server.
INFO:     Application shutdown complete.
```

拆出三件可分离的事，用户确认三件全做，且**关停时序 bug（第三件）先修**：

1. **在途请求不可观测（明细，不只是计数）**——运行中无法知道「现在有哪些请求在跑、各跑了多久、哪个模型、卡在哪个阶段」，LOG 和 WebUI 都没有这个面
2. **优雅关停不播报仍在途的请求与各自已耗时**——`Ctrl+C` 后 uvicorn 静默等了 3 分钟，用户完全不知道在等什么、等了多久
3. **关停时序 bug**——engine 死亡触发 watchdog 在关停期反复重连；在途请求收尾记账撞上已断的 DB/redis，`ClientNotConnectedError` 与 redis 断连 traceback 刷屏

### 已核实的现状

**在途追踪已存在，但只是个整数计数器，无明细、无前端。**

- [in_flight_requests_middleware.py](../../../litellm/proxy/middleware/in_flight_requests_middleware.py) 的 `InFlightRequestsMiddleware` 进请求 `_in_flight += 1`、出请求 `-= 1`，只维护一个整数
- 只在两处露出且都只有数字：`/health/backlog`（[_health_endpoints.py:1582-1595](../../../litellm/proxy/health_endpoints/_health_endpoints.py#L1582)，返回 `{"in_flight_requests": N}`）与 Prometheus gauge `litellm_in_flight_requests`
- WebUI 无任何在途/活跃请求面（`view_logs` 那套查的是历史 SpendLogs，非在途）
- 中间件挂载顺序：[proxy_server.py:1778-1780](../../../litellm/proxy/proxy_server.py#L1778)，`InFlightRequestsMiddleware` 足够靠外，覆盖近乎全程

**优雅排空已有骨架，但站错层，在交互式关停里空转。**

- [graceful_shutdown_manager.py](../../../litellm/proxy/shutdown/graceful_shutdown_manager.py) 的 `wait_for_drain` 每 5s 打 `drain_waiting in_flight_requests=N elapsed_s=X`，只有计数
- 它挂在 lifespan shutdown（[proxy_server.py:1073-1074](../../../litellm/proxy/proxy_server.py#L1073) 先 `start_shutdown()` 再 `wait_for_drain()`）
- **uvicorn 关停两阶段**：先 `Waiting for connections to close.`（uvicorn 自己等连接跑完，默认无限等），再 `Waiting for application shutdown.`（此时 lifespan shutdown 才执行）。3 分钟静默卡在**第一阶段**；等进入第二阶段在途已是 0，`wait_for_drain` 立即返回，故日志里一行 `drain_waiting` 都没有

**关停时序 bug 的真实机理（推翻了「隔离 prisma 子进程」的初判）。**

- prisma-client-py **故意**让 engine 子进程接收 SIGINT：[prisma/engine/query.py:195-196](../../../.venv/lib/python3.13/site-packages/prisma/engine/query.py#L195) 用 `preexec_fn` 把 SIGINT/SIGTERM 解除屏蔽，disconnect 时还主动 `send_signal(signal.SIGINT)`（[:100](../../../.venv/lib/python3.13/site-packages/prisma/engine/query.py#L100)）。**engine 随终端 SIGINT 一起死是它的设计意图，不是 bug；反向隔离子进程是错的方向**
- 真正的 bug 在 litellm：engine 一死，DB watchdog 把它当「意外死亡」触发重连（[utils.py `_on_engine_death_from_thread`](../../../litellm/proxy/utils.py#L4245) → `triggering reconnect` → `Attempting Prisma DB reconnect`），在关停期 thrash
- watchdog 本该被 `stop_db_health_watchdog_task()`（[utils.py:4678](../../../litellm/proxy/utils.py#L4678)）停掉，但那行在 lifespan（[proxy_server.py:1096](../../../litellm/proxy/proxy_server.py#L1096)），晚于连接排空
- `SpendCounterReseed.from_db` 内部已 try/except 并 `return None`（[spend_counter_reseed.py:118-120](../../../litellm/proxy/db/spend_counter_reseed.py#L118)），traceback 刷屏来自那句 `verbose_proxy_logger.exception(...)`。reseed 由记账/鉴权路径触发（[proxy_server.py:2183/2200/2253/2583/2620](../../../litellm/proxy/proxy_server.py#L2183)）
- litellm **已有**「计划内死亡就不重连」机制：`_consume_expected_death(pid)`（[utils.py:4222](../../../litellm/proxy/utils.py#L4222)）读 `self.db._expected_engine_deaths`，IAM 刷新做计划内重建时靠它跳过重连

**根因一句话**：litellm 直到 lifespan 才知道自己在关停，而这**晚于**真正卡住的 uvicorn 连接排空阶段，导致 watchdog 重连、在途记账在整个窗口里都不知道该停手。

### 关键架构洞察

组件 2（关停播报）与组件 3（时序 bug）**共用同一道缝**——「在信号层就置起早停标志」。uvicorn `Server.handle_exit(sig, frame)`（[uvicorn/server.py:334](../../../.venv/lib/python3.13/site-packages/uvicorn/server.py#L334)，信号在 [:322](../../../.venv/lib/python3.13/site-packages/uvicorn/server.py#L322) 安装）是干净的覆写点。自建 `Server` 子类在此调 `GracefulShutdownManager.start_shutdown()` 即可让 `is_shutting_down()` 在信号处就为真（幂等，lifespan 那次自动变 no-op）。修 bug 需要这道缝，而它正是播报要建的缝，故两者绑定。

## 目标

- 运行中可查每条在途请求的 HTTP 基础 + LLM 上下文 + 阶段状态：LOG、`/health/in-flight`(JSON)、`/health/in-flight/stream`(SSE)、WebUI 实时面板
- 优雅关停在 uvicorn 连接排空阶段就周期性播报仍在途的请求与各自 elapsed、卡在哪个 stage
- 关停期不再有 watchdog 重连 thrash、不再有 `ClientNotConnectedError`/redis 断连 traceback 刷屏
- 全部改动在 litellm 内，不碰 `prisma-client-py`、不改 site-packages

## 非目标

- 多 worker（`workers>1`）/ gunicorn / hypercorn / granian 下的自建 Server 播报（保持现状，标志仍由 lifespan 兜底置起；文档标注局限）
- 鉴权上下文字段（key alias/hash、team、user）——用户未选，避免鉴权层耦合
- 关停期 spend 的零丢失保证（关停时丢失少量未 flush 的 spend 计数可接受，降级为一行日志而非同步强制 flush）
- k8s preStop/`/health/drain` 路径的重构（保持能用即可）

## 设计

### 组件 0：早停标志缝（Phase 1 基础）

自建 `uvicorn.Server` 子类，覆写 `handle_exit`：

```python
class DrainingServer(uvicorn.Server):
    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        GracefulShutdownManager.start_shutdown()  # 信号处即置 is_shutting_down()
        # Phase 3 在此追加：启动在途明细播报后台任务
        super().handle_exit(sig, frame)
```

接线（[proxy_cli.py:1264](../../../litellm/proxy/proxy_cli.py#L1264)）：`workers<=1` 路径用 `uvicorn.Config(**uvicorn_args)` + `DrainingServer(config)` + `server.run()` 取代 `uvicorn.run(...)`，并给 `timeout_graceful_shutdown` 一个可配的合理默认（不再无限等）。`workers>1` 与其它 server 保持现状。

`start_shutdown()` 幂等（[graceful_shutdown_manager.py:77](../../../litellm/proxy/shutdown/graceful_shutdown_manager.py#L77) `if cls._is_shutting_down: return`），故信号处早调 + lifespan 再调不冲突。

### 组件 4：关停时序加固（Phase 1，先修）

三个消费方读 `GracefulShutdownManager.is_shutting_down()`：

1. **DB watchdog 关停中跳过重连**——`_on_engine_death_from_thread` 与重连路径在 `is_shutting_down()` 时把 engine 死亡视为预期（复用 `_consume_expected_death`/`_expected_engine_deaths` 语义，或直接短路 `return`），并停止调度新探测。消除 `triggering reconnect` thrash
2. **reseed 关停中碰 DB 前短路**——`SpendCounterReseed.from_db` 首行加 `if is_shutting_down(): return None`，压根不进 query，无异常无 traceback。（`from_db` 已 catch+return None，只是不再触发那句 `.exception`）
3. **redis spend 写降级**——关停 + 连接已断时，`redis_cache` 的 `async_increment` 精确捕 redis `ConnectionError`（[redis_cache.py:911](../../../litellm/caching/redis_cache.py#L911) 附近）在 `is_shutting_down()` 下降级为一行 `debug`，不打 error。**只精确捕连接类异常并带原因降级，不无差别吞异常**（守 `never-swallow-errors`）

依赖注入：三处消费方接收一个 `is_shutting_down: Callable[[], bool]`（默认 `GracefulShutdownManager.is_shutting_down`），单测可注入假 flag，不 monkeypatch。

### 组件 1：在途登记表（Phase 2）

替换只计数的中间件。`RequestRecord` 冻结、`InFlightRegistry` 封装唯一可变单元。

`RequestRecord`（`frozen dataclass(slots=True)`）字段：

| 字段 | 来源层 | 说明 |
|---|---|---|
| `id` | 中间件 | 进入时发 uuid（`litellm_call_id` 在深层才生成，[common_request_processing.py:1117](../../../litellm/proxy/common_request_processing.py#L1117)，不可复用于入口） |
| `method` / `path` | 中间件 | 如 `POST /v1/messages` |
| `client_ip` | 中间件 | scope 里取 |
| `started_at_monotonic` / `started_at_wall` | 中间件 | monotonic 算 elapsed，wall 供展示 |
| `model` / `call_type` / `provider` / `streaming` | 路由/预调用层回填 | `common_request_processing` / `litellm_pre_call_utils` |
| `stage` | 全程更新 | 枚举 `RECEIVED / AUTH / UPSTREAM / STREAMING / ACCOUNTING` |

`InFlightRegistry`（进程级，方法封装变更面）：

- `register(record) -> None` / `deregister(id) -> None`
- `update(id, **fields) -> None`——**替换**该 id 的冻结 record（`dataclasses.replace`），非原地改字段，符合项目不可变约束
- `snapshot() -> tuple[RequestRecord, ...]`——供 JSON 端点与 LOG
- `subscribe() -> asyncio.Queue` / `unsubscribe(q)`——供 SSE 的 pub/sub；`register/update/deregister` 向各订阅队列投递事件
- 单事件循环、asyncio 协作式，dict 操作在 await 之间原子，无需锁

`stage` 是关停播报判断「这条请求到底卡在哪」的关键，由请求生命周期在各阶段 `update`。

`InFlightRequestsMiddleware` 改为 `register`/`deregister` record，Prometheus gauge 从 `len(registry)` 派生保留；入口把 `id` 塞进 `ContextVar current_inflight_id`，深层回填点通过它拿 id 调 `update`。

否掉的备选：计数与明细分两处存（漂移）；只存 logging obj（鉴权前不存在、不可实时查）。

### 组件 2：可观测面——端点 + LOG（Phase 2 后端）

- `GET /health/in-flight`——`registry.snapshot()` 序列化为 JSON（record 列表 + 各自 elapsed），落在 `/health/backlog` 旁
- `GET /health/in-flight/stream`——SSE，订阅登记表 pub/sub，实时推送进/出/阶段变更事件；前端 `EventSource` 直连
- 周期性 LOG——默认**关**（避免刷屏），开关打开后每 N 秒在 `snapshot()` 非空时打一张明细表。关停播报走组件 3，始终开

鉴权：两个端点复用 `/health/*` 现有鉴权语义。

### 组件 3：关停播报明细化（Phase 3）

复用组件 0 的 `DrainingServer`。`handle_exit`（或 `shutdown`）里起一个后台任务：uvicorn 排空连接的整段每 N 秒 `registry.snapshot()` 打明细表（哪些请求、各自 elapsed、卡在哪个 stage），排空结束取消该任务。这正好覆盖真正卡住的第一阶段。

`GracefulShutdownManager.wait_for_drain` 同步升级：注入一个 `detail_fn: Callable[[], tuple[RequestRecord, ...]]`（默认 `registry.snapshot`），把 `drain_waiting`/`timeout` 日志从「只计数」升级为「带明细」，惠及 k8s/lifespan 路径。

### 组件 2 前端：WebUI 实时面板（Phase 4）

新增「Active Requests」页，挂 dashboard 现有 admin 鉴权，`EventSource` 直连 `/health/in-flight/stream`，实时表格 + elapsed 走秒（前端 tick）。

## 测试

- **登记表**：`register/update/deregister/snapshot` 与 pub/sub 单测；`update` 产出新冻结实例（原实例不变）；变异测试。中间件集成：进出登记、`ContextVar` 回填、gauge 与 `len(registry)` 一致
- **组件 4 回归**（每条独立、可证伪）：
  - 注入 `is_shutting_down=lambda: True`，断言 `SpendCounterReseed.from_db` 未触碰 prisma（mock client 的 `find_unique` 断言 not called）且返回 None、无 `.exception` 日志
  - watchdog：`is_shutting_down` 为真时 engine 死亡不触发重连（断言 reconnect 协程/`recreate_prisma_client` not called）
  - redis：关停 + 注入 `ConnectionError`，断言降级为单行 debug、无 error、不 re-raise
- **组件 0/3**：`DrainingServer.handle_exit` 早置 `is_shutting_down()`；注入假 registry + 假排空，断言播报任务在排空期确实打明细、结束即取消
- **端点**：`/health/in-flight` 在模拟在途 record 下返回该 record；SSE 端点在 register/update/deregister 时推出对应事件
- **可选 E2E**（[tests/e2e/](../../../tests/e2e/) 约定）：真 `Ctrl+C` + 一条长流式请求，验证关停日志打出在途明细与 elapsed，且无 `ClientNotConnectedError`/redis traceback

## 分期

1. **Phase 1 — 关停正确性（先修 bug）**：组件 0（`DrainingServer` + 早停标志 + `proxy_cli` 单进程接线）+ 组件 4（watchdog 跳重连 / reseed 短路 / redis 降级）+ 回归测试。**独立可交付，直接消掉 thrash 与刷屏**
2. **Phase 2 — 在途登记表 + 可观测后端**：组件 1 + 组件 2 端点/LOG
3. **Phase 3 — 关停播报明细化**：组件 3（复用 Phase 1 的 Server 加播报任务 + `wait_for_drain` 明细化）
4. **Phase 4 — WebUI 实时面板**：组件 2 前端

依赖：Phase 1 建 `DrainingServer` 缝（仅早停标志 + 可保留计数级排空日志）；Phase 3 在同一 Server 上加明细播报，需要 Phase 2 的登记表。顺序天然。

## 待评审重点

- 组件 4 里 watchdog「关停中视为预期死亡」是复用 `_expected_engine_deaths` 登记 PID，还是在 `_on_engine_death_from_thread` 顶部直接 `if is_shutting_down(): cleanup + return`——倾向后者更直接，评审定夺
- `DrainingServer` 只覆盖 `workers<=1`：是否需要为多 worker 留一条 lifespan 兜底之外的路径，还是明确记为 backlog
- SSE 端点鉴权与 admin UI 鉴权的具体对齐点
