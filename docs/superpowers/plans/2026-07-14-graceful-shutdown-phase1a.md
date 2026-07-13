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

**Files**: `litellm/proxy/shutdown/uvicorn_runner.py`（新）, `litellm/proxy/proxy_cli.py`（改）, `tests/test_litellm/proxy/shutdown/test_uvicorn_runner.py`（新）, `tests/test_litellm/proxy/test_proxy_cli.py`（已确认存在，扩展）。

**Interfaces**

```python
def run_uvicorn_with_draining_server(uvicorn_args: dict[str, Any], *, workers: int) -> None: ...
```

`uvicorn_args` 复用 `proxy_cli.py` 里已经组装好的同一个 dict（`_get_default_unvicorn_init_args` 的产物），不重新定义参数模型，避免和现有 CLI 参数组装逻辑产生第二套真相来源。行为上必须复刻 `uvicorn.main.run()`（`.venv/lib/python3.13/site-packages/uvicorn/main.py:569-579`，已核对）自身的 `try: <三分支 dispatch> except KeyboardInterrupt: pass` 包裹——direct 模式下前台 Ctrl+C 会在 `Server.capture_signals` 恢复默认信号处理并重新发送后，以 `KeyboardInterrupt` 形式穿透 `server.run()`；uvicorn 自己在 `main.run()` 里吞掉它以保证正常返回、不改变调用方看到的退出行为。本计划的 runner 是这层 try/except 的唯一落脚点（`proxy_cli.py` 不会再自己包一层），所以必须在这里补上，否则 direct 模式下的 Ctrl+C 会让异常穿过 runner，与 Task 8 `exit_code == 0` 的断言矛盾。import-string 校验（`main.run()` 里 `if (config.reload or config.workers > 1) and not isinstance(app, str)` 那段）与 UDS 清理（`finally: if config.uds ...`）不在本任务范围内——`proxy_cli.py` 现有参数面（无 UDS 选项、`app` 恒为 import string）已经排除了这两种场景所服务的失败模式，引入等于给不会触发的分支写测试；`KeyboardInterrupt` 包裹则是唯一必须复刻的部分。

**Steps**

1. `tests/test_litellm/proxy/test_proxy_cli.py` 是已确认存在的映射测试文件（`find tests/test_litellm/proxy -iname "*proxy_cli*"` 已核实），本任务直接扩展它，不再有命名不确定性。

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


def test_direct_mode_swallows_keyboard_interrupt_from_server_run():
    """Regression: uvicorn.main.run() (uvicorn/main.py:569-579) wraps its own
    three-branch dispatch in try/except KeyboardInterrupt: pass so a
    foreground Ctrl+C — which Server.capture_signals lets through as a real
    KeyboardInterrupt after restoring the default handler — returns normally
    instead of propagating. Losing this wrapper here would let the exception
    escape the runner and change the process's exit behavior (Task 8 asserts
    exit_code == 0 for a clean direct-mode SIGINT)."""
    with patch("litellm.proxy.shutdown.uvicorn_runner.DrainingServer") as mock_server_cls:
        mock_server = MagicMock()
        mock_server.config.should_reload = False
        mock_server.config.workers = 1
        mock_server.run.side_effect = KeyboardInterrupt()
        mock_server_cls.return_value = mock_server
        run_uvicorn_with_draining_server(_base_args(), workers=1)  # must not raise


def test_multiprocess_mode_swallows_keyboard_interrupt_from_supervisor_run():
    """Same wrapper, exercised through the Multiprocess supervisor branch —
    Ctrl+C on the parent process during reload/workers mode reaches
    supervisor.run(), not server.run() directly."""
    with (
        patch("litellm.proxy.shutdown.uvicorn_runner.DrainingServer") as mock_server_cls,
        patch("litellm.proxy.shutdown.uvicorn_runner.Multiprocess") as mock_multiprocess,
    ):
        mock_server = MagicMock()
        mock_server.config.should_reload = False
        mock_server.config.workers = 2
        mock_server.config.bind_socket.return_value = "SOCK"
        mock_server_cls.return_value = mock_server
        mock_multiprocess.return_value.run.side_effect = KeyboardInterrupt()
        run_uvicorn_with_draining_server(_base_args(), workers=2)  # must not raise
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


def run_uvicorn_with_draining_server(uvicorn_args: dict[str, Any], *, workers: int) -> None:
    from litellm.proxy.shutdown.draining_server import DrainingServer

    config = uvicorn.Config(**uvicorn_args, workers=workers)
    server = DrainingServer(config=config)
    try:
        if config.should_reload:
            sock = config.bind_socket()
            ChangeReload(config, target=server.run, sockets=[sock]).run()
        elif config.workers > 1:
            sock = config.bind_socket()
            Multiprocess(config, target=server.run, sockets=[sock]).run()
        else:
            server.run()
    except KeyboardInterrupt:
        # Mirrors uvicorn.main.run()'s own try/except KeyboardInterrupt: pass:
        # a foreground Ctrl+C surfaces here as a real KeyboardInterrupt once
        # Server.capture_signals restores the default handler and re-raises.
        # Swallow it so this function returns normally, matching uvicorn's own
        # contract instead of letting the exception change the process's exit
        # behavior out from under the CLI.
        pass
```

（`DrainingServer` 的 import 挪到函数体内，避免 `uvicorn_runner` 模块顶层和 `draining_server` 模块顶层互相 import 造成循环——两者都在 `litellm/proxy/shutdown/` 包内，`draining_server.py` 不反向 import `uvicorn_runner`，此处挪动纯粹是防御性写法，不代表存在实际循环依赖。）

4. 跑测试转绿。

5. 修改 `proxy_cli.py`：把 `uvicorn.run(**uvicorn_args, workers=num_workers)` 替换为 `run_uvicorn_with_draining_server(uvicorn_args, workers=num_workers)`，并加对应 import：

```python
from litellm.proxy.shutdown.uvicorn_runner import run_uvicorn_with_draining_server
```

6. **更新既有测试**：`test_proxy_cli.py` 里有 29 处 `@patch("uvicorn.run")`/`mock_uvicorn_run` 引用（已用 `grep -c` 核实），全部基于"调用点是裸 `uvicorn.run`"这一假设——改了调用点之后，这些测试会全部失败（`mock_uvicorn_run.assert_called_once()` 落空，因为 `uvicorn.run` 再也不会被调用）。这是纯机械的 patch 目标替换，不是设计决策：把每处

```python
    @patch("uvicorn.run")
```

替换为

```python
    @patch("litellm.proxy.proxy_cli.run_uvicorn_with_draining_server")
```

参数名 `mock_uvicorn_run` 保留不变（只是现在 mock 的是新函数，断言语义不变：`assert_not_called()`/`assert_called_once()` 依旧成立，因为新函数在同一个位置被调用同一次）。以 `test_skip_server_startup`（第 499-570 行，已读取确认）为例，完整改法：

```python
    @patch("litellm.proxy.proxy_cli.run_uvicorn_with_draining_server")
    @patch("atexit.register")  # critical
    @patch("litellm.proxy.db.prisma_client.PrismaManager.setup_database")
    @patch(
        "litellm.proxy.db.prisma_client.should_update_prisma_schema", return_value=False
    )
    def test_skip_server_startup(
        self, mock_should_update, mock_setup_db, mock_atexit_register, mock_uvicorn_run
    ):
        from click.testing import CliRunner

        from litellm.proxy.proxy_cli import run_server

        runner = CliRunner()

        mock_proxy_module = MagicMock(
            app=MagicMock(),
            ProxyConfig=MagicMock(),
            KeyManagementSettings=MagicMock(),
            save_worker_config=MagicMock(),
        )
        clean_env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("DATABASE_URL", "DIRECT_URL")
        }
        with (
            patch.dict(
                os.environ,
                clean_env,
                clear=True,
            ),
            patch.dict(
                "sys.modules",
                {
                    "proxy_server": mock_proxy_module,
                    "litellm.proxy.proxy_server": mock_proxy_module,
                },
            ),
            patch(
                "litellm.proxy.proxy_cli.ProxyInitializationHelpers._get_default_unvicorn_init_args"
            ) as mock_get_args,
        ):
            mock_get_args.return_value = {
                "app": "litellm.proxy.proxy_server:app",
                "host": "localhost",
                "port": 8000,
            }

            result = runner.invoke(run_server, ["--local", "--skip_server_startup"])

            assert (
                result.exit_code == 0
            ), f"exit_code={result.exit_code}, output={result.output}"
            assert "Skipping server startup" in result.output
            mock_uvicorn_run.assert_not_called()

            mock_uvicorn_run.reset_mock()

            result = runner.invoke(run_server, ["--local"])

            assert (
                result.exit_code == 0
            ), f"exit_code={result.exit_code}, output={result.output}"
            mock_uvicorn_run.assert_called_once()
