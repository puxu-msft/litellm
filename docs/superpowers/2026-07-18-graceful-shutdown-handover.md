# Graceful Shutdown / In-Flight Observability — 交接文档

日期：2026-07-18
分支：`ghc`（私有 fork，聚焦 github_copilot provider，用户本地单机 standalone 运行）
交接原因：换人续做；本文档让接手者无缝续上，无需重建心智模型

---

## 0. TL;DR（30 秒读完）

用户报了一个关停 bug：本地 `Ctrl+C` 关停 litellm proxy 时**静默 hang 约 3 分钟**，期间日志刷 `triggering reconnect` / `Attempting Prisma DB reconnect` / `SpendCounterReseed ... ClientNotConnectedError` / `redis async_increment Connection closed`。

做法走 SDD：冻结 spec（3 轮对抗评审）→ 拆 Phase 1a（关停正确性，纯 guard/wiring）+ Phase 1b（记账排空的完整生命周期）。

**当前状态**：
- **Phase 1a：完整落地 + 加固 + E2E 验证**。用户的原始问题（单进程 direct 模式）已修复并端到端验证
- **Phase 1b：组件 1-4/5 完成**。supervisor 并发状态机与 `LoggingWorker.quiesce` 共享 SDK 集成均已 TDD 锁定；剩组件 5（记账点接入 + lifespan 9 步 + E2E）
- **Phase 1b 已从「纸面 plan」转为「TDD 增量实现」**（见 §5 决策）——`docs/.../plans/...-phase1b.md` 仅作**设计参考**，不要照它逐行执行（它与真实实现有已知漂移，见 §6）

**先跑一遍确认基线绿**：
```bash
.venv/bin/python -m pytest tests/test_litellm/proxy/shutdown/ -q
```

---

## 1. 冻结产物（先读这些）

| 文件 | 作用 | 注意 |
|---|---|---|
| `docs/superpowers/specs/2026-07-14-in-flight-observability-graceful-shutdown-design.md` | **冻结 spec**，3 轮对抗评审定稿 | 权威意图来源。含 §A-G 设计、9 步 quiesce、三变体 outcome、uvicorn force_exit 不变量 |
| `docs/superpowers/plans/2026-07-14-graceful-shutdown-phase1a.md` | Phase 1a TDD 计划 | 大体落地，但有**三处有意偏离**（见 §4），计划文本**未同步**到偏离 |
| `docs/superpowers/plans/2026-07-14-graceful-shutdown-phase1b.md` | Phase 1b TDD 计划 | **仅设计参考**。经四轮评审仍有 blocker + 与真实实现漂移；已改为 TDD 增量（§5）。用它理解设计意图，别照它逐行执行 |

spec 有一段「三件事」拆分：① 在途请求明细可观测（LOG+WebUI）② 优雅关停播报在途请求 ③ 关停时序 bug。**注意**：目前只做了 ③（关停正确性）的 Phase 1a/1b；① ② 属 Phase 2/3/4，spec 里有设计但**尚未实现**。用户当前优先级是把 ③ 做扎实。

---

## 2. Phase 1a：已完成（关停正确性）

8 个 Task 全部落地、接线、E2E 验证，全在 HEAD。核心链路：

**`DrainingServer`（自建 uvicorn.Server 子类）在信号时刻就置起 `GracefulShutdownManager.is_shutting_down()`**，于是所有 guard 在 uvicorn 连接排空阶段就生效。

源码（`litellm/proxy/shutdown/`）：
- `graceful_shutdown_manager.py` — `is_shutting_down()` / `start_shutdown()`(冻结绝对 deadline) / `deadline_remaining()` / `is_force_exit()` / `request_force_exit()` / `wait_for_drain()`(用剩余 deadline)
- `draining_server.py` — `DrainingServer` 覆写 `handle_exit`(信号→start_shutdown；二次 SIGINT→request_force_exit) + async `shutdown`(幂等 + 对齐 uvicorn timeout 到 deadline_remaining)
- `uvicorn_runner.py` — `run_uvicorn_with_draining_server` 复刻 uvicorn 三分支(reload/workers/direct) + KeyboardInterrupt 包裹 + STARTUP_FAILURE(3) 退出
- `proxy_cli.py:~1270` — 调用 runner（已接线，`git grep run_uvicorn_with_draining_server` 确认）

