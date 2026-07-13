# 下游 SSE 保活 Implementation Plan

> **状态: 已实现（2026-07-14，本会话内联 TDD，Task 1-11 全绿）。** 偏离项见各 Task 注与 spec 实现说明 / BACKLOG。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 litellm 代理三个下游 SSE 面（anthropic_messages / chat / responses）的真实上游流式路径注入周期性保活字节，防止上游 github_copilot 沉默时下游 Claude Code 客户端因 idle/read 超时断连。

**Architecture:** 单一注入咽喉 `create_response`（三面共享）。两个面：面 1 TTFB 用「延迟提交」（首 chunk / interval / 断连三方竞速，慢则提交 200 并边等边发保活）；面 2 chunk 间隙用 `_sse_keepalive` 组合子（持久 task + `asyncio.wait` 竞速，绝不 cancel producer）。Anthropic native 字节流先经 bytes-safe frame normalizer 规整成完整帧再注入。资源用唯一幂等 `StreamLease` 管理。错误处理下沉 producer 层按 surface 出可识别 error 帧。配置 `stream_keepalive` 走 Override/Resolved 双类型 + `all_litellm_params` 防泄漏。

**Tech Stack:** Python 3.10+（`asyncio.wait`/`wait_for` 版本兼容）、Pydantic v2（frozen + `extra="forbid"`）、httpx/Starlette SSE、pytest + pytest-asyncio。

**Spec:** [docs/superpowers/specs/2026-07-14-downstream-sse-keepalive-design.md](../specs/2026-07-14-downstream-sse-keepalive-design.md)

## Global Constraints

- Python max line length 120（非 88）
- 强类型，无 `Any` / 裸 `dict`；吃 yaml/JSON 用 Pydantic/`TypeAdapter` 边界校验
- no-mutation：不重赋值局部/全局变量；不用可变 list/dict/set 累积，用 comprehension + `tuple()`/`frozenset()`；LIT001/LIT002 触发时函数式重写而非 `# mutable-ok`
- 组合优于继承；never-nester 早返回；失败建模为值（tagged union + match + `assert_never`）不抛
- 依赖注入（clock / interval / strategy / producer 以参数传入），单测传假实现，不 monkeypatch
- 测试须能被 mutate 时失败（>90% kill）；注入 timer/clock 或事件屏障，避免墙钟 `≈interval` 易抖测试
- `tests/test_litellm/` 镜像 `litellm/`；bug 修复扩现有映射测试文件，新特性建新文件按目录命名约定
- 提交前跑测试 + `make pre-commit`（暂存后运行）；违反 `*-budget.json` 的修复后跑 `make lint-budget-update`
- Conventional Commits
- `SSEFrame = str | bytes`：所有新组件签名用此别名，不窄化成 `str`

---

## Phase 0（门禁）：PoC — Claude Code 超时类型

**已在后台以 `gpt-souls:poc-runner` 执行**，产出留 `exp/downstream-keepalive-timeout/`。进入 Phase 1 前必须确认结论为「read/idle 型（保活字节可重置）」。若结论为「固定 wall-clock total 主导」→ 停止,回 spec 重新定性(保活无效)。

- [ ] **Step 0: 确认 PoC 结论**。读 `exp/downstream-keepalive-timeout/conclusions.md`，确认 httpx `read` timeout 是 inactivity 型且 Claude Code 的 timeout 映射到该轴。PASS 才继续。

---

## Task 1: 配置模型 `StreamKeepaliveOverride` / `ResolvedStreamKeepaliveConfig`

**Files:**
- Create: `litellm/proxy/common_utils/stream_keepalive_config.py`
- Test: `tests/test_litellm/proxy/common_utils/test_stream_keepalive_config.py`

**Interfaces:**
- Produces:
  - `StreamKeepaliveOverride(BaseModel, frozen, extra="forbid")`: `enabled: bool | None = None`, `interval: float | None = None`
  - `ResolvedStreamKeepaliveConfig(BaseModel, frozen)`: `enabled: bool`, `interval: float`
  - `parse_override(raw: object) -> StreamKeepaliveOverride`（边界校验，非法抛 `ValidationError`）
  - `merge_overrides(global_o: StreamKeepaliveOverride | None, deployment_o: StreamKeepaliveOverride | None) -> StreamKeepaliveOverride`（按 `model_fields_set`：deployment 显式设置的字段覆盖 global）
  - `resolve(merged: StreamKeepaliveOverride) -> ResolvedStreamKeepaliveConfig`（末端加默认 `enabled=True`/`interval=15.0`）
  - 常量 `KEEPALIVE_MIN_INTERVAL_SECONDS = 1.0`, `KEEPALIVE_DEFAULT_INTERVAL_SECONDS = 15.0`

- [ ] **Step 1: 写失败测试**

```python
import math
import pytest
from pydantic import ValidationError
from litellm.proxy.common_utils.stream_keepalive_config import (
    StreamKeepaliveOverride, ResolvedStreamKeepaliveConfig,
    parse_override, merge_overrides, resolve,
    KEEPALIVE_DEFAULT_INTERVAL_SECONDS,
)

def test_parse_rejects_unknown_key():
    with pytest.raises(ValidationError):
        parse_override({"enabled": True, "interbal": 10})  # typo -> extra=forbid

@pytest.mark.parametrize("bad", [0, -5, math.inf, math.nan, 0.5])  # <=0, inf, nan, <min
def test_parse_rejects_bad_interval(bad):
    with pytest.raises(ValidationError):
        parse_override({"interval": bad})

def test_partial_override_does_not_reset_global_interval():
    # global interval=5; deployment only sets enabled=false -> interval stays 5
    g = parse_override({"interval": 5})
    d = parse_override({"enabled": False})
    merged = merge_overrides(g, d)
    resolved = resolve(merged)
    assert resolved.enabled is False
    assert resolved.interval == 5

def test_resolve_defaults_when_unset():
    resolved = resolve(merge_overrides(None, None))
    assert resolved.enabled is True
    assert resolved.interval == KEEPALIVE_DEFAULT_INTERVAL_SECONDS

def test_deployment_overrides_global_field():
    g = parse_override({"enabled": True, "interval": 5})
    d = parse_override({"interval": 20})
    assert resolve(merge_overrides(g, d)).interval == 20
```

