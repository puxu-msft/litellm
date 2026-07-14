# Graceful Shutdown — Phase 1b 实施计划：进程级记账 drain 正确性

- Spec: `docs/superpowers/specs/2026-07-14-in-flight-observability-graceful-shutdown-design.md`（已冻结，3 轮 adversarial review）
- 前置计划: `docs/superpowers/plans/2026-07-14-graceful-shutdown-phase1a.md`（review-cleared，**假定已落地**；本计划所有对 `GracefulShutdownManager`/`DrainingServer`/`AccountingSkippedDuringShutdown` 的引用均以该计划文本中定义的签名为准，而非当前仓库里 Phase 1a 落地前的旧代码）
- 状态: Draft，待 subagent review

## Goal

把 Phase 1a 遗留的"关停短路"（记账 DB/redis 触点在关停时提前返回 `AccountingSkippedDuringShutdown`）升级为一条完整的**进程级记账 drain**：

1. 先让 `LoggingWorker` 把队列里已产出的记账工作跑完（`quiesce`，而非 `flush`/`stop`）；
2. 再让一个新的 `ManagedTaskSupervisor` 把 quiesce 过程中派生出的、脱离主队列独立生命周期的记账/遥测子任务，用 fixed-point 方式排空；
3. deadline 到期时，对尚未完成的记账工作做统一 cancel + gather，不再各自为政；
4. 所有关停路径下未完成的记账动作，返回值统一收敛到 `AccountingCompleted | AccountingSkippedDuringShutdown | AccountingFailed` 这个 tagged union，而不是各写各的 sentinel。

## 治理边界（裁决，非待议）

Per-request 登记表（registry）、记录级 transport/accounting lease 的**归属**（ownership，即"这条 lease 属于哪个 request_id"）、ACCOUNTING 阶段的可观测性（把 accounting 阶段计入 in-flight 展示）——这三项是 **Phase 2**，本计划不做。

Phase 1b 严格限定在**进程级**正确性：`AccountingLease`/`ManagedTaskSupervisor` 只回答"进程里还有多少个未完工的记账工作单元"，drain 等这个计数归零；不回答"这个工作单元属于哪次 HTTP 请求"。`wait_for_drain` 的计数口径保持/确认为纯 transport lease 数（已核实：`GracefulShutdownManager.wait_for_drain` 当前就是靠 `get_in_flight_requests()`，纯 transport 语义，Phase 1b 不需要改这个口径，只需要在新增的 9 步 quiesce 协议里把它放在正确的顺序位置）。

**读码复核结论：这条边界划分没有结构性缺陷，不需要重新切分 Phase 1b/Phase 2**。下面"设计说明"一节记录了两个需要如实汇报、但不影响这条边界的发现（记账创建点数量与 spec 原文简化描述不完全一致；核心 SDK 与 proxy 层的引用方向约束）。

## Architecture（对已接受合同的落地细化，不新增跨模块协议）

### 记账创建点的两条路径（沿用 Phase 1a 计划正文已使用的 "Path A / Path B" 措辞）

Phase 1a 计划第 2250-2252 行的"承诺的后续"一节已经点名"六个 Path A / 七个 Path B 记账任务创建点"是 1b 的范围，本计划沿用这个措辞而不是另造词汇。逐个读码后的精确清单（细化、而非否定 1a 的概括计数，两者的差异在"设计说明"一节如实记录）：

**Path A —— 经由 `GLOBAL_LOGGING_WORKER.ensure_initialized_and_enqueue` 的队列型创建点（6 个，均需要 `LoggingTask.token` 传递 lease）**：

| # | 文件:行 | 场景 |
|---|---|---|
| A1 | `litellm/utils.py:1089`（`_client_async_logging_helper`） | 非流式 success（spec §B 原文唯一显式点名的一处） |
| A2 | `litellm/caching/caching_handler.py:646`（`_async_log_cache_hit_on_callbacks`） | cache-hit success |
| A3 | `litellm/litellm_core_utils/realtime_streaming.py:322`（`log_messages`） | realtime |
| A4 | `litellm/proxy/pass_through_endpoints/streaming_handler.py:99` | pass-through 流式 |
| A5 | `litellm/proxy/pass_through_endpoints/pass_through_endpoints.py:1349` | pass-through HTTP success |
| A6 | `litellm/proxy/pass_through_endpoints/pass_through_endpoints.py:2133` | pass-through WebSocket success |

**Path B —— 裸 `asyncio.create_task`、不经过 `LoggingWorker` 队列的创建点**，按记账语义分两类：

*Path B / accounting（drain 必须等待，3 个）*：

| # | 文件:行 | 函数 |
|---|---|---|
| B1 | `litellm/litellm_core_utils/streaming_handler.py:2053` | `dispatch_success_handlers`（标准流式 success，核心 SDK 代码——跨层，见下） |
| B2 | `litellm/proxy/hooks/proxy_track_cost_callback.py:248`（`_PROXY_track_cost_callback`） | `update_cache(...)` |
| B3 | `litellm/proxy/db/db_spend_update_writer.py:~189`（`DBSpendUpdateWriter.update_database`） | `_batch_database_updates(...)` |

*Path B / telemetry（deadline 到期直接 cancel，不阻塞 drain，共 18 个，1 个嵌套 + 17 个独立站点）*：

| # | 文件:行 | 函数 |
|---|---|---|
| T1 | `litellm/proxy/hooks/proxy_track_cost_callback.py:305` | `failed_tracking_alert(...)` |
| T2 | `litellm/proxy/proxy_server.py:2760`（`update_cache` 内 `_update_key_cache`） | `budget_alerts(type="projected_limit_exceeded")` |
| T3 | `litellm/proxy/proxy_server.py:2984`（`update_cache` 尾部） | `async_set_cache_pipeline(...)` |
| T4-T12 | `litellm/proxy/auth/auth_checks.py:3476/3574/3665/3697/3859/3973/4017/4067/4205` | 9 处鉴权阶段 `budget_alerts(...)`（无父记账 scope，root-level） |
| T13 | `litellm/proxy/auth/auth_checks.py:1568` | SSO 场景后台回写 `sso_user_id`（`UserRepository(...).table.update(...)`） |
| T14-T15 | `litellm/proxy/auth/user_api_key_auth.py:540/1982` | 2 处鉴权阶段 `budget_alerts(...)`（同上） |
| T16-T17 | `litellm/proxy/auth/user_api_key_auth.py:1597/1996` | 2 处鉴权成功后回填 key 缓存 `_cache_key_object(...)`（cache-warm，best-effort） |
| T18 | `litellm/proxy/auth/user_api_key_auth.py:2612` | `user_api_key_service_logger_obj.async_service_success_hook(...)`（spec §B 原文点名的"service logging hooks"这一类） |

（**设计说明（Task 9 落笔时发现，如实记录）**：本节最初只列了 14 个 T 站点，落笔 Task 9 时用 `grep -n "asyncio.create_task" litellm/proxy/auth/auth_checks.py litellm/proxy/auth/user_api_key_auth.py` 全量核对后发现，`auth_checks.py:1568` 的 SSO `sso_user_id` 回写、`user_api_key_auth.py:1597/1996` 的 `_cache_key_object` 缓存回填、`user_api_key_auth.py:2612` 的 `async_service_success_hook` 这 4 处此前被漏收——它们和已收录的 `budget_alerts` 站点结构完全相同（都是鉴权/缓存路径上脱离请求的裸 `asyncio.create_task`，proxy 层代码，import `GLOBAL_MANAGED_TASK_SUPERVISOR` 不存在核心 SDK 层级污染问题），其中 `async_service_success_hook` 更是 spec §B 原文明确点名的"service logging hooks 属 telemetry"这一类的具体实例。这不是可以"暂不处理"的边缘情况——遗漏它们等于让这 4 个 fire-and-forget task 继续在关停时被 GC 弱引用悄悄丢弃，和本 Phase 要修的问题是同一类 bug，只是发生在鉴权路径而非记账路径。故本次已把 T 列表从 14 扩到 18，一并纳入 Task 9。）

`litellm/litellm_core_utils/streaming_handler.py:2011`（`async_cache_streaming_response`，缓存写回，非记账）与 `:2080`（`async_failure_handler`，通用 CustomLogger 失败回调，已核实 `_ProxyDBLogger` 未实现 `async_log_failure_event`，与 proxy 记账链路无关）**维持现状、不纳入任何管理集合**——这不是遗漏，是复用 Phase 1a 计划"未采纳方案"一节里对 `redis_cache.py` 的 `async_service_failure_hook` 已经做出的同款裁决：核心 SDK 里纯遥测、无记账语义的裸 `create_task`，纳入任何管理原语都会造成 SDK 反向依赖 proxy 类型的层级污染，收益（能在 deadline 时多 cancel 掉几个已经是"尽力而为"性质的任务）不值得这个代价。这条裁决只适用于**核心 SDK**代码；T13/T16-T18 都是 proxy 层代码，不适用同一豁免理由，必须收进管理集合。

### 核心 SDK / proxy 分层与 scope 传播机制

`litellm_core_utils/streaming_handler.py`（Path B1）和 5 个 Path A 站点里的 4 个（A1-A3 在核心 SDK；A4-A6 在 proxy 层）都是核心 SDK 代码，不能直接 `import` `ManagedTaskSupervisor`/`AccountingLease`（proxy-only 类型）。复用 spec 已经采纳的 `CompletionToken` 中立协议模式，新增一个同样中立的 `AccountingScope` 协议 + `ContextVar` + 两个中立辅助函数，全部放在 `litellm/litellm_core_utils/accounting_scope.py`：

- `current_accounting_scope: ContextVar[AccountingScope | None]`——不是"用 `ContextVar != None` 做 admission 授权"（spec §B 明确禁止的做法），而是"用它在同一条真实调度链路上传递一个已经被显式校验过的 scope **引用**"；真正的授权判断永远是对这个引用调用 `scope.is_valid()`/`scope.spawn(...)` 的显式方法调用，`ContextVar` 只负责让这个引用能免 import 地传到核心 SDK 代码手上。
- `register_root_scope_provider(provider)` / `acquire_root_scope()`——**可撤销**的 DI 注册钩子（major 9 修订：不是模块导入时一次性注册、永不撤销）。`GLOBAL_MANAGED_TASK_SUPERVISOR = ManagedTaskSupervisor()` 这个进程级单例仍然在 `managed_task_supervisor.py` 模块导入时创建（同构于本文件既有的 `GLOBAL_LOGGING_WORKER = LoggingWorker()` 写法），但 `register_root_scope_provider(GLOBAL_MANAGED_TASK_SUPERVISOR.acquire_root_lease)` 这个*注册调用*本身挪到 Task 10 的 lifespan 启动阶段才执行，并在 lifespan 关停阶段——`drain()` 完全跑完之后——显式调用 `register_root_scope_provider(None)` 撤销注册。这样同一个进程内"先跑一个 proxy 实例、完整关停、再跑纯 SDK 调用"（例如测试场景，或者未来允许在同进程里重启第二个 lifespan）时，`acquire_root_scope()` 不会继续持有一个指向已经关停 supervisor 的悬挂引用——供应商闭包本身仍然经由 `AccountingLease.is_valid()`（blocker 1/2 的 `_hard_shutdown`/root admission 状态）反映"proxy 运行时是否仍处于活跃状态"，但撤销注册这一步把"引用是否还挂着"和"挂着的引用是否还有效"两件事都做干净，而不是只依赖后者。纯 SDK 场景（从未 import 过 `litellm.proxy.*`，因而这个注册调用从未发生过）下这个 provider 永远是 `None`，所有相关函数退化为逐字节等价于今天的裸 `asyncio.create_task`——这就是"pure-SDK 行为必须逐字节不变"这条硬约束的落地方式。
- `spawn_detached(coro, *, name)`——核心 SDK 代码里替代裸 `asyncio.create_task` 的唯一入口：先读 ambient scope，读不到（或已失效）就尝试 `acquire_root_scope()` 拿一个新 root scope，两者都拿不到就回退到裸 `asyncio.create_task`（byte-for-byte 匹配现状）。
- `create_task_with_scope(coro, *, token)`——被 `LoggingWorker._process_log_task` 通过 `task.context.run(...)` 调用，在 `task.context` 内先绑定 `current_accounting_scope`（如果 `token` 同时满足 `AccountingScope` 的 `is_valid`/`spawn` 结构，用 `@runtime_checkable` Protocol 判断，不需要收窄 `LoggingTask.token` 的类型注解，那个字段维持 spec 原文写的中立 `CompletionToken | None`），再 `asyncio.create_task`，让 Path B 的嵌套创建点（如 `update_cache` 内部的裸 `create_task`）能在同一条 Context 链路上看到并使用这个 scope。

`AccountingLease`（proxy 层，`managed_task_supervisor.py`）同时实现 `CompletionToken`（`settle`）与 `AccountingScope`（`is_valid`/`spawn`）两个协议——"记账根单元"与"这个单元自己能否再派生 child"是同一个对象的两面，这也是 spec 里"允许持有效 scope 的已登记 accounting task 派生 child"的最小实现方式。

### `LoggingWorker.quiesce()` 与 `ManagedTaskSupervisor.drain()` 的组合顺序

不做跨两个组件的单一合并计数器。严格顺序组合，呼应 spec"先 flush 产出的工作，再排空 child，方向不可反"：`LoggingWorker.quiesce(deadline_remaining, admission_policy)` 先跑到底（或到 deadline），期间把所有经由 `token=`/`spawn_detached` 派生出的 Path B 子任务转交给 supervisor 的托管集合；随后 `ManagedTaskSupervisor.drain()` 用剩余的 `deadline_remaining` 对自己的 fixed point（`accounting_tasks == 0 && admissions_in_progress == 0`）做第二阶段。两者各自独立可测，顺序编排在 Task 10（lifespan 接线）里体现，不塞进任一组件内部。

## Tech Stack

纯标准库 `asyncio`/`contextvars`/`dataclasses`（`frozen=True, slots=True`）/`typing.Protocol`/`typing.Literal` + `match`/`assert_never`。不引入第三方依赖——这类"进程内任务生命周期跟踪"没有成熟的第三方包能比标准库 `asyncio.Task` + 一个 `set` 更简单可靠（`battle-tested-over-hand-rolled` 的反向应用：标准库本身就是 battle-tested 的实现，不需要额外包装）。

## Global Constraints（verbatim spec 摘录）

> `ManagedTaskSet`（shared by `LoggingWorker` and new supervisor; not merging their high-level drain semantics）

> 不让通用 `LoggingWorker` 依赖 proxy 类型……`LoggingTask` 从 mutable `TypedDict` 改为 `frozen dataclass(slots=True)`，新增可选中立字段 `token: CompletionToken | None`

> lease 绑到真实调度拓扑，不是替换表面的 `asyncio.create_task`……故 lease 在**最早的 logging enqueue 边界**同步 acquire，把 `inflight_id` + once-only lease token 存入 `LoggingTask` queue item（**注**：`inflight_id` 字段本身是 Phase 2 的 per-request 登记表字段，Phase 1b 的 `LoggingTask.token` 不携带 `inflight_id`，只携带 lease/token）

> admission 授权用不可伪造 scope，不靠 `ContextVar != None`

> `ManagedTaskSupervisor.spawn_telemetry`/`close_root_admission`/`async drain() -> DrainOutcome`，fixed-point `root_queue_unfinished == 0 && accounting_tasks == 0 && admissions_in_progress == 0`；B2/B3 两个 accounting 记账创建点直接复用 Task 2 的 `spawn_detached`（见 Task 8 "记录未采纳方案"），不新增 `ManagedTaskSupervisor.spawn_child` 这个专用入口

> 子任务分类表：`update_cache`/`_batch_database_updates` = accounting（必须 drain）；`budget_alerts`/`async_set_cache_pipeline`/`failed_tracking_alert`/service hooks = telemetry（deadline 可直接 cancel）

> 9 步 quiesce 协议（§C）

## File Structure

```
litellm/litellm_core_utils/
  managed_task_set.py          # 新增：ManagedTaskSet（LoggingWorker 与 supervisor 共用）
  accounting_scope.py          # 新增：CompletionToken/AccountingScope 协议 + ContextVar + spawn_detached/create_task_with_scope
  logging_worker.py            # 修改：LoggingTask -> frozen dataclass；quiesce()；token= 参数
  litellm_logging.py           # 不改动（dispatch_success_handlers/async_success_handler 签名不变）

litellm/proxy/shutdown/
  managed_task_supervisor.py   # 新增：AccountingLease、ManagedTaskSupervisor、DrainOutcome union
  accounting_outcome.py        # 修改（Phase 1a 产物）：扩展为 AccountingCompleted|AccountingSkippedDuringShutdown|AccountingFailed
  graceful_shutdown_manager.py # 不改动（Phase 1a 已提供 deadline_remaining/is_force_exit）

litellm/utils.py                                          # 修改：A1 站点接入 token=
litellm/caching/caching_handler.py                         # 修改：A2 站点接入 token=
litellm/litellm_core_utils/realtime_streaming.py            # 修改：A3 站点接入 token=
litellm/litellm_core_utils/streaming_handler.py             # 修改：B1 站点改用 spawn_detached
litellm/proxy/pass_through_endpoints/streaming_handler.py    # 修改：A4 站点接入 token=
litellm/proxy/pass_through_endpoints/pass_through_endpoints.py # 修改：A5/A6 站点接入 token=
litellm/proxy/hooks/proxy_track_cost_callback.py             # 修改：B2 站点接入 spawn_detached；T1 站点接入 spawn_telemetry
litellm/proxy/db/db_spend_update_writer.py                   # 修改：B3 站点接入 spawn_detached
litellm/proxy/proxy_server.py                                # 修改：T2/T3 站点接入 spawn_telemetry；lifespan 9 步 quiesce 接线
litellm/proxy/auth/auth_checks.py                            # 修改：T4-T13 接入 spawn_telemetry
litellm/proxy/auth/user_api_key_auth.py                      # 修改：T14-T18 接入 spawn_telemetry

tests/test_litellm/litellm_core_utils/
  test_managed_task_set.py       # 新增
  test_accounting_scope.py       # 新增
  test_logging_worker.py         # 扩展既有文件（quiesce + token + dataclass 迁移的回归测试）
tests/test_litellm/proxy/shutdown/
  test_managed_task_supervisor.py  # 新增
  test_accounting_outcome.py       # 扩展 Phase 1a 新增的文件（tagged union 扩展）
tests/test_litellm/proxy/hooks/test_proxy_track_cost_callback.py  # 扩展既有文件
tests/test_litellm/proxy/db/test_db_spend_update_writer.py        # 扩展既有文件
tests/test_litellm/proxy/proxy_server/test_spend_counters.py      # 扩展既有文件（先核实是否存在，见 Task 8）
tests/test_litellm/proxy/auth/test_auth_checks.py                 # 扩展既有文件
tests/test_litellm/proxy/auth/test_user_api_key_auth.py           # 先核实是否存在，见 Task 9
tests/test_litellm/caching/test_caching_handler.py                # 扩展既有文件
tests/test_litellm/litellm_core_utils/test_realtime_streaming.py  # 扩展既有文件
tests/test_litellm/litellm_core_utils/test_streaming_handler.py   # 扩展既有文件
tests/test_litellm/proxy/pass_through_endpoints/test_pass_through_endpoints.py       # 扩展既有文件
tests/test_litellm/proxy/pass_through_endpoints/test_streaming_handler_interrupt.py  # 扩展既有文件
tests/test_litellm/proxy/test_proxy_server.py                     # 扩展既有文件（lifespan 9 步接线）
tests/e2e/shutdown/test_graceful_shutdown_e2e.py                  # 扩展 Phase 1a 新增的 E2E 套件（记账 drain 场景）
```

---

## Task 1 — `ManagedTaskSet`

独立，无前置依赖。纯新增文件。

**Files**: `litellm/litellm_core_utils/managed_task_set.py`（新）, `tests/test_litellm/litellm_core_utils/test_managed_task_set.py`（新）

**设计说明**：这是一个**有状态的运行时资源管理器**（跟 `asyncio.Queue`/`asyncio.Semaphore`、以及 `LoggingWorker` 现有的 `self._running_tasks: set[asyncio.Task]` 同一类别），不是业务值对象，因此不用 `frozen dataclass`；内部持有的 `set` 会被 `add`/done-callback 修改，这是该类型存在的唯一理由，不违反项目"不用可变 list/dict 累积业务值"的约定（那条约定针对的是"用可变容器一步步拼出一个本该不可变的值"这种反模式，不针对天然状态性的运行时原语）。

**Interfaces**

```python
class ManagedTaskSet:
    def add(self, task: asyncio.Task[object]) -> None: ...
    def __len__(self) -> int: ...
    def is_empty(self) -> bool: ...
    def cancel_all(self) -> None: ...
    async def wait_settled(self) -> None: ...
    async def cancel_all_and_count_failures(self) -> tuple[int, int]: ...
```

`cancel_all_and_count_failures()` is a new addition (previously only `cancel_all()` existed) needed by Task 4/5's `DeadlineExceeded.cancellation_failed`/`LoggingDeadlineExceeded.cancellation_failed` fields (spec's three-variant `DrainOutcome`, see Task 4/5/10 below): the deadline path must not just cancel best-effort, it must report back how many tasks actually honored the cancellation cleanly vs. how many did not (suppressed it, returned normally anyway, or raised something else), so ops can tell a "cancel worked" shutdown from a "some task refused to die" one from the returned outcome alone, not just from logs.

**Steps**

1. 写失败测试（新文件）：

```python
"""ManagedTaskSet: strong-ref task set shared by LoggingWorker and
ManagedTaskSupervisor for drainable/cancellable detached-task tracking."""

import asyncio

import pytest

from litellm.litellm_core_utils.managed_task_set import ManagedTaskSet


class TestManagedTaskSet:
    def test_new_set_is_empty(self):
        s = ManagedTaskSet()
        assert s.is_empty()
        assert len(s) == 0

    @pytest.mark.asyncio
    async def test_add_tracks_task_until_it_completes(self):
        s = ManagedTaskSet()
        started = asyncio.Event()

        async def work():
            started.set()
            await asyncio.sleep(0.01)

        task = asyncio.create_task(work())
        s.add(task)
        assert len(s) == 1

        await started.wait()
        await task
        # done-callback must remove it without any explicit cleanup call
        await asyncio.sleep(0)  # let the done-callback run
        assert s.is_empty()

    @pytest.mark.asyncio
    async def test_cancel_all_cancels_every_tracked_task(self):
        s = ManagedTaskSet()
        started = asyncio.Event()

        async def work():
            started.set()
            await asyncio.sleep(10)

        task = asyncio.create_task(work())
        s.add(task)
        await started.wait()

        s.cancel_all()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_wait_settled_returns_once_all_tracked_tasks_finish(self):
        s = ManagedTaskSet()
        order: list[str] = []

        async def work(label, delay):
            await asyncio.sleep(delay)
            order.append(label)

        s.add(asyncio.create_task(work("a", 0.01)))
        s.add(asyncio.create_task(work("b", 0.02)))

        await s.wait_settled()
        assert set(order) == {"a", "b"}
        assert s.is_empty()

    @pytest.mark.asyncio
    async def test_wait_settled_also_awaits_tasks_added_while_waiting(self):
        """Fixed-point behavior: a task that spawns another tracked task
        before finishing must not let wait_settled() return early."""
        s = ManagedTaskSet()
        order: list[str] = []

        async def child():
            await asyncio.sleep(0.01)
            order.append("child")

        async def parent():
            await asyncio.sleep(0.005)
            s.add(asyncio.create_task(child()))
            order.append("parent")

        s.add(asyncio.create_task(parent()))
        await s.wait_settled()
        assert order == ["parent", "child"]
        assert s.is_empty()

    @pytest.mark.asyncio
    async def test_wait_settled_does_not_raise_on_task_exception(self):
        s = ManagedTaskSet()

        async def failing():
            raise ValueError("boom")

        s.add(asyncio.create_task(failing()))
        await s.wait_settled()  # must not propagate ValueError
        assert s.is_empty()

    @pytest.mark.asyncio
    async def test_cancel_all_and_count_failures_counts_clean_cancellations(self):
        s = ManagedTaskSet()
        started = asyncio.Event()

        async def work():
            started.set()
            await asyncio.sleep(10)

        task = asyncio.create_task(work())
        s.add(task)
        await started.wait()

        cancelled, cancellation_failed = await s.cancel_all_and_count_failures()
        assert (cancelled, cancellation_failed) == (1, 0)
        assert s.is_empty()

    @pytest.mark.asyncio
    async def test_cancel_all_and_count_failures_counts_suppressed_cancellation_as_failed(self):
        """A task that catches CancelledError and returns normally instead of
        re-raising did not honor the cancellation -- it must count toward
        cancellation_failed, not cancelled."""
        s = ManagedTaskSet()
        started = asyncio.Event()

        async def swallow_cancellation():
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return "ignored cancellation on purpose"

        task = asyncio.create_task(swallow_cancellation())
        s.add(task)
        await started.wait()

        cancelled, cancellation_failed = await s.cancel_all_and_count_failures()
        assert (cancelled, cancellation_failed) == (0, 1)
        assert s.is_empty()

    @pytest.mark.asyncio
    async def test_cancel_all_and_count_failures_counts_other_exception_as_failed(self):
        """A task that raises something other than CancelledError in
        response to cancel() is not a clean cancellation either."""
        s = ManagedTaskSet()
        started = asyncio.Event()

        async def raise_other_error():
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise ValueError("not a clean cancellation") from None

        task = asyncio.create_task(raise_other_error())
        s.add(task)
        await started.wait()

        cancelled, cancellation_failed = await s.cancel_all_and_count_failures()
        assert (cancelled, cancellation_failed) == (0, 1)
        assert s.is_empty()
```

2. 确认失败：`pytest tests/test_litellm/litellm_core_utils/test_managed_task_set.py -v`——`ModuleNotFoundError: No module named 'litellm.litellm_core_utils.managed_task_set'`。

3. 实现：

```python
"""
ManagedTaskSet: a strong-referenced set of asyncio.Task objects with
self-removing done callbacks, bulk cancellation, and fixed-point settlement
awaiting.

Shared by LoggingWorker (its existing `_running_tasks` bookkeeping, Task 5 of
this plan folds it in) and ManagedTaskSupervisor (Task 4). Sharing this one
primitive does NOT merge the two components' own drain semantics -- each
still decides for itself what "settled" means and in what order to check it.

Not thread-safe; single-event-loop use only, matching every other asyncio
primitive already in this codebase.
"""

from __future__ import annotations

import asyncio


class ManagedTaskSet:
    __slots__ = ("_tasks",)

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[object]] = set()

    def add(self, task: "asyncio.Task[object]") -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def __len__(self) -> int:
        return len(self._tasks)

    def is_empty(self) -> bool:
        return len(self._tasks) == 0

    def cancel_all(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()

    async def wait_settled(self) -> None:
        """Await every currently-tracked task to reach a terminal state
        (success, exception, or cancellation), swallowing exceptions --
        callers that care about individual outcomes must inspect the task
        themselves via other means (e.g. CompletionToken.settle). Loops
        until the set is a genuine fixed point: a task's own done-callback
        (invoked via call_soon, i.e. the next loop iteration) may add a new
        task to this same set before we re-check, so a single gather() over
        one snapshot would miss it.
        """
        while self._tasks:
            pending = tuple(self._tasks)
            await asyncio.gather(*pending, return_exceptions=True)

    async def cancel_all_and_count_failures(self) -> tuple[int, int]:
        """Cancel every currently-tracked task and report how many actually
        honored the cancellation cleanly.

        Returns (cancelled, cancellation_failed):
          - cancelled: tasks whose result was a CancelledError raised in
            direct response to this call's cancel().
          - cancellation_failed: tasks that, despite being cancelled, either
            returned normally (the coroutine caught CancelledError and chose
            not to re-raise) or raised a different exception. asyncio.gather()
            only returns once every snapshotted task is done, so there is no
            third "still pending" outcome for a task inside one snapshot --
            the fixed-point while-loop below exists only to also cancel a task
            that a done-callback races into adding to this set mid-round, the
            same race wait_settled() already guards against above.
        """
        cancelled = 0
        cancellation_failed = 0
        while self._tasks:
            pending = tuple(self._tasks)
            for task in pending:
                task.cancel()
            results = await asyncio.gather(*pending, return_exceptions=True)
            for result in results:
                if isinstance(result, asyncio.CancelledError):
                    cancelled += 1
                else:
                    cancellation_failed += 1
        return cancelled, cancellation_failed
```
```

4. 确认转绿：同一 pytest 命令全绿。

5. 提交：`git add litellm/litellm_core_utils/managed_task_set.py tests/test_litellm/litellm_core_utils/test_managed_task_set.py && git commit -m "feat: add ManagedTaskSet primitive for drainable detached tasks"`

---

## Task 2 — `CompletionToken` / `AccountingScope` 协议 + 中立 `ContextVar` + `spawn_detached`

独立，无前置依赖。纯新增文件。

**Files**: `litellm/litellm_core_utils/accounting_scope.py`（新）, `tests/test_litellm/litellm_core_utils/test_accounting_scope.py`（新）

**Interfaces**

```python
class CompletionToken(Protocol):
    def settle(self, outcome: AccountingOutcomeLike) -> None: ...

class AccountingOutcomeLike(Protocol):
    kind: str

class AccountingScope(Protocol):
    def is_valid(self) -> bool: ...
    def spawn(self, coro: Coroutine[object, object, object], *, name: str, kind: Literal["accounting", "telemetry"]) -> None: ...

class AccountingRootToken(CompletionToken, AccountingScope, Protocol):
    """Structural intersection: whatever the registered root-scope provider
    returns (a concrete AccountingLease, Task 4) satisfies both
    CompletionToken.settle() and AccountingScope.is_valid()/spawn() at once.
    Only Path A's root acquisition point (Task 6) needs both bound together
    into one value it can thread through `enqueue(token=...)`."""

current_accounting_scope: ContextVar[AccountingScope | None]

def register_root_scope_provider(provider: Callable[[], AccountingRootToken | None] | None) -> None: ...
def acquire_root_scope() -> AccountingRootToken | None: ...
def spawn_detached(coro: Coroutine[object, object, object], *, name: str) -> None: ...
def create_task_with_scope(coro: Coroutine[object, object, object], *, token: CompletionToken | None) -> "asyncio.Task[object]": ...

@contextlib.asynccontextmanager
def ambient_or_root_scope() -> "AsyncIterator[AccountingScope | None]": ...

@dataclass(frozen=True, slots=True)
class NeutralOutcomeCompleted:
    kind: Literal["completed"] = "completed"
```

**补充说明（Task 6 落笔时回填，如实记录）**：`AccountingRootToken` 这个组合 Protocol 与 `register_root_scope_provider`/`acquire_root_scope` 的返回类型放宽，是撰写 Task 6（Path A 6 处接入）时才发现的真实需要——Path A 的调用点需要把"能 settle 的 token"和"能做子任务授权的 scope"合并成同一个值传给 `ensure_initialized_and_enqueue(token=...)`，而 Task 2 最初落盘时只顾到 Path B（`spawn_detached`）单独需要 `AccountingScope`。这是纯粹的类型收紧/放宽（`AccountingRootToken` 结构上是 `AccountingScope` 的子类型，处处可替换），不改变任何已落盘运行时行为，也不破坏 Task 2 已保存的任何测试断言（那些测试只检查 `.spawn()`/`.settle()` 等运行时调用，从不检查静态类型标注）。

**范围调整说明（本轮评审回填，如实记录，非新增功能）**：以下两处调整都是把 Task 4/Task 8 已定的裁决（blocker 1/2：admission 只在 `settle()` 上关闭，不在 `spawn()` 上关闭；一条 lease 在自己 scope 存活期内可以派生任意有限次子任务）如实落到 Task 2 自身代码上的必然结果，不是独立新功能：

1. **`NeutralOutcomeCompleted` 从 Task 3 提前到本 Task 定义**。原计划把它和 `NeutralOutcomeFailed`/`NeutralOutcomeDropped` 一起放在 Task 3（`accounting_scope.py` 的追加改动里），但 `spawn_detached` 本身（本 Task 定义）就需要在下面第 2 点的修复里 settle 一个"完成"结果，晚到 Task 3 才有定义会在本 Task 内产生前向引用。`NeutralOutcomeFailed`/`NeutralOutcomeDropped` 不受影响，继续留在 Task 3（它们服务的 drop/rebind 路径本身也定义在 Task 3）。
2. **`spawn_detached` 的 ad hoc root 分支必须在 `spawn()` 之后立即自行 `settle()`**——这是撰写 Task 4 的 blocker 1/2 修复时才发现的真实缺口，如实记录：`spawn_detached` 在"没有 ambient scope、但 `acquire_root_scope()` 能拿到一个根 scope"这条回退路径里（即本次调用本身就是 root，例如 `streaming_handler.py` 的标准成功收尾场景），目前的实现只调用 `scope.spawn(...)` 就返回，从不调用 `scope.settle(...)`。在 blocker 1/2 修复前，这条路径能"蒙混过关"是因为旧版 `AccountingLease.spawn()` 的 `finally` 块本身就会关闭 admission（相当于把"一次性使用"的语义焐在了 `spawn()` 里）；一旦 blocker 1/2 把关闭 admission 的时机严格收敛到只有 `settle()` 才会触发（好让同一条 lease 在其 scope 存活期内可以派生任意有限次子任务），这条 ad hoc root 分支如果继续只 `spawn()` 不 `settle()`，`_admissions_in_progress` 会永久留一个計数不清零——因为这个 ad hoc root scope 只是这次函数调用里的局部变量，从未被返回给任何调用方，不会再有第二次机会去 `settle()` 它。修复：ad hoc root 分支在 `spawn()` 之后立即 `settle(NeutralOutcomeCompleted())`——这条 ad hoc root scope 存在的唯一目的就是"授权这一次 spawn"，spawn 调用本身返回的瞬间就是它生命周期的自然终点。
3. **新增 `ambient_or_root_scope()` 异步上下文管理器（本轮评审 blocker 4 落笔时补充，本 Task 内追加，非 Task 8 自行拍板新接口）**：blocker 4（`_ProxyDBLogger.async_post_call_failure_hook`，详见 Task 8 B4 小节）需要的不是"给单次 `spawn()` 授权"（`spawn_detached` 已经做的事），而是"给一整段可能先后触碰多次 Path B 调用点的函数体授权，函数体自己收尾时才 settle"——两者的 ambient-或-root 判定逻辑（`current_accounting_scope.get()`、`is_valid()`、否则 `acquire_root_scope()`、只在自己新拿的 root 上才 settle）完全相同，唯一区别是"谁负责在什么时刻调用 settle()"：`spawn_detached` 自己在 `spawn()` 之后立即 settle（服务单次派生），`ambient_or_root_scope()` 把 settle 挪到 `async with` 块退出时（服务一整段可能派生多次的函数体）。为避免在 Task 8 B4 的落笔点手写第二份重复的 is_ambient 判定逻辑（违反本项目"不重复造轮子"的编码约定），把这个判定逻辑收敛成 `accounting_scope.py` 自己的一个薄封装，供 `spawn_detached` 之外的"整段函数体自行acquire/settle"场景复用。

**Steps**

1. 写失败测试（新文件）：

```python
"""Neutral core/proxy-boundary contracts for accounting-lease propagation.

These protocols let core SDK code (e.g. streaming_handler.py) participate in
Phase 1b's accounting drain without ever importing a proxy-only type -- the
concrete AccountingLease (Task 4) is only ever reached via ContextVar
propagation or the registered root-scope provider, never via import.
"""

import asyncio
import contextvars

import pytest

from litellm.litellm_core_utils.accounting_scope import (
    acquire_root_scope,
    create_task_with_scope,
    current_accounting_scope,
    register_root_scope_provider,
    spawn_detached,
)


class _FakeOutcome:
    kind = "completed"


class _FakeScope:
    """Minimal stand-in satisfying both CompletionToken and AccountingScope,
    mirroring how the real AccountingLease (Task 4) implements both."""

    def __init__(self, valid: bool = True):
        self._valid = valid
        self.spawned: list[tuple[object, str, str]] = []
        self.settled: list[object] = []

    def is_valid(self) -> bool:
        return self._valid

    def spawn(self, coro, *, name, kind) -> None:
        coro.close()  # don't actually run it; just record the call
        self.spawned.append((coro, name, kind))

    def settle(self, outcome) -> None:
        self.settled.append(outcome)


