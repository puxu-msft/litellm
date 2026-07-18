# 流式 SSE 改写状态机 / Streaming Rewrite

在 `async_post_call_streaming_iterator_hook` 里改写发往客户端的 Anthropic SSE 流。用于补全模型漏填的工具参数等。**高风险**：改流一旦丢块/破坏事件序列/吞异常，客户端会话会卡死。

## ⚠️ chunk 的真实形态：多半是 SSE bytes，不是 dict

**最先要确认的事**：`async_post_call_streaming_iterator_hook` 拿到的 chunk **不一定是事件 dict**。在 `anthropic_messages` 路径，SSE 序列化发生在 hook **之前**（`adapters/transformation.py:303` → `async_anthropic_sse_wrapper` `yield payload.encode()`），所以 hook 收到的是 **`bytes`**：
```
b'event: content_block_start\ndata: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","name":"AskUserQuestion",...}}\n\n'
```
按 dict 解析（`chunk.get("type")` / `getattr(chunk,"type",None)`）会全部落空，状态机一个块都匹配不到，**修复静默失效**。**动手前先加 `chunk_shape` 诊断**（记 `type(chunk).__name__` + repr 前缀）确认真实形态。

处理办法：`_sse_parse(chunk)` 把 bytes/str 的 `data: <json>` 行解析成事件 dict（dict 直接返回，`[DONE]`/ping/空行返回 None→透传）；状态机在 dict 上判断；`_sse_serialize(ev, template_chunk)` 按原 chunk 形态（bytes/str/dict）重序列化输出，wire 形态与 litellm 的 `async_anthropic_sse_wrapper` 一致（`event: <type>\ndata: <json>\n\n`）。

## 流式下工具 input 的形态

一个 tool_use 块的事件序列（解析出的 dict 视角）：
```
content_block_start  {content_block:{type:"tool_use", id, name, input:{}}}
content_block_delta  {delta:{type:"input_json_delta", partial_json:"{\"que"}}
content_block_delta  {delta:{type:"input_json_delta", partial_json:"stions\":[..."}}
...
content_block_stop
```
工具 `input` 是**分片 JSON**（`partial_json` 累积），由客户端拼接还原。litellm 逐片透传，中途不重组。

## 改写策略：缓冲—重组—补—重发

对匹配工具的 tool_use 块：
1. 遇到 `content_block_start`(type=tool_use, name 匹配) → **进入缓冲，先不发 start**。
2. 累积所有 `input_json_delta.partial_json`。
3. `content_block_stop` → `"".join(partial)` 重组 → `json.loads` → 补字段 → 发出 `start` + delta + `stop`。**仅当真有改动（或原本无 delta）时**才发一条完整 JSON 的 input_json_delta；**未改动时字节级回放原始分片 delta**（`hookpkg/stream.py` 条件：`changed or not buf_deltas` 才发全量，否则回放）——避免无谓重序列化。

**单条全量 delta 等价于多条增量**：客户端靠拼接 partial_json 还原 input，一大条与多小条 JSON 字符串拼接结果相同。（已实测客户端接受。）

非匹配块**原样透传**，绝不缓冲。

## 必守的安全规则（评审血泪）

### 1. 异常必闭合块，且不 raise 到薄壳
transform 是 async 生成器。若在缓冲中途上游抛异常或 json 出错：
- **必须补发 `content_block_stop`**（用 buf_start 的 index）闭合已发的块，否则客户端 SSE 状态机悬挂。
- **补完后 `return`，不要 raise**。因为薄壳的兜底 `async for chunk in response` **无法重放已被 transform 消费的上游**（async generator 消费即失效），raise 只会让薄壳从消费点之后继续 → 丢掉已消费的块（可能含 stop）。

```python
except Exception as e:
    logger.warning("stream_fix: error (%r); closing buffered block", e)
    if buffering and buf_start is not None:
        yield buf_start
        for d in buf_deltas: yield d
        yield {"type": "content_block_stop", "index": _chunk_get(buf_start, "index", 0)}
    return  # 不 raise
```
薄壳侧对应：`except` 里也**不再** `async for chunk in response`，直接 `return`。

### 2. JSON 解析失败回放原始分片
重组 `json.loads` 失败时，回放缓冲的原始 delta（字节级一致），绝不丢块、绝不发半个 JSON。

