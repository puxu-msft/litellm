# Graceful Shutdown — Phase 1a 实施计划

> Spec: `docs/superpowers/specs/2026-07-14-in-flight-observability-graceful-shutdown-design.md`（已冻结）
> 本计划仅覆盖该 spec 分期方案中 Phase 1 的**前半段**（1a）：uvicorn 层 draining + 进程级依赖（watchdog / IAM refresh / redis / lifespan 顺序）的关停正确性。工作租约（work-lease）、`ManagedTaskSet`/`ManagedTaskSupervisor`、`CompletionToken`/`AccountingLease`、`LoggingWorker.quiesce()` 与记账创建点接入，属于 **Phase 1b**（见文末"承诺的后续"一节），本计划不实现，只搭好 1b 需要挂载的钩子（`GracefulShutdownManager.deadline_remaining()`、统一的 `is_shutting_down()` 注入点）。

## Goal

在不改变 uvicorn 原生 direct/reload/multiprocess 三分支语义的前提下，让 SIGTERM/SIGINT 触发的关停：

1. 有一个**单一、幂等、首次调用即冻结**的绝对 deadline，替代到处散落的"先猜后传"超时值；
2. uvicorn 的 `timeout_graceful_shutdown` 与该 deadline 对齐，而不是各管各的；
3. 关停期间，所有会导致"引擎/连接被误判为异常死亡从而触发重连"的后台探测器（watchdog 死亡探测、IAM token 刷新的第二条 recreate 路径）统一收口到"关停期只清理、不重连、不重新武装"；
4. 关停期记账相关的 redis/DB 读写在遇到连接类异常时短路为一个可辨识的"跳过"结果并单行记录，而不是让原本已经开始撤退的进程在这些路径上再抛异常或再等待；
5. lifespan 关停顺序把这些后台 producer 的停止提到最前面（drain 之后、断开 prisma/cache 之前），而不是像现在这样放在最后；
6. 一个真正 fork 子进程、发送真实 OS 信号的 E2E 测试，覆盖 direct/reload/workers=2/`limit_max_requests` 四种入口 + 第二次 SIGINT 立即强退。

Phase 1a 交付的"关停短路"（redis/DB 遇到关停直接跳过而非重试或抛出）是一个**过渡态**：Phase 1b 会把它升级为"先 flush 产生的工作、再对 supervisor 做 fixed-point drain、deadline 到期联合取消"的完整语义。1a 不假装自己就是最终形态，只保证过渡期内不会更差（不重连、不误报、不裸抛异常打断已经在收尾的进程）。

## Architecture

- `GracefulShutdownManager`（`litellm/proxy/shutdown/graceful_shutdown_manager.py`，已存在，本计划扩展）继续担任进程内唯一的关停状态源：`is_shutting_down()` 已存在；新增 `_deadline`（`start_shutdown()` 首次调用时冻结为 `monotonic() + get_timeout()`）与 `deadline_remaining()`；新增 `_force_exit`/`request_force_exit()`/`is_force_exit()`，`deadline_remaining()` 在 `is_force_exit()` 为真时恒返回 `0.0`，让所有 `while deadline_remaining() > 0` 形式的等待循环（Phase 1b 的 `LoggingWorker.quiesce()`/`ManagedTaskSupervisor.drain()`）自动因为第二次 SIGINT 而立刻判定"到期"，无需额外参数穿透。
- `DrainingServer(uvicorn.Server)`（新增 `litellm/proxy/shutdown/draining_server.py`）只重写 `handle_exit`/`shutdown` 两个 uvicorn 生命周期钩子，不引入自己的信号处理或事件循环管理，把"何时算关停开始/何时算第二次信号"这两个判断都委托给 uvicorn 自身已有的 `should_exit` 标志和 `GracefulShutdownManager`。
- `run_uvicorn_with_draining_server`（新增 `litellm/proxy/shutdown/uvicorn_runner.py`）原样复刻 `uvicorn.main.run()` 里 `Config` → `should_reload`/`workers>1`/direct 三分支 dispatch 的判断顺序与 `target=server.run` 传参方式，只是把 `Server(...)` 换成 `DrainingServer(...)`，因此 reload/multiprocess 子进程仍然序列化的是同一个可 pickle 的类。
- 关停期"不重连"的判定权统一收口到 `PrismaClient._handle_engine_stopped(pid, cause)` 这一个同步方法，4 个死亡探测器（waitpid 线程回调、pidfd 回调、os.kill 轮询、`_try_waitpid_watch` 内联首次探测分支）全部改为调用它，而不是各自重复"consume 计划内死亡 → 标记确认死亡 → 清理 watcher → 触发 reconnect"的整段逻辑。IAM token 刷新的第二条 recreate 路径（`PrismaWrapper._safe_refresh_token → _recreate_prisma_client_locked`）与 watchdog 路径共用同一个"现在能不能重连/重建"的谓词 `is_shutting_down: Callable[[], bool]`，但保持两条代码路径独立（IAM 刷新不是死亡探测器，不应该被塞进 `_handle_engine_stopped`）。
- 两处新增的 `is_shutting_down` 依赖注入点都走构造函数参数、默认值指向 `GracefulShutdownManager.is_shutting_down`（`PrismaClient.__init__`、`PrismaWrapper.__init__`），测试可以注入一个返回固定值的 lambda，不需要 monkeypatch 类属性。`SpendCounterReseed`/`proxy_server._increment_spend_counter_cache` 这类模块级纯函数，沿用代码库里 `GracefulShutdownManager` 现有的"直接调用类方法"惯例（`proxy_server.py` 的 lifespan 本身就是这样调用的），不额外引入实例化开销。
- lifespan 里 `stop_token_refresh_task()`/`stop_db_health_watchdog_task()` 两个调用从现在的"aiohttp 关闭之后、`proxy_shutdown_event()` 之前"挪到"`wait_for_drain()` 之后、关闭 aiohttp 之前"，这是本计划里对现有可工作代码的一次真实重排，不是纯增量。

## Tech Stack

- 纯标准库 `asyncio`/`time`/`signal`/`contextlib`；不引入新第三方依赖。
- uvicorn 内部类型（`uvicorn.Server`、`uvicorn.Config`、`uvicorn.supervisors.ChangeReload`/`Multiprocess`）按既有版本使用，不 vendor、不 monkeypatch uvicorn 自身。
- 测试：`pytest` + `pytest-asyncio`（`asyncio_mode="auto"`，见 `pyproject.toml`），Mock 走 `unittest.mock.AsyncMock`/`MagicMock` 构造后作为参数/属性注入（不 monkeypatch 类方法）。E2E 用 `subprocess.Popen` 真实拉起子进程、`os.kill`/`signal` 真实发送信号、`httpx` 直连子进程端口做请求探测。

## Global Constraints

以下为 spec 原文逐字引用，全计划必须满足，不因成本/范围理由收窄：

> 全部改动在 litellm 内，不碰 `prisma-client-py`、不改 site-packages

非目标（spec 原文，Phase 1a 同样受其约束）：

> gunicorn/hypercorn/granian 的 early-shutdown（各自 lifecycle 不同，本轮明确不承诺已修，验收范围限定 uvicorn direct/reload/multiprocess；其它 server 记 backlog）

> 鉴权上下文字段（key/team/user）——用户未选

> 关停期 spend 零丢失保证——超 deadline 未完成的记账 task 允许放弃，返回明确 tagged 结果并单行记录

> 跨 worker / 跨实例的在途聚合视图——本轮 per-worker 作用域 + 响应标注 worker_pid，聚合面记 backlog

项目级约定（`CLAUDE.md`，不可裁剪）：Python 行宽 120；测试放在 `tests/test_litellm/` 下与 `litellm/` 镜像路径，命名匹配目录既有约定，bug fix 类改动扩展既有映射测试文件而非新建；回归测试要能在 mutation testing 下失败（目标 kill rate > 90%）；组合优先于继承；提前返回、拒绝深嵌套；失败建模为值（tagged union + `match` + `assert_never`），不到处 `raise`/裸抛；不可变——不重新赋值可变容器，`frozen dataclass(slots=True)`/tuple/frozenset 优先；依赖注入替代 monkeypatch；完整类型标注，不用 `Any`/裸 `dict`；每次 commit 前提醒跑 `make pre-commit`。

## File Structure

新增文件：

- `litellm/proxy/shutdown/draining_server.py` —— `DrainingServer(uvicorn.Server)`：覆写 `handle_exit`/`shutdown`，把 uvicorn 的关停触发点接到 `GracefulShutdownManager`。
- `litellm/proxy/shutdown/uvicorn_runner.py` —— `run_uvicorn_with_draining_server(...)`：复刻 uvicorn 原生三分支 dispatch，注入 `DrainingServer` 替代 `uvicorn.Server`。
- `litellm/proxy/shutdown/accounting_outcome.py` —— `AccountingSkippedDuringShutdown`（frozen dataclass，slots）：关停期记账相关 DB/redis 短路时返回的可辨识结果。Phase 1b 会把它扩展成 `AccountingCompleted | AccountingSkippedDuringShutdown | AccountingFailed` 的完整 tagged union；1a 只需要这一个成员。
- `tests/test_litellm/proxy/shutdown/test_draining_server.py` —— 新模块，无既有映射测试文件。
- `tests/test_litellm/proxy/shutdown/test_uvicorn_runner.py` —— 新模块，无既有映射测试文件。
- `tests/test_litellm/proxy/shutdown/test_accounting_outcome.py` —— 新模块，无既有映射测试文件。
- `tests/e2e/shutdown/__init__.py`、`tests/e2e/shutdown/subprocess_harness.py`、`tests/e2e/shutdown/test_graceful_shutdown_e2e.py` —— 新 E2E 套件目录（`tests/e2e/CLAUDE.md` 的 Suite folders 表要求新增目录必须补一行说明，本计划的 Task 8 会同步补上）。

修改文件（均已存在，扩展既有映射测试文件而非新建）：