class TestSpawnDetached:
    @pytest.mark.asyncio
    async def test_falls_back_to_bare_create_task_with_no_scope_and_no_provider(self):
        register_root_scope_provider(None)  # explicit: pure-SDK has no provider
        done = asyncio.Event()

        async def coro():
            done.set()

        spawn_detached(coro(), name="x")
        await asyncio.wait_for(done.wait(), timeout=1)

    @pytest.mark.asyncio
    async def test_uses_ambient_scope_when_present(self):
        scope = _FakeScope()
        token = current_accounting_scope.set(scope)
        try:
            async def coro():
                pass

            spawn_detached(coro(), name="update_cache")
        finally:
            current_accounting_scope.reset(token)

        assert len(scope.spawned) == 1
        _, name, kind = scope.spawned[0]
        assert name == "update_cache"
        assert kind == "accounting"
        assert scope.settled == []  # nested/ambient case: caller who created
        # this scope owns settling it, spawn_detached must never touch it

    @pytest.mark.asyncio
    async def test_ignores_ambient_scope_once_invalid_and_falls_back_to_bare_task(self):
        scope = _FakeScope(valid=False)
        token = current_accounting_scope.set(scope)
        done = asyncio.Event()
        try:
            async def coro():
                done.set()

            spawn_detached(coro(), name="x")
            await asyncio.wait_for(done.wait(), timeout=1)
        finally:
            current_accounting_scope.reset(token)

        assert scope.spawned == []  # never delegated to an invalid scope

    @pytest.mark.asyncio
    async def test_uses_root_scope_provider_when_no_ambient_scope(self):
        root_scope = _FakeScope()
        register_root_scope_provider(lambda: root_scope)
        try:
            async def coro():
                pass

            spawn_detached(coro(), name="root_boundary")
        finally:
            register_root_scope_provider(None)

        assert len(root_scope.spawned) == 1

    @pytest.mark.asyncio
    async def test_ad_hoc_root_scope_settles_itself_immediately_after_spawn(self):
        """补充说明 #2（本轮评审前回填）的回归测试：ad hoc root scope（本次
        调用本身就是 root，从 acquire_root_scope() 现拿现用、从未交还给任何
        调用方）必须在 spawn() 之后立即自行 settle()，否则
        AccountingLease._admissions_in_progress 会永久多计一次、永不清零——
        这条 lease 从此再也没有第二次机会被 settle()。"""
        root_scope = _FakeScope()
        register_root_scope_provider(lambda: root_scope)
        try:
            async def coro():
                pass

            spawn_detached(coro(), name="root_boundary")
        finally:
            register_root_scope_provider(None)

        assert len(root_scope.settled) == 1
        assert root_scope.settled[0].kind == "completed"


class TestCreateTaskWithScope:
    @pytest.mark.asyncio
    async def test_binds_scope_only_when_token_satisfies_accounting_scope_protocol(self):
        ctx = contextvars.copy_context()
        combined_token = _FakeScope()

        async def inner():
            return current_accounting_scope.get()

        task = ctx.run(create_task_with_scope, inner(), token=combined_token)
        seen = await task
        assert seen is combined_token

    @pytest.mark.asyncio
    async def test_leaves_scope_as_none_when_token_lacks_spawn_is_valid(self):
        class _PlainToken:
            def settle(self, outcome):
                pass

        ctx = contextvars.copy_context()

        async def inner():
            return current_accounting_scope.get()

        task = ctx.run(create_task_with_scope, inner(), token=_PlainToken())
        assert await task is None

    @pytest.mark.asyncio
    async def test_leaves_scope_as_none_when_token_is_none(self):
        ctx = contextvars.copy_context()

        async def inner():
            return current_accounting_scope.get()

        task = ctx.run(create_task_with_scope, inner(), token=None)
        assert await task is None


class TestAmbientOrRootScope:
    """blocker 4 (本轮评审): async_post_call_failure_hook 需要的是"整段函数体
    自行 acquire/settle"，不是 spawn_detached 那种单次派生授权——见 Task 2
    "范围调整说明" #3。"""

    @pytest.mark.asyncio
    async def test_yields_ambient_scope_without_settling_it_on_exit(self):
        from litellm.litellm_core_utils.accounting_scope import ambient_or_root_scope

        scope = _FakeScope()
        token = current_accounting_scope.set(scope)
        try:
            async with ambient_or_root_scope() as yielded:
                assert yielded is scope
        finally:
            current_accounting_scope.reset(token)

        assert scope.settled == []  # nested/ambient: caller who created this
        # scope still owns settling it, ambient_or_root_scope must never touch it

    @pytest.mark.asyncio
    async def test_acquires_and_settles_a_fresh_root_scope_when_no_ambient_scope(self):
        from litellm.litellm_core_utils.accounting_scope import (
            ambient_or_root_scope,
            register_root_scope_provider,
        )

        root_scope = _FakeScope()
        register_root_scope_provider(lambda: root_scope)
        try:
            async with ambient_or_root_scope() as yielded:
                assert yielded is root_scope
                assert root_scope.settled == []  # not yet -- still inside the block
        finally:
            register_root_scope_provider(None)

        assert len(root_scope.settled) == 1
        assert root_scope.settled[0].kind == "completed"

    @pytest.mark.asyncio
    async def test_settles_even_when_the_body_raises(self):
        from litellm.litellm_core_utils.accounting_scope import (
            ambient_or_root_scope,
            register_root_scope_provider,
        )

        root_scope = _FakeScope()
        register_root_scope_provider(lambda: root_scope)
        try:
            with pytest.raises(ValueError):
                async with ambient_or_root_scope():
                    raise ValueError("boom")
        finally:
            register_root_scope_provider(None)

        assert len(root_scope.settled) == 1

    @pytest.mark.asyncio
    async def test_yields_none_and_settles_nothing_when_no_scope_available_at_all(self):
        from litellm.litellm_core_utils.accounting_scope import (
            ambient_or_root_scope,
            register_root_scope_provider,
        )

        register_root_scope_provider(None)  # explicit: pure-SDK has no provider
        async with ambient_or_root_scope() as yielded:
            assert yielded is None
