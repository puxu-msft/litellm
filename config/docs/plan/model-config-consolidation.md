# litellm 模型配置整合计划 / Model Config Consolidation

状态：**已搁置/交接**（2026-07-13）。此计划 over-plan 了——用户指出该改动可直接操作。已转为直接执行：config.yaml 的 `model_list`/`router_settings` 已按目标态改写（4 主名 + 完整 model_info 迁移），**gpt 条目部分由用户接手整理**，DB 清理与重启**未执行**。本文档保留作背景记录，勿再当作待办执行。备份在 `~/.config/litellm-backups/`（仓库外）。
日期：2026-07-13

## 目标

把散落在 `config.yaml` + DB（`LiteLLM_ProxyModelTable` + `LiteLLM_Config.router_settings`）的模型配置，整合为「**config 主导、DB 运行时兜底**」的单一声明式真相。**策略改为「迁移优先」**：先把 DB 里丰富的模型元数据完整搬进 config、验证 config 能完整接管，**再**谈删除 DB 脏行。保留 `store_model_in_db:true`。

## 用户决策（已定）

- 存储策略：保留 DB，config 主导。
- 凭证：用 `litellm_credential_name` 引用已有凭证 `ghe_puxu`（provider=github_copilot），config 不写明文 key。
- DB 后端行处置：**先移入 config 再说删除**——因 config 现有条目缺失大量信息（成本/max_tokens/supported_openai_params 等），必须先把 DB 的完整 model_info 迁移进 config，不可先删。
- 主名命名：**`github_copilot/*` 全名做主 model_name**；裸名/变体做 alias。

## ⚠️ 评审修正的错误事实（v1 → v2）

- **v1 错误**：称"config.yaml 目前无 router_settings，证明 DB 覆盖 config"。**实测 config.yaml 第 20 行起已有 `router_settings.model_group_alias`**，且与 DB 那份近乎逐条相同 → "观测到 alias 生效"**无法区分**是 config 还是 DB 生效。**"DB 覆盖 config"这条论断作废**，改为「必须实验判定」（见 待验证 E1）。
- **v1 遗漏**：清理清单漏掉一批 `db_model=true` 的裸名/变体后端行（`opus` 32eef3a1、`haiku` 06209bf1、`claude-haiku-4-5` daab3f1e）。它们的 model_name **恰好等于将要用作 alias 键的名字** → alias 键与真实 deployment 撞名，解析优先级不定，别名可能静默失效。必须纳入处置。
- **v1 附带发现**：config 现有 `model_list` 用 `config/*` 名，但 alias 目标是 `github_copilot/*`（只存在于 DB）→ config 那 3 个 `config/*` 是无别名指向的"僵尸"，实际路由全落到 DB 行。

## 现状盘点（已核实，2026-07-13）

### DB `LiteLLM_ProxyModelTable`（10 行）
| model_id(前8) | model_name | db_model | backend key | 分类 |
|---|---|---|---|---|
| 24c5be56 | github_copilot/claude-haiku-4.5 | true | .../claude-haiku-4.5 | **规范后端（迁移源）** |
| 6ad92d77 | github_copilot/claude-sonnet-5 | true | .../claude-sonnet-5 | **规范后端（迁移源）** |
| 34decbf8 | github_copilot/claude-opus-4.8 | true | .../claude-opus-4.8 | **规范后端（迁移源）** |
| 7066537e | github_copilot/gpt-5.6-sol | true | .../gpt-5.5 | 名实不符（backend=gpt-5.5）→ 迁移为 gpt-5.5 |
| daab3f1e | claude-haiku-4-5 | true | .../claude-haiku-4.5 | 变体裸名，将转 alias → 待删 |
| 06209bf1 | haiku | true | .../claude-haiku-4.5 | 裸名，将转 alias → 待删 |
| 32eef3a1 | opus | true | .../claude-opus-4.8 | 裸名，将转 alias → 待删 |
| 9477544f | haiku | false | （空） | 残缺重复 → 待删 |
| 33c036ef | sonnet | true | （空） | 残缺（无 backend）→ 待删 |
| 55edee73 | github_copilot/gpt-* | true | （空） | 残缺 wildcard → 待删 |

### config.yaml
- `model_list`：`config/gpt-5.5`、`config/claude-opus-4-8`、`config/claude-sonnet-5`（僵尸，无别名指向）。
- `router_settings.model_group_alias`：13 条，`gpt`/`gpt-5.5` 指向 `github_copilot/gpt-5.6-sol`。