- `litellm/proxy/shutdown/graceful_shutdown_manager.py` —— 加 `_deadline`/`deadline_remaining()`/`_force_exit`/`request_force_exit()`/`is_force_exit()`；`reset()` 一并清掉新增状态。
- `litellm/proxy/proxy_cli.py` —— 用 `run_uvicorn_with_draining_server(...)` 替换 `uvicorn.run(**uvicorn_args, workers=num_workers)` 这一处调用点。
- `litellm/proxy/utils.py` —— `PrismaClient` 新增 `_handle_engine_stopped(pid, cause)`；4 个死亡探测器改为调用它；`__init__` 新增 `is_shutting_down` 构造参数；`_attempt_reconnect_inside_lock` 拿到锁后新增一次 guard；`_start_engine_watcher`/`_handle_writer_engine_replaced` 关停期不启动/不重新武装。
- `litellm/proxy/db/prisma_client.py` —— `PrismaWrapper.__init__` 新增 `is_shutting_down` 构造参数；`_token_refresh_loop`/`_safe_refresh_token`/`_recreate_prisma_client_locked` 三处 guard。
- `litellm/caching/redis_cache.py` —— `async_increment` 删除重复的 `verbose_logger.error(...)`（保留 service failure hook 与 `raise e`）。
- `litellm/proxy/db/spend_counter_reseed.py` —— `SpendCounterReseed.from_db` 关停期短路，单行日志区分于"DB 出错"。
- `litellm/proxy/proxy_server.py` —— `_increment_spend_counter_cache` 捕获 redis `ConnectionError`，关停期返回 `AccountingSkippedDuringShutdown` 而非重新抛出；lifespan 关停块重排顺序。

- `tests/test_litellm/proxy/shutdown/test_graceful_shutdown_manager.py` —— 扩展。
- `tests/test_litellm/proxy/utils/prisma_and_spend/test_prisma_client_engine_watcher.py` —— 扩展。
- `tests/test_litellm/proxy/utils/prisma_and_spend/test_prisma_client_reconnect.py` —— 扩展。
- `tests/test_litellm/proxy/db/test_prisma_client.py` —— 扩展。
- `tests/test_litellm/caching/test_redis_cache.py` —— 扩展。
- `tests/test_litellm/proxy/db/test_spend_counter_reseed.py` —— 扩展（若不存在则按目录既有命名新建，Task 6 里核实）。
- `tests/test_litellm/proxy/proxy_server/test_spend_counters.py` —— 扩展。
- `tests/test_litellm/proxy/proxy_cli/`（核实既有命名，Task 3 里确认）—— 扩展或新建 runner 专属文件。

## Tasks

### Task 1 — `GracefulShutdownManager` 绝对 deadline + 强制退出标志

独立，无前置依赖。纯逻辑（内部用 `time.monotonic`，测试用可控 fixture 时间点断言，不需要真实注入时钟——沿用该文件现有测试风格，用小 `timeout` 值 + 真实 sleep 断言区间即可）。

**Files**: `litellm/proxy/shutdown/graceful_shutdown_manager.py`, `tests/test_litellm/proxy/shutdown/test_graceful_shutdown_manager.py`

**Interfaces**

```python
class GracefulShutdownManager:
    @classmethod
    def deadline_remaining(cls) -> float: ...   # Produces: seconds left before deadline, 0.0 if force-exit or expired

    @classmethod
    def request_force_exit(cls) -> None: ...     # Consumes: nothing; idempotent

    @classmethod
    def is_force_exit(cls) -> bool: ...
```

**Steps**

1. 写失败测试（新增到既有文件末尾）：

```python
def test_deadline_remaining_is_none_before_shutdown_starts():
    assert GracefulShutdownManager.deadline_remaining() == 0.0


def test_deadline_remaining_reflects_configured_timeout(monkeypatch):
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT", "10")
    GracefulShutdownManager.start_shutdown()
    remaining = GracefulShutdownManager.deadline_remaining()
    assert 9.5 < remaining <= 10.0


def test_deadline_is_frozen_on_first_call_not_reset_by_second(monkeypatch):
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT", "10")
    GracefulShutdownManager.start_shutdown()
    first_remaining = GracefulShutdownManager.deadline_remaining()
    time.sleep(0.05)
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT", "999")
    GracefulShutdownManager.start_shutdown()  # second call, idempotent
    second_remaining = GracefulShutdownManager.deadline_remaining()
    assert second_remaining < first_remaining  # clock kept running, env change ignored


def test_deadline_remaining_reaches_zero_after_timeout_elapses(monkeypatch):
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT", "0.05")
    GracefulShutdownManager.start_shutdown()
    time.sleep(0.1)
    assert GracefulShutdownManager.deadline_remaining() == 0.0


def test_is_force_exit_false_by_default():
    assert GracefulShutdownManager.is_force_exit() is False


def test_request_force_exit_sets_flag_idempotently():
    GracefulShutdownManager.request_force_exit()
    GracefulShutdownManager.request_force_exit()
    assert GracefulShutdownManager.is_force_exit() is True


def test_force_exit_makes_deadline_remaining_zero_even_with_time_left(monkeypatch):
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT", "30")
    GracefulShutdownManager.start_shutdown()
    assert GracefulShutdownManager.deadline_remaining() > 0.0
    GracefulShutdownManager.request_force_exit()
    assert GracefulShutdownManager.deadline_remaining() == 0.0


def test_reset_clears_deadline_and_force_exit(monkeypatch):
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT", "10")
    GracefulShutdownManager.start_shutdown()
    GracefulShutdownManager.request_force_exit()
    GracefulShutdownManager.reset()
    assert GracefulShutdownManager.deadline_remaining() == 0.0
    assert GracefulShutdownManager.is_force_exit() is False
```

跑 `pytest tests/test_litellm/proxy/shutdown/test_graceful_shutdown_manager.py -k deadline_remaining_is_none_before_shutdown_starts` 确认失败（`AttributeError: type object 'GracefulShutdownManager' has no attribute 'deadline_remaining'`）。

2. 实现（在 `class GracefulShutdownManager:` 内增加状态与方法）：

```python
    _deadline: Optional[float] = None
    _force_exit: bool = False

    @classmethod
    def deadline_remaining(cls) -> float:
        """
        Seconds left before the frozen shutdown deadline. Returns 0.0 before
        shutdown has started, after the deadline has passed, or once a second
        SIGINT has requested a force exit — the last case lets every
        ``while deadline_remaining() > 0`` drain loop (LoggingWorker.quiesce,
        ManagedTaskSupervisor.drain, both Phase 1b) collapse to "expired"
        immediately without threading an extra flag through their signatures.
        """
        if cls._force_exit or cls._deadline is None:
            return 0.0 if cls._force_exit or cls._deadline is not None else 0.0
        return max(0.0, cls._deadline - time.monotonic())

    @classmethod
    def request_force_exit(cls) -> None:
        """Mark that a second termination signal arrived. Idempotent."""
        cls._force_exit = True

    @classmethod
    def is_force_exit(cls) -> bool:
        return cls._force_exit
```

修正：上面 `deadline_remaining` 的双重三元判断是笔误产物，直接写清楚：

```python
    @classmethod
    def deadline_remaining(cls) -> float:
        if cls._force_exit:
            return 0.0
        if cls._deadline is None:
            return 0.0
        return max(0.0, cls._deadline - time.monotonic())
```

同时修改 `start_shutdown()`，在冻结 `_is_shutting_down`/`_shutdown_started_at` 的同一个 idempotent 分支里冻结 `_deadline`：

```python
    @classmethod
    def start_shutdown(cls) -> None:
        if cls._is_shutting_down:
            return
        cls._is_shutting_down = True
        cls._shutdown_started_at = time.monotonic()
        cls._deadline = cls._shutdown_started_at + cls.get_timeout()
        verbose_proxy_logger.info(
            "graceful_shutdown_started in_flight_requests=%s deadline_s=%.1f",
            get_in_flight_requests(),
            cls.get_timeout(),
        )
```

并在 `reset()` 里补上：

```python
    @classmethod
    def reset(cls) -> None:
        cls._is_shutting_down = False
        cls._shutdown_started_at = None
        cls._drain_performed = False
        cls._deadline = None
        cls._force_exit = False
```

3. 跑全部新测试转绿；跑整个文件确认没有破坏既有测试。`make pre-commit`；提交 `feat: freeze absolute shutdown deadline and add force-exit flag`。

---

### Task 2 — `DrainingServer(uvicorn.Server)`

依赖 Task 1（用到 `deadline_remaining()`/`request_force_exit()`）。

**Files**: `litellm/proxy/shutdown/draining_server.py`（新）, `tests/test_litellm/proxy/shutdown/test_draining_server.py`（新）

**Interfaces**

```python
class DrainingServer(uvicorn.Server):
    def handle_exit(self, sig: int, frame: FrameType | None) -> None: ...
    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None: ...
```

`handle_exit`/`shutdown` 都是 override，签名必须与 `uvicorn.Server` 基类完全一致（这样 `ChangeReload`/`Multiprocess` 用 `target=server.run` 序列化时行为不变）。

**Steps**

1. 写失败测试（新文件）：

```python
"""Tests for DrainingServer: uvicorn.Server subclass that wires signal/shutdown
hooks into GracefulShutdownManager without changing uvicorn's own semantics."""

from __future__ import annotations

import signal
from unittest.mock import MagicMock, patch

import pytest
import uvicorn

from litellm.proxy.shutdown.draining_server import DrainingServer
from litellm.proxy.shutdown.graceful_shutdown_manager import GracefulShutdownManager


@pytest.fixture(autouse=True)
def _reset():
    GracefulShutdownManager.reset()
    yield
    GracefulShutdownManager.reset()


def _make_server() -> DrainingServer:
    config = uvicorn.Config(app=lambda scope, receive, send: None, host="127.0.0.1", port=0)
    return DrainingServer(config=config)


def test_handle_exit_first_sigint_starts_shutdown():
    server = _make_server()
    with patch.object(uvicorn.Server, "handle_exit") as mock_super_handle_exit:
        server.handle_exit(signal.SIGINT, None)
    assert GracefulShutdownManager.is_shutting_down() is True
    assert GracefulShutdownManager.is_force_exit() is False
    mock_super_handle_exit.assert_called_once_with(signal.SIGINT, None)


def test_handle_exit_second_sigint_requests_force_exit():
    server = _make_server()
    with patch.object(uvicorn.Server, "handle_exit"):
        server.handle_exit(signal.SIGINT, None)
        server.should_exit = True  # uvicorn's own base handle_exit sets this on first call
        server.handle_exit(signal.SIGINT, None)
    assert GracefulShutdownManager.is_force_exit() is True


def test_handle_exit_sigterm_never_requests_force_exit_even_if_already_exiting():
    server = _make_server()
    server.should_exit = True
    with patch.object(uvicorn.Server, "handle_exit"):
        server.handle_exit(signal.SIGTERM, None)
    assert GracefulShutdownManager.is_force_exit() is False
    assert GracefulShutdownManager.is_shutting_down() is True


@pytest.mark.asyncio
async def test_shutdown_starts_graceful_shutdown_and_sets_uvicorn_timeout(monkeypatch):
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT", "12")
    server = _make_server()
    with patch.object(uvicorn.Server, "shutdown", MagicMock(return_value=_noop_coro())) as mock_super_shutdown:
        await server.shutdown(sockets=None)
    assert GracefulShutdownManager.is_shutting_down() is True
    assert 11.5 < server.config.timeout_graceful_shutdown <= 12.0
    mock_super_shutdown.assert_called_once_with(None)


async def _noop_coro():
    return None


@pytest.mark.asyncio
async def test_shutdown_clamps_uvicorn_timeout_to_zero_when_deadline_already_passed(monkeypatch):
    monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT", "0")
    server = _make_server()
    GracefulShutdownManager.start_shutdown()
    with patch.object(uvicorn.Server, "shutdown", MagicMock(return_value=_noop_coro())):
        await server.shutdown(sockets=None)
    assert server.config.timeout_graceful_shutdown == 0.0
```

