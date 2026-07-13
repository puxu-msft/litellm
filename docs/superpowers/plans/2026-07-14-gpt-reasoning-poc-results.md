# Phase 0 PoC 结果 · gpt reasoning carrier 回放可行性

状态: **待你跑（人机协同门禁）**
关联: `docs/superpowers/plans/2026-07-14-gpt-reasoning-thinking-fidelity.md` Task 6、spec §5。
门禁作用: 证实/证伪 **Claude Code 是否原样存储并回放我们塞进 thinking 的私有载体**（signature_delta），后端是否接受还原的 reasoning item。**不过则停，回到载体/编码决策，不进 Phase 3。**

## 前置（重要）

litellm 从 fork 源码树运行（editable），进程内存里是**旧代码**，且需要 flag 环境变量。所以:

1. `export GHC_REASONING_POC=1`
2. **重启 litellm**（让新 `streaming_iterator.py` + flag 生效）——`~/.claude/litellm/start-ghc-api.sh` 或你的启动方式；确认 `import litellm; litellm.__file__` 指向 fork 源码树。
3. 抓 wire: 开 `~/.claude/litellm/hooks.config.json` 的 `stream_fix.probe_only=true` + 相关 diag，`reload.sh`；或直接看 `probe-logs/`。
4. 在 Claude Code 里用 **model=gpt** 跑一段能触发推理的对话。

> flag 关闭或没重启 → 载体不发射、`content_block_stop` 前无 `signature_delta`，等于没插桩。

## Oracle 表 · A 载体（signature，当前 PoC 实现）

| # | oracle | 期望 | 实测 | 通过? |
|---|---|---|---|---|
| 1 | direct wrapper 收到的原始 `output_item.done` 的 `id`/`encrypted_content` | 记录到值 | | |
| 2 | Claude Code transcript 里 thinking 块 `signature` = `ghc-rsn:v1:...` **逐字节原样** | 存在且一致 | | |
| 3 | 下一轮代理构造的 Responses input 里 reasoning item 的 `id`/`encrypted_content`/`summary` | 与轮1一致 | | |
| 4 | 该轮后端响应码 | 200 | | |
| 5 | 重启 Claude Code 再触发下一轮 | 载体从 transcript 恢复、仍 200 | | |
| 6 | 切 model=opus 再发一轮 | 不把 `ghc-rsn` 伪签名发往 claude 后端（否则 400） | | |
| 7 | 手改 transcript 里载体一字节再回放 | 明确失败/被丢，不静默错乱 | | |
| 8 | 完整 SSE 事件顺序 | 记录；双 `message_start` 是否致客户端重置/拒绝（spec §7） | | |
| 9 | encrypted_content 大小 p50/p95/max | 记录（R2） | | |

## Oracle 表 · B 载体（redacted_thinking）

> 需临时把流式插桩改成发 `redacted_thinking` 块（Phase 3 才正式做；PoC 阶段可手改 `_poc_reasoning_signature_delta` 或等 Phase 3）。若 A 已全绿，B 可缓测。

| # | oracle | 期望 | 实测 | 通过? |
|---|---|---|---|---|
| 1-9 | 同上，载体字段换成 `redacted_thinking.data` | | | |

## 门禁判定（跑完填这里）

- **PASS**: oracle 2/3/4/5 至少一种载体全绿、6 不违规 → 记录默认载体（全绿者；都绿默认 A）→ 进 Phase 3。
- **FAIL 分支**:
  - 载体被 Claude Code 规范化/篡改（oracle 2 红）→ 调编码（纯 ASCII/更短）或换载体默认。
  - signature 被校验拒绝（oracle 4 红且报签名相关）→ 切 B 默认。
  - 后端不认还原的 reasoning item（oracle 4 红且报 reasoning/encrypted）→ 复核 §4.1 envelope 是否缺字段（id 必带）。
  - 两载体皆败 → 上报，重议 spec（可能需先修 block 2 双 start，或换机制）。
- **oracle 8 特例**: 若双 `message_start` 致客户端重置/拒绝 → 把 spec §7 该项升为 block 1 前置，插到 Phase 3 前。

## 结论

（跑完写: 默认载体 = ? / 是否进 Phase 3 / 需要的调整）
