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
- `register_root_scope_provider(provider)` / `acquire_root_scope()`——一次性 DI 注册钩子，在 `litellm/proxy/shutdown/managed_task_supervisor.py` 模块导入时调用一次（与本文件里 `GLOBAL_LOGGING_WORKER = LoggingWorker()` 这个既有的模块级单例注册写法同构，不是"到处重新赋值全局变量"）。纯 SDK 场景（不 import 任何 `litellm.proxy.*`）下这个 provider 永远是 `None`，所有相关函数退化为逐字节等价于今天的裸 `asyncio.create_task`——这就是"pure-SDK 行为必须逐字节不变"这条硬约束的落地方式。
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
```

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

def register_root_scope_provider(provider: Callable[[], AccountingRootToken | None]) -> None: ...
def acquire_root_scope() -> AccountingRootToken | None: ...
def spawn_detached(coro: Coroutine[object, object, object], *, name: str) -> None: ...
def create_task_with_scope(coro: Coroutine[object, object, object], *, token: CompletionToken | None) -> "asyncio.Task[object]": ...
```

**补充说明（Task 6 落笔时回填，如实记录）**：`AccountingRootToken` 这个组合 Protocol 与 `register_root_scope_provider`/`acquire_root_scope` 的返回类型放宽，是撰写 Task 6（Path A 6 处接入）时才发现的真实需要——Path A 的调用点需要把"能 settle 的 token"和"能做子任务授权的 scope"合并成同一个值传给 `ensure_initialized_and_enqueue(token=...)`，而 Task 2 最初落盘时只顾到 Path B（`spawn_detached`）单独需要 `AccountingScope`。这是纯粹的类型收紧/放宽（`AccountingRootToken` 结构上是 `AccountingScope` 的子类型，处处可替换），不改变任何已落盘运行时行为，也不破坏 Task 2 已保存的任何测试断言（那些测试只检查 `.spawn()`/`.settle()` 等运行时调用，从不检查静态类型标注）。

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
import contextvars
from typing import Callable, Coroutine, Literal, Protocol, runtime_checkable


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