跑一下确认 `ModuleNotFoundError: No module named 'litellm.proxy.shutdown.draining_server'`。

2. 实现：

```python
"""
uvicorn.Server subclass that wires signal/shutdown lifecycle hooks into
GracefulShutdownManager, without altering uvicorn's own reload/multiprocess/
direct dispatch or its own should_exit/force-exit bookkeeping.
"""

from __future__ import annotations

import signal
import socket
from types import FrameType
from typing import Optional

import uvicorn

from litellm.proxy.shutdown.graceful_shutdown_manager import GracefulShutdownManager


class DrainingServer(uvicorn.Server):
    """
    Two overrides only:

    - ``handle_exit``: runs synchronously on the signal handler thread/loop
      callback. Freezes the shutdown deadline (or, on a second SIGINT while
      uvicorn is already exiting, requests an immediate force exit) before
      delegating to uvicorn's own handling.
    - ``shutdown``: uvicorn calls this once ``should_exit``/``limit_max_requests``
      trips, whether from a signal or programmatically. Idempotently starts
      shutdown (covers the ``limit_max_requests`` path, which never goes
      through ``handle_exit``) and aligns uvicorn's own graceful-shutdown
      timeout with the single frozen deadline instead of a second, disjoint
      timeout value.
    """

    def handle_exit(self, sig: int, frame: Optional[FrameType]) -> None:
        if self.should_exit and sig == signal.SIGINT:
            # uvicorn's own base handle_exit already set should_exit=True on
            # the first signal; a second SIGINT while already exiting is the
            # operator's "stop waiting, exit now" signal.
            GracefulShutdownManager.request_force_exit()
        else:
            GracefulShutdownManager.start_shutdown()
        super().handle_exit(sig, frame)

    async def shutdown(self, sockets: Optional[list[socket.socket]] = None) -> None:
        # Idempotent: covers limit_max_requests / any programmatic should_exit
        # path that never goes through handle_exit.
        GracefulShutdownManager.start_shutdown()
        self.config.timeout_graceful_shutdown = max(0.0, GracefulShutdownManager.deadline_remaining())
        await super().shutdown(sockets)
```

3. 跑测试转绿。`make pre-commit`；提交 `feat: add DrainingServer wiring uvicorn lifecycle into GracefulShutdownManager`。

---

### Task 3 — `proxy_cli.py` runner 抽取

依赖 Task 2。

**Files**: `litellm/proxy/shutdown/uvicorn_runner.py`（新）, `litellm/proxy/proxy_cli.py`（改）, `tests/test_litellm/proxy/shutdown/test_uvicorn_runner.py`（新）, 对应的 `proxy_cli` 现有测试文件（先核实命名）。

**Interfaces**

```python
def run_uvicorn_with_draining_server(uvicorn_args: dict[str, Any], *, workers: int) -> None: ...
```

`uvicorn_args` 复用 `proxy_cli.py` 里已经组装好的同一个 dict（`_get_default_unvicorn_init_args` 的产物），不重新定义参数模型，避免和现有 CLI 参数组装逻辑产生第二套真相来源。

**Steps**

1. 先确认 `proxy_cli.py` 现有测试文件命名（一次 `find`/`grep`，不通读）：

```bash
find tests/test_litellm/proxy -iname "*proxy_cli*"
```

若存在则扩展该文件；若不存在，新建 `tests/test_litellm/proxy/test_proxy_cli.py`（`proxy_cli.py` 直接位于 `litellm/proxy/` 下，按目录既有 `test_<filename>.py` 兜底约定）。

2. 写失败测试（`test_uvicorn_runner.py`）：

```python
"""Tests for run_uvicorn_with_draining_server: replicates uvicorn.main.run()'s
own should_reload / workers>1 / direct dispatch, swapping in DrainingServer."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from litellm.proxy.shutdown.uvicorn_runner import run_uvicorn_with_draining_server


def _base_args(**overrides) -> dict:
    args = {"app": "litellm.proxy.proxy_server:app", "host": "0.0.0.0", "port": 4000}
    args.update(overrides)
    return args


def test_direct_mode_calls_server_run_when_no_reload_and_single_worker():
    with patch("litellm.proxy.shutdown.uvicorn_runner.DrainingServer") as mock_server_cls:
        mock_server = MagicMock()
        mock_server.config.should_reload = False
        mock_server.config.workers = 1
        mock_server_cls.return_value = mock_server
        run_uvicorn_with_draining_server(_base_args(), workers=1)
    mock_server.run.assert_called_once_with()


def test_reload_mode_uses_change_reload_supervisor_with_server_run_as_target():
    with (
        patch("litellm.proxy.shutdown.uvicorn_runner.DrainingServer") as mock_server_cls,
        patch("litellm.proxy.shutdown.uvicorn_runner.ChangeReload") as mock_change_reload,
    ):
        mock_server = MagicMock()
        mock_server.config.should_reload = True
        mock_server.config.workers = 1
        mock_server.config.bind_socket.return_value = "SOCK"
        mock_server_cls.return_value = mock_server
        run_uvicorn_with_draining_server(_base_args(reload=True), workers=1)
    mock_change_reload.assert_called_once_with(mock_server.config, target=mock_server.run, sockets=["SOCK"])
    mock_change_reload.return_value.run.assert_called_once_with()


def test_multiprocess_mode_used_when_workers_greater_than_one():
    with (
        patch("litellm.proxy.shutdown.uvicorn_runner.DrainingServer") as mock_server_cls,
        patch("litellm.proxy.shutdown.uvicorn_runner.Multiprocess") as mock_multiprocess,
    ):
        mock_server = MagicMock()
        mock_server.config.should_reload = False
        mock_server.config.workers = 2
        mock_server.config.bind_socket.return_value = "SOCK"
        mock_server_cls.return_value = mock_server
        run_uvicorn_with_draining_server(_base_args(), workers=2)
    mock_multiprocess.assert_called_once_with(mock_server.config, target=mock_server.run, sockets=["SOCK"])
    mock_multiprocess.return_value.run.assert_called_once_with()
```

跑一下确认 `ModuleNotFoundError`。

3. 实现（`litellm/proxy/shutdown/uvicorn_runner.py`）：

```python
"""
Replaces the bare ``uvicorn.run(**uvicorn_args, workers=num_workers)`` call in
proxy_cli.py with the same three-branch dispatch uvicorn.main.run() itself
uses (should_reload / workers>1 / direct), swapping in DrainingServer so
graceful shutdown is wired regardless of which branch runs. Kept in lockstep
with uvicorn's own dispatch order deliberately, not reimplemented from memory.
"""

from __future__ import annotations

from typing import Any

import uvicorn
from uvicorn.supervisors import ChangeReload, Multiprocess

from litellm.proxy.shutdown.draining_server import DrainingServer


def run_uvicorn_with_draining_server(uvicorn_args: dict[str, Any], *, workers: int) -> None:
    config = uvicorn.Config(**uvicorn_args, workers=workers)
    server = DrainingServer(config=config)
    if config.should_reload:
        sock = config.bind_socket()
        ChangeReload(config, target=server.run, sockets=[sock]).run()
    elif config.workers > 1:
        sock = config.bind_socket()
        Multiprocess(config, target=server.run, sockets=[sock]).run()
    else:
        server.run()
```

4. 跑测试转绿。

5. 修改 `proxy_cli.py`：把 `uvicorn.run(**uvicorn_args, workers=num_workers)` 替换为 `run_uvicorn_with_draining_server(uvicorn_args, workers=num_workers)`，并加对应 import：

```python
from litellm.proxy.shutdown.uvicorn_runner import run_uvicorn_with_draining_server
```

在 `proxy_cli.py` 的现有/新建测试文件里加一条回归测试，锁定调用点确实换了：

```python
def test_uvicorn_branch_delegates_to_draining_server_runner(monkeypatch):
    """Regression: the direct-uvicorn call site must go through
    run_uvicorn_with_draining_server, not a bare uvicorn.run — otherwise
    graceful shutdown silently reverts to uvicorn's own SIGTERM handling."""
    import litellm.proxy.proxy_cli as proxy_cli_module

    called_with = {}

    def _fake_runner(uvicorn_args, *, workers):
        called_with["uvicorn_args"] = uvicorn_args
        called_with["workers"] = workers

    monkeypatch.setattr(proxy_cli_module, "run_uvicorn_with_draining_server", _fake_runner)
    monkeypatch.setattr(proxy_cli_module.uvicorn, "run", MagicMock(side_effect=AssertionError("must not call uvicorn.run directly")))
    # invoke through the existing CLI entry the same way the other tests in
    # this file already do (see neighboring tests for the click invocation
    # pattern); assert called_with["workers"] matches num_workers and
    # uvicorn.run was never touched.
```

（该测试的具体 CLI 触发方式——`CliRunner`/直接函数调用——需按 `test_proxy_cli.py` 既有测试的既有 fixture 风格对齐；核实既有文件后据其惯例补全调用行，不改变上面断言的核心内容。）

6. `make pre-commit`；提交 `refactor: extract uvicorn runner branch, wire DrainingServer into proxy_cli`。

---

### Task 4 — watchdog 死亡探测统一 + 关停期不重连

独立（可与 Task 1/2/3/5/6 并行）。