### 3. 只补不覆盖
仅当目标字段为空且源字段存在才复制（`if not it.get(dst) and it.get(src)`）。已有值不动。

### 4. 空 input 不凭空造
若 input 整个为空（无源数据可补），保持透传。别凭空造合法结构——那是猜测。用 probe_only 先观察模型是否真会发空 input。

### 5. index 对齐
重组的 delta 复制 start 块的 `index`，保证客户端把 delta 拼回正确的 content block（一条消息可能有多个 block）。

### 6. chunk 形态兼容
chunk 可能是 dict 或带属性对象。用 `_chunk_get(chunk, key, default)` 兼容两者。

## 触达诊断（最重要的排错手法）

**修 L5 前，先确认 chunk 的真实形态、再确认目标工具调用是否出现在流里。** 否则修复逻辑再对也不触发。

在 probe_only 模式下埋多级诊断（都写同一 probe_file，用 `_diag` 字段区分）：
```python
# 0) chunk 真实形态(最先看!): 判定 chunk 是 dict 还是 bytes/str —— 决定要不要 _sse_parse
{"_diag":"chunk_shape", "py_type": type(chunk).__name__,
 "parsed_type": ev.get("type") if ev else None, "repr_head": repr(chunk)[:180]}
# a) 请求侧(process, 一定触发): 证明请求是流式且带目标工具定义
{"_diag":"request_seen", "stream": data.get("stream"), "has_AskUserQuestion": ...}
# b) transform 入口: 证明流式 hook 进了链路
{"_diag":"stream_transform_entered"}
# c) 每个 content_block_start 的类型(解析后): 证明流里有没有 tool_use
{"_diag":"block_start", "block_type": _ev_block_type(ev)}
# d) 每个 tool_use 块的工具名: 证明工具名拼写、是否命中规则
{"_diag":"tool_use_seen", "tool": name, "matched": bool}
```
另有生产模式审计（probe_only 关时也记）：真正补全时写 `{"_diag":"patched","tool":...}` 到 `stream_fix.audit_file`，用于验收「修复是否真触发」（不落敏感全文）。

判读：
- `chunk_shape.py_type == bytes/str` → **必须 `_sse_parse` 先解析**，别按 dict 处理（本案第一轮就栽在这）。
- `request_seen` 有、`stream_transform_entered` 为 0 → **hook 没进链路**（多半没定义在薄壳类上，`vars(cls)` 检测失败；改后需重启）。
- `stream_transform_entered` 有、`block_start` 空、但 `chunk_shape` 是 bytes → **不是没 block，是你没解析 bytes**（别急着归因为「不走代理」；`request_seen` 有记录已证明走了代理）。
- `tool_use_seen` 里工具名和配置对不上 → 工具名拼写/大小写问题。

## 配置（hooks.config.json）

```json
"stream_fix": {
  "enabled": true,
  "probe_only": false,
  "probe_file": "/home/xp/.config/litellm/probe-logs/stream-tool-input.jsonl",
  "tools": {
    "AskUserQuestion": {
      "items_key": "questions",
      "copy_within_items": [{"src": "header", "dst": "question"}]
    }
  }
}
```
- `probe_only: true` → 只落盘重组后的 input + 诊断，不改写。用于核对真实 SSE 结构。
- `tools` 可扩展到任意工具/字段规则。
- `audit_file` → 真正补全时记 `{"_diag":"patched","tool":...}`，生产模式(probe_only 关)也记，用于线上验收「补全是否真触发」。

## 进阶：泄漏 invoke → tool_use 转换

`convert_text_invoke` 是本状态机的高风险扩展——把泄漏进 text block 的 `<invoke>` 转成真正的 tool_use block（拆分 block、index 偏移传播、stop_reason 改写、白名单防误伤）。详见 `invoke-conversion.md`。

## 给流式路径新增一个 text-block 消费者的安全纪律（血泪版）

`degen_trim` 的 `mode="live"`（边流边去重 `LiveDedup`）是继 `convert_text_invoke` 之后第二个「不整块缓冲、边流边改写 text block」的消费者。它经**三轮对抗评审、逐轮揪出 4 个 BLOCKER**才磨对。任何将来往流式路径加这类消费者(截断/去重/改写/注入)的人,先按这份清单自查——这些坑单测很难先想到,但线上一旦命中就是丢内容或客户端 SSE 崩。

### 铁律一:每一条退出路径都必须闭合 block

