# 验证部署真的生效 / Verify a code change is actually live

> 本次教训的**正确版本**(经用户 stdout 日志校正):SIGUSR2 代码热重载**是通的**——用户在 litellm stdout 看到 `hookpkg: reloaded 15 modules via SIGUSR2`(15 = RELOAD_ORDER 条数)即证。我曾据「运行进程 `block-seq.jsonl` 1600 条 0 条含新代码才写的 `in_blocks`」**误判为「reload 静默失效、必须重启」**,并一度把这个错结论写进本文——后被日志推翻。留作教训:**别拿一个间接推断去否定一个可直接观测的机制;先找一手信号(日志/落盘),再下结论。**

那 0/1600 的异常另有原因(未完全坐实,见文末「reload 仍可能不落地的真实原因」),重启恰好绕过了它,不代表 reload 机制本身坏。

## 核心纪律:部署后要「确认生效」,别停在「跑过部署命令」

reload.sh 跑了 ≠ 新代码在跑。有两条独立通道,失效表现不同:

| 变更类型 | 生效机制 | 成功信号 |
|---|---|---|
| `hooks.config.json`(开关/参数) | `config.py` 按 **mtime 热读** | stdout:`hookpkg: reloaded config from ...` |
| `hookpkg/` **代码** | **SIGUSR2** → `importlib.reload` | stdout:`hookpkg: reloaded N modules via SIGUSR2` |

两个**双重确认**信号,缺一别声称已上线:

1. **stdout 日志**(litellm 进程的 stdout,本项目落在启动它的伪终端如 `/dev/pts/28`):
   - 成功:`hookpkg: reloaded N modules via SIGUSR2`(N 应等于 `RELOAD_ORDER` 条数;偏少=中途失败)
   - 失败:`hookpkg: reload failed at <module> (...); keeping loaded modules` —— **reload 抛错、停在该模块之前**,后续模块仍是旧代码(部分生效,最坑)
   - 启动时一次:`hookpkg: SIGUSR2 reload handler installed`(没有它=handler 没装,信号白发)
   - `_on_signal` 只置 flag、**不打日志**;下一次请求触发 `maybe_reload` 才 reload+打日志,故**发信号后要有一个真实请求**日志才出现。
2. **代码级落盘可观测量**(新代码独有的字段/审计事件),在真实流量上确认出现:
   - 新字段 → 查落盘记录**有没有它**(本次:`'in_blocks' in record`)。
   - 新审计事件 → 查 audit 文件有没有该事件名(`dropped_truncated_frame` / `dropped_duplicate_tool_use`)。

日志说「reloaded N modules」是**机制层**确认;落盘出现新字段是**行为层**确认。两者都过,才算真上线。本次翻车正是只做了「离线单测绿 + 跑了 reload.sh」,没做这两条**线上**确认。

> `strace` 抓 stdout 在本机不可用:`ptrace_scope=1`,只能附加子进程。所以 stdout 日志得靠启动 litellm 的那个终端去看,或改造 litellm 落一份日志文件。

## reload 仍可能不落地的真实原因(排查清单)

即使 `reloaded N modules` 出现,以下情形仍会让某次改动看似「没生效」:

- **改了 `reload.py` 本身**(如 `RELOAD_ORDER`):它**不在 RELOAD_ORDER 里、永不自我 reload**,运行中的 `maybe_reload`/`RELOAD_ORDER` 恒为**进程启动时**的版本 → 改 reload 逻辑/顺序**必须重启**。
- **改了 `hooks.py` 薄壳**(新增 hook 方法):litellm 启动时只加载一次薄壳、`vars(cls)` 那时固定 → **必须重启**。
- **reload 中途失败**:`reload failed at <module>` 后,失败模块及其后的模块保留旧代码(N 偏小是信号)。常因某模块被并发编辑到语法/导入错误的中间态。
- **in-flight 流不受影响**:进行中的流式请求闭包已绑定旧模块;只有 reload 之后**新发起**的请求用新代码。刚 reload 就看旧流的落盘会误判。

## 检查清单(改完 hookpkg 代码后)

1. `./reload.sh`(改 `reload.py`/薄壳则改为**重启**)。
2. 看 litellm stdout:出现 `reloaded N modules via SIGUSR2` 且 N 对得上;没有 `reload failed at`。
3. 触发一次能命中新代码的真实请求。
4. 查线上落盘:新字段/新事件**确实出现**。出现=生效;没出现=没生效,别声称已上线。