关停 guard（分散在真实文件）：
- `litellm/proxy/utils.py` — watchdog：4 个 engine-death 探测器统一到 `_handle_engine_death` → `_reconnect_after_engine_death`（**延迟到任务时刻读 flag**，防 Ctrl+C 进程组竞态）；3 个补充 guard（`_attempt_reconnect_inside_lock` 拿锁后 / `_start_engine_watcher` / `_handle_writer_engine_replaced`）；`PrismaClient.__init__` 注入 `is_shutting_down`，透传给 3 个 `PrismaWrapper` 构造点
- `litellm/proxy/db/prisma_client.py` — `PrismaWrapper` 的 IAM refresh 三处 guard（`_token_refresh_loop` 用 **break** / `_safe_refresh_token` 拿锁后 / `_recreate_prisma_client_locked` kill 前）+ `is_shutting_down` DI
- `litellm/proxy/db/spend_counter_reseed.py` — `from_db` / `window_from_spend_logs` 关停短路返回 None
- `litellm/proxy/proxy_server.py` — `_increment_spend_counter_cache` 关停短路**返回 None**（不是 dataclass，见 §4 回归教训）；lifespan 关停顺序：`wait_for_drain → stop IAM refresh → stop watchdog → close aiohttp → proxy_shutdown_event`

E2E：`tests/e2e/shutdown/`（marker `spawned_proxy_e2e`，自拉起子进程发真信号）
```bash
.venv/bin/python -m pytest tests/e2e/shutdown/test_graceful_shutdown_e2e.py -p no:cacheprovider -q
```

单元测试（`tests/test_litellm/proxy/shutdown/` + reseed/spend_counters/budget_reservation/prisma_client/lifecycle 各文件的 shutdown 测试）——约 184 绿。

---

## 3. Phase 1b：supervisor + LoggingWorker 已完成（组件 1-4/5）

`litellm/litellm_core_utils/managed_task_set.py`：`ManagedTaskSet` 标准 asyncio task 注册表，供 `LoggingWorker` 与 supervisor 组合复用；含 `add` + done-callback 自移除 + `is_empty` + `cancel_all_and_count_failures`。5 测试。

`litellm/proxy/shutdown/managed_task_supervisor.py`：`ManagedTaskSupervisor` + `AccountingLease` + `AccountingScope` + `current_accounting_scope` ContextVar + `Drained|DeadlineExceeded|ForcedExit`，实现状态机 + `drain`。14 测试。

**四轮评审萃取、已由测试钉死的不变量（改动它们前务必理解）**：
1. `_root_admission_open` 与 `_hard_shutdown` **两个独立状态**
2. `close_root_admission()` 只关前者；`AccountingLease.is_valid()` = `not settled and not hard_shutdown`（**不看 root_admission**）→ 关 admission 后既有 lease 仍有效、仍能派生 child
3. root lease 在 **root 协程完成时**（`_run_root` 的 finally）才 settle，不是首次 spawn 时 → 一个 root 能顺序派生多个 sibling child（`_batch_database_updates` + `update_cache`）
4. `drain()` fixed-point：`task 集空 AND admissions_in_progress==0`，await 一 tick 后重检确认稳定；deadline/force-exit → **先 `begin_hard_shutdown()`**（封住走私 child）再 cancel+settle → 三变体 outcome

```bash
.venv/bin/python -m pytest tests/test_litellm/litellm_core_utils/test_managed_task_set.py tests/test_litellm/proxy/shutdown/test_managed_task_supervisor.py tests/test_litellm/litellm_core_utils/test_logging_worker.py -q -W error::RuntimeWarning
```

---

## 4. Phase 1a 的三处有意偏离 + 一个回归教训（重要）

计划文本**未同步**这些，接手后需同步（§7 待办）：

