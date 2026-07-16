# litellm 代理配置与 hook / litellm Proxy Config & Hooks

本地 litellm 代理,把 Claude Code 的 `/v1/messages` 请求转发到 GitHub Copilot 后端
(`github_copilot/claude-*`、`gpt-*` 等),并通过官方 hook 修复代理转换层引入的各类问题。

> 注:模型清单(`config.yaml`、`AVAILABLE_MODELS.json`)与 `docs/plan/` 下的规划另有维护,
> 本 README 聚焦 **hook 系统**(`hooks.py` + `hookpkg/`)与运维。

## 快速上手

```sh
./start-ghc-api.sh          # 启动 litellm(127.0.0.1:4143)
./reload.sh                 # 改 hookpkg/ 代码后热重载(SIGUSR2),无需重启
python smoke_test.py        # 冒烟测试(用 litellm 的 python 跑)
```

改 `hooks.config.json` 无需任何操作(按 mtime 即时热读)。

## 启动与恢复性能

Prisma schema 的 `generator client` 必须保留 `recursive_type_depth = -1`。本项目使用 basedpyright，该模式使用生成器推荐的真实递归类型，避免默认深度 5 把关系输入类型展开成约 48.8 万行。2026-07-17 实测生成的 `types.py` 从 22 MiB 降至 5 MiB，同机暖启动到 `Application startup complete` 从约 12.34 秒降至 8.44 秒。

普通启动不负责迁移数据库。`general_settings.disable_prisma_schema_update` 和 `disable_prisma_schema_check` 均为 `true`，因此启动只连接数据库并执行 health check，不运行 `prisma migrate deploy`、`db push` 或 `migrate diff`。schema 变化后，先按项目 migration runbook 显式生成并应用迁移，再重启服务；根目录 `start.sh` 只按 schema 内容哈希决定是否重新生成 Python client。

Caddy 对上游 4142/4143 每 2 秒做一次主动健康检查，一次通过后恢复。这样 LiteLLM 就绪后，入口 4141 再等待 0～2 秒即可恢复流量。

### 创建客户端 API key

调用 `/key/generate` 时传入 `key`，即可手动指定客户端用于 `Authorization: Bearer sk-...` 的 Virtual Key。该值必须以 `sk-` 开头且至少包含 16 个字符：

```sh
curl --fail-with-body --silent --show-error \
   http://127.0.0.1:4141/key/generate \
   --header 'Authorization: Bearer admin' \
   --header 'Content-Type: application/json' \
   --data '{"key":"sk-my-fixed-client-key","key_alias":"manual-client-key"}'
```

省略 `key` 时，LiteLLM 仍会自动生成随机的 `sk-...` key：

```sh
curl --fail-with-body --silent --show-error \
   http://127.0.0.1:4141/key/generate \
   --header 'Authorization: Bearer admin' \
   --header 'Content-Type: application/json' \
   --data '{"key_alias":"generated-client-key"}'
```

响应中的 `key` 只应在创建时保存；数据库只存哈希，之后无法找回明文。若 `general_settings.master_key` 不再是 `admin`，请同步替换请求头中的管理 key。

## 目录

```
hooks.py            稳定薄壳:注册 SIGUSR2、委托 hookpkg。litellm 唯一直接加载的 hook 文件
hooks.config.json   hook 行为配置(热读)
hookpkg/            hook 实现包(见下)
reload.sh           kill -USR2 触发 hookpkg 热重载
smoke_test.py       每个 config 分支跑最小请求,暴露搬运缺陷
start-ghc-api.sh    启动脚本(设 copilot token 目录、Redis 等)
patches/            litellm site-packages 的幂等 patch(转换层 bug 修复)
exp/signal-reload/  SIGUSR2 热重载 PoC + 结论
config.yaml         litellm 模型/通用配置(另行维护)
github_copilot/     copilot 凭证(.gitignore,不入库)
probe-logs/         探针/审计输出(.gitignore,含敏感对话,chmod 700)
```

## hook 系统做什么

Claude Code 经此代理打到 copilot 后端时,`/v1/messages`(Anthropic 格式)会被转成
OpenAI chat/completions,copilot 再转回 Anthropic——**一次请求两次格式往返**,期间会引入
各种损坏。hook 通过官方 `CustomLogger` 五个回调(不改 site-packages)修复:

| hook | 时机 | 修复 |
|---|---|---|
| `process` | 转换前 | strip cache_control.scope、fix tool_choice、注入工具、请求侧孤儿 tool_result 补全、thinking block 修复(见下) |
| `process_deployment` | 转换后/发出前 | 转换后 OpenAI 载荷的孤儿 tool_call 观测/修复(盲区) |
| `observe_failure`/`observe_success` | 失败/成功 | 只观测(孤儿检测、抓失败载荷) |
| `stream_transform` | 流式响应 | 见下"流式修复" |