- [ ] **Step 2: 运行确认失败**。Run: `pytest tests/test_litellm/proxy/common_utils/test_stream_keepalive_config.py -v` → FAIL（模块不存在）

- [ ] **Step 3: 实现**

```python
"""Downstream SSE keepalive config — Override (partial, all-optional) vs Resolved."""
from __future__ import annotations

import math
from pydantic import BaseModel, ConfigDict, field_validator

KEEPALIVE_MIN_INTERVAL_SECONDS = 1.0
KEEPALIVE_DEFAULT_INTERVAL_SECONDS = 15.0


class StreamKeepaliveOverride(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    enabled: bool | None = None
    interval: float | None = None

    @field_validator("interval")
    @classmethod
    def _validate_interval(cls, v: float | None) -> float | None:
        if v is None:
            return v
        if not math.isfinite(v) or v < KEEPALIVE_MIN_INTERVAL_SECONDS:
            raise ValueError(f"interval must be finite and >= {KEEPALIVE_MIN_INTERVAL_SECONDS}")
        return v


class ResolvedStreamKeepaliveConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    enabled: bool
    interval: float


def parse_override(raw: object) -> StreamKeepaliveOverride:
    if isinstance(raw, StreamKeepaliveOverride):
        return raw
    return StreamKeepaliveOverride.model_validate(raw)


def merge_overrides(
    global_o: StreamKeepaliveOverride | None,
    deployment_o: StreamKeepaliveOverride | None,
) -> StreamKeepaliveOverride:
    g = global_o or StreamKeepaliveOverride()
    d = deployment_o or StreamKeepaliveOverride()
    # deployment 显式设置的字段覆盖 global（按 model_fields_set）
    enabled = d.enabled if "enabled" in d.model_fields_set else g.enabled
    interval = d.interval if "interval" in d.model_fields_set else g.interval
    return StreamKeepaliveOverride(enabled=enabled, interval=interval)


def resolve(merged: StreamKeepaliveOverride) -> ResolvedStreamKeepaliveConfig:
    return ResolvedStreamKeepaliveConfig(
        enabled=merged.enabled if merged.enabled is not None else True,
        interval=merged.interval if merged.interval is not None else KEEPALIVE_DEFAULT_INTERVAL_SECONDS,
    )
```

- [ ] **Step 4: 运行确认通过**。Run: `pytest tests/test_litellm/proxy/common_utils/test_stream_keepalive_config.py -v` → PASS

- [ ] **Step 5: 提交**

```bash
git add litellm/proxy/common_utils/stream_keepalive_config.py tests/test_litellm/proxy/common_utils/test_stream_keepalive_config.py
git commit -m "feat(keepalive): stream_keepalive Override/Resolved config models"
```

---

## Task 2: 保活策略（两阶段）+ `SSEFrame` 类型

**Files:**
- Create: `litellm/proxy/common_utils/sse_keepalive.py`（本文件后续任务继续扩充）
- Test: `tests/test_litellm/proxy/common_utils/test_sse_keepalive_strategy.py`

**Interfaces:**
- Consumes: `ResolvedStreamKeepaliveConfig`（Task 1）
- Produces:
  - `SSEFrame = str | bytes`（module-level type alias）
  - `KEEPALIVE_COMMENT = ": ping\n\n"`, `ANTHROPIC_PING_EVENT = 'event: ping\ndata: {"type": "ping"}\n\n'`
  - `KeepaliveStrategy`（frozen dataclass，slots）：`idle_frames(seen_message_start: bool) -> tuple[str, ...]`、`observe_advances_to_phase2(frame: SSEFrame) -> bool`
  - `CommentOnlyKeepaliveStrategy(KeepaliveStrategy)`：两阶段都仅 `(KEEPALIVE_COMMENT,)`；`observe_advances_to_phase2` 恒 `False`
  - `AnthropicKeepaliveStrategy(KeepaliveStrategy)`：阶段1 `(KEEPALIVE_COMMENT,)`；阶段2 `(KEEPALIVE_COMMENT, ANTHROPIC_PING_EVENT)`；`observe_advances_to_phase2` 检测帧是否为 `message_start` 事件
  - `frame_is_anthropic_message_start(frame: SSEFrame) -> bool`（解码 bytes/str，判 `event: message_start`）

- [ ] **Step 1: 写失败测试**

```python
from litellm.proxy.common_utils.sse_keepalive import (
    KEEPALIVE_COMMENT, ANTHROPIC_PING_EVENT,
    CommentOnlyKeepaliveStrategy, AnthropicKeepaliveStrategy,
    frame_is_anthropic_message_start,
)

def test_comment_only_both_phases():
    s = CommentOnlyKeepaliveStrategy()
    assert s.idle_frames(seen_message_start=False) == (KEEPALIVE_COMMENT,)
    assert s.idle_frames(seen_message_start=True) == (KEEPALIVE_COMMENT,)
    assert s.observe_advances_to_phase2(b"event: message_start\ndata: {}\n\n") is False

def test_anthropic_phase1_comment_only_phase2_adds_ping():
    s = AnthropicKeepaliveStrategy()
    assert s.idle_frames(seen_message_start=False) == (KEEPALIVE_COMMENT,)
    assert s.idle_frames(seen_message_start=True) == (KEEPALIVE_COMMENT, ANTHROPIC_PING_EVENT)

@pytest.mark.parametrize("frame,expected", [
    (b'event: message_start\ndata: {"type":"message_start"}\n\n', True),
    ('event: message_start\ndata: {}\n\n', True),
    (b'event: content_block_delta\ndata: {}\n\n', False),
    (b'event: ping\ndata: {"type":"ping"}\n\n', False),
])
def test_frame_is_message_start(frame, expected):
    assert frame_is_anthropic_message_start(frame) is expected
```
（`import pytest` 顶部补上。）