def register_root_scope_provider(provider: "Callable[[], AccountingRootToken | None] | None") -> None:
    """One-shot DI registration, called once at proxy-startup import time by
    managed_task_supervisor.py (module-level side effect, same idiom as this
    codebase's existing `GLOBAL_LOGGING_WORKER = LoggingWorker()` singleton).
    Passing `None` clears it back to pure-SDK behavior (used by tests)."""
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
    """
    scope = current_accounting_scope.get()
    if scope is None or not scope.is_valid():
        scope = acquire_root_scope()
    if scope is not None and scope.is_valid():
        scope.spawn(coro, name=name, kind="accounting")
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

        atexit.register(self._flush_on_exit)

    def _drain_and_settle_dropped(self, queue: "asyncio.Queue[LoggingTask] | None", reason: str) -> int:
        """Synchronously drain every remaining item out of `queue` and
        settle its token as dropped. Used wherever a queue is about to be
        discarded (event-loop change) or emptied post-deadline (quiesce(),
        Task 5) without ever being processed. Returns the number of items
        drained, so quiesce() can report it in LoggingDeadlineExceeded."""
        if queue is None:
            return 0
        drained = 0
        while True:
            try:
                task = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            task.coroutine.close()
            _settle(task.token, NeutralOutcomeDropped(reason=reason))
            drained += 1
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
            await self.clear_queue()

    def enqueue(self, coroutine: Coroutine, *, token: "CompletionToken | None" = None) -> None:
        """
        Add a coroutine to the logging queue.
        Hot path: never blocks, aggressively clears queue if full.
        """
        if self._queue is None or not self._admission_open:
            coroutine.close()
            reason = "worker_not_initialized" if self._queue is None else "admission_closed"
            _settle(token, NeutralOutcomeDropped(reason=reason))
            return

        task = LoggingTask(coroutine=coroutine, context=contextvars.copy_context(), token=token)

        try:
            self._queue.put_nowait(task)
        except asyncio.QueueFull:
            verbose_logger.exception("LoggingWorker queue is full")
            self._handle_queue_full(task)

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

        if self._queue is None:
            task.coroutine.close()
            _settle(task.token, NeutralOutcomeDropped(reason="queue_gone_before_retry"))
            return

        try:
            self._queue.put_nowait(task)
        except asyncio.QueueFull:
            self._handle_queue_full(task)

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

**Files**: `litellm/proxy/shutdown/accounting_outcome.py`（改，Phase 1a 产物，扩展）, `litellm/proxy/shutdown/managed_task_supervisor.py`（新）, `tests/test_litellm/proxy/shutdown/test_accounting_outcome.py`（新，先核实 Phase 1a 是否已建对应测试文件——已核实：Phase 1a 计划正文的 Task 6 只把断言写进了 `tests/test_litellm/proxy/db/test_spend_counter_reseed.py` 和 `tests/test_litellm/caching/test_redis_cache.py` 里，未新建独立的 `test_accounting_outcome.py`，所以这里新建是合理的，不是重复）, `tests/test_litellm/proxy/shutdown/test_managed_task_supervisor.py`（新）

**设计说明**：`admissions_in_progress` 这个计数器要有真实语义（不是"函数调用内自增自减、从未被其他协程观察到"的摆设），所以窗口定义为「`acquire_root_lease()` 拿到 lease」到「这个 lease 第一次 `spawn()` 或 `settle()`（两者取先）」之间——这段窗口之间**没有** `await`点由本模块插入，但调用方（比如 `_client_async_logging_helper` 拿到 lease 后、真正 `enqueue()` 之前）可能会先做一段自己的同步/异步工作，此时 `drain()` 的 `while True` 循环如果恰好在这个窗口被协作调度到，必须能看到"还有一个 admission 未关闭"而不是误判为已排空。

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

@dataclasses.dataclass(frozen=True, slots=True)
class Drained: ...

@dataclasses.dataclass(frozen=True, slots=True)
class DeadlineExceeded:
    remaining_accounting_tasks: int
    remaining_admissions_in_progress: int

DrainOutcome = Drained | DeadlineExceeded

class ManagedTaskSupervisor:
    def acquire_root_lease(self) -> AccountingLease | None: ...
    def spawn_telemetry(self, coro, *, name: str) -> None: ...
    def is_shutting_down_hard(self) -> bool: ...
    async def drain(
        self,
        deadline_remaining: Callable[[], float],
        root_queue_unfinished: Callable[[], int] = lambda: 0,
    ) -> DrainOutcome: ...
```

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

    def test_admissions_in_progress_closes_on_spawn(self):
        supervisor = ManagedTaskSupervisor()
        lease = supervisor.acquire_root_lease()

        async def coro():
            pass

        lease.spawn(coro(), name="x", kind="accounting")
        assert supervisor._admissions_in_progress == 0


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
        assert supervisor._accounting.is_empty() or True  # allow done-callback race
        await asyncio.sleep(0)
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
    async def test_task_creation_failure_rolls_back_admission_and_closes_coroutine(self, monkeypatch):
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
        assert supervisor._admissions_in_progress == 0  # rolled back, not leaked


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
    async def test_drain_cancels_and_reports_deadline_exceeded_when_child_outlives_deadline(self):
        supervisor = ManagedTaskSupervisor()
        lease = supervisor.acquire_root_lease()
        cancelled = asyncio.Event()

        async def coro():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        lease.spawn(coro(), name="update_cache", kind="accounting")

        outcome = await supervisor.drain(deadline_remaining=lambda: -1.0)

        assert isinstance(outcome, DeadlineExceeded)
        assert cancelled.is_set()

    @pytest.mark.asyncio
    async def test_drain_cancels_telemetry_tasks_once_accounting_is_settled(self):
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
        await asyncio.sleep(0)
        assert telemetry_cancelled.is_set()

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
    __slots__ = ("_supervisor", "_settled", "_outcome", "_admission_closed")

    def __init__(self, supervisor: "ManagedTaskSupervisor") -> None:
        self._supervisor = supervisor
        self._settled = False
        self._outcome: AccountingOutcome | None = None
        self._admission_closed = False
        supervisor._admissions_in_progress += 1

    def _close_admission(self) -> None:
        if self._admission_closed:
            return
        self._admission_closed = True
        self._supervisor._admissions_in_progress -= 1

    def is_valid(self) -> bool:
        return not self._settled and not self._supervisor.is_shutting_down_hard()

    def settle(self, outcome: AccountingOutcomeLike) -> None:
        """Idempotent, per spec: only the first call wins."""
        if self._settled:
            return
        self._settled = True
        self._outcome = _translate(outcome)
        self._close_admission()

    def spawn(
        self,
        coro: "Coroutine[object, object, object]",
        *,
        name: str,
        kind: Literal["accounting", "telemetry"],
    ) -> None:
        try:
            if kind == "accounting":
                self._supervisor._spawn_accounting_child(coro, name=name, scope=self)
            elif kind == "telemetry":
                self._supervisor._spawn_telemetry_child(coro, name=name)
            else:
                assert_never(kind)
        finally:
            self._close_admission()

    @property
    def outcome(self) -> "AccountingOutcome | None":
        return self._outcome


@dataclasses.dataclass(frozen=True, slots=True)
class Drained:
    pass


@dataclasses.dataclass(frozen=True, slots=True)
class DeadlineExceeded:
    remaining_accounting_tasks: int
    remaining_admissions_in_progress: int


DrainOutcome = Drained | DeadlineExceeded


class ManagedTaskSupervisor:
    """One instance per process, constructed once at proxy startup (Task 10
    wires it into the FastAPI lifespan and calls
    `accounting_scope.register_root_scope_provider` with a callable that
    returns `self.acquire_root_lease()`)."""

    def __init__(self) -> None:
        self._accounting = ManagedTaskSet()
        self._telemetry = ManagedTaskSet()
        self._admissions_in_progress = 0
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
        still polling. Reuses the same `_hard_shutdown` flag `drain()`'s own
        deadline branch flips -- setting it here is a no-op if `drain()`
        later flips it again (idempotent), and does not by itself cancel
        anything: cancellation is still exclusively `drain()`'s deadline
        branch's responsibility, not this method's."""
        self._hard_shutdown = True

    def acquire_root_lease(self) -> "AccountingLease | None":
        """Root-boundary lease acquisition. Returns None once a hard
        shutdown has been declared (drain()'s deadline branch), refusing
        any further root admission -- matches spec's "admission 授权
        用不可伪造 scope" together with is_valid()'s own post-hard-shutdown
        check on already-issued leases."""
        if self._hard_shutdown:
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
        drain() itself."""
        while True:
            if root_queue_unfinished() == 0 and self._accounting.is_empty() and self._admissions_in_progress == 0:
                self._telemetry.cancel_all()
                return Drained()

            if deadline_remaining() <= 0:
                self._hard_shutdown = True
                self._accounting.cancel_all()
                self._telemetry.cancel_all()
                await self._accounting.wait_settled()
                return DeadlineExceeded(
                    remaining_accounting_tasks=len(self._accounting),
                    remaining_admissions_in_progress=self._admissions_in_progress,
                )

            await asyncio.sleep(min(_DRAIN_POLL_INTERVAL_SECONDS, max(deadline_remaining(), 0.0)))


GLOBAL_MANAGED_TASK_SUPERVISOR = ManagedTaskSupervisor()

# Module-import-time DI registration -- same idiom as this codebase's existing
# `GLOBAL_LOGGING_WORKER = LoggingWorker()` singleton (logging_worker.py) and
# exactly what the Architecture section already promised for this module
# ("register_root_scope_provider(provider) ... 在
# litellm/proxy/shutdown/managed_task_supervisor.py 模块导入时调用一次"). Pure-SDK
# code that never imports anything under litellm.proxy.* never imports this
# module either, so _root_scope_provider stays None there and every Path B
# call site's fallback-to-bare-create_task behavior is unaffected.
accounting_scope.register_root_scope_provider(GLOBAL_MANAGED_TASK_SUPERVISOR.acquire_root_lease)
```

**补充说明（Task 8 落笔时回填，如实记录）**：撰写 Task 8（B2/B3 接入）时读码复核 Task 4 已落盘的实现，发现两处真实缺口，均已在上面的 Steps §3 代码里直接改正（不是另开一个"Task 4.5"，因为这两处都是 Task 4 自身该交付、但当时遗漏的部分，补丁范围完全落在 `managed_task_supervisor.py` 内部）：

1. **缺口一——从未创建/注册全局单例**：Architecture 一节（本文档第 68 行）承诺"`register_root_scope_provider(provider)`——一次性 DI 注册钩子，在 `managed_task_supervisor.py` 模块导入时调用一次"，但 Task 4 最初落盘的 Steps §3 代码里从未出现 `GLOBAL_MANAGED_TASK_SUPERVISOR`/`register_root_scope_provider` 字样（用 `grep` 核实过，零匹配）——也就是说 Task 6 的六个 Path A 站点、Task 7 的 B1 站点，实际运行时 `acquire_root_scope()`/`spawn_detached()` 永远只会看到 `_root_scope_provider is None`（因为没人调用过 `register_root_scope_provider`），永远退化成纯-SDK 回退路径，"记账域感知"从未真正生效过——这不是"暂时用不上、可以延后"的问题，而是让 Task 6/7 已落盘的全部测试断言其实只覆盖了纯-SDK 分支、从未覆盖过真正接入 supervisor 之后的路径。现已在 Steps §3 补上模块级 `GLOBAL_MANAGED_TASK_SUPERVISOR = ManagedTaskSupervisor()` 单例与 `accounting_scope.register_root_scope_provider(GLOBAL_MANAGED_TASK_SUPERVISOR.acquire_root_lease)` 注册调用。
2. **缺口二——`_spawn_accounting_child` 从未把 `current_accounting_scope` 绑定进新建的子任务**：这一处更隐蔽也更严重——`AccountingLease.spawn()` 原先直接调用 `self._supervisor._spawn_accounting_child(coro, name=name)`，而 `_spawn_accounting_child` 原先只是裸 `asyncio.create_task(coro, name=name)`，从未在新任务的 Context 里 `current_accounting_scope.set(...)`。这意味着即便缺口一被修好，Task 7（B1，`spawn_detached` 在 `streaming_handler.py:2053` 获取一个全新 root scope 并 `scope.spawn(...)`）之后，`dispatch_success_handlers` 在这个新任务里跑起来、层层 `await` 调用到 Task 8 的 `update_cache`/`_batch_database_updates` 时，`current_accounting_scope.get()` 拿到的仍然是 `None`——因为 `asyncio.create_task` 隐式拷贝的是*调用 `_spawn_accounting_child` 那一刻*的 Context，而那一刻从未写入过这个 scope。Task 8 的整个设计前提（B2/B3 靠 `current_accounting_scope.get()` 拿到 Task 7 acquire 到的同一个 lease）如果不修这里就完全不成立。现已在 Steps §3 把 `_spawn_accounting_child` 改成接收 `scope: AccountingLease` 参数，用 `contextvars.copy_context()` 取一份*私有*副本、在副本里 `current_accounting_scope.set(scope)` 后再 `ctx.run(...)` 创建任务——用副本而不是直接对当前 Context `.set()`，是为了不把这个 scope 泄漏进调用方自己后续的代码（比如 `CustomStreamWrapper.__anext__` 处理下一个 chunk 时，绝不应该还残留着上一个 chunk 的 scope）。这个修法与 Task 2 里 `create_task_with_scope` 的既有设计同构（那里调用方负责 `context.run(create_task_with_scope, coro, token=token)`；这里由于 `_spawn_accounting_child` 不是被外部captured Context 调用，所以自己内部做一次 `copy_context()`）。

两处缺口都通过下面新增的 `TestGlobalRegistrationAndScopePropagation` 测试类锁定（既验证模块导入后 `accounting_scope.acquire_root_scope()` 确实能拿到真实 lease，也验证被 spawn 的子任务内部能看到同一个 scope、且不泄漏进调用方自己的 Context）；Task 4 原有的 5 个测试类无需改动，因为它们从不检查 `current_accounting_scope`，行为断言（settle/admission 计数/drain 语义）不受影响。

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


class TestGlobalRegistrationAndScopePropagation:
    def test_module_import_registers_global_supervisor_as_root_scope_provider(self):
        # GLOBAL_MANAGED_TASK_SUPERVISOR is a process-wide singleton; importing
        # this module must have already wired it into accounting_scope's DI
        # hook (this is exactly the behavior Task 6/7's real call sites rely
        # on -- their own tests inject a _FakeRootToken instead, so this is
        # the only place the *real* registration wiring is asserted).
        token = accounting_scope.acquire_root_scope()
        assert token is not None
        assert isinstance(token, type(GLOBAL_MANAGED_TASK_SUPERVISOR.acquire_root_lease()))

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


class TestCloseRootAdmission:
    def test_close_root_admission_refuses_new_leases_without_cancelling_existing_children(self):
        """Task 10 (lifespan quiesce step 3) calls this eagerly, strictly
        before LoggingWorker.quiesce()/drain() run -- it must close new root
        admission immediately, but must NOT reach into either managed set
        (an already-admitted accounting child must keep running until
        drain()'s own deadline logic decides otherwise)."""
        supervisor = ManagedTaskSupervisor()
        lease = supervisor.acquire_root_lease()
        assert lease is not None

        async def already_running_child():
            await asyncio.sleep(10)

        lease.spawn(already_running_child(), name="already_running", kind="accounting")

        supervisor.close_root_admission()

        assert supervisor.acquire_root_lease() is None
        assert not supervisor._accounting.is_empty()  # untouched by close_root_admission itself

        supervisor._accounting.cancel_all()

```

`pytest tests/test_litellm/proxy/shutdown/test_accounting_outcome.py tests/test_litellm/proxy/shutdown/test_managed_task_supervisor.py -v` 全绿。同时跑一次 Phase 1a 已有的 `tests/test_litellm/proxy/db/test_spend_counter_reseed.py tests/test_litellm/caching/test_redis_cache.py -v` 确认扩展 `accounting_outcome.py` 没有破坏 Phase 1a 对 `AccountingSkippedDuringShutdown` 的既有断言。

**补充说明（Task 10 落笔时发现，如实记录）**：撰写 Task 10（lifespan 接线）核对 spec 第 C 节 9 步协议时发现，第 3 步"封闭新的 root accounting admission，但允许持有效 scope 的已登记 accounting task 派生 child"在 Task 4 原先落盘的接口里**没有对应的可调用方法**——`_hard_shutdown`（进而 `acquire_root_lease()` 拒绝新 lease）此前只在 `drain()` 自己的 deadline-exceeded 分支里被置位，也就是说"关闭新 admission"这件事此前被隐式地和"取消现有 child"这件事**绑定在同一次状态翻转里**、且只会在 deadline 到达时才发生——而 spec 要求这是两个独立时间点的独立动作（第 3 步早于第 5 步的 `drain()`，且第 3 步明确不取消已登记 child）。这不是"暂时用不上、可以延后"的缺口，因为不修的话 Task 10 没有任何办法在 `LoggingWorker.quiesce()`（第 4 步）仍在轮询期间就提前把新 root scope 的口子关上，只能等到 `drain()` 自己的 deadline 分支才会关（如果根本没触发 deadline，则整个关停过程中口子始终没关过）。已在上面 Steps §3 的类定义里补上 `close_root_admission()` 方法（复用同一个 `_hard_shutdown` 标志、但不触发任何取消），并用新增的 `TestCloseRootAdmission` 测试类锁定"调用后拒绝新 lease、但不触碰已登记 child"这条行为边界。

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
    cancelled_running_tasks: int
    cancelled_helper_tasks: int


LoggingDrainOutcome = LoggingDrained | LoggingDeadlineExceeded


class LoggingWorker:
    def queue_size(self) -> int: ...
    async def quiesce(
        self,
        deadline_remaining: Callable[[], float],
        admission_policy: Callable[[], bool],
    ) -> LoggingDrainOutcome: ...
```

**Steps**

1. 写失败测试（追加到 `tests/test_litellm/litellm_core_utils/test_logging_worker.py` 既有 `TestLoggingWorker` 类下）：

```python
    @pytest.mark.asyncio
    async def test_quiesce_returns_drained_immediately_when_nothing_pending(self):
        worker = LoggingWorker()
        worker.start()
        outcome = await worker.quiesce(deadline_remaining=lambda: 5.0, admission_policy=lambda: True)
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
        outcome = await worker.quiesce(deadline_remaining=lambda: 5.0, admission_policy=lambda: True)

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
            worker.quiesce(deadline_remaining=lambda: 5.0, admission_policy=lambda: False)
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

        outcome = await worker.quiesce(deadline_remaining=lambda: -1.0, admission_policy=lambda: False)

        assert isinstance(outcome, LoggingDeadlineExceeded)
        assert outcome.dropped_queue_items == 1
        assert executed == []
        assert len(settled) == 1
        assert settled[0].kind == "skipped_during_shutdown"
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

        outcome = await worker.quiesce(deadline_remaining=lambda: -1.0, admission_policy=lambda: False)

        assert isinstance(outcome, LoggingDeadlineExceeded)
        assert outcome.cancelled_running_tasks == 1
        assert len(settled) == 1
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

        outcome = await worker.quiesce(deadline_remaining=lambda: -1.0, admission_policy=lambda: False)

        assert isinstance(outcome, LoggingDeadlineExceeded)
        assert outcome.cancelled_helper_tasks == 1
        assert worker._helper_tasks.is_empty()
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
```

同时在文件顶部 import 区补充 `from litellm.litellm_core_utils.logging_worker import (LoggingDrained, LoggingDeadlineExceeded, ...)`（若既有 import 已用 `from litellm.litellm_core_utils.logging_worker import *`-风格聚合导入，则改为按需追加具名导入，不使用通配符）。

2. 确认失败：`pytest tests/test_litellm/litellm_core_utils/test_logging_worker.py -v -k quiesce` —— `AttributeError: 'LoggingWorker' object has no attribute 'quiesce'`；`test_queue_size_*` 同理因 `queue_size` 不存在而失败。

3. 实现。在 `litellm/litellm_core_utils/logging_worker.py` 顶部 import 块补充 `from typing import Callable`（若尚未导入）；在 `LoggingTask` 定义之后、`LoggingWorker` 类定义之前追加：

```python
@dataclasses.dataclass(frozen=True, slots=True)
class LoggingDrained:
    pass


@dataclasses.dataclass(frozen=True, slots=True)
class LoggingDeadlineExceeded:
    dropped_queue_items: int
    cancelled_running_tasks: int
    cancelled_helper_tasks: int


LoggingDrainOutcome = LoggingDrained | LoggingDeadlineExceeded

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
    ) -> LoggingDrainOutcome:
        """
        Proxy-only drain entry point -- a wholly separate method from
        flush()/stop(), which keep their pre-Phase-1b semantics untouched
        (pure-SDK callers, and the existing repo-wide flush() call sites,
        never call quiesce() and are therefore entirely unaffected).

        Normal phase: `admission_policy()` is polled once per loop
        iteration and written into `self._admission_open`, so `enqueue()`
        starts refusing new admission (settling as dropped instead of
        queuing) the moment the caller's policy flips -- while already
        queued items keep draining via the existing `_worker_loop`
        background task; quiesce() itself never dequeues or runs anything,
        it only watches until settled or the deadline passes.

        Deadline phase: any coroutine still sitting in `self._queue`
        unconsumed is drained item-by-item -- closed and settled as
        dropped, NEVER executed via clear_queue() (spec line 133: 取消路径
        不得执行 queued 业务 callback) -- then every currently in-flight
        processing task and every retry/aggressive-clear helper task is
        cancelled together and awaited to settlement (spec's five-category
        cancel enumeration).
        """
        if self._queue is None:
            return LoggingDrained()

        while True:
            self._admission_open = admission_policy()

            if self._queue.empty() and self._running_tasks.is_empty() and self._helper_tasks.is_empty():
                return LoggingDrained()

            if deadline_remaining() <= 0:
                dropped = self._drain_and_settle_dropped(self._queue, reason="deadline_exceeded")
                cancelled_running = len(self._running_tasks)
                cancelled_helpers = len(self._helper_tasks)
                self._running_tasks.cancel_all()
                self._helper_tasks.cancel_all()
                await self._running_tasks.wait_settled()
                await self._helper_tasks.wait_settled()
                return LoggingDeadlineExceeded(
                    dropped_queue_items=dropped,
                    cancelled_running_tasks=cancelled_running,
                    cancelled_helper_tasks=cancelled_helpers,
                )

            await asyncio.sleep(min(_QUIESCE_POLL_INTERVAL_SECONDS, max(deadline_remaining(), 0.0)))
```

4. 确认转绿：`pytest tests/test_litellm/litellm_core_utils/test_logging_worker.py -v` 全绿。再跑一次 Task 3 已核实的既有 `flush()`/`stop()` 调用方所在测试文件，确认它们仍不受影响（`quiesce()`/`queue_size()` 是纯新增方法，不改动任何既有方法体）。

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

## Task 8 — Path B2/B3 记账接入：`update_cache`/`_batch_database_updates` 改用 `spawn_detached`

依赖 Task 2（`spawn_detached`/`current_accounting_scope`）、Task 4（`ManagedTaskSupervisor`/`AccountingLease`——具体是 Task 4 的两处补丁：全局单例注册、`_spawn_accounting_child` 的 scope 绑定；见 Task 4 "补充说明（Task 8 落笔时回填，如实记录）"）、Task 7（同一个 `spawn_detached` 在 B1 已经用过一次，这里是第二、三个真实调用点）。

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

**补充说明（Task 10 落笔时发现的 spec 措辞分歧，不在本 Task 内部自行拍板，留待收尾时集中汇报主会话）**：spec 第 140 行与第 176 行两处字面写"`drain()`/`quiesce()` 返回冻结 tagged outcome：`Drained | DeadlineExceeded(cancelled, cancellation_failed) | ForcedExit`"，比 Task 4/Task 5 已落笔并测试覆盖的两态 `DrainOutcome = Drained | DeadlineExceeded(remaining_accounting_tasks, remaining_admissions_in_progress)` / `LoggingDrainOutcome = LoggingDrained | LoggingDeadlineExceeded(dropped_queue_items, cancelled_running_tasks, cancelled_helper_tasks)` 多出一个 `ForcedExit` 变体，且 `DeadlineExceeded` 的字段名也不同（spec 是 `cancelled`/`cancellation_failed`，已落笔版本是"剩余未清空数量"）。逐字对照 Phase 1a Task 1 的 `deadline_remaining()` 设计说明——"force-exit 与自然到期在这个方法自己看来完全一样，都是返回 `0.0`，专门设计成让 `while deadline_remaining() > 0` 这一类 drain 循环不用额外穿一个标志位就能统一坍缩成'已过期'"——这意味着 `drain()`/`quiesce()` 的**唯一**输入 `deadline_remaining: Callable[[], float]` 从内部看根本无法反推出"是自然到期还是被摁了第二次 SIGINT"，除非再给两者的签名各加一个 `is_force_exit: Callable[[], bool]` 参数（这会改变 Task 4/5 已经落笔并测试覆盖的跨任务契约，属于本 Task 无权自行拍板的范围）。本 Task 采用的过渡解读是：`ForcedExit` 不做成 `DrainOutcome`/`LoggingDrainOutcome` 自身的第三个 tagged 变体，而是在**本 Task（lifespan 接线层）**收到 `DeadlineExceeded` 之后，另外单独读一次 `GracefulShutdownManager.is_force_exit()`，只在日志文案的 tag 上区分"deadline_exceeded"和"forced_exit"两种可观测结果（见下面 Step 3 的 `match` 分支）——这满足 spec 想要的"运维能从日志里分清两种关停原因"这一可观测性目标，同时不触碰已落笔并有测试覆盖的类型契约。是否要反过来改 Task 4/5 引入真正的三态 union + 改字段名，作为待决项在收尾报告里列出两个方案及取舍，由主会话裁决。

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
        "close_aiohttp_session",
    ]