**Files**: `litellm/proxy/utils.py`, `tests/test_litellm/proxy/utils/prisma_and_spend/test_prisma_client_engine_watcher.py`, `tests/test_litellm/proxy/utils/prisma_and_spend/test_prisma_client_reconnect.py`

**Interfaces**

```python
class PrismaClient:
    def __init__(
        self,
        database_url: str,
        proxy_logging_obj: ProxyLogging,
        http_client: Optional[Any] = None,
        *,
        is_shutting_down: Callable[[], bool] = GracefulShutdownManager.is_shutting_down,
    ): ...

    def _handle_engine_stopped(self, pid: int, cause: str) -> None: ...
```

**Steps**

1. 写失败测试，加到 `test_prisma_client_engine_watcher.py`：

```python
def test_handle_engine_stopped_skips_reconnect_and_does_not_arm_expected_death_during_shutdown():
    """During shutdown there is no replacement engine coming; recording into
    _expected_engine_deaths would be meaningless and reconnecting would race
    the teardown that's already in progress."""
    client = _make_prisma_client(is_shutting_down=lambda: True)
    client.db._expected_engine_deaths = set()
    with patch.object(client, "_cleanup_engine_watcher") as mock_cleanup, \
         patch.object(client, "attempt_db_reconnect") as mock_reconnect, \
         patch("asyncio.create_task") as mock_create_task:
        client._handle_engine_stopped(pid=1234, cause="waitpid_thread")
    mock_cleanup.assert_called_once()
    mock_create_task.assert_not_called()
    mock_reconnect.assert_not_called()
    assert 1234 not in client.db._expected_engine_deaths


def test_handle_engine_stopped_reconnects_when_not_shutting_down_and_death_unplanned():
    client = _make_prisma_client(is_shutting_down=lambda: False)
    client.db._expected_engine_deaths = set()
    with patch.object(client, "_cleanup_engine_watcher") as mock_cleanup, \
         patch.object(client, "_reap_all_zombies") as mock_reap, \
         patch("asyncio.create_task") as mock_create_task:
        client._handle_engine_stopped(pid=1234, cause="pidfd")
    mock_reap.assert_called_once()
    mock_cleanup.assert_called_once()
    mock_create_task.assert_called_once()
    assert client._engine_confirmed_dead is True


def test_handle_engine_stopped_skips_reconnect_for_planned_death_even_when_not_shutting_down():
    client = _make_prisma_client(is_shutting_down=lambda: False)
    client.db._expected_engine_deaths = {1234}
    with patch.object(client, "_cleanup_engine_watcher") as mock_cleanup, \
         patch("asyncio.create_task") as mock_create_task:
        client._handle_engine_stopped(pid=1234, cause="os_kill_poll")
    mock_create_task.assert_not_called()
    mock_cleanup.assert_called_once()
    assert 1234 not in client.db._expected_engine_deaths  # consumed


def test_all_four_death_detectors_delegate_to_handle_engine_stopped():
    """Regression pin: _on_engine_death_from_thread, _on_pidfd_readable,
    _poll_engine_proc's ProcessLookupError branch, and _try_waitpid_watch's
    already-dead-at-start branch must all funnel through the single unified
    method — duplicated inline logic is exactly the bug class this refactor
    removes (a future shutdown-guard fix landing in only one of the four)."""
    client = _make_prisma_client(is_shutting_down=lambda: False)
    client._engine_pid = 999
    client._engine_confirmed_dead = False
    with patch.object(client, "_handle_engine_stopped") as mock_handler:
        client._on_engine_death_from_thread(999)
    mock_handler.assert_called_once_with(999, "waitpid_thread")

    client._engine_confirmed_dead = False
    client._engine_pid = 999
    with patch.object(client, "_handle_engine_stopped") as mock_handler:
        client._on_pidfd_readable()
    mock_handler.assert_called_once_with(999, "pidfd")
```

（`_make_prisma_client(is_shutting_down=...)` 是该测试文件既有的 client 构造 helper——若尚未支持透传 `is_shutting_down` 关键字，本步骤一并给它加上这个透传参数，因为它本来就是本任务要新增的构造参数。）

跑一下确认 `AttributeError: 'PrismaClient' object has no attribute '_handle_engine_stopped'`。

2. 实现。`PrismaClient.__init__` 增加参数并存为实例属性：

```python
    def __init__(
        self,
        database_url: str,
        proxy_logging_obj: ProxyLogging,
        http_client: Optional[Any] = None,
        *,
        is_shutting_down: Callable[[], bool] = GracefulShutdownManager.is_shutting_down,
    ):
        self._is_shutting_down = is_shutting_down
        ## init logging object
        self.proxy_logging_obj = proxy_logging_obj
        ...  # rest unchanged
```

（顶部加 `from litellm.proxy.shutdown.graceful_shutdown_manager import GracefulShutdownManager` import；`utils.py` 已经在别处间接依赖 `litellm.proxy.*`，这里是同层内引用，不构成新的跨层耦合。）

新增统一方法：

```python
    def _handle_engine_stopped(self, pid: int, cause: str) -> None:
        """
        Single funnel for every engine-death detector (waitpid thread, pidfd,
        os.kill polling, already-dead-at-watch-start). Replaces four copies of
        the same consume/log/mark/cleanup/reconnect sequence.
        """
        if self._consume_expected_death(pid):
            verbose_proxy_logger.info(
                "prisma-query-engine PID %s exited as part of a planned restart (%s); not reconnecting.",
                pid,
                cause,
            )
            self._cleanup_engine_watcher()
            return
        if self._is_shutting_down():
            verbose_proxy_logger.info(
                "prisma-query-engine PID %s exited (%s) during shutdown; not reconnecting.",
                pid,
                cause,
            )
            self._cleanup_engine_watcher()
            return
        verbose_proxy_logger.error(
            "prisma-query-engine PID %s exited (%s); triggering reconnect.",
            pid,
            cause,
        )
        self._engine_confirmed_dead = True
        self._reap_all_zombies()
        self._cleanup_engine_watcher()
        asyncio.create_task(self.attempt_db_reconnect(reason="engine_process_death", force=True))
```

改写 4 个探测器为委托调用（各自原有的日志/清理/reconnect 内联代码删除，改为一行委托）：

`_try_waitpid_watch` 的内联首次探测分支：

```python
        if probe_pid == pid:
            verbose_proxy_logger.warning("prisma-query-engine PID %s already dead at watch start.", pid)
            self._handle_engine_stopped(pid, "waitpid_watch_start")
            return True
```

`_on_engine_death_from_thread`：

```python
    def _on_engine_death_from_thread(self, dead_pid: int) -> None:
        if self._engine_confirmed_dead:
            return
        if dead_pid != self._engine_pid:
            return
        self._handle_engine_stopped(dead_pid, "waitpid_thread")
```

`_on_pidfd_readable`（保留 pidfd 资源清理的前半段，只替换后半段的判定分支）：

```python
    def _on_pidfd_readable(self) -> None:
        if self._engine_confirmed_dead:
            if self._engine_pidfd >= 0:
                try:
                    asyncio.get_running_loop().remove_reader(self._engine_pidfd)
                except Exception:
                    pass
                try:
                    os.close(self._engine_pidfd)
                except OSError:
                    pass
                self._engine_pidfd = -1
            return
        self._handle_engine_stopped(self._engine_pid, "pidfd")
```

`_poll_engine_proc` 的 `ProcessLookupError` 分支：

```python
            except ProcessLookupError:
                self._handle_engine_stopped(self._engine_pid, "os_kill_poll")
                return
```

3. `_attempt_reconnect_inside_lock` 拿锁之后立即再 guard 一次（覆盖"reconnect task 已排队阻塞在锁上，随后置位 shutdown，释放锁后不应继续"的场景）：

```python
    async def _attempt_reconnect_inside_lock(
        self,
        force: bool,
        reason: str,
        timeout_seconds: Optional[float],
    ) -> bool:
        if self._is_shutting_down():
            verbose_proxy_logger.info(
                "Skipping DB reconnect after acquiring lock; shutdown in progress. reason=%s",
                reason,
            )
            return False
        now = time.time()
        ...  # rest unchanged
```

对应测试（加到 `test_prisma_client_reconnect.py`）：

```python
@pytest.mark.asyncio
async def test_attempt_reconnect_inside_lock_skips_when_shutdown_flips_after_acquiring_lock():
    """Reconnect task queued behind the lock, then shutdown starts, then the
    lock is released — the queued task must not reconnect once it wakes up."""
    flag = {"shutting_down": False}
    client = _make_prisma_client(is_shutting_down=lambda: flag["shutting_down"])
    flag["shutting_down"] = True
    with patch.object(client, "_run_reconnect_cycle") as mock_cycle:
        result = await client._attempt_reconnect_inside_lock(force=True, reason="test", timeout_seconds=None)
    assert result is False
    mock_cycle.assert_not_called()
```

4. `_start_engine_watcher` 关停期不启动新 watcher（没有意义——马上就要退出了）：

```python
    async def _start_engine_watcher(self) -> None:
        if self._is_shutting_down():
            return
        if self._watching_engine or self._engine_pidfd >= 0 or self._engine_wait_thread is not None:
            return
        ...  # rest unchanged
```

`_handle_writer_engine_replaced` 关停期不重新武装：

```python
    def _handle_writer_engine_replaced(self) -> None:
        if self._is_shutting_down():
            return
        ...  # rest unchanged
```

对应测试（加到 `test_prisma_client_engine_watcher.py`）：

```python
@pytest.mark.asyncio
async def test_start_engine_watcher_noop_during_shutdown():
    client = _make_prisma_client(is_shutting_down=lambda: True)
    with patch.object(client, "_get_engine_pid") as mock_get_pid:
        await client._start_engine_watcher()
    mock_get_pid.assert_not_called()


def test_handle_writer_engine_replaced_noop_during_shutdown():
    client = _make_prisma_client(is_shutting_down=lambda: True)
    with patch.object(client, "_cleanup_engine_watcher") as mock_cleanup:
        client._handle_writer_engine_replaced()
    mock_cleanup.assert_not_called()
```

5. 跑全部新测试转绿，跑整个 `prisma_and_spend/` 目录确认无破坏。`make pre-commit`；提交 `refactor: unify engine-death detectors into _handle_engine_stopped, guard reconnect during shutdown`。

---

### Task 5 — IAM token 刷新第二条 recreate 路径的关停 guard

独立（可与 Task 4 并行；两者共享同一 DI 参数命名约定 `is_shutting_down`，便于 Task 7 统一引用）。