- [ ] **Step 2: 运行确认失败** → FAIL（模块/符号不存在）

- [ ] **Step 3: 实现**

```python
"""Downstream SSE keepalive: frame type, strategies (two-phase, no mutable flag)."""
from __future__ import annotations

from dataclasses import dataclass

SSEFrame = str | bytes

KEEPALIVE_COMMENT = ": ping\n\n"
ANTHROPIC_PING_EVENT = 'event: ping\ndata: {"type": "ping"}\n\n'


def _as_text(frame: SSEFrame) -> str:
    return frame.decode("utf-8", errors="replace") if isinstance(frame, (bytes, bytearray)) else frame


def frame_is_anthropic_message_start(frame: SSEFrame) -> bool:
    text = _as_text(frame)
    return any(line.strip() == "event: message_start" for line in text.split("\n"))


@dataclass(frozen=True, slots=True)
class KeepaliveStrategy:
    def idle_frames(self, seen_message_start: bool) -> tuple[str, ...]:
        return (KEEPALIVE_COMMENT,)

    def observe_advances_to_phase2(self, frame: SSEFrame) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class CommentOnlyKeepaliveStrategy(KeepaliveStrategy):
    pass


@dataclass(frozen=True, slots=True)
class AnthropicKeepaliveStrategy(KeepaliveStrategy):
    def idle_frames(self, seen_message_start: bool) -> tuple[str, ...]:
        if seen_message_start:
            return (KEEPALIVE_COMMENT, ANTHROPIC_PING_EVENT)
        return (KEEPALIVE_COMMENT,)

    def observe_advances_to_phase2(self, frame: SSEFrame) -> bool:
        return frame_is_anthropic_message_start(frame)
```

- [ ] **Step 4: 运行确认通过** → PASS

- [ ] **Step 5: 提交**

```bash
git add litellm/proxy/common_utils/sse_keepalive.py tests/test_litellm/proxy/common_utils/test_sse_keepalive_strategy.py
git commit -m "feat(keepalive): SSEFrame type + two-phase keepalive strategies"
```

---

## Task 3: SSE delimiter 共享 helper + bytes-safe frame normalizer

**Files:**
- Create: `litellm/proxy/common_utils/sse_frame_normalizer.py`
- Modify: `litellm/proxy/proxy_server.py:6902-6915`（抽取 delimiter 查找为共享 helper 并改调用点复用，不改行为）
- Test: `tests/test_litellm/proxy/common_utils/test_sse_frame_normalizer.py`

**Interfaces:**
- Produces:
  - `SSE_FRAME_DELIMITERS: tuple[bytes, ...] = (b"\r\n\r\n", b"\n\n", b"\r\r")`
  - `find_frame_delimiter(buf: bytes) -> int`（返回**首个**完整帧末尾的 exclusive 索引，含分隔符；无则 `-1`；多分隔符取最靠前的完整帧）
  - `async def normalize_anthropic_sse_frames(byte_iter: AsyncIterator[bytes], max_unterminated_bytes: int = 1_048_576) -> AsyncIterator[bytes]`：累积字节、按完整帧 yield（含分隔符），末端非空残片在 EOF 原样 yield 并 debug 日志；超 `max_unterminated_bytes` 抛 `ValueError`

**Design notes:**
- 只在 `bytes` 上找 ASCII 分隔符，**不**逐 chunk `decode(errors="replace")`（跨 chunk UTF-8 会破坏字节等价）
- 多分隔符共存时取产生**最短完整帧**的那个（最靠前边界），避免把两帧粘一起

- [ ] **Step 1: 写失败测试**

```python
import logging
import pytest
from litellm.proxy.common_utils.sse_frame_normalizer import (
    find_frame_delimiter, normalize_anthropic_sse_frames, SSE_FRAME_DELIMITERS,
)

async def _aiter(chunks):
    for c in chunks:
        yield c

@pytest.mark.parametrize("buf,expected_end", [
    (b"event: ping\n\n", len(b"event: ping\n\n")),
    (b"a\r\n\r\nb", len(b"a\r\n\r\n")),
    (b"a\r\rb", len(b"a\r\r")),
    (b"no delimiter yet", -1),
])
def test_find_frame_delimiter(buf, expected_end):
    assert find_frame_delimiter(buf) == expected_end

@pytest.mark.asyncio
async def test_reassembles_frame_split_mid_json():
    # 上游把一帧切在 JSON 中间
    chunks = [b'data: {"text":"hel', b'lo"}\n\n']
    out = [f async for f in normalize_anthropic_sse_frames(_aiter(chunks))]
    assert out == [b'data: {"text":"hello"}\n\n']

@pytest.mark.asyncio
async def test_preserves_multibyte_utf8_split_across_chunks():
    emoji = "🎉".encode("utf-8")  # 4 bytes
    chunks = [b"data: " + emoji[:2], emoji[2:] + b"\n\n"]
    out = b"".join([f async for f in normalize_anthropic_sse_frames(_aiter(chunks))])
    assert out == b"data: " + emoji + b"\n\n"  # 字节等价，无 replacement char
    assert b"\xef\xbf\xbd" not in out  # U+FFFD 不出现

@pytest.mark.asyncio
async def test_two_frames_in_one_chunk_not_glued():
    chunks = [b"event: a\n\nevent: b\n\n"]
    out = [f async for f in normalize_anthropic_sse_frames(_aiter(chunks))]
    assert out == [b"event: a\n\n", b"event: b\n\n"]

@pytest.mark.asyncio
async def test_eof_残片_原样下发_and_logs(caplog):
    chunks = [b"data: trailing-no-delim"]
    with caplog.at_level(logging.DEBUG):
        out = [f async for f in normalize_anthropic_sse_frames(_aiter(chunks))]
    assert out == [b"data: trailing-no-delim"]

@pytest.mark.asyncio
async def test_unterminated_over_limit_raises():
    chunks = [b"x" * 10]
    with pytest.raises(ValueError):
        [f async for f in normalize_anthropic_sse_frames(_aiter(chunks), max_unterminated_bytes=4)]
```