1. **Task 4 watchdog**：没按计划的单方法 `_handle_engine_stopped` 同步检查，而**复用树里已有的**双方法 `_handle_engine_death` + `_reconnect_after_engine_death`（延迟读 flag，防 Ctrl+C 进程组 SIGINT 竞态——比计划更优，有 `539cac4d2a` 回归测试）。这套 watchdog 代码原是被 `git add -A` 从别的会话 sweep 进来的 WIP，经核对是本特性工作、复用是对的
2. **Task 6b redis**：没删 `RedisCache.async_increment` 的底层 error log（避免 SDK 级行为变更），而在 `_increment_spend_counter_cache` **顶部关停短路**。更 scoped
3. **Task 7 lifespan 顺序**：作为过渡态 defense-in-depth（正确性已由 Task 4/5 guard 覆盖），未写 fragile 全 lifespan 顺序测试。1b 组件 5 会整块重写这段
4. **回归教训（务必记住）**：`_increment_spend_counter_cache` 一度返回 `AccountingSkippedDuringShutdown` dataclass，但**漏了一个调用方** `budget_reservation._reserve_counter` 会 `float(返回值)` → 关停时 `float(dataclass)` TypeError → 反而重刷屏。已改回返回 **None**（匹配既有 None-means-skip 契约）+ 移除了 unused `AccountingSkippedDuringShutdown`。**教训：改函数返回类型前，全仓 grep 所有调用方，别只看眼前几个**

---

## 5. 为什么 Phase 1b 从纸面 plan 转 TDD 增量（决策记录）

1b 计划经**四轮评审**，每轮都在并发记账状态机里挖出真 blocker（lease/scope 生命周期、admission gate 时序、与真实 LoggingWorker 调度拓扑漂移、假设了一个不存在的 Phase 1a 测试形状等）。评审一致背书**架构与分期正确**，但纸面 diff 反复在两处失败：① 细粒度并发时序正确性，② 与真实实现的漂移。这两类恰是 TDD 边实现边测最能逼出的。

**用户决定：1b 走 TDD 增量，计划降级为设计指南。** 评审已把正确设计萃取出来（§3 的不变量 + §6 的组件 4 设计），照这些护栏 TDD 即可。

---

## 6. 组件 4（LoggingWorker.quiesce，已实现）

**背景**：记账不是裸 `asyncio.create_task`，而是先进 `GLOBAL_LOGGING_WORKER` 队列（`litellm/litellm_core_utils/logging_worker.py`，已读清）。所以记账 lease 必须绑到**最早的 logging enqueue 边界**并随 queue item 走，`LoggingWorker` 的关停必须成为记账 drain 的一环。

**硬约束**：`LoggingWorker` 是共享 SDK 基础设施（SDK/router/proxy 都用）——**不能 import proxy 类型**，**token=None 纯 SDK 路径必须字节不变**（回归测试守）。

实现按 4a / 4b 两个 TDD 切片完成：

