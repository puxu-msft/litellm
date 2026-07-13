# Phase 0 PoC 结果 · gpt reasoning carrier 回放可行性

状态: **server/wire 侧已验证（curl 探针，2026-07-14）；client 侧回放待真实 Claude Code gpt 会话**
关联: `docs/superpowers/plans/2026-07-14-gpt-reasoning-thinking-fidelity.md` Task 6、spec §5。
门禁作用: 证实/证伪 **Claude Code 是否原样存储并回放我们塞进 thinking 的私有载体**（signature_delta），后端是否接受还原的 reasoning item。**不过则停，回到载体/编码决策，不进 Phase 3。**

## 本轮已验证（server/wire 侧，curl 直打运行中的代理）

代理已带新流式插桩 + `GHC_REASONING_POC=1` 重启。原始数据 `~/.claude/litellm/probe-logs/poc-live.sse`。

- **响应侧插桩在线上真的触发**: model=gpt 流式响应里出现 1 个 `signature_delta`，载体 `ghc-rsn:v1:...`，位于 reasoning 块的 `content_block_stop` 之前。
- **oracle 1（encrypted_content 可得且被捕获）✅**: 解码载体得到真实 `encrypted_content` **1688 字符**（head `UYavmpbScfYJcMPZ...`）+ 真实 reasoning item id。证明 encrypted_content 在 `output_item.done` 确实在手、被正确搬进 envelope。
- **codec 线上往返 ✅**: 用 `decode_carrier` 解线上抓到的 token → `DecodedCarrier`，字段完整。
- **oracle 9（R2 体积）**: encrypted_content 1688 字符 → 载体 token **~2859 字节**。signature 字段约 2.8KB，可控，暂不需压缩。
- **oracle 4（后端容忍，partial）✅**: 把载体 thinking 块作为历史回放（turn 2）→ 后端 **200**，不 400。注: 此时请求侧 decode（Phase 4）未建，载体只是作为 thinking 块透传被 gpt 容忍——证明「载体存在于历史不破坏正常运行」，但**尚未**测到 reasoning item 重建/推理连续性（需 Phase 4）。
- 附带观察: 本轮未请求 `reasoning.summary`，故 `summary_parts=0`、thinking 文本为空，但载体已带完整 encrypted_content（即 spec 的 `reasoning_summary=off` 形态）。可见 summary 待 Phase 5 开 `reasoning.summary`。

## 仍需真实 Claude Code gpt 会话（我在 opus 主会话，curl 替不了 client 行为）

**这些是 R1 的核心——纯客户端存储/回放行为，只有真实 gpt 会话能测:**

- **oracle 2**: Claude Code transcript 是否**逐字节原样**存下 `ghc-rsn:v1:...` signature。
- **oracle 3/5**: 下一轮 / 重启后是否原样回放该载体（reconstruction 那半还需 Phase 4）。
- **oracle 6**: 切 opus 后是否不外泄伪签名（需 Phase 4 剥离）。
- **oracle 8**: 双 `message_start` 对真实 Claude Code 的影响（我的 curl 里确实见到 2 个 message_start，但 curl 客户端不代表 Claude Code 的严格度）。

### 最小 client 验证步骤（你来跑，不依赖 Phase 4 的只有 2/8）

1. 起一个 Claude Code 会话，**model 设为 gpt**，发一句能触发推理的话。
2. 找该会话 transcript（`~/.claude/projects/<proj>/<uuid>.jsonl`），`grep -o 'ghc-rsn:v1:' <file> | head` —— 有命中 = **oracle 2 ✅**（Claude Code 存下了载体）。
3. 在同会话再发一句；看 `~/.claude/litellm/probe-logs/`（或 `stream_fix.probe_only`）里该轮请求的 assistant 历史是否带 `ghc-rsn` 载体 = 客户端**回放**了它。
4. 记录该会话头部 SSE 是否因双 `message_start` 出问题（渲染错乱/报错）= oracle 8。

## Oracle 表 · A 载体（signature，当前 PoC 实现）

| # | oracle | 期望 | 实测 | 通过? |
|---|---|---|---|---|
| 1 | direct wrapper 收到的 `output_item.done` 的 `id`/`encrypted_content` | 记录到值 | id 真实、ec 1688 字符 | ✅ |
| 2 | Claude Code transcript 里 `signature` = `ghc-rsn:v1:...` 逐字节原样 | 存在且一致 | 待真实 gpt 会话 | ⏳ |
| 3 | 下一轮 Responses input 的 reasoning item `id`/`ec`/`summary` | 与轮1一致 | 待 Phase 4 + 会话 | ⏳ |
| 4 | 回放载体后端响应码 | 200 | 200（透传容忍，未测重建） | ✅(partial) |
| 5 | 重启 Claude Code 再触发下一轮 | 载体从 transcript 恢复、仍 200 | 待真实 gpt 会话 | ⏳ |
| 6 | 切 model=opus 再发一轮 | 不把伪签名发往 claude 后端 | 待 Phase 4 剥离 | ⏳ |
| 7 | 手改 transcript 载体一字节再回放 | 明确失败/被丢 | 待（codec 已保证 InvalidCarrier） | ⏳ |
| 8 | 完整 SSE 顺序 / 双 message_start 客户端影响 | 记录/不致重置 | curl 见 2 个 message_start；Claude Code 影响待测 | ⏳ |
| 9 | encrypted_content 大小 | 记录 | ec 1688 字符、token ~2859 字节 | ✅ |

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