- [ ] **Step 2: 运行确认失败** → FAIL

- [ ] **Step 3: 实现 normalizer**（no-mutation：用 `bytes` 局部累加经由生成器 yield 后重置为切片，避免可变 buffer；下例用尾递归式 while + 单一 `buffer` 名字的重新绑定属于流式必要状态，若 lint 允许则保留，否则封装成显式状态推进函数）

```python
"""Bytes-safe SSE frame normalizer for Anthropic passthrough (no per-chunk decode)."""
from __future__ import annotations

import logging
from typing import AsyncIterator

verbose = logging.getLogger("litellm.proxy")

SSE_FRAME_DELIMITERS: tuple[bytes, ...] = (b"\r\n\r\n", b"\n\n", b"\r\r")


def find_frame_delimiter(buf: bytes) -> int:
    ends = tuple(
        idx + len(d)
        for d in SSE_FRAME_DELIMITERS
        if (idx := buf.find(d)) != -1
    )
    return min(ends) if ends else -1


async def normalize_anthropic_sse_frames(
    byte_iter: AsyncIterator[bytes],
    max_unterminated_bytes: int = 1_048_576,
) -> AsyncIterator[bytes]:
    buffer = b""
    async for chunk in byte_iter:
        buffer += chunk
        end = find_frame_delimiter(buffer)
        while end != -1:
            yield buffer[:end]
            buffer = buffer[end:]
            end = find_frame_delimiter(buffer)
        if len(buffer) > max_unterminated_bytes:
            raise ValueError(
                f"SSE frame exceeded {max_unterminated_bytes} bytes without delimiter"
            )
    if buffer:
        verbose.debug("normalize_anthropic_sse_frames: flushing %d trailing bytes at EOF", len(buffer))
        yield buffer
```

> no-mutation 说明：`buffer` 的重新绑定是流式规范化不可避免的演进状态。若 LIT/type-discipline 拦截，改写为 `while` 循环调用纯函数 `advance(buffer) -> tuple[list_of_frames, remainder]` 并用 `tuple` 承载 frames；实现者按 lint 结果二选一，禁止 `# mutable-ok` 兜底除非确证无法重写。

- [ ] **Step 4: 抽取共享 delimiter helper**。读 `proxy_server.py:6902-6915`，把其 delimiter 查找逻辑替换为调用 `find_frame_delimiter`（保持原行为；若原逻辑语义不同则新增 helper 不改旧调用点，改为在 normalizer 内自用）。为该抽取补一条 characterization 测试保证 `proxy_server` 旧路径行为不变。

- [ ] **Step 5: 运行确认通过** → PASS。Run: `pytest tests/test_litellm/proxy/common_utils/test_sse_frame_normalizer.py -v`

- [ ] **Step 6: 提交**

```bash
git add litellm/proxy/common_utils/sse_frame_normalizer.py litellm/proxy/proxy_server.py tests/test_litellm/proxy/common_utils/test_sse_frame_normalizer.py
git commit -m "feat(keepalive): bytes-safe SSE frame normalizer + shared delimiter helper"
```

---

## Task 4: `StreamLease` — 唯一幂等资源 owner

**Files:**
- Modify: `litellm/proxy/common_utils/sse_keepalive.py`（追加）
- Test: `tests/test_litellm/proxy/common_utils/test_stream_lease.py`

**Interfaces:**
- Produces:
  - `StreamLease`：持有可选 `pending_task: asyncio.Task | None` + `inner: AsyncGenerator[SSEFrame, None]`；`async def close() -> None`（幂等，顺序 `cancel → shielded await → inner.aclose()`）
  - 构造：`StreamLease(inner, pending_task=None)`

- [ ] **Step 1: 写失败测试**

```python
import asyncio
import pytest
from litellm.proxy.common_utils.sse_keepalive import StreamLease

class _RecordingGen:
    def __init__(self): self.aclose_calls = 0
    def __aiter__(self): return self
    async def __anext__(self):
        await asyncio.sleep(3600)  # 永不产出
    async def aclose(self): self.aclose_calls += 1

@pytest.mark.asyncio
async def test_close_is_idempotent_single_upstream_close():
    gen = _RecordingGen()
    task = asyncio.ensure_future(gen.__anext__())
    lease = StreamLease(inner=gen, pending_task=task)
    await asyncio.gather(lease.close(), lease.close())  # 并发两次
    assert gen.aclose_calls == 1
    assert task.cancelled() or task.done()
    # 无孤儿任务
    await asyncio.sleep(0)
    assert task not in asyncio.all_tasks()

@pytest.mark.asyncio
async def test_close_cancels_before_aclose_order():
    order = []
    class G:
        def __aiter__(self): return self
        async def __anext__(self):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                order.append("cancelled"); raise
        async def aclose(self): order.append("aclosed")
    g = G(); task = asyncio.ensure_future(g.__anext__())
    await asyncio.sleep(0)
    await StreamLease(inner=g, pending_task=task).close()
    assert order == ["cancelled", "aclosed"]
```

- [ ] **Step 2: 运行确认失败** → FAIL

- [ ] **Step 3: 实现**

```python
# 追加到 sse_keepalive.py
import asyncio
from typing import AsyncGenerator, Optional


class StreamLease:
    """唯一、幂等的响应级资源 owner：pending __anext__ task + 最内层 producer。"""

    def __init__(
        self,
        inner: AsyncGenerator[SSEFrame, None],
        pending_task: Optional["asyncio.Task[SSEFrame]"] = None,
    ) -> None:
        self._inner = inner
        self._pending_task = pending_task
        self._closed = False

    def set_pending_task(self, task: "asyncio.Task[SSEFrame]") -> None:
        self._pending_task = task

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = self._pending_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.shield(task)
            except BaseException:  # noqa: BLE001 - 吞取消/异常，仅确保取消已送达
                pass
        try:
            await self._inner.aclose()
        except BaseException:  # noqa: BLE001
            pass
```