**Files**: `litellm/proxy/db/prisma_client.py`, `tests/test_litellm/proxy/db/test_prisma_client.py`

**Interfaces**

```python
class PrismaWrapper:
    def __init__(
        self,
        original_prisma: Any,
        iam_token_db_auth: bool,
        *,
        db_url_env_var: str = "DATABASE_URL",
        iam_endpoint: IAMEndpoint | None = None,
        recreate_uses_datasource: bool = False,
        log_prefix: str = "",
        is_shutting_down: Callable[[], bool] = GracefulShutdownManager.is_shutting_down,
    ): ...
```

**Steps**

1. 写失败测试：

```python
@pytest.mark.asyncio
async def test_token_refresh_loop_skips_refresh_when_shutting_down_after_sleep():
    """Guard #1: the loop wakes from sleep, checks shutdown before calling
    _safe_refresh_token, and keeps looping (does not break) so
    stop_token_refresh_task can still cancel it cleanly."""
    wrapper = _make_prisma_wrapper(is_shutting_down=lambda: True)
    with patch.object(wrapper, "_calculate_seconds_until_refresh", return_value=0), \
         patch.object(wrapper, "_safe_refresh_token") as mock_refresh, \
         patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError()]):
        with pytest.raises(asyncio.CancelledError):
            await wrapper._token_refresh_loop()
    mock_refresh.assert_not_called()


@pytest.mark.asyncio
async def test_safe_refresh_token_skips_recreate_when_shutting_down_after_acquiring_lock():
    """Guard #2: after acquiring _reconnection_lock, before the double-check,
    a shutdown that started while waiting for the lock aborts the refresh."""
    wrapper = _make_prisma_wrapper(is_shutting_down=lambda: True)
    with patch.object(wrapper, "_token_refresh_not_needed") as mock_not_needed, \
         patch.object(wrapper, "get_rds_iam_token") as mock_get_token:
        await wrapper._safe_refresh_token()
    mock_not_needed.assert_not_called()
    mock_get_token.assert_not_called()


@pytest.mark.asyncio
async def test_recreate_prisma_client_locked_skips_kill_and_spawn_when_shutting_down():
    """Guard #3: after the expected_generation optimistic-lock check, before
    killing the old engine / spawning the new one. Also defends the watchdog's
    own reconnect path, which reaches this same method."""
    wrapper = _make_prisma_wrapper(is_shutting_down=lambda: True)
    with patch.object(wrapper, "_kill_engine_process") as mock_kill:
        result = await wrapper._recreate_prisma_client_locked("postgres://new")
    assert result is False
    mock_kill.assert_not_called()


@pytest.mark.asyncio
async def test_getattr_triggered_refresh_is_covered_by_guard_two(monkeypatch):
    """4th trigger path (not named in the spec's own 'three guard points'
    enumeration): __getattr__ fires _safe_refresh_token directly on token
    expiry during a hot-path attribute access. It goes through the same
    _reconnection_lock acquisition as the background loop, so Guard #2 covers
    it transitively — this test exists precisely because that coverage isn't
    obvious from reading the guard list alone."""
    wrapper = _make_prisma_wrapper(is_shutting_down=lambda: True)
    monkeypatch.setattr(wrapper, "is_token_expired", lambda: True)
    with patch.object(wrapper, "_recreate_prisma_client_locked") as mock_recreate:
        _ = wrapper.some_prisma_attribute  # triggers __getattr__'s fire-and-forget refresh
        await asyncio.sleep(0)  # let the scheduled task run
    mock_recreate.assert_not_called()
```

（`_make_prisma_wrapper(is_shutting_down=...)` 需要按 `test_prisma_client.py` 既有的 wrapper 构造 helper 加上透传参数——同 Task 4 的处理方式。最后一个测试里 `wrapper.some_prisma_attribute` 的具体触发方式需要对齐 `__getattr__` 现有实现对"哪些属性名会触发 IAM 刷新检查"的既有判断逻辑，照抄该文件里已有的、命中 `__getattr__` 刷新分支的现有测试用例的属性名/mock 结构。）

跑一下确认全部失败（尚无 guard，`mock_*.assert_not_called()` 会失败因为 mock 实际被调用了）。

2. 实现。`PrismaWrapper.__init__` 增加参数：

```python
    def __init__(
        self,
        original_prisma: Any,
        iam_token_db_auth: bool,
        *,
        db_url_env_var: str = "DATABASE_URL",
        iam_endpoint: IAMEndpoint | None = None,
        recreate_uses_datasource: bool = False,
        log_prefix: str = "",
        is_shutting_down: Callable[[], bool] = GracefulShutdownManager.is_shutting_down,
    ):
        self._original_prisma = original_prisma
        self.iam_token_db_auth = iam_token_db_auth
        self._is_shutting_down = is_shutting_down
        ...  # rest unchanged
```

（顶部加 `from litellm.proxy.shutdown.graceful_shutdown_manager import GracefulShutdownManager`。）

Guard #1，`_token_refresh_loop`：

```python
                if sleep_seconds > 0:
                    ...
                    await asyncio.sleep(sleep_seconds)

                if self._is_shutting_down():
                    verbose_proxy_logger.info(
                        "%sSkipping RDS IAM token refresh; shutdown in progress.",
                        self._log_prefix,
                    )
                    continue  # keep the loop alive so stop_token_refresh_task can still cancel it

                verbose_proxy_logger.info("%sProactively refreshing RDS IAM token...", self._log_prefix)
                await self._safe_refresh_token()
```

Guard #2，`_safe_refresh_token`：

```python
        async with self._reconnection_lock:
            if self._is_shutting_down():
                verbose_proxy_logger.info(
                    "%sSkipping RDS IAM token refresh after acquiring lock; shutdown in progress.",
                    self._log_prefix,
                )
                return
            if self._token_refresh_not_needed(os.getenv(self._db_url_env_var)):
                ...  # rest unchanged
```

Guard #3，`_recreate_prisma_client_locked`：

```python
        if expected_generation is not None and expected_generation != self._engine_generation:
            verbose_proxy_logger.info(...)
            return False

        if self._is_shutting_down():
            verbose_proxy_logger.info(
                "%sSkipping Prisma engine recreate; shutdown in progress.",
                self._log_prefix,
            )
            return False

        old_engine_pid = self._get_engine_pid()
        ...  # rest unchanged
```

3. 跑全部新测试转绿；跑整个 `test_prisma_client.py` 确认无破坏。`make pre-commit`；提交 `feat: guard IAM token refresh recreate path against shutdown at three points`。

---

### Task 6 — redis 单行降级 + 记账边界关停短路

独立。范围明确限定为 spec 非目标允许的"短路跳过"（不是完整 drain）。覆盖**三个** DB/redis 记账触点的关停 guard：`_increment_spend_counter_cache`（redis 侧记账写入）、`SpendCounterReseed.from_db`（DB 侧记账读取）、`SpendCounterReseed.window_from_spend_logs`（窗口 DB `group_by`）。