**切片 4a（基础，先做）**：
- 先做纯重构子步：`LoggingTask` 从 `TypedDict` 改 `frozen dataclass(slots=True)`，改掉全部 `task["coroutine"]`/`task["context"]` 访问为属性访问，跑既有 LoggingWorker 测试证明无行为变化；再做 token 行为子步，避免把机械迁移与生命周期改动混在一个红灯里
- 定义中立 `CompletionToken` Protocol（`settle() -> None`，放 core-utils 或 `logging_worker.py` 内，**不依赖 proxy**）——proxy 的 `AccountingLease.settle()` 已结构满足。冻结 spec §B 早期文本曾写 `settle(outcome)`，这里裁决为无参 `settle()`：token 只表达「该 queue item 已终结」，三变体 outcome 由 worker/supervisor 的 drain 结果在高层表达
- `LoggingTask` 从 `TypedDict` 改 `frozen dataclass(slots=True)`，加 `token: CompletionToken | None = None`；改掉全部 `task["coroutine"]`/`task["context"]` 访问（`_process_log_task` L91、`_process_single_task` L304、`clear_queue` L389、`_flush_on_exit` L501）为 `task.coroutine`/`task.context`
- `enqueue(coroutine, *, token=None)` + `ensure_initialized_and_enqueue(coroutine, *, token=None)`；`token` 存进 `LoggingTask`
- **唯一所有权规则**：token 随 `LoggingTask` 走，只在 callback 真正结束（成功、异常、超时、取消）或该 task 被确定丢弃时 settle；转交到 retry/aggressive-clear helper 时不得提前 settle。所有终结路径都必须先 `close()` 未执行的 coroutine，再 settle 非空 token；`settle()` 幂等但每条路径仍应只有一个逻辑 owner
- callback 执行终结路径：`_process_log_task`、`clear_queue`、`_process_single_task`、`_flush_on_exit` 的每个已取出 item，均在各自 finally settle；`clear_queue` 必须在清空局部 `task` 引用前 settle
- drop/rebind 终结路径：`enqueue()` 发现 queue 未初始化、`_schedule_delayed_enqueue_retry()` 无 running loop、`_retry_enqueue_task()` 醒来后 queue 已不存在、`_ensure_queue()` 换 loop 丢旧 queue、`_flush_on_exit()` 达到时间/迭代上限后的全部剩余 item。loop rebind 必须保存旧 queue，再逐项 `get_nowait()`、`task_done()`、close+settle，不能直接丢 queue 引用
- queue-full aggressive-clear 还有一个与 4b 正确性直接相关的预存缺陷：`new_task` 从未 put 入 queue，却与 extracted queue items 一样在 `_process_single_task` 调 `task_done()`，会让 unfinished counter 少 1、使 `flush()`/`quiesce()` 提前返回。4a 必须让处理函数显式区分 queue-owned item 与 direct `new_task`，只有前者调用 `task_done()`
- retry/aggressive-clear helper task 必须进强引用 task set并在完成时自移除；否则 4b 无法按契约 cancel+await helper，helper 也可能被 GC 或在 quiesce 判空后重新入队
- 回归测试：`token=None` 时 enqueue/flush/stop 行为与改动前完全一致；`token` 提供时处理后被 settle、各 drop 路径也 settle
- **注意评审 major**：测试若要人为阻止 dequeue，用 `LoggingWorker(concurrency=1)` 并在 start 后取唯一 semaphore permit（默认 concurrency=100，只 acquire 一次挡不住）

**切片 4b（quiesce）**：
- 新增 `quiesce(*, deadline_remaining, is_force_exit, admission_policy, downstream_is_quiescent) -> LoggingDrainOutcome`（**不复用** `flush`/`stop`）：关 proxy root logging admission；正常阶段让已登记 item 执行；deadline 后**不再执行 queued coroutine**，逐项 `close()` + settle token + `task_done()`；cancel worker processing + retry + aggressive-clear task 并 await settlement。`is_force_exit` 是三变体 outcome 所必需的独立输入，不能从 deadline 推断
- `_worker_loop` 的 `except CancelledError: await clear_queue()`（L130-133）加 **quiesced 状态**：quiesce 态下取消只 drop+settle，**禁止 `clear_queue()` 跑业务 callback**（评审 blocker：否则 deadline 后仍执行 callback 撞 teardown）
- 新增 `stop_after_quiesce()` 供 Task 10 显式停 worker loop
- 统一 retry admission：`_retry_enqueue_task`（L236）现在直接 `put_nowait` 绕过 admission——改为统一入口检查 admission/quiesced（评审 blocker）
- fixed-point 必须**联合** LoggingWorker + supervisor（不只查任一方）：worker 不能 import proxy supervisor，故 `quiesce` 接收中立的 downstream-quiescent 观察回调；只有 `queue unfinished == 0`、processing/retry/aggressive-clear helper 均空、supervisor task/admission 均空，并在 `await sleep(0)` 后复检仍稳定，才可返回 `Drained`。单纯「先 `queue.join()` 返回，再 `supervisor.drain()`」存在 producer 在两次检查之间重新 enqueue/spawn 的竞态，不是可接受实现
- `LoggingDrainOutcome` 与 supervisor 一致的三变体
- 普通 `flush()` 语义保持不变（全仓 `flush()` 仅 3 处**测试**调用，无生产调用方，安全）

---

## 7. 组件 5 设计（记账点接入 + lifespan 9 步）+ 剩余待办