> `_closed` 是幂等闭包必要状态；这是资源生命周期对象，不是数据结构，`# mutable-ok` 不适用（LIT 针对集合，非布尔 flag）。

- [ ] **Step 4: 运行确认通过** → PASS

- [ ] **Step 5: 提交**

```bash
git add litellm/proxy/common_utils/sse_keepalive.py tests/test_litellm/proxy/common_utils/test_stream_lease.py
git commit -m "feat(keepalive): idempotent StreamLease resource owner"
```

---

## Task 5: `_sse_keepalive` 组合子（面 2 + 持久 task + 同轮优先级）

**Files:**
- Modify: `litellm/proxy/common_utils/sse_keepalive.py`（追加）
- Test: `tests/test_litellm/proxy/common_utils/test_sse_keepalive_combinator.py`

**Interfaces:**
- Consumes: `KeepaliveStrategy`（Task 2）、`StreamLease`（Task 4）、`ResolvedStreamKeepaliveConfig`（Task 1）
- Produces:
  - `async def sse_keepalive(real_frames: AsyncGenerator[SSEFrame, None], strategy: KeepaliveStrategy, interval: float, lease: StreamLease) -> AsyncGenerator[SSEFrame, None]`
  - 语义：每次 `real_frames.__anext__()` 作持久 task（存入 lease），`asyncio.wait({task}, timeout=interval)`；超时 → yield `strategy.idle_frames(seen_message_start)`（逐帧）；task done → **先消费真实帧**（同轮优先级：不因超时同轮而插 ping）；消费后据 `strategy.observe_advances_to_phase2(frame)` 推进阶段（阶段 2 从下一次等待生效，message_start 帧本身不触发 ping）；`StopAsyncIteration` 结束；close 传播走 `lease.close()`

- [ ] **Step 1: 写失败测试**（注入慢/快 fake gen + 手控事件，避免墙钟抖动）

```python
import asyncio
import pytest
from litellm.proxy.common_utils.sse_keepalive import (
    sse_keepalive, StreamLease, CommentOnlyKeepaliveStrategy, AnthropicKeepaliveStrategy,
    KEEPALIVE_COMMENT, ANTHROPIC_PING_EVENT,
)

async def _gen(items, gate: asyncio.Event | None = None):
    for it in items:
        if gate is not None:
            await gate.wait()
        yield it

@pytest.mark.asyncio
async def test_idle_emits_comment_then_forwards_real_frame():
    gate = asyncio.Event()
    real = _gen([b"data: real\n\n"], gate)
    lease = StreamLease(inner=real)
    out = []
    async def drive():
        async for f in sse_keepalive(real, CommentOnlyKeepaliveStrategy(), interval=0.05, lease=lease):
            out.append(f)
    task = asyncio.ensure_future(drive())
    await asyncio.sleep(0.16)      # ~3 个 interval 无真实帧
    gate.set()                     # 放行真实帧
    await task
    assert out.count(KEEPALIVE_COMMENT) >= 2      # 发过保活注释
    assert out[-1] == b"data: real\n\n"           # 真实帧在最后

@pytest.mark.asyncio
async def test_fast_stream_no_keepalive():
    real = _gen([b"a\n\n", b"b\n\n"])
    lease = StreamLease(inner=real)
    out = [f async for f in sse_keepalive(real, CommentOnlyKeepaliveStrategy(), interval=10, lease=lease)]
    assert out == [b"a\n\n", b"b\n\n"]            # 零保活帧

@pytest.mark.asyncio
async def test_producer_not_cancelled_across_pings():
    # producer 每帧前等待，验证 ping 后同一 task 结果仍被转发（未被 wait_for 取消）
    gate = asyncio.Event()
    real = _gen([b"slow\n\n"], gate)
    lease = StreamLease(inner=real)
    out = []
    async def drive():
        async for f in sse_keepalive(real, CommentOnlyKeepaliveStrategy(), interval=0.03, lease=lease):
            out.append(f)
    t = asyncio.ensure_future(drive())
    await asyncio.sleep(0.1); gate.set(); await t
    assert b"slow\n\n" in out                     # 若被取消则永远收不到

@pytest.mark.asyncio
async def test_anthropic_phase2_only_after_message_start_and_no_ping_on_that_frame():
    gate2 = asyncio.Event()
    real = _gen([b"event: message_start\ndata: {}\n\n", b"event: content_block_delta\ndata: {}\n\n"], None)
    # 用两段：message_start 立即到，之后 idle
    async def staged():
        yield b"event: message_start\ndata: {}\n\n"
        await gate2.wait()
        yield b"event: content_block_delta\ndata: {}\n\n"
    g = staged(); lease = StreamLease(inner=g)
    out = []
    async def drive():
        async for f in sse_keepalive(g, AnthropicKeepaliveStrategy(), interval=0.04, lease=lease):
            out.append(f)
    t = asyncio.ensure_future(drive())
    await asyncio.sleep(0.12); gate2.set(); await t
    # message_start 与其后帧之间的 idle 期应出现 comment+ping（阶段2）
    assert ANTHROPIC_PING_EVENT in out
    # message_start 帧本身之前无 ping（阶段1只可能有 comment，且它立即到达无 idle）
    idx_ms = out.index(b"event: message_start\ndata: {}\n\n")
    assert ANTHROPIC_PING_EVENT not in out[:idx_ms]
```

- [ ] **Step 2: 运行确认失败** → FAIL

- [ ] **Step 3: 实现**（never-nester，持久 task，`asyncio.wait` 非 `wait_for`）