```

（唯一改动是装饰器的 patch target，函数体和断言完全不变——因为调用参数形状不变：`run_uvicorn_with_draining_server(uvicorn_args, workers=num_workers)` 和原来的 `uvicorn.run(**uvicorn_args, workers=num_workers)` 在"是否被调用一次/零次"这个层面等价，只是从关键字展开变成位置+关键字传参，不影响 `assert_called_once()`/`assert_not_called()` 这类不检查具体参数的断言；对少数确实检查了 `call_args`/`call_args.kwargs` 具体内容的测试——如第 1041-1046 行、1177-1178 行、1240-1241 行读取 `timeout_keep_alive`/`limit_max_requests`/`limit_max_requests_jitter`——要把 `call_args.kwargs["xxx"]` 的取值方式确认一遍：`run_uvicorn_with_draining_server(uvicorn_args, workers=num_workers)` 的第一个位置参数就是完整的 `uvicorn_args` dict，所以原本从 `mock_uvicorn_run.call_args.kwargs["timeout_keep_alive"]` 取值的断言要改成 `mock_uvicorn_run.call_args.args[0]["timeout_keep_alive"]` 或 `mock_uvicorn_run.call_args[0][0]["timeout_keep_alive"]`，因为 `uvicorn_args` 现在是整个第一个位置参数而不是被展开的关键字）。用

```bash
grep -n '@patch("uvicorn.run")' tests/test_litellm/proxy/test_proxy_cli.py
```

逐一定位剩余 28 处，按上述规则替换 patch target；对每一处额外检查该测试是否读取了 `call_args.kwargs[...]`（`grep -n "call_args" tests/test_litellm/proxy/test_proxy_cli.py` 定位），凡命中的都要把取值方式从 kwargs 展开改成"读第一个位置参数字典里的对应 key"。全部替换完后跑：

```bash
pytest tests/test_litellm/proxy/test_proxy_cli.py -q
```

确认零失败、且 `grep -c '@patch("uvicorn.run")' tests/test_litellm/proxy/test_proxy_cli.py` 结果为 0（不再有测试假设裸 `uvicorn.run` 调用点）。

7. 在 `test_uvicorn_runner.py` 里补一条锁定调用点确实换了的回归测试（这条留在新文件里，因为它测的是 `proxy_cli` 模块级别的 import/调用关系，不依赖 CLI runner 的 click 触发链路）：

```python
def test_proxy_cli_module_imports_draining_runner_not_bare_uvicorn_run():
    """Regression pin: proxy_cli.py's direct-uvicorn branch must reference
    run_uvicorn_with_draining_server. A silent revert to a bare uvicorn.run
    call would compile and pass every existing behavioral test that doesn't
    specifically check for this import, since uvicorn.run and
    run_uvicorn_with_draining_server have compatible call shapes."""
    import inspect

    import litellm.proxy.proxy_cli as proxy_cli_module

    assert hasattr(proxy_cli_module, "run_uvicorn_with_draining_server")
    source = inspect.getsource(proxy_cli_module)
    assert "run_uvicorn_with_draining_server(uvicorn_args, workers=num_workers)" in source