```

2. 确认失败：`pytest tests/test_litellm/litellm_core_utils/test_accounting_scope.py -v`——`ModuleNotFoundError`。

3. 实现：

```python
"""
Neutral (core-SDK-layer) contracts for accounting-lease propagation across
the core/proxy boundary.

`litellm_core_utils` must never import proxy-only types. The concrete
AccountingLease (litellm/proxy/shutdown/managed_task_supervisor.py) is only
ever reached from here via ContextVar propagation (nested case) or the
one-time `register_root_scope_provider` DI hook (root case) -- never via a
direct import.

Pure-SDK usage (no `litellm.proxy.*` package ever imported) never triggers
`register_root_scope_provider`, so `_root_scope_provider` stays `None` and
every function below degrades to byte-identical bare `asyncio.create_task`
behavior -- this is the exact mechanism that keeps the pre-Phase-1b pure-SDK
code path unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import dataclasses
from typing import AsyncIterator, Callable, Coroutine, Literal, Protocol, runtime_checkable


class AccountingOutcomeLike(Protocol):
    """Structural stand-in for AccountingCompleted|Skipped|Failed (Task 6)
    so this module never imports litellm.proxy.shutdown.accounting_outcome."""

    kind: str


class CompletionToken(Protocol):
    """A once-only settlement handle for a single unit of accounting work."""

    def settle(self, outcome: AccountingOutcomeLike) -> None: ...


@runtime_checkable
class AccountingScope(Protocol):
    """Capability object authorizing further child-task admission.

    Deliberately NOT authorized by ContextVar presence alone (spec: "admission
    授权用不可伪造 scope，不靠 ContextVar != None") -- every caller must hold
    an explicit reference and call `is_valid()`/`spawn()` on it; the
    ContextVar below only carries that reference across an import boundary.
    """

    def is_valid(self) -> bool: ...

    def spawn(
        self,
        coro: "Coroutine[object, object, object]",
        *,
        name: str,
        kind: Literal["accounting", "telemetry"],
    ) -> None: ...


class AccountingRootToken(CompletionToken, AccountingScope, Protocol):
    """Structural intersection returned by acquire_root_scope() at the root
    (Task 6, Path A) acquisition points: the concrete provider (a real
    AccountingLease, Task 4) always satisfies both CompletionToken.settle()
    and AccountingScope.is_valid()/spawn() at once, so a single value can be
    threaded through LoggingWorker.ensure_initialized_and_enqueue(token=...)
    to serve both roles. Declared as its own Protocol (rather than folding
    settle() into AccountingScope directly) to keep AccountingScope's own
    contract minimal and independently reusable for Path B call sites
    (spawn_detached) that only ever need the scope half."""


current_accounting_scope: "contextvars.ContextVar[AccountingScope | None]" = contextvars.ContextVar(
    "current_accounting_scope", default=None
)

_root_scope_provider: "Callable[[], AccountingRootToken | None] | None" = None


@dataclasses.dataclass(frozen=True, slots=True)
class NeutralOutcomeCompleted:
    """Neutral (proxy-agnostic) completed-outcome value, settled by
    spawn_detached()'s own ad hoc root branch onto a scope it acquired and
    owns for the duration of a single call (see this Task's "范围调整说明"
    #1/#2 above): kept here, not in Task 3's NeutralOutcomeFailed/Dropped,
    because spawn_detached (defined in this same module) needs it, and
    defining it at Task 3 instead would create a forward reference within
    this Task."""

    kind: Literal["completed"] = "completed"


def register_root_scope_provider(provider: "Callable[[], AccountingRootToken | None] | None") -> None:
    """Revocable DI registration hook (major 9): Task 10's lifespan startup
    calls this once, after the proxy has finished starting up, with
    `GLOBAL_MANAGED_TASK_SUPERVISOR.acquire_root_lease`; Task 10's lifespan
    shutdown calls it again with `None` once `drain()` has fully returned.
    Deliberately NOT a one-shot import-time side effect (an earlier revision
    of this docstring said so; superseded once this Task's registration call
    itself moved out of module-import time and into Task 10's lifespan, so
    the same-process "run a proxy, shut it down, then make a pure-SDK call"
    scenario never keeps a dangling reference to an already-shut-down
    supervisor). Passing `None` is also this module's own default (never
    imported/registered at all == pure-SDK usage, unaffected)."""
    global _root_scope_provider
    _root_scope_provider = provider


def acquire_root_scope() -> "AccountingRootToken | None":
    if _root_scope_provider is None:
        return None
    return _root_scope_provider()


def spawn_detached(coro: "Coroutine[object, object, object]", *, name: str) -> None:
    """The only sanctioned replacement for a bare `asyncio.create_task(coro)`
    at a Path B accounting-relevant creation point in core SDK code.

    Resolution order: ambient scope (nested Path B site, e.g. update_cache
    running inside a Path A/B root's context) -> freshly acquired root scope
    (this call site *is* the root, e.g. streaming_handler.py's standard
    streaming success boundary) -> bare create_task (no accounting machinery
    registered at all, i.e. pure-SDK; or the scope refused admission).

    The ad hoc root branch settles its freshly-acquired scope immediately
    after spawning through it (see this Task's "范围调整说明" #2 above): this
    scope is a purely local variable, never handed back to any caller, so
    nothing else will ever call settle() on it -- without this, a
    supervisor's `_admissions_in_progress` counter would leak one
    permanently-uncleared count per ad hoc call. An ambient (nested) scope is
    never settled here -- its root caller owns that.
    """
    scope = current_accounting_scope.get()
    is_ambient = scope is not None and scope.is_valid()
    if not is_ambient:
        scope = acquire_root_scope()
    if scope is not None and scope.is_valid():
        scope.spawn(coro, name=name, kind="accounting")
        if not is_ambient:
            scope.settle(NeutralOutcomeCompleted())
        return
    asyncio.create_task(coro)


def create_task_with_scope(
    coro: "Coroutine[object, object, object]", *, token: "CompletionToken | None"
) -> "asyncio.Task[object]":
    """Bind `current_accounting_scope` from `token` (only if it structurally
    satisfies AccountingScope too -- true for every concrete AccountingLease,
    per Task 4) before creating the task, so that nested Path B sites reached
    from within `coro`'s own call stack (e.g. update_cache called from
    _PROXY_track_cost_callback) can find the scope via
    `current_accounting_scope.get()` without this module ever importing
    AccountingLease.

    Callers that need this bound inside a specific captured Context (e.g.
    LoggingWorker replaying a per-item `contextvars.Context`) invoke this via
    `context.run(create_task_with_scope, coro, token=token)`; called directly,
    it binds into whatever Context is currently active.
    """
    if isinstance(token, AccountingScope):
        current_accounting_scope.set(token)
    return asyncio.create_task(coro)


@contextlib.asynccontextmanager
async def ambient_or_root_scope() -> "AsyncIterator[AccountingScope | None]":
    """Yield the ambient scope if one is already set and still valid;
    otherwise acquire a fresh root scope for the duration of the `async with`
    block and settle it (NeutralOutcomeCompleted) on exit -- including when
    the block raises, so a failure inside the body never leaks an
    unsettled ad hoc root the way spawn_detached's own fallback would if it
    forgot to settle (Task 2 "范围调整说明" #2).

    Unlike spawn_detached (which settles immediately after a single spawn()),
    this is for a whole unit of work that may spawn zero, one, or several
    Path B children before it is done -- e.g. blocker 4's
    async_post_call_failure_hook, which reaches update_database's single
    spawn_detached call site but should not force spawn_detached to keep
    re-resolving ambient-vs-root on every nested call once the outer function
    has already established one for its own duration.

    A nested/ambient scope is never settled here -- its own root caller owns
    that, exactly as in spawn_detached.
    """
    existing = current_accounting_scope.get()
    is_ambient = existing is not None and existing.is_valid()
    scope = existing if is_ambient else acquire_root_scope()
    token = current_accounting_scope.set(scope) if scope is not None and not is_ambient else None
    try:
        yield scope
    finally:
        if scope is not None and not is_ambient:
            scope.settle(NeutralOutcomeCompleted())
        if token is not None:
            current_accounting_scope.reset(token)
```

4. 确认转绿：`pytest tests/test_litellm/litellm_core_utils/test_accounting_scope.py -v` 全绿（已核实 `pyproject.toml:291` 设置 `asyncio_mode = "auto"`，全部用 `@pytest.mark.asyncio` + `async def` 写法，与 `test_logging_worker.py` 等既有测试一致，不引入本仓库未用过的 `event_loop` fixture）。

5. 提交：`git add litellm/litellm_core_utils/accounting_scope.py tests/test_litellm/litellm_core_utils/test_accounting_scope.py && git commit -m "feat: add neutral CompletionToken/AccountingScope protocols for cross-layer accounting propagation"`

---

## Task 3 — `LoggingTask` → frozen dataclass + `token` 传递 + 每条 drop/rebind 路径 settle

依赖 Task 2（`CompletionToken`/`AccountingOutcomeLike`/`create_task_with_scope`）。

**已读现状**（`litellm/litellm_core_utils/logging_worker.py`，523 行，全文已核对）：`LoggingTask` 目前是 `TypedDict(coroutine, context)`；`task["coroutine"]`/`task["context"]` 的读取点共 4 处（`_process_log_task`、`_process_single_task`、`clear_queue`、`_flush_on_exit`）；drop/rebind 路径共 4 处：`enqueue()` 的 `if self._queue is None: return`、`_ensure_queue()` 换 loop 时丢旧 `_running_tasks`（且旧 `Queue` 对象连带里面的未处理 item 一起被垃圾回收，从未被排空/settle 过——这是本次读码新发现的一个真实丢失点，Phase 1a/spec 文本都没有点名到这个具体分支，下文按 spec"每一条 drop/rebind 路径"的字面要求把它也纳入）、`_schedule_delayed_enqueue_retry` 的 `except RuntimeError: pass`、`_retry_enqueue_task` 的 `if self._queue is None: return`。

**设计说明（协议形状的必要细化，非违反）**：spec 原文"`token.settle(outcome)` 幂等"是单方法、单参数的形状，本计划照此实现，不拆成多个方法。但 `outcome` 参数如果要求调用方传入 proxy 层的具体 `AccountingCompleted`/`AccountingFailed`/`AccountingSkippedDuringShutdown`（Task 8），会让核心层 `logging_worker.py` 需要 import proxy 类型，直接违反"不让通用 LoggingWorker 依赖 proxy 类型"这条更高优先级的硬约束。`AccountingOutcomeLike` 是纯结构化 `Protocol`（只要求 `kind: str`），所以解法是：在同样中立的 `accounting_scope.py`（Task 2 所在模块）里补三个极小的中立 outcome 值类型，`logging_worker.py` 只构造这三个中立类型；Task 4 的 `AccountingLease.settle()` 收到后按 `outcome.kind` 转换成它自己对外暴露的 proxy 侧富类型（复用 Phase 1a 已经定义、`kind` 判别字段完全同名的 `AccountingSkippedDuringShutdown`），不需要 `logging_worker.py` 反向 import proxy 包，也不需要移动 Phase 1a 已经落好的 `litellm/proxy/shutdown/accounting_outcome.py`。两组 dataclass 字段形状故意保持一致（`kind` 字面量同名字符串），只是分别属于"中立层允许构造的值"和"proxy 层对自己调用方暴露的富值"两个不同的构造语境，不是意外重复——这一点在收尾报告里会再次提醒复核。

**Files**: `litellm/litellm_core_utils/logging_worker.py`（改）, `litellm/litellm_core_utils/accounting_scope.py`（改，补 3 个中立 outcome 类型）, `tests/test_litellm/litellm_core_utils/test_logging_worker.py`（改，扩展既有文件）

**Interfaces**

```python
# accounting_scope.py 追加：
@dataclass(frozen=True, slots=True)
class NeutralOutcomeCompleted:
    kind: Literal["completed"] = "completed"

@dataclass(frozen=True, slots=True)
class NeutralOutcomeFailed:
    error: str
    kind: Literal["failed"] = "failed"

@dataclass(frozen=True, slots=True)
class NeutralOutcomeDropped:
    reason: str
    kind: Literal["skipped_during_shutdown"] = "skipped_during_shutdown"

# logging_worker.py:
@dataclasses.dataclass(frozen=True, slots=True)
class LoggingTask:
    coroutine: Coroutine
    context: contextvars.Context
    token: "CompletionToken | None" = None

def enqueue(self, coroutine: Coroutine, *, token: "CompletionToken | None" = None) -> None: ...
def ensure_initialized_and_enqueue(self, async_coroutine: Coroutine, *, token: "CompletionToken | None" = None) -> None: ...
```

**Steps**

1. 写失败测试（扩展 `tests/test_litellm/litellm_core_utils/test_logging_worker.py`，追加到既有 `TestLoggingWorker` 类里；已核对该文件当前没有任何地方直接构造 `LoggingTask(...)`，所以把 `TypedDict` 换成 `frozen dataclass` 不会破坏既有测试的构造点）：

```python
    def test_pure_sdk_enqueue_and_flush_unchanged_when_token_is_none(self):
        """Regression guard: token=None must be byte-for-byte today's
        behavior. This test must already pass against pre-Task-3 code too."""
        worker = LoggingWorker()
        worker.start()
        ran = []

        async def coro():
            ran.append("x")

        worker.ensure_initialized_and_enqueue(coro())
        return worker  # helper continued below via pytest-asyncio

    @pytest.mark.asyncio
    async def test_pure_sdk_flush_runs_coroutine_without_a_token(self):
        worker = LoggingWorker()
        worker.start()
        ran = []

        async def coro():
            ran.append("x")

        worker.ensure_initialized_and_enqueue(coro())
        await worker.flush()
        assert ran == ["x"]
        await worker.stop()

    @pytest.mark.asyncio
    async def test_settles_token_as_completed_when_coroutine_succeeds(self):
        worker = LoggingWorker()
        worker.start()
        settled = []

        class FakeToken:
            def settle(self, outcome):
                settled.append(outcome)

        async def coro():
            return None

        worker.ensure_initialized_and_enqueue(coro(), token=FakeToken())
        await worker.flush()
        assert len(settled) == 1
        assert settled[0].kind == "completed"
        await worker.stop()

    @pytest.mark.asyncio
    async def test_settles_token_as_failed_when_coroutine_raises(self):
        worker = LoggingWorker()
        worker.start()
        settled = []

        class FakeToken:
            def settle(self, outcome):
                settled.append(outcome)

        async def coro():
            raise ValueError("boom")

        worker.ensure_initialized_and_enqueue(coro(), token=FakeToken())
        await worker.flush()
        assert len(settled) == 1
        assert settled[0].kind == "failed"
        await worker.stop()

    def test_settles_token_as_dropped_when_queue_not_initialized(self):
        """enqueue() with no queue (worker never started) must settle the
        token as dropped instead of silently discarding it."""
        worker = LoggingWorker()  # start() never called -> self._queue is None
        settled = []

        class FakeToken:
            def settle(self, outcome):
                settled.append(outcome)

        async def coro():
            pass

        c = coro()
        worker.enqueue(c, token=FakeToken())
        c.close()
        assert len(settled) == 1
        assert settled[0].kind == "skipped_during_shutdown"

    def test_ensure_queue_settles_tokens_of_items_still_in_the_old_queue_on_loop_change(self):
        """Regression for the previously-unhandled event-loop-change drop:
        _ensure_queue() must drain and settle the OLD queue's remaining
        items before replacing it, not just clear `_running_tasks`."""
        worker = LoggingWorker()
        worker.start()
        settled = []

        class FakeToken:
            def settle(self, outcome):
                settled.append(outcome)

        async def coro():
            pass

        c = coro()
        worker.enqueue(c, token=FakeToken())
        assert worker._queue.qsize() == 1

        # Simulate a bound-loop change without actually needing a second
        # real event loop: fake a different loop object identity.
        class _FakeLoop:
            pass

        worker._bound_loop = _FakeLoop()
        worker._ensure_queue()

        assert len(settled) == 1
        assert settled[0].kind == "skipped_during_shutdown"
        c.close()

    @pytest.mark.asyncio
    async def test_schedule_delayed_enqueue_retry_settles_dropped_token_with_no_running_loop(self, monkeypatch):
        worker = LoggingWorker()
        worker.start()
        settled = []

        class FakeToken:
            def settle(self, outcome):
                settled.append(outcome)

        async def coro():
            pass

        c = coro()
        task = LoggingTask(coroutine=c, context=contextvars.copy_context(), token=FakeToken())

        def _raise_runtime_error():
            raise RuntimeError("no running event loop")

        monkeypatch.setattr(asyncio, "get_running_loop", _raise_runtime_error)
        worker._schedule_delayed_enqueue_retry(task)
        assert len(settled) == 1
        assert settled[0].kind == "skipped_during_shutdown"
        c.close()

    @pytest.mark.asyncio
    async def test_retry_enqueue_task_settles_dropped_token_when_queue_gone(self):
        worker = LoggingWorker()
        worker.start()
        settled = []

        class FakeToken:
            def settle(self, outcome):
                settled.append(outcome)

        async def coro():
            pass

        c = coro()
        task = LoggingTask(coroutine=c, context=contextvars.copy_context(), token=FakeToken())
        worker._queue = None  # simulate queue having been torn down

        await worker._retry_enqueue_task(task, delay=0)
        assert len(settled) == 1
        assert settled[0].kind == "skipped_during_shutdown"
        c.close()

    @pytest.mark.asyncio
    async def test_stop_cancellation_settles_token_of_in_flight_task(self):
        """stop()'s cancellation path must settle the token too, not leave
        it forever unsettled just because the coroutine never finished."""
        worker = LoggingWorker()
        worker.start()
        settled = []

        class FakeToken:
            def settle(self, outcome):
                settled.append(outcome)

        async def slow_coro():
            await asyncio.sleep(10)

        worker.ensure_initialized_and_enqueue(slow_coro(), token=FakeToken())
        await asyncio.sleep(0.05)  # let the worker dequeue and start it
        await worker.stop()

        assert len(settled) == 1
        assert settled[0].kind in {"failed", "skipped_during_shutdown"}
```

2. 确认失败：`pytest tests/test_litellm/litellm_core_utils/test_logging_worker.py -v -k "token or dropped or loop_change or stop_cancellation"`——`TypeError: LoggingTask() takes no keyword argument 'token'` 及若干 `AttributeError`。

3. 实现。先扩展 `accounting_scope.py`（在 Task 2 文件末尾追加）：

```python
import dataclasses
from typing import Literal


@dataclasses.dataclass(frozen=True, slots=True)
class NeutralOutcomeCompleted:
    """Neutral value satisfying AccountingOutcomeLike for a fully-run
    coroutine. The concrete AccountingLease.settle() (Task 4) reads
    `.kind` to decide what proxy-visible outcome to record."""

    kind: Literal["completed"] = "completed"


@dataclasses.dataclass(frozen=True, slots=True)
class NeutralOutcomeFailed:
    error: str
    kind: Literal["failed"] = "failed"


@dataclasses.dataclass(frozen=True, slots=True)
class NeutralOutcomeDropped:
    reason: str
    kind: Literal["skipped_during_shutdown"] = "skipped_during_shutdown"
```

（把顶部 `from typing import Callable, Coroutine, Literal, Protocol, runtime_checkable` 和 `import dataclasses` 合并进 Task 2 已有的 import 块，不重复导入。）

然后改写 `litellm/litellm_core_utils/logging_worker.py`：

```python
import asyncio
import contextvars
import dataclasses
import logging
from typing import Coroutine, Optional
import atexit

from litellm._logging import verbose_logger
from litellm.constants import (
    LOGGING_WORKER_CONCURRENCY,
    LOGGING_WORKER_MAX_QUEUE_SIZE,
    LOGGING_WORKER_MAX_TIME_PER_COROUTINE,
    LOGGING_WORKER_CLEAR_PERCENTAGE,
    LOGGING_WORKER_AGGRESSIVE_CLEAR_COOLDOWN_SECONDS,
    MAX_ITERATIONS_TO_CLEAR_QUEUE,
    MAX_TIME_TO_CLEAR_QUEUE,
)
from litellm.litellm_core_utils.accounting_scope import (
    CompletionToken,
    NeutralOutcomeCompleted,
    NeutralOutcomeDropped,
    NeutralOutcomeFailed,
    create_task_with_scope,
)
from litellm.litellm_core_utils.managed_task_set import ManagedTaskSet


@dataclasses.dataclass(frozen=True, slots=True)
class LoggingTask:
    """
    A logging task with its associated context to ensure logging is executed
    in the original task's context.

    `token` is optional and neutral (CompletionToken protocol, satisfied by
    proxy's AccountingLease -- see accounting_scope.py). Pure-SDK callers
    never pass one, so it stays None and every settle-on-drop path below is
    a no-op for them, preserving today's behavior byte-for-byte.
    """

    coroutine: Coroutine
    context: contextvars.Context
    token: "CompletionToken | None" = None


def _settle(token: "CompletionToken | None", outcome) -> None:
    if token is not None:
        token.settle(outcome)


@dataclasses.dataclass(frozen=True, slots=True)
class Enqueued:
    pass


@dataclasses.dataclass(frozen=True, slots=True)
class Rejected:
    reason: str


EnqueueOutcome = Enqueued | Rejected
"""blocker 3（本轮评审）：`enqueue()`的hot-path首次尝试与`_retry_enqueue_task`的
延迟重试，此前各自独立检查不同的前置条件（前者查 `_queue is None`/`_admission_open`
两项，后者只查 `_queue is None` 一项，从未检查 `_admission_open` 或任何"硬 quiesce"
状态）——意味着 quiesce() 关闭 admission 之后，一个仍在延迟重试队列里的旧 item
依然可能在 admission 已关闭的情况下被重新塞回队列、并被 worker loop 正常执行，
与"关停期不再接受/执行新工作"这条约定矛盾。统一成 `_try_enqueue_existing_task`
一个入口，两个调用点都过同一套检查（queue 存在性 / `_admission_open` / 
`_quiesced`（major 8 新增的硬 quiesce 标志）/ 实际 `put_nowait()` 是否成功）。"""


class LoggingWorker:
    """
    A simple, async logging worker that processes log coroutines in the background.
    Designed to be best-effort with bounded queues to prevent backpressure.

    This leads to a +200 RPS performance improvement when using LiteLLM Python SDK or Proxy Server.
    - Use this to queue coroutine tasks that are not critical to the main flow of the application. e.g Success/Error callbacks, logging, etc.
    """

    def __init__(
        self,
        timeout: float = LOGGING_WORKER_MAX_TIME_PER_COROUTINE,
        max_queue_size: int = LOGGING_WORKER_MAX_QUEUE_SIZE,
        concurrency: int = LOGGING_WORKER_CONCURRENCY,
    ):
        self.timeout = timeout
        self.max_queue_size = max_queue_size
        self.concurrency = concurrency
        self._queue: Optional[asyncio.Queue[LoggingTask]] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._running_tasks: ManagedTaskSet = ManagedTaskSet()
        self._helper_tasks: ManagedTaskSet = ManagedTaskSet()
        self._sem: Optional[asyncio.Semaphore] = None
        self._bound_loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_aggressive_clear_time: float = 0.0
        self._aggressive_clear_in_progress: bool = False
        self._admission_open: bool = True
        """Flipped by quiesce() (Task 5) via its `admission_policy` callable.
        SDK-only usage never calls quiesce(), so this stays True forever for
        pure-SDK callers -- part of the byte-for-byte-unchanged guarantee."""
        self._quiesced: bool = False
        """major 8 (本轮评审新增)：一旦 `stop_after_quiesce()`（Task 5）被调用，
        永久置真——与 `_admission_open` 是两个独立维度：`_admission_open` 是
        quiesce() 循环期间逐轮翻面的"这一刻是否还接受新 enqueue"，`_quiesced`
        是"worker loop 自身是否已经进入终态、绝不能再执行 clear_queue() 里的
        业务 callback"。`_worker_loop` 自己的 `except CancelledError` 分支据此
        判断：为真则走 `_drain_and_settle_dropped`（只丢弃结算，不执行），为假
        则维持 pre-Phase-1b 既有行为，仍走 `clear_queue()`（会执行队列里的
        业务 callback，供既有非 quiesce 的 `stop()` 调用方保持行为不变）。"""

        atexit.register(self._flush_on_exit)

    def _drain_and_settle_dropped(self, queue: "asyncio.Queue[LoggingTask] | None", reason: str) -> int:
        """Synchronously drain every remaining item out of `queue` and
        settle its token as dropped. Used wherever a queue is about to be
        discarded (event-loop change) or emptied post-deadline (quiesce(),
        Task 5) without ever being processed. Returns the number of items
        drained, so quiesce() can report it in LoggingDeadlineExceeded.

        **major 7 修订**：每一次 `get_nowait()` 都必须配一次 `task_done()`——
        `get_nowait()` 会递减 `asyncio.Queue` 内部的 unfinished-task 计数器，
        而 `flush()`/`worker.flush()` 依赖 `queue.join()` 等这个计数器归零；
        此前这里从未调用过 `task_done()`，导致任何一次真正走到这个分支的 drop
        （event-loop 切换、或 quiesce() 的 deadline 分支）都会让 `join()`
        永远挂起，因为计数器从未被这些"直接丢弃、从未真正处理"的 item 减到底。"""
        if queue is None:
            return 0
        drained = 0
        while True:
            try:
                task = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                task.coroutine.close()
                _settle(task.token, NeutralOutcomeDropped(reason=reason))
                drained += 1
            finally:
                queue.task_done()
        return drained

    def _ensure_queue(self) -> None:
        """Initialize the queue if it doesn't exist or if event loop has changed."""
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        if self._queue is not None and self._bound_loop is not current_loop:
            verbose_logger.debug("LoggingWorker: Event loop changed, reinitializing queue and worker")
            self._drain_and_settle_dropped(self._queue, reason="event_loop_changed")
            self._queue = None
            self._sem = None
            self._worker_task = None
            # A cross-loop cancel/await of the old ManagedTaskSet's tasks is
            # not meaningful (asyncio tasks are bound to the loop that
            # created them) -- replace with a fresh, empty tracker instead.
            self._running_tasks = ManagedTaskSet()

        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self.max_queue_size)
            self._bound_loop = current_loop

    def start(self) -> None:
        """Start the logging worker. Idempotent - safe to call multiple times."""
        self._ensure_queue()
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.concurrency)
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker_loop())

    async def _run_task_and_settle(self, task: LoggingTask) -> None:
        """Run `task.coroutine` in its captured context (with the
        accounting scope bound from `task.token`, if any) and settle the
        token exactly once, whatever the outcome."""
        try:
            inner = task.context.run(create_task_with_scope, task.coroutine, token=task.token)
            await asyncio.wait_for(inner, timeout=self.timeout)
        except asyncio.CancelledError:
            _settle(task.token, NeutralOutcomeDropped(reason="cancelled_during_shutdown"))
            raise
        except Exception as e:
            verbose_logger.exception(f"LoggingWorker error: {e}")
            _settle(task.token, NeutralOutcomeFailed(error=str(e)))
        else:
            _settle(task.token, NeutralOutcomeCompleted())

    async def _process_log_task(self, task: LoggingTask, sem: asyncio.Semaphore):
        """Runs the logging task and handles cleanup. Releases semaphore when done."""
        try:
            if self._queue is not None:
                try:
                    await self._run_task_and_settle(task)
                finally:
                    self._queue.task_done()
        finally:
            sem.release()

    async def _worker_loop(self) -> None:
        """Main worker loop that gets tasks and schedules them to run concurrently."""
        try:
            if self._queue is None or self._sem is None:
                return

            while True:
                await self._sem.acquire()
                try:
                    task = await self._queue.get()
                    processing_task = asyncio.create_task(self._process_log_task(task, self._sem))
                    self._running_tasks.add(processing_task)
                except Exception:
                    self._sem.release()
                    raise

        except asyncio.CancelledError:
            verbose_logger.debug("LoggingWorker cancelled during shutdown")
            if self._quiesced:
                # major 8: once stop_after_quiesce() has run, cancellation
                # must never fall through to clear_queue() -- that method
                # still executes queued business coroutines (see its own
                # docstring), which is exactly what quiesce()'s deadline
                # path and the whole shutdown contract forbid past this
                # point. Drop-and-settle only.
                self._drain_and_settle_dropped(self._queue, reason="quiesced_shutdown")
            else:
                await self.clear_queue()

    def _try_enqueue_existing_task(self, task: LoggingTask) -> "EnqueueOutcome":
        """Single admission gate for handing an already-constructed
        LoggingTask to `self._queue` -- shared by enqueue()'s hot-path
        attempt and _retry_enqueue_task()'s delayed retry (blocker 3), so
        both check exactly the same preconditions in the same order instead
        of drifting apart (the retry path previously only checked `_queue is
        None`, silently ignoring `_admission_open`)."""
        if self._queue is None:
            return Rejected(reason="worker_not_initialized")
        if not self._admission_open:
            return Rejected(reason="admission_closed")
        if self._quiesced:
            return Rejected(reason="quiesced")
        try:
            self._queue.put_nowait(task)
        except asyncio.QueueFull:
            return Rejected(reason="queue_full")
        return Enqueued()

    def enqueue(self, coroutine: Coroutine, *, token: "CompletionToken | None" = None) -> None:
        """
        Add a coroutine to the logging queue.
        Hot path: never blocks, aggressively clears queue if full.
        """
        task = LoggingTask(coroutine=coroutine, context=contextvars.copy_context(), token=token)
        outcome = self._try_enqueue_existing_task(task)
        match outcome:
            case Enqueued():
                return
            case Rejected(reason="queue_full"):
                verbose_logger.exception("LoggingWorker queue is full")
                self._handle_queue_full(task)
            case Rejected(reason=reason):
                coroutine.close()
                _settle(token, NeutralOutcomeDropped(reason=reason))
            case _:
                assert_never(outcome)

    def _should_start_aggressive_clear(self) -> bool:
        if self._aggressive_clear_in_progress:
            return False

        try:
            loop = asyncio.get_running_loop()
            current_time = loop.time()
            time_since_last_clear = current_time - self._last_aggressive_clear_time

            if time_since_last_clear < LOGGING_WORKER_AGGRESSIVE_CLEAR_COOLDOWN_SECONDS:
                return False

            return True
        except RuntimeError:
            return False

    def _mark_aggressive_clear_started(self) -> None:
        loop = asyncio.get_running_loop()
        self._last_aggressive_clear_time = loop.time()
        self._aggressive_clear_in_progress = True

    def _handle_queue_full(self, task: LoggingTask) -> None:
        if self._should_start_aggressive_clear():
            self._mark_aggressive_clear_started()
            self._helper_tasks.add(asyncio.create_task(self._aggressively_clear_queue_async(task)))
        else:
            self._schedule_delayed_enqueue_retry(task)


    def _calculate_retry_delay(self) -> float:
        try:
            loop = asyncio.get_running_loop()
            current_time = loop.time()
            time_since_last_clear = current_time - self._last_aggressive_clear_time
            remaining_cooldown = max(
                0.0,
                LOGGING_WORKER_AGGRESSIVE_CLEAR_COOLDOWN_SECONDS - time_since_last_clear,
            )
            return remaining_cooldown + max(0.05, LOGGING_WORKER_AGGRESSIVE_CLEAR_COOLDOWN_SECONDS * 0.1)
        except RuntimeError:
            return 0.1

    def _schedule_delayed_enqueue_retry(self, task: LoggingTask) -> None:
        try:
            asyncio.get_running_loop()
            delay = self._calculate_retry_delay()
            self._helper_tasks.add(asyncio.create_task(self._retry_enqueue_task(task, delay)))
        except RuntimeError:
            task.coroutine.close()
            _settle(task.token, NeutralOutcomeDropped(reason="no_event_loop_for_retry"))

    async def _retry_enqueue_task(self, task: LoggingTask, delay: float) -> None:
        await asyncio.sleep(delay)

        outcome = self._try_enqueue_existing_task(task)
        match outcome:
            case Enqueued():
                return
            case Rejected(reason="queue_full"):
                self._handle_queue_full(task)
            case Rejected(reason=reason):
                task.coroutine.close()
                _settle(task.token, NeutralOutcomeDropped(reason=reason))
            case _:
                assert_never(outcome)

    def _extract_tasks_from_queue(self) -> list[LoggingTask]:
        if self._queue is None:
            return []

        items_to_extract = (self.max_queue_size * LOGGING_WORKER_CLEAR_PERCENTAGE) // 100
        actual_size = self._queue.qsize()
        if actual_size == 0:
            return []
        items_to_extract = min(items_to_extract, actual_size)

        extracted_tasks = []
        for _ in range(items_to_extract):
            try:
                extracted_tasks.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        return extracted_tasks

    async def _aggressively_clear_queue_async(self, new_task: Optional[LoggingTask] = None) -> None:
        try:
            if self._queue is None:
                return

            extracted_tasks = self._extract_tasks_from_queue()

            if new_task is not None:
                extracted_tasks.append(new_task)

            if extracted_tasks:
                await self._process_extracted_tasks(extracted_tasks)
        except Exception as e:
            verbose_logger.exception(f"LoggingWorker error during aggressive clear: {e}")
        finally:
            self._aggressive_clear_in_progress = False

    async def _process_single_task(self, task: LoggingTask) -> None:
        """Process a single task and mark it done."""
        if self._queue is None:
            return

        try:
            await self._run_task_and_settle(task)
        finally:
            self._queue.task_done()

    async def _process_extracted_tasks(self, tasks: list[LoggingTask]) -> None:
        if not tasks or self._queue is None:
            return

        await asyncio.gather(*[self._process_single_task(task) for task in tasks])

    def ensure_initialized_and_enqueue(
        self, async_coroutine: Coroutine, *, token: "CompletionToken | None" = None
    ):
        """
        Ensure the logging worker is initialized and enqueue the coroutine.
        """
        self.start()
        self.enqueue(async_coroutine, token=token)

    async def stop(self) -> None:
        """Stop the logging worker and clean up resources."""
        if self._worker_task is None and self._running_tasks.is_empty():
            return

        self._running_tasks.cancel_all()
        if self._worker_task:
            self._worker_task.cancel()
            await asyncio.gather(self._worker_task, return_exceptions=True)
        await self._running_tasks.wait_settled()

        self._worker_task = None

    async def flush(self) -> None:
        """Flush the logging queue. Semantics unchanged from pre-Phase-1b:
        only waits for `queue.join()`, no admission control, no cancellation.
        `quiesce()` (Task 5) is the new, separate proxy-facing method."""
        if self._queue is None:
            return
        await self._queue.join()

    async def clear_queue(self):
        """
        Clear the queue with a maximum time limit. Still executes queued
        business coroutines (existing, pre-Phase-1b semantics; this method
        is NOT the quiesce path and quiesce() must never call it)."""
        if self._queue is None:
            return

        start_time = asyncio.get_event_loop().time()

        for _ in range(MAX_ITERATIONS_TO_CLEAR_QUEUE):
            if asyncio.get_event_loop().time() - start_time >= MAX_TIME_TO_CLEAR_QUEUE:
                verbose_logger.warning(f"clear_queue exceeded max_time of {MAX_TIME_TO_CLEAR_QUEUE}s, stopping early")
                break

            try:
                task = self._queue.get_nowait()
                try:
                    await self._run_task_and_settle(task)
                finally:
                    task = None
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break

    def _safe_log(self, level: str, message: str) -> None:
        if not hasattr(verbose_logger, "handlers") or not verbose_logger.handlers:
            return

        has_valid_handler = False
        for handler in verbose_logger.handlers:
            try:
                if hasattr(handler, "stream") and handler.stream and not handler.stream.closed:
                    has_valid_handler = True
                    break
                elif not hasattr(handler, "stream"):
                    has_valid_handler = True
                    break
            except (AttributeError, ValueError):
                continue

        if not has_valid_handler:
            return

        try:
            if level == "debug":
                verbose_logger.debug(message)
            elif level == "info":
                verbose_logger.info(message)
            elif level == "warning":
                verbose_logger.warning(message)
            elif level == "error":
                verbose_logger.error(message)
        except (ValueError, OSError, AttributeError):
            pass

    def _flush_on_exit(self):
        """
        Flush remaining events synchronously before process exit. Unchanged
        from pre-Phase-1b except for the `.coroutine` attribute access and
        settling any token (atexit is a last-resort path outside any
        quiesce/deadline machinery, so best-effort settle is all it does).
        """
        if self._queue is None:
            self._safe_log("debug", "[LoggingWorker] atexit: No queue initialized")
            return

        if self._queue.empty():
            self._safe_log("debug", "[LoggingWorker] atexit: Queue is empty")
            return

        queue_size = self._queue.qsize()
        self._safe_log("info", f"[LoggingWorker] atexit: Flushing {queue_size} remaining events...")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            processed = 0
            start_time = loop.time()

            previous_raise_exceptions = logging.raiseExceptions
            logging.raiseExceptions = False
            try:
                while not self._queue.empty() and processed < MAX_ITERATIONS_TO_CLEAR_QUEUE:
                    if loop.time() - start_time >= MAX_TIME_TO_CLEAR_QUEUE:
                        self._safe_log(
                            "warning",
                            f"[LoggingWorker] atexit: Reached time limit ({MAX_TIME_TO_CLEAR_QUEUE}s), stopping flush",
                        )
                        break

                    try:
                        task = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                    try:
                        loop.run_until_complete(task.coroutine)
                        _settle(task.token, NeutralOutcomeCompleted())
                        processed += 1
                    except Exception as e:
                        _settle(task.token, NeutralOutcomeFailed(error=str(e)))
                    finally:
                        task = None
            finally:
                logging.raiseExceptions = previous_raise_exceptions

            self._safe_log(
                "info",
                f"[LoggingWorker] atexit: Successfully flushed {processed} events!",
            )

        finally:
            loop.close()


GLOBAL_LOGGING_WORKER = LoggingWorker()
```

4. 确认转绿：`pytest tests/test_litellm/litellm_core_utils/test_logging_worker.py -v` 全绿（既有 416 行测试 + 本 Task 新增部分）。额外手动确认三处既有 `flush()` 调用方（`tests/batches_tests/test_openai_batches_and_files.py:55`、`tests/local_testing/test_tpm_rpm_routing_v2.py:562`、`tests/test_litellm/responses/test_no_duplicate_spend_logs.py:116`）不受影响：`pytest tests/batches_tests/test_openai_batches_and_files.py tests/local_testing/test_tpm_rpm_routing_v2.py tests/test_litellm/responses/test_no_duplicate_spend_logs.py -v`。

5. 提交：`git add litellm/litellm_core_utils/logging_worker.py litellm/litellm_core_utils/accounting_scope.py tests/test_litellm/litellm_core_utils/test_logging_worker.py && git commit -m "refactor: LoggingTask to frozen dataclass with settleable accounting token"`

---

## Task 4 — `AccountingOutcome` 完整 tagged union + `AccountingLease` + `ManagedTaskSupervisor`

依赖 Task 1（`ManagedTaskSet`）、Task 2（`CompletionToken`/`AccountingOutcomeLike`/`AccountingScope`）。

**范围调整说明（如实记录，非静默改动）**：原 8 点任务清单把"扩展 `AccountingSkippedDuringShutdown` 为完整 tagged union"列为独立的第 7 点、和"记账创建点接入"的第 6 点分开。读码后发现 `AccountingLease.settle()`（本 Task）必须把中立 `NeutralOutcome*`（Task 3）翻译成 proxy 侧的富类型才能对外暴露 `.outcome`，也就是说**完整 union 必须先于/随 `AccountingLease` 一起存在**，不能拖到后面单独的"接入"任务里才定义，否则 Task 4 自己都编译不过。因此把"扩展 tagged union"的定义并入本 Task，原第 8 点（Task 8）改为纯粹的"B2/B3 两个记账创建点接入 accounting 感知的任务派生机制"，不再重复定义类型。这是任务编号内部的先后顺序调整，不改变任何验收范围或 spec 覆盖面。（Task 8 落笔时进一步发现，具体机制应复用 Task 2 的 `spawn_detached`，而非本 Task 最初设想的专用 `spawn_child` 入口——详见下面"补充说明"与 Task 8 正文的"记录未采纳方案"。）

**本轮评审已定裁决（非本计划自行拍板，四条一并落到本 Task）**：

1. **blocker 1（拆分 admission 与 hard shutdown）**：此前 `close_root_admission()` 直接把 `_hard_shutdown` 置位，这个标志同时也是 `AccountingLease.is_valid()` 判断自己是否还有效的依据——等于"只封闭新 root 入口"这一步会连带让所有已发放、仍在存活期内的 lease 瞬间失效，违反 spec"仍允许已登记 accounting task 派生 child"这条要求。修复：拆成两个独立标志，`_root_admission_open`（`close_root_admission()` 只翻这一个）与 `_hard_shutdown`（只有 `drain()` 自己的 deadline/force-exit 分支才翻）；`acquire_root_lease()` 只看前者，`AccountingLease.is_valid()` 只看自身 `_settled` 与后者，永不看前者。
2. **blocker 2（scope 生命周期绑定 root 协程自身生命周期，而非首次 spawn）**：见上方"设计说明"的修订——`AccountingLease.spawn()` 不再在自己的 `finally` 里关闭 admission，只有 `settle()` 才关闭；同一条 lease 在 root 协程自己跑完之前可以先后派生任意有限次子任务。
3. **`DrainOutcome` 改为 spec 字面三变体**：见上方 Interfaces 一节。
4. **Phase 2 预留**：`AccountingLease` 新增一个不透明的 `correlation_token: object | None = None` 字段，Phase 1b 里永远是 `None`、不参与任何判断逻辑，只是提前把字段占位留好，避免 Phase 2 需要时再动一次这个已冻结的构造签名。

**major 6（drain() 必须等 telemetry 也 settle，且 `cancellation_failed` 要有真实统计口径）**：`drain()` 的 `Drained`/`DeadlineExceeded`/`ForcedExit` 三条返回路径，在 `self._telemetry.cancel_all()` 之后都必须 `await self._telemetry.wait_settled()`，不能取消完就直接返回、把"这些任务到底有没有真的响应取消"这件事丢给垃圾回收器；deadline 分支的 `cancelled`/`cancellation_failed` 改用 Task 1 新增的 `self._accounting.cancel_all_and_count_failures()` 产出真实统计（区分"干净响应了 cancel()"与"取消后仍返回正常值/抛出其他异常"），不再是此前 `len(self._accounting)`/`self._admissions_in_progress` 这种"取消完之后还剩多少个"的静态快照（那种写法从不反映取消本身是否成功）。

**Files**: `litellm/proxy/shutdown/accounting_outcome.py`（改，Phase 1a 产物，扩展）, `litellm/proxy/shutdown/managed_task_supervisor.py`（新）, `tests/test_litellm/proxy/shutdown/test_accounting_outcome.py`（新，先核实 Phase 1a 是否已建对应测试文件——已核实：Phase 1a 计划正文的 Task 6 只把断言写进了 `tests/test_litellm/proxy/db/test_spend_counter_reseed.py` 和 `tests/test_litellm/caching/test_redis_cache.py` 里，未新建独立的 `test_accounting_outcome.py`，所以这里新建是合理的，不是重复）, `tests/test_litellm/proxy/shutdown/test_managed_task_supervisor.py`（新）

**设计说明**：`admissions_in_progress` 这个计数器要有真实语义（不是"函数调用内自增自减、从未被其他协程观察到"的摆设），所以窗口定义为「`acquire_root_lease()` 拿到 lease」到「这个 lease 调用 `settle()`」之间——**已定裁决（本轮评审修订，blocker 2）**：窗口终点**只有** `settle()`，不再是"`spawn()` 或 `settle()`（两者取先）"。scope 的生命周期绑定的是它所属 root 协程自身的生命周期，而不是"第一次派生子任务"这个时间点：同一个 root 协程在自己彻底跑完之前，可以先后派生任意有限次数的子任务（比如同一个流式响应先后触发 `_batch_database_updates` 和 `update_cache` 两次记账工作），每一次都还应该被算作"这个 root 仍然存活、仍然可能再派生"，只有 root 自己的 `finally` 块调用 `settle()`（无论成功还是失败收尾）才是这段窗口的真正终点。这段窗口之间**没有** `await`点由本模块插入，但调用方（比如 `_client_async_logging_helper` 拿到 lease 后、真正 `enqueue()` 之前，或者派生了第一个子任务之后还要继续派生第二个之前）可能会先做一段自己的同步/异步工作，此时 `drain()` 的 `while True` 循环如果恰好在这个窗口被协作调度到，必须能看到"还有一个 admission 未关闭"而不是误判为已排空。

**Interfaces**

```python
# accounting_outcome.py（在 Phase 1a 已有 AccountingSkippedDuringShutdown 基础上追加）：
@dataclasses.dataclass(frozen=True, slots=True)
class AccountingCompleted:
    kind: Literal["completed"] = "completed"

@dataclasses.dataclass(frozen=True, slots=True)
class AccountingFailed:
    error: str
    kind: Literal["failed"] = "failed"

AccountingOutcome = AccountingCompleted | AccountingSkippedDuringShutdown | AccountingFailed

# managed_task_supervisor.py:
class AccountingLease:
    def is_valid(self) -> bool: ...
    def settle(self, outcome: AccountingOutcomeLike) -> None: ...
    def spawn(self, coro, *, name: str, kind: Literal["accounting", "telemetry"]) -> None: ...
    @property
    def outcome(self) -> AccountingOutcome | None: ...
    correlation_token: object | None  # Phase 2 reservation, always None in Phase 1b

@dataclasses.dataclass(frozen=True, slots=True)
class Drained: ...

@dataclasses.dataclass(frozen=True, slots=True)
class DeadlineExceeded:
    cancelled: int
    cancellation_failed: int

@dataclasses.dataclass(frozen=True, slots=True)
class ForcedExit:
    cancelled: int
    cancellation_failed: int

DrainOutcome = Drained | DeadlineExceeded | ForcedExit

class ManagedTaskSupervisor:
    def acquire_root_lease(self) -> AccountingLease | None: ...
    def spawn_telemetry(self, coro, *, name: str) -> None: ...
    def is_shutting_down_hard(self) -> bool: ...
    def close_root_admission(self) -> None: ...
    async def drain(
        self,
        deadline_remaining: Callable[[], float],
        root_queue_unfinished: Callable[[], int] = lambda: 0,
        is_force_exit: Callable[[], bool] = GracefulShutdownManager.is_force_exit,
    ) -> DrainOutcome: ...
```

**已定裁决（本轮评审，非本计划自行拍板）：`DrainOutcome` 是 spec 第 140/176 行字面要求的三变体 `Drained | DeadlineExceeded(cancelled, cancellation_failed) | ForcedExit`**，不是此前几轮撰写里一直维持的两变体（`remaining_accounting_tasks`/`remaining_admissions_in_progress` 字段、无独立 `ForcedExit`）。`drain()` 新增 `is_force_exit: Callable[[], bool]` 参数，默认绑定 `GracefulShutdownManager.is_force_exit`（Phase 1a 已有的无参 `@classmethod`，可以直接作为一个零参可调用对象传递，见该方法自身文档：`deadline_remaining()` 在 `_force_exit` 为真时本来就坍缩成 `0.0`，让 `while deadline_remaining() > 0` 型循环不用额外多穿一个 flag 就能同时应对两种触发原因——但这也意味着 `drain()`/`quiesce()` 自己无法只从 `deadline_remaining()` 的返回值反推出"是强制退出、还是单纯到期"，必须显式再问一次 `is_force_exit()`）；到达 deadline 分支时判断 `is_force_exit()`：为真则产出 `ForcedExit`，为假则产出 `DeadlineExceeded`——两者字段形状相同（`cancelled`/`cancellation_failed`），只是变体标签不同，供调用方（Task 10）程序化区分，不再依赖日志文本。`cancelled`/`cancellation_failed` 由新增的 `ManagedTaskSet.cancel_all_and_count_failures()`（Task 1）产出，取代此前的 `remaining_accounting_tasks`/`remaining_admissions_in_progress`（那两个字段本来就只是"取消之后 `len()`/计数器还剩多少"的静态快照，从未真正反映"取消是否成功"）。

**Steps**

1. 写失败测试。

`tests/test_litellm/proxy/shutdown/test_accounting_outcome.py`（新）：

```python
"""AccountingOutcome tagged union: AccountingCompleted | AccountingSkippedDuringShutdown | AccountingFailed."""

from litellm.proxy.shutdown.accounting_outcome import (
    AccountingCompleted,
    AccountingFailed,
    AccountingSkippedDuringShutdown,
)


class TestAccountingOutcome:
    def test_completed_has_matching_kind(self):
        assert AccountingCompleted().kind == "completed"

    def test_failed_carries_error_and_matching_kind(self):
        outcome = AccountingFailed(error="boom")
        assert outcome.kind == "failed"
        assert outcome.error == "boom"

    def test_skipped_during_shutdown_unchanged_from_phase_1a(self):
        # Regression guard: Phase 1a's shape (reason + literal kind) must
        # not have shifted while expanding the union around it.
        outcome = AccountingSkippedDuringShutdown(reason="deadline_exceeded")
        assert outcome.kind == "skipped_during_shutdown"
        assert outcome.reason == "deadline_exceeded"

    def test_all_three_variants_are_frozen(self):
        outcome = AccountingCompleted()
        try:
            outcome.kind = "failed"  # type: ignore[misc]
        except Exception:
            pass
        else:
            raise AssertionError("AccountingCompleted must be frozen")
```

`tests/test_litellm/proxy/shutdown/test_managed_task_supervisor.py`（新）：

```python
"""ManagedTaskSupervisor + AccountingLease: process-scoped accounting/telemetry
child-task tracking with a joint fixed-point drain."""

import asyncio

import pytest

from litellm.proxy.shutdown.managed_task_supervisor import (
    DeadlineExceeded,
    Drained,
    ForcedExit,
    ManagedTaskSupervisor,
)


class TestAccountingLease:
    def test_settle_is_idempotent_and_keeps_first_outcome(self):
        supervisor = ManagedTaskSupervisor()
        lease = supervisor.acquire_root_lease()
        assert lease is not None

        class _Outcome:
            kind = "completed"

        class _SecondOutcome:
            kind = "failed"
            error = "ignored"

        lease.settle(_Outcome())
        lease.settle(_SecondOutcome())  # must be a no-op

        assert lease.outcome is not None
        assert lease.outcome.kind == "completed"

    def test_acquire_root_lease_returns_none_once_hard_shutdown(self):
        supervisor = ManagedTaskSupervisor()
        supervisor._hard_shutdown = True  # simulate post-deadline state
        assert supervisor.acquire_root_lease() is None

    def test_admissions_in_progress_closes_on_settle_without_ever_spawning(self):
        supervisor = ManagedTaskSupervisor()
        lease = supervisor.acquire_root_lease()
        assert supervisor._admissions_in_progress == 1

        class _Outcome:
            kind = "completed"

        lease.settle(_Outcome())
        assert supervisor._admissions_in_progress == 0

    def test_admissions_in_progress_stays_open_across_any_number_of_spawns_until_settle(self):
        """blocker 2 regression: scope lifetime is bound to the owning root
        coroutine's own lifetime, not to the first spawn() call -- the same
        lease must be able to spawn multiple children (e.g. a root that
        triggers both _batch_database_updates and update_cache) before its
        owner finally calls settle()."""
        supervisor = ManagedTaskSupervisor()
        lease = supervisor.acquire_root_lease()

        async def coro():
            pass

        lease.spawn(coro(), name="_batch_database_updates", kind="accounting")
        assert supervisor._admissions_in_progress == 1  # still open after first spawn

        lease.spawn(coro(), name="update_cache", kind="accounting")
        assert supervisor._admissions_in_progress == 1  # still open after second spawn

        class _Outcome:
            kind = "completed"

        lease.settle(_Outcome())
        assert supervisor._admissions_in_progress == 0  # only settle() closes it

    def test_correlation_token_defaults_to_none_and_is_not_interpreted(self):
        """Phase 2 reservation: the field exists so Phase 2 does not need to
        touch this already-frozen constructor signature again, but Phase 1b
        never assigns it a non-None value nor branches on it."""
        supervisor = ManagedTaskSupervisor()
        lease = supervisor.acquire_root_lease()
        assert lease is not None
        assert lease.correlation_token is None


class TestCloseRootAdmissionVsHardShutdown:
    """blocker 1 regression: closing root admission and declaring a hard
    shutdown are two independent state transitions -- the former must never
    invalidate leases that were already issued before it ran."""

    def test_close_root_admission_refuses_new_root_leases(self):
        supervisor = ManagedTaskSupervisor()
        supervisor.close_root_admission()
        assert supervisor.acquire_root_lease() is None

    def test_close_root_admission_leaves_existing_lease_valid_and_able_to_spawn_a_child(self):
        supervisor = ManagedTaskSupervisor()
        lease = supervisor.acquire_root_lease()
        assert lease is not None

        supervisor.close_root_admission()

        assert lease.is_valid()

        async def coro():
            pass

        lease.spawn(coro(), name="child_after_root_closed", kind="accounting")
        assert not supervisor._accounting.is_empty()  # the spawn actually went through

    def test_hard_shutdown_invalidates_existing_lease_and_refuses_further_spawns(self):
        supervisor = ManagedTaskSupervisor()
        lease = supervisor.acquire_root_lease()
        assert lease is not None

        supervisor._hard_shutdown = True  # simulate drain()'s deadline branch

        assert not lease.is_valid()

        ran = []

        async def coro():
            ran.append("ran")

        lease.spawn(coro(), name="refused", kind="accounting")
        assert supervisor._accounting.is_empty()  # refused: coroutine closed, never scheduled


class TestSpawnChildAndTelemetry:
    @pytest.mark.asyncio
    async def test_lease_spawn_with_accounting_kind_tracks_and_self_removes(self):
        supervisor = ManagedTaskSupervisor()
        lease = supervisor.acquire_root_lease()
        done = asyncio.Event()

        async def coro():
            done.set()

        lease.spawn(coro(), name="update_cache", kind="accounting")
        await asyncio.wait_for(done.wait(), timeout=1)
        await asyncio.sleep(0)  # let the done-callback run
        assert supervisor._accounting.is_empty()

    @pytest.mark.asyncio
    async def test_spawn_telemetry_does_not_require_a_lease(self):
        supervisor = ManagedTaskSupervisor()
        done = asyncio.Event()

        async def coro():
            done.set()

        supervisor.spawn_telemetry(coro(), name="budget_alerts")
        await asyncio.wait_for(done.wait(), timeout=1)

    @pytest.mark.asyncio
    async def test_task_creation_failure_closes_coroutine_but_leaves_admission_open_for_caller_to_settle(
        self, monkeypatch
    ):
        """blocker 2 regression: admission bookkeeping is exclusively
        settle()'s job now -- a failed spawn() attempt (e.g. the event loop
        rejects task creation) must not silently roll back admission either;
        the owning root coroutine is still expected to reach its own
        `finally` and call settle() itself, regardless of how many
        intermediate spawn() attempts succeeded or failed."""
        supervisor = ManagedTaskSupervisor()
        lease = supervisor.acquire_root_lease()
        closed = []

        class _Coro:
            def close(self):
                closed.append(True)

            def __await__(self):
                raise AssertionError("must never actually run")
                yield

        def _raise(*args, **kwargs):
            raise RuntimeError("event loop is closed")

        monkeypatch.setattr(asyncio, "create_task", _raise)

        with pytest.raises(RuntimeError):
            lease.spawn(_Coro(), name="x", kind="accounting")

        assert closed == [True]
        assert supervisor._admissions_in_progress == 1  # unaffected -- only settle() closes it


class TestDrain:
    @pytest.mark.asyncio
    async def test_drain_returns_drained_immediately_when_nothing_pending(self):
        supervisor = ManagedTaskSupervisor()
        outcome = await supervisor.drain(deadline_remaining=lambda: 5.0)
        assert isinstance(outcome, Drained)

    @pytest.mark.asyncio
    async def test_drain_waits_for_accounting_child_to_finish_before_reporting_drained(self):
        supervisor = ManagedTaskSupervisor()
        lease = supervisor.acquire_root_lease()
        finished = []

        async def coro():
            await asyncio.sleep(0.02)
            finished.append("x")

        lease.spawn(coro(), name="update_cache", kind="accounting")
        outcome = await supervisor.drain(deadline_remaining=lambda: 5.0)

        assert isinstance(outcome, Drained)
        assert finished == ["x"]

    @pytest.mark.asyncio
    async def test_drain_reports_deadline_exceeded_with_clean_cancellation_count(self):
        supervisor = ManagedTaskSupervisor()
        lease = supervisor.acquire_root_lease()
        cancelled_event = asyncio.Event()

        async def coro():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled_event.set()
                raise

        lease.spawn(coro(), name="update_cache", kind="accounting")

        outcome = await supervisor.drain(deadline_remaining=lambda: -1.0, is_force_exit=lambda: False)

        assert outcome == DeadlineExceeded(cancelled=1, cancellation_failed=0)
        assert cancelled_event.is_set()

    @pytest.mark.asyncio
    async def test_drain_reports_forced_exit_instead_of_deadline_exceeded_when_is_force_exit_true(self):
        """已定裁决（三变体）：到达 deadline 时是 DeadlineExceeded 还是 ForcedExit 完全由
        注入的 is_force_exit() 决定，drain() 自己不重新猜测触发原因。"""
        supervisor = ManagedTaskSupervisor()
        lease = supervisor.acquire_root_lease()

        async def coro():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise

        lease.spawn(coro(), name="update_cache", kind="accounting")

        outcome = await supervisor.drain(deadline_remaining=lambda: -1.0, is_force_exit=lambda: True)

        assert outcome == ForcedExit(cancelled=1, cancellation_failed=0)

    @pytest.mark.asyncio
    async def test_drain_deadline_exceeded_counts_a_suppressed_cancellation_as_cancellation_failed(self):
        supervisor = ManagedTaskSupervisor()
        lease = supervisor.acquire_root_lease()

        async def swallow_cancellation():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return "ignored on purpose"

        lease.spawn(swallow_cancellation(), name="update_cache", kind="accounting")

        outcome = await supervisor.drain(deadline_remaining=lambda: -1.0, is_force_exit=lambda: False)

        assert outcome == DeadlineExceeded(cancelled=0, cancellation_failed=1)

    @pytest.mark.asyncio
    async def test_drain_awaits_telemetry_settlement_before_returning_on_the_happy_path(self):
        """major 6: even the non-deadline Drained path must not return while
        a cancelled telemetry task is still mid-cancellation -- it must be
        observably settled by the time drain() itself returns, not merely
        "cancel() was called at some point"."""
        supervisor = ManagedTaskSupervisor()
        telemetry_cancelled = asyncio.Event()

        async def telemetry_coro():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                telemetry_cancelled.set()
                raise

        supervisor.spawn_telemetry(telemetry_coro(), name="budget_alerts")
        await asyncio.sleep(0)  # let it start

        outcome = await supervisor.drain(deadline_remaining=lambda: 5.0)

        assert isinstance(outcome, Drained)
        assert telemetry_cancelled.is_set()  # already true -- no extra sleep needed after drain() returns
        assert supervisor._telemetry.is_empty()

    @pytest.mark.asyncio
    async def test_drain_awaits_telemetry_settlement_before_returning_on_the_deadline_path(self):
        supervisor = ManagedTaskSupervisor()
        lease = supervisor.acquire_root_lease()
        telemetry_cancelled = asyncio.Event()

        async def accounting_coro():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise

        async def telemetry_coro():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                telemetry_cancelled.set()
                raise

        lease.spawn(accounting_coro(), name="update_cache", kind="accounting")
        supervisor.spawn_telemetry(telemetry_coro(), name="budget_alerts")
        await asyncio.sleep(0)

        outcome = await supervisor.drain(deadline_remaining=lambda: -1.0, is_force_exit=lambda: False)

        assert isinstance(outcome, DeadlineExceeded)
        assert telemetry_cancelled.is_set()
        assert supervisor._telemetry.is_empty()

    @pytest.mark.asyncio
    async def test_drain_honors_injected_root_queue_unfinished_before_reporting_drained(self):
        """Joint fixed-point guard: even with zero accounting children,
        drain() must not report Drained() while the caller-supplied
        root_queue_unfinished() still reports outstanding LoggingWorker
        items (e.g. a re-entrant enqueue triggered by a just-finished
        accounting child)."""
        supervisor = ManagedTaskSupervisor()
        calls = {"n": 0}

        def root_queue_unfinished():
            calls["n"] += 1
            return 0 if calls["n"] > 3 else 1

        outcome = await supervisor.drain(
            deadline_remaining=lambda: 5.0, root_queue_unfinished=root_queue_unfinished
        )
        assert isinstance(outcome, Drained)
        assert calls["n"] > 3
```

2. 确认失败：`pytest tests/test_litellm/proxy/shutdown/test_accounting_outcome.py tests/test_litellm/proxy/shutdown/test_managed_task_supervisor.py -v`——`ModuleNotFoundError`/`ImportError: cannot import name 'AccountingCompleted'`。

3. 实现。

先扩展 `litellm/proxy/shutdown/accounting_outcome.py`（在 Phase 1a 已有的 `AccountingSkippedDuringShutdown` 定义后追加，不改动那个既有 dataclass 本身）：

```python
@dataclasses.dataclass(frozen=True, slots=True)
class AccountingCompleted:
    """The accounting unit ran to completion normally."""

    kind: Literal["completed"] = "completed"


@dataclasses.dataclass(frozen=True, slots=True)
class AccountingFailed:
    """The accounting unit raised before completing."""

    error: str
    kind: Literal["failed"] = "failed"


AccountingOutcome = AccountingCompleted | AccountingSkippedDuringShutdown | AccountingFailed
```

（`Literal`/`dataclasses` 已经是 Phase 1a 该文件顶部既有 import，不重复添加。）

新建 `litellm/proxy/shutdown/managed_task_supervisor.py`：

```python
"""
ManagedTaskSupervisor: process-scoped tracking of detached accounting and
telemetry child tasks, with a joint fixed-point drain.

An AccountingLease is the unforgeable capability object admitted at a Path
A/B *root* creation boundary (one lease per root unit of accounting work,
never per awaited nested call -- see spec "work-lease 配平"). It implements
both CompletionToken (settle) and AccountingScope (is_valid/spawn), since
"can this unit itself admit further children" and "how did this unit
finish" are two facets of the same object.
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
from typing import Callable, Coroutine, Literal, assert_never

from litellm.litellm_core_utils.accounting_scope import (
    AccountingOutcomeLike,
    current_accounting_scope,
)
from litellm.litellm_core_utils.managed_task_set import ManagedTaskSet
from litellm.proxy.shutdown.accounting_outcome import (
    AccountingCompleted,
    AccountingFailed,
    AccountingOutcome,
    AccountingSkippedDuringShutdown,
)
from litellm.proxy.shutdown.graceful_shutdown_manager import GracefulShutdownManager

# Bounded poll interval for the fixed-point re-check in drain()/quiesce()
# (Task 5 reuses this same constant). A bare `await asyncio.sleep(0)` would
# only yield once per loop tick without ever actually waiting, turning the
# fixed-point re-check into a CPU-pegging busy loop for however long the
# real accounting work (DB writes, etc.) takes to finish. 50ms is short
# enough to stay responsive against typical multi-second shutdown deadlines
# while keeping CPU usage negligible; a fully event-driven wake-up (e.g. an
# asyncio.Event flipped by every ManagedTaskSet mutation) was considered and
# rejected as needless complexity for a drain path that runs at most once
# per process lifetime -- see plan's Architecture section "未采纳方案".
_DRAIN_POLL_INTERVAL_SECONDS = 0.05


def _translate(outcome: AccountingOutcomeLike) -> AccountingOutcome:
    match outcome.kind:
        case "completed":
            return AccountingCompleted()
        case "failed":
            return AccountingFailed(error=getattr(outcome, "error", ""))
        case "skipped_during_shutdown":
            return AccountingSkippedDuringShutdown(reason=getattr(outcome, "reason", "unknown"))
        case _:
            raise ValueError(f"unrecognized AccountingOutcomeLike.kind: {outcome.kind!r}")


class AccountingLease:
    """已定裁决（blocker 1/2，本轮评审）：admission 只在 `settle()` 上关闭，`spawn()`
    自身从不关闭——scope 的生命周期绑定的是它所属 root 协程自身的生命周期，而不是"第一次
    派生子任务"这个时间点，允许同一条 lease 在存活期内先后派生任意有限次子任务。"""

    __slots__ = ("_supervisor", "_settled", "_outcome", "correlation_token")

    def __init__(self, supervisor: "ManagedTaskSupervisor", *, correlation_token: object | None = None) -> None:
        self._supervisor = supervisor
        self._settled = False
        self._outcome: AccountingOutcome | None = None
        # Phase 2 预留字段：Phase 1b 里永远是 None，不参与任何判断逻辑，只是提前把构造
        # 签名占位留好，避免 Phase 2 需要时再动一次这个已冻结的签名。
        self.correlation_token = correlation_token
        supervisor._admissions_in_progress += 1

    def is_valid(self) -> bool:
        """只看自身是否已 settle、以及 supervisor 是否已进入 hard shutdown——**从不**看
        root admission 是否已经关闭（blocker 1）：`close_root_admission()` 只拒绝*新*
        lease，不使已发放的 lease 失效。"""
        return not self._settled and not self._supervisor.is_shutting_down_hard()

    def settle(self, outcome: AccountingOutcomeLike) -> None:
        """Idempotent, per spec: only the first call wins. This is the ONLY
        place admission closes (blocker 2) -- `spawn()` never closes it."""
        if self._settled:
            return
        self._settled = True
        self._outcome = _translate(outcome)
        self._supervisor._admissions_in_progress -= 1

    def spawn(
        self,
        coro: "Coroutine[object, object, object]",
        *,
        name: str,
        kind: Literal["accounting", "telemetry"],
    ) -> None:
        if not self.is_valid():
            # Refuse silently rather than raise: an invalid lease (already
            # settled, or the supervisor has since declared a hard shutdown)
            # means the caller's own root coroutine is (or should be) already
            # winding down -- closing the coroutine here is the same "don't
            # leak an unawaited coroutine" contract every other rejection
            # path in this plan follows (LoggingWorker.enqueue(), etc.).
            coro.close()
            return
        if kind == "accounting":
            self._supervisor._spawn_accounting_child(coro, name=name, scope=self)
        elif kind == "telemetry":
            self._supervisor._spawn_telemetry_child(coro, name=name)
        else:
            assert_never(kind)

    @property
    def outcome(self) -> "AccountingOutcome | None":
        return self._outcome


@dataclasses.dataclass(frozen=True, slots=True)
class Drained:
    pass


@dataclasses.dataclass(frozen=True, slots=True)
class DeadlineExceeded:
    cancelled: int
    cancellation_failed: int


@dataclasses.dataclass(frozen=True, slots=True)
class ForcedExit:
    cancelled: int
    cancellation_failed: int


DrainOutcome = Drained | DeadlineExceeded | ForcedExit


class ManagedTaskSupervisor:
    """One instance per process, constructed once at proxy startup (Task 10
    registers `self.acquire_root_lease` as the accounting_scope root-scope
    provider at lifespan startup, and revokes that registration at lifespan
    shutdown once `drain()` has fully returned -- major 9, see Architecture
    section)."""

    def __init__(self) -> None:
        self._accounting = ManagedTaskSet()
        self._telemetry = ManagedTaskSet()
        self._admissions_in_progress = 0
        self._root_admission_open = True
        self._hard_shutdown = False

    def is_shutting_down_hard(self) -> bool:
        return self._hard_shutdown

    def close_root_admission(self) -> None:
        """Eagerly refuse any *new* root-level `acquire_root_lease()` call
        from this point on, without touching already-admitted accounting
        children or the telemetry set -- Task 10's lifespan wiring calls
        this at quiesce step 3 (spec section C), strictly before
        `LoggingWorker.quiesce()` (step 4) and `drain()` (step 5) run, so a
        root scope cannot sneak in during the window those two steps are
        still polling. **blocker 1 修订**：只翻 `_root_admission_open` 这一个
        独立标志，绝不触碰 `_hard_shutdown`——已发放、仍在存活期内的 lease 完全
        不受影响（`AccountingLease.is_valid()` 从不检查 `_root_admission_open`），
        也不会由此触发任何取消：取消仍然唯一由 `drain()` 自己的 deadline/force-exit
        分支负责。"""
        self._root_admission_open = False

    def acquire_root_lease(self) -> "AccountingLease | None":
        """Root-boundary lease acquisition. Returns None once root admission
        has been explicitly closed (`close_root_admission()`, quiesce step 3)
        **or** a hard shutdown has since been declared (`drain()`'s deadline
        branch) -- the latter check is a defensive belt-and-suspenders
        addition: in the real quiesce sequence `close_root_admission()`
        always runs strictly before `drain()` can ever flip `_hard_shutdown`
        (step 3 precedes step 5), so this branch is unreachable in
        production, but it keeps this method from ever handing out a
        lease that would be born already-invalid via
        `AccountingLease.is_valid()`'s own hard-shutdown check."""
        if not self._root_admission_open or self._hard_shutdown:
            return None
        return AccountingLease(self)

    def spawn_telemetry(self, coro: "Coroutine[object, object, object]", *, name: str) -> None:
        """Root-level, lease-less telemetry admission (T1-T14): cancellable
        outright at deadline, never counted toward the drain fixed point."""
        self._spawn_telemetry_child(coro, name=name)

    def _spawn_accounting_child(
        self,
        coro: "Coroutine[object, object, object]",
        *,
        name: str,
        scope: "AccountingLease",
    ) -> None:
        """Binds `current_accounting_scope` to `scope` inside a private copy
        of the calling context before creating the task, so nested Path B
        call sites reached from within `coro`'s own call stack (Task 8's
        update_cache / _batch_database_updates, several plain `await`s deep
        inside dispatch_success_handlers) can find this same lease via
        `accounting_scope.current_accounting_scope.get()`. Using a *copy* of
        the context (rather than `.set()` on the ambient one) is load-bearing:
        a bare `.set()` here would permanently leak `scope` into whatever
        code runs in the *caller's* own context after this method returns
        (e.g. the next chunk's iteration in CustomStreamWrapper.__anext__,
        which calls `spawn_detached` -> here once per completed stream)."""
        ctx = contextvars.copy_context()

        def _bind_and_create() -> "asyncio.Task[object]":
            current_accounting_scope.set(scope)
            return asyncio.create_task(coro, name=name)

        try:
            task = ctx.run(_bind_and_create)
        except Exception:
            coro.close()
            raise
        self._accounting.add(task)

    def _spawn_telemetry_child(self, coro: "Coroutine[object, object, object]", *, name: str) -> None:
        try:
            task = asyncio.create_task(coro, name=name)
        except Exception:
            coro.close()
            raise
        self._telemetry.add(task)

    async def drain(
        self,
        deadline_remaining: Callable[[], float],
        root_queue_unfinished: Callable[[], int] = lambda: 0,
        is_force_exit: Callable[[], bool] = GracefulShutdownManager.is_force_exit,
    ) -> DrainOutcome:
        """Joint fixed-point over (root_queue_unfinished, accounting_tasks,
        admissions_in_progress). `root_queue_unfinished` defaults to a
        constant 0 for standalone use/tests; Task 10's lifespan wiring
        passes `lambda: GLOBAL_LOGGING_WORKER.queue_size()` so a rare
        re-entrant enqueue during drain (an accounting child that itself
        calls back into GLOBAL_LOGGING_WORKER) is not missed -- LoggingWorker
        itself has already fully quiesced its own backlog before drain() is
        ever invoked (strict quiesce-then-drain ordering), so this guard
        only ever matters for genuinely re-entrant work created during
        drain() itself. `is_force_exit` defaults to
        `GracefulShutdownManager.is_force_exit` (an unbound classmethod
        reference, valid as a zero-arg callable); injectable for tests so
        this module never has to reach into process-global state directly.
        已定裁决（三变体）：`deadline_remaining() <= 0` 本身无法分辨"自然到期"还是
        "第二次 SIGINT 强制退出"（`GracefulShutdownManager.deadline_remaining()`
        在 `_force_exit` 为真时本就坍缩成 0.0），所以到达 deadline 时必须显式再问一次
        `is_force_exit()` 才能决定产出 `DeadlineExceeded` 还是 `ForcedExit`——drain()
        自己绝不重新猜测触发原因。"""
        while True:
            if root_queue_unfinished() == 0 and self._accounting.is_empty() and self._admissions_in_progress == 0:
                self._telemetry.cancel_all()
                await self._telemetry.wait_settled()
                return Drained()

            if deadline_remaining() <= 0:
                self._hard_shutdown = True
                cancelled, cancellation_failed = await self._accounting.cancel_all_and_count_failures()
                self._telemetry.cancel_all()
                await self._telemetry.wait_settled()
                if is_force_exit():
                    return ForcedExit(cancelled=cancelled, cancellation_failed=cancellation_failed)
                return DeadlineExceeded(cancelled=cancelled, cancellation_failed=cancellation_failed)

            await asyncio.sleep(min(_DRAIN_POLL_INTERVAL_SECONDS, max(deadline_remaining(), 0.0)))


GLOBAL_MANAGED_TASK_SUPERVISOR = ManagedTaskSupervisor()

# Module-import-time creation of the process-scoped singleton -- same idiom as
# this codebase's existing `GLOBAL_LOGGING_WORKER = LoggingWorker()` singleton
# (logging_worker.py). The *registration* of this singleton as the root-scope
# provider is deliberately NOT done here (major 9: a permanent import-time
# registration can never be revoked, leaking a reference to a torn-down
# supervisor into any later pure-SDK call in the same process) -- Task 10's
# lifespan startup calls `accounting_scope.register_root_scope_provider(
# GLOBAL_MANAGED_TASK_SUPERVISOR.acquire_root_lease)` explicitly, and lifespan
# shutdown calls `accounting_scope.register_root_scope_provider(None)` once
# `drain()` has fully returned. Pure-SDK code that never imports anything
# under litellm.proxy.* never triggers either call, so `_root_scope_provider`
# stays None there and every Path B call site's fallback-to-bare-create_task
# behavior is unaffected.
```

**补充说明（Task 8 落笔时回填，如实记录）**：撰写 Task 8（B2/B3 接入）时读码复核 Task 4 已落盘的实现，发现两处真实缺口，均已在上面的 Steps §3 代码里直接改正（不是另开一个"Task 4.5"，因为这两处都是 Task 4 自身该交付、但当时遗漏的部分，补丁范围完全落在 `managed_task_supervisor.py` 内部）：

1. **缺口一——从未创建全局单例**：Architecture 一节（本文档第 68 行）承诺"`register_root_scope_provider(provider)` ... 在 `managed_task_supervisor.py` 模块导入时创建单例"，但 Task 4 最初落盘的 Steps §3 代码里从未出现 `GLOBAL_MANAGED_TASK_SUPERVISOR` 字样（用 `grep` 核实过，零匹配）——也就是说 Task 6 的六个 Path A 站点、Task 7 的 B1 站点，实际运行时 `acquire_root_scope()`/`spawn_detached()` 永远只会看到 `_root_scope_provider is None`，永远退化成纯-SDK 回退路径，"记账域感知"从未真正生效过——这不是"暂时用不上、可以延后"的问题，而是让 Task 6/7 已落盘的全部测试断言其实只覆盖了纯-SDK 分支、从未覆盖过真正接入 supervisor 之后的路径。现已在 Steps §3 补上模块级 `GLOBAL_MANAGED_TASK_SUPERVISOR = ManagedTaskSupervisor()` 单例（**本轮评审再修订（major 9）**：把实际的 `register_root_scope_provider(...)` 注册调用挪到 Task 10 的 lifespan 启动阶段执行，本模块只创建单例，不在导入时自行注册——理由见 Architecture 一节的更新与 Task 10 正文）。
2. **缺口二——`_spawn_accounting_child` 从未把 `current_accounting_scope` 绑定进新建的子任务**：这一处更隐蔽也更严重——`AccountingLease.spawn()` 原先直接调用 `self._supervisor._spawn_accounting_child(coro, name=name)`，而 `_spawn_accounting_child` 原先只是裸 `asyncio.create_task(coro, name=name)`，从未在新任务的 Context 里 `current_accounting_scope.set(...)`。这意味着即便缺口一被修好，Task 7（B1，`spawn_detached` 在 `streaming_handler.py:2053` 获取一个全新 root scope 并 `scope.spawn(...)`）之后，`dispatch_success_handlers` 在这个新任务里跑起来、层层 `await` 调用到 Task 8 的 `update_cache`/`_batch_database_updates` 时，`current_accounting_scope.get()` 拿到的仍然是 `None`——因为 `asyncio.create_task` 隐式拷贝的是*调用 `_spawn_accounting_child` 那一刻*的 Context，而那一刻从未写入过这个 scope。Task 8 的整个设计前提（B2/B3 靠 `current_accounting_scope.get()` 拿到 Task 7 acquire 到的同一个 lease）如果不修这里就完全不成立。现已在 Steps §3 把 `_spawn_accounting_child` 改成接收 `scope: AccountingLease` 参数，用 `contextvars.copy_context()` 取一份*私有*副本、在副本里 `current_accounting_scope.set(scope)` 后再 `ctx.run(...)` 创建任务——用副本而不是直接对当前 Context `.set()`，是为了不把这个 scope 泄漏进调用方自己后续的代码（比如 `CustomStreamWrapper.__anext__` 处理下一个 chunk 时，绝不应该还残留着上一个 chunk 的 scope）。这个修法与 Task 2 里 `create_task_with_scope` 的既有设计同构（那里调用方负责 `context.run(create_task_with_scope, coro, token=token)`；这里由于 `_spawn_accounting_child` 不是被外部captured Context 调用，所以自己内部做一次 `copy_context()`）。

两处缺口都通过下面新增的 `TestGlobalSingletonAndScopePropagation` 测试类锁定（既验证 `GLOBAL_MANAGED_TASK_SUPERVISOR` 单例确实存在、其 `acquire_root_lease` 可以被 `register_root_scope_provider` 接受并如实产出真实 lease——**本轮评审再修订（major 9）**：不再断言"模块导入本身就完成了注册"，因为注册调用已挪到 Task 10 的 lifespan 启动阶段，本测试改为像 Task 10 lifespan 代码那样显式调用 `register_root_scope_provider(...)`、并在 `finally` 里撤销注册，避免污染同一 pytest 进程里的其他测试模块；lifespan 启动/关停两端的真实接线由 Task 10 自己的测试端到端锁定——，也验证被 spawn 的子任务内部能看到同一个 scope、且不泄漏进调用方自己的 Context）；Task 4 原有的 5 个测试类无需改动，因为它们从不检查 `current_accounting_scope`，行为断言（settle/admission 计数/drain 语义）不受影响。

```python
# tests/test_litellm/proxy/shutdown/test_managed_task_supervisor.py（追加）

import asyncio

import pytest

from litellm.litellm_core_utils import accounting_scope
from litellm.litellm_core_utils.accounting_scope import current_accounting_scope
from litellm.proxy.shutdown.managed_task_supervisor import (
    GLOBAL_MANAGED_TASK_SUPERVISOR,
    ManagedTaskSupervisor,
)


class TestGlobalSingletonAndScopePropagation:
    def test_global_supervisor_singleton_acquire_root_lease_is_a_valid_provider(self):
        # GLOBAL_MANAGED_TASK_SUPERVISOR is a process-wide singleton created at
        # module import time; only *registering* it as accounting_scope's DI
        # hook happens later, at Task 10's lifespan startup (major 9: a
        # revocable registration, not a permanent import-time side effect).
        # This test exercises that exact registration call directly (the same
        # call Task 10 makes), then tears it down, so it never leaks into
        # any other test module running in the same pytest process.
        accounting_scope.register_root_scope_provider(GLOBAL_MANAGED_TASK_SUPERVISOR.acquire_root_lease)
        try:
            token = accounting_scope.acquire_root_scope()
            assert token is not None
            assert isinstance(token, type(GLOBAL_MANAGED_TASK_SUPERVISOR.acquire_root_lease()))
        finally:
            accounting_scope.register_root_scope_provider(None)

    def test_unregistering_leaves_no_reference_to_the_supervisor(self):
        """major 9's core guarantee: once unregistered, acquire_root_scope()
        must fall back to None -- not silently return a lease from a
        supervisor that the caller believes is no longer live."""
        accounting_scope.register_root_scope_provider(GLOBAL_MANAGED_TASK_SUPERVISOR.acquire_root_lease)
        accounting_scope.register_root_scope_provider(None)

        assert accounting_scope.acquire_root_scope() is None

    @pytest.mark.asyncio
    async def test_lease_spawn_propagates_scope_to_nested_call_site_without_leaking_to_caller(self):
        supervisor = ManagedTaskSupervisor()
        lease = supervisor.acquire_root_lease()
        seen_scope_inside_child = []
        done = asyncio.Event()

        async def child():
            seen_scope_inside_child.append(current_accounting_scope.get())
            done.set()

        assert current_accounting_scope.get() is None
        lease.spawn(child(), name="update_cache", kind="accounting")
        # lease.spawn must not leak the scope into the caller's own Context
        assert current_accounting_scope.get() is None

        await asyncio.wait_for(done.wait(), timeout=1)
        assert seen_scope_inside_child == [lease]
```

`pytest tests/test_litellm/proxy/shutdown/test_accounting_outcome.py tests/test_litellm/proxy/shutdown/test_managed_task_supervisor.py -v` 全绿。同时跑一次 Phase 1a 已有的 `tests/test_litellm/proxy/db/test_spend_counter_reseed.py tests/test_litellm/caching/test_redis_cache.py -v` 确认扩展 `accounting_outcome.py` 没有破坏 Phase 1a 对 `AccountingSkippedDuringShutdown` 的既有断言。

（原先此处有一段"补充说明（Task 10 落笔时发现）"及配套的 `TestCloseRootAdmission` 测试类，描述"`close_root_admission()` 复用同一个 `_hard_shutdown` 标志"——那是本轮评审之前的旧设计。blocker 1 已经把 `_root_admission_open`/`_hard_shutdown` 拆成两个独立标志，`close_root_admission()` 只翻前者，对应的行为边界已经由上面 Steps §1 的 `TestCloseRootAdmissionVsHardShutdown`（`test_close_root_admission_refuses_new_root_leases`/`test_close_root_admission_leaves_existing_lease_valid_and_able_to_spawn_a_child`/`test_hard_shutdown_invalidates_existing_lease_and_refuses_further_spawns`）完整覆盖，旧的说明段落与重复的测试类已删除，避免两套测试类各自维护同一行为的边界条件。）

5. 提交：`git add litellm/proxy/shutdown/accounting_outcome.py litellm/proxy/shutdown/managed_task_supervisor.py tests/test_litellm/proxy/shutdown/test_accounting_outcome.py tests/test_litellm/proxy/shutdown/test_managed_task_supervisor.py && git commit -m "feat: add ManagedTaskSupervisor, AccountingLease, and full AccountingOutcome union"`

---

## Task 5 — `LoggingWorker.quiesce(deadline_remaining, admission_policy) -> LoggingDrainOutcome`

依赖 Task 3（`LoggingTask.token`/`_settle`/`NeutralOutcome*`/`_drain_and_settle_dropped` 已返回 drained 计数）、Task 1（`ManagedTaskSet`，Task 3 已把 `_running_tasks` 换成它、新增 `_helper_tasks`、新增 `_admission_open`）。

**结构性说明（如实记录）**：Task 3 落盘时已经把 `_running_tasks` 从裸 `set[asyncio.Task]` 换成 `ManagedTaskSet`、新增了 `_helper_tasks: ManagedTaskSet`（收纳 `_schedule_delayed_enqueue_retry`/`_handle_queue_full` 派生的重试与 aggressive-clear 辅助任务）和 `_admission_open: bool`（默认 `True`）三处结构调整——这三处调整对 Task 3 已有测试而言是不可观察的内部实现替换（`stop()` 的 cancel+gather 语义不变），所以当时没有为它们单独写失败测试；`quiesce()` 是第一个真正让这三处调整产生外部可观察行为差异的方法，因此把它们的针对性回归测试放在本 Task，而不是回补进 Task 3。

**Files**: `litellm/litellm_core_utils/logging_worker.py`（改，新增 `quiesce`/`queue_size`/`LoggingDrained`/`LoggingDeadlineExceeded`/`LoggingDrainOutcome`）, `tests/test_litellm/litellm_core_utils/test_logging_worker.py`（改，扩展既有文件）

**Interfaces**

```python
@dataclasses.dataclass(frozen=True, slots=True)
class LoggingDrained:
    pass


@dataclasses.dataclass(frozen=True, slots=True)
class LoggingDeadlineExceeded:
    dropped_queue_items: int
    cancelled: int
    cancellation_failed: int


@dataclasses.dataclass(frozen=True, slots=True)
class LoggingForcedExit:
    dropped_queue_items: int
    cancelled: int
    cancellation_failed: int


LoggingDrainOutcome = LoggingDrained | LoggingDeadlineExceeded | LoggingForcedExit


class LoggingWorker:
    def queue_size(self) -> int: ...
    async def quiesce(
        self,
        deadline_remaining: Callable[[], float],
        admission_policy: Callable[[], bool],
        is_force_exit: Callable[[], bool],
    ) -> LoggingDrainOutcome: ...
    async def stop_after_quiesce(self) -> None: ...
```

**本轮评审的三处修订，如实记录**：

1. **三变体裁决**：`LoggingDeadlineExceeded`/新增 `LoggingForcedExit` 现在与 Task 4 的 `DeadlineExceeded`/`ForcedExit` 共用同一对字段名 `cancelled`/`cancellation_failed`（分别来自对 `_running_tasks`/`_helper_tasks` 各自调用 Task 1 的 `cancel_all_and_count_failures()` 后再相加）。`dropped_queue_items` 单独保留为第三个字段——它统计的是从未出队、从未成为 `asyncio.Task` 的队列项（`_drain_and_settle_dropped()` 直接 `coroutine.close()`），概念上不是"取消一个任务"而是"从未开始就地结算"，套不进 `cancelled`/`cancellation_failed` 这对只描述"已发起取消、是否清爽退出"的字段里；把它们硬塞进同一对字段会丢失"完全没跑过"与"跑了一半被取消"这两种运维需要分辨的情形，所以按需要保留为独立字段，而不是削足适履揉进两个字段。
2. **`is_force_exit` 无默认值（与 Task 4`drain()`不同，如实记录一处刻意的不对称）**：Task 4 的 `drain()` 给 `is_force_exit` 挂了 `GracefulShutdownManager.is_force_exit` 默认值，因为 `managed_task_supervisor.py` 本来就在 `litellm/proxy/shutdown/` 目录下，与 `GracefulShutdownManager` 同层，import 它不产生跨层依赖。而本 Task 的 `logging_worker.py` 位于 `litellm/litellm_core_utils/`——这一层是给纯 SDK（非 proxy）调用方共用的低层工具（major 9 讨论的可撤销 runtime binding 正是为了保这条边界），若在这里 `from litellm.proxy.shutdown.graceful_shutdown_manager import GracefulShutdownManager` 当默认值，就会让一个 core-utils 模块反向依赖 proxy 专属模块，方向性错误，且有绕出循环 import 的风险（`litellm/proxy/` 下大量模块本就依赖 `litellm_core_utils`）。因此本 Task 把 `is_force_exit` 定义成**必填**参数，不带默认值；调用方（Task 10 的 lifespan 接线，本就在 `litellm/proxy/` 下）显式传 `is_force_exit=GracefulShutdownManager.is_force_exit`——这与协调者对 Task 10"两处调用都必须显式传 `is_force_exit=...`"的要求完全一致，只是没有在 Task 5 自己的签名上重复挂一个永远不会被跨层默认值用到的默认值。下面 Steps §1 新增/修订的测试因此都显式传一个 lambda（与既有 `deadline_remaining`/`admission_policy` 参数的注入风格一致），不受影响。
3. **`stop_after_quiesce()` 新增（major 8）**：`quiesce()` 本身从不触碰 `_worker_task`/`_worker_loop()`——那个后台任务在 `quiesce()` 返回之后仍然活着（这是有意的：`ManagedTaskSupervisor.drain()` 的 `root_queue_unfinished` 参数依赖 `_worker_loop` 仍在运行才能继续处理 `drain()` 窗口期内姗姗来迟的队列项，见 Task 10 接线）。因此需要一个独立的终态停止方法，由 Task 10 在 `drain()` 也返回之后再调用一次，见下方 Steps §3。

**Steps**

1. 写失败测试（追加到 `tests/test_litellm/litellm_core_utils/test_logging_worker.py` 既有 `TestLoggingWorker` 类下）：

```python
    @pytest.mark.asyncio
    async def test_quiesce_returns_drained_immediately_when_nothing_pending(self):
        worker = LoggingWorker()
        worker.start()
        outcome = await worker.quiesce(
            deadline_remaining=lambda: 5.0, admission_policy=lambda: True, is_force_exit=lambda: False
        )
        assert isinstance(outcome, LoggingDrained)
        await worker.stop()

    @pytest.mark.asyncio
    async def test_quiesce_waits_for_already_queued_item_to_finish_normally(self):
        worker = LoggingWorker()
        worker.start()
        ran = []

        async def coro():
            await asyncio.sleep(0.02)
            ran.append("x")

        worker.enqueue(coro())
        outcome = await worker.quiesce(
            deadline_remaining=lambda: 5.0, admission_policy=lambda: True, is_force_exit=lambda: False
        )

        assert isinstance(outcome, LoggingDrained)
        assert ran == ["x"]
        await worker.stop()

    @pytest.mark.asyncio
    async def test_quiesce_closes_admission_so_new_enqueue_is_settled_dropped_not_queued(self):
        worker = LoggingWorker()
        worker.start()
        settled = []

        class FakeToken:
            def settle(self, outcome):
                settled.append(outcome)

        quiesce_task = asyncio.create_task(
            worker.quiesce(
                deadline_remaining=lambda: 5.0, admission_policy=lambda: False, is_force_exit=lambda: False
            )
        )
        await asyncio.sleep(0)  # let quiesce's first loop iteration set _admission_open

        async def late_coro():
            pass

        worker.enqueue(late_coro(), token=FakeToken())

        assert len(settled) == 1
        assert settled[0].kind == "skipped_during_shutdown"

        quiesce_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await quiesce_task
        await worker.stop()

    @pytest.mark.asyncio
    async def test_quiesce_never_runs_unconsumed_queue_items_past_deadline(self):
        """Core "取消路径不再执行 queued 业务 callback" 保证：仍在队列中、
        worker loop 从未取出的 item，必须以 dropped 结算，其协程体绝不能被
        执行。"""
        worker = LoggingWorker()
        worker.start()
        await worker._sem.acquire()  # 占满唯一并发名额，worker loop 永远无法出队
        executed = []
        settled = []

        class FakeToken:
            def settle(self, outcome):
                settled.append(outcome)

        async def coro():
            executed.append("ran")

        worker.enqueue(coro(), token=FakeToken())
        assert worker._queue.qsize() == 1

        outcome = await worker.quiesce(
            deadline_remaining=lambda: -1.0, admission_policy=lambda: False, is_force_exit=lambda: False
        )

        assert isinstance(outcome, LoggingDeadlineExceeded)
        assert outcome.dropped_queue_items == 1
        assert executed == []
        assert len(settled) == 1
        assert settled[0].kind == "skipped_during_shutdown"
        worker._sem.release()
        await worker.stop()

    @pytest.mark.asyncio
    async def test_quiesce_never_runs_unconsumed_queue_items_when_forced_exit(self):
        """三变体裁决：is_force_exit() 为 True 时同样不许执行 queued 业务
        callback，只是把结果 tag 换成 LoggingForcedExit——drop 语义与
        deadline-exceeded 分支完全一致，唯一区别是外部可观测的变体类型。"""
        worker = LoggingWorker()
        worker.start()
        await worker._sem.acquire()
        executed = []

        async def coro():
            executed.append("ran")

        worker.enqueue(coro())
        assert worker._queue.qsize() == 1

        outcome = await worker.quiesce(
            deadline_remaining=lambda: -1.0, admission_policy=lambda: False, is_force_exit=lambda: True
        )

        assert isinstance(outcome, LoggingForcedExit)
        assert outcome.dropped_queue_items == 1
        assert executed == []
        worker._sem.release()
        await worker.stop()

    @pytest.mark.asyncio
    async def test_quiesce_cancels_in_flight_running_task_past_deadline(self):
        worker = LoggingWorker()
        worker.start()
        settled = []

        class FakeToken:
            def settle(self, outcome):
                settled.append(outcome)

        async def slow_coro():
            await asyncio.sleep(10)

        worker.ensure_initialized_and_enqueue(slow_coro(), token=FakeToken())
        await asyncio.sleep(0.02)  # 让 worker loop 出队并启动它

        outcome = await worker.quiesce(
            deadline_remaining=lambda: -1.0, admission_policy=lambda: False, is_force_exit=lambda: False
        )

        assert isinstance(outcome, LoggingDeadlineExceeded)
        assert outcome.cancelled == 1
        assert outcome.cancellation_failed == 0
        assert len(settled) == 1
        await worker.stop()

    @pytest.mark.asyncio
    async def test_quiesce_counts_a_suppressed_running_task_cancellation_as_cancellation_failed(self):
        """镜像 Task 4 `test_drain_deadline_exceeded_counts_a_suppressed_cancellation_as_cancellation_failed`：
        一个吞掉 CancelledError、正常返回的运行中任务，必须记进
        `cancellation_failed` 而不是 `cancelled`——`cancelled`/`cancellation_failed`
        字段名与语义在 Task 4/5 之间完全一致，是三变体裁决的一部分。"""
        worker = LoggingWorker()
        worker.start()

        async def swallow_cancellation():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return "ignored on purpose"

        worker.ensure_initialized_and_enqueue(swallow_cancellation())
        await asyncio.sleep(0.02)

        outcome = await worker.quiesce(
            deadline_remaining=lambda: -1.0, admission_policy=lambda: False, is_force_exit=lambda: False
        )

        assert isinstance(outcome, LoggingDeadlineExceeded)
        assert outcome.cancelled == 0
        assert outcome.cancellation_failed == 1
        await worker.stop()

    @pytest.mark.asyncio
    async def test_quiesce_cancels_retry_helper_task_past_deadline(self):
        """重试 helper task（队列一度写满时派生）在 deadline 分支也必须被
        quiesce() 取消，不能留到进程决定退出之后再尝试一次 enqueue。"""
        worker = LoggingWorker(max_queue_size=1)
        worker.start()
        await worker._sem.acquire()  # 占满并发名额，避免出队

        async def first():
            pass

        async def second():
            pass

        worker.enqueue(first())
        assert worker._queue.qsize() == 1
        worker.enqueue(second())  # 队列已满 -> 派生一个 retry helper task
        await asyncio.sleep(0)
        assert len(worker._helper_tasks) == 1

        outcome = await worker.quiesce(
            deadline_remaining=lambda: -1.0, admission_policy=lambda: False, is_force_exit=lambda: False
        )

        assert isinstance(outcome, LoggingDeadlineExceeded)
        assert outcome.cancelled == 1
        assert worker._helper_tasks.is_empty()
        worker._sem.release()
        await worker.stop()

    @pytest.mark.asyncio
    async def test_flush_returns_immediately_after_deadline_triggered_quiesce(self):
        """major 7 回归测试：`_drain_and_settle_dropped()` 此前每个 `get_nowait()`
        出的 item 都不配对 `task_done()`，导致 `queue.join()`（`flush()` 的全部实现）
        永远挂起。修复前这个测试会在默认 pytest 超时下挂死/超时失败；修复后必须
        立刻返回。"""
        worker = LoggingWorker()
        worker.start()
        await worker._sem.acquire()  # 占满唯一并发名额，逼 item 停在队列里不出队

        async def coro():
            pass

        worker.enqueue(coro())
        assert worker._queue.qsize() == 1

        outcome = await worker.quiesce(
            deadline_remaining=lambda: -1.0, admission_policy=lambda: False, is_force_exit=lambda: False
        )
        assert isinstance(outcome, LoggingDeadlineExceeded)

        await asyncio.wait_for(worker.flush(), timeout=1.0)
        worker._sem.release()
        await worker.stop()

    def test_queue_size_reports_zero_before_start(self):
        worker = LoggingWorker()
        assert worker.queue_size() == 0

    @pytest.mark.asyncio
    async def test_queue_size_reports_unconsumed_items(self):
        worker = LoggingWorker()
        worker.start()
        await worker._sem.acquire()

        async def noop():
            pass

        worker.enqueue(noop())
        assert worker.queue_size() == 1
        worker._sem.release()
        await worker.stop()

    @pytest.mark.asyncio
    async def test_stop_after_quiesce_cancels_worker_loop_and_never_falls_through_to_clear_queue(self):
        """major 8: 一旦 `stop_after_quiesce()` 跑过，`_worker_loop` 的
        `except CancelledError` 分支必须走 drop-and-settle，绝不能落到
        `clear_queue()`（那个方法仍然会真正执行 queued 业务 callback）。"""
        worker = LoggingWorker()
        worker.start()
        executed = []

        async def coro():
            executed.append("ran")

        await worker._sem.acquire()  # 占满并发名额，逼 item 停在队列里不出队
        worker.enqueue(coro())
        assert worker._queue.qsize() == 1

        await worker.stop_after_quiesce()

        assert worker._worker_task is None
        assert executed == []  # clear_queue() 从未被调用，协程体从未执行

    @pytest.mark.asyncio
    async def test_enqueue_after_stop_after_quiesce_is_settled_dropped_not_queued(self):
        """blocker 3 + major 8 的交叉验证：`stop_after_quiesce()` 跑过之后，
        `_try_enqueue_existing_task` 必须走 `_quiesced` 分支拒绝，而不是
        `_admission_open` 分支（两者是本轮评审区分出的独立标志，见 Task 3
        `__init__` 的字段说明）。"""
        worker = LoggingWorker()
        worker.start()
        await worker.stop_after_quiesce()
        settled = []

        class FakeToken:
            def settle(self, outcome):
                settled.append(outcome)

        async def late_coro():
            pass

        worker.enqueue(late_coro(), token=FakeToken())

        assert len(settled) == 1
        assert settled[0].kind == "skipped_during_shutdown"
        assert settled[0].reason == "quiesced"
```

同时在文件顶部 import 区补充 `from litellm.litellm_core_utils.logging_worker import (LoggingDrained, LoggingDeadlineExceeded, LoggingForcedExit, ...)`（若既有 import 已用 `from litellm.litellm_core_utils.logging_worker import *`-风格聚合导入，则改为按需追加具名导入，不使用通配符）。

2. 确认失败：`pytest tests/test_litellm/litellm_core_utils/test_logging_worker.py -v -k quiesce` —— `AttributeError: 'LoggingWorker' object has no attribute 'quiesce'`；`test_queue_size_*`、`test_stop_after_quiesce_*`、`test_enqueue_after_stop_after_quiesce_*` 同理因对应属性/方法不存在而失败。

3. 实现。在 `litellm/litellm_core_utils/logging_worker.py` 顶部 import 块补充 `from typing import Callable`（若尚未导入，`assert_never` 已在 Task 3 的 blocker 3 修订里补过）；在 `LoggingTask` 定义之后、`LoggingWorker` 类定义之前追加：

```python
@dataclasses.dataclass(frozen=True, slots=True)
class LoggingDrained:
    pass


@dataclasses.dataclass(frozen=True, slots=True)
class LoggingDeadlineExceeded:
    dropped_queue_items: int
    cancelled: int
    cancellation_failed: int


@dataclasses.dataclass(frozen=True, slots=True)
class LoggingForcedExit:
    dropped_queue_items: int
    cancelled: int
    cancellation_failed: int


LoggingDrainOutcome = LoggingDrained | LoggingDeadlineExceeded | LoggingForcedExit

# Same rationale as managed_task_supervisor.py's _DRAIN_POLL_INTERVAL_SECONDS
# (see that module's comment): bounded polling, not a bare `sleep(0)` busy
# loop, and not a bespoke event-driven wake-up -- see this plan's
# Architecture section "未采纳方案".
_QUIESCE_POLL_INTERVAL_SECONDS = 0.05
```

在已保存的 `flush()` 方法之后、`clear_queue()` 之前插入（`flush()`/`clear_queue()` 本体均不改动）：

```python
    def queue_size(self) -> int:
        """Unconsumed-item count, exposed for ManagedTaskSupervisor.drain()'s
        `root_queue_unfinished` guard (Task 10 lifespan wiring) and for
        tests."""
        if self._queue is None:
            return 0
        return self._queue.qsize()

    async def quiesce(
        self,
        deadline_remaining: Callable[[], float],
        admission_policy: Callable[[], bool],
        is_force_exit: Callable[[], bool],
    ) -> LoggingDrainOutcome:
        """
        Proxy-only drain entry point -- a wholly separate method from
        flush()/stop(), which keep their pre-Phase-1b semantics untouched
        (pure-SDK callers, and the existing repo-wide flush() call sites,
        never call quiesce() and are therefore entirely unaffected).

        `is_force_exit` has no default (unlike
        ManagedTaskSupervisor.drain()'s default of
        `GracefulShutdownManager.is_force_exit`) because this module lives in
        `litellm_core_utils`, a layer shared with pure-SDK callers, and must
        not import anything from `litellm.proxy.*` -- see this Task's
        "本轮评审的三处修订" note above the Interfaces block. Callers (Task
        10's lifespan wiring) pass it explicitly.

        Normal phase: `admission_policy()` is polled once per loop
        iteration and written into `self._admission_open`, so `enqueue()`
        starts refusing new admission (settling as dropped instead of
        queuing) the moment the caller's policy flips -- while already
        queued items keep draining via the existing `_worker_loop`
        background task; quiesce() itself never dequeues or runs anything,
        it only watches until settled or the deadline passes.

        Deadline/force-exit phase: any coroutine still sitting in
        `self._queue` unconsumed is drained item-by-item -- closed and
        settled as dropped, NEVER executed via clear_queue() (spec line 133:
        取消路径不得执行 queued 业务 callback) -- then every currently
        in-flight processing task and every retry/aggressive-clear helper
        task is cancelled together and awaited to settlement, counting clean
        vs. failed cancellations the same way Task 4's `drain()` does (major
        6/three-variant). `is_force_exit()` is checked once, after
        cancellation has already happened, purely to pick which of
        `LoggingDeadlineExceeded`/`LoggingForcedExit` to tag the result with
        -- it is never re-queried a second time to guess the reason.
        """
        if self._queue is None:
            return LoggingDrained()

        while True:
            self._admission_open = admission_policy()

            if self._queue.empty() and self._running_tasks.is_empty() and self._helper_tasks.is_empty():
                return LoggingDrained()

            if deadline_remaining() <= 0:
                dropped = self._drain_and_settle_dropped(self._queue, reason="deadline_exceeded")
                running_cancelled, running_failed = await self._running_tasks.cancel_all_and_count_failures()
                helper_cancelled, helper_failed = await self._helper_tasks.cancel_all_and_count_failures()
                cancelled = running_cancelled + helper_cancelled
                cancellation_failed = running_failed + helper_failed
                if is_force_exit():
                    return LoggingForcedExit(
                        dropped_queue_items=dropped,
                        cancelled=cancelled,
                        cancellation_failed=cancellation_failed,
                    )
                return LoggingDeadlineExceeded(
                    dropped_queue_items=dropped,
                    cancelled=cancelled,
                    cancellation_failed=cancellation_failed,
                )

            await asyncio.sleep(min(_QUIESCE_POLL_INTERVAL_SECONDS, max(deadline_remaining(), 0.0)))

    async def stop_after_quiesce(self) -> None:
        """
        Terminal stop for the background `_worker_loop` task itself (major
        8). A wholly separate method from `stop()` (unchanged, still used by
        pure-SDK/non-quiesce callers) -- `quiesce()` above never touches
        `_worker_loop`/`_worker_task` on its own, since
        `ManagedTaskSupervisor.drain()`'s `root_queue_unfinished` guard (Task
        10 wiring) depends on `_worker_loop` staying alive to keep draining
        late-arriving queue items during `drain()`'s own window. Call this
        only once, only after both `quiesce()` and `drain()` have returned.

        Sets `_quiesced = True` first so `_worker_loop`'s
        `except CancelledError` handler drops-and-settles any item still in
        the queue instead of falling through to `clear_queue()` --
        `clear_queue()` still *executes* queued business coroutines (see its
        own docstring), which is exactly what the whole shutdown contract
        forbids past this point.
        """
        self._quiesced = True
        if self._worker_task is not None:
            self._worker_task.cancel()
            await asyncio.gather(self._worker_task, return_exceptions=True)
            self._worker_task = None
```

4. 确认转绿：`pytest tests/test_litellm/litellm_core_utils/test_logging_worker.py -v` 全绿。再跑一次 Task 3 已核实的既有 `flush()`/`stop()` 调用方所在测试文件，确认它们仍不受影响（`quiesce()`/`queue_size()`/`stop_after_quiesce()` 是纯新增方法，不改动任何既有方法体）。

5. 提交：`git add litellm/litellm_core_utils/logging_worker.py tests/test_litellm/litellm_core_utils/test_logging_worker.py && git commit -m "feat: add LoggingWorker.quiesce() as a distinct proxy-facing drain method"`

---

## Task 6 — Path A 6 处接入：`token=acquire_root_scope()` 线程化

依赖 Task 2（`acquire_root_scope`/`AccountingRootToken`，含本文档在 Task 6 处回填的组合 Protocol）、Task 3（`ensure_initialized_and_enqueue(..., token=...)` 已接受 `token` 关键字参数）。

**已核对现状（读码结论，6 处调用点行号均已用 `grep -n ensure_initialized_and_enqueue -r litellm/` 复核，与既有 Phase 1a 文本描述一致）**：

| 站点 | 文件:行号 | 层 |
|---|---|---|
| A1 | `litellm/utils.py:1089`（`_client_async_logging_helper`） | 核心 SDK |
| A2 | `litellm/caching/caching_handler.py:646`（`_async_log_cache_hit_on_callbacks`） | 核心 SDK |
| A3 | `litellm/litellm_core_utils/realtime_streaming.py:322`（`RealTimeStreaming` 内部方法） | 核心 SDK |
| A4 | `litellm/proxy/pass_through_endpoints/streaming_handler.py:99`（`chunk_processor` 的 `finally` 块） | proxy |
| A5 | `litellm/proxy/pass_through_endpoints/pass_through_endpoints.py:1349` | proxy |
| A6 | `litellm/proxy/pass_through_endpoints/pass_through_endpoints.py:2133`（websocket 分支） | proxy |

六处调用点的改法完全同构：只在既有 `GLOBAL_LOGGING_WORKER.ensure_initialized_and_enqueue(...)` 调用上追加一个 `token=acquire_root_scope()` 关键字参数，不改动 `async_coroutine=` 参数本身、不改动周边任何业务逻辑分支；`acquire_root_scope()` 是中立函数（`litellm/litellm_core_utils/accounting_scope.py`，Task 2），核心 SDK 三处（A1-A3）调用它不构成"核心层 import 代理层类型"违规。纯 SDK 场景下 `acquire_root_scope()` 返回 `None`（因为 `register_root_scope_provider` 从未被调用），`token=None` 这条既有分支（Task 3 已覆盖）保证行为逐字节不变。

**Files**:
- `litellm/utils.py`（改，A1）, `tests/test_litellm/test_utils.py`（改）
- `litellm/caching/caching_handler.py`（改，A2）, `tests/test_litellm/caching/test_caching_handler.py`（改）
- `litellm/litellm_core_utils/realtime_streaming.py`（改，A3）, `tests/test_litellm/litellm_core_utils/test_realtime_streaming.py`（改，扩展既有 `test_log_messages_routes_async_logging_through_bounded_worker`）
- `litellm/proxy/pass_through_endpoints/streaming_handler.py`（改，A4）, `tests/test_litellm/proxy/pass_through_endpoints/test_streaming_handler_interrupt.py`（改）
- `litellm/proxy/pass_through_endpoints/pass_through_endpoints.py`（改，A5+A6）, `tests/test_litellm/proxy/pass_through_endpoints/test_pass_through_endpoints.py`（改）

**Interfaces**：本 Task 不新增任何公共接口，纯调用点线程化。

**Steps**

1. 写失败测试。六处站点用同一手法：通过 `accounting_scope.register_root_scope_provider(lambda: fake_token)` 这个正式 DI 钩子注入一个可观察的假 token（不 monkeypatch 类属性），再 patch 住 `GLOBAL_LOGGING_WORKER.ensure_initialized_and_enqueue` 本身（模块级单例替换，属于既有测试惯用手法，非本计划新增反模式）断言其 `token=` 关键字实参就是这个假 token。

   A1（追加到 `tests/test_litellm/test_utils.py`）：

```python
class TestClientAsyncLoggingHelperAccountingToken:
    @pytest.mark.asyncio
    async def test_threads_root_accounting_token_into_logging_worker_enqueue(self):
        from litellm.litellm_core_utils.accounting_scope import register_root_scope_provider

        class _FakeRootToken:
            def is_valid(self):
                return True

            def spawn(self, coro, *, name, kind):
                coro.close()

            def settle(self, outcome):
                pass

        fake_token = _FakeRootToken()
        register_root_scope_provider(lambda: fake_token)
        try:
            with patch(
                "litellm.litellm_core_utils.logging_worker.GLOBAL_LOGGING_WORKER.ensure_initialized_and_enqueue"
            ) as mock_enqueue:
                logging_obj = MagicMock()
                logging_obj.handle_sync_success_callbacks_for_async_calls = MagicMock()
                await litellm.utils._client_async_logging_helper(
                    logging_obj=logging_obj,
                    result=MagicMock(),
                    start_time=0,
                    end_time=1,
                    is_completion_with_fallbacks=False,
                )
                mock_enqueue.assert_called_once()
                assert mock_enqueue.call_args.kwargs["token"] is fake_token
        finally:
            register_root_scope_provider(None)
```

   A2（追加到 `tests/test_litellm/caching/test_caching_handler.py`；沿用同一 `_FakeRootToken`，故只列差异部分）：

```python
class TestAsyncLogCacheHitAccountingToken:
    def test_threads_root_accounting_token_into_logging_worker_enqueue(self):
        from litellm.litellm_core_utils.accounting_scope import register_root_scope_provider

        class _FakeRootToken:
            def is_valid(self):
                return True

            def spawn(self, coro, *, name, kind):
                coro.close()

            def settle(self, outcome):
                pass

        fake_token = _FakeRootToken()
        register_root_scope_provider(lambda: fake_token)
        try:
            with patch(
                "litellm.litellm_core_utils.logging_worker.GLOBAL_LOGGING_WORKER.ensure_initialized_and_enqueue"
            ) as mock_enqueue:
                handler = LLMCachingHandler(
                    original_function=MagicMock(), request_kwargs={}, call_type="acompletion"
                )
                handler._async_log_cache_hit_on_callbacks(
                    logging_obj=MagicMock(),
                    cached_result=MagicMock(),
                    start_time=datetime.now(),
                    end_time=datetime.now(),
                    cache_hit=True,
                )
                mock_enqueue.assert_called_once()
                assert mock_enqueue.call_args.kwargs["token"] is fake_token
        finally:
            register_root_scope_provider(None)
```

   （具体 `LLMCachingHandler` 构造参数以 `tests/test_litellm/caching/test_caching_handler.py` 既有测试里的构造惯例为准，实现时按该文件里已有的 fixture/helper 对齐，不要重新发明构造方式。）

   A3——扩展既有测试 `test_log_messages_routes_async_logging_through_bounded_worker`，在其 `patch(...)` 块内追加对 `register_root_scope_provider` 的注入，并把断言从 `assert_called_once()` 加强为对 `token=` 实参的检查：

```python
@pytest.mark.asyncio
async def test_log_messages_routes_async_logging_through_bounded_worker():
    """Realtime success logging must go through GLOBAL_LOGGING_WORKER (bounded
    queue + per-coroutine timeout), not a bare asyncio.create_task. A bare task
    has no timeout/concurrency cap, so when a logging callback is slow every
    realtime turn leaves a suspended task pinning its response in memory -> an
    unbounded leak. Regression for that fix. Also verifies the root accounting
    token (Task 6, Phase 1b) is threaded through so downstream Path B children
    spawned from this coroutine's own call stack can find the ambient scope."""
    from litellm.litellm_core_utils.accounting_scope import register_root_scope_provider

    class _FakeRootToken:
        def is_valid(self):
            return True

        def spawn(self, coro, *, name, kind):
            coro.close()

        def settle(self, outcome):
            pass

    fake_token = _FakeRootToken()
    logging_obj = MagicMock()
    streaming = RealTimeStreaming(MagicMock(), MagicMock(), logging_obj)
    streaming.messages = [{"type": "session.created"}]

    register_root_scope_provider(lambda: fake_token)
    try:
        with (
            patch("litellm.litellm_core_utils.realtime_streaming.GLOBAL_LOGGING_WORKER") as mock_worker,
            patch("litellm.litellm_core_utils.realtime_streaming.asyncio.create_task") as mock_create_task,
            patch("litellm.litellm_core_utils.realtime_streaming.executor.submit"),
        ):
            await streaming.log_messages()

            mock_worker.ensure_initialized_and_enqueue.assert_called_once()
            assert mock_worker.ensure_initialized_and_enqueue.call_args.kwargs["token"] is fake_token
            # the bare create_task path must no longer be used for success logging
            mock_create_task.assert_not_called()
    finally:
        register_root_scope_provider(None)
```

   A4（追加到 `tests/test_litellm/proxy/pass_through_endpoints/test_streaming_handler_interrupt.py`；`ensure_initialized_and_enqueue` 在此文件已有的 `test_chunk_processor_routes_logging_through_logging_worker` 里是用 `patch.object(..., side_effect=_capture)` 手动接住协程并 `.close()`，而不是整体 mock 掉——因为它是异步生成器的 `finally` 块，若整体 mock 会让协程对象泄漏出 "never awaited" 警告；沿用同一 `_capture` 手法，只是把捕获签名从 `_capture(async_coroutine)` 扩成 `_capture(async_coroutine, **kwargs)` 以便断言 `token`）：

```python
class TestChunkProcessorAccountingToken:
    @pytest.mark.asyncio
    async def test_threads_root_accounting_token_into_logging_worker_enqueue(self):
        from litellm.litellm_core_utils.accounting_scope import register_root_scope_provider

        class _FakeRootToken:
            def is_valid(self):
                return True

            def spawn(self, coro, *, name, kind):
                coro.close()

            def settle(self, outcome):
                pass

        fake_token = _FakeRootToken()
        chunks = [b"chunk-1", b"chunk-2"]
        response = _make_streaming_response(chunks)

        enqueued: list[tuple[object, dict]] = []

        def _capture(async_coroutine, **kwargs):
            enqueued.append((async_coroutine, kwargs))
            async_coroutine.close()

        register_root_scope_provider(lambda: fake_token)
        try:
            with (
                patch.object(
                    PassThroughStreamingHandler,
                    "_route_streaming_logging_to_handler",
                    new=AsyncMock(),
                ),
                patch.object(
                    GLOBAL_LOGGING_WORKER,
                    "ensure_initialized_and_enqueue",
                    side_effect=_capture,
                ) as mock_enqueue,
            ):
                async for _ in PassThroughStreamingHandler.chunk_processor(
                    response=response,
                    request_body={"model": "claude-3-haiku"},
                    litellm_logging_obj=MagicMock(),
                    endpoint_type=EndpointType.GENERIC,
                    start_time=datetime.now(),
                    passthrough_success_handler_obj=MagicMock(),
                    url_route="/bedrock/model/claude/invoke-with-response-stream",
                ):
                    pass

            mock_enqueue.assert_called_once()
            assert enqueued[0][1]["token"] is fake_token
        finally:
            register_root_scope_provider(None)
```

   （沿用 `_make_streaming_response` 这个本文件已有的模块级 helper，不重新发明流式 response 构造方式；`EndpointType`/`datetime`/`GLOBAL_LOGGING_WORKER`/`PassThroughStreamingHandler` 均为本文件已有 import，无需新增。）

   A5/A6——追加到 `tests/test_litellm/proxy/pass_through_endpoints/test_pass_through_endpoints.py`。这两处的 mock 手法已用一次性 PoC（`/tmp/poc_test_pass_through_success2.py`、`/tmp/poc_test_websocket_success.py`，均通过 `uv run pytest` 实跑验证，PoC 为一次性验证脚本，不纳入正式测试套件）核实过，原因是它们的调用路径不像 A1-A4 那样能一步 mock 到位：

   - HTTP 分支（A5）：`pass_through_request` 内部并不直接调用可轻易拦截的 httpx 客户端方法，而是先经过 `HttpPassThroughEndpointHelpers.non_streaming_http_request_handler`（同模块内的另一个 classmethod）间接发起请求；直接 mock 传输层 client（如 `get_async_httpx_client().client.request`）会因为 `state_raw_body`/`MagicMock(spec=Request)` 的属性访问语义不吻合预期分支，导致测试放行到真实网络调用（PoC v1 复现为 `aiohttp.client_exceptions.ClientConnectorDNSError`）。正确做法是直接 mock 掉 `HttpPassThroughEndpointHelpers.non_streaming_http_request_handler` 本身，绕开这层间接调用与 SigV4 分支判断。
   - websocket 分支（A6）：需要让 `forward_client_to_upstream`/`forward_upstream_to_client` 两个内部 task 都"干净退场"才能到达 2133 行的成功块——`websocket.receive()` 直接返回 `{"type": "websocket.disconnect"}` 结束前者，`upstream_ws.recv(decode=False)` 抛出 `ConnectionClosedOK`（该分支已被源码捕获为正常收尾）结束后者，`connect(...)` 本身 mock 成一个返回该 fake upstream ws 的 async context manager。

```python
class TestPassThroughSuccessLoggingAccountingToken:
    @pytest.mark.asyncio
    async def test_http_success_path_threads_root_accounting_token(self):
        from fastapi import Request
        from starlette.datastructures import Headers, QueryParams

        from litellm.litellm_core_utils.accounting_scope import register_root_scope_provider
        from litellm.proxy.pass_through_endpoints.pass_through_endpoints import (
            HttpPassThroughEndpointHelpers,
            pass_through_request,
        )

        class _FakeRootToken:
            def is_valid(self):
                return True

            def spawn(self, coro, *, name, kind):
                coro.close()

            def settle(self, outcome):
                pass

        fake_token = _FakeRootToken()
        enqueued: list[tuple[object, dict]] = []

        def _capture(async_coroutine, **kwargs):
            enqueued.append((async_coroutine, kwargs))
            async_coroutine.close()

        register_root_scope_provider(lambda: fake_token)
        try:
            with patch("litellm.proxy.proxy_server.proxy_logging_obj") as mock_proxy_logging:
                with patch(
                    "litellm.proxy.pass_through_endpoints.pass_through_endpoints.ProxyBaseLLMRequestProcessing"
                ) as mock_processing:
                    with patch.object(
                        GLOBAL_LOGGING_WORKER, "ensure_initialized_and_enqueue", side_effect=_capture
                    ) as mock_enqueue:
                        with patch.object(
                            HttpPassThroughEndpointHelpers, "non_streaming_http_request_handler"
                        ) as mock_handler:
                            mock_proxy_logging.post_call_failure_hook = AsyncMock()
                            mock_proxy_logging.pre_call_hook = AsyncMock(side_effect=lambda **kw: kw["data"])
                            mock_proxy_logging.post_call_success_hook = AsyncMock(
                                side_effect=lambda **kw: kw.get("response")
                            )
                            mock_proxy_logging.post_call_response_headers_hook = AsyncMock(return_value=None)

                            mock_response = MagicMock(spec=httpx.Response)
                            mock_response.status_code = 200
                            mock_response.headers = httpx.Headers({"content-type": "application/json"})
                            mock_response.aread = AsyncMock(return_value=b'{"ok": true}')
                            mock_response.text = '{"ok": true}'
                            mock_response.json = MagicMock(return_value={"ok": True})
                            mock_response.request = MagicMock()
                            mock_handler.return_value = mock_response

                            mock_processing.get_custom_headers.return_value = {}

                            mock_request = MagicMock(spec=Request)
                            mock_request.method = "POST"
                            mock_request.body = AsyncMock(return_value=b'{"test": "data"}')
                            mock_request.headers = Headers({})
                            mock_request.query_params = QueryParams({})

                            await pass_through_request(
                                request=mock_request,
                                target="http://test.com",
                                custom_headers={},
                                user_api_key_dict=MagicMock(),
                            )
                            mock_enqueue.assert_called_once()
                            assert enqueued[0][1]["token"] is fake_token
        finally:
            register_root_scope_provider(None)

    @pytest.mark.asyncio
    async def test_websocket_success_path_threads_root_accounting_token(self):
        from websockets.exceptions import ConnectionClosedOK

        from litellm.litellm_core_utils.accounting_scope import register_root_scope_provider
        from litellm.proxy.pass_through_endpoints.pass_through_endpoints import (
            websocket_passthrough_request,
        )

        class _FakeRootToken:
            def is_valid(self):
                return True

            def spawn(self, coro, *, name, kind):
                coro.close()

            def settle(self, outcome):
                pass

        class _FakeUpstreamWS:
            async def close(self):
                pass

            async def recv(self, decode=False):
                raise ConnectionClosedOK(None, None)

            def __aiter__(self):
                async def _empty():
                    return
                    yield  # pragma: no cover

                return _empty()

        class _FakeConnectCtx:
            async def __aenter__(self):
                return _FakeUpstreamWS()

            async def __aexit__(self, *exc):
                return False

        fake_token = _FakeRootToken()
        enqueued: list[tuple[object, dict]] = []

        def _capture(*, async_coroutine, **kwargs):
            enqueued.append((async_coroutine, kwargs))
            async_coroutine.close()

        mock_websocket = MagicMock()
        mock_websocket.accept = AsyncMock()
        mock_websocket.headers = {}
        mock_websocket.receive = AsyncMock(return_value={"type": "websocket.disconnect"})
        mock_websocket.send_text = AsyncMock()
        mock_websocket.send_bytes = AsyncMock()
        mock_websocket.close = AsyncMock()
        from starlette.websockets import WebSocketState

        mock_websocket.client_state = WebSocketState.CONNECTED

        register_root_scope_provider(lambda: fake_token)
        try:
            with patch("litellm.proxy.proxy_server.proxy_logging_obj") as mock_proxy_logging:
                with patch(
                    "litellm.proxy.pass_through_endpoints.pass_through_endpoints.connect",
                    return_value=_FakeConnectCtx(),
                ):
                    with patch.object(
                        GLOBAL_LOGGING_WORKER, "ensure_initialized_and_enqueue", side_effect=_capture
                    ) as mock_enqueue:
                        mock_proxy_logging.pre_call_hook = AsyncMock(side_effect=lambda **kw: kw["data"])
                        mock_proxy_logging.post_call_success_hook = AsyncMock(return_value=None)

                        await websocket_passthrough_request(
                            websocket=mock_websocket,
                            target="ws://test.com",
                            custom_headers={},
                            user_api_key_dict=MagicMock(),
                        )

                        mock_enqueue.assert_called_once()
                        assert enqueued[0][1]["token"] is fake_token
        finally:
            register_root_scope_provider(None)
```

   （两个测试都需要 `httpx`/`MagicMock`/`AsyncMock`/`patch` 这些本文件已有的顶层 import；`GLOBAL_LOGGING_WORKER` 若本文件尚未导入，需补一行 `from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER`。websocket 用例是 `websocket_passthrough_request` 这个函数第一次获得测试覆盖——如实记录：`grep -rn "websocket_passthrough_request" tests/test_litellm/proxy/pass_through_endpoints/*.py` 复核过，此前为空。）

2. 确认失败：对每个上述测试跑一次 `pytest <file> -v -k accounting_token`——全部因为 `mock_enqueue.call_args.kwargs` 里没有 `token` 键（`KeyError`）或 `token=None`（`AssertionError`）而失败。

3. 实现。六处一致的最小 diff（以 A1 为例，其余 5 处同构，仅函数名/文件不同）：

```python
# litellm/utils.py
from litellm.litellm_core_utils.accounting_scope import acquire_root_scope
from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

GLOBAL_LOGGING_WORKER.ensure_initialized_and_enqueue(
    async_coroutine=logging_obj.async_success_handler(result=result, start_time=start_time, end_time=end_time),
    token=acquire_root_scope(),
)
```

对应地：
- `caching_handler.py:646` 的 `ensure_initialized_and_enqueue(async_coroutine=...)` 追加 `token=acquire_root_scope()`。
- `realtime_streaming.py:322` 的单行调用改为多行，追加 `token=acquire_root_scope()`。
- `pass_through_endpoints/streaming_handler.py:99`、`pass_through_endpoints.py:1349`、`pass_through_endpoints.py:2133` 同样各自追加 `token=acquire_root_scope()`。

每处修改前先在文件顶部补一行 `from litellm.litellm_core_utils.accounting_scope import acquire_root_scope`（若该文件已有从 `accounting_scope` 的其他导入则合并到同一 `import` 语句）。

4. 确认转绿：对 5 个受影响测试文件各跑一次全量 `pytest <file> -v`，确认新断言与既有断言均通过，且没有因新增 import 触发循环导入（`litellm/utils.py`/`caching_handler.py`/`realtime_streaming.py` 均属核心层，`accounting_scope.py` 本身不 import 这三者中任何一个，不构成环）。

5. 提交：`git add litellm/utils.py litellm/caching/caching_handler.py litellm/litellm_core_utils/realtime_streaming.py litellm/proxy/pass_through_endpoints/streaming_handler.py litellm/proxy/pass_through_endpoints/pass_through_endpoints.py tests/test_litellm/test_utils.py tests/test_litellm/caching/test_caching_handler.py tests/test_litellm/litellm_core_utils/test_realtime_streaming.py tests/test_litellm/proxy/pass_through_endpoints/test_streaming_handler_interrupt.py tests/test_litellm/proxy/pass_through_endpoints/test_pass_through_endpoints.py && git commit -m "feat: thread root accounting token through Path A logging-worker enqueue sites"`

---

## Task 7 — Path B1 接入：`streaming_handler.py:2053` 改用 `spawn_detached`

依赖 Task 2（`spawn_detached`）。

**已核对现状**：`litellm/litellm_core_utils/streaming_handler.py:2053`，在 `CustomStreamWrapper.__anext__`（唯一入口，`__next__` 同步版没有这条分支）里，流式正常结束（`StopAsyncIteration` 前）且没有走 `_deferred_stream_complete_args` 延迟路径时，用裸 `asyncio.create_task(self.logging_obj.dispatch_success_handlers(...))` 派发标准流式成功记账（legacy string 回调走 `dispatch_success_handlers` 内部的 `executor.submit`，`prefer_async_handlers=True` 让 `CustomLogger` 走 `async_success_handler`）。这是 B1 唯一站点。

同一文件里另外 3 处裸 `asyncio.create_task`——`:2011`（`async_cache_streaming_response`，写缓存，非记账）、`:2080`/`:2092`（`async_failure_handler`，通用失败回调）——已在"架构"一节记录裁决：维持现状、不纳入本 Task，理由是它们没有记账语义或已确认与 proxy 记账链路无关，纳入会造成核心层反向依赖 proxy 类型却换不来实质收益。本 Task **只改 `:2053` 这一行**，不动其余 3 处。

**Files**：`litellm/litellm_core_utils/streaming_handler.py`（改）, `tests/test_litellm/litellm_core_utils/test_streaming_handler.py`（改，扩展既有文件，复用已有的 `bedrock_chunks` 模块级 fixture 数据与 `CustomStreamWrapper`/`Logging` 真实对象构造惯例，不新增 mock 框架）

**Interfaces**：本 Task 不新增公共接口，纯调用点替换。

**Steps**

1. 写失败测试（追加到 `test_streaming_handler.py` 末尾）。用真实 `CustomStreamWrapper` + 真实 `Logging` 驱动一次正常到 `StopAsyncIteration` 的 `async for`，patch 住 `spawn_detached`（而非 `asyncio.create_task`——同文件其余 3 处仍合法使用裸 `asyncio.create_task`，一刀切断言"不许调用 `asyncio.create_task`"会把那 3 处的正当用法也当成回归），断言被调度的协程正是 `dispatch_success_handlers`（用 `coro.cr_code.co_name` 识别具体协程函数，不实际执行/await 它，`.close()` 掉避免 "never awaited" 警告，这个识别手法已通过一次性 PoC——`/tmp/poc_test_streaming_b1.py`，`uv run pytest` 实跑通过——验证可行）：

```python
@pytest.mark.asyncio
async def test_dispatch_success_handlers_scheduled_via_spawn_detached():
    """B1 (Phase 1b Task 7): the standard async-streaming success dispatch must
    go through spawn_detached (accounting-aware) instead of a bare
    asyncio.create_task, so a graceful shutdown's drain phase can find and wait
    on it instead of losing it to a GC'd weakly-referenced task."""
    final_chunk = ModelResponseStream(
        id="chatcmpl-b1-final",
        created=1742056047,
        model=None,
        object="chat.completion.chunk",
        choices=[
            StreamingChoices(
                finish_reason="stop",
                index=0,
                delta=Delta(content="", role="assistant"),
            )
        ],
        usage=Usage(completion_tokens=1, prompt_tokens=1, total_tokens=2),
    )

    logging_obj = Logging(
        model="bedrock/claude-haiku-4-5-20251001-v1:0",
        messages=[{"role": "user", "content": "Hey"}],
        stream=True,
        call_type="completion",
        start_time=time.time(),
        litellm_call_id="b1-test",
        function_id="1245",
    )

    response = CustomStreamWrapper(
        completion_stream=ModelResponseListIterator(model_responses=bedrock_chunks + [final_chunk]),
        model="bedrock/claude-haiku-4-5-20251001-v1:0",
        custom_llm_provider="bedrock",
        logging_obj=logging_obj,
        stream_options={"include_usage": True},
    )

    scheduled = []

    def _capture(coro, *, name):
        scheduled.append((coro, name))
        coro.close()

    with patch(
        "litellm.litellm_core_utils.streaming_handler.spawn_detached", side_effect=_capture
    ) as mock_spawn_detached:
        chunks = [c async for c in response]

    assert len(chunks) > 0
    mock_spawn_detached.assert_called_once()
    coro, name = scheduled[0]
    assert coro.cr_code.co_name == "dispatch_success_handlers"
    assert name == "dispatch_success_handlers"
```

（`bedrock_chunks`、`Logging`、`CustomStreamWrapper`、`ModelResponseListIterator`、`Delta`/`ModelResponseStream`/`StreamingChoices`/`Usage`、`patch`、`time` 均为本文件已有顶层 import/module-level fixture，无需新增。）

2. 确认失败：`pytest tests/test_litellm/litellm_core_utils/test_streaming_handler.py -v -k spawn_detached`——`AttributeError: <module 'litellm.litellm_core_utils.streaming_handler'> does not have the attribute 'spawn_detached'`（`patch(...)` 在 target 模块里找不到这个名字，因为还没导入）。

3. 实现：

```python
# litellm/litellm_core_utils/streaming_handler.py 顶部 import 追加
from litellm.litellm_core_utils.accounting_scope import spawn_detached
```

```python
# 替换 :2053 起的调用（原 asyncio.create_task(...) 整体替换为 spawn_detached(...)，
# 参数从位置参数改为显式关键字传参给 dispatch_success_handlers，
# 因为 spawn_detached 只接受一个协程位置参数 + name 关键字参数，
# 不能像 asyncio.create_task 那样直接把协程套一层就地内联同样的调用写法——
# 实际上写法几乎不变，只是把外层函数名从 asyncio.create_task 换成 spawn_detached，
# 并新增 name= 关键字）
spawn_detached(
    self.logging_obj.dispatch_success_handlers(
        complete_streaming_response,
        cache_hit=cache_hit,
        start_time=None,
        end_time=None,
        prefer_async_handlers=True,
    ),
    name="dispatch_success_handlers",
)
```

4. 确认转绿：`pytest tests/test_litellm/litellm_core_utils/test_streaming_handler.py -v`——全量跑一次该文件（2981 行、约 90 个既有测试），确认新测试通过且没有破坏任何既有断言（尤其是本 Task 摘要里提到的 `test_stream_chunk_builder_raise_at_end_of_stream_still_recovers_usage` 等同样会经过这条 `__anext__` 路径的既有用例）；再单独跑一次 `tests/test_litellm/litellm_core_utils/test_accounting_scope.py -v` 确认 Task 2 的纯-SDK 回退路径（`register_root_scope_provider` 从未被 proxy 注册时，`spawn_detached` 退化为裸 `asyncio.create_task`）在这条真实调用路径下没有被破坏——这一步是新增的交叉验证，因为 Task 2 当时只用假 `_FakeScope` 单测过 `spawn_detached` 本身，这是它第一次被一个真实业务调用点使用。

5. 提交：`git add litellm/litellm_core_utils/streaming_handler.py tests/test_litellm/litellm_core_utils/test_streaming_handler.py && git commit -m "feat: route standard streaming success dispatch through spawn_detached (Path B1)"`

---

## Task 8 — Path B2/B3/B4 记账接入：`update_cache`/`_batch_database_updates`/`async_post_call_failure_hook` 改用 `spawn_detached`/`ambient_or_root_scope`

依赖 Task 2（`spawn_detached`/`current_accounting_scope`/`ambient_or_root_scope`）、Task 4（`ManagedTaskSupervisor`/`AccountingLease`——具体是 Task 4 的两处补丁：全局单例注册、`_spawn_accounting_child` 的 scope 绑定；见 Task 4 "补充说明（Task 8 落笔时回填，如实记录）"）、Task 7（同一个 `spawn_detached` 在 B1 已经用过一次，这里是第二、三个真实调用点）。

**范围调整说明（本轮评审 blocker 4，如实记录）**：本 Task 原本只覆盖成功路径的 B2/B3 两个记账创建点。评审指出失败路径（`ProxyLogging.post_call_failure_hook` → `_ProxyDBLogger.async_post_call_failure_hook` → `db_spend_update_writer.update_database` → `_batch_database_updates`）虽然最终落到*同一个* `update_database` 方法、B3 的 `spawn_detached` 接入天然覆盖了它的内部调用点，但 `async_post_call_failure_hook` 自身从未 acquire 过 ambient scope——它是从请求自己的异常处理路径直接 `await` 进来的（不像 B2/B3 那样，从 Task 6/7 已经在更早时点 acquire 过 root 的同一条调用链上被 `await` 到），所以 `current_accounting_scope.get()` 在它执行期间恒为 `None`。这本身并不总是构成 bug——只要 root admission 仍开放（Task 10 第 3 步 `close_root_admission()` 严格排在 `wait_for_drain()` 之后才执行，而 `async_post_call_failure_hook` 执行期间这次请求自己必然仍计入 in-flight 计数——这一点已经用 `grep -n "close_root_admission\|wait_for_drain" ` 核对过 Task 10 的落笔顺序），`update_database` 内部 B3 的 `spawn_detached` 仍能通过自己的 ad hoc root 兜底正确拿到一条新 lease、正确追踪子任务。**唯一的残余风险**（如实记录，不是本 Task 声称已完全消除的东西）：如果 `wait_for_drain()` 因为 deadline 到期而提前放弃、仍有请求真正在途（`wait_for_drain` 自己的"timeout 分支"允许这种情况），`close_root_admission()` 会在这些"掉队"请求还没跑完的情况下提前触发；这些请求如果随后失败、落到 `async_post_call_failure_hook`，此时 root admission 已经关闭，`acquire_root_scope()` 会返回 `None`，`spawn_detached` 退化为裸 `create_task`——这个残余竞态在 B1/B2/B3 的成功路径上同样存在（同一个"掉队"流式请求，如果之后走成功收尾，`spawn_detached` 一样会在 root admission 已关闭的情况下退化为裸任务），不是本 Task 独有、也不是本 Task 能单独解决的架构性权衡（一旦 `wait_for_drain` 已经放弃等待，就已经进入"尽力而为"区间）——已在收尾报告"已知未决事项"一节里记录为已接受、非阻塞的残余风险，不要求本 Task 或后续 Task 新增机制去封堵。本 Task 能做到、也确实要做到的是：把"acquire scope on entry, settle on exit"这个模式补齐到 `async_post_call_failure_hook` 自己身上，把它从"完全没有任何 scope 意识、只能被动依赖 B3 内部 `spawn_detached` 的隐式 ad hoc 兜底"提升到和 B1/B2/B3 同一个显式水位——即使两者在 root admission 仍开放的绝大多数窗口期内实际效果相同，显式声明也让这条失败记账路径不再单方面依赖一个深埋在 `update_database` 内部、自己完全看不见的隐式兜底，并为未来这条路径上新增更多 Path B 调用点（目前只有 B3 一处）打好可复用的基础。

**Files**: `litellm/proxy/hooks/proxy_track_cost_callback.py`（改）, `litellm/proxy/db/db_spend_update_writer.py`（改）, `tests/test_litellm/proxy/hooks/test_proxy_track_cost_callback.py`（改，扩展既有文件）, `tests/test_litellm/proxy/db/test_db_spend_update_writer.py`（改，扩展既有文件 + 修正一处因本 Task 而失真的既有测试）

**站点确认（已读码）**：
- **B2**：`litellm/proxy/hooks/proxy_track_cost_callback.py:248`，在 `_ProxyDBLogger._PROXY_track_cost_callback` 的成功分支里，`await _update_database_and_spend_counters(...)` 之后、`await proxy_logging_obj.slack_alerting_instance.customer_spend_alert(...)` 之前，裸 `asyncio.create_task(update_cache(token=..., user_id=..., end_user_id=..., response_cost=..., team_id=..., parent_otel_span=..., tags=...))`。`update_cache`/`proxy_logging_obj` 都是函数体内部对 `litellm.proxy.proxy_server` 的惰性 import（第 180-181 行，为绕开 proxy_server 的循环 import，不是本 Task 要改的东西）。
- **B3**：`litellm/proxy/db/db_spend_update_writer.py:189`，在 `DBSpendUpdateWriter.update_database` 里，`await self._insert_spend_log_to_db(...)`（受 `disable_spend_logs` 开关控制）之后、`self._enqueue_tool_registry_upsert(...)` 之前，裸 `asyncio.create_task(self._batch_database_updates(response_cost=..., user_id=..., hashed_token=..., team_id=..., org_id=..., end_user_id=..., prisma_client=..., user_api_key_cache=..., litellm_proxy_budget_name=..., payload=...))`，注释写着"Single task replaces 11 create_task() calls"。

两处都满足"从 Path A/B1 已建立的同一条真实调用链路上、纯 `await` 到达，中间不跨 `asyncio.create_task` 边界"这个前提（`_PROXY_track_cost_callback`/`update_database` 都是作为 `CustomLogger.async_log_success_event` 回调，从 `dispatch_success_handlers`/`async_success_handler` 的回调遍历循环里被直接 `await` 调用——而 `dispatch_success_handlers` 本身，在标准 streaming 路径下由 Task 7 的 `spawn_detached` 创建；在非流式/Path A 路径下则由 Task 6 的 `create_task_with_scope` 创建），所以 `current_accounting_scope.get()` 在这两处能天然看到 Task 6/7 那次调用绑定的同一个 lease，不需要在 B2/B3 自己的调用点再做任何额外的显式传参。

**记录未采纳方案**：Task 4 最初的 Interfaces 里设想了一个专用入口 `ManagedTaskSupervisor.spawn_child(scope, coro, *, name)`（`scope.spawn(coro, name=name, kind="accounting")` 的一行委托），并预告"Task 8 的调用点直接用它"。真正落笔 Task 8 时发现这个专用入口是多余的重复实现：`spawn_detached`（Task 2）已经把"取 ambient scope，没有就 `acquire_root_scope()` 兜底获取一个全新 root scope，都没有就退化成裸 `asyncio.create_task`"这套解析逻辑完整实现了一遍，B2/B3 需要的行为和 B1（Task 7）**完全相同**——都是"这个调用点本身可能是 nested，也可能（万一 ambient 传递链路出问题）退化成了事实上的 root"，直接复用 `spawn_detached` 反而比"`isinstance(scope, AccountingLease)` 收窄 + 手写兜底到裸 `create_task`"更安全：后者一旦 ambient scope 因为某个未来的回归而没能传递下来，会静默退化成完全不被 `drain()` 追踪的裸任务；而 `spawn_detached` 的兜底会先尝试 `acquire_root_scope()`，只要 supervisor 还在运行，仍然能拿到一个可追踪的全新 root lease。因此没有采纳 `spawn_child` 这个专用入口，已经把它从 Task 4 的 Interfaces/实现/测试里整体移除（详见 Task 4 正文与"补充说明"），B2/B3 与 B1 三处都统一改用 `spawn_detached`。

**Files**: `litellm/proxy/hooks/proxy_track_cost_callback.py`（改）, `litellm/proxy/db/db_spend_update_writer.py`（改）, `tests/test_litellm/proxy/hooks/test_proxy_track_cost_callback.py`（改，扩展既有文件）, `tests/test_litellm/proxy/db/test_db_spend_update_writer.py`（改，扩展既有文件 + 修正一处因本 Task 而失真的既有测试）

**Steps**

1. 写失败测试。

`tests/test_litellm/proxy/hooks/test_proxy_track_cost_callback.py`（追加）：

```python
class TestTrackCostCallbackAccountingScope:
    @pytest.mark.asyncio
    async def test_update_cache_routes_through_spawn_detached_with_ambient_scope(self):
        """B2 (Phase 1b Task 8): once a real accounting lease is ambient on
        this task's Context (as it would be, threaded down from Task 6/7's
        root acquisition), the fire-and-forget update_cache call must be
        routed through spawn_detached instead of a bare asyncio.create_task,
        so ManagedTaskSupervisor.drain() can find and wait on it at shutdown
        instead of losing it to an untracked task."""
        from litellm.litellm_core_utils.accounting_scope import current_accounting_scope
        from litellm.proxy.shutdown.managed_task_supervisor import ManagedTaskSupervisor

        logger = _ProxyDBLogger()
        supervisor = ManagedTaskSupervisor()
        lease = supervisor.acquire_root_lease()
        scope_token = current_accounting_scope.set(lease)

        kwargs = {
            "call_type": "completion",
            "model": "gpt-4",
            "litellm_params": {"metadata": {"user_api_key": "hashed-key"}},
            "standard_logging_object": {
                "response_cost": 0.05,
                "request_tags": [],
                "metadata": {},
            },
        }

        scheduled = []

        def _capture(coro, *, name):
            scheduled.append((coro, name))
            coro.close()

        try:
            with (
                patch(
                    "litellm.proxy.proxy_server.increment_spend_counters",
                    new_callable=AsyncMock,
                ),
                patch(
                    "litellm.proxy.proxy_server.proxy_logging_obj",
                ) as mock_proxy_logging,
                patch(
                    "litellm.proxy.hooks.proxy_track_cost_callback.spawn_detached",
                    side_effect=_capture,
                ) as mock_spawn_detached,
            ):
                mock_proxy_logging.db_spend_update_writer.update_database = AsyncMock()
                mock_proxy_logging.slack_alerting_instance.customer_spend_alert = AsyncMock()

                await logger._PROXY_track_cost_callback(
                    kwargs=kwargs,
                    completion_response={"id": "call-1"},
                    start_time=datetime.now(),
                    end_time=datetime.now(),
                )
        finally:
            current_accounting_scope.reset(scope_token)

        mock_spawn_detached.assert_called_once()
        coro, name = scheduled[0]
        assert coro.cr_code.co_name == "update_cache"
        assert name == "update_cache"
```

（`_ProxyDBLogger`/`AsyncMock`/`MagicMock`/`patch`/`datetime`/`pytest` 均为本文件顶部既有 import，无需新增；沿用 `test_track_cost_callback_enriches_user_id_for_mcp_style_metadata` 已验证过的 `kwargs`/mock 形状，只额外加了 `current_accounting_scope` 绑定与对 `spawn_detached` 的替换断言。）

`tests/test_litellm/proxy/db/test_db_spend_update_writer.py`（追加）：

```python
class TestUpdateDatabaseAccountingScope:
    @pytest.mark.asyncio
    async def test_batch_database_updates_routes_through_spawn_detached_with_ambient_scope(self):
        """B3 (Phase 1b Task 8): same rationale as B2 -- update_database's
        fire-and-forget _batch_database_updates call must go through
        spawn_detached so an ambient accounting lease (threaded down from
        Task 6/7's root acquisition) tracks it for drain()."""
        from litellm.litellm_core_utils.accounting_scope import current_accounting_scope
        from litellm.proxy.shutdown.managed_task_supervisor import ManagedTaskSupervisor

        db_writer = DBSpendUpdateWriter()
        db_writer._insert_spend_log_to_db = AsyncMock()
        db_writer._batch_database_updates = AsyncMock()

        supervisor = ManagedTaskSupervisor()
        lease = supervisor.acquire_root_lease()
        scope_token = current_accounting_scope.set(lease)

        scheduled = []

        def _capture(coro, *, name):
            scheduled.append((coro, name))
            coro.close()

        try:
            with (
                patch("litellm.proxy.proxy_server.disable_spend_logs", False),
                patch("litellm.proxy.proxy_server.prisma_client", MagicMock()),
                patch("litellm.proxy.proxy_server.user_api_key_cache", MagicMock()),
                patch("litellm.proxy.proxy_server.litellm_proxy_budget_name", "test-budget"),
                patch(
                    "litellm.proxy.db.db_spend_update_writer.spawn_detached",
                    side_effect=_capture,
                ) as mock_spawn_detached,
            ):
                await db_writer.update_database(
                    token="test-token",
                    user_id="test-user",
                    end_user_id="test-end-user",
                    start_time=datetime.now(),
                    end_time=datetime.now(),
                    team_id="test-team",
                    org_id="test-org",
                    completion_response=MagicMock(),
                    response_cost=0.1,
                    kwargs={"model": "gpt-4", "custom_llm_provider": "openai"},
                )
        finally:
            current_accounting_scope.reset(scope_token)

        mock_spawn_detached.assert_called_once()
        coro, name = scheduled[0]
        assert name == "_batch_database_updates"
        db_writer._batch_database_updates.assert_called_once()
```

（这里刻意不像 B2 那样断言 `coro.cr_code.co_name`：B2 能用 `cr_code.co_name == "update_cache"` 是因为测试让*真实*的 `async def update_cache` 被调用产生协程；B3 为了隔离 `_batch_database_updates` 内部真正落库的逻辑，把它整个替换成了 `AsyncMock()`——用 `uv run python -c "..."` 现场验证过，`AsyncMock()` 被调用后拿到的协程，其 `cr_code.co_name` 恒为 `"_execute_mock_call"`，不是被赋值的属性名 `_batch_database_updates`，所以按 B2 的写法断言会必然失败。改用 `db_writer._batch_database_updates.assert_called_once()` 直接确认"产生 `coro` 的正是这次 `_batch_database_updates` 调用"，加上 `mock_spawn_detached.assert_called_once()` 与 `name == "_batch_database_updates"`，三者合起来已经能唯一确定"这个协程就是 `_batch_database_updates` 产生的、且是通过 `spawn_detached` 而非裸 `create_task` 派发的"，不需要也不能再依赖 `cr_code.co_name`。）

再修正既有 `test_update_database_creates_single_task`（第 1352 行）——本 Task 落地后，这条既有测试原本 patch 的 `litellm.proxy.db.db_spend_update_writer.asyncio.create_task` 不会再被这个调用点触碰到了：`spawn_detached` 内部真正调用 `asyncio.create_task` 时，引用的是 `accounting_scope.py` 自己模块命名空间里的 `asyncio`，不是 `db_spend_update_writer.py` 里的，`patch("litellm.proxy.db.db_spend_update_writer.asyncio.create_task")` 完全拦不到它——如果不修，这条既有测试会从"验证只建了 1 个 task"静默退化成"实际上从未真正跑到断言、`mock_create_task.call_count` 变成 0，却因为原断言写的是 `== 1` 而不是 `> 0`，会直接跑出一个看似合理但含义已经变了的失败"（这不是本 Task 引入的新 bug，是必须同步修的既有测试）：

```python
@pytest.mark.asyncio
async def test_update_database_creates_single_task():
    """
    Test that update_database() fires exactly 1 spawn_detached() call
    (the batched task) instead of the previous 11 create_task() calls.
    Phase 1b Task 8: this call site is spawn_detached-based, not a direct
    asyncio.create_task, so route through the module's own imported name.
    """
    db_writer = DBSpendUpdateWriter()

    # Mock all helpers so nothing real runs
    db_writer._insert_spend_log_to_db = AsyncMock()
    db_writer._batch_database_updates = AsyncMock()

    with (
        patch("litellm.proxy.proxy_server.disable_spend_logs", False),
        patch("litellm.proxy.proxy_server.prisma_client", MagicMock()),
        patch("litellm.proxy.proxy_server.user_api_key_cache", MagicMock()),
        patch("litellm.proxy.proxy_server.litellm_proxy_budget_name", "test-budget"),
        patch(
            "litellm.proxy.db.db_spend_update_writer.spawn_detached"
        ) as mock_spawn_detached,
    ):
        await db_writer.update_database(
            token="test-token",
            user_id="test-user",
            end_user_id="test-end-user",
            start_time=datetime.now(),
            end_time=datetime.now(),
            team_id="test-team",
            org_id="test-org",
            completion_response=MagicMock(),
            response_cost=0.1,
            kwargs={"model": "gpt-4", "custom_llm_provider": "openai"},
        )

        # Exactly 1 spawn_detached call (the batch), not 11 create_task calls
        assert mock_spawn_detached.call_count == 1
```

（这里没有传入协程实参就直接被 mock 拦下，`mock_spawn_detached` 是普通 `MagicMock`，不会真正执行/关闭内部协程——因为 `_batch_database_updates` 本身已经被 mock 成 `AsyncMock()`，`db_writer._batch_database_updates(...)` 调用产生的是一个 mock 协程对象，未被 `await` 也未被 `.close()` 不会有真实 `RuntimeWarning: coroutine was never awaited`——`AsyncMock()` 生成的 mock 协程在这方面是安全的，这与既有测试改动前的行为一致，本次修正不新增风险。）

2. 确认失败：
   - `pytest tests/test_litellm/proxy/hooks/test_proxy_track_cost_callback.py -v -k accounting_scope`——`AttributeError: <module 'litellm.proxy.hooks.proxy_track_cost_callback'> does not have the attribute 'spawn_detached'`。
   - `pytest tests/test_litellm/proxy/db/test_db_spend_update_writer.py -v -k accounting_scope`——同类 `AttributeError`。
   - 单独跑一次改过的 `test_update_database_creates_single_task`：`AttributeError`（同上，`spawn_detached` 还未导入）。

3. 实现。

`litellm/proxy/hooks/proxy_track_cost_callback.py`：

```python
# 顶部 import 追加（第 34 行 `from litellm.utils import ...` 之后）
from litellm.litellm_core_utils.accounting_scope import spawn_detached
```

```python
# 替换第 248-258 行
spawn_detached(
    update_cache(
        token=user_api_key,
        user_id=user_id,
        end_user_id=end_user_id,
        response_cost=response_cost,
        team_id=team_id,
        parent_otel_span=parent_otel_span,
        tags=tags,
    ),
    name="update_cache",
)
```

`litellm/proxy/db/db_spend_update_writer.py`：

```python
# 顶部 import 追加
from litellm.litellm_core_utils.accounting_scope import spawn_detached
```

```python
# 替换第 189-202 行
spawn_detached(
    self._batch_database_updates(
        response_cost=response_cost,
        user_id=user_id,
        hashed_token=hashed_token,
        team_id=team_id,
        org_id=org_id,
        end_user_id=end_user_id,
        prisma_client=prisma_client,
        user_api_key_cache=user_api_key_cache,
        litellm_proxy_budget_name=litellm_proxy_budget_name,
        payload=payload,
    ),
    name="_batch_database_updates",
)
```

4. 确认转绿：
   - `pytest tests/test_litellm/proxy/hooks/test_proxy_track_cost_callback.py -v`——全量跑一次该文件（1179+ 行），确认新测试通过且 `test_track_cost_callback_enriches_user_id_for_mcp_style_metadata` 等既有用例（本身没有绑定 `current_accounting_scope`，所以走 `spawn_detached` 的兜底裸 `create_task` 分支，行为逐字节不变）仍然全绿。
   - `pytest tests/test_litellm/proxy/db/test_db_spend_update_writer.py -v`——全量跑一次该文件，确认新测试与修正后的 `test_update_database_creates_single_task` 都通过。
   - `pytest tests/test_litellm/proxy/shutdown/test_managed_task_supervisor.py -v`——确认 Task 4 补丁（全局单例注册 + scope 绑定）没有被本 Task 的改动破坏。

5. 提交：`git add litellm/proxy/hooks/proxy_track_cost_callback.py litellm/proxy/db/db_spend_update_writer.py tests/test_litellm/proxy/hooks/test_proxy_track_cost_callback.py tests/test_litellm/proxy/db/test_db_spend_update_writer.py && git commit -m "feat: route update_cache/_batch_database_updates through spawn_detached (Path B2/B3)"`

（`managed_task_supervisor.py`/`accounting_outcome.py`/`test_managed_task_supervisor.py` 不在本 Task 的提交范围内——Task 4"补充说明"里记录的两处补丁，文字上是"Task 8 落笔时才发现"，但已经直接改写进了 Task 4 自己的 Steps §3 正文，实际执行本计划的人在执行 Task 4 时就会写出已经修正过的版本、随 Task 4 自己的commit 一并提交；这里的"补充说明"只是如实记录*撰写*这份计划过程中的发现顺序，不代表*执行*这份计划时还需要一次单独的 Task 4 追加提交。）

### Task 8 续 — B4：`async_post_call_failure_hook` 记账接入（本轮评审 blocker 4）

依赖上面已经落笔的 Task 2 `ambient_or_root_scope()`（本 Task 复用，不重新实现 is_ambient 判定）。

6. 写失败测试（追加到 `tests/test_litellm/proxy/hooks/test_proxy_track_cost_callback.py`，复用文件顶部已有的 `_ProxyDBLogger`/`UserAPIKeyAuth`/`AsyncMock`/`MagicMock`/`patch`/`pytest` import；`Usage` 沿用 `test_async_post_call_failure_hook_records_recovered_partial_spend` 已经确立的 `from litellm.types.utils import Usage` 局部导入方式）。三个场景对应评审要求的"stream interruption / provider failure / post-auth failure"，验证手法相同：patch 掉 `update_database`，在替身实现里读一次 `current_accounting_scope.get()`，断言读到的正是 `register_root_scope_provider` 注入的假 root token，且这条 token 在整个 `async_post_call_failure_hook` 调用结束后被 `settle()` 恰好一次：

```python
class TestAsyncPostCallFailureHookAccountingScope:
    """blocker 4（本轮评审）：async_post_call_failure_hook 必须自己在入口
    acquire scope、在出口 settle，而不是被动依赖 update_database 内部
    spawn_detached 那次调用点自己的隐式 ad hoc 兜底。三个场景覆盖评审列出的
    stream interruption / provider failure / post-auth failure，机制完全相同
    （async_post_call_failure_hook 本身不按异常类型分支 scope 处理逻辑），
    分别验证是因为它们是评审明确要求覆盖的、真实会触达这条路径的三种触发方式。
    """

    @staticmethod
    def _make_fake_root_token():
        class _FakeRootToken:
            def __init__(self):
                self.settled = []

            def is_valid(self):
                return True

            def spawn(self, coro, *, name, kind):
                coro.close()

            def settle(self, outcome):
                self.settled.append(outcome)

        return _FakeRootToken()

    @pytest.mark.asyncio
    async def test_stream_interruption_threads_ambient_scope_into_update_database(self):
        from litellm.litellm_core_utils.accounting_scope import (
            current_accounting_scope,
            register_root_scope_provider,
        )
        from litellm.types.utils import Usage

        fake_token = self._make_fake_root_token()
        seen_scope_during_call = []

        async def _fake_update_database(**kwargs):
            seen_scope_during_call.append(current_accounting_scope.get())

        logger = _ProxyDBLogger()
        user_api_key_dict = UserAPIKeyAuth(api_key="test_api_key", user_id="u", team_id="t")
        request_data = {
            "model": "anthropic/claude-haiku-4-5",
            "messages": [{"role": "user", "content": "Hello"}],
            "metadata": {},
            "proxy_server_request": {"request_id": "rid"},
            "response_cost": 3.5e-05,
            "combined_usage_object": Usage(prompt_tokens=30, completion_tokens=1, total_tokens=31),
        }

        register_root_scope_provider(lambda: fake_token)
        try:
            with patch(
                "litellm.proxy.db.db_spend_update_writer.DBSpendUpdateWriter.update_database",
                side_effect=_fake_update_database,
            ):
                await logger.async_post_call_failure_hook(
                    request_data=request_data,
                    original_exception=Exception("MidStreamFallbackError: read timeout"),
                    user_api_key_dict=user_api_key_dict,
                )
        finally:
            register_root_scope_provider(None)

        assert seen_scope_during_call == [fake_token]
        assert len(fake_token.settled) == 1
        assert fake_token.settled[0].kind == "completed"

    @pytest.mark.asyncio
    async def test_provider_failure_threads_ambient_scope_into_update_database(self):
        from litellm.litellm_core_utils.accounting_scope import (
            current_accounting_scope,
            register_root_scope_provider,
        )

        fake_token = self._make_fake_root_token()
        seen_scope_during_call = []

        async def _fake_update_database(**kwargs):
            seen_scope_during_call.append(current_accounting_scope.get())

        logger = _ProxyDBLogger()
        user_api_key_dict = UserAPIKeyAuth(
            api_key="test_api_key",
            key_alias="test_alias",
            user_id="test_user_id",
            team_id="test_team_id",
            org_id="test_org_id",
        )
        request_data = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hello"}],
            "metadata": {"original_key": "original_value"},
            "proxy_server_request": {"request_id": "test_request_id"},
        }

        register_root_scope_provider(lambda: fake_token)
        try:
            with patch(
                "litellm.proxy.db.db_spend_update_writer.DBSpendUpdateWriter.update_database",
                side_effect=_fake_update_database,
            ):
                await logger.async_post_call_failure_hook(
                    request_data=request_data,
                    original_exception=Exception("APIConnectionError: upstream provider unreachable"),
                    user_api_key_dict=user_api_key_dict,
                )
        finally:
            register_root_scope_provider(None)

        assert seen_scope_during_call == [fake_token]
        assert len(fake_token.settled) == 1
        assert fake_token.settled[0].kind == "completed"

    @pytest.mark.asyncio
    async def test_post_auth_failure_threads_ambient_scope_into_update_database(self):
        """post-auth failure（401，仅 api_key 已知）：沿用
        test_async_post_call_failure_hook_enriches_auth_error_metadata 已确立的
        mock 形状（get_key_object/get_team_object 惰性 import 需要一并 patch），
        额外验证 scope acquire/settle。"""
        from litellm.litellm_core_utils.accounting_scope import (
            current_accounting_scope,
            register_root_scope_provider,
        )

        fake_token = self._make_fake_root_token()
        seen_scope_during_call = []

        async def _fake_update_database(**kwargs):
            seen_scope_during_call.append(current_accounting_scope.get())

        logger = _ProxyDBLogger()
        user_api_key_dict = UserAPIKeyAuth(api_key="hashed_key")
        request_data = {
            "model": "claude-haiku-4-5",
            "messages": [{"role": "user", "content": "Hello"}],
            "metadata": {},
            "litellm_params": {},
        }

        mock_key_obj = MagicMock()
        mock_key_obj.key_alias = "my-key-alias"
        mock_key_obj.user_id = "my-user-id"
        mock_key_obj.team_id = "my-team-id"
        mock_key_obj.org_id = None

        mock_team_obj = MagicMock()
        mock_team_obj.team_alias = "my-team-alias"

        register_root_scope_provider(lambda: fake_token)
        try:
            with (
                patch(
                    "litellm.proxy.db.db_spend_update_writer.DBSpendUpdateWriter.update_database",
                    side_effect=_fake_update_database,
                ),
                patch(
                    "litellm.proxy.hooks.proxy_track_cost_callback.get_key_object",
                    new_callable=AsyncMock,
                    return_value=mock_key_obj,
                ),
                patch(
                    "litellm.proxy.hooks.proxy_track_cost_callback.get_team_object",
                    new_callable=AsyncMock,
                    return_value=mock_team_obj,
                ),
            ):
                await logger.async_post_call_failure_hook(
                    request_data=request_data,
                    original_exception=Exception("401 - model not allowed"),
                    user_api_key_dict=user_api_key_dict,
                )
        finally:
            register_root_scope_provider(None)

        assert seen_scope_during_call == [fake_token]
        assert len(fake_token.settled) == 1
        assert fake_token.settled[0].kind == "completed"
```

7. 确认失败：对三个新测试各跑一次 `pytest tests/test_litellm/proxy/hooks/test_proxy_track_cost_callback.py -v -k accounting_scope`——均因 `seen_scope_during_call == [None]`（`async_post_call_failure_hook` 从未 acquire 任何 scope，`current_accounting_scope.get()` 恒为 `None`）而 `AssertionError`，且 `fake_token.settled == []`（从未被调用过）。

8. 实现。`litellm/proxy/hooks/proxy_track_cost_callback.py`：

```python
# 顶部 import 追加
from litellm.litellm_core_utils.accounting_scope import ambient_or_root_scope
```

```python
# async_post_call_failure_hook 整个既有方法体（第 48-168 行，从 "try: await _release_budget_reservation(...)"
# 到最后一次 "await proxy_logging_obj.db_spend_update_writer.update_database(...)"）
# 缩进一级，套进这一层 async with；方法体内部逻辑不做任何其他改动
async def async_post_call_failure_hook(
    self,
    request_data: dict,
    original_exception: Exception,
    user_api_key_dict: UserAPIKeyAuth,
    traceback_str: Optional[str] = None,
):
    async with ambient_or_root_scope():
        try:
            await _release_budget_reservation(budget_reservation=user_api_key_dict.budget_reservation)
        except Exception:
            ...  # 既有异常处理逻辑不变
        # ... 既有方法体其余部分原样保留，只是多缩进一级 ...
```

（这里没有引入任何新的分支或异常处理路径——`async with ambient_or_root_scope():` 本身在 `__aexit__` 里已经处理了正常返回与异常两种收尾情形，见 Task 2 Steps §3 的 `try/finally`，方法体内部原有的 `try/except` 结构不受影响、无需改动。）

9. 确认转绿：
   - `pytest tests/test_litellm/proxy/hooks/test_proxy_track_cost_callback.py -v`——全量跑一次该文件，确认三个新测试与全部既有 `async_post_call_failure_hook` 相关测试（`test_async_post_call_failure_hook`、`test_async_post_call_failure_hook_non_llm_route`、`test_async_post_call_failure_hook_releases_budget_reservation_before_route_skip`、`test_async_post_call_failure_hook_propagates_trace_id_from_logging_obj`、`test_async_post_call_failure_hook_enriches_auth_error_metadata`、`test_async_post_call_failure_hook_enriches_missing_team_alias`、`test_async_post_call_failure_hook_uses_actual_start_time`、`test_async_post_call_failure_hook_records_recovered_partial_spend` 等）都保持全绿——这些既有测试都没有注入 `register_root_scope_provider`，所以走 `ambient_or_root_scope()` 的"无 provider、无 ambient scope"分支（`acquire_root_scope()` 返回 `None`，`yield None`，`finally` 里两个 `if` 都不成立），方法体本身逐字节行为不变。
   - `pytest tests/test_litellm/litellm_core_utils/test_accounting_scope.py -v`——确认 Task 2 新增的 `ambient_or_root_scope` 测试仍然全绿，没有被本 Task 的真实调用点破坏。

10. 提交：`git add litellm/proxy/hooks/proxy_track_cost_callback.py tests/test_litellm/proxy/hooks/test_proxy_track_cost_callback.py && git commit -m "feat: acquire and settle an explicit accounting scope around async_post_call_failure_hook (Path B4)"`

---

## Task 9 — Path B telemetry T1-T18 接入：`ManagedTaskSupervisor.spawn_telemetry`

**依赖**：Task 4（`GLOBAL_MANAGED_TASK_SUPERVISOR` 单例 + `spawn_telemetry`）。**不**依赖 Task 6/7/8——telemetry 站点不读 `current_accounting_scope`，不需要任何 ambient scope 存在即可派发（这正是它与 accounting 站点的本质区别：`spawn_telemetry` 内部就是无条件的 `asyncio.create_task` + 记入 `self._telemetry` 集合，deadline 到期直接 `cancel_all()`，见 Task 4 `_spawn_telemetry_child` 实现）。

**站点清单**：见 Architecture 一节表格（T1-T18，18 个站点，本 Task 落笔时从 14 个修正为 18 个——见该表格下方"设计说明"如实记录的发现过程）。

**改法统一**：每个站点把 `asyncio.create_task(coro)` 原样替换为 `GLOBAL_MANAGED_TASK_SUPERVISOR.spawn_telemetry(coro, name="<语义名>")`，`coro` 表达式本身不变（不改被调用的函数/参数），只包一层。`name=` 取该调用最能辨识意图的短语（如 `"budget_alerts:token_budget"`、`"budget_alerts:proxy_budget"`——同一 `budget_alerts` 在不同调用点传的 `type=` 不同，`name=` 里带上 `type` 值，方便 `drain()` 到期时 `cancelled` 日志能定位是哪一种预算告警卡住了），而不是不带区分度的统一 `"budget_alerts"`。

**测试策略（如实记录的取舍，非静默削减）**：T1-T18 里，T4-T12（9 个）与 T14-T15（2 个）都是完全同构的 `asyncio.create_task(proxy_logging_obj.budget_alerts(type=..., user_info=...))` 一行替换；既有测试（如 `test_virtual_key_budget_check_reads_from_spend_counter`）本身就不 mock/断言 `create_task` 机制，只 mock `budget_alerts` 本身并断言业务副作用（预算超限异常/告警内容），这些既有测试在替换后不需要改动、必须继续全绿——因为 `spawn_telemetry` 的调度路径与裸 `create_task` 对调用方外部可观察行为逐字节相同（依然是"排入事件循环、fire-and-forget、不等待"）。给这 11 个同构站点每一个都各写一份"断言走 `spawn_telemetry`"的新测试，是对同一机制的重复验证、不产生新增信号；因此本 Task 只给每种**不同调用形状**各写一个新测试（T1、T2、T3、代表 T4-T12 的 T4、T13、代表 T14-T15 的 T14、代表 T16-T17 的 T16、T18，共 8 个新测试，覆盖全部 6 种不同的调用形状：`failed_tracking_alert` / `budget_alerts`(auth_checks.py 内) / `async_set_cache_pipeline` / SSO 回写 / `budget_alerts`(user_api_key_auth.py 内) / `_cache_key_object` / `async_service_success_hook`），其余 10 个同构站点（T5-T12、T15、T17）改动后用第 4 步"确认转绿"里的仓库级 `grep` 断言 + 既有测试套件全绿作为验证——`grep` 能可靠确认"这一行确实从 `asyncio.create_task(` 变成了 `GLOBAL_MANAGED_TASK_SUPERVISOR.spawn_telemetry(`"，比再写 10 份结构相同的 mock 断言更能防止"改了 A 忘了改 B"这类真实风险，且不产生虚假的测试数量。

1. 写失败测试。

`tests/test_litellm/proxy/hooks/test_proxy_track_cost_callback.py`（追加，T1）：

```python
class TestTrackCostCallbackTelemetryScope:
    @pytest.mark.asyncio
    async def test_failed_tracking_alert_routes_through_spawn_telemetry(self):
        """T1 (Phase 1b Task 9): the failure-path alert is best-effort telemetry,
        not accounting -- it must be tracked by the supervisor's cancelable
        _telemetry set (so a deadline-exceeded drain can cancel it outright)
        rather than an untracked bare asyncio.create_task."""
        logger = _ProxyDBLogger()

        scheduled = []

        def _capture(coro, *, name):
            scheduled.append((coro, name))
            coro.close()

        with (
            patch(
                "litellm.proxy.hooks.proxy_track_cost_callback.GLOBAL_MANAGED_TASK_SUPERVISOR"
            ) as mock_supervisor,
            patch(
                "litellm.proxy.proxy_server.proxy_logging_obj",
            ) as mock_proxy_logging,
        ):
            mock_supervisor.spawn_telemetry.side_effect = _capture
            mock_proxy_logging.failed_tracking_alert = AsyncMock()

            await logger._PROXY_track_cost_callback(
                kwargs={"call_type": "completion"},  # missing model -> triggers except branch
                completion_response=None,
                start_time=datetime.now(),
                end_time=datetime.now(),
            )

        mock_supervisor.spawn_telemetry.assert_called_once()
        _, name = scheduled[0]
        assert name == "failed_tracking_alert"
```

（沿用同文件既有测试对 `_PROXY_track_cost_callback` 触发 `except` 分支的构造方式——传缺 `model`/`standard_logging_object` 的 `kwargs` 即可命中 `raise Exception(...)` 再被外层 `except Exception as e` 捕获。）

`tests/test_litellm/proxy/test_proxy_server.py`（若该文件尚无 `update_cache` 相关测试类，追加到文件末尾；T2/T3）：

```python
class TestUpdateCacheTelemetryScope:
    @pytest.mark.asyncio
    async def test_budget_alert_and_cache_pipeline_route_through_spawn_telemetry(self):
        """T2/T3 (Phase 1b Task 9): both the projected-budget alert and the
        cache-pipeline write inside update_cache() are best-effort telemetry
        nested inside the accounting-tracked update_cache coroutine (B2) --
        they must go through spawn_telemetry so a deadline-exceeded drain can
        cancel them without blocking on update_cache's own accounting lease."""
        scheduled = []

        def _capture(coro, *, name):
            scheduled.append((coro, name))
            coro.close()

        existing_spend_obj = MagicMock()
        existing_spend_obj.soft_budget_cooldown = False
        existing_spend_obj.soft_budget = 1.0
        existing_spend_obj.spend = 0.0
        existing_spend_obj.token = "hashed-token"
        existing_spend_obj.key_alias = "alias"
        existing_spend_obj.user_id = "user-1"
        existing_spend_obj.team_spend = None
        existing_spend_obj.team_member_spend = None

        mock_user_api_key_cache = MagicMock()
        mock_user_api_key_cache.async_get_cache = AsyncMock(return_value=existing_spend_obj)
        mock_user_api_key_cache.async_set_cache_pipeline = AsyncMock()

        with (
            patch(
                "litellm.proxy.proxy_server.GLOBAL_MANAGED_TASK_SUPERVISOR"
            ) as mock_supervisor,
            patch(
                "litellm.proxy.proxy_server._is_projected_spend_over_limit",
                return_value=True,
            ),
            patch(
                "litellm.proxy.proxy_server._get_projected_spend_over_limit",
                return_value=(2.0, "2026-01-01"),
            ),
            patch("litellm.proxy.proxy_server.user_api_key_cache", mock_user_api_key_cache),
        ):
            mock_supervisor.spawn_telemetry.side_effect = _capture

            # token deliberately does not start with "sk-" so _update_key_cache
            # uses it as the hashed_token verbatim (see proxy_server.py's
            # `if isinstance(token, str) and token.startswith("sk-")` branch).
            await update_cache(
                token="hashed-token",
                user_id=None,
                end_user_id=None,
                team_id=None,
                response_cost=0.5,
                parent_otel_span=None,
            )

        names = [name for _, name in scheduled]
        assert "budget_alerts:projected_limit_exceeded" in names
        assert "async_set_cache_pipeline" in names
```

（已对照 `proxy_server.py:2697-2992` 的 `update_cache` 真实实现核对过：`_update_key_cache` 内部从 `user_api_key_cache.async_get_cache(key=hashed_token, model_type=UserAPIKeyAuth)` 取 `existing_spend_obj`，`token` 不以 `"sk-"` 开头时 `hashed_token` 就是 `token` 本身；T3 的 `async_set_cache_pipeline` 在函数尾部无条件触发，不依赖 T2 的软预算分支是否命中。上面的 mock 挂载点已按此核对结果写就，不是未核实的骨架占位。）

`tests/test_litellm/proxy/auth/test_auth_checks.py`（追加，代表 T4）：

```python
class TestVirtualKeyMaxBudgetCheckTelemetryScope:
    @pytest.mark.asyncio
    async def test_budget_alert_routes_through_spawn_telemetry(self):
        """T4 (Phase 1b Task 9), representative of T4-T12: all 9 budget_alerts
        call sites in this file follow this identical one-line substitution;
        see Task 9's grep-based verification step for the other 8."""
        from litellm.proxy.utils import ProxyLogging

        valid_token = UserAPIKeyAuth(
            token="test-hashed-token",
            spend=0.0,
            max_budget=1.0,
            user_id="test-user",
        )
        proxy_logging_obj = ProxyLogging(user_api_key_cache=None)
        proxy_logging_obj.budget_alerts = AsyncMock()

        scheduled = []

        def _capture(coro, *, name):
            scheduled.append((coro, name))
            coro.close()

        async def mock_get_current_spend(counter_key, fallback_spend, max_budget=None, **kwargs):
            return 1.5

        with (
            patch("litellm.proxy.proxy_server.get_current_spend", mock_get_current_spend),
            patch(
                "litellm.proxy.auth.auth_checks.GLOBAL_MANAGED_TASK_SUPERVISOR"
            ) as mock_supervisor,
        ):
            mock_supervisor.spawn_telemetry.side_effect = _capture
            with pytest.raises(litellm.BudgetExceededError):
                await _virtual_key_max_budget_check(
                    valid_token=valid_token,
                    proxy_logging_obj=proxy_logging_obj,
                )

        mock_supervisor.spawn_telemetry.assert_called_once()
        _, name = scheduled[0]
        assert name == "budget_alerts:token_budget"
```

`tests/test_litellm/proxy/auth/test_auth_checks.py`（追加，T13）：

```python
class TestSsoUserIdBackfillTelemetryScope:
    @pytest.mark.asyncio
    async def test_sso_user_id_backfill_routes_through_spawn_telemetry(self):
        """T13 (Phase 1b Task 9): the SSO user_id backfill is a best-effort
        background DB write triggered from the auth path, structurally
        identical to the budget_alerts sites -- same fix, different call
        shape (a table.update(...) coroutine instead of budget_alerts).
        Function under test is `_get_fuzzy_user_object` (auth_checks.py:1536);
        find_unique must return None so the lookup falls through to the
        find_first-by-email branch, which is the only branch that reaches
        the sso_user_id backfill create_task (auth_checks.py:1568)."""
        scheduled = []

        def _capture(coro, *, name):
            scheduled.append((coro, name))
            coro.close()

        mock_table = MagicMock()
        mock_table.find_unique = AsyncMock(return_value=None)
        mock_table.find_first = AsyncMock(
            return_value=MagicMock(user_id="existing-user-id")
        )
        mock_table.update = AsyncMock()

        with (
            patch(
                "litellm.proxy.auth.auth_checks.UserRepository"
            ) as mock_user_repo_cls,
            patch(
                "litellm.proxy.auth.auth_checks.GLOBAL_MANAGED_TASK_SUPERVISOR"
            ) as mock_supervisor,
        ):
            mock_user_repo_cls.return_value.table = mock_table
            mock_supervisor.spawn_telemetry.side_effect = _capture

            await _get_fuzzy_user_object(
                prisma_client=MagicMock(),
                sso_user_id="sso-id-123",
                user_email="user@example.com",
            )

        mock_supervisor.spawn_telemetry.assert_called_once()
        _, name = scheduled[0]
        assert name == "sso_user_id_backfill"
```

（已对照 `auth_checks.py:1536-1571` 的 `_get_fuzzy_user_object` 真实实现核对：`sso_user_id` 非空时会先走 `find_unique`，必须让它返回 `None` 才会落到 `find_first` 分支，兼具 `response is not None` 与 `sso_user_id is not None` 两个条件才触发 T13 那次 `create_task`；上面的 mock 已按此核对结果写就。）

`tests/test_litellm/proxy/auth/test_user_api_key_auth.py`（追加，代表 T14）：

```python
class TestGlobalProxySpendTelemetryScope:
    @pytest.mark.asyncio
    async def test_proxy_budget_alert_routes_through_spawn_telemetry(self):
        """T14 (Phase 1b Task 9), representative of T14-T15."""
        from litellm.proxy.auth.user_api_key_auth import get_global_proxy_spend

        scheduled = []

        def _capture(coro, *, name):
            scheduled.append((coro, name))
            coro.close()

        proxy_logging_obj = MagicMock()
        proxy_logging_obj.budget_alerts = AsyncMock()

        with (
            patch("litellm.max_budget", 1.0),
            patch(
                "litellm.proxy.auth.user_api_key_auth._fetch_global_spend_with_event_coordination",
                new=AsyncMock(return_value=5.0),
            ),
            patch(
                "litellm.proxy.auth.user_api_key_auth.GLOBAL_MANAGED_TASK_SUPERVISOR"
            ) as mock_supervisor,
        ):
            mock_supervisor.spawn_telemetry.side_effect = _capture

            await get_global_proxy_spend(
                litellm_proxy_admin_name="admin",
                user_api_key_cache=MagicMock(),
                prisma_client=MagicMock(),
                token="test-token",
                proxy_logging_obj=proxy_logging_obj,
            )

        mock_supervisor.spawn_telemetry.assert_called_once()
        _, name = scheduled[0]
        assert name == "budget_alerts:proxy_budget"
```

（`patch("litellm.max_budget", 1.0)`：`user_api_key_auth.py:23` 顶部是 `import litellm`（模块级共享单例引用），`get_global_proxy_spend` 内 `litellm.max_budget > 0` 读的就是这同一个 `litellm` 包对象的属性，故直接 `patch("litellm.max_budget", ...)` 即可命中，不需要按每个引用它的模块分别 patch。`_fetch_global_spend_with_event_coordination` 的 mock 形状 `AsyncMock(return_value=5.0)` 也已对照真实源码核实：该函数（`user_api_key_auth.py:493-513`）直接 `return await _global_spend_coordinator.get_or_load(...)`，声明返回类型是 `Optional[float]`，与这里打的桩形状一致，无需改动。）

`tests/test_litellm/proxy/auth/test_user_api_key_auth.py`（追加，代表 T16）：

```python
class TestCacheKeyObjectTelemetryScope:
    @pytest.mark.asyncio
    async def test_cache_key_object_routes_through_spawn_telemetry(self):
        """T16 (Phase 1b Task 9), representative of T16-T17: post-auth cache
        warm is best-effort telemetry, not accounting -- safe to cancel at
        shutdown deadline instead of blocking drain on it.

        Drives the *master-key* branch (user_api_key_auth.py:1597) rather than
        T17's non-master-key branch (:1996), because the master-key branch is
        reachable without any prisma_client/DB setup: _return_user_api_key_auth_obj
        (called just before the _cache_key_object create_task at line 1597) builds
        its UserAPIKeyAuth purely from in-memory kwargs. T17 is structurally the
        exact same one-line substitution (asyncio.create_task(_cache_key_object(...))
        -> GLOBAL_MANAGED_TASK_SUPERVISOR.spawn_telemetry(_cache_key_object(...), name=...))
        applied to a second call site inside the same function; the repo-wide grep
        check in Step 4 below is the completeness backstop for T17 (see the
        test-economy rationale above this Task's test list).

        To reach line 1597 without hitting the PROXY_ADMIN cache-hit fast path at
        line 1540 (which returns *before* ever reaching the master-key check),
        `valid_token` from the cache lookup must resolve to None -- IdentityStore
        is patched to raise on `.resolve(...)`, which the real code already wraps
        in a bare `except Exception: valid_token = None` (auth builder's own
        "Check CACHE" block), so cache-miss is a legitimate, already-handled path,
        not a hack around the function's contract.
        """
        from fastapi import Request
        from starlette.datastructures import URL

        from litellm.proxy.auth.user_api_key_auth import _user_api_key_auth_builder

        scheduled = []

        def _capture(coro, *, name):
            scheduled.append((coro, name))
            coro.close()

        mock_cache = AsyncMock()
        mock_cache.async_get_cache = AsyncMock(return_value=None)

        mock_identity_store_instance = MagicMock()
        mock_identity_store_instance.resolve = AsyncMock(
            side_effect=Exception("cache miss")
        )

        import litellm.proxy.proxy_server as _proxy_server_mod

        _attrs_to_set = {
            "prisma_client": MagicMock(),
            "user_api_key_cache": mock_cache,
            "proxy_logging_obj": MagicMock(),
            "master_key": "sk-master-key",
            "general_settings": {},
            "llm_model_list": [],
            "llm_router": None,
            "open_telemetry_logger": None,
            "model_max_budget_limiter": MagicMock(),
            "user_custom_auth": None,
            "jwt_handler": None,
            "litellm_proxy_admin_name": "admin",
        }
        _original_values = {
            attr: getattr(_proxy_server_mod, attr, None) for attr in _attrs_to_set
        }
        try:
            for attr, val in _attrs_to_set.items():
                setattr(_proxy_server_mod, attr, val)

            request = Request(scope={"type": "http"})
            request._url = URL(url="/chat/completions")

            with (
                patch(
                    "litellm.proxy.auth.user_api_key_auth.IdentityStore",
                    return_value=mock_identity_store_instance,
                ),
                patch(
                    "litellm.proxy.auth.user_api_key_auth.GLOBAL_MANAGED_TASK_SUPERVISOR"
                ) as mock_supervisor,
            ):
                mock_supervisor.spawn_telemetry.side_effect = _capture

                await _user_api_key_auth_builder(
                    request=request,
                    api_key="Bearer sk-master-key",
                    azure_api_key_header="",
                    anthropic_api_key_header=None,
                    google_ai_studio_api_key_header=None,
                    azure_apim_header=None,
                    request_data={},
                )
        finally:
            for attr, val in _original_values.items():
                setattr(_proxy_server_mod, attr, val)

        # _return_user_api_key_auth_obj (called just before line 1597) also
        # schedules T18's async_service_success_hook via spawn_telemetry, so
        # this call site is exercised twice per request -- filter by name
        # rather than asserting call count 1.
        names = [name for _, name in scheduled]
        assert "cache_key_object" in names
```

（这个测试驱动的是 T16 本身（第 1597 行，master-key 分支），而非 T17（第 1996 行，非 master-key 分支）；已对照真实源码核实：master-key 分支在 `valid_token` 为 `None`（缓存未命中）时，从 `_return_user_api_key_auth_obj` 构建纯内存对象、无需 `prisma_client` 真实可用即可走到第 1597 行并 `return`，是全文件里能最干净独立触发到 `_cache_key_object` 调用点的路径；T17 是同一函数内结构完全相同的第二处一次性替换，按第 4 步的仓库级 `grep` 检查兜底，不重复造一份同构测试。

这套 mock 组合已针对**当前（Task 9 实现前）代码**实际跑过一次 PoC：把上面测试里 `patch(".../GLOBAL_MANAGED_TASK_SUPERVISOR")` 换成 `patch(".../asyncio.create_task", side_effect=_capture)`（因为 `GLOBAL_MANAGED_TASK_SUPERVISOR` 这个名字要等 Task 9 实现后才会被导入进 `user_api_key_auth.py`，现在 patch 它会直接 `AttributeError`），实际执行 `_user_api_key_auth_builder(...)` 后确认：函数正常返回一个 `user_role=PROXY_ADMIN` 的 `UserAPIKeyAuth`，且 `create_task` 被调用两次，其协程分别是 `_cache_key_object`（T16）与 `async_service_success_hook`（T18）——与本测试断言的调用形状完全一致。Task 9 实现后，这两处都会变成 `spawn_telemetry(..., name=...)`，测试断言的 `name in names` 检查方式不变。）

`tests/test_litellm/proxy/auth/test_user_api_key_auth.py`（追加，T18）：

```python
class TestServiceSuccessHookTelemetryScope:
    @pytest.mark.asyncio
    async def test_service_success_hook_routes_through_spawn_telemetry(self):
        """T18 (Phase 1b Task 9): spec §B explicitly names 'service logging
        hooks' as telemetry -- this is that exact call site."""
        from litellm.proxy.auth.user_api_key_auth import _return_user_api_key_auth_obj

        user_obj = type(
            "LiteLLM_UserTable",
            (),
            {
                "tpm_limit": None,
                "rpm_limit": None,
                "user_email": None,
                "spend": 0.0,
                "max_budget": None,
                "user_role": "internal_user",
            },
        )

        scheduled = []

        def _capture(coro, *, name):
            scheduled.append((coro, name))
            coro.close()

        with patch(
            "litellm.proxy.auth.user_api_key_auth.GLOBAL_MANAGED_TASK_SUPERVISOR"
        ) as mock_supervisor:
            mock_supervisor.spawn_telemetry.side_effect = _capture

            await _return_user_api_key_auth_obj(
                user_obj=user_obj,
                api_key="sk-test-key",
                parent_otel_span=None,
                valid_token_dict={"user_id": "test-user"},
                route="/chat/completions",
                start_time=datetime.now(),
                user_role=None,
            )

        mock_supervisor.spawn_telemetry.assert_called_once()
        _, name = scheduled[0]
        assert name == "async_service_success_hook"
```

2. 确认失败：以上 8 个新测试此刻应因 `GLOBAL_MANAGED_TASK_SUPERVISOR` 尚未在对应模块被 import/patch（`patch(...)` 找不到该属性会抛 `AttributeError`）或 `spawn_telemetry` 从未被调用（`assert_called_once()` 失败）而红。

3. 实现（每个文件顶部加一行 import，18 处调用点各自把 `asyncio.create_task(` 替换为 `GLOBAL_MANAGED_TASK_SUPERVISOR.spawn_telemetry(`，并给每个补上先前不存在的 `name=` 关键字参数）：

```python
# 4 个文件顶部均追加
from litellm.proxy.shutdown.managed_task_supervisor import GLOBAL_MANAGED_TASK_SUPERVISOR
```

```python
# proxy_track_cost_callback.py:305（T1）
GLOBAL_MANAGED_TASK_SUPERVISOR.spawn_telemetry(
    proxy_logging_obj.failed_tracking_alert(
        error_message=error_msg,
        failing_model=model,
    ),
    name="failed_tracking_alert",
)
```

```python
# proxy_server.py:2759（T2）
GLOBAL_MANAGED_TASK_SUPERVISOR.spawn_telemetry(
    proxy_logging_obj.budget_alerts(
        type="projected_limit_exceeded",
        user_info=call_info,
    ),
    name="budget_alerts:projected_limit_exceeded",
)
```

```python
# proxy_server.py:2984（T3）
GLOBAL_MANAGED_TASK_SUPERVISOR.spawn_telemetry(
    user_api_key_cache.async_set_cache_pipeline(
        cache_list=values_to_update_in_cache,
        ttl=get_management_object_ttl(user_api_key_cache),
        litellm_parent_otel_span=parent_otel_span,
    ),
    name="async_set_cache_pipeline",
)
```

```python
# auth_checks.py：T4-T12 九处，逐个把 asyncio.create_task(proxy_logging_obj.budget_alerts(type="<X>", ...)) 换成
# GLOBAL_MANAGED_TASK_SUPERVISOR.spawn_telemetry(proxy_logging_obj.budget_alerts(type="<X>", ...), name="budget_alerts:<X>")
# 九个 <X> 依次为：token_budget（3476）/ soft_budget（3574）/ max_budget_alert（3665）/ max_budget_alert（3697）
# / team_budget（3859）/ soft_budget（3973）/ project_budget（4017）/ soft_budget（4067）/ organization_budget（4205）
# ——注意 3574/3973/4067 三处的 type 都是 "soft_budget"、3665/3697 两处都是 "max_budget_alert"，name= 里除 type 外
# 无法进一步靠字符串本身区分同名站点；如果 drain 到期日志需要唯一定位到具体文件行，name= 可改为
# f"budget_alerts:soft_budget:{event_group.value}"（KEY/TEAM/PROJECT 各不相同，天然唯一），实现时按需选用。
```

```python
# auth_checks.py:1568（T13）
GLOBAL_MANAGED_TASK_SUPERVISOR.spawn_telemetry(  # background task to update user with sso id
    UserRepository(prisma_client).table.update(
        where={"user_id": response.user_id},
        data={"sso_user_id": sso_user_id},
    ),
    name="sso_user_id_backfill",
)
```

```python
# user_api_key_auth.py：T14-T15 两处 budget_alerts（540 的 type="proxy_budget"、1982 的 type="proxy_budget"）
# 与 T16-T17 两处 _cache_key_object（1597、1996）与 T18 的 async_service_success_hook（2612），
# 均按同一模式替换，name= 分别为 "budget_alerts:proxy_budget"（两处相同，均为 root-level proxy 预算告警，
# 语义上就是同一件事在两条不同鉴权分支各触发一次，不需要再细分）、"cache_key_object"（两处相同，同一缓存
# 回填意图）、"async_service_success_hook"。
```

4. 确认转绿：
   - `pytest tests/test_litellm/proxy/hooks/test_proxy_track_cost_callback.py tests/test_litellm/proxy/test_proxy_server.py tests/test_litellm/proxy/auth/test_auth_checks.py tests/test_litellm/proxy/auth/test_user_api_key_auth.py -v`——4 个文件全量跑一次，确认新增 8 个测试转绿，且全部既有测试（尤其是本 Task 触碰到的 4 个文件里，所有 mock `budget_alerts`/`_cache_key_object`/`async_service_success_hook` 为 `AsyncMock` 但不断言调度机制的既有用例）依旧全绿——这是这条 PR 最大的回归面，任何一处遗漏替换或替换错位置都会在这一步暴露。
   - 仓库级 grep 断言，确认 18 个站点全部替换、且没有引入新的裸站点（把这条命令的输出粘进 commit message 或 PR 描述，作为人工可核查的完整性证据）：
     ```bash
     grep -n "asyncio.create_task" litellm/proxy/hooks/proxy_track_cost_callback.py litellm/proxy/proxy_server.py litellm/proxy/auth/auth_checks.py litellm/proxy/auth/user_api_key_auth.py
     ```
     预期输出：`proxy_track_cost_callback.py` 里只剩 0 处（248 的 B2 已在 Task 8 改掉，305 的 T1 本 Task 改掉）；`auth_checks.py`/`user_api_key_auth.py` 里的 `asyncio.create_task` 应全部消失（本文件范围内所有既存 `asyncio.create_task` 调用都在本次 T 站点清单里，没有本 Task 之外、故意保留的裸 `create_task`——如果 grep 后仍有残留，说明清单本身还有遗漏，需要回到 Architecture 一节的表格补充，而不是跳过）；`proxy_server.py` 里除 T2/T3 外仍会剩下大量与本 Task 无关的 `asyncio.create_task`（启动期后台循环、admin 端点触发的一次性维护任务等，均不在 Path B 记账/鉴权范畴内，不属于本 Phase 的处理对象，读码判断依据见 Architecture 表格未收录理由）——这一条是预期的正常残留，不是遗漏。

5. 提交：`git add litellm/proxy/hooks/proxy_track_cost_callback.py litellm/proxy/proxy_server.py litellm/proxy/auth/auth_checks.py litellm/proxy/auth/user_api_key_auth.py tests/test_litellm/proxy/hooks/test_proxy_track_cost_callback.py tests/test_litellm/proxy/test_proxy_server.py tests/test_litellm/proxy/auth/test_auth_checks.py tests/test_litellm/proxy/auth/test_user_api_key_auth.py && git commit -m "feat: route Path B telemetry sites (T1-T18) through ManagedTaskSupervisor.spawn_telemetry"`

---

## Task 10 — lifespan 9 步 quiesce 协议接线

依赖 Task 4（`ManagedTaskSupervisor.close_root_admission`/`drain`）、Task 5（`LoggingWorker.quiesce`/`queue_size`）、Phase 1a Task 1（`GracefulShutdownManager.deadline_remaining`/`is_force_exit`）、Phase 1a Task 7（lifespan 关停块已重排为 `wait_for_drain → stop_token_refresh_task/stop_db_health_watchdog_task → close shared aiohttp session → proxy_shutdown_event`——本 Task 在"stop watchdog"和"close aiohttp session"之间插入 spec 第 3-8 步）。

**规划前提说明（如实记录，不是当前仓库事实）**：本计划撰写时，Phase 1a 全部 Task、以及本计划自身的 Task 1-9，在真实仓库里都**尚未落地实现**（`git log` 只有各阶段的 "docs:" 计划提交；`litellm/proxy/proxy_server.py` 现在的关停块仍是重排前的旧顺序，`GracefulShutdownManager` 现在只有 `is_shutting_down`/`get_timeout`/`start_shutdown`/`wait_for_drain`/`reset` 五个方法，没有 `deadline_remaining`/`is_force_exit`；`litellm/litellm_core_utils/logging_worker.py` 只有 `flush`，没有 `quiesce`/`queue_size`；`litellm/proxy/shutdown/` 目录下没有 `managed_task_supervisor.py`）。这是**正常的顺序执行前提**（Phase 1a 先落地，Phase 1b 才接着落地），不是需要上报的矛盾——本 Task 的失败测试、实现代码，都写成"假设 Phase 1a Task 7 与本计划 Task 4/5 已经落地"之后的目标状态，供未来按顺序执行的 implementer 使用，而不是针对当前 HEAD 状态可以直接跑通的代码。

**Files**: `litellm/proxy/proxy_server.py`（改，lifespan 关停块，在 Phase 1a Task 7 已重排的基础上再插入 spec 第 3-8 步）, `tests/test_litellm/proxy/proxy_server/test_lifecycle.py`（改，复用 Phase 1a Task 7 已写入的 `_FakeShutdownPrisma`/`_FakeShutdownDb`）

**设计说明——`admission_policy` 怎么接线**：`LoggingWorker.quiesce()` 的 `admission_policy: Callable[[], bool]` 语义是"这一轮循环里，日志队列是否还应该继续接受新 enqueue"（见 Task 5 测试：`admission_policy=lambda: False` 时，新 enqueue 立即被结算为 `skipped_during_shutdown` 而不进队列）。quiesce()（spec 第 4 步）在 supervisor `drain()`（第 5 步）**之前**跑，此时 supervisor 还没开始排空、已登记的 accounting child 仍在正常运行，它们随时可能产生新的 logging enqueue（比如一个仍在跑的 `update_cache` 协程末尾要写一条 spend 日志）——如果这时候就把 admission 关掉，会把这些本该能正常完成的 enqueue 错误地当成"关停期新流量"直接丢弃，与 spec 第 4 步"flush 仍持 accounting lease 的 logging queue，让顶层 success/failure callback 真正开始并完成"这句话矛盾。因此 `admission_policy` 不接 `GLOBAL_MANAGED_TASK_SUPERVISOR.is_shutting_down_hard()`（那个标志在第 3 步就已经被 `close_root_admission()` 设为 `True`，如果拿来做 `admission_policy` 会导致 quiesce 从第一轮循环起就直接关闭 admission，等于让第 3/4 两步之间的"允许已登记 child 继续派生"名存实亡），而是直接复用与 `deadline_remaining` 同一把时钟：`admission_policy=lambda: GracefulShutdownManager.deadline_remaining() > 0`——只要 deadline 还没到，admission 就保持开放；deadline 一到，两个回调（`deadline_remaining`本身与`admission_policy`）在同一次循环迭代里同时翻面，quiesce() 自己的到期分支与 admission 关闭同步生效，不需要引入第二根时钟或额外参数。

**Steps**

1. 写失败测试（追加到 `tests/test_litellm/proxy/proxy_server/test_lifecycle.py`，紧跟在 Phase 1a Task 7 写的 `test_lifespan_shutdown_stops_iam_refresh_and_watchdog_before_closing_aiohttp_session` 之后，复用同一个 `_FakeShutdownPrisma`/`_FakeShutdownDb`）：

```python
@pytest.mark.asyncio
async def test_lifespan_shutdown_wires_9_step_quiesce_protocol_in_order(monkeypatch):
    """Regression: spec 第 C 节冻结的 9 步 quiesce 协议要求，紧跟在 stop
    watchdog/IAM refresh 之后、断 shared aiohttp session 之前，严格按顺序做
    (3) 封闭新 root accounting admission -> (4) LoggingWorker.quiesce() flush
    -> (5) ManagedTaskSupervisor.drain() fixed-point 排空。任何一处顺序被静默
    颠倒（例如先 drain 再关 admission），都会让一个新 root scope 在排空窗口
    里偷偷溜进来。"""
    call_order: list[str] = []
    fake_prisma = _FakeShutdownPrisma(call_order)

    async def _fake_wait_for_drain() -> None:
        call_order.append("wait_for_drain")

    async def _fake_close_session() -> None:
        call_order.append("close_aiohttp_session")

    fake_session = MagicMock()
    fake_session.close = AsyncMock(side_effect=_fake_close_session)

    mock_supervisor = MagicMock()
    mock_supervisor.close_root_admission = MagicMock(
        side_effect=lambda: call_order.append("close_root_admission")
    )

    async def _fake_drain(**_kwargs):
        call_order.append("supervisor_drain")
        return Drained()

    mock_supervisor.drain = AsyncMock(side_effect=_fake_drain)

    mock_logging_worker = MagicMock()

    async def _fake_quiesce(**_kwargs):
        call_order.append("logging_quiesce")
        return LoggingDrained()

    mock_logging_worker.quiesce = AsyncMock(side_effect=_fake_quiesce)
    mock_logging_worker.queue_size = MagicMock(return_value=0)
    mock_logging_worker.stop_after_quiesce = AsyncMock(
        side_effect=lambda: call_order.append("stop_after_quiesce")
    )

    monkeypatch.setattr(ps.GracefulShutdownManager, "start_shutdown", lambda: None)
    monkeypatch.setattr(ps.GracefulShutdownManager, "wait_for_drain", _fake_wait_for_drain)
    monkeypatch.setattr(ps.GracefulShutdownManager, "deadline_remaining", lambda: 5.0)
    monkeypatch.setattr(ps.GracefulShutdownManager, "is_force_exit", lambda: False)
    monkeypatch.setattr(ps, "_initialize_shared_aiohttp_session", AsyncMock(return_value=fake_session))
    monkeypatch.setattr(ps, "proxy_shutdown_event", AsyncMock())  # 与本 Task 无关的收尾，本用例不覆盖
    monkeypatch.setattr(ps, "GLOBAL_MANAGED_TASK_SUPERVISOR", mock_supervisor)
    monkeypatch.setattr(ps, "GLOBAL_LOGGING_WORKER", mock_logging_worker)

    with patch.object(ps.ProxyStartupEvent, "_setup_prisma_client", return_value=fake_prisma):
        app = FastAPI()
        async with proxy_startup_event(app):
            pass

    assert call_order == [
        "wait_for_drain",
        "stop_token_refresh_task",
        "stop_db_health_watchdog_task",
        "close_root_admission",
        "logging_quiesce",
        "supervisor_drain",
        "stop_after_quiesce",
        "close_aiohttp_session",
    ]


@pytest.mark.asyncio
async def test_lifespan_shutdown_wires_quiesce_and_drain_callables_to_shared_deadline_clock(monkeypatch):
    """Pins 四处易错的接线细节：(1) quiesce()/drain() 必须共享
    GracefulShutdownManager 同一把冻结 deadline 时钟，而不是各自起一个独立
    计时器；(2) quiesce() 的 admission_policy 必须在 deadline 耗尽的那一刻才
    翻面关闭，而不是从一开始就常闭（那样会让第 3/4 步之间"允许已登记 child
    继续派生"名存实亡）；(3) drain() 的 root_queue_unfinished 必须绑定
    GLOBAL_LOGGING_WORKER.queue_size，否则 supervisor 会在 LoggingWorker 队列
    里还有未 flush 完的 item 时就误判 fixed point 已达成；(4) 两处调用都必须
    显式传 is_force_exit=GracefulShutdownManager.is_force_exit——不依赖 Task 4
    `drain()` 自带的默认值，因为本轮评审要求两个调用点都不能让"是否强制退出"
    这件事隐式发生。"""
    fake_prisma = _FakeShutdownPrisma([])
    captured_quiesce_kwargs: dict = {}
    captured_drain_kwargs: dict = {}

    async def _fake_quiesce(**kwargs):
        captured_quiesce_kwargs.update(kwargs)
        return LoggingDrained()

    async def _fake_drain(**kwargs):
        captured_drain_kwargs.update(kwargs)
        return Drained()

    mock_supervisor = MagicMock()
    mock_supervisor.close_root_admission = MagicMock()
    mock_supervisor.drain = AsyncMock(side_effect=_fake_drain)

    mock_logging_worker = MagicMock()
    mock_logging_worker.quiesce = AsyncMock(side_effect=_fake_quiesce)
    mock_logging_worker.queue_size = MagicMock(return_value=42)
    mock_logging_worker.stop_after_quiesce = AsyncMock()

    monkeypatch.setattr(ps.GracefulShutdownManager, "start_shutdown", lambda: None)
    monkeypatch.setattr(ps.GracefulShutdownManager, "wait_for_drain", AsyncMock())
    monkeypatch.setattr(ps.GracefulShutdownManager, "is_force_exit", lambda: False)
    monkeypatch.setattr(
        ps, "_initialize_shared_aiohttp_session", AsyncMock(return_value=MagicMock(close=AsyncMock()))
    )
    monkeypatch.setattr(ps, "proxy_shutdown_event", AsyncMock())
    monkeypatch.setattr(ps, "GLOBAL_MANAGED_TASK_SUPERVISOR", mock_supervisor)
    monkeypatch.setattr(ps, "GLOBAL_LOGGING_WORKER", mock_logging_worker)

    with patch.object(ps.ProxyStartupEvent, "_setup_prisma_client", return_value=fake_prisma):
        app = FastAPI()

        monkeypatch.setattr(ps.GracefulShutdownManager, "deadline_remaining", lambda: 3.0)
        async with proxy_startup_event(app):
            pass

    # 共享同一把时钟：两处传入的 deadline_remaining 都等于
    # GracefulShutdownManager.deadline_remaining（classmethod 两次取值不是同一
    # 个对象，用 == 而不是 is 比较——bound method 的 __eq__ 比较 __func__/__self__）。
    assert captured_quiesce_kwargs["deadline_remaining"] == ps.GracefulShutdownManager.deadline_remaining
    assert captured_drain_kwargs["deadline_remaining"] == ps.GracefulShutdownManager.deadline_remaining

    # root_queue_unfinished 绑定 LoggingWorker.queue_size，不是常量 0 或另起的计数器。
    assert captured_drain_kwargs["root_queue_unfinished"] == mock_logging_worker.queue_size

    # 两处调用都显式传 is_force_exit，不依赖 drain() 自带的默认值。
    assert captured_quiesce_kwargs["is_force_exit"] == ps.GracefulShutdownManager.is_force_exit
    assert captured_drain_kwargs["is_force_exit"] == ps.GracefulShutdownManager.is_force_exit

    # admission_policy 与 deadline_remaining 同步翻面：还没到期时开放，到期后关闭。
    monkeypatch.setattr(ps.GracefulShutdownManager, "deadline_remaining", lambda: 3.0)
    assert captured_quiesce_kwargs["admission_policy"]() is True
    monkeypatch.setattr(ps.GracefulShutdownManager, "deadline_remaining", lambda: 0.0)
    assert captured_quiesce_kwargs["admission_policy"]() is False
```

（`Drained`、`ForcedExit`、`LoggingDrained`、`LoggingForcedExit` 需要在文件顶部补两行 import：`from litellm.proxy.shutdown.managed_task_supervisor import Drained, ForcedExit` 与 `from litellm.litellm_core_utils.logging_worker import LoggingDrained, LoggingForcedExit`；其余 `ps`/`FastAPI`/`AsyncMock`/`MagicMock`/`patch` 均已在该文件顶部导入，直接复用。）

跑一下确认失败：此刻 `ps.GLOBAL_MANAGED_TASK_SUPERVISOR`/`ps.GLOBAL_LOGGING_WORKER` 在 `proxy_server.py` 里还不存在（`monkeypatch.setattr` 会因找不到目标属性抛 `AttributeError`），且即便先假设它们已 import 进来，lifespan 关停块此刻也根本不会调用 `close_root_admission`/`quiesce`/`drain`，`call_order` 断言必然不匹配。

2. 实现：在 `proxy_server.py` 顶部追加两行 import，并把 Phase 1a Task 7 已重排的关停块，在"stop watchdog"和"close shared aiohttp session"之间插入 spec 第 3-8 步：

```python
# proxy_server.py 顶部追加
from litellm.litellm_core_utils.logging_worker import (
    GLOBAL_LOGGING_WORKER,
    LoggingDeadlineExceeded,
    LoggingDrained,
    LoggingForcedExit,
)
from litellm.proxy.shutdown.managed_task_supervisor import (
    GLOBAL_MANAGED_TASK_SUPERVISOR,
    DeadlineExceeded,
    Drained,
    ForcedExit,
)
```

```python
    if prisma_client is not None and hasattr(prisma_client, "stop_db_health_watchdog_task"):
        try:
            await prisma_client.stop_db_health_watchdog_task()
        except Exception as e:
            verbose_proxy_logger.error(f"Error stopping DB health watchdog task: {e}")

    # Shutdown event - quiesce protocol steps 3-8 (frozen spec section C):
    # close new root accounting admission, flush the logging queue, then
    # fixed-point drain any remaining accounting children, then stop the
    # logging worker's own background loop -- strictly before the shared
    # aiohttp session (and prisma/redis, via proxy_shutdown_event below) get
    # torn out from under them.
    GLOBAL_MANAGED_TASK_SUPERVISOR.close_root_admission()  # step 3

    logging_outcome = await GLOBAL_LOGGING_WORKER.quiesce(  # step 4
        deadline_remaining=GracefulShutdownManager.deadline_remaining,
        admission_policy=lambda: GracefulShutdownManager.deadline_remaining() > 0,
        is_force_exit=GracefulShutdownManager.is_force_exit,
    )
    match logging_outcome:
        case LoggingDrained():
            verbose_proxy_logger.info("graceful_shutdown_logging_drained")
        case LoggingDeadlineExceeded(
            dropped_queue_items=dropped, cancelled=cancelled, cancellation_failed=cancellation_failed
        ):
            verbose_proxy_logger.warning(
                "graceful_shutdown_logging_deadline_exceeded dropped_queue_items=%s cancelled=%s "
                "cancellation_failed=%s",
                dropped,
                cancelled,
                cancellation_failed,
            )
        case LoggingForcedExit(
            dropped_queue_items=dropped, cancelled=cancelled, cancellation_failed=cancellation_failed
        ):
            verbose_proxy_logger.warning(
                "graceful_shutdown_logging_forced_exit dropped_queue_items=%s cancelled=%s "
                "cancellation_failed=%s",
                dropped,
                cancelled,
                cancellation_failed,
            )
        case _ as unreachable:
            assert_never(unreachable)

    drain_outcome = await GLOBAL_MANAGED_TASK_SUPERVISOR.drain(  # step 5 (fixed-point), 6-7 (cancel+settle on deadline)
        deadline_remaining=GracefulShutdownManager.deadline_remaining,
        root_queue_unfinished=GLOBAL_LOGGING_WORKER.queue_size,
        is_force_exit=GracefulShutdownManager.is_force_exit,
    )
    match drain_outcome:
        case Drained():
            verbose_proxy_logger.info("graceful_shutdown_accounting_drained")
        case DeadlineExceeded(cancelled=cancelled, cancellation_failed=cancellation_failed):
            verbose_proxy_logger.warning(
                "graceful_shutdown_accounting_deadline_exceeded cancelled=%s cancellation_failed=%s",
                cancelled,
                cancellation_failed,
            )
        case ForcedExit(cancelled=cancelled, cancellation_failed=cancellation_failed):
            verbose_proxy_logger.warning(
                "graceful_shutdown_accounting_forced_exit cancelled=%s cancellation_failed=%s",
                cancelled,
                cancellation_failed,
            )
        case _ as unreachable:
            assert_never(unreachable)

    await GLOBAL_LOGGING_WORKER.stop_after_quiesce()  # step 8

    # Shutdown event - close shared aiohttp session
    if shared_aiohttp_session is not None:
        try:
            await shared_aiohttp_session.close()
            verbose_proxy_logger.info("SESSION REUSE: Closed shared aiohttp session")
        except Exception as e:
            verbose_proxy_logger.error(f"Error closing shared aiohttp session: {e}")

    await proxy_shutdown_event()  # type: ignore[reportGeneralTypeIssues]  # step 9 (prisma + redis)
```

（`assert_never` 需要从 `typing_extensions`（或 3.11+ 的 `typing`）import；`proxy_server.py` 里如果尚未 import，本 Task 一并在顶部补一行——本仓库遵循的"不 throw、tagged union + 穷举 match"约定，`assert_never` 是让 basedpyright 在未来新增变体时对漏 `case` 报类型错误的标准写法，不是本 Task 新发明的模式。两处日志分支都直接从 `match` 命中的变体标签取文案——`LoggingForcedExit`/`ForcedExit`各自单独一支`case`——不再像上一稿那样在 `DeadlineExceeded`/`LoggingDeadlineExceeded` 分支内部另外调用一次 `GracefulShutdownManager.is_force_exit()` 去猜文案：三态 union 本身已经在 Task 4/Task 5 把"是否强制退出"这件事编码进了变体标签，`match` 只需要照抄，不需要重新查询一次管理器状态——这正是本轮评审"不得二次查询猜测原因"的字面要求。）

**关于 step 8：`stop_after_quiesce()`（major 8，Task 5 已实现）**：spec 第 8 步"stop logging worker"现在有了明确的调用点——Task 5 的 `stop_after_quiesce()`——而不是复用既有的 `stop()`（那个方法的 `clear_queue()` 兜底路径仍然会真正执行 queued 业务回调，与"到期后绝不执行 queued 业务回调"的约定冲突，见 Task 5 该方法自身的 docstring）。调用时机被 Task 5 明确约束为"只有在 `quiesce()` 与 `drain()` 都已返回之后调用一次"——因为 `drain()` 的 `root_queue_unfinished` 参数依赖 `_worker_loop` 仍然存活才能继续处理 `drain()` 自己窗口期内姗姗来迟的队列项；本 Task 因此把这次调用放在 `drain()` 的 `match` 块结束之后、关闭共享 aiohttp session 之前，与 Task 5 的约束严格对齐。

3. 确认转绿：
   - `pytest tests/test_litellm/proxy/proxy_server/test_lifecycle.py -v`——确认新增两个测试转绿，且 Phase 1a Task 7 写的顺序测试依旧转绿（本 Task 插入的 `close_root_admission`/`quiesce`/`drain`/`stop_after_quiesce` 四步不能打乱 Task 7 已经断言过的四段相对顺序）。
   - `pytest tests/test_litellm/proxy/test_proxy_server.py -k startup_master_key -v`——Phase 1a Task 7 的 Steps 1 已经确认这是仓库里唯一一处完整驱动 `proxy_startup_event` 的既有测试，本 Task 再次触碰同一段代码，必须确认它没被打破。
   - `make pre-commit`。

4. 提交：`git add litellm/proxy/proxy_server.py tests/test_litellm/proxy/proxy_server/test_lifecycle.py && git commit -m "feat: wire the 9-step quiesce protocol into the FastAPI lifespan shutdown block"`

---

## Task 11 — E2E 套件扩展：完整 9 步 quiesce 的进程级验收

依赖 Task 10（新增的 `graceful_shutdown_logging_drained`/`graceful_shutdown_logging_deadline_exceeded`/`graceful_shutdown_logging_forced_exit`/`graceful_shutdown_accounting_drained`/`graceful_shutdown_accounting_deadline_exceeded`/`graceful_shutdown_accounting_forced_exit` 六条日志），依赖 Phase 1a Task 8（`tests/e2e/shutdown/` 整套 harness——`subprocess_harness.py`/`fake_upstream.py`/`conftest.py`/`test_graceful_shutdown_e2e.py`——已落地；本 Task **扩展**既有文件，不新建套件）。

**范围澄清（承接 Phase 1a"承诺的后续"一节）**：Phase 1a 计划正文明确写道，Phase 1b 要把 1a 留下的"关停短路"（`AccountingSkippedDuringShutdown` 提前返回）升级为完整 9 步 quiesce；本 Task 是这句承诺在 E2E 验收层面的落地——把 spec 第 186 行"uvicorn 入口"验收标准里，Phase 1a 当时还没有对应生产代码、因而没法断言的"记账 settled/skipped 单行日志"与"quiesce 顺序"两项，补进已有的 E2E 用例。

**Files**: `tests/e2e/shutdown/test_graceful_shutdown_e2e.py`（改，扩展既有测试与断言，不新建文件）

**设计说明——为什么不额外写一个"E2E 级 deadline-exceeded 记账 drain"用例（记录未采纳方案，附理由）**：spec 第 176 行要求"quiesce 顺序断言"与"deadline 到期 cancel + settle"两类断言都要有覆盖，但没有规定必须在哪个测试层级覆盖。Task 4（`ManagedTaskSupervisor.drain()`）、Task 5（`LoggingWorker.quiesce()`）、Task 10（lifespan 接线）三处都已经用**注入的** `deadline_remaining=lambda: -1.0` 或 `lambda: 0.0` 这类确定性 fixture，在单元测试层面完整覆盖了"到期后联合 cancel + settle、不再执行 queued callback"这条路径——不依赖真实时钟、不依赖真实 DB/Redis 延迟，每次跑结果都确定。反过来，如果要在 E2E 层面（真实子进程、真实 DB/Redis）真正逼出"记账任务还没跑完、deadline 就到了"这个状态，唯一现实的手段是把 `GRACEFUL_SHUTDOWN_TIMEOUT` 设得极短（比如 10ms），赌真实 Postgres/Redis 往返来不及在这个窗口内完成——这是一场跟真实时钟和真实网络延迟的赛跑，本机、CI、不同负载下的真实往返时延本就不稳定，跑出来的测试会不可靠：负载低时这个"deadline exceeded"分支可能根本不会被触发（写操作 10ms 内就完成了），测试要么变成偶发 flaky，要么为了"保证触发"进一步压低超时到不现实的数值、反而让人怀疑这是否还是"真实场景"。这类不可靠信号正是 `CLAUDE.md`"宁可没有信号，也不要不会在代码坏时报警的测试"明确要拒绝的模式——一个大多数时候通过、只在特定负载下才断言到期分支的测试，无法在代码回归时可靠报警，等同于虚假信号。因此本 Task **不**在 E2E 层引入这类真实时钟竞速测试；deadline-exceeded 路径的验收保留在 Task 4/5/10 已经完成的确定性单元测试里，E2E 层只覆盖"进程真的会启动、真的会跑完整套 quiesce 协议、真的不会残留 reconnect/traceback"这类必须依赖真实子进程才能验证、且不依赖时钟竞速就能确定性触发的部分（正常路径下 `drain()`/`quiesce()` 必然落在 `Drained`/`LoggingDrained` 分支，不需要故意逼近 deadline）。

**设计说明——为什么严格的"日志行先后顺序"断言只加到 `direct` 单进程模式**：`reload`/`workers` 模式下，关停时是每个 worker 子进程各自独立执行同一套 `GracefulShutdownManager`/`ManagedTaskSupervisor`/`GLOBAL_LOGGING_WORKER`（进程级单例，不跨进程同步——这是 Phase 1a 自检里已经确认过的既定非目标），多个 worker 的 stdout/stderr 交织写进同一个捕获文件，"整份合并文本里 A 子串第一次出现的位置早于 B 子串"不能保证反映"同一个 worker 内部 A 确实先于 B 发生"——可能是 worker 1 的 `graceful_shutdown_accounting_drained` 先打印出来，而 worker 2 的 `graceful_shutdown_started` 才刚刚开始交织进来，顺序断言这时候是对交织噪音的误读，不是对真实执行顺序的验证。`direct` 模式是唯一的单进程场景，日志天然是单一时间线，顺序断言在这里有真实意义；`reload`/`workers` 模式改用弱一档的"存在性"断言（新增的四条日志里，"drained"这一对至少各出现一次，且"deadline_exceeded"这一对**不**出现——因为正常路径不该触发到期分支），不做强顺序断言。

**设计说明——`ForcedExit` 分支的可达性边界（已裁决，供未来读者理解这条不变量）**：本轮评审曾要求给第二次 SIGINT 的既有 E2E 用例（Phase 1a Task 8 的 `test_second_signal_forces_immediate_exit_without_waiting_full_deadline`）追加一条 `ForcedExit` 专属日志行断言。撰写本 Task 时直接读了真实 uvicorn 源码（`.venv/lib/python3.13/site-packages/uvicorn/server.py`），发现并向主协调者报告了一处架构事实——主协调者已核实并裁决如下，不再是待定问题：

- 事实：`Server.shutdown()`（第 261-294 行）里，"Send the lifespan shutdown event"那一步写的是 `if not self.force_exit: await self.lifespan.shutdown()`（第 293-294 行）——`force_exit` 为真时，uvicorn 自己的基类直接跳过整个 ASGI lifespan shutdown 事件；`_wait_tasks_to_complete`（连接排空阶段）同样在 `force_exit` 时提前 bail。`Server.handle_exit()`（第 334-339 行）只有"已经 `should_exit` 且这次信号还是 `SIGINT`"（第二次 SIGINT）才会把 `self.force_exit` 置 `True`；Phase 1a 的 `DrainingServer.handle_exit()` 同样只在这个分支调用 `GracefulShutdownManager.request_force_exit()`。
- **裁决 1——不改 Phase 1a 的 `DrainingServer`**：uvicorn 在 `force_exit` 时跳过 `lifespan.shutdown()` 是**正确语义**：第二次 SIGINT 的 operator 意图就是"别排空了，立刻退"，我们的记账 drain 本就是 best-effort（spec 非目标已明确"关停期允许少量未 flush spend 丢失"）。强行让 lifespan 在 `force_exit` 下仍然跑完，等于让"强退"不强退，违背 operator 意图，因此拒绝此前列出的"改 `DrainingServer.shutdown()`"这个选项。
- **裁决 2——保留三变体 `ForcedExit`，可达窗口真实存在但窄**：`ForcedExit` 可达当且仅当"第二次 SIGINT 落在我们的 quiesce 已经在 `lifespan.shutdown()` 里运行时"——即：请求已跑完、uvicorn 自己的连接排空阶段已过，Task 10 的 quiesce/drain 正在跑，operator 此时双击 Ctrl+C，drain 循环在下一次迭代看到 `deadline_remaining()==0` 且 `is_force_exit()` 为真 → 产出 `ForcedExit`。**不**可达：当第二次 SIGINT 落在 uvicorn 自己的 `_wait_tasks_to_complete`（连接排空）阶段时，那次调用会跳过 `lifespan.shutdown()`，本计划的整套 quiesce 协议根本不会开始跑——这是 uvicorn 架构决定的时序窗口，不是本计划的 bug，也不需要修复。
- **裁决 3——Task 11 只断言 user-observable 行为，不断言 `ForcedExit` 专属日志**：要精确让第二次 SIGINT 落在"记账 drain 正在进行"这条窄窗口内，时序太脆，不适合当 E2E 断言；`ForcedExit`/`LoggingForcedExit` 两个变体及其日志由 **Task 4/5 单测**（注入 `is_force_exit=lambda: True` 与可控 `deadline_remaining`）确定性覆盖，已经完成。E2E 层只断言用户真正在意的事：双击 Ctrl+C 后，进程在远小于完整 deadline 的时间内退出——见下方 Step 2b。

**Steps**

1. 在 `test_graceful_shutdown_e2e.py` 顶部，`_BANNED_LOG_PATTERNS` 定义之后，新增两个断言 helper：

```python
_QUIESCE_DRAINED_LOG_PATTERNS = (
    "graceful_shutdown_logging_drained",
    "graceful_shutdown_accounting_drained",
)
_QUIESCE_DEADLINE_EXCEEDED_LOG_PATTERNS = (
    "graceful_shutdown_logging_deadline_exceeded",
    "graceful_shutdown_accounting_deadline_exceeded",
)


def _assert_quiesce_drained_without_deadline_exceeded(proxy: SpawnedProxy) -> None:
    """Weak (interleaving-safe) form: usable across direct/reload/workers.
    Confirms both new quiesce steps actually ran and actually reached their
    happy-path branch, without claiming anything about cross-worker ordering."""
    text = _combined_output(proxy)
    for pattern in _QUIESCE_DRAINED_LOG_PATTERNS:
        assert pattern in text, f"expected {pattern!r} in combined stdout+stderr, got:\n{text}"
    for pattern in _QUIESCE_DEADLINE_EXCEEDED_LOG_PATTERNS:
        assert pattern not in text, f"unexpected {pattern!r} on the happy path in combined stdout+stderr"


def _assert_quiesce_log_order_single_process(proxy: SpawnedProxy) -> None:
    """Strong (ordering) form: only valid for a single-process (direct) spawn,
    where the combined log is one true timeline, not several workers'
    interleaved streams. Pins spec 第 176 行 quiesce 顺序契约 end-to-end against
    a real process: logging flush precedes supervisor drain, and both precede
    shared-dependency teardown (aiohttp session close, then prisma/redis via
    proxy_shutdown_event's own "Shutting down LiteLLM Proxy Server" line)."""
    text = _combined_output(proxy)
    markers = [
        "graceful_shutdown_started",
        "graceful_shutdown_logging_drained",
        "graceful_shutdown_accounting_drained",
        "SESSION REUSE: Closed shared aiohttp session",
        "Shutting down LiteLLM Proxy Server",
    ]
    positions = [text.index(marker) for marker in markers]  # raises ValueError with a clear marker name if missing
    assert positions == sorted(positions), f"quiesce log markers out of order: {list(zip(markers, positions))}"
```

2. 写失败测试（先把上面两个 helper 接进既有测试，此刻应因日志里还没有这四条新 quiesce 日志而失败——`text.index(...)` 抛 `ValueError`／`assert pattern in text` 抛 `AssertionError`）：

```python
# 在 TestSignalDrivenShutdown.test_signal_drains_inflight_request_then_exits_within_deadline 内，
# 紧跟在既有 `_assert_no_shutdown_races(proxy)` 之后追加：
            _assert_quiesce_drained_without_deadline_exceeded(proxy)
            if mode == "direct":
                _assert_quiesce_log_order_single_process(proxy)
```

```python
# 在 TestSelfTriggeredShutdown.test_limit_max_requests_self_initiates_shutdown_without_any_signal 内，
# 紧跟在既有 `_wait_for_log_line(proxy, "graceful_shutdown_started", timeout=0.1)` 之后追加：
            _assert_quiesce_drained_without_deadline_exceeded(proxy)
            _assert_quiesce_log_order_single_process(proxy)  # this test only ever spawns mode="direct"
```

2b. 第二次 SIGINT 强制退出场景（**已定稿**，见上方"设计说明——`ForcedExit` 分支的可达性边界"）：主协调者裁决不要求 E2E 断言 `ForcedExit` 专属日志，只要求断言 user-observable 行为——第二次 SIGINT 后进程在远小于完整 deadline 的时间内退出。这条断言 Phase 1a Task 8 的 `test_second_signal_forces_immediate_exit_without_waiting_full_deadline` 早已具备（`elapsed < 2.0`，对比配置的 30s deadline；`exit_code != 0`），本 Task **不需要改动这条既有测试的核心断言**。

额外给这条既有测试追加一处"缺席性"断言，把"uvicorn 在 force_exit 时正确跳过 `lifespan.shutdown()`、Task 10 全套 quiesce 协议这次根本不会执行"这条不变量钉成一条确定性回归测试，而不只是文档里的一句话——这条断言复用 Step 1 已经定义好的 `_QUIESCE_DRAINED_LOG_PATTERNS`/`_QUIESCE_DEADLINE_EXCEEDED_LOG_PATTERNS` 常量，不依赖任何时序竞速，只在进程退出后检查一次完整捕获的输出：

```python
# 追加在既有 test_second_signal_forces_immediate_exit_without_waiting_full_deadline 里
# `assert exit_code != 0` 之后：
text = _combined_output(proxy)
# 强制退出路径下，lifespan.shutdown() 被 uvicorn 有意跳过，Task 10 的整套 9 步
# quiesce 协议因此根本不会执行——这条断言把上方"设计说明"里的不变量钉成一条
# 确定性回归测试：未来如果有人不小心让 lifespan 在 force_exit 下也跑了起来
# （无论是改坏 DrainingServer.shutdown()，还是引入了新的调用路径），这里会先报警。
for pattern in _QUIESCE_DRAINED_LOG_PATTERNS + _QUIESCE_DEADLINE_EXCEEDED_LOG_PATTERNS:
    assert pattern not in text, f"unexpected {pattern!r}: lifespan.shutdown() should have been skipped on force_exit"
```

3. 实现：本 Task 在 `tests/e2e/` 侧没有生产代码要改——四条新日志已经由 Task 10 在 `proxy_server.py` 里落地；这一步纯粹是"确认 Task 10 已完成后，E2E 测试自然转绿"，不需要额外写生产代码。若在 Task 10 尚未落地时先跑本 Task 的测试，预期失败信息应精确指向缺失的日志文本（而不是进程崩溃/超时），从而确认新断言本身而非环境问题是失败原因；Step 2b 的缺席性断言在 Task 10 未落地时天然为真（此时全部日志本来就不存在），因此该断言只有在 Task 10 落地之后才具备真正的区分力，需配合"若强行让 lifespan 在 force_exit 下也执行，本断言必须变红"这条心智模型去读。

4. 确认转绿（要求 Task 10 已经落地）：
   - `pytest tests/e2e/shutdown/test_graceful_shutdown_e2e.py -m spawned_proxy_e2e -v -k "TestSignalDrivenShutdown or TestSelfTriggeredShutdown"`——确认扩展后的断言全部通过，尤其确认 `direct` 模式下的顺序断言、`reload`/`workers` 模式下的存在性断言、以及 Step 2b 新增的缺席性断言都覆盖到（`reload`/`workers` 各自至少有一个 parametrize case 命中，见既有 `@pytest.mark.parametrize`）。
   - 跑 `TestShutdownRaceWithRealInfra`（有 `DATABASE_URL`/`REDIS_HOST` 时）：确认真实 DB/Redis 场景下同样能观察到 `graceful_shutdown_accounting_drained`（而不是 skip 或异常）——这条断言复用 Phase 1a 已完成的该测试体，若发现该测试体尚未按 Phase 1a 自己的说明补全（仍是 `...` 占位），先回 Phase 1a 计划补全它、再叠加本 Task 的新断言，不在本 Task 里重新设计这个测试。
   - `make pre-commit`。

5. 提交：`git add tests/e2e/shutdown/test_graceful_shutdown_e2e.py && git commit -m "test: extend e2e graceful shutdown suite with full 9-step quiesce log assertions"`

---

## 未采纳方案（全文汇总索引）

以下每条在对应 Task 正文里都有完整推理，这里只做汇总索引，方便评审一次性看全，不代表这里的一句话摘要可以脱离原文单独引用。

1. **不新增 `ManagedTaskSupervisor.spawn_child` 专用入口**（Task 8）——B2/B3 两个记账创建点直接复用 Task 2 已有的 `spawn_detached`，理由是两处调用点都天然处在 Task 6/7 已经建立的同一条真实调用链路上、`current_accounting_scope.get()` 能直接看到上游绑定的 lease，专门再开一个 `spawn_child` 入口只是给同一件事起第二个名字，不提供额外能力。
2. **不采用"两变体 `DrainOutcome`/`LoggingDrainOutcome` + Task 10 层面按 `is_force_exit()` 查询结果给日志文本打标签"这一早期临时方案**（Task 4/5/10，本轮评审已裁决改判）——早期草稿曾计划保留两变体 union，只在 Task 10 的 lifespan 层单独反查 `GracefulShutdownManager.is_force_exit()` 来决定日志文案，不改动 Task 4/5 已写好的两变体类型契约；本轮评审明确要求改为 spec 第 140/176 行字面写的三变体 tagged union（`Drained | DeadlineExceeded(cancelled, cancellation_failed) | ForcedExit`），且两处调用点（`quiesce()`/`drain()`）必须显式传入 `is_force_exit=...`、`match` 分支内部不得再反查——已在 Task 4/5/10 落实，两变体+反查方案不再采用。
3. **不在 Task 11 里新增一条 E2E 级"deadline-exceeded 记账 drain"用例**——理由是可靠地把真实子进程逼进这条分支需要用一个人为极短的 `GRACEFUL_SHUTDOWN_TIMEOUT` 去赛真实 DB/Redis 网络延迟，本质上不可靠（本地快速 DB 写入完全可能在任意短 deadline 内提前完成，取决于机器/CI 负载），与项目 CLAUDE.md「测试要在代码坏的时候确实失败」的原则冲突；deadline-exceeded 路径已经被 Task 4/5/10 的单元测试用注入的 `deadline_remaining=lambda: -1.0` 类 fixture 完全且确定性地覆盖。
4. **`reload`/`workers` 模式下不加严格的日志行先后顺序断言**（Task 11）——这两种模式各自派生独立子进程、各自持有进程级单例（`GracefulShutdownManager`/`ManagedTaskSupervisor`/`GLOBAL_LOGGING_WORKER` 不跨进程同步，Phase 1a 自检里已确认的既定非目标），多进程 stdout/stderr 交织进同一份捕获文件后，子串先后位置不能反映单进程内部真实时序；改用较弱的"两条 drained 日志都出现、两条 deadline_exceeded 日志都不出现"存在性断言。
5. **不合并 `LoggingWorker.quiesce()` 与 `ManagedTaskSupervisor.drain()` 为跨两个组件的单一计数器**（Architecture 一节）——严格顺序组合（先 `quiesce()` 跑到底或到 deadline，再 `drain()` 用剩余 `deadline_remaining` 排空 supervisor 自己的 fixed point），呼应 spec"先 flush 产出的工作，再排空 child，方向不可反"的顺序约束；顺序编排放在 Task 10（lifespan 接线），不塞进任一组件内部，保持两者各自独立可测。
6. **核心 SDK 里两处纯遥测裸 `create_task`（`streaming_handler.py:2011`/`:2080`）维持现状、不纳入任何管理集合**（Architecture 一节）——复用 Phase 1a 计划对 `redis_cache.py` 的 `async_service_failure_hook` 已经做出的同款裁决，理由是纳入管理原语会造成核心 SDK 反向依赖 proxy 类型的层级污染，而收益仅是"deadline 时多 cancel 掉几个本来就是尽力而为性质的任务"，不值得这个代价。

## 自检

### Spec 覆盖审计

逐项核对 spec 第 B、C 两节（Phase 1b 的范围）里每一条已接受的验收要求，映射到覆盖它的 Task；spec 第 A 节（uvicorn/`DrainingServer`/deadline 基础设施）与第 C 节里 watchdog/IAM-refresh/redis 单行降级相关的条目属 Phase 1a 范围，本计划仅在需要衔接处引用其签名，不重复覆盖；spec 第 D/E/F/G 节（per-request 登记表、lease 归属、ACCOUNTING 阶段可观测性）属 Phase 2，「治理边界」一节已裁决不在本计划范围内，读码复核确认这条边界没有结构性缺陷。

| Spec 要求（章节/行号） | 覆盖 Task |
|---|---|
| `ManagedTaskSet`（跨 `LoggingWorker` 与新 supervisor 共用的最小任务集合原语，不合并两者高层 drain 语义） | Task 1 |
| 不用 `ContextVar != None` 做 admission 授权，改用不可伪造 scope + 显式方法调用 | Task 2（`AccountingScope` 协议 + `current_accounting_scope` + `spawn_detached`） |
| `LoggingTask` 从 mutable `TypedDict` 改为 `frozen dataclass(slots=True)`，新增中立 `token: CompletionToken \| None` 字段，每条 drop/rebind 路径都要 `settle` | Task 3 |
| lease 在最早的 logging enqueue 边界同步 acquire，绑定到真实调度拓扑而非替换表面 API | Task 2（acquire 时机）+ Task 6-9（六个 Path A 站点 + Path B 站点接入） |
| `AccountingSkippedDuringShutdown` 扩展为完整 `AccountingCompleted \| AccountingSkippedDuringShutdown \| AccountingFailed` tagged union | Task 4 |
| `ManagedTaskSupervisor.spawn_telemetry`/`close_root_admission`/`async drain() -> DrainOutcome`，fixed-point `root_queue_unfinished == 0 && accounting_tasks == 0 && admissions_in_progress == 0` | Task 4（含收尾时补的 `close_root_admission()`，见下方"已修复的实现缺口"） |
| 子任务分类表：`update_cache`/`_batch_database_updates` = accounting；`budget_alerts`/`async_set_cache_pipeline`/`failed_tracking_alert`/service hooks = telemetry | Task 6-9（Path A/B1 记账接入，B2/B3 记账接入，T1-T18 遥测接入） |
| `LoggingWorker.quiesce(deadline_remaining, admission_policy) -> LoggingDrainOutcome` | Task 5 |
| 第 C 节 9 步 quiesce 协议：步骤 1-2（uvicorn 停止接受+drain transport；lifespan 停止非请求后台生产者）——**属 Phase 1a**，本计划仅在 Task 10 里衔接其后 | Phase 1a Task 7（前置，非本计划覆盖范围） |
| 第 C 节步骤 3（封闭新 root accounting admission，允许已登记 accounting task 派生 child） | Task 4（`close_root_admission()`）+ Task 10（接线） |
| 第 C 节步骤 4（`LoggingWorker.quiesce()` 冲刷仍持有记账 lease 的队列项） | Task 5 + Task 10（接线） |
| 第 C 节步骤 5（supervisor fixed-point drain：先冲刷产出工作，再排空 child，顺序不可反） | Task 4（`drain()`）+ Architecture 一节的顺序组合裁决 + Task 10（接线） |
| 第 C 节步骤 6（deadline 到期 → 联合 cancel `LoggingWorker` 队列项/运行中任务/重试与激进清理 helper/supervisor child，然后 `gather(return_exceptions=True)`） | Task 5（`LoggingDeadlineExceeded`）+ Task 4（`DeadlineExceeded`）+ Task 10（接线，`match`/`assert_never` 分支处理两种到期结果） |
| 第 C 节步骤 7（未完成记录写 `shutdown_dropped`，结清剩余 lease） | Task 3（每条 drop/rebind 路径 settle）+ Task 5（`LoggingDeadlineExceeded` 内部结清逻辑） |
| 第 C 节步骤 8（停止 logging worker） | Task 5（`stop_after_quiesce()` 实现）+ Task 10（在 `quiesce()`/`drain()` 均返回后显式调用一次，接线顺序见正文） |
| 第 C 节步骤 9（关闭共享 aiohttp/prisma/redis） | Phase 1a 既有代码（未改）+ Task 10（确认接线顺序把这一步放在 quiesce 完成之后） |
| 测试要求：记账生命周期全链路（lease 获取→派生→结清）单测覆盖 | Task 2/3/4（各自的单元测试） |
| 测试要求：`drain()`/`quiesce()` 的 deadline-exceeded 分支必须可确定性触发、不依赖真实计时器 race | Task 4/5/10（注入 `deadline_remaining=lambda: -1.0` 类 fixture） |
| 测试要求：quiesce 顺序契约端到端验证 | Task 10（进程内单测锁定调用顺序）+ Task 11（E2E 日志行顺序，`direct` 模式） |
| 测试要求：`LoggingWorker.quiesce()` 独立可测 | Task 5 |
| 测试要求：uvicorn 关停入口的进程级验收 | Phase 1a Task 8（基础 E2E harness）+ Task 11（本计划的扩展断言） |

### 占位符扫描

对全文（约 4000 余行）执行 `grep -n -E "TODO|TBD|类似|以此类推|同理省略"` 与逐行扫描裸 `\.\.\.`：命中分两类，均非未完成占位符——(1) `Protocol`/抽象基类方法体的合法 `def foo(...) -> X: ...` 桩代码（`typing.Protocol` 惯用写法，本身就是最终形态）；(2) Task 8 续 B4 的实现 diff 里，`async_post_call_failure_hook` 方法体内 `...  # 既有异常处理逻辑不变` 与 `# ... 既有方法体其余部分原样保留，只是多缩进一级 ...` 这两处——这是"展示 diff 时省略未改动的既有代码"的惯例写法，明确标注了"不变/原样保留"，不是待补内容；该方法完整的既有实现在真实代码库 `litellm/proxy/hooks/proxy_track_cost_callback.py` 里已经存在，本 Task 只要求把它整体缩进一级、套进 `async with ambient_or_root_scope():`，不要求重新誊写全文。全文零命中真正意义上的未完成占位符（无遗留 `TODO`/`TBD`/裸省略号段落/"类似做法，此处省略"式模糊表述）。全部 11 个 Task 均以可直接执行的失败测试代码 + 实现 diff + 确认转绿步骤 + 提交命令收尾，没有任何一个 Task 只写了大纲。

### 跨 Task 类型一致性核对

- `DrainOutcome`（`Drained | DeadlineExceeded(cancelled, cancellation_failed) | ForcedExit`）与 `LoggingDrainOutcome`（`LoggingDrained | LoggingDeadlineExceeded(dropped_queue_items, cancelled, cancellation_failed) | LoggingForcedExit(dropped_queue_items, cancelled, cancellation_failed)`）两个三变体 tagged union，在定义处（Task 4、Task 5）与全部消费处（Task 10 的三臂 `match`/`assert_never` 分支、Task 11 的日志断言）字段名与变体数量保持一致；`quiesce()`/`drain()` 两处调用都由 Task 10 显式传入 `is_force_exit=GracefulShutdownManager.is_force_exit`，`match` 分支内部没有任何一处再次反查 `is_force_exit()` 去猜测归因。字段名与变体数量已按本轮评审裁决对齐 spec 第 140/176 行字面写法，不再是未决事项（三变体本身是否在生产环境可达，是另一个新发现的独立问题，见下方"已知未决事项"）。
- `AccountingOutcome`（`AccountingCompleted | AccountingSkippedDuringShutdown | AccountingFailed`）在 Task 4 定义、Task 6-9 的全部调用点（`AccountingLease.settle()` 的消费方）里字段与变体数量保持一致；`AccountingSkippedDuringShutdown` 是 Phase 1a 既有变体的直接复用，未重新定义。
- `GracefulShutdownManager.deadline_remaining`/`is_force_exit` 的签名（Phase 1a Task 1 定义：`deadline_remaining() -> float` 无参 classmethod；`is_force_exit() -> bool` 无参 classmethod）与 Phase 1b Task 10 的实际调用方式（作为可调用对象整体传给 `quiesce(deadline_remaining=...)`/`drain(deadline_remaining=...)`，而非在 Task 10 内部重新包一层）完全一致；Task 10 第二条单测已用 PoC 验证过的 `==`（而非 `is`）语义正确断言了这层"透传同一个 classmethod 引用"的契约。
- `AccountingScope`/`CompletionToken` 两个协议（Task 2 定义）在 `AccountingLease`（Task 4，同时实现两者）与 Path A/B 各接入点（Task 6-9）之间的方法签名（`is_valid()`/`spawn()`/`settle()`）保持一致，没有任何接入点对协议做隐式收窄或扩展。

### 依赖图（Task 执行顺序，无法并行的强依赖用 `→`，可并行的用 `∥` 标注）

```
Phase 1a Task 1（GracefulShutdownManager）──┐
Phase 1a Task 7（lifespan 重排）───────────┼──→ Task 10
Phase 1a Task 8（E2E harness）─────────────────────────────→ Task 11

Task 1（ManagedTaskSet）
  → Task 2（CompletionToken/AccountingScope/spawn_detached）
    → Task 3（LoggingTask frozen dataclass + token）
      → Task 4（AccountingOutcome + AccountingLease + ManagedTaskSupervisor）∥ Task 5（LoggingWorker.quiesce）
        → Task 6（Path A 6 处接入）
          → Task 7（Path B1 streaming_handler.py spawn_detached）
            → Task 8（Path B2/B3 记账接入）
              → Task 9（Path B telemetry T1-T18 接入）
                → Task 10（lifespan 9 步 quiesce 接线，同时依赖 Phase 1a Task 1/7）
                  → Task 11（E2E 套件扩展，同时依赖 Phase 1a Task 8）
```

Task 4 与 Task 5 之间没有直接的类型依赖（`AccountingLease`不依赖`LoggingWorker`内部实现，反之亦然），理论上可以并行撰写/实现，但两者都依赖 Task 3 已经把 `LoggingTask.token` 落地，且 Task 6 起的接入工作需要两者都已完成，故排在图中同一层。Task 6-9（四个接入 Task）在文本顺序上是线性写的，但 Task 7（B1）/Task 8（B2/B3）之间除了都依赖 Task 6 建立的 scope 传播链路外没有相互依赖，实现时也可并行，是否并行执行属编排决策，不在本计划内裁定。

### 已知未决事项（均已裁决/已接受，非阻塞——保留本节仅为向未来读者交代结论与理由，不再需要主协调者进一步拍板）

**`force_exit` 路径下 ASGI `lifespan.shutdown()` 从不执行，`ForcedExit`/`LoggingForcedExit` 在生产环境只有一个窄可达窗口（已裁决，见 Task 11 正文"设计说明——`ForcedExit` 分支的可达性边界"）**：撰写 Task 11 时读真实 uvicorn 源码（`uvicorn/server.py` 第 261-294、334-339 行）发现并上报了这一架构事实；主协调者已核实并裁决，结论要点（完整推理见 Task 11 正文，这里不重复全文，只记录结论，避免评审时漏看）：

- 事实：`GracefulShutdownManager.is_force_exit()` 唯一能变为 `True` 的路径（第二次 SIGINT），恰好也是 uvicorn 基类 `Server.shutdown()` 里 `if not self.force_exit: await self.lifespan.shutdown()` 跳过 lifespan shutdown 事件的唯一路径。
- **裁决 1**：不改 Phase 1a 已冻结的 `DrainingServer`——uvicorn 在 `force_exit` 时跳过 `lifespan.shutdown()` 是正确语义（第二次 SIGINT 的 operator 意图就是"别排空了，立刻退"），强行让 lifespan 在 `force_exit` 下仍跑完，等于让"强退"不强退。
- **裁决 2**：保留三变体 `ForcedExit`——它的可达窗口真实存在但窄，仅当第二次 SIGINT 落在"quiesce 已经在 `lifespan.shutdown()` 里运行"这个时间点才会命中；落在 uvicorn 自己的连接排空阶段（`_wait_tasks_to_complete`）则不可达，这是 uvicorn 架构决定的时序窗口，不是本计划的 bug。
- **裁决 3**：Task 11 的 E2E 只断言 user-observable 行为（第二次 SIGINT 后进程远快于完整 deadline 退出），不断言 `ForcedExit` 专属日志；`ForcedExit`/`LoggingForcedExit` 两个变体由 Task 4/5 的确定性单测（注入 `is_force_exit=lambda: True`）覆盖，已经完成。Task 11 的 Step 2b 已按此定稿，不再是 pending 状态。
- 与"已定裁决：三变体"的关系：三变体 union 本身的裁决（`Drained | DeadlineExceeded | ForcedExit`，字段名 `cancelled`/`cancellation_failed`）与本项是两件独立已裁决事项，均不影响 Task 4/5/10 已经完成的类型契约与实现，无需回头改动。

**Task 8 B4（`async_post_call_failure_hook`）"掉队请求"残余竞态：已接受、非阻塞**：`wait_for_drain()` 因 deadline 到期而提前放弃、但仍有请求真正在途时，`close_root_admission()` 会在这些"掉队"请求跑完前提前触发；若这些请求随后失败并落到 `async_post_call_failure_hook`，此时 root admission 已关闭，`acquire_root_scope()` 返回 `None`，`spawn_detached`/`ambient_or_root_scope()` 退化为裸 `create_task`/无 ambient scope（完整推理见 Task 8 续"B4"小节"唯一的残余风险"段落）。这是一个 deadline 附近的窄时间窗口竞态，且与 B1/B2/B3 成功路径共享同一种"掉队请求"性质，不是 Task 8 独有、也不是 Task 8/本计划能单独消除的架构性权衡——spec 的非目标已明确"关停期允许少量未 flush spend 丢失"，deadline 机制本身加上 `spawn_detached` 的 ad hoc root 兜底已经把这条残余竞态收敛到一个有界（bounded）的尽力而为区间。按主协调者裁决，接受为已知残余、不再要求新增机制去封堵这个窗口。

## Kick-off Prompt

（可直接复制到新会话或委派给 implementer 型 agent 使用；执行者应为 `gpt-souls:implementer` 或同等角色，本计划撰写阶段不代主协调者指定执行主体，是否新起会话/是否委派 agent 属编排决策。）

```
你将执行一份已完成 3 轮撰写、待评审的 TDD 实施计划：
docs/superpowers/plans/2026-07-14-graceful-shutdown-phase1b.md

前置条件（必须先确认，而非假设）：
1. docs/superpowers/plans/2026-07-14-graceful-shutdown-phase1a.md 是否已经在当前仓库落地？
   用 `git log --oneline --all | grep -i "graceful shutdown\|GracefulShutdownManager"` 及直接读
   litellm/proxy/shutdown/graceful_shutdown_manager.py 核实：如果该文件目前只有
   is_shutting_down/get_timeout/start_shutdown/wait_for_drain/reset 五个方法（没有
   deadline_remaining/request_force_exit/is_force_exit），说明 Phase 1a 尚未落地，必须先执行
   Phase 1a 计划全部 Task，再回到本计划——不要跳过直接做 Phase 1b，Task 4 起的大量代码直接依赖
   Phase 1a Task 1 的 GracefulShutdownManager 新增接口，Task 10 直接依赖 Phase 1a Task 7 的
   lifespan 重排结果和 Task 8 的 E2E harness。
2. 读一遍 docs/superpowers/specs/2026-07-14-in-flight-observability-graceful-shutdown-design.md
   全文（214 行，已冻结），尤其第 C 节（9 步 quiesce 协议）和"测试"一节，建立整体上下文。

执行顺序：严格按 Task 1 → 2 → 3 → 4/5（可并行）→ 6 → 7 → 8 → 9 → 10 → 11 的顺序执行，每个 Task
遵循 TDD：先写失败测试（跑一遍确认真的失败，且失败原因是"功能未实现"而不是环境/依赖问题）→
写最小实现使其转绿 → 跑 Task 里列出的"确认转绿"命令 → 跑 `make pre-commit`（若失败则修复后重新跑，
涉及 ruff-strict-budget.json/type-discipline-budget.json/basedpyright-code-budget.json 的收紧要
跑 `make lint-budget-update` 并把降低后的预算一并提交）→ 按 Task 里写好的 commit message 提交。

不要因为"看起来工作量大"或"这部分暂时用不上"而跳过、合并、简化任何一个 Task 或其中列出的验收步骤
——这是一份经过 against-yagni-on-feature 原则把关的计划，每个 Task 都对应 spec 里的一条已接受验收
要求（完整映射见计划正文"自检"一节的"Spec 覆盖审计"表）。

如果在实现过程中发现计划文本与真实代码库不符（比如某个被引用的文件/行号/函数签名已经变了，或者
某个假设的前置状态其实不成立），不要静默调整范围或悄悄改写逻辑绕过去——先如实记录发现了什么、
为什么和计划假设不符，再决定：如果是纯粹的事实性偏差（行号漂移、变量改名，但语义不变），可以自行
修正并在 commit message 或 Task 旁注里说明；如果偏差会改变验收范围、架构合同、或某个已经写死的接口
形状，停下来向主协调者报告，不要自行拍板。

Task 4/5/10 已经按 spec 第 140/176 行字面要求实现了三变体 `DrainOutcome`/`LoggingDrainOutcome`
（`Drained | DeadlineExceeded(cancelled, cancellation_failed) | ForcedExit`），这部分不需要在实现
阶段重新纠结。本计划文档末尾"自检"一节的"已知未决事项"记录了撰写 Task 11 时才浮现的一个架构
问题——uvicorn 基类在"第二次 SIGINT 强制退出"这条路径上会跳过整个 ASGI `lifespan.shutdown()` 事件，
而 Task 10 的整套 quiesce 协议（含 `ForcedExit` 分支）恰好就活在这个事件里——主协调者已经就此裁决
并定稿：不改 Phase 1a 的 `DrainingServer`；保留三变体 `ForcedExit`（生产环境有窄但真实的可达窗口）；
Task 11 的 E2E 只断言"第二次 SIGINT 后进程远快于完整 deadline 退出"这一 user-observable 行为，不
断言 `ForcedExit` 专属日志（那两个变体由 Task 4/5 的确定性单测覆盖）。Task 11 的 Step 2b 已按此定稿
写好，实现阶段按 Step 2b 原文执行即可，不需要再做任何选择或等待进一步裁决。

每完成一个 Task 就更新一次本计划文档里对应 Task 的状态（如果计划文档还没有状态追踪字段，建议在每个
Task 标题后追加 "（已完成，commit <hash>）"，保持 sync-plan-with-impl）。全部 11 个 Task 完成后，
按项目惯例发起一次 review-merged-state 级别的整体评审（覆盖 Task 1-11 累积效果，而不仅是逐 Task
评审），再交付。
```

（收尾自检 + Kick-off Prompt 已完成撰写。）
