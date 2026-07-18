# Kick-off：终端可观测 Phase 1 — Versioned events 与 SQLite primitives

你正在 `/home/xp/refs/ai-agents/litellm` 的私有 `ghc` fork、`ghc` 分支工作。本次只执行 Phase 1，不接生产请求、不接 TTY、不改当前 `config/hookpkg/logline.py` PoC 的 ownership。

开始前按顺序阅读：

1. `CLAUDE.md`
2. `docs/superpowers/specs/2026-07-18-terminal-observability-event-archive-design.md`
3. `docs/ADR.md` 的 ADR-0002
4. `exp/terminal-observability-phase0/CONCLUSION.md`
5. `docs/superpowers/plans/2026-07-18-terminal-observability-event-archive.md` 的 §0–§2、§11–§12
6. `docs/superpowers/specs/2026-07-14-in-flight-observability-graceful-shutdown-design.md`
7. `docs/TRACKING.md`

工作树很脏且存在 graceful-shutdown 并行开发。先执行 `git status --short`，识别他人改动；不得 reset、checkout、stash、删除或覆盖不属于本阶段的文件。本阶段只新增 `litellm/proxy/observability/terminal/` 下的离线 core、镜像测试和必要文档。不要修改 proxy startup、uvicorn runner、shared httpx、hookpkg、config.yaml 或 UI。

## 本阶段目标

按 TDD 完成：

1. Versioned immutable event envelope、tagged payloads、length-prefixed orjson codec
2. Session/event/worker identity 与固定 credential-header 掩码
3. Per-worker SQLite WAL spool、sequence/range/durable ack/compact primitive
4. zstd+BLAKE3 content pool、chunk manifest、body completeness
5. Active/draining/immutable central segment、非轮转 catalog、session aliases、crash recovery 与 orphan GC

## 执行纪律

每个 task 都必须：

1. 先写一个会失败且能杀死目标 mutation 的测试
2. 运行该窄测试确认因缺实现而红，不是 import/fixture 错
3. 写最小 typed 实现
4. 立即重跑同一测试
5. 再跑该模块相邻测试和 Ruff/类型诊断
6. 一个 task 完成后才进入下一个

核心代码要求：fully typed、无 `Any`、frozen dataclass + slots、tagged union + exhaustive match、composition、dependency injection、不可变集合。未知 JSON 在边界用 Pydantic/TypeAdapter 验证。不要用 monkeypatch class attributes；注入 clock、UUID、connection factory、filesystem fault points。

## 独立 oracle

- BLAKE3/zstd 用官方库直接计算/解码，不用自己的 round-trip 作为唯一证明
- SQLite 用标准 `sqlite3` 直接检查 rows/schema/WAL/transaction，不只经 repository API 读取
- codec 人工切割 prefix/payload、多帧、EOF、超上限
- crash protocol 在每个 fsync/rename/catalog publish fault point重开目录验证
- content manifest随机chunk cuts拼接必须等于原bytes；缺blob不能complete
- secret原值全树搜索不得出现在event/DB/repr/error

## 禁止事项

- 不创建 Rich renderer、Web/API、DuckDB coordinator、network replay
- 不接 CustomLogger/InFlightRegistry/真实请求
- 不修改当前PoC
- 不把 Phase 0 experiment module复制进生产；只实现其验证过的合同
- 不自动commit

## 验收

按主计划 Phase 1 的 Task 1.1–1.5 逐项完成。至少运行：

```bash
.venv/bin/python -m pytest tests/test_litellm/proxy/observability/terminal -q
.venv/bin/ruff check litellm/proxy/observability/terminal tests/test_litellm/proxy/observability/terminal
```

然后运行相关类型检查、mutation testing（核心纯逻辑 >90% kill）和 `make pre-commit`。如果修复 lint/type budget，运行 `make lint-budget-update`。若 `make pre-commit` 因本阶段之外的脏工作树文件失败，不得 stash/delete/修复他人文件；保存完整输出，运行本阶段路径的 Ruff、basedpyright 与测试，明确报告全局 pre-commit 仍被阻塞，并在最终合并前到隔离 worktree 或协调后的干净工作树补过。更新 Spec/TRACKING/DESIGN 的实际状态。

完成后交独立 reviewer；处理 findings 后 re-review。最终报告必须列：新增 schema版本、测试/变异结果、crash-point覆盖、仍未实现的 Phase 2+ 内容、任何与冻结 Spec 的偏差。遇到无法满足幂等、segment closure 或不阻塞业务的结构性问题时停止并回到 ADR，不自行缩减能力。