```

8. `make pre-commit`；提交 `refactor: extract uvicorn runner branch, wire DrainingServer into proxy_cli`。

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

**关于测试构造方式的更正**：本文件目录里**没有** `_make_prisma_client` 这样的工厂函数（已用 grep 核实 `test_prisma_client_engine_watcher.py`/`test_prisma_client_reconnect.py` 均未直接构造 `PrismaClient(`，两者都只用同目录 `conftest.py` 里的 `prisma_client` fixture）。该 fixture 构造的是真实 `PrismaClient` 实例，`.db` 被替换成 `mock_prisma_client.db`（一个 `MagicMock`）。本任务测试一律改用这个既有 fixture，通过直接赋值实例属性来控制关停状态（与该 fixture 自身"`pc.db = mock_prisma_client.db`"的既有先例一致，不是新引入的 monkeypatch 模式）：`prisma_client._is_shutting_down = lambda: True`。

1. 写失败测试，加到 `test_prisma_client_engine_watcher.py`：

```python
def test_handle_engine_stopped_skips_reconnect_and_does_not_arm_expected_death_during_shutdown(
    prisma_client: Any,
):
    """During shutdown there is no replacement engine coming; recording into
    _expected_engine_deaths would be meaningless and reconnecting would race
    the teardown that's already in progress."""
    prisma_client._is_shutting_down = lambda: True
    prisma_client.db._expected_engine_deaths = set()
    with patch.object(prisma_client, "_cleanup_engine_watcher") as mock_cleanup, \
         patch.object(prisma_client, "attempt_db_reconnect", new_callable=AsyncMock) as mock_reconnect:
        prisma_client._handle_engine_stopped(pid=1234, cause="waitpid_thread")
    mock_cleanup.assert_called_once()
    mock_reconnect.assert_not_called()
    assert 1234 not in prisma_client.db._expected_engine_deaths


@pytest.mark.asyncio
async def test_handle_engine_stopped_reconnects_when_not_shutting_down_and_death_unplanned(
    prisma_client: Any,
):
    """Mocks attempt_db_reconnect itself (an AsyncMock, so asyncio.create_task
    can schedule the coroutine it returns) rather than patching the global
    asyncio.create_task — patching the global leaves unawaited-coroutine
    warnings for every OTHER task the code under test schedules, and hides
    what's actually being scheduled."""
    prisma_client._is_shutting_down = lambda: False
    prisma_client.db._expected_engine_deaths = set()
    with patch.object(prisma_client, "_cleanup_engine_watcher") as mock_cleanup, \
         patch.object(prisma_client, "_reap_all_zombies") as mock_reap, \
         patch.object(prisma_client, "attempt_db_reconnect", new_callable=AsyncMock) as mock_reconnect:
        prisma_client._handle_engine_stopped(pid=1234, cause="pidfd")
        await asyncio.sleep(0)  # let the create_task-scheduled coroutine run
    mock_reap.assert_called_once()
    mock_cleanup.assert_called_once()
    mock_reconnect.assert_awaited_once_with(reason="engine_process_death", force=True)
    assert prisma_client._engine_confirmed_dead is True


def test_handle_engine_stopped_skips_reconnect_for_planned_death_even_when_not_shutting_down(
    prisma_client: Any,
):
    prisma_client._is_shutting_down = lambda: False
    prisma_client.db._expected_engine_deaths = {1234}
    with patch.object(prisma_client, "_cleanup_engine_watcher") as mock_cleanup, \
         patch.object(prisma_client, "attempt_db_reconnect", new_callable=AsyncMock) as mock_reconnect:
        prisma_client._handle_engine_stopped(pid=1234, cause="os_kill_poll")
    mock_reconnect.assert_not_called()
    mock_cleanup.assert_called_once()
    assert 1234 not in prisma_client.db._expected_engine_deaths  # consumed


def test_on_engine_death_from_thread_delegates_to_handle_engine_stopped(prisma_client: Any):
    prisma_client._engine_pid = 999
    prisma_client._engine_confirmed_dead = False
    with patch.object(prisma_client, "_handle_engine_stopped") as mock_handler:
        prisma_client._on_engine_death_from_thread(999)
    mock_handler.assert_called_once_with(999, "waitpid_thread")


def test_on_pidfd_readable_delegates_to_handle_engine_stopped(prisma_client: Any):
    prisma_client._engine_pid = 999
    prisma_client._engine_confirmed_dead = False
    with patch.object(prisma_client, "_handle_engine_stopped") as mock_handler:
        prisma_client._on_pidfd_readable()
    mock_handler.assert_called_once_with(999, "pidfd")


def test_try_waitpid_watch_delegates_to_handle_engine_stopped_when_already_dead_at_start(
    prisma_client: Any,
):
    """Previously untested branch (reviewer Major 7): os.waitpid(pid, WNOHANG)
    reports the PID already reaped before the watcher even starts (the engine
    died in the window between spawn and watch-start)."""
    with patch("os.waitpid", return_value=(4321, 0)), \
         patch.object(prisma_client, "_handle_engine_stopped") as mock_handler:
        result = prisma_client._try_waitpid_watch(4321)
    assert result is True
    mock_handler.assert_called_once_with(4321, "waitpid_watch_start")


@pytest.mark.asyncio
async def test_poll_engine_proc_process_lookup_error_delegates_to_handle_engine_stopped(
    prisma_client: Any,
):
    """Previously untested branch (reviewer Major 7): os.kill(pid, 0) raises
    ProcessLookupError (the engine died between poll ticks). Only one loop
    iteration runs because the delegate call is followed by `return`, so this
    awaits directly with no risk of hanging."""
    prisma_client._watching_engine = True
    prisma_client._engine_pid = 555
    with patch("os.kill", side_effect=ProcessLookupError), \
         patch.object(prisma_client, "_handle_engine_stopped") as mock_handler:
        await prisma_client._poll_engine_proc()
    mock_handler.assert_called_once_with(555, "os_kill_poll")
```

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

**需要透明标注的一处行为变化**：`_poll_engine_proc` 原本是 `await self.attempt_db_reconnect(...)`——重连是这条轮询协程自身生命周期内的一部分，若轮询任务被取消，正在进行的重连也会跟着被取消。统一到 `_handle_engine_stopped` 之后变成 `asyncio.create_task(self.attempt_db_reconnect(...))`（fire-and-forget），重连不再受轮询协程取消的牵连，独立运行直到完成或被 `is_shutting_down()` guard 短路。这是四个探测器统一为一个方法**必然带来**的行为差异（另外三个探测器——waitpid 线程回调、pidfd 回调、watch-start 时已死分支——原本就是 `asyncio.create_task`，只有这一个是 `await`），不是本任务引入的新缺陷；如果这个差异不可接受（例如依赖轮询任务取消来连带取消重连的既有调用方），需要在实施期单独处理，计划层面按"统一路径优先、这一处差异可接受"来推进。

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
async def test_attempt_reconnect_inside_lock_skips_when_shutdown_flips_after_acquiring_lock(
    prisma_client: Any,
):
    """Reconnect task queued behind the lock, then shutdown starts, then the
    lock is released — the queued task must not reconnect once it wakes up."""
    flag = {"shutting_down": False}
    prisma_client._is_shutting_down = lambda: flag["shutting_down"]
    flag["shutting_down"] = True
    with patch.object(prisma_client, "_run_reconnect_cycle") as mock_cycle:
        result = await prisma_client._attempt_reconnect_inside_lock(force=True, reason="test", timeout_seconds=None)
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
async def test_start_engine_watcher_noop_during_shutdown(prisma_client: Any):
    prisma_client._is_shutting_down = lambda: True
    with patch.object(prisma_client, "_get_engine_pid") as mock_get_pid:
        await prisma_client._start_engine_watcher()
    mock_get_pid.assert_not_called()


def test_handle_writer_engine_replaced_noop_during_shutdown(prisma_client: Any):
    prisma_client._is_shutting_down = lambda: True
    with patch.object(prisma_client, "_cleanup_engine_watcher") as mock_cleanup:
        prisma_client._handle_writer_engine_replaced()
    mock_cleanup.assert_not_called()
```

5. 跑全部新测试转绿，跑整个 `prisma_and_spend/` 目录确认无破坏。`make pre-commit`；提交 `refactor: unify engine-death detectors into _handle_engine_stopped, guard reconnect during shutdown`。

---

### Task 5 — IAM token 刷新第二条 recreate 路径的关停 guard

**依赖 Task 4**（不再是"独立"——见下方 Major 8 修复说明）。两者共享同一 DI 参数命名约定 `is_shutting_down`，便于 Task 7 统一引用。

> **计划依赖图变化（需向协调者报告的真实取舍）**：reviewer 的 Major 8 要求把 `is_shutting_down` 一路透传进 `PrismaClient.__init__`（Task 4 所在文件 `litellm/proxy/utils.py`）里构造 `PrismaWrapper(...)` 的 3 个调用点。但 `PrismaWrapper.__init__` 接受 `is_shutting_down` 这个新参数本身是 Task 5 的产物。也就是说"3 个构造点透传"这一步必须在 Task 5 的 `PrismaWrapper.__init__` 改动完成之后才能落地，原计划"Task 4 与 Task 5 互相独立、可并行"的说法不再成立。处理方式：把 3 个构造点的透传 + 对应 DI 身份断言测试放在 **Task 5 的最后一步**（Step 4，见下）；Task 5 因此从"独立"改为"依赖 Task 4"（需要 `PrismaClient._is_shutting_down`、`_handle_engine_stopped` 等 Task 4 产物已经落地，构造点所在的类才存在完整上下文）。Task 4 自身不受影响，仍可独立先行。

**Files**: `litellm/proxy/db/prisma_client.py`, `tests/test_litellm/proxy/db/test_prisma_client.py`, `litellm/proxy/utils.py`（新增 Step 4，透传 3 个构造点）, `tests/test_litellm/proxy/utils/prisma_and_spend/test_prisma_client_lifecycle.py`（新增 Step 4 的 DI 身份断言测试）

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

**关于测试构造方式的更正**：`test_prisma_client.py` 目前**没有** `_make_prisma_wrapper` 这样的共享 helper——文件里现有 3 处用法都是直接内联 `PrismaWrapper(original_prisma=mock_prisma, iam_token_db_auth=False)`（`mock_prisma_binary` 是 autouse fixture，已 patch 了 `prisma` 模块导入）。本任务在文件内新增一个局部 helper `_wrapper(is_shutting_down=...)`（不是"复用既有 helper"，是新写的，仅供本文件内新增的 guard 测试使用），签名对齐现有内联构造：

```python
def _wrapper(is_shutting_down: Callable[[], bool] = lambda: False, iam_token_db_auth: bool = True) -> PrismaWrapper:
    mock_prisma = AsyncMock()
    return PrismaWrapper(
        original_prisma=mock_prisma,
        iam_token_db_auth=iam_token_db_auth,
        is_shutting_down=is_shutting_down,
    )
```

1. 写失败测试：

```python
@pytest.mark.asyncio
async def test_token_refresh_loop_exits_without_busy_looping_when_shutting_down(mock_prisma_binary):
    """Guard #1 - real bug this pins: when _calculate_seconds_until_refresh()
    returns <= 0, `if sleep_seconds > 0` skips the sleep entirely (see
    litellm/proxy/db/prisma_client.py's real loop body), so a shutdown check
    placed right after it has ZERO await points between iterations if it
    merely `continue`s - a genuine CPU-spinning busy loop that can never be
    cancelled. The fix must `break` (or `return`), not `continue`: the loop
    terminates normally once shutdown is detected, and _safe_refresh_token is
    never called. stop_token_refresh_task() awaiting/cancelling an
    already-exited loop is a no-op, so exiting early is safe."""
    wrapper = _wrapper(is_shutting_down=lambda: True)
    with patch.object(wrapper, "_calculate_seconds_until_refresh", return_value=0), \
         patch.object(wrapper, "_safe_refresh_token") as mock_refresh:
        try:
            # bounded wait: pre-fix (`continue`) this spins forever with no
            # await point, so a bare `await` would hang the whole suite.
            await asyncio.wait_for(wrapper._token_refresh_loop(), timeout=1.0)
        except asyncio.TimeoutError:
            pytest.fail(
                "_token_refresh_loop did not return within 1s: the "
                "post-sleep shutdown check is busy-looping without an "
                "await point (continue instead of break)"
            )
    mock_refresh.assert_not_called()


@pytest.mark.asyncio
async def test_safe_refresh_token_skips_recreate_when_shutting_down_after_acquiring_lock():
    """Guard #2. This test doubles as the reviewer-required 'lock-race'
    coverage for Guard #1's interaction with the loop: it exercises the exact
    scenario of "already inside _safe_refresh_token, lock acquired, shutdown
    flag flips" - which is what actually happens when the loop's Guard #1
    check races a shutdown that starts *after* the loop has already entered
    _safe_refresh_token. No separate test is added for that race; it would
    duplicate this one under a different name."""
    wrapper = _wrapper(is_shutting_down=lambda: True)
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
    wrapper = _wrapper(is_shutting_down=lambda: True)
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
    wrapper = _wrapper(is_shutting_down=lambda: True)
    monkeypatch.setattr(wrapper, "is_token_expired", lambda: True)
    with patch.object(wrapper, "_recreate_prisma_client_locked") as mock_recreate:
        _ = wrapper.some_prisma_attribute  # triggers __getattr__'s fire-and-forget refresh
        await asyncio.sleep(0)  # let the scheduled task run
    mock_recreate.assert_not_called()
```

（最后一个测试里 `wrapper.some_prisma_attribute` 的具体触发方式需要对齐 `__getattr__` 现有实现对"哪些属性名会触发 IAM 刷新检查"的既有判断逻辑，照抄该文件里已有的、命中 `__getattr__` 刷新分支的现有测试用例的属性名/mock 结构。）

跑一下确认全部失败：前三个因为 guard 尚未实现而 `mock_*.assert_not_called()` 失败（Guard #1 那条会在 1 秒超时后以 `pytest.fail` 的明确诊断信息失败，而不是挂起整个测试进程）。

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
                        "%sExiting RDS IAM token refresh loop; shutdown in progress.",
                        self._log_prefix,
                    )
                    break  # NOT continue: when sleep_seconds <= 0 the sleep above is
                    # skipped entirely, so continue would spin with zero await points
                    # between iterations - a genuine busy loop that cancellation can
                    # never interrupt. Exiting is safe: stop_token_refresh_task() only
                    # cancels/awaits this task, and cancelling/awaiting an
                    # already-completed task is a documented no-op.

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