一个 text block 一旦发了 `content_block_start`,就**必须**在某处发对应的 `content_block_stop`,否则客户端 SSE 状态机悬挂(非法流)。边流边改写意味着 block 可能在很多「异常退出点」半开着:

- 上游违约:`message_delta` 早于 `content_block_stop` 到达
- 上游违约:旧 block 没发 stop 就来新 `content_block_start`(任意类型:text/tool_use/thinking/未知)
- block 内出现非 `text_delta` 的 delta(如合法 `citations_delta`)
- 流末尾截断(EOF,无 stop)
- 生成器内部异常(outer `except`)

**做法**:抽一个统一收尾函数(如 `_live_close_events()`:flush 尾段 + 补一个 stop,返回要 yield 的 chunk 列表),在**上述每一条**路径调用它。**正常收到上游真 stop 时不走它**(转发真 stop 即闭合,别双补)。验收 oracle 用 `block_audit.py` 的 `BlockSeqAuditor`,断言每条 fixture 输出 `start==stop`、无 `unclosed/orphan/double-stop`——比人肉数事件靠谱。

### 铁律二:「禁用改写」≠「块已关」,别用一个 None 表达两件事

最隐蔽的一个 BLOCKER:改写器出错时若 `consumer = None` 同时表达「停止改写」和「块已关」,那么**降级后**再遇到 EOF/message_delta/新 start,收尾守卫(`if consumer is not None`)就失效,块永远补不上 stop。

**做法**:拆分状态。给消费者一个 `disable()`(停改写、**保留实例**,后续原样透传)而非置 None;`consumer is not None` 只表示「块仍开」。降级路径:取回未提交内容 + `disable()`,实例仍在 → 收尾守卫照常补 stop。合法非 `text_delta`(如 `citations_delta`)同理:flush 已缓冲文本 + `disable()` + **不提前补 stop**(保持块开),透传该 delta,交真 stop 收尾——**别**把 citations_delta 当块异常去补 stop,那会造出 orphan/双 stop。

### 铁律三:异常安全 = 绝不吞异常丢内容(feed 与 flush 都要事务化)

「never-swallow-errors / 绝不丢内容」是硬规则。改写器内部往往有「已提交的内部缓冲」(如 `_pending` 尾段),降级时最容易把它蒸发:

- **消费者的 `feed()` 与 `flush()` 都做 pending 提交事务化**:异常时**不清空/回滚** `pending`,使调用方能原样取回。
- **提供一个不会抛的 `drain_raw()`**(只读取+清空 pending,不经任何处理逻辑),降级路径一律用它取回内容,**别**用会再抛的 `flush()`。
- **收尾函数吞 flush 异常时也用 `drain_raw()` 兜底**,不是 `rem=""`(那就是吞异常丢内容)。
- 闭合序列**先构造完整帧列表(delta+stop)再整体 yield**,避免「已发半截帧才处理异常」的半提交。
- 入口校验:非 str 的 `text` 先强转,别让 `pending += text` 抛。

自查口诀:**「任一单点异常 → 内容是否仍完整外发 + 块是否仍闭合?」** 对 feed 抛、flush 抛、_consume 抛各注入一次 failpoint 测试(mock 抛异常),用 `BlockSeqAuditor` + 文本断言证明不丢、不崩、不悬挂。

### 铁律四:每个 block 一个新消费者实例;跨 block 状态必清

流式检测器(游程计数等)有状态。**每个 text `content_block_start` 起一个新实例**,在正常 stop / 防御 flush / 非 text delta / 流末 / 异常**所有**收尾点废弃它。否则「block 0 尾部 9 个重复 + block 1 首个重复」会被误当成第 10 个而误触发。

### 与既有缓冲/转换的互斥

边流边改写(不整块缓冲)与需要**整块缓冲**的消费者(如 `convert_text_invoke` 靠全块检测 `<invoke>`)**互斥**。互斥时要**显式 warning + 明确回落**(别静默),并在文档写清「同开时谁生效」。用 `text_buffer_on = not <streaming_mode>` 之类开关切换两条路径,新分支**附加**、不动已验证的缓冲路径。

> 实例:`LiveDedup`(纯状态机,`degen.py`)+ stream `mode="live"` 分支;决策与三轮评审处置见 `~/.claude/litellm/docs/plan/degeneration-cutoff.md`。默认 `mode="buffered"`,现有路径不动。