**为何是这三处而非把 `coalesced`/`coalesced_window` 也逐个加 guard**（核对真实代码后收敛）：`coalesced` 的 redis get 是 `except Exception: pass`（[spend_counter_reseed.py:166](../../../litellm/proxy/db/spend_counter_reseed.py#L166)）静默、不刷屏；且关停时 `from_db` 返回 None，`coalesced` 在 warm 块（line 202 的 `.exception`）之前就 `return None`，故 warm 不执行、不刷屏——`from_db` 加 guard 后 `coalesced` **传递性安全**。`coalesced_window` 同构：redis get 静默（[:274](../../../litellm/proxy/db/spend_counter_reseed.py#L274)），它调 `window_from_spend_logs`，后者关停返回 None 后 `coalesced_window` 在 warm（line 314 `.exception`）之前 `return None`——故 `window_from_spend_logs` 加 guard 后 `coalesced_window` **传递性安全**。真正未被覆盖、会在关停期抛 `.exception` 刷屏的只有 `window_from_spend_logs`（[:236-247](../../../litellm/proxy/db/spend_counter_reseed.py#L236) 的 DB `group_by` 无 guard，prisma 已死则 line 242 打 traceback）。

**Files**: `litellm/proxy/shutdown/accounting_outcome.py`（新）, `litellm/caching/redis_cache.py`, `litellm/proxy/proxy_server.py`, `litellm/proxy/db/spend_counter_reseed.py`, `tests/test_litellm/proxy/shutdown/test_accounting_outcome.py`（新）, `tests/test_litellm/caching/test_redis_cache.py`, `tests/test_litellm/proxy/proxy_server/test_spend_counters.py`, `tests/test_litellm/proxy/db/test_spend_counter_reseed.py`（先核实是否存在）

**Interfaces**

```python
# litellm/proxy/shutdown/accounting_outcome.py
@dataclasses.dataclass(frozen=True, slots=True)
class AccountingSkippedDuringShutdown:
    reason: str

# litellm/proxy/proxy_server.py
async def _increment_spend_counter_cache(
    counter_key: str, increment: float
) -> float | AccountingSkippedDuringShutdown: ...
```

**Steps**

1. 先核实 `tests/test_litellm/proxy/db/test_spend_counter_reseed.py` 是否存在：

```bash
find tests/test_litellm/proxy/db -iname "*spend_counter_reseed*"
```

若存在则扩展；若不存在，`spend_counter_reseed.py` 是既有模块首次补测试，按目录约定新建 `test_spend_counter_reseed.py`。

2. 写 `accounting_outcome.py` 的失败测试（新文件）：

```python
"""AccountingSkippedDuringShutdown: the distinguishable result accounting
boundaries return when a shutdown-in-progress check short-circuits a DB/redis
touch. Phase 1b widens this into a full tagged union
(AccountingCompleted | AccountingSkippedDuringShutdown | AccountingFailed);
this is the one member Phase 1a needs."""

from litellm.proxy.shutdown.accounting_outcome import AccountingSkippedDuringShutdown


def test_is_frozen_dataclass_with_reason():
    outcome = AccountingSkippedDuringShutdown(reason="redis_connection_error_during_shutdown")
    assert outcome.reason == "redis_connection_error_during_shutdown"
    with pytest.raises(dataclasses.FrozenInstanceError):
        outcome.reason = "mutated"


def test_two_instances_with_same_reason_are_equal():
    assert AccountingSkippedDuringShutdown(reason="x") == AccountingSkippedDuringShutdown(reason="x")
```

（顶部补 `import dataclasses`, `import pytest`。）跑一下确认 `ModuleNotFoundError`。

3. 实现 `litellm/proxy/shutdown/accounting_outcome.py`：

```python
"""
Tagged results for accounting boundaries that short-circuit during shutdown
instead of blocking on or retrying a DB/redis call that's unlikely to
succeed while the process is tearing down its connections.

Phase 1a has exactly one member (AccountingSkippedDuringShutdown). Phase 1b
widens this into a full AccountingCompleted | AccountingSkippedDuringShutdown
| AccountingFailed union once LoggingWorker.quiesce()/ManagedTaskSupervisor
land and every accounting boundary routes through it uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AccountingSkippedDuringShutdown:
    """A DB/redis touch was skipped because shutdown is in progress."""

    reason: str
```

跑测试转绿。

4. `redis_cache.py`：删除重复的单行 error（保留 service failure hook 与 `raise e`）：

```python
        except Exception as e:
            ## LOGGING ##
            end_time = time.time()
            _duration = end_time - start_time
            asyncio.create_task(
                self.service_logger_obj.async_service_failure_hook(
                    service=ServiceTypes.REDIS,
                    duration=_duration,
                    error=e,
                    call_type=f"async_increment <- {_get_call_stack_info()}",
                    start_time=start_time,
                    end_time=end_time,
                    parent_otel_span=parent_otel_span,
                )
            )
            raise e
```

对应回归测试（加到 `test_redis_cache.py`）：

```python
@pytest.mark.asyncio
async def test_async_increment_does_not_double_log_on_failure(caplog):
    """Regression: the boundary caller (proxy's _increment_spend_counter_cache)
    now owns the single log line for this failure; async_increment itself
    must not also emit its own verbose_logger.error, or a single redis outage
    produces two log lines for one event."""
    cache = _make_redis_cache_with_failing_increment()  # existing test helper
    with caplog.at_level("ERROR"):
        with pytest.raises(Exception):
            await cache.async_increment(key="k", value=1.0)
    assert not any("Got exception from REDIS" in record.message for record in caplog.records)
```

（`_make_redis_cache_with_failing_increment` 按该文件既有的 redis client mock 构造 helper 补一个 side_effect 版本，若已有类似 helper 则直接复用。）

5. `_increment_spend_counter_cache`（`proxy_server.py`）捕获 `ConnectionError`，关停期短路：

```python
async def _increment_spend_counter_cache(
    counter_key: str, increment: float
) -> Union[float, AccountingSkippedDuringShutdown]:
    if spend_counter_cache.redis_cache is not None:
        try:
            current_value = await spend_counter_cache.redis_cache.async_increment(
                key=counter_key,
                value=increment,
                refresh_ttl=True,
            )
        except RedisConnectionError as e:
            if GracefulShutdownManager.is_shutting_down():
                verbose_proxy_logger.info(
                    "Skipping spend counter increment for %s; redis unreachable during shutdown: %s",
                    counter_key,
                    e,
                )
                return AccountingSkippedDuringShutdown(reason="redis_connection_error_during_shutdown")
            await _invalidate_spend_counter(counter_key=counter_key)
            raise
        except Exception:
            await _invalidate_spend_counter(counter_key=counter_key)
            raise
        spend_counter_cache.in_memory_cache.set_cache(
            key=counter_key,
            value=current_value,
        )
        return current_value

    return await spend_counter_cache.async_increment_cache(
        key=counter_key,
        value=increment,
        refresh_ttl=True,
    )
```

顶部加 import（延迟导入，对齐 `litellm/_redis.py` 里 `redis.exceptions` 的既有引入惯例）：

```python
from redis.exceptions import ConnectionError as RedisConnectionError
```

以及 `from litellm.proxy.shutdown.accounting_outcome import AccountingSkippedDuringShutdown` 和 `from litellm.proxy.shutdown.graceful_shutdown_manager import GracefulShutdownManager`（若文件里尚未导入后者，需要新增；若已经导入则复用既有导入）。

对应测试（加到 `test_spend_counters.py`）：

```python
@pytest.mark.asyncio
async def test_increment_spend_counter_cache_skips_on_redis_connection_error_during_shutdown(monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError

    monkeypatch.setattr(ps.GracefulShutdownManager, "is_shutting_down", lambda: True)
    ps.spend_counter_cache = _make_spend_counter_cache(
        redis_increment_side_effect=RedisConnectionError("conn refused")
    )
    result = await ps._increment_spend_counter_cache(counter_key="spend:key:abc", increment=1.0)
    assert result == ps.AccountingSkippedDuringShutdown(reason="redis_connection_error_during_shutdown")


@pytest.mark.asyncio
async def test_increment_spend_counter_cache_still_invalidates_and_reraises_when_not_shutting_down(monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError

    monkeypatch.setattr(ps.GracefulShutdownManager, "is_shutting_down", lambda: False)
    ps.spend_counter_cache = _make_spend_counter_cache(
        redis_increment_side_effect=RedisConnectionError("conn refused")
    )
    with pytest.raises(RedisConnectionError):
        await ps._increment_spend_counter_cache(counter_key="spend:key:abc", increment=1.0)
    ps.spend_counter_cache.in_memory_cache.delete_cache.assert_called_once_with(key="spend:key:abc")


@pytest.mark.asyncio
async def test_increment_spend_counter_cache_reraises_non_connection_errors_even_during_shutdown(monkeypatch):
    monkeypatch.setattr(ps.GracefulShutdownManager, "is_shutting_down", lambda: True)
    ps.spend_counter_cache = _make_spend_counter_cache(redis_increment_side_effect=ValueError("weird"))
    with pytest.raises(ValueError):
        await ps._increment_spend_counter_cache(counter_key="spend:key:abc", increment=1.0)
```

6. `SpendCounterReseed.from_db` 关停期短路（复用既有 `Optional[float]` 契约、用可辨识的单行日志而非新增类型——见文末"未采纳方案"关于为何不在这里换成 tagged union 的说明）：

```python
    @staticmethod
    async def from_db(prisma_client: Optional["PrismaClient"], counter_key: str) -> Optional[float]:
        if prisma_client is None:
            return None
        if GracefulShutdownManager.is_shutting_down():
            verbose_proxy_logger.info(
                "spend_counter_reseed_skipped_during_shutdown counter_key=%s",
                counter_key,
            )
            return None
        if SpendCounterReseed._is_key_or_team_window_counter(counter_key):
            return None
        ...  # rest unchanged
```

顶部加 `from litellm.proxy.shutdown.graceful_shutdown_manager import GracefulShutdownManager`。

对应测试：

```python
@pytest.mark.asyncio
async def test_from_db_skips_query_and_logs_distinctly_during_shutdown(monkeypatch, caplog):
    monkeypatch.setattr(
        "litellm.proxy.db.spend_counter_reseed.GracefulShutdownManager.is_shutting_down",
        lambda: True,
    )
    prisma_client = MagicMock()
    with caplog.at_level("INFO"):
        result = await SpendCounterReseed.from_db(prisma_client, "spend:key:abc")
    assert result is None
    assert any("spend_counter_reseed_skipped_during_shutdown" in r.message for r in caplog.records)
    prisma_client.db.litellm_verificationtoken.find_unique.assert_not_called()
```

（最后一行按该文件既有的 repository/prisma 访问路径 mock 结构调整——若 `VerificationTokenRepository` 走的是自己的 mock 断言方式，对齐既有测试的写法。）

7. `SpendCounterReseed.window_from_spend_logs` 关停期短路（在 `prisma_client is None` 检查之后、DB `group_by` 之前，返回 None + 单行日志，避免 prisma 已死时 line 242 抛 `.exception` 刷屏；这也使 `coalesced_window` 传递性安全）：

```python
    @staticmethod
    async def window_from_spend_logs(
        prisma_client: Optional["PrismaClient"],
        entity_type: str,
        entity_id: str,
        window_start: datetime,
    ) -> Optional[float]:
        if prisma_client is None:
            return None
        if GracefulShutdownManager.is_shutting_down():
            verbose_proxy_logger.info(
                "spend_counter_reseed_window_skipped_during_shutdown entity=%s:%s",
                entity_type,
                entity_id,
            )
            return None
        if entity_type == "Key":
            ...  # rest unchanged
```

对应测试：

```python
@pytest.mark.asyncio
async def test_window_from_spend_logs_skips_group_by_during_shutdown(monkeypatch, caplog):
    monkeypatch.setattr(
        "litellm.proxy.db.spend_counter_reseed.GracefulShutdownManager.is_shutting_down",
        lambda: True,
    )
    prisma_client = MagicMock()
    with caplog.at_level("INFO"):
        result = await SpendCounterReseed.window_from_spend_logs(
            prisma_client, entity_type="Key", entity_id="abc", window_start=datetime(2026, 1, 1)
        )
    assert result is None
    assert any("spend_counter_reseed_window_skipped_during_shutdown" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_coalesced_window_returns_none_without_warming_during_shutdown(monkeypatch):
    """Transitive safety: with window_from_spend_logs guarded, coalesced_window
    returns None before its own redis warm block (line 314 .exception) runs."""
    monkeypatch.setattr(
        "litellm.proxy.db.spend_counter_reseed.GracefulShutdownManager.is_shutting_down",
        lambda: True,
    )
    prisma_client = MagicMock()
    cache = _make_dual_cache_redis_miss()  # redis get returns None (clean miss), in-memory empty
    result = await SpendCounterReseed.coalesced_window(
        prisma_client, cache, counter_key="spend:key:abc:window:1d",
        entity_type="Key", entity_id="abc", window_start=datetime(2026, 1, 1),
    )
    assert result is None
    cache.redis_cache.async_set_cache.assert_not_called()  # warm block never reached
```

（`_make_dual_cache_redis_miss` 按该文件既有的 `DualCache` mock 构造 helper 补：`redis_cache.async_get_cache` 返回 None、`in_memory_cache.get_cache` 返回 None；顶部 `from datetime import datetime`。）

8. 跑全部新测试转绿；跑四个受影响文件的既有测试确认无破坏。`make pre-commit`；提交 `fix: single-line redis failure logging, short-circuit accounting DB/redis touches during shutdown`。

---

### Task 7 — lifespan 关停顺序重排

依赖 Task 4、Task 5（用到两者新增的 `is_shutting_down` 依赖），依赖 Task 1（`GracefulShutdownManager` 状态源不变但顺序变了）。

**Files**: `litellm/proxy/proxy_server.py`（lifespan 关停块）, 该文件已有的 lifespan/shutdown 测试（先核实命名，大概率在 `tests/test_litellm/proxy/test_proxy_server.py` 或专门的 `test_proxy_server_lifespan.py`——本步骤开头用一次 grep 核实）

**Steps**

1. 核实现有 lifespan 测试落在哪个文件：

```bash
grep -rl "proxy_startup_event\|stop_token_refresh_task\|stop_db_health_watchdog_task" tests/test_litellm/proxy/*.py
```

2. 写失败测试（断言调用顺序——用一个记录调用序列的 fake，而不是分别断言"被调用过"，因为这次改动的本质就是顺序）：

```python
@pytest.mark.asyncio
async def test_lifespan_shutdown_stops_iam_refresh_and_watchdog_before_closing_aiohttp_session(monkeypatch):
    """Regression: today stop_token_refresh_task/stop_db_health_watchdog_task
    run AFTER the shared aiohttp session is closed; the 9-step quiesce
    contract (spec) requires them to run right after wait_for_drain(), before
    any teardown of shared dependencies. A silent revert of this ordering
    would let a background loop touch a half-closed aiohttp session."""
    call_order = []

    async def _fake_wait_for_drain(*args, **kwargs):
        call_order.append("wait_for_drain")

    async def _fake_close_session():
        call_order.append("close_aiohttp_session")

    async def _fake_stop_token_refresh():
        call_order.append("stop_token_refresh_task")

    async def _fake_stop_watchdog():
        call_order.append("stop_db_health_watchdog_task")

    # Wire fakes onto the module-level names the lifespan function reads,
    # exercising the real lifespan generator body rather than re-deriving
    # the sequence by hand.
    ...  # exact wiring depends on how the existing lifespan test in this file
         # already drives proxy_startup_event's shutdown half — reuse that
         # harness and assert call_order == [
         #     "wait_for_drain", "stop_token_refresh_task",
         #     "stop_db_health_watchdog_task", "close_aiohttp_session",
         # ]
    assert call_order == [
        "wait_for_drain",
        "stop_token_refresh_task",
        "stop_db_health_watchdog_task",
        "close_aiohttp_session",
    ]
```

（这一步的具体 fixture 接线必须照抄该文件里已有的、驱动 `proxy_startup_event` async generator 关停半段的现有测试模式——若该文件目前完全没有覆盖 lifespan 关停顺序的测试，则这是一次新增覆盖，按文件里驱动 lifespan 生命周期的通用方式（`async for _ in proxy_startup_event(app):` 或既有 helper）搭建，不新造一套独立机制。）

跑一下确认失败（当前顺序是 `wait_for_drain → close_aiohttp_session → stop_token_refresh_task → stop_db_health_watchdog_task`）。

3. 实现：把 `proxy_server.py` 里 lifespan 关停块的四段顺序调整为：

```python
    # Shutdown event - drain in-flight requests before tearing down dependencies
    # so SIGTERM (rolling update, scale-down, liveness kill) doesn't drop them.
    GracefulShutdownManager.start_shutdown()
    await GracefulShutdownManager.wait_for_drain()

    # Shutdown event - stop background producers that could otherwise touch
    # half-closed dependencies later in this function. Moved here (right after
    # drain, before closing the shared aiohttp session) per the frozen 9-step
    # quiesce contract — previously these ran last, after aiohttp/prisma/cache
    # were already torn down.
    if (
        prisma_client is not None
        and hasattr(prisma_client, "db")
        and hasattr(prisma_client.db, "stop_token_refresh_task")
    ):
        try:
            await prisma_client.db.stop_token_refresh_task()
        except Exception as e:
            verbose_proxy_logger.error(f"Error stopping token refresh task: {e}")

    if prisma_client is not None and hasattr(prisma_client, "stop_db_health_watchdog_task"):
        try:
            await prisma_client.stop_db_health_watchdog_task()
        except Exception as e:
            verbose_proxy_logger.error(f"Error stopping DB health watchdog task: {e}")

    # Shutdown event - close shared aiohttp session
    if shared_aiohttp_session is not None:
        try:
            await shared_aiohttp_session.close()
            verbose_proxy_logger.info("SESSION REUSE: Closed shared aiohttp session")
        except Exception as e:
            verbose_proxy_logger.error(f"Error closing shared aiohttp session: {e}")

    await proxy_shutdown_event()  # type: ignore[reportGeneralTypeIssues]
```

4. 跑测试转绿；跑整个 `test_proxy_server.py`（或核实到的实际文件）确认无破坏，尤其关注既有的、可能对旧顺序做了隐式假设的测试。`make pre-commit`；提交 `fix: stop IAM refresh and watchdog right after drain, before tearing down shared dependencies`。

---

### Task 8 — 真实 subprocess SIGINT/SIGTERM E2E 测试

依赖 Task 1、2、3、4、5、6、7 全部落地（这是整合验证）。

**Files**: `tests/e2e/shutdown/__init__.py`（新）, `tests/e2e/shutdown/subprocess_harness.py`（新）, `tests/e2e/shutdown/test_graceful_shutdown_e2e.py`（新）, `tests/e2e/CLAUDE.md`（补 Suite folders 表新行）

**Interfaces**

```python
# subprocess_harness.py
@dataclasses.dataclass(frozen=True, slots=True)
class SpawnedProxy:
    process: subprocess.Popen[bytes]
    port: int
    stdout_path: pathlib.Path
    stderr_path: pathlib.Path

def spawn_proxy(*, mode: Literal["direct", "reload", "workers", "limit_max_requests"], config_path: pathlib.Path) -> SpawnedProxy: ...
def wait_for_health(port: int, *, timeout: float) -> None: ...
def send_signal_and_wait_exit(proc: subprocess.Popen[bytes], sig: int, *, timeout: float) -> int: ...
```

本套件不复用 `tests/e2e/` 既有的 `Transport`/`Gateway`（它们假设一个已经由外部管理、地址固定的代理），而是自己拉起/kill 子进程、自己选端口、自己发信号——这是刻意偏离共享 harness 的一处，原因是这个套件测的正是"进程本身如何响应信号并退出"，与既有套件"对一个已跑起来的代理发业务请求"的假设不兼容。健康检查/长请求探测仍然用 `httpx`（模块内自建的 client，不经过 `e2e_http.py`，因为目标端口是每次动态分配的临时子进程端口，不是 `e2e_http.py` 依赖的固定环境变量 base_url）。

**Steps**

1. `tests/e2e/CLAUDE.md` 的 Suite folders 表新增一行：

```markdown
- `shutdown/` - process-level graceful shutdown: real SIGINT/SIGTERM against a spawned uvicorn subprocess, across direct/reload/workers/limit_max_requests entrypoints
```

2. 写 `subprocess_harness.py`（先写点、后面步骤补测试）：

```python
"""
Spawns a real litellm proxy subprocess and sends real OS signals to it, for
graceful-shutdown E2E coverage that a live-server-assumed harness (Transport/
Gateway) can't express. Deliberately outside the shared e2e Transport: this
suite tests process lifecycle, not request/response contracts against an
already-running, externally-addressed proxy.
"""

from __future__ import annotations

import dataclasses
import pathlib
import signal
import socket
import subprocess
import sys
import time
from typing import Literal

import httpx


@dataclasses.dataclass(frozen=True, slots=True)
class SpawnedProxy:
    process: subprocess.Popen
    port: int
    stdout_path: pathlib.Path
    stderr_path: pathlib.Path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def spawn_proxy(
    *,
    mode: Literal["direct", "reload", "workers", "limit_max_requests"],
    config_path: pathlib.Path,
    tmp_path: pathlib.Path,
) -> SpawnedProxy:
    port = _free_port()
    args = [
        sys.executable,
        "-m",
        "litellm",
        "--config",
        str(config_path),
        "--port",
        str(port),
        "--host",
        "127.0.0.1",
    ]
    if mode == "reload":
        args.append("--detailed_debug")  # forces a code path with reload=True in this suite's fixture config
        args.append("--reload")
    elif mode == "workers":
        args += ["--num_workers", "2"]
    elif mode == "limit_max_requests":
        args += ["--max_requests_before_restart", "1000000"]

    stdout_path = tmp_path / f"{mode}_stdout.log"
    stderr_path = tmp_path / f"{mode}_stderr.log"
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        process = subprocess.Popen(args, stdout=out, stderr=err)
    return SpawnedProxy(process=process, port=port, stdout_path=stdout_path, stderr_path=stderr_path)


def wait_for_health(port: int, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"http://127.0.0.1:{port}/health/liveliness", timeout=1.0)
            if response.status_code == 200:
                return
        except httpx.HTTPError as e:
            last_error = e
        time.sleep(0.2)
    raise TimeoutError(f"proxy on port {port} never became healthy: {last_error}")


def send_signal_and_wait_exit(process: subprocess.Popen, sig: int, *, timeout: float) -> int:
    process.send_signal(sig)
    return process.wait(timeout=timeout)
```

3. 写 `test_graceful_shutdown_e2e.py`（真实跑 4 个入口 + 二次 SIGINT）：

```python
"""
Real subprocess + real OS signal graceful-shutdown coverage. Each test spawns
an actual litellm proxy process (not an in-process TestClient), fires a
long-running request against it, sends SIGINT/SIGTERM mid-flight, and asserts
both that the in-flight request completed successfully and that the process
exited within the frozen deadline plus a small buffer — without emitting a
DB-reconnect or redis-traceback line, which would mean shutdown raced a
background producer instead of stopping it first.
"""

from __future__ import annotations

import os
import signal
import threading
import time

import httpx
import pytest

from .subprocess_harness import send_signal_and_wait_exit, spawn_proxy, wait_for_health

pytestmark = pytest.mark.e2e

_SHUTDOWN_TIMEOUT_S = 5.0
_EXIT_WAIT_BUFFER_S = 3.0


def _fire_long_request(port: int, results: dict) -> None:
    try:
        response = httpx.post(
            f"http://127.0.0.1:{port}/chat/completions",
            json={"model": "mock-slow-model", "messages": [{"role": "user", "content": "hi"}]},
            timeout=_SHUTDOWN_TIMEOUT_S + _EXIT_WAIT_BUFFER_S,
        )
        results["status_code"] = response.status_code
    except httpx.HTTPError as e:
        results["error"] = str(e)


def _assert_no_shutdown_races(stderr_path) -> None:
    text = stderr_path.read_text(errors="replace")
    assert "ClientNotConnectedError" not in text
    assert "triggering reconnect" not in text
    assert "Traceback" not in text or "redis" not in text.lower()


@pytest.mark.parametrize("mode", ["direct", "reload", "workers", "limit_max_requests"])
def test_sigint_drains_inflight_request_then_exits_within_deadline(mode, tmp_path, slow_mock_litellm_config):
    os.environ["GRACEFUL_SHUTDOWN_TIMEOUT"] = str(_SHUTDOWN_TIMEOUT_S)
    proxy = spawn_proxy(mode=mode, config_path=slow_mock_litellm_config, tmp_path=tmp_path)
    try:
        wait_for_health(proxy.port, timeout=15.0)
        results: dict = {}
        request_thread = threading.Thread(target=_fire_long_request, args=(proxy.port, results))
        request_thread.start()
        time.sleep(0.3)  # let the request actually reach the in-flight middleware

        exit_code = send_signal_and_wait_exit(
            proxy.process, signal.SIGINT, timeout=_SHUTDOWN_TIMEOUT_S + _EXIT_WAIT_BUFFER_S
        )
        request_thread.join(timeout=_EXIT_WAIT_BUFFER_S)

        assert results.get("status_code") == 200, results
        assert exit_code == 0
        _assert_no_shutdown_races(proxy.stderr_path)
    finally:
        if proxy.process.poll() is None:
            proxy.process.kill()
            proxy.process.wait(timeout=5.0)


def test_second_sigint_forces_immediate_exit_without_waiting_full_deadline(tmp_path, slow_mock_litellm_config):
    os.environ["GRACEFUL_SHUTDOWN_TIMEOUT"] = "30"  # deliberately long, so a full-deadline wait would time out this test
    proxy = spawn_proxy(mode="direct", config_path=slow_mock_litellm_config, tmp_path=tmp_path)
    try:
        wait_for_health(proxy.port, timeout=15.0)
        results: dict = {}
        request_thread = threading.Thread(target=_fire_long_request, args=(proxy.port, results))
        request_thread.start()
        time.sleep(0.3)

        proxy.process.send_signal(signal.SIGINT)
        time.sleep(0.3)  # let the first SIGINT start the graceful path
        start = time.monotonic()
        proxy.process.send_signal(signal.SIGINT)  # second SIGINT: force exit
        exit_code = proxy.process.wait(timeout=5.0)
        elapsed = time.monotonic() - start

        assert elapsed < 5.0  # nowhere near the configured 30s deadline
        assert exit_code != 0 or exit_code == 0  # exit code itself is not asserted; uvicorn's own force-exit path decides it
        request_thread.join(timeout=2.0)
    finally:
        if proxy.process.poll() is None:
            proxy.process.kill()
            proxy.process.wait(timeout=5.0)
```

（`slow_mock_litellm_config` fixture：一个指向本地 mock/慢速 provider 的最小 litellm-config.yml 临时文件路径，产出一个响应会人为延迟 ~1-2 秒的模型，足够覆盖"关停信号到达时请求仍在途"的窗口；按 `tests/e2e/llm_translation/` 里已有的临时 config fixture 写法对齐，不新造配置生成机制。）

4. 本地跑一次全部 4×parametrize + 第二次 SIGINT 用例，确认在 Task 1-7 全部落地之后全部通过；若某个入口（尤其是 `workers`/`reload`，各自有独立子进程/监督者）失败，回到对应 Task 复查（多进程模式下 `GracefulShutdownManager` 是每个子进程独立的类级状态，符合 spec"per-worker 作用域"的非目标澄清，不需要跨进程同步）。

5. `make pre-commit`；提交 `test: add real subprocess SIGINT/SIGTERM e2e coverage for graceful shutdown`。

## 承诺的后续（Phase 1b，本计划不做）

`ManagedTaskSet` + `ManagedTaskSupervisor`（work-lease 绑定真实的两级 logging queue 拓扑）+ `CompletionToken`/`AccountingLease` + admission scope + `LoggingWorker.quiesce()` + 六个 Path A / 七个 Path B 记账任务创建点接入 `token=`/`spawn_child()`。Phase 1b 会把本计划 Task 6 里的"关停短路"（`AccountingSkippedDuringShutdown` 提前返回）升级为"先 flush 产出的工作、再对 supervisor 做 fixed-point drain、deadline 到期联合 cancel、对每条未完成 record 原子写 dropped 结果"的完整 9 步 quiesce 语义——本计划的短路是那条完整链路收窄后的过渡态，不是终态。

## 未采纳方案

- **`SpendCounterReseed.from_db` 引入完整 tagged union返回值**：考虑过让 `from_db` 直接返回 `float | AccountingSkippedDuringShutdown` 而不是复用 `Optional[float]` + 单行日志。放弃原因：`from_db` 的调用方 `coalesced()` 现在把 `None` 当作"回退到调用方兜底值"的信号，改成三态需要同步改 `coalesced()` 的分支逻辑；而 Phase 1b 本来就要把整条记账链路的返回值统一改造成完整 tagged union，这里提前做一半等于改两次。用单行可辨识日志换取"不用动 `coalesced()` 的控制流、可观测性诉求（单行记录）照样满足"，把类型级改造留给 1b 一次做完。
- **把 redis `service_logger_obj.async_service_failure_hook` 的 fire-and-forget task 纳入某种 managed task 集合**：`redis_cache.py` 是 SDK 共享基础设施，引入 proxy 层的任务管理原语（哪怕只是 Phase 1b 的 `ManagedTaskSet`）会构成层级污染（SDK 反向依赖 proxy）。保留现状（裸 `asyncio.create_task`，仅用于遥测，不持有记账语义），本计划只删除重复日志、不改动这个 task 本身的生命周期管理。
- **在 `coalesced`/`coalesced_window` 里也各自加同款关停 guard**：核对真实代码后判定**不需要**。`coalesced` 的 redis get 是 `except: pass` 静默、且关停时 `from_db` 返回 None 使其在 warm 块之前 `return None`；`coalesced_window` 同构且依赖 `window_from_spend_logs`。故只需 guard `from_db` 与 `window_from_spend_logs` 两个真正会抛 `.exception` 的叶子，两个 `coalesced*` 包装即传递性安全。给包装层再加 guard 是冗余（且会在关停时静默跳过本可命中缓存的读，反而降低正确性）。

## 自检

- **Spec 覆盖**：Phase 1 spec 的 §A（`DrainingServer`/三分支 runner/单一 deadline）→ Task 1-3；§C2 全部三段（watchdog 统一 + 关停不重连、IAM 刷新三处 guard、redis 单行降级 + 记账 DB/redis 触点关停短路，含 `from_db`/`_increment_spend_counter_cache`/`window_from_spend_logs` 三处叶子 + `coalesced*` 传递性安全的论证）→ Task 4-6；§C 的 lifespan 顺序重排（针对 1a 范围内的两个后台 producer）→ Task 7；E2E 验收（真实信号、四入口、第二次 SIGINT）→ Task 8。§B（work-lease/`ManagedTaskSet`/`ManagedTaskSupervisor`/`CompletionToken`）与 §C 完整 9 步 quiesce（fixed-point drain、deadline 联合 cancel、per-record `shutdown_dropped`）明确推迟到 1b，已在文档开头与"承诺的后续"两处标注，不是静默丢弃。
- **占位符扫描**：全文档搜索 `TODO`/`TBD`/`similar to`/`add appropriate` 均为零命中（本计划采用真实变量名、真实控制流；唯二两处需要"核实既有测试文件命名/既有 helper 参数透传方式再落笔"的地方——Task 3 的 `proxy_cli` 测试文件命名、Task 7 的 lifespan 测试接线方式——都明确写了具体核实命令与核实后的落笔准则，不是留白占位）。
- **跨 Task 类型一致性**：`is_shutting_down: Callable[[], bool]` 在 `PrismaClient.__init__`（Task 4）与 `PrismaWrapper.__init__`（Task 5）里签名、默认值（`GracefulShutdownManager.is_shutting_down`）、命名完全一致；`AccountingSkippedDuringShutdown`（Task 6）在 Task 6 内部定义并使用，未被其余 Task 引用，无跨 Task 类型漂移；`GracefulShutdownManager.deadline_remaining()`/`is_force_exit()`（Task 1）被 Task 2 的 `DrainingServer` 直接调用，签名（无参数、返回 `float`/`bool`）在两处一致。

## Kick-off Prompt

```
你是本次会话的实施者（implementer）。请严格按照
`/home/xp/refs/ai-agents/litellm/docs/superpowers/plans/2026-07-14-graceful-shutdown-phase1a.md`
逐个 Task 实施，遵循文档里写明的 TDD 步骤（先跑失败测试确认失败、再实现、再跑测试确认转绿、再提交），
不要跳过"确认测试先失败"这一步。

背景：这是一个私有 litellm fork（分支 ghc），本计划是"in-flight observability / graceful shutdown"
特性冻结 spec 的 Phase 1a（uvicorn 层 draining + watchdog/IAM refresh/redis 的关停正确性），
spec 原文见 `docs/superpowers/specs/2026-07-14-in-flight-observability-graceful-shutdown-design.md`。
Phase 1b（work-lease/ManagedTaskSupervisor/LoggingWorker.quiesce）是后续独立计划，不在本次范围内。

Task 1、Task 4、Task 5、Task 6 彼此没有前置依赖，可以任选顺序（甚至并行，如果你打算多开会话/子代理处理——
但由谁在哪执行不是你要决定的事，若需要拆分执行请向上一层汇报，不要自行派生）。
Task 2 依赖 Task 1；Task 3 依赖 Task 2；Task 7 依赖 Task 4 和 Task 5；Task 8 依赖前七个 Task 全部落地。

每个 Task 完成后运行一次 `make pre-commit`（若失败，修复后再提交；它只检查已 staged 的改动，
提交前确认 git status 里 staged 的文件就是这次改动涉及的文件）。

如果实施过程中发现某个 Task 的现有代码与本计划描述的"确认过的现状"不一致（行号漂移、
辅助函数签名不同、某个既有测试用例的构造 helper 还不支持计划里假设的关键字参数等），
先用最小必要的读操作核实清楚，按计划的设计意图调整落地方式，并在完成后的汇报里如实说明这处偏差——
不要因为偏差就静默收窄计划范围。
```