4. **打通 DI 透传（Major 8）**。到这一步 `PrismaWrapper.__init__` 已经接受 `is_shutting_down`（Step 2），但 `litellm/proxy/utils.py` 里 `PrismaClient.__init__` 内构造 `PrismaWrapper(...)` 的 3 个调用点（确认于当前代码 `litellm/proxy/utils.py:2807-2865`）都还没有把它传进去——DI 契约在这里断开了。3 个调用点分别是：`http_client is not None` 分支的 writer、`http_client is None` 分支的 writer、以及 `read_replica_url` 分支内的 reader。

先写失败测试，加到 `tests/test_litellm/proxy/utils/prisma_and_spend/test_prisma_client_lifecycle.py`（该文件现有测试只用 `patched_prisma_import`，不用会整体替换 `PrismaWrapper` 的 `prisma_client` fixture，所以 `pc.db` 落地的是一个真实、未被替换的 `PrismaWrapper` 实例，身份断言才有意义）：

```python
def test_prismaclient_init_wires_is_shutting_down_into_writer_wrapper(patched_prisma_import: MagicMock) -> None:
    from litellm.proxy.utils import PrismaClient, PrismaWrapper

    proxy_logging_obj = MagicMock(name="MockProxyLogging")
    proxy_logging_obj.failure_handler = AsyncMock()
    pc = PrismaClient(
        database_url="postgresql://test:test@localhost:5432/test",
        proxy_logging_obj=proxy_logging_obj,
    )
    assert isinstance(pc.db, PrismaWrapper)
    assert pc.db._is_shutting_down is pc._is_shutting_down


def test_prismaclient_init_wires_is_shutting_down_into_writer_and_reader_wrappers(
    monkeypatch: pytest.MonkeyPatch,
    patched_prisma_import: MagicMock,
) -> None:
    """Read-replica path: both the writer and reader PrismaWrapper inside the
    RoutingPrismaWrapper must receive the same is_shutting_down callable."""
    from litellm.proxy.utils import PrismaClient, PrismaWrapper, RoutingPrismaWrapper

    monkeypatch.setenv("DATABASE_URL_READ_REPLICA", "postgresql://reader@reader.local:5432/test")
    proxy_logging_obj = MagicMock(name="MockProxyLogging")
    proxy_logging_obj.failure_handler = AsyncMock()
    pc = PrismaClient(
        database_url="postgresql://test:test@localhost:5432/test",
        proxy_logging_obj=proxy_logging_obj,
    )
    assert isinstance(pc.db, RoutingPrismaWrapper)
    assert isinstance(pc.db.writer, PrismaWrapper)
    assert isinstance(pc.db.reader, PrismaWrapper)
    assert pc.db.writer._is_shutting_down is pc._is_shutting_down
    assert pc.db.reader._is_shutting_down is pc._is_shutting_down
```

（第二个测试的 `DATABASE_URL_READ_REPLICA` 走读副本构造分支时不带 IAM（`IAM_TOKEN_DB_AUTH` 未设置，`iam_flag` 为 `False`），所以不会触发 `generate_iam_auth_token`，构造路径干净；这个"设置读副本 env var、不启用 IAM、走真实构造路径"的组合已经是 `tests/test_litellm/proxy/db/test_routing_prisma_wrapper.py:855-899` 里验证过的可行模式。）

跑一下确认失败：`AttributeError`（`is_shutting_down` 还没作为关键字传下去，`pc.db._is_shutting_down` 会是 `PrismaWrapper.__init__` 的默认值 `GracefulShutdownManager.is_shutting_down`，不是 `pc._is_shutting_down`，身份断言 `is` 失败）。

实现：3 个构造点各加一行 `is_shutting_down=self._is_shutting_down`：

```python
        if http_client is not None:
            writer_wrapper = PrismaWrapper(
                original_prisma=Prisma(http=http_client),
                iam_token_db_auth=iam_flag,
                log_prefix=writer_log_prefix,
                is_shutting_down=self._is_shutting_down,
            )
        else:
            writer_wrapper = PrismaWrapper(
                original_prisma=Prisma(),
                iam_token_db_auth=iam_flag,
                log_prefix=writer_log_prefix,
                is_shutting_down=self._is_shutting_down,
            )
        ...  # read-replica branch unchanged until:
                reader_wrapper = PrismaWrapper(
                    original_prisma=reader_prisma,
                    iam_token_db_auth=iam_flag,
                    db_url_env_var="DATABASE_URL_READ_REPLICA",
                    iam_endpoint=reader_iam_endpoint,
                    recreate_uses_datasource=True,
                    log_prefix="[reader]",
                    is_shutting_down=self._is_shutting_down,
                )
```

跑新测试转绿；跑整个 `prisma_and_spend/` 目录 + `test_routing_prisma_wrapper.py` 确认无破坏。`make pre-commit`；提交 `fix: wire is_shutting_down through all three PrismaWrapper construction sites`。

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
    kind: Literal["skipped_during_shutdown"] = "skipped_during_shutdown"

# litellm/proxy/proxy_server.py
async def _increment_spend_counter_cache(
    counter_key: str, increment: float
) -> float | AccountingSkippedDuringShutdown: ...
```

（`kind` 字段是可选加固：Phase 1a 只有这一个成员，`isinstance` 已经足以让调用方判别；但 Phase 1b 要把它扩成 `AccountingCompleted | AccountingSkippedDuringShutdown | AccountingFailed` 的完整 tagged union，现在就把 `Literal` 判别字段焊死在成员上，能让 Phase 1b 落地时直接用 `match`/`kind` 判别而不必回头给已经存在的类型加字段、不必找出所有现存构造点补参数——这是"现在顺手做，避免以后返工"，不是当前 Phase 1a 逻辑需要它。）

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
    assert outcome.kind == "skipped_during_shutdown"
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
from typing import Literal


@dataclass(frozen=True, slots=True)
class AccountingSkippedDuringShutdown:
    """A DB/redis touch was skipped because shutdown is in progress."""

    reason: str
    kind: Literal["skipped_during_shutdown"] = "skipped_during_shutdown"
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

**Files**: `litellm/proxy/proxy_server.py`（lifespan 关停块）, `tests/test_litellm/proxy/proxy_server/test_lifecycle.py`（冻结为本 Task 的测试落点——已核实该文件存在，是 `proxy_startup_event`/`proxy_shutdown_event` 的既定映射测试文件，不新建）

**Steps**

1. 已核实：`tests/test_litellm/proxy/proxy_server/test_lifecycle.py` 顶部文档字符串就声明覆盖 `proxy_startup_event`/`proxy_shutdown_event`；文件里已有 `test_proxy_shutdown_event_disconnects_prisma_and_resets`（覆盖独立的 `proxy_shutdown_event()` 函数，不是本 Task 要改的、嵌在 `proxy_startup_event` 生成器 `yield` 之后的那段关停块）与 `test_proxy_startup_event_is_async_context_manager_with_expected_signature`（只查签名，不实际驱动）。另外核实到 `tests/test_litellm/proxy/test_proxy_server.py:1105-1174`（`test_aaaproxy_startup_master_key`）是仓库里唯一一处真正 `async with proxy_startup_event(app):` 完整驱动、只 mock `ProxyStartupEvent._setup_prisma_client` 返回一个手写 fake prisma 类的既有先例——本 Task 的新测试照抄这一驱动方式（不新造机制），落在 `test_lifecycle.py` 而不是 `test_proxy_server.py`（后者是遗留大文件，`test_lifecycle.py` 才是 `proxy_startup_event` 的既定映射位置）。

2. 写失败测试（完整代码，无 `...` 占位；加到 `test_lifecycle.py` 的 `# proxy_startup_event` 分区）：