```python
# 追加到 sse_keepalive.py
async def sse_keepalive(
    real_frames: "AsyncGenerator[SSEFrame, None]",
    strategy: KeepaliveStrategy,
    interval: float,
    lease: StreamLease,
) -> "AsyncGenerator[SSEFrame, None]":
    seen_message_start = False
    while True:
        task: "asyncio.Task[SSEFrame]" = asyncio.ensure_future(real_frames.__anext__())
        lease.set_pending_task(task)
        done, _ = await asyncio.wait({task}, timeout=interval)
        if not done:
            for frame in strategy.idle_frames(seen_message_start):
                yield frame
            continue
        try:
            frame = task.result()
        except StopAsyncIteration:
            return
        yield frame
        if not seen_message_start and strategy.observe_advances_to_phase2(frame):
            seen_message_start = True
```

> `seen_message_start` / `task` 的重新绑定是流式循环固有状态。若 no-mutation lint 拦截，改用递归 helper 传阶段参数；实现者按 lint 结果处理。异常（非 `StopAsyncIteration`）从 `task.result()` 抛出，由 Task 8 的 producer 层错误契约在上游已转成 error 帧，故此处正常不会见到裸异常；防御性：其它异常向上传播由 `create_response` 层 `lease.close()` 兜底。

- [ ] **Step 4: 运行确认通过** → PASS

- [ ] **Step 5: 提交**

```bash
git add litellm/proxy/common_utils/sse_keepalive.py tests/test_litellm/proxy/common_utils/test_sse_keepalive_combinator.py
git commit -m "feat(keepalive): sse_keepalive combinator (persistent task, same-tick priority, two-phase)"
```

---

## Task 6: `DownstreamSSESurface` 枚举 + strategy 工厂

**Files:**
- Modify: `litellm/proxy/common_utils/sse_keepalive.py`（追加）
- Test: `tests/test_litellm/proxy/common_utils/test_sse_keepalive_strategy.py`（扩充）

**Interfaces:**
- Produces:
  - `class DownstreamSSESurface(str, Enum)`: `ANTHROPIC = "anthropic"`, `OPENAI_CHAT = "openai_chat"`, `OPENAI_RESPONSES = "openai_responses"`
  - `strategy_for(surface: DownstreamSSESurface) -> KeepaliveStrategy`（match + `assert_never`）
  - `needs_frame_normalizer(surface: DownstreamSSESurface) -> bool`（仅 `ANTHROPIC` True）

- [ ] **Step 1: 写失败测试**

```python
from litellm.proxy.common_utils.sse_keepalive import (
    DownstreamSSESurface, strategy_for, needs_frame_normalizer,
    AnthropicKeepaliveStrategy, CommentOnlyKeepaliveStrategy,
)

def test_strategy_for_surface():
    assert isinstance(strategy_for(DownstreamSSESurface.ANTHROPIC), AnthropicKeepaliveStrategy)
    assert isinstance(strategy_for(DownstreamSSESurface.OPENAI_CHAT), CommentOnlyKeepaliveStrategy)
    assert isinstance(strategy_for(DownstreamSSESurface.OPENAI_RESPONSES), CommentOnlyKeepaliveStrategy)

def test_only_anthropic_needs_normalizer():
    assert needs_frame_normalizer(DownstreamSSESurface.ANTHROPIC) is True
    assert needs_frame_normalizer(DownstreamSSESurface.OPENAI_CHAT) is False
    assert needs_frame_normalizer(DownstreamSSESurface.OPENAI_RESPONSES) is False
```

- [ ] **Step 2: 运行确认失败** → FAIL

- [ ] **Step 3: 实现**

```python
# 追加到 sse_keepalive.py
from enum import Enum
from typing import assert_never


class DownstreamSSESurface(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI_CHAT = "openai_chat"
    OPENAI_RESPONSES = "openai_responses"


def strategy_for(surface: DownstreamSSESurface) -> KeepaliveStrategy:
    match surface:
        case DownstreamSSESurface.ANTHROPIC:
            return AnthropicKeepaliveStrategy()
        case DownstreamSSESurface.OPENAI_CHAT | DownstreamSSESurface.OPENAI_RESPONSES:
            return CommentOnlyKeepaliveStrategy()
    assert_never(surface)


def needs_frame_normalizer(surface: DownstreamSSESurface) -> bool:
    return surface is DownstreamSSESurface.ANTHROPIC
```

- [ ] **Step 4: 运行确认通过** → PASS

- [ ] **Step 5: 提交**

```bash
git add litellm/proxy/common_utils/sse_keepalive.py tests/test_litellm/proxy/common_utils/test_sse_keepalive_strategy.py
git commit -m "feat(keepalive): DownstreamSSESurface enum + strategy factory"
```

---

## Task 7: 配置注册 + `all_litellm_params` 防泄漏 + Router pop

**Files:**
- Modify: `litellm/types/router.py`（`GenericLiteLLMParams` + `LiteLLMParamsTypedDict` 加 `stream_keepalive`）
- Modify: `litellm/types/utils.py:3054-3074`（`all_litellm_params` 加 `"stream_keepalive"`）
- Modify: `litellm/router.py:1633-1677`、`:2657-2694`、`:4341-4372`（三处 pop `stream_keepalive` 出 provider-visible copy）
- Test: `tests/test_litellm/router/test_stream_keepalive_no_leak.py`

**Interfaces:**
- Consumes: `StreamKeepaliveOverride`（Task 1）
- Produces: 保证 `stream_keepalive` 不进上游 body；选定 deployment 的 override 可从 `litellm_params` 取回

- [ ] **Step 1: 写失败测试**（mock transport 抓上游 body，三面各一）

```python
# 断言：配了 litellm_params.stream_keepalive 的 deployment，上游 body 不含该键
# 用现有 router wire-capture 模式（参考同目录其它 no-leak 测试）
def test_stream_keepalive_not_in_upstream_body_chat(mock_upstream_capture):
    ...  # 构造 github_copilot chat deployment + stream_keepalive，断 captured_body 无 "stream_keepalive"
def test_stream_keepalive_not_in_upstream_body_messages(mock_upstream_capture): ...
def test_stream_keepalive_not_in_upstream_body_responses(mock_upstream_capture): ...
def test_all_litellm_params_contains_stream_keepalive():
    from litellm.types.utils import all_litellm_params
    assert "stream_keepalive" in all_litellm_params
```
（实现者按仓库既有 wire-capture fixture 补全三条；参照 upstream http_client spec 的 wire-body 测试写法。）