### 已核实事实
- 运行进程环境**无 `LITELLM_SALT_KEY`**；`master_key=admin`。litellm 加密回退 `SALT_KEY→master_key`，故 DB 凭证/加密字段现由 `admin` 解密。
- 实际认证走文件 `GITHUB_COPILOT_TOKEN_DIR=.../github_copilot`（`access-token`+`api-key.json`）；启动脚本另 export `GITHUB_COPILOT_API_BASE=https://enterprise.api.githubcopilot.com`。
- 凭证 `ghe_puxu`（provider=github_copilot）存在，`credential_values` 含**加密的 api_key+api_base**。
- **SpendLogs/AuditLog 对 ProxyModelTable 无外键**（全库 FK 命中 0）；SpendLogs 4004 条、AuditLog 空 → **删模型不级联、不破坏历史**。
- teams=0，keys 仅 `all-team-models` → 无 team/key 引用被删模型的风险。
- DB 后端行 litellm_params 里 `model`/`api_key`/`api_base`/`litellm_credential_name` **全是加密串**，不可照搬进 config；config 用明文 `model:` + `litellm_credential_name: ghe_puxu`。

## 目标态设计

### 命名规范
- 主 model_name（= DB backend）：`github_copilot/claude-opus-4.8`、`github_copilot/claude-sonnet-5`、`github_copilot/claude-haiku-4.5`、`github_copilot/gpt-5.5`。
- alias（多对一，指向上面主名）：`opus/sonnet/haiku/gpt` + `claude-opus-4-8`、`claude-opus-4.8`、`claude-sonnet-5`、`claude-haiku-4-5`、`claude-haiku-4.5`、日期后缀变体等。
- 废弃 `config/*` 与 `gpt-5.6-sol` 名。

### config.yaml `model_list` 目标（含 DB 迁移来的完整 model_info）
以 opus 为例（其余同构，字段取自 DB 对应行；成本/params 逐字段搬入；cache 成本仅 sonnet 有）：
```yaml
  - model_name: github_copilot/claude-opus-4.8
    litellm_params:
      model: github_copilot/claude-opus-4.8
      litellm_credential_name: ghe_puxu   # 见 H1/H2：解密与 api_base 须实证
    model_info:
      mode: chat
      supported_endpoints: [/v1/chat/completions, /v1/messages]
      input_cost_per_token: 0.0005
      output_cost_per_token: 0.0025
      # sonnet 额外: cache_read_input_token_cost / cache_creation_input_token_cost / thinking/reasoning_effort params
      # haiku 额外: max_tokens:16000, max_input_tokens:128000, max_output_tokens:16000, supports_vision, supports_function_calling
```
- gpt-5.5：`mode: responses`，`supported_endpoints: [/responses]`（DB 值），`input 0.0005 / output 0.003`，params 含 `reasoning_effort/verbosity`。
> 迁移素材已从 DB 4 个规范行完整提取（成本、max_tokens、cache 成本、supported_openai_params、supported_endpoints、supports_vision/function_calling）。

### router_settings 目标
alias 全部指向 `github_copilot/*` 主名；`gpt`/`gpt-5.5` 死链改指 `github_copilot/gpt-5.5`。

## 执行步骤（安全化顺序）

### 阶段 0：备份（放仓库外，强制）
- `pg_dump -Fc` 全库 → `~/.config/litellm-backups/litellm-<date>.dump`，`chmod 600`。**不放仓库树内**（SpendLogs 含明文 prompt，防 git 泄漏；禁 `git add -A`）。
- 单表快照：`pg_dump -t ProxyModelTable -t LiteLLM_Config` → 同目录 `.sql`（秒级回滚点）。
- `router_settings` 旧值单独 `\o` 落盘 JSON。
- `cp config.yaml ~/.config/litellm-backups/config-<date>.yaml`。
- **恢复演练**：`pg_restore` 到临时库 `litellm_restore_test`，核对 ProxyModelTable count=10，通过后 `dropdb`。

### 阶段 1：config.yaml 迁移落地（不动 DB）
- 改写 `model_list`：4 个 `github_copilot/*` 主名，逐字段搬入 DB 的 model_info。
- 改写 `router_settings`：alias 指向新主名，修死链。
- 离线校验 YAML：`python -c 'import yaml;yaml.safe_load(open("config.yaml"))'`。