```python
class _FakeShutdownDb:
    def __init__(self, call_order: list[str]) -> None:
        self._call_order = call_order

    async def stop_token_refresh_task(self) -> None:
        self._call_order.append("stop_token_refresh_task")


class _FakeShutdownPrisma:
    """Stands in for PrismaClient across the ordering test: only the two
    hasattr-guarded shutdown hooks the lifespan block actually calls."""

    def __init__(self, call_order: list[str]) -> None:
        self.db = _FakeShutdownDb(call_order)
        self._call_order = call_order

    async def stop_db_health_watchdog_task(self) -> None:
        self._call_order.append("stop_db_health_watchdog_task")


@pytest.mark.asyncio
async def test_lifespan_shutdown_stops_iam_refresh_and_watchdog_before_closing_aiohttp_session(monkeypatch):
    """Regression: today stop_token_refresh_task/stop_db_health_watchdog_task
    run AFTER the shared aiohttp session is closed; the 9-step quiesce
    contract (spec) requires them to run right after wait_for_drain(), before
    any teardown of shared dependencies. A silent revert of this ordering
    would let a background loop touch a half-closed aiohttp session."""
    call_order: list[str] = []
    fake_prisma = _FakeShutdownPrisma(call_order)

    async def _fake_wait_for_drain() -> None:
        call_order.append("wait_for_drain")

    async def _fake_close_session() -> None:
        call_order.append("close_aiohttp_session")

    fake_session = MagicMock()
    fake_session.close = AsyncMock(side_effect=_fake_close_session)

    monkeypatch.setattr(ps.GracefulShutdownManager, "start_shutdown", lambda: None)
    monkeypatch.setattr(ps.GracefulShutdownManager, "wait_for_drain", _fake_wait_for_drain)
    monkeypatch.setattr(ps, "_initialize_shared_aiohttp_session", AsyncMock(return_value=fake_session))
    monkeypatch.setattr(ps, "proxy_shutdown_event", AsyncMock())  # unrelated teardown, not under test here

    with patch.object(ps.ProxyStartupEvent, "_setup_prisma_client", return_value=fake_prisma):
        app = FastAPI()
        async with proxy_startup_event(app):
            pass  # only the shutdown half (after yield, on context exit) is under test

    assert call_order == [
        "wait_for_drain",
        "stop_token_refresh_task",
        "stop_db_health_watchdog_task",
        "close_aiohttp_session",
    ]
```

（`ps` 是该文件顶部既有的 `import litellm.proxy.proxy_server as ps` 别名；`FastAPI`、`AsyncMock`、`MagicMock`、`patch` 均已在该文件顶部导入，直接复用。）

跑一下确认失败（当前顺序是 `wait_for_drain → close_aiohttp_session → stop_token_refresh_task → stop_db_health_watchdog_task`，断言的 list 不匹配）。

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

4. 跑测试转绿；跑整个 `tests/test_litellm/proxy/proxy_server/test_lifecycle.py` 确认无破坏，尤其关注既有的、可能对旧顺序做了隐式假设的测试。`make pre-commit`；提交 `fix: stop IAM refresh and watchdog right after drain, before tearing down shared dependencies`。

---

### Task 8 — 真实 subprocess SIGINT/SIGTERM E2E 测试

依赖 Task 1、2、3、4、5、6、7 全部落地（这是整合验证）。

**Files**: `tests/e2e/shutdown/__init__.py`（新）, `tests/e2e/shutdown/conftest.py`（新）, `tests/e2e/shutdown/subprocess_harness.py`（新）, `tests/e2e/shutdown/fake_upstream.py`（新）, `tests/e2e/shutdown/test_graceful_shutdown_e2e.py`（新）, `tests/e2e/CLAUDE.md`（补 Suite folders 表新行）

**Interfaces**

```python
# subprocess_harness.py
@dataclasses.dataclass(frozen=True, slots=True)
class SpawnedProxy:
    process: subprocess.Popen[bytes]
    port: int
    stdout_path: pathlib.Path
    stderr_path: pathlib.Path

def spawn_proxy(*, mode: Literal["direct", "reload", "workers", "limit_max_requests"], config_path: pathlib.Path, tmp_path: pathlib.Path, limit_max_requests: int | None = None) -> SpawnedProxy: ...
def wait_for_health(port: int, *, timeout: float) -> None: ...
def terminate_process_group(proxy: SpawnedProxy, *, term_timeout: float) -> None: ...

# fake_upstream.py
@dataclasses.dataclass(frozen=True, slots=True)
class FakeUpstream:
    port: int
    received: threading.Event  # set the instant a request body arrives
    release: threading.Event  # test sets this to let the handler respond

def start_fake_upstream() -> FakeUpstream: ...
def stop_fake_upstream(upstream: FakeUpstream) -> None: ...
```

本套件不复用 `tests/e2e/` 既有的 `Transport`/`Gateway`（它们假设一个已经由外部管理、地址固定的代理），而是自己拉起/kill 子进程、自己选端口、自己发信号——这是刻意偏离共享 harness 的一处，原因是这个套件测的正是"进程本身如何响应信号并退出"，与既有套件"对一个已跑起来的代理发业务请求"的假设不兼容，`scoped_key`/`resources`/覆盖率注册表里按 endpoint/behavior 建模的 `Gateway` 方法在这里都用不上。健康检查/请求探测用 `httpx`（模块内自建的 client），**不是** `e2e_http.py` 禁止的 `requests` 库（`tests/e2e/CLAUDE.md` 里"never touch requests directly"这条规则的检查脚本按名字就是查 `requests.*`，`httpx` 是完全不同的包，不在该规则的字面约束范围内；目标端口本身也是每次动态分配的临时子进程端口，不是 `e2e_http.py` 依赖的固定环境变量 base_url，硬套 `Transport` 反而要为一次性子进程伪造一整套 base_url 生命周期管理）。

**关于 e2e 全局跳过钩子的绕开方式（Blocker 3a）**：本套件的测试**不**打 `@pytest.mark.e2e`——`tests/e2e/conftest.py` 的 `pytest_runtest_setup` 只在 `item.get_closest_marker("e2e")` 命中时才跳过（探测固定的 `PROXY_BASE_URL`），本套件测试如果打了这个 marker 会在还没跑到自己的代码之前就被判定"没有外部代理在跑"而跳过。改用独立 marker `spawned_proxy_e2e`，在新增的 `tests/e2e/shutdown/conftest.py` 里注册，不touch 根 `conftest.py`（不需要为它加任何跳过豁免——本来就不会被那个钩子捕获）：

```python
# tests/e2e/shutdown/conftest.py
import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "spawned_proxy_e2e: real subprocess + real OS signal graceful-shutdown coverage; "
        "spawns and tears down its own proxy process per test rather than assuming one is "
        "already running, so it is deliberately NOT caught by the global `e2e` liveness-probe skip.",
    )
```

**Steps**

1. `tests/e2e/CLAUDE.md` 的 Suite folders 表新增一行：

```markdown
- `shutdown/` - process-level graceful shutdown: real SIGINT/SIGTERM against a spawned uvicorn subprocess, across direct/reload/workers/limit_max_requests entrypoints. Uses its own `spawned_proxy_e2e` marker and spawns/tears down its own proxy per test rather than assuming a fixed externally-managed one; not part of the shared Transport/Gateway pattern (see the suite's own module docstring for why)
```

2. 写 `subprocess_harness.py`：

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
import os
import pathlib
import signal
import socket
import subprocess
import sys
import time
from typing import Literal

import httpx

_PORT_BIND_RETRIES = 3


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


def _build_args(
    *,
    mode: Literal["direct", "reload", "workers", "limit_max_requests"],
    config_path: pathlib.Path,
    port: int,
    limit_max_requests: int | None,
) -> list[str]:
    args = [
        sys.executable,
        "-m",
        "litellm.proxy.proxy_cli",
        "--config",
        str(config_path),
        "--port",
        str(port),
        "--host",
        "127.0.0.1",
    ]
    if mode == "reload":
        args.append("--reload")
    elif mode == "workers":
        args += ["--num_workers", "2"]
    elif mode == "limit_max_requests":
        assert limit_max_requests is not None, "limit_max_requests mode requires an explicit threshold"
        args += ["--max_requests_before_restart", str(limit_max_requests)]
    return args