### 流式工具参数修复(`stream_fix`)

模型经代理生成工具调用时,参数常损坏。`stream_transform` 在流式响应里缓冲工具 input、
修复、重发。四类修复按**意图优先级**串联(`hookpkg/fixes/`):

1. **string-to-array** — 本该是数组的字段被双重编码成字符串(如 AskUserQuestion 的
   `questions`),还原成数组。
2. **json-fix** — 截断的 JSON(缺尾部 `]}`)用括号栈补齐闭合。
3. **unicode-fix** — 坏 unicode 转义(`\u` 后非十六进制、孤立代理)修成可解析。
4. **header-to-question** — 缺失字段从同级字段补(`question ← header`)。

以及**泄漏 invoke 转换**:模型把本该是 tool_use 的 `<invoke name=...>` 吐进 text block
时,拆成 `[text + tool_use]` 真 block、传播 index 偏移、改 stop_reason 让客户端执行。
白名单(`convert_text_invoke_tools`,支持 glob 如 `mcp__plugin_*`)防误伤。

### thinking block 修复(`fix_thinking`,请求侧)

客户端可能把损坏的 thinking block 排列作为历史发回,后端报错。`process` 在转换前修复
(`hookpkg/thinking.py`):

1. **连续 thinking** — 同一 message 内相邻两个 thinking block 之间插空格文本块。
2. **空 signature** — signature 空是错误(区别于 thinking 内容空——那是新版常态,不动)。
   `empty_signature` 配置:`to_text`(转文本,text=thinking 内容;内容为空则删;删后 content
   空则空格占位)/ `remove`(整个删)/ `off`。
3. **strip_all** — 一键剥离所有 thinking/redacted_thinking block(优先级最高)。

优先级:`strip_all` > `empty_signature` > `insert_text`。

## 配置(hooks.config.json)

顶层键:`fix_tool_choice`、`inject_tools`、`fix_thinking`、`probe`、`deployment_probe`、
`failure_probe`、`success_probe`、`stream_fix`。各修复/探针独立开关,默认多为关。改动即时热读生效。

`stream_fix` 关键项:
- `enabled` — 总开关
- `tools` — 每工具的修复规则(`items_key` + `copy_within_items`)
- `convert_text_invoke` + `convert_text_invoke_tools` — 泄漏转换 + 白名单
- `probe_only` — 只观测不改写(排错时抓真实 wire 数据)
- `audit_file` — 修复动作审计落盘

`fix_thinking` 关键项:
- `insert_text` — 连续 thinking 插空格(默认 True)
- `empty_signature` — 空 signature 处理:`to_text`/`remove`/`off`(默认 to_text)
- `strip_all` — 一键剥离所有 thinking(默认 False)


## 可观测(ProbeContext)

`hookpkg/probes.py` 的 `ProbeContext` 统一事件:
- `audit(type, ...)` — 修复动作(patched/json_repaired/invoke_converted/parse_failed),
  落 `audit_file`,**生产也记**,是线上信号。
- `diag(type, ...)` — 调试观测,仅 `probe_only` 落 `probe_file`。
- 每事件自动带 `ts`/`model`/`call_id`。

排错真实问题时:置 `stream_fix.probe_only=true` → `reload.sh` → 触发请求 → 看
`probe-logs/` 里的诊断事件(chunk_shape/block_start/tool_input/text_leak_* 等)确认真实
wire 格式,再写针对性修复。

## 热重载机制

- 改 `hookpkg/` 代码 → `./reload.sh`(SIGUSR2)→ **下次请求**时拓扑 reload 整个包
  (进行中的流不受影响,因闭包已绑定旧模块)。
- 改 `hooks.config.json` → 无需操作(config.py 按 mtime 即时热读)。
- 改 `hooks.py` 薄壳本身(如新增 hook 方法)→ **必须重启 litellm**(启动时只加载一次薄壳,
  `vars(cls)` 那时固定)。

## patches/

litellm 转换层有个真实 bug:`tool_result` 内层全是 `tool_reference` 块(来自 ToolSearch/
deferred tools)时被静默吞掉 → tool_use 孤儿 → API 报错。`patches/` 提供幂等 patch 修
site-packages(官方 hook 够不到那一层)。用法见 `patches/README.md`。升级 litellm 后重跑
`patches/apply.sh`。

## 相关知识

流式畸形/截断响应的处理谱系(各类畸形 → 客户端症状 → hook 现状/已知缺口)见活文档
`docs/illformed-fix.md`;文本块全缓冲机制见 `docs/plan/degeneration-trim.md`。

调试方法论、官方 hook 机制、模块化架构等沉淀在 skill:
`~/.claude/skills/debugging-llm-proxy-transforms/`(尤其 `reference/package-architecture.md`)。