**组件 5**（评审已识别的关键点，务必覆盖）：
- 在最早 logging enqueue 边界 acquire lease 并 `token=` 透传（`utils.py:1071-1091/1738-1767` 的 `_client_async_logging_helper`）
- 脱离父 coroutine 的 detached 记账 task 经 `supervisor.spawn_root`/`spawn_child` 创建：success/failure logging、`_ProxyDBLogger.update_cache`、`DBSpendUpdateWriter._batch_database_updates`、pass-through logging；awaited 子调用不另取
- **失败记账路径**（评审 blocker）：proxy 失败 spend 走 `ProxyLogging.post_call_failure_hook → _ProxyDBLogger.async_post_call_failure_hook → update_database → _batch_database_updates`（**不**走 `async_log_failure_event`）——也要纳入 scope 追踪
- **child 分类**：`update_cache`/`_batch_database_updates` 属 accounting（必 drain）；`budget_alerts`/`async_set_cache_pipeline`/`failed_tracking_alert`/service hooks 属 telemetry（deadline 可取消）
- **provider 注册/撤销**（评审 blocker）：lifespan startup 注册 root-scope provider、shutdown quiesce 后撤销；provider 恒为 None 会让 1b 主体功能不启用——务必真正接线，别只写在架构文字里
- **root telemetry admission gate**（评审 blocker）：`spawn_telemetry` 在 root admission 关闭/hard shutdown 后拒绝，drain 后不得新建 telemetry 撞 teardown
- **单例 reset**（评审 major）：supervisor/LoggingWorker 是一次性终态单例；若要支持同进程二次 lifespan（测试/嵌入），需 reset_for_startup 或 lifespan 起新实例
- **Task 10 lifespan 9 步 quiesce**：替换 Phase 1a 现有的 lifespan 关停块（`proxy_server.py:~1072-1108`，当前顺序见 §2）为完整 9 步（停接入→排空 transport→停 IAM/watchdog→关 root admission 但允许持 scope child→`LoggingWorker.quiesce` flush→supervisor fixed-point drain→deadline 联合取消→写 shutdown_dropped→stop logging worker→关 aiohttp/prisma/redis）。`drain(is_force_exit=GracefulShutdownManager.is_force_exit)` + `wait_for_drain` 只数 transport lease
- **uvicorn force_exit 不变量**（已裁决，写进 spec）：uvicorn 在二次 SIGINT force_exit 时**有意跳过 `lifespan.shutdown()`**（`server.py:293` `if not self.force_exit`），所以整套 lifespan quiesce（含 ForcedExit 分支）在**真实二次 SIGINT 上可能不运行**——这是正确语义（强退=别排空）。ForcedExit 变体主要由**单测**用测试替身覆盖；E2E 二次 SIGINT 只断言「快速退出」，不断言 quiesce 日志

**延后的 Phase 1a major（评审发现、非用户主用模式，优先级低）**：
- Major 2：redis 关停刷屏完整性——spend 主路径已修，但 reseed warm(`spend_counter_reseed.py:315`)/limiter rollback(`parallel_request_limiter_v3.py:993`)/dual_cache 等其它路径关停期仍可能碰 redis 底层 error。1b 联合 quiesce 是根治；或给这些 proxy-owned 边界逐个加 guard，或把 1a 承诺收窄为「spend-counter 路径」
- Major 3：uvloop `term_signal=None` 窄竞态——延迟读 flag 降低但没完全消除 Ctrl+C engine resurrection；在构造新 engine 前加最后一道 check
- Major 4：reload/workers 二次 SIGINT——uvicorn supervisor 不转发第二次信号给 child，故「二次 SIGINT 强退」目前**只 direct 单进程成立**。用户本地单机 direct，影响小
- Major 7：E2E 广度——现只 direct SIGTERM + direct 二次 SIGINT；reload/workers/limit 入口 + 真 DB/redis 变体（skip-if-absent）未覆盖

**文档同步待办**：
- 同步 Phase 1a 计划文本到 §4 的三处偏离 + accounting_outcome 已移除
- 1b 计划已作废为「设计参考」，可在其头部标注「已转 TDD 增量，见交接文档」

---

## 8. 关键操作纪律（务必遵守）