def spawn_proxy(
    *,
    mode: Literal["direct", "reload", "workers", "limit_max_requests"],
    config_path: pathlib.Path,
    tmp_path: pathlib.Path,
    limit_max_requests: int | None = None,
) -> SpawnedProxy:
    """Retries a bounded number of times on a port-bind race (the free port
    picked by `_free_port()` gets grabbed by another process before uvicorn
    itself binds it) - detected as the process exiting near-instantly instead
    of ever answering the health probe."""
    last_error: Exception | None = None
    for attempt in range(_PORT_BIND_RETRIES):
        port = _free_port()
        args = _build_args(mode=mode, config_path=config_path, port=port, limit_max_requests=limit_max_requests)
        stdout_path = tmp_path / f"{mode}_{attempt}_stdout.log"
        stderr_path = tmp_path / f"{mode}_{attempt}_stderr.log"
        with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
            process = subprocess.Popen(args, stdout=out, stderr=err, start_new_session=True)
        try:
            wait_for_health(port, timeout=15.0)
            return SpawnedProxy(process=process, port=port, stdout_path=stdout_path, stderr_path=stderr_path)
        except TimeoutError as e:
            last_error = e
            if process.poll() is None:
                terminate_process_group(
                    SpawnedProxy(process=process, port=port, stdout_path=stdout_path, stderr_path=stderr_path),
                    term_timeout=5.0,
                )
            continue
    raise TimeoutError(f"proxy never became healthy after {_PORT_BIND_RETRIES} attempts: {last_error}")


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


def terminate_process_group(proxy: SpawnedProxy, *, term_timeout: float) -> None:
    """TERM the whole process group, KILL as fallback. `reload`/`workers` mode
    spawns child worker processes under the supervisor we launched directly;
    signalling only that single PID (not its group) leaves orphaned children
    behind if the graceful path itself is what's broken. Started with
    `start_new_session=True` so this process is its own group leader and
    `os.killpg` doesn't reach the pytest runner's own group."""
    if proxy.process.poll() is not None:
        return
    try:
        os.killpg(proxy.process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proxy.process.wait(timeout=term_timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proxy.process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    proxy.process.wait(timeout=5.0)
```

（`send_signal_and_wait_exit` 不作为 harness 的共享函数：真正要验证的是"uvicorn 自己的 reload/workers supervisor 收到信号后如何把关停传导给子 worker"，所以测试里直接对 `proxy.process`（我们 spawn 出来的唯一顶层进程）调用 `process.send_signal(sig)` + `process.wait(timeout=...)`——同一个 PID，只是不经过封装，避免封装出"发信号"这一步反而掩盖了"到底是发给单进程还是发给整个 group"这个关键区别。`terminate_process_group` 只用于 `finally` 清理，不用于测试主体要验证的信号路径。）

3. 写 `fake_upstream.py`（Major 5 需要的可观测握手：不用 `time.sleep` 猜测请求是否已到达工作进程，而是让请求真正打到一个本地假上游，事件由假上游的处理线程置位，这个线程和 pytest 主线程同在一个进程里，所以 `threading.Event` 天然跨"发请求的线程"和"断言的主线程"可见）：

```python
"""A minimal in-process HTTP server standing in for the LLM provider. It runs
inside the pytest process (not the spawned proxy subprocess), so a
threading.Event it sets is directly observable from the test's main thread the
instant a request body arrives - replacing a guessed `time.sleep()` with a
real handshake for "has the in-flight request actually reached the point where
it would block on the upstream call".
"""

from __future__ import annotations

import dataclasses
import http.server
import threading


@dataclasses.dataclass(frozen=True, slots=True)
class FakeUpstream:
    _server: http.server.HTTPServer
    _thread: threading.Thread
    received: threading.Event
    release: threading.Event
    port: int


_RESPONSE_BODY = (
    b'{"id": "fake", "object": "chat.completion", "model": "fake-model", '
    b'"choices": [{"index": 0, "finish_reason": "stop", '
    b'"message": {"role": "assistant", "content": "ok"}}], '
    b'"usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}'
)


def start_fake_upstream() -> FakeUpstream:
    received = threading.Event()
    release = threading.Event()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib-mandated method name
            _ = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            received.set()
            release.wait(timeout=30.0)  # held until the test permits the response
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(_RESPONSE_BODY)))
            self.end_headers()
            self.wfile.write(_RESPONSE_BODY)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # silence stdlib's default per-request stderr logging

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="fake-upstream")
    thread.start()
    return FakeUpstream(_server=server, _thread=thread, received=received, release=release, port=server.server_port)


def stop_fake_upstream(upstream: FakeUpstream) -> None:
    upstream.release.set()  # unblock a handler thread stuck in a still-open request, if any
    upstream._server.shutdown()
    upstream._thread.join(timeout=5.0)
```

4. 两个不同的最小 config fixture（Blocker 3c 与 Major 5 各自服务不同的测试需求，不是同一个 fixture 的两种叫法），都加到 `tests/e2e/shutdown/conftest.py`（在 Step 2 已有的 `pytest_configure` 之上追加，顶部补齐这两个 fixture 需要的 import）：

```python
# tests/e2e/shutdown/conftest.py（在已有 pytest_configure 基础上追加）
import pathlib
from typing import Iterator

import pytest

from .fake_upstream import FakeUpstream, start_fake_upstream, stop_fake_upstream
```

- `slow_mock_litellm_config`：用 LiteLLM SDK 自带的 `mock_response`/`mock_delay`（`litellm/main.py:613-622` 确认过这两个 kwarg 走 `should_run_mock_completion` 分支，不需要真实 provider key），产出一个响应人为延迟 ~1.5 秒的模型。给**不需要精确握手**的测试用（Major 4 的 `limit_max_requests` 自触发测试、Major 6 的纯生命周期日志 oracle 测试）：

```python
@pytest.fixture
def slow_mock_litellm_config(tmp_path: pathlib.Path) -> pathlib.Path:
    config_path = tmp_path / "litellm-config.yaml"
    config_path.write_text(
        "model_list:\n"
        "  - model_name: mock-slow-model\n"
        "    litellm_params:\n"
        "      model: openai/mock-slow-model\n"
        "      mock_response: \"ok\"\n"
        "      mock_delay: 1.5\n"
        "general_settings: {}\n"
        "litellm_settings: {}\n"
    )
    return config_path
```

- `fake_upstream_litellm_config`：产出一个真正指向本地假上游 `api_base` 的 config，给**需要精确握手**的测试用（Major 5 的所有"信号中途到达"测试）：

```python
@pytest.fixture
def fake_upstream_litellm_config(tmp_path: pathlib.Path) -> Iterator[tuple[pathlib.Path, FakeUpstream]]:
    upstream = start_fake_upstream()
    config_path = tmp_path / "litellm-config.yaml"
    config_path.write_text(
        "model_list:\n"
        "  - model_name: fake-upstream-model\n"
        "    litellm_params:\n"
        "      model: openai/fake-model\n"
        f"      api_base: http://127.0.0.1:{upstream.port}/v1\n"
        "      api_key: sk-fake\n"
        "general_settings: {}\n"
        "litellm_settings: {}\n"
    )
    try:
        yield config_path, upstream
    finally:
        stop_fake_upstream(upstream)
```

（先本地手动跑一次 `litellm --config <这个生成的 config>` 确认 `openai/fake-model` + 自定义 `api_base` 这个组合确实会把请求路由到 `api_base` 而不是走真实 OpenAI 域名——这是本任务新引入的 fixture，必须先验证其行为再信任，不是照抄一个已验证过的既有 fixture。）

5. 写 `test_graceful_shutdown_e2e.py`。分两类：不碰真实 DB/Redis 的纯生命周期用例（默认跑），以及需要真实 Postgres/Redis 才能验证"关停期不重连/不写 redis"的竞态用例（没有对应环境变量时显式 `pytest.skip`，不是静默通过）：

```python
"""
Real subprocess + real OS signal graceful-shutdown coverage. Each test spawns
an actual litellm proxy process (not an in-process TestClient), synchronizes
on an observable handshake (fake-upstream Event, not a guessed sleep) before
firing a signal or letting uvicorn's own limit_max_requests trigger shutdown
on its own, then asserts both that the in-flight request drained successfully
and that the process exited within the frozen deadline plus a small buffer.
"""

from __future__ import annotations

import os
import signal
import threading
import time

import httpx
import pytest