- [ ] **Step 2: 运行确认失败** → FAIL

- [ ] **Step 3: 实现**。`GenericLiteLLMParams` 加 `stream_keepalive: StreamKeepaliveOverride | None = None`；`LiteLLMParamsTypedDict` 同步；`all_litellm_params` 追加 `"stream_keepalive"`；Router 三处在构造 provider-visible kwargs 时 `pop("stream_keepalive", None)`（保留原 deployment 对象上的值供 proxy 层取回）。

- [ ] **Step 4: 运行确认通过** → PASS

- [ ] **Step 5: 提交**

```bash
git add litellm/types/router.py litellm/types/utils.py litellm/router.py tests/test_litellm/router/test_stream_keepalive_no_leak.py
git commit -m "feat(keepalive): register stream_keepalive param + prevent upstream leak"
```

---

## Task 8: producer 层错误契约（failure-once，按 surface 出 error 帧）

**Files:**
- Modify: `litellm/proxy/common_request_processing.py:2460-2585`（`async_streaming_data_generator`：加 `committed` 感知 + surface error serializer）
- Modify: `litellm/proxy/common_request_processing.py:2603-2611`（anthropic error serializer 改 `event: error`）
- Test: `tests/test_litellm/proxy/test_common_request_processing.py`（扩现有文件）

**Interfaces:**
- Consumes: `DownstreamSSESurface`
- Produces: producer 在**已提交**时把 `HTTPException` 与普通异常都序列化为 surface 可识别 error 帧（不 re-raise）；typed outcome 标记 `failure_recorded`；未提交时保持现行 re-raise（供 `create_response` 转 JSON）

**Design notes:**
- anthropic error 帧：`event: error\ndata: {"type":"error","error":{...}}\n\n`
- chat：`data: {"error":...}\n\n`（不加 `[DONE]`，与现状一致）
- responses：其 iterator failure 形态；typed error 需补 `message`/`sequence_number`
- `committed: bool` 由 `create_response` 传入 producer（Task 9 接线）；默认 `False` 保持现行为

- [ ] **Step 1: 写失败测试**（三类来源 × 每 surface × failure hook 恰一次）

```python
# committed=True 时：
# - producer 内抛 HTTPException -> yield anthropic event: error 帧、不 re-raise、post_call_failure_hook 调一次
# - 普通异常 -> 同上
# - producer 已 yield 一帧 error（普通异常路径）-> 不再被外层重复处理
# committed=False 时：HTTPException 仍 re-raise（回归）
```
（实现者按现有 `test_common_request_processing.py` 的 async generator 驱动模式补全；注入假 `proxy_logging_obj` 计数 hook 调用。）

- [ ] **Step 2: 运行确认失败** → FAIL

- [ ] **Step 3: 实现**。给 `async_streaming_data_generator` 加 `committed: bool` 与 `surface: DownstreamSSESurface | None` 参数；`except HTTPException` 分支：`if committed and surface is not None: 调 hook 一次 → yield surface_error_frame → return`，否则保持 `raise`；普通异常分支已 yield 一帧，确认只调一次 hook。anthropic serializer 改产 `event: error`。

- [ ] **Step 4: 运行确认通过** → PASS

- [ ] **Step 5: 提交**

```bash
git add litellm/proxy/common_request_processing.py tests/test_litellm/proxy/test_common_request_processing.py
git commit -m "fix(keepalive): producer-layer per-surface error contract (failure-once, event: error)"
```

---

## Task 9: `create_response` 三方竞速 + 慢路径提交 + lease 接线

**Files:**
- Modify: `litellm/proxy/common_request_processing.py:354-548`（`_buffer_first_chunk_honoring_disconnect` → 三方竞速返回 `FirstChunkRace`；`create_response` 新增 `keepalive`/`surface` 参数、慢路径提交、lease、real-frame 管线、`_sse_keepalive` 包裹）
- Test: `tests/test_litellm/proxy/test_common_request_processing.py`（扩）

**Interfaces:**
- Consumes: Task 1/2/4/5/6/8 全部
- Produces: `create_response(..., keepalive: ResolvedStreamKeepaliveConfig | None = None, surface: DownstreamSSESurface | None = None)`；`None` → 完全走原路径（字节级不变）

**Design notes（层次冻结）:**
```
producer(committed-aware, surface error) → [anthropic: normalizer] → real-frame gen(DD span/first-frame) → sse_keepalive → Starlette
```
- `FirstChunkRace = Disconnected | FirstChunk(frame) | SlowCommit(lease)`；优先级 `disconnect > 已完成首 chunk > timer`
- 快路径（`FirstChunk`）保持现行：错误首帧转 JSON、正常首帧走管线
- 慢路径（`SlowCommit`）：先 cancel+await 旧 disconnect watcher；返回 `_UpstreamClosingStreamingResponse`，body 先 yield 面 1 保活帧，pending 首帧 task（lease 持有）以 interval 竞速续发保活，首真帧经 real-frame 管线 + `sse_keepalive` 续流；关闭走 `lease.close()`
- `keepalive is None` 或 `keepalive.enabled is False` → 走**原** `_buffer_first_chunk_honoring_disconnect` 两方竞速路径（回归保护）

- [ ] **Step 1: 写失败测试**（注入 clock/gate）

```python
# - keepalive=None: 行为与今日字节级一致（对拍现有测试输出）
# - 慢路径：首帧 2×interval 后到 -> 已返回 StreamingResponse、body 先出保活帧、再出首真帧
# - 快路径错误流（首帧在 interval 内且是 error SSE）-> 仍返回 JSONResponse（现行为）
# - 同轮：disconnect 与 timer 同 turn -> 走 499，不误提交
# - 未启动 body 即取消 -> lease.close 生效、无孤儿 task、上游关闭一次
```