- **共享工作树 + 并发会话**：这个 worktree 被另一个会话共享（在做 github_copilot reasoning-fidelity）。**只用精确 pathspec 提交自己的文件**（`git add -- <exact paths>` + `git commit -- <exact paths>`），**绝不 `git add -A`/`git add .`/`git commit -am`**。曾发生过 `git add -A` sweep 别人 WIP、以及误 amend 并发会话提交的事故（已恢复）。改文件前先重读（可能被 peer 动过）。参考 skill `git-preference:coordinating-a-shared-git-worktree`
- **提交前**：CLAUDE.md 要求 `make pre-commit`，但它 lint 整个 worktree litellm/（会碰 peer 的未提交文件）。实践中改为对自己的文件做 scoped `ruff format` + `ruff check`，再 pathspec 提交
- **`# mutable-ok`**：LIT001/LIT002 禁止 seed 空容器再 mutate。`ManagedTaskSet._tasks` 用了 `# mutable-ok`（活的 task 注册表固有可变）——这是合理场景。别滥用
- **测试真实性**：CLAUDE.md 要求测试能在 mutation 下失败（>90% kill）。别写只断言 `result is None` 而不断言「DB/redis 方法未被调」的空测试（评审抓过这类）。参考已落地的 watchdog/reseed/spend 测试的反向断言写法
- **Python 行宽 120**

---

## 9. Kick-off 提示词（复制给下一个会话）

```
你接手 litellm 私有 fork（ghc 分支）的「graceful shutdown」特性续做。先读交接文档
docs/superpowers/2026-07-18-graceful-shutdown-handover.md（含全部上下文、已完成、剩余、
关键不变量、操作纪律），再读冻结 spec docs/superpowers/specs/2026-07-14-in-flight-observability-graceful-shutdown-design.md。

背景：这是一个共享工作树、有并发会话——严格只用精确 pathspec 提交自己的文件，绝不 git add -A。

现状：Phase 1a 完整落地+加固+E2E 验证（用户的关停 hang/刷屏 bug 已修）；Phase 1b 的 supervisor 核心
（ManagedTaskSet + 状态机 + drain，litellm/proxy/shutdown/managed_task_*.py）已 TDD 完成。

先跑 `.venv/bin/python -m pytest tests/test_litellm/proxy/shutdown/ -q` 确认基线绿。

你的任务：继续 Phase 1b 的 TDD 增量实现，从组件 4（LoggingWorker.quiesce）开始，严格按交接文档 §6
的切片 4a→4b 设计走（中立 CompletionToken、LoggingTask 转 frozen+token、token=None 纯 SDK 路径字节不变、
quiesce 不复用 flush/stop、_worker_loop quiesced 状态禁 clear_queue 跑 callback）。这是共享 SDK
（litellm/litellm_core_utils/logging_worker.py，一坏全坏）的精细集成——先读清它、TDD 每一步、注意评审
点名的坑（semaphore 测试要 concurrency=1、retry 绕过 admission、settle 每条 drop/rebind 路径）。

然后组件 5（记账点接入 + provider 注册/撤销 + Task 10 lifespan 9 步 + E2E，见 §7）。

1b 计划文档（plans/...-phase1b.md）仅作设计参考，别照它逐行执行（已知与真实实现漂移）。
遇到并发正确性抉择，对照交接文档 §3 的四条不变量。
```

---

## 10. 验证清单（接手后自检）

```bash
# 基线：全部 shutdown 单测绿
.venv/bin/python -m pytest tests/test_litellm/proxy/shutdown/ -q
# 关停相关的边界测试绿
.venv/bin/python -m pytest tests/test_litellm/proxy/db/test_spend_counter_reseed.py tests/test_litellm/proxy/db/test_prisma_client.py tests/test_litellm/proxy/proxy_server/test_spend_counters.py tests/test_litellm/proxy/spend_tracking/test_budget_reservation_redis_failure.py -q
# E2E：真信号（~30s）
.venv/bin/python -m pytest tests/e2e/shutdown/test_graceful_shutdown_e2e.py -p no:cacheprovider -q
# DrainingServer 真被接线（应有输出）
git grep -n run_uvicorn_with_draining_server litellm/proxy/proxy_cli.py
```