from .fake_upstream import FakeUpstream
from .subprocess_harness import SpawnedProxy, spawn_proxy, terminate_process_group

pytestmark = pytest.mark.spawned_proxy_e2e

_SHUTDOWN_TIMEOUT_S = 5.0
_EXIT_WAIT_BUFFER_S = 3.0

_BANNED_LOG_PATTERNS = (
    "triggering reconnect",
    "Attempting Prisma DB reconnect",
    "ClientNotConnectedError",
    "LiteLLM Redis Caching: async async_increment",
)


def _combined_output(proxy: SpawnedProxy) -> str:
    return proxy.stdout_path.read_text(errors="replace") + proxy.stderr_path.read_text(errors="replace")


def _assert_no_shutdown_races(proxy: SpawnedProxy) -> None:
    text = _combined_output(proxy)
    for pattern in _BANNED_LOG_PATTERNS:
        assert pattern not in text, f"found banned pattern {pattern!r} in combined stdout+stderr"


def _fire_request(port: int, results: dict, *, timeout: float) -> None:
    try:
        response = httpx.post(
            f"http://127.0.0.1:{port}/chat/completions",
            json={"model": "fake-upstream-model", "messages": [{"role": "user", "content": "hi"}]},
            timeout=timeout,
        )
        results["status_code"] = response.status_code
    except httpx.HTTPError as e:
        results["error"] = str(e)


class TestSignalDrivenShutdown:
    """Pure process-lifecycle coverage: no real DB/Redis configured, so the
    banned-pattern assertions are meaningful because those subsystems are
    entirely absent, not because they merely happened not to fail this run."""

    @pytest.mark.parametrize(
        "mode,sig",
        [
            ("direct", signal.SIGINT),
            ("direct", signal.SIGTERM),
            ("reload", signal.SIGINT),
            ("workers", signal.SIGTERM),  # supervisor-mode SIGTERM coverage
        ],
    )
    def test_signal_drains_inflight_request_then_exits_within_deadline(
        self, mode, sig, tmp_path, fake_upstream_litellm_config, monkeypatch
    ):
        config_path, upstream = fake_upstream_litellm_config
        monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT", str(_SHUTDOWN_TIMEOUT_S))
        proxy = spawn_proxy(mode=mode, config_path=config_path, tmp_path=tmp_path)
        try:
            results: dict = {}
            request_thread = threading.Thread(
                target=_fire_request,
                args=(proxy.port, results),
                kwargs={"timeout": _SHUTDOWN_TIMEOUT_S + _EXIT_WAIT_BUFFER_S},
            )
            request_thread.start()
            assert upstream.received.wait(timeout=10.0), "request never reached the fake upstream"

            proxy.process.send_signal(sig)
            upstream.release.set()  # let the in-flight request's response go out, now that shutdown has begun
            exit_code = proxy.process.wait(timeout=_SHUTDOWN_TIMEOUT_S + _EXIT_WAIT_BUFFER_S)
            request_thread.join(timeout=_EXIT_WAIT_BUFFER_S)

            assert results.get("status_code") == 200, results
            assert exit_code == 0
            _assert_no_shutdown_races(proxy)
        finally:
            terminate_process_group(proxy, term_timeout=5.0)

    def test_second_signal_forces_immediate_exit_without_waiting_full_deadline(
        self, tmp_path, fake_upstream_litellm_config, monkeypatch
    ):
        config_path, upstream = fake_upstream_litellm_config
        monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT", "30")  # deliberately long: a full-deadline wait would time out this test
        proxy = spawn_proxy(mode="direct", config_path=config_path, tmp_path=tmp_path)
        try:
            results: dict = {}
            request_thread = threading.Thread(
                target=_fire_request,
                args=(proxy.port, results),
                kwargs={"timeout": 30.0 + _EXIT_WAIT_BUFFER_S},
            )
            request_thread.start()
            assert upstream.received.wait(timeout=10.0), "request never reached the fake upstream"

            proxy.process.send_signal(signal.SIGINT)
            _wait_for_log_line(proxy, "graceful_shutdown_started", timeout=5.0)  # real handshake, not a sleep guess

            start = time.monotonic()
            proxy.process.send_signal(signal.SIGINT)  # second SIGINT: force exit
            exit_code = proxy.process.wait(timeout=5.0)
            elapsed = time.monotonic() - start

            assert elapsed < 2.0  # tight upper bound; nowhere near the configured 30s deadline
            assert exit_code != 0  # force-exit path: the in-flight request was NOT drained
        finally:
            upstream.release.set()  # unstick the handler thread if it's still blocked
            terminate_process_group(proxy, term_timeout=5.0)