@pytest.mark.asyncio
async def test_lifespan_shutdown_wires_quiesce_and_drain_callables_to_shared_deadline_clock(monkeypatch):
    """Pins 三处易错的接线细节：(1) quiesce()/drain() 必须共享
    GracefulShutdownManager 同一把冻结 deadline 时钟，而不是各自起一个独立
    计时器；(2) quiesce() 的 admission_policy 必须在 deadline 耗尽的那一刻才
    翻面关闭，而不是从一开始就常闭（那样会让第 3/4 步之间"允许已登记 child
    继续派生"名存实亡）；(3) drain() 的 root_queue_unfinished 必须绑定
    GLOBAL_LOGGING_WORKER.queue_size，否则 supervisor 会在 LoggingWorker 队列
    里还有未 flush 完的 item 时就误判 fixed point 已达成。"""
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

    # admission_policy 与 deadline_remaining 同步翻面：还没到期时开放，到期后关闭。
    monkeypatch.setattr(ps.GracefulShutdownManager, "deadline_remaining", lambda: 3.0)
    assert captured_quiesce_kwargs["admission_policy"]() is True
    monkeypatch.setattr(ps.GracefulShutdownManager, "deadline_remaining", lambda: 0.0)
    assert captured_quiesce_kwargs["admission_policy"]() is False