### 阶段 2：异端口验证 config 完整接管（关键门控，不停生产）
- 另起 `--port 4199` 新进程加载新 config。
- 验证清单：
  - `/v1/models` 列出 4 主名。
  - **credential 解密**（H1）：发一条走 `ghe_puxu` 的真实请求，非 401/decrypt error。
  - **api_base 生效**（H2）：确认最终打到企业端点，与 env 一致。
  - 别名路由：`opus`/`claude-opus-4-8`/`claude-opus-4.8` → opus 后端；haiku/sonnet 同理。
  - **全名主名解析**（M1）：直接 curl `github_copilot/claude-opus-4.8`，确认不被误当 provider+model 二次解析。
  - **gpt responses**（M3）：curl gpt 走 `/responses`，reasoning_effort 可用。
  - **router_settings 加载判定**（E1，见待验证）：确认新 config 的 alias 确实由 config 提供（此时 DB 那份仍在，需设计可判定探针）。
- 全绿 → 停 4199，原地重启生产（或切端口）；任一红 → 丢弃新 config，生产零影响。

### 阶段 3：DB 清理（破坏性，验证通过后才做；事务 + model_id 精确 + 断言行数）
- **前置**：先确认 config 已完整接管（阶段 2 全绿）。
- 处理 DB `router_settings`（E1 定论后）：清空或对齐（见待决 1）。先 `\o` 备份原值，`BEGIN; DELETE WHERE param_name='router_settings'; -- 预期 DELETE 1; COMMIT`。
- 删脏行：`BEGIN;` → `SELECT ... WHERE model_id IN (...)` 人工核对 → `DELETE ... WHERE model_id IN (...)` 断言 `DELETE N` → `COMMIT`（异常 `ROLLBACK`）。
  - **绝不用 `WHERE model_name=...`**（两个 haiku，会误删生效行 06209bf1）。
  - 待删 model_id：9477544f、33c036ef、55edee73（残缺）；daab3f1e、06209bf1、32eef3a1（转 alias 的裸名）；7066537e（gpt-5.6-sol，**先确认阶段1已建 gpt-5.5 且别名改指后**再删，避免空指针窗口）；规范 4 行（24c5be56/6ad92d77/34decbf8）**是否删取决于待决 2**。

### 阶段 4：重启与端到端验证
- 原地重启；失败判据（任一 alias 404 或后端 401）触发 → `cp` 回 config + `pg_restore`/单表 sql 还原 + 重启。
- 一次真实 Claude Code 请求端到端（确认现有 hook 修复链路不受影响）。

### 阶段 5：文档归档
- 新建 `docs/CONFIG.md` 记录最终配置真相 + 命名规范 + credential/api_base 结论 + E1 实证结果。
- 本计划留 `docs/plan/`；记忆索引登记。

## 待验证（执行中实证，非假设）
- **E1**：`store_model_in_db:true` 下 config 与 DB 均有 `router_settings` 时的**合并/覆盖顺序**。用可判定探针：config 加一个 DB 没有的独特 alias（如 `zzz_probe→opus`），重启后 curl `zzz_probe` 是否路由成功；反向再验 DB-only。**这是删 DB router_settings 前的门控**——若 config 不接管就删，别名全断。
- **H1**：`ghe_puxu` 能否被 `master_key=admin` 解密（凭证可能在曾设 SALT 的环境写入）。阶段 2 发请求实证。
- **H2**：`ghe_puxu.api_base` 与 env 企业端点是否一致；不一致则凭证剥 api_base、依赖 env。
- **M1**：`github_copilot/*` 全名做 public 名不被 provider 前缀二次解析。

## 待决问题（评审后请用户拍板）
1. E1 定论后，DB `router_settings`：(a) 清空只留 config；(b) 保留 DB 版并与 config 对齐。倾向 a（config 主导）。
2. DB 规范 4 行（24c5be56 等）在 config 完整接管后**是否删**？删=纯 config 单一真相；留=config+DB 同名双 deployment（评审 B2 指出行为不确定，不推荐留）。倾向删。
3. gpt-5.6-sol 迁移为 gpt-5.5 后确认删除，无异议？

## 关联工作（登记，不在本计划实施）
- **hook `_remap_model`**：glob + 有序 + 忽略大小写多对一，覆盖 `model_group_alias` 无法表达的无穷变体（日期后缀等）。上一轮已设计草案，取决于 Claude Code 实际发出的 model 名集合。