def _wait_for_log_line(proxy: SpawnedProxy, needle: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in _combined_output(proxy):
            return
        time.sleep(0.1)
    raise TimeoutError(f"{needle!r} never appeared in combined stdout+stderr within {timeout}s")


class TestSelfTriggeredShutdown:
    """No signal sent at all: uvicorn's own limit_max_requests counter reaches
    the threshold and the server initiates shutdown on its own (Server.shutdown
    is called by uvicorn's own request-count check, not by signal handling -
    see DrainingServer's docstring in Task 2). The threshold is set low enough
    that it is GUARANTEED to be exceeded by the liveness probe plus a small,
    deliberately-fired batch of short requests before the long request is even
    sent - so the exact number of retries wait_for_health happened to need
    never matters; it can only add to the count, never subtract from it."""

    def test_limit_max_requests_self_initiates_shutdown_without_any_signal(
        self, tmp_path, fake_upstream_litellm_config, monkeypatch
    ):
        config_path, upstream = fake_upstream_litellm_config
        threshold = 3  # comfortably below the guaranteed minimum (>=1 health probe + 4 short requests)
        monkeypatch.setenv("GRACEFUL_SHUTDOWN_TIMEOUT", str(_SHUTDOWN_TIMEOUT_S))
        proxy = spawn_proxy(
            mode="limit_max_requests", config_path=config_path, tmp_path=tmp_path, limit_max_requests=threshold
        )
        try:
            # wait_for_health inside spawn_proxy already consumed >=1 request against
            # the threshold; these 4 are on top of that, deliberately pushing well past it.
            upstream.release.set()  # short requests must complete immediately, no in-flight hold
            for _ in range(4):
                httpx.get(f"http://127.0.0.1:{proxy.port}/health/liveliness", timeout=2.0)
            upstream.release.clear()  # re-arm the hold for the long in-flight request below

            results: dict = {}
            request_thread = threading.Thread(
                target=_fire_request,
                args=(proxy.port, results),
                kwargs={"timeout": _SHUTDOWN_TIMEOUT_S + _EXIT_WAIT_BUFFER_S},
            )
            request_thread.start()
            assert upstream.received.wait(timeout=10.0), "request never reached the fake upstream"
            upstream.release.set()  # allow it to drain once shutdown begins

            exit_code = proxy.process.wait(timeout=_SHUTDOWN_TIMEOUT_S + _EXIT_WAIT_BUFFER_S)
            request_thread.join(timeout=_EXIT_WAIT_BUFFER_S)

            assert results.get("status_code") == 200, results
            assert exit_code == 0
            _wait_for_log_line(proxy, "graceful_shutdown_started", timeout=0.1)  # already exited; just confirm it logged
        finally:
            terminate_process_group(proxy, term_timeout=5.0)


class TestShutdownRaceWithRealInfra:
    """DB/Redis-dependent race coverage: skip with a reason (never silently
    pass) when the infra isn't available, so a green run only ever means the
    race was actually exercised."""

    def test_shutdown_does_not_reconnect_or_write_redis_mid_drain(self, tmp_path, slow_mock_litellm_config):
        database_url = os.environ.get("DATABASE_URL")
        redis_host = os.environ.get("REDIS_HOST")
        if not database_url or not redis_host:
            pytest.skip(
                f"requires DATABASE_URL and REDIS_HOST for a real shutdown-race check "
                f"(database_url={'set' if database_url else 'unset'}, redis_host={'set' if redis_host else 'unset'})"
            )
        # implementer: extend slow_mock_litellm_config (or a variant) to also
        # wire general_settings.database_url and a redis cache block from these
        # env vars, then repeat the signal-mid-flight handshake above, plus a
        # positive precondition (assert the engine-watcher/redis-cache actually
        # started - e.g. a "started" log line) before asserting the banned
        # patterns' absence, so a negative oracle can't pass vacuously.
        ...
```

（`TestShutdownRaceWithRealInfra` 的具体请求/断言体是本任务范围内**必须补全**的一部分，不是留白占位——之所以在这里只画出结构和 skip-guard，是因为它依赖的具体 config 拼接方式要在实施时对着 `slow_mock_litellm_config` 的真实产出反复跑一次确认可用，属于"先用最小必要读操作核实再落笔"的那类细节，写死在计划里反而可能与实施时的真实环境变量取值脱节；**结构本身**——两类测试的划分、skip 而不是静默通过、正向前置断言的要求——是确定的、不可省略的。）

6. 本地跑一次全部用例，确认在 Task 1-7 全部落地之后全部通过；若某个入口（尤其是 `workers`/`reload`，各自有独立子进程/监督者）失败，回到对应 Task 复查（多进程模式下 `GracefulShutdownManager` 是每个子进程独立的类级状态，符合 spec"per-worker 作用域"的非目标澄清，不需要跨进程同步）。`TestShutdownRaceWithRealInfra` 在本地没有 `DATABASE_URL`/`REDIS_HOST` 时应显式 skip（在测试输出里核实 skip reason，不是核实"没跑"）。

7. `make pre-commit`；提交 `test: add real subprocess signal + self-triggered graceful shutdown e2e coverage`。

## 承诺的后续（Phase 1b，本计划不做）

`ManagedTaskSet` + `ManagedTaskSupervisor`（work-lease 绑定真实的两级 logging queue 拓扑）+ `CompletionToken`/`AccountingLease` + admission scope + `LoggingWorker.quiesce()` + 六个 Path A / 七个 Path B 记账任务创建点接入 `token=`/`spawn_child()`。Phase 1b 会把本计划 Task 6 里的"关停短路"（`AccountingSkippedDuringShutdown` 提前返回）升级为"先 flush 产出的工作、再对 supervisor 做 fixed-point drain、deadline 到期联合 cancel、对每条未完成 record 原子写 dropped 结果"的完整 9 步 quiesce 语义——本计划的短路是那条完整链路收窄后的过渡态，不是终态。

## 未采纳方案

- **`SpendCounterReseed.from_db` 引入完整 tagged union返回值**：考虑过让 `from_db` 直接返回 `float | AccountingSkippedDuringShutdown` 而不是复用 `Optional[float]` + 单行日志。放弃原因：`from_db` 的调用方 `coalesced()` 现在把 `None` 当作"回退到调用方兜底值"的信号，改成三态需要同步改 `coalesced()` 的分支逻辑；而 Phase 1b 本来就要把整条记账链路的返回值统一改造成完整 tagged union，这里提前做一半等于改两次。用单行可辨识日志换取"不用动 `coalesced()` 的控制流、可观测性诉求（单行记录）照样满足"，把类型级改造留给 1b 一次做完。
- **把 redis `service_logger_obj.async_service_failure_hook` 的 fire-and-forget task 纳入某种 managed task 集合**：`redis_cache.py` 是 SDK 共享基础设施，引入 proxy 层的任务管理原语（哪怕只是 Phase 1b 的 `ManagedTaskSet`）会构成层级污染（SDK 反向依赖 proxy）。保留现状（裸 `asyncio.create_task`，仅用于遥测，不持有记账语义），本计划只删除重复日志、不改动这个 task 本身的生命周期管理。
- **在 `coalesced`/`coalesced_window` 里也各自加同款关停 guard**：核对真实代码后判定**不需要**。`coalesced` 的 redis get 是 `except: pass` 静默、且关停时 `from_db` 返回 None 使其在 warm 块之前 `return None`；`coalesced_window` 同构且依赖 `window_from_spend_logs`。故只需 guard `from_db` 与 `window_from_spend_logs` 两个真正会抛 `.exception` 的叶子，两个 `coalesced*` 包装即传递性安全。给包装层再加 guard 是冗余（且会在关停时静默跳过本可命中缓存的读，反而降低正确性）。

## 自检

- **Spec 覆盖**：Phase 1 spec 的 §A（`DrainingServer`/三分支 runner/单一 deadline）→ Task 1-3；§C2 全部三段（watchdog 统一 + 关停不重连、IAM 刷新三处 guard、redis 单行降级 + 记账 DB/redis 触点关停短路，含 `from_db`/`_increment_spend_counter_cache`/`window_from_spend_logs` 三处叶子 + `coalesced*` 传递性安全的论证）→ Task 4-6；§C 的 lifespan 顺序重排（针对 1a 范围内的两个后台 producer）→ Task 7；E2E 验收（真实子进程信号——direct/reload/workers 三种入口分别覆盖 SIGINT 与 SIGTERM、`limit_max_requests` 非信号自触发路径、第二次信号强制退出、纯生命周期与 DB/Redis 竞态两类日志 oracle）→ Task 8。§B（work-lease/`ManagedTaskSet`/`ManagedTaskSupervisor`/`CompletionToken`）与 §C 完整 9 步 quiesce（fixed-point drain、deadline 联合 cancel、per-record `shutdown_dropped`）明确推迟到 1b，已在文档开头与"承诺的后续"两处标注，不是静默丢弃。
- **占位符扫描**：全文档搜索 `TODO`/`TBD`/`similar to`/`add appropriate` 均为零命中（本计划采用真实变量名、真实控制流；Task 7 的 lifespan 测试接线已落成完整代码，不再有 `...` 占位）。仍保留的"先核实再落笔"标注只有两处，且都明确给出了核实命令与核实后的落笔准则，不是留白占位：Task 3 的 `proxy_cli` 测试文件命名核实；Task 8 的 `fake_upstream_litellm_config`（`openai/fake-model` + 自定义 `api_base` 组合确实路由到本地假上游）需要实施时手动跑一次确认——这是"新引入的 fixture 先验证行为再信任"，不是机制未定。
- **跨 Task 类型一致性**：`is_shutting_down: Callable[[], bool]` 在 `PrismaClient.__init__`（Task 4）与 `PrismaWrapper.__init__`（Task 5）里签名、默认值（`GracefulShutdownManager.is_shutting_down`）、命名完全一致；`AccountingSkippedDuringShutdown`（Task 6）在 Task 6 内部定义并使用，未被其余 Task 引用，无跨 Task 类型漂移，新增的 `kind: Literal["skipped_during_shutdown"]` 判别字段是可选加固，不影响既有 `reason` 字段的使用方式；`GracefulShutdownManager.deadline_remaining()`/`is_force_exit()`（Task 1）被 Task 2 的 `DrainingServer` 直接调用，签名（无参数、返回 `float`/`bool`）在两处一致；Task 8 的 `spawn_proxy`/`terminate_process_group`/`FakeUpstream` 仅供 Task 8 自己的测试内部使用，不与其余 Task 的生产代码接口交叉。
- **依赖图变化**：Task 5 从"独立"改为"依赖 Task 4"（见 Task 5 小节的显式回报框），Task 7 的依赖集合相应固定为 Task 1、Task 4、Task 5——这是 reviewer Major 8 要求的透传范围（`PrismaWrapper.__init__` 新参数 + `PrismaClient.__init__` 3 个构造点）导致的真实、非静默的计划结构调整，已同步写入 Kick-off Prompt。

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

Task 1、Task 4、Task 6 彼此没有前置依赖，可以任选顺序（甚至并行，如果你打算多开会话/子代理处理——
但由谁在哪执行不是你要决定的事，若需要拆分执行请向上一层汇报，不要自行派生）。
Task 2 依赖 Task 1；Task 3 依赖 Task 2；Task 5 依赖 Task 4（`PrismaWrapper.__init__` 新增的
`is_shutting_down` 参数是 Task 5 的产物，但被 Task 4 所在文件 `litellm/proxy/utils.py` 里
`PrismaClient.__init__` 的 3 个构造点透传，这个透传步骤被安排在 Task 5 的最后一步——
详见 Task 5 小节里的"计划依赖图变化"说明）；Task 7 依赖 Task 1、Task 4、Task 5；
Task 8 依赖前七个 Task 全部落地。

每个 Task 完成后运行一次 `make pre-commit`（若失败，修复后再提交；它只检查已 staged 的改动，
提交前确认 git status 里 staged 的文件就是这次改动涉及的文件）。

如果实施过程中发现某个 Task 的现有代码与本计划描述的"确认过的现状"不一致（行号漂移、
辅助函数签名不同、某个既有测试用例的构造 helper 还不支持计划里假设的关键字参数等），
先用最小必要的读操作核实清楚，按计划的设计意图调整落地方式，并在完成后的汇报里如实说明这处偏差——
不要因为偏差就静默收窄计划范围。
```