- [ ] **Step 2: 运行确认失败** → FAIL

- [ ] **Step 3: 实现**。改 `_buffer_first_chunk_honoring_disconnect` 为返回 `FirstChunkRace` 的三方竞速（`chunk_task` / `disconnect_task` / `asyncio.sleep(interval)` timer，`asyncio.wait(FIRST_COMPLETED)` + 冻结优先级）；`create_response` 依 `match race` 分派：`Disconnected`→现 499；`FirstChunk`→现快路径；`SlowCommit`→组装慢路径 body（面 1 保活 + real-frame 管线 + `sse_keepalive`），传 `committed=True`/`surface` 给 producer。`keepalive is None/enabled False` 走原两方竞速。

- [ ] **Step 4: 运行确认通过** → PASS

- [ ] **Step 5: 提交**

```bash
git add litellm/proxy/common_request_processing.py tests/test_litellm/proxy/test_common_request_processing.py
git commit -m "feat(keepalive): create_response three-way race + slow-commit + StreamLease wiring"
```

---

## Task 10: 三调用点接线 + 全局配置校验 + 启动 warning

**Files:**
- Modify: `litellm/proxy/anthropic_endpoints/endpoints.py:95`（传 `surface=ANTHROPIC`）
- Modify: `litellm/proxy/proxy_server.py:8472`（chat：传 `surface=OPENAI_CHAT`，仅 `/chat/completions`）
- Modify: `litellm/proxy/response_api_endpoints/endpoints.py:200`（+cursor `:394`，传 `surface=OPENAI_RESPONSES`）
- Modify: `litellm/proxy/common_request_processing.py:1225+`（`base_process_llm_request` 加 `downstream_sse_surface` 参数；取选定 deployment 的 `stream_keepalive` override + global，merge→resolve，传 `create_response`）
- Modify: `litellm/proxy/proxy_server.py:4315-4321`（global `litellm_settings.stream_keepalive` 加载边界校验 + `enabled` 但无上游超时时 warning）
- Modify: `litellm/proxy/response_polling/background_streaming.py:146-165`（内部消费显式不启用 keepalive）
- Test: `tests/test_litellm/proxy/anthropic_endpoints/test_endpoints.py`、chat/responses e2e、`test_stream_keepalive_config.py`

**Interfaces:**
- Consumes: 全部
- Produces: 三面端到端保活生效；`enabled=false` 对拍等同现状

- [ ] **Step 1: 写失败测试**（三面 e2e：慢 fake 上游 → 下游收到保活帧；`enabled=false` → 零保活帧对拍；global 校验拒非法；warning 断言）

- [ ] **Step 2: 运行确认失败** → FAIL

- [ ] **Step 3: 实现**。`base_process_llm_request` 解析 global(`litellm.stream_keepalive`) + deployment override → merge → resolve → 若 `enabled` 依 `downstream_sse_surface` 建 strategy 传 `create_response`。三 endpoint 传对应 surface。global 加载边界 `parse_override` 校验。启动时 `enabled and 无有效上游 read/total 超时` → `verbose_proxy_logger.warning`。background polling 传 surface=None。

- [ ] **Step 4: 运行确认通过** → PASS

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "feat(keepalive): wire three SSE surfaces + global config validation + startup warning"
```

---

## Task 11: 合并态回归 + 文档 + BACKLOG

**Files:**
- Modify: `docs/CONFIG.md` 或等价（`stream_keepalive` 配置文档 + 「须与上游超时配套」提示）
- Modify: `docs/BACKLOG.md`（upstream-timeout 依赖门槛 + 可观测性指标：active keepalive streams / ping count / stream age / timeout termination；面 1 native ping PoC 非门禁）
- Modify: `docs/superpowers/specs/2026-07-14-downstream-sse-keepalive-design.md`（标注「已实现」+ PoC 结论回填）
- Test: 全量 `pytest tests/test_litellm/proxy -k keepalive` + 三面合并态 e2e

- [ ] **Step 1: 合并态 e2e**：真实三面 + `include_cost_in_streaming_usage=True`（ping 不进成本）+ DD tracing 开关（只跟真实 chunk）+ 断连各资源恰一次，一次跑通
- [ ] **Step 2: 文档更新**（配置参考 + BACKLOG + spec 回填 PoC 结论）
- [ ] **Step 3: 全量测试 + mutation 抽查**：`pytest tests/test_litellm/proxy -k keepalive -v`；对 `sse_keepalive` / normalizer / lease 抽样 mutation（改 `wait`→`wait_for`、去掉优先级、`\n\n`→单 `\n`）确认测试失败
- [ ] **Step 4: `make pre-commit`**（暂存后运行，修全部 lint/type；`*-budget.json` 变动跑 `make lint-budget-update`）
- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "docs(keepalive): config reference, backlog (upstream-timeout dep + observability), spec impl status"
```

---

## Self-Review 覆盖对照

| Spec 契约 | 落地任务 |
|---|---|
| bytes-safe normalizer + 三 delimiter + EOF + 恒等 adapter | Task 3 |
| 唯一幂等 StreamLease | Task 4 |
| producer 层错误契约 failure-once + event:error | Task 8 |
| all_litellm_params + 三 Router pop 防泄漏 | Task 7 |
| SSEFrame str\|bytes + 统一 real-frame 管线 | Task 2/9 |
| Override/Resolved 双类型 + fields_set merge | Task 1 |
| surface→strategy 冻结映射 + typed 传参 | Task 6/10 |
| 面 1 三方竞速 + 慢路径提交 + 同轮优先级 | Task 9 |
| 面 2 持久 task 组合子 + 两阶段 | Task 5 |
| 上游超时兜底 + 启动 warning + BACKLOG 可观测性 | Task 10/11 |
| PoC 门禁前置 | Phase 0 |
| enabled=false 字节级等同现状 | Task 9/10 对拍 |