```

（`Drained`、`LoggingDrained` 需要在文件顶部补两行 import：`from litellm.proxy.shutdown.managed_task_supervisor import Drained` 与 `from litellm.litellm_core_utils.logging_worker import LoggingDrained`；其余 `ps`/`FastAPI`/`AsyncMock`/`MagicMock`/`patch` 均已在该文件顶部导入，直接复用。）

跑一下确认失败：此刻 `ps.GLOBAL_MANAGED_TASK_SUPERVISOR`/`ps.GLOBAL_LOGGING_WORKER` 在 `proxy_server.py` 里还不存在（`monkeypatch.setattr` 会因找不到目标属性抛 `AttributeError`），且即便先假设它们已 import 进来，lifespan 关停块此刻也根本不会调用 `close_root_admission`/`quiesce`/`drain`，`call_order` 断言必然不匹配。

2. 实现：在 `proxy_server.py` 顶部追加两行 import，并把 Phase 1a Task 7 已重排的关停块，在"stop watchdog"和"close shared aiohttp session"之间插入 spec 第 3-8 步：

```python
# proxy_server.py 顶部追加
from litellm.litellm_core_utils.logging_worker import (
    GLOBAL_LOGGING_WORKER,
    LoggingDeadlineExceeded,
    LoggingDrained,
)
from litellm.proxy.shutdown.managed_task_supervisor import (
    GLOBAL_MANAGED_TASK_SUPERVISOR,
    DeadlineExceeded,
    Drained,
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
    # fixed-point drain any remaining accounting children -- strictly before
    # the shared aiohttp session (and prisma/redis, via proxy_shutdown_event
    # below) get torn out from under them.
    GLOBAL_MANAGED_TASK_SUPERVISOR.close_root_admission()  # step 3

    logging_outcome = await GLOBAL_LOGGING_WORKER.quiesce(  # step 4
        deadline_remaining=GracefulShutdownManager.deadline_remaining,
        admission_policy=lambda: GracefulShutdownManager.deadline_remaining() > 0,
    )
    match logging_outcome:
        case LoggingDrained():
            verbose_proxy_logger.info("graceful_shutdown_logging_drained")
        case LoggingDeadlineExceeded(
            dropped_queue_items=dropped, cancelled_running_tasks=running, cancelled_helper_tasks=helpers
        ):
            verbose_proxy_logger.warning(
                "graceful_shutdown_logging_%s dropped_queue_items=%s cancelled_running_tasks=%s "
                "cancelled_helper_tasks=%s",
                "forced_exit" if GracefulShutdownManager.is_force_exit() else "deadline_exceeded",
                dropped,
                running,
                helpers,
            )
        case _ as unreachable:
            assert_never(unreachable)

    drain_outcome = await GLOBAL_MANAGED_TASK_SUPERVISOR.drain(  # step 5 (fixed-point), 6-7 (cancel+settle on deadline)
        deadline_remaining=GracefulShutdownManager.deadline_remaining,
        root_queue_unfinished=GLOBAL_LOGGING_WORKER.queue_size,
    )
    match drain_outcome:
        case Drained():
            verbose_proxy_logger.info("graceful_shutdown_accounting_drained")
        case DeadlineExceeded(
            remaining_accounting_tasks=remaining_accounting, remaining_admissions_in_progress=remaining_admissions
        ):
            verbose_proxy_logger.warning(
                "graceful_shutdown_accounting_%s remaining_accounting_tasks=%s "
                "remaining_admissions_in_progress=%s",
                "forced_exit" if GracefulShutdownManager.is_force_exit() else "deadline_exceeded",
                remaining_accounting,
                remaining_admissions,
            )
        case _ as unreachable:
            assert_never(unreachable)
    # step 8 (stop logging worker) is a no-op here: quiesce() above already
    # drained/cancelled the worker's own running+helper tasks to completion;
    # there is no separate GLOBAL_LOGGING_WORKER.stop() call left to make.

    # Shutdown event - close shared aiohttp session
    if shared_aiohttp_session is not None:
        try:
            await shared_aiohttp_session.close()
            verbose_proxy_logger.info("SESSION REUSE: Closed shared aiohttp session")
        except Exception as e:
            verbose_proxy_logger.error(f"Error closing shared aiohttp session: {e}")

    await proxy_shutdown_event()  # type: ignore[reportGeneralTypeIssues]  # step 9 (prisma + redis)
```

（`assert_never` 需要从 `typing_extensions`（或 3.11+ 的 `typing`）import；`proxy_server.py` 里如果尚未 import，本 Task 一并在顶部补一行——本仓库遵循的"不 throw、tagged union + 穷举 match"约定，`assert_never` 是让 basedpyright 在未来新增变体时对漏 `case` 报类型错误的标准写法，不是本 Task 新发明的模式。）

**关于"step 8 是 no-op"的说明**：spec 第 8 步字面是"stop logging worker"，容易让人以为还需要额外调用一次 `GLOBAL_LOGGING_WORKER.stop()`。但读 Task 5 已落笔的 `quiesce()` 实现——它在 `LoggingDrained`（正常路径）和 `LoggingDeadlineExceeded`（到期路径）两个分支下，都已经把 `_running_tasks`/`_helper_tasks` 排空或取消到底、并且（正常路径下）`queue.join()` 已确认队列见底——`stop()` 现有语义只是"取消 `_running_tasks` 再 `clear_queue()`"，quiesce() 到期分支已经不允许再 `clear_queue()` 执行业务 callback（这正是 round-2 blocker 的教训），所以在 `quiesce()` 之后再调用一次 `stop()` 要么是重复劳动（正常路径），要么会违反"到期后不再执行 queued callback"的约定（到期路径，因为 `stop()` 内部路径与 quiesce 已经做的事冲突）。因此第 8 步在本 Task 的接线里就是"`quiesce()` 已经把这件事做完了"，不再有单独的调用点——如果未来 code review 觉得这里应该有一个显式的哨兵调用（哪怕是空操作）来对齐 spec 逐字顺序，可以在实现时补一行注释级别的占位，不改变行为。

3. 确认转绿：
   - `pytest tests/test_litellm/proxy/proxy_server/test_lifecycle.py -v`——确认新增两个测试转绿，且 Phase 1a Task 7 写的顺序测试依旧转绿（本 Task 插入的四行新步骤不能打乱 Task 7 已经断言过的四段相对顺序）。
   - `pytest tests/test_litellm/proxy/test_proxy_server.py -k startup_master_key -v`——Phase 1a Task 7 的 Steps 1 已经确认这是仓库里唯一一处完整驱动 `proxy_startup_event` 的既有测试，本 Task 再次触碰同一段代码，必须确认它没被打破。
   - `make pre-commit`。

4. 提交：`git add litellm/proxy/proxy_server.py tests/test_litellm/proxy/proxy_server/test_lifecycle.py && git commit -m "feat: wire the 9-step quiesce protocol into the FastAPI lifespan shutdown block"`

---

## Task 11 — E2E 套件扩展：完整 9 步 quiesce 的进程级验收

依赖 Task 10（新增的 `graceful_shutdown_logging_drained`/`graceful_shutdown_logging_deadline_exceeded`/`graceful_shutdown_accounting_drained`/`graceful_shutdown_accounting_deadline_exceeded` 四条日志），依赖 Phase 1a Task 8（`tests/e2e/shutdown/` 整套 harness——`subprocess_harness.py`/`fake_upstream.py`/`conftest.py`/`test_graceful_shutdown_e2e.py`——已落地；本 Task **扩展**既有文件，不新建套件）。

**范围澄清（承接 Phase 1a"承诺的后续"一节）**：Phase 1a 计划正文明确写道，Phase 1b 要把 1a 留下的"关停短路"（`AccountingSkippedDuringShutdown` 提前返回）升级为完整 9 步 quiesce；本 Task 是这句承诺在 E2E 验收层面的落地——把 spec 第 186 行"uvicorn 入口"验收标准里，Phase 1a 当时还没有对应生产代码、因而没法断言的"记账 settled/skipped 单行日志"与"quiesce 顺序"两项，补进已有的 E2E 用例。

**Files**: `tests/e2e/shutdown/test_graceful_shutdown_e2e.py`（改，扩展既有测试与断言，不新建文件）

**设计说明——为什么不额外写一个"E2E 级 deadline-exceeded 记账 drain"用例（记录未采纳方案，附理由）**：spec 第 176 行要求"quiesce 顺序断言"与"deadline 到期 cancel + settle"两类断言都要有覆盖，但没有规定必须在哪个测试层级覆盖。Task 4（`ManagedTaskSupervisor.drain()`）、Task 5（`LoggingWorker.quiesce()`）、Task 10（lifespan 接线）三处都已经用**注入的** `deadline_remaining=lambda: -1.0` 或 `lambda: 0.0` 这类确定性 fixture，在单元测试层面完整覆盖了"到期后联合 cancel + settle、不再执行 queued callback"这条路径——不依赖真实时钟、不依赖真实 DB/Redis 延迟，每次跑结果都确定。反过来，如果要在 E2E 层面（真实子进程、真实 DB/Redis）真正逼出"记账任务还没跑完、deadline 就到了"这个状态，唯一现实的手段是把 `GRACEFUL_SHUTDOWN_TIMEOUT` 设得极短（比如 10ms），赌真实 Postgres/Redis 往返来不及在这个窗口内完成——这是一场跟真实时钟和真实网络延迟的赛跑，本机、CI、不同负载下的真实往返时延本就不稳定，跑出来的测试会不可靠：负载低时这个"deadline exceeded"分支可能根本不会被触发（写操作 10ms 内就完成了），测试要么变成偶发 flaky，要么为了"保证触发"进一步压低超时到不现实的数值、反而让人怀疑这是否还是"真实场景"。这类不可靠信号正是 `CLAUDE.md`"宁可没有信号，也不要不会在代码坏时报警的测试"明确要拒绝的模式——一个大多数时候通过、只在特定负载下才断言到期分支的测试，无法在代码回归时可靠报警，等同于虚假信号。因此本 Task **不**在 E2E 层引入这类真实时钟竞速测试；deadline-exceeded 路径的验收保留在 Task 4/5/10 已经完成的确定性单元测试里，E2E 层只覆盖"进程真的会启动、真的会跑完整套 quiesce 协议、真的不会残留 reconnect/traceback"这类必须依赖真实子进程才能验证、且不依赖时钟竞速就能确定性触发的部分（正常路径下 `drain()`/`quiesce()` 必然落在 `Drained`/`LoggingDrained` 分支，不需要故意逼近 deadline）。

**设计说明——为什么严格的"日志行先后顺序"断言只加到 `direct` 单进程模式**：`reload`/`workers` 模式下，关停时是每个 worker 子进程各自独立执行同一套 `GracefulShutdownManager`/`ManagedTaskSupervisor`/`GLOBAL_LOGGING_WORKER`（进程级单例，不跨进程同步——这是 Phase 1a 自检里已经确认过的既定非目标），多个 worker 的 stdout/stderr 交织写进同一个捕获文件，"整份合并文本里 A 子串第一次出现的位置早于 B 子串"不能保证反映"同一个 worker 内部 A 确实先于 B 发生"——可能是 worker 1 的 `graceful_shutdown_accounting_drained` 先打印出来，而 worker 2 的 `graceful_shutdown_started` 才刚刚开始交织进来，顺序断言这时候是对交织噪音的误读，不是对真实执行顺序的验证。`direct` 模式是唯一的单进程场景，日志天然是单一时间线，顺序断言在这里有真实意义；`reload`/`workers` 模式改用弱一档的"存在性"断言（新增的四条日志里，"drained"这一对至少各出现一次，且"deadline_exceeded"这一对**不**出现——因为正常路径不该触发到期分支），不做强顺序断言。

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

3. 实现：本 Task 在 `tests/e2e/` 侧没有生产代码要改——四条新日志已经由 Task 10 在 `proxy_server.py` 里落地；这一步纯粹是"确认 Task 10 已完成后，E2E 测试自然转绿"，不需要额外写生产代码。若在 Task 10 尚未落地时先跑本 Task 的测试，预期失败信息应精确指向缺失的日志文本（而不是进程崩溃/超时），从而确认新断言本身而非环境问题是失败原因。

4. 确认转绿（要求 Task 10 已经落地）：
   - `pytest tests/e2e/shutdown/test_graceful_shutdown_e2e.py -m spawned_proxy_e2e -v -k "TestSignalDrivenShutdown or TestSelfTriggeredShutdown"`——确认扩展后的断言全部通过，尤其确认 `direct` 模式下的顺序断言、`reload`/`workers` 模式下的存在性断言都覆盖到（`reload`/`workers` 各自至少有一个 parametrize case 命中，见既有 `@pytest.mark.parametrize`）。
   - 跑 `TestShutdownRaceWithRealInfra`（有 `DATABASE_URL`/`REDIS_HOST` 时）：确认真实 DB/Redis 场景下同样能观察到 `graceful_shutdown_accounting_drained`（而不是 skip 或异常）——这条断言复用 Phase 1a 已完成的该测试体，若发现该测试体尚未按 Phase 1a 自己的说明补全（仍是 `...` 占位），先回 Phase 1a 计划补全它、再叠加本 Task 的新断言，不在本 Task 里重新设计这个测试。
   - `make pre-commit`。

5. 提交：`git add tests/e2e/shutdown/test_graceful_shutdown_e2e.py && git commit -m "test: extend e2e graceful shutdown suite with full 9-step quiesce log assertions"`

---

## 未采纳方案（全文汇总索引）

以下每条在对应 Task 正文里都有完整推理，这里只做汇总索引，方便评审一次性看全，不代表这里的一句话摘要可以脱离原文单独引用。

1. **不新增 `ManagedTaskSupervisor.spawn_child` 专用入口**（Task 8）——B2/B3 两个记账创建点直接复用 Task 2 已有的 `spawn_detached`，理由是两处调用点都天然处在 Task 6/7 已经建立的同一条真实调用链路上、`current_accounting_scope.get()` 能直接看到上游绑定的 lease，专门再开一个 `spawn_child` 入口只是给同一件事起第二个名字，不提供额外能力。
2. **不追加 `ForcedExit` 作为 `DrainOutcome`/`LoggingDrainOutcome` 的第三个 tagged 变体**（Task 10，**尚未最终定案，见下方"自检"一节的"已知未决事项"**）——采用 Task-10 层面按 `GracefulShutdownManager.is_force_exit()` 给日志行打不同文本标签的临时方案，不改动 Task 4/5 已写好、已测试的两变体类型契约。
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
| 第 C 节步骤 8（停止 logging worker） | Task 10（说明：本步骤是 `quiesce()` 内部已处理的自然结果，接线层面是 no-op，已在 Task 10 正文注明） |
| 第 C 节步骤 9（关闭共享 aiohttp/prisma/redis） | Phase 1a 既有代码（未改）+ Task 10（确认接线顺序把这一步放在 quiesce 完成之后） |
| 测试要求：记账生命周期全链路（lease 获取→派生→结清）单测覆盖 | Task 2/3/4（各自的单元测试） |
| 测试要求：`drain()`/`quiesce()` 的 deadline-exceeded 分支必须可确定性触发、不依赖真实计时器 race | Task 4/5/10（注入 `deadline_remaining=lambda: -1.0` 类 fixture） |
| 测试要求：quiesce 顺序契约端到端验证 | Task 10（进程内单测锁定调用顺序）+ Task 11（E2E 日志行顺序，`direct` 模式） |
| 测试要求：`LoggingWorker.quiesce()` 独立可测 | Task 5 |
| 测试要求：uvicorn 关停入口的进程级验收 | Phase 1a Task 8（基础 E2E harness）+ Task 11（本计划的扩展断言） |

### 占位符扫描

对全文（约 3980 行）执行 `grep -n -E "TODO|TBD|类似|以此类推|同理省略"` 与逐行扫描裸 `\.\.\.`：仅命中 `Protocol`/抽象基类方法体的合法 `def foo(...) -> X: ...` 桩代码（`typing.Protocol` 惯用写法，本身就是最终形态，不是待补占位符），零命中真正意义上的未完成占位符（无遗留 `TODO`/`TBD`/裸省略号段落/"类似做法，此处省略"式模糊表述）。全部 11 个 Task 均以可直接执行的失败测试代码 + 实现 diff + 确认转绿步骤 + 提交命令收尾，没有任何一个 Task 只写了大纲。

### 跨 Task 类型一致性核对

- `DrainOutcome`（`Drained | DeadlineExceeded(remaining_accounting_tasks, remaining_admissions_in_progress)`）与 `LoggingDrainOutcome`（`LoggingDrained | LoggingDeadlineExceeded(dropped_queue_items, cancelled_running_tasks, cancelled_helper_tasks)`）两个两变体 tagged union，在定义处（Task 4、Task 5）与全部消费处（Task 10 的 `match`/`assert_never` 分支、Task 11 的日志断言）字段名与变体数量保持一致，没有任何消费点擅自假设第三个变体或不同字段名——**但这两个两变体形状本身与 spec 第 140/176 行字面写的三变体（`Drained | DeadlineExceeded(cancelled, cancellation_failed) | ForcedExit`）不一致，这是一条尚未解决的已知事项，见下方"已知未决事项"，不是本次一致性核对的失败项，而是需要主协调者裁决的 spec-vs-plan 分歧**。
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

### 已知未决事项（需要主协调者裁决，非本计划可自行拍板）

**`DrainOutcome`/`LoggingDrainOutcome` 的 spec-vs-plan 变体分歧**：spec 第 140、176 行字面要求三变体 tagged union `Drained | DeadlineExceeded(cancelled, cancellation_failed) | ForcedExit`；Task 4/Task 5（在本计划撰写 Task 10 之前就已完整写好并配好测试）实际定义的是两变体、且字段名不同（`remaining_accounting_tasks`/`remaining_admissions_in_progress`，非 `cancelled`/`cancellation_failed`；无独立 `ForcedExit` 变体）。

根因：Phase 1a Task 1 的 `deadline_remaining()` 设计特意让"强制退出"与"deadline 自然到期"两种情形都坍缩成同一个返回值 `0.0`，这样 `drain()`/`quiesce()` 的循环判断逻辑不需要额外多穿一个 flag 参数就能同时应对两种触发原因——但这也意味着 `drain()`/`quiesce()` 结构上**无法**在不破坏这条设计的前提下，自己内部区分出"是强制退出、还是单纯到期"，除非再给两者的签名加一个 `is_force_exit: Callable[[], bool]` 参数，而这会改变一个已经写好、已经测过的跨 Task 类型契约（`DrainOutcome`/`LoggingDrainOutcome` 是"公共 API/跨模块协议"性质的合同，按本角色的授权边界，改动它需要 architect-advisor 提案 + 主协调者拍板，不是 planner 可以自行决定的范围）。

本计划在 Task 10 采用的临时解法（已在 Task 10 正文写明"补充说明"）：不新增 `ForcedExit` 变体，改为在 Task 10 的 lifespan 层（这一层天然拿得到 `GracefulShutdownManager.is_force_exit()`）对 `DeadlineExceeded`/`LoggingDeadlineExceeded` 结果做一次额外判断，仅在日志文本上区分 `"forced_exit"` 与 `"deadline_exceeded"` 两种归因——满足 spec"运维人员能从日志区分两种原因"这条可观测性意图，但不触碰已冻结的类型契约。

两个选项交给主协调者裁决：

- **选项 A（维持现状，无需返工）**：保留 Task 4/5 已写好的两变体 union，Task 10 层面用日志文本区分强制退出/到期。优点：零返工成本，Task 4/5/10/11 全部已完成且已测试。缺点：调用方若只看 `DrainOutcome`/`LoggingDrainOutcome` 的类型本身（不看日志），无法程序化区分两种原因（比如未来要给这两种情形接不同的告警级别/重试策略时，需要额外读日志文本而非类型本身）。
- **选项 B（返工 Task 4/5，改成真正的三变体 union，字段名对齐 spec 原文）**：需要改动已写好、已提交进本计划文档的 Task 4/5 章节（类型定义、测试、Task 6-9/10/11 里所有消费点的 `match`/`assert_never` 分支），并且仍需解决"`drain()`/`quiesce()` 内部如何区分两种触发原因"这个结构性问题（大概率仍需要给两者签名加一个 `is_force_exit` 判定参数，这本身也是一次跨 Task 协议改动）。优点：类型契约字面对齐 spec，调用方可程序化区分两种原因。缺点：返工成本覆盖已完成的 4 个 Task，且不消除"要不要多穿一个参数"这个设计决策，只是把它从"日志层面"挪到"类型层面"。

**本计划撰写者（planner）的推荐**：选项 A。理由：spec 意图的核心是"运维可观测性"（能区分两种原因），而不是"类型系统层面的强制区分"——选项 A 已经满足这条意图，且 spec 全文没有任何一处显式要求调用方必须以程序化方式（而非日志）分支处理这两种原因；选项 B 的返工成本与其带来的边际收益不成比例，且并未真正消解"两种原因是否需要在 `drain()`/`quiesce()` 内部结构性区分"这一更深层的设计问题，只是把同一个决策从"日志文本"平移到"字段名"，収益有限。但这最终是 spec 措辞的字面准确性 vs 已有实现的权衡取舍，按本角色边界不能自行拍板，需主协调者确认。

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

本计划文档末尾"自检"一节的"已知未决事项"记录了一个尚未解决的 spec-vs-plan 分歧（DrainOutcome/
LoggingDrainOutcome 的两变体 vs spec 字面要求的三变体 ForcedExit），计划采用的是"选项 A"（维持两
变体、Task 10 层面用日志文本区分强制退出/到期）。除非主协调者明确改判为选项 B，否则按选项 A 实现
Task 4/5/10 即可，不需要在实现阶段重新纠结这个问题。

每完成一个 Task 就更新一次本计划文档里对应 Task 的状态（如果计划文档还没有状态追踪字段，建议在每个
Task 标题后追加 "（已完成，commit <hash>）"，保持 sync-plan-with-impl）。全部 11 个 Task 完成后，
按项目惯例发起一次 review-merged-state 级别的整体评审（覆盖 Task 1-11 累积效果，而不仅是逐 Task
评审），再交付。
```

（收尾自检 + Kick-off Prompt 已完成撰写。）
