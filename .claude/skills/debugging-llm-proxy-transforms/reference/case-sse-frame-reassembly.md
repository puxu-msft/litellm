# 案例：SSE 帧被 chunk 边界劈开 → `JSON Parse error: Unexpected identifier "event"`

## 症状

客户端（Claude Code）报 `API Error: JSON Parse error: Unexpected identifier "event"`。`event` 是 SSE 流的第一个 token（`event: <type>\ndata: {...}\n\n`）——所以这是**客户端把一段 SSE 文本喂进了 `JSON.parse`**，而那段文本以 `event:` 开头。

区别于同族的 `Unterminated string`：后者是**半截 JSON 串**（`data: {...,"text":"hel`，字符串未闭合），本案是**半截 `event:` 帧**（一个完整帧后面跟了下一帧的开头 `event: content_block_delta\nda`）。两者根因、修法都不同。

## 定位（按第一原则：先证是哪一层，别臆断）

1. **确认走代理**：`ANTHROPIC_BASE_URL` 指向本地代理（本例经 Caddy 4143 → litellm）。
2. **看审计落盘**（真相源，不是客户端 transcript）：`stream-patched.jsonl` 里 `dropped_truncated_frame` 的 `head` 结尾是 `...content_block_delta"}\n\nevent: content_block_delta\nda`——一个 chunk 里「上一帧的尾 + 下一帧被劈断的头」。
3. **读被丢 chunk 的 head 开头**：形如 `lan 之前...）\\n\\n- **Claude",...`——它**开头就是半截 JSON 值的续尾**（上一个 chunk 把 `data: {"partial_json":"...某文本` 切断了，这个 chunk 是它的后半）。**证明：一帧被劈到两个 chunk。**
4. **排除自己的改动**：`git show <base>:...` 确认相关路由/落点在本次改动前就已存在；`git diff --name-only <base>..HEAD` 确认自己没碰流式/SSE/adapters 层。**相关不等于因果，用 git 坐实。**

## 根因

`hookpkg/sse.py` 的 `sse_parse` / `is_truncated_json_frame` 是**纯函数、逐 chunk、无跨 chunk 状态**，且 `sse_parse` **只取第一个 `data:` 行**。于是上游（copilot 双重转换 + httpx 分块）按网络边界产出的 bytes 一旦不对齐 SSE 帧边界：

- 帧被劈到两个 chunk → 前半 chunk `sse_parse` 失败被 `is_truncated_json_frame` 丢弃（丢内容），后半 chunk 开头是 JSON 续尾、也解析失败 → 被丢或半截透传给客户端（`Unexpected identifier "event"`）。
- 多帧挤进一个 chunk → `sse_parse` 只返回第一帧，后续帧被丢。

**block/事件层缓冲（`buf_partial` 等）救不了这层**：它作用在「已解析事件」上，劈帧根本 parse 不出事件，永远到不了那层。这是关键误区——「我们有 buffering 为什么没生效」的答案是「层不对」。

## 修复：流入口做字节级帧重组

在主循环**之前**插一个 async 包装器，把 chunk 流重组成「一 chunk 一完整帧」：

```python
async def _reassemble_sse_frames(response, ctx=None):
    carry = b""; emit_str = False; stitched = 0
    async for chunk in response:
        if carry and not isinstance(chunk, dict):
            stitched += 1                       # 上个 chunk 留了半截,本 chunk 在续接
        if isinstance(chunk, (bytes, bytearray)):
            carry += bytes(chunk)
        elif isinstance(chunk, str):
            carry += chunk.encode("utf-8"); emit_str = True
        else:                                   # dict 等非 SSE 文本:吐 carry 保序 + 透传
            if carry:
                yield carry.decode("utf-8","replace") if emit_str else carry; carry = b""
            yield chunk; continue
        while b"\n\n" in carry:                 # 只下发完整帧
            frame, carry = carry.split(b"\n\n", 1)
            full = frame + b"\n\n"
            yield full.decode("utf-8","replace") if emit_str else full
    if carry:                                   # 流末尾残留:真·截断,照原样交下游 is_truncated 丢弃
        yield carry.decode("utf-8","replace") if emit_str else carry
    if stitched and ctx is not None:
        ctx.audit("reassembled_split_frame", n=stitched)
```

要点：
- **carry 用 bytes**（不是 decode 后的 str）：否则 chunk 边界劈开多字节 UTF-8 字符时 `decode(errors="replace")` 会把半个字符替换掉、拼不回。只在「已是完整帧」时才 decode。
- **dict chunk 透传**：litellm 有时 yield 已解析事件 dict，不是 SSE 文本，别当帧切。
- **真·流末尾截断保留既有丢弃**：重组器只保证「完整帧成帧」，流真断在半截时把残留交给下游 `is_truncated_json_frame`（那条逻辑对「后面没 chunk 了」的场景仍正确）。
- 插入位置在 `ctx` 建好之后、主 `async for` 之前：`response = _reassemble_sse_frames(response, ctx)`。

## 验证（正样本对照 + 正向可观测量）

1. **先证 bug 真实**（红）：构造「把一帧劈成两个 chunk」「两帧挤一个 chunk」的字节流喂进当前代码，断言文本丢失（`'' != 'alpha beta gamma'`、`'one ' != 'one two'`）。遍历**整条 blob 的每个字节切点**都不丢文本的 property 测试最狠（`test_many_random_cuts_never_lose_text`）。
2. **再证修复**（绿）：加重组后全绿，且真·流末尾截断仍被丢弃（回归不破）。
3. **上线坐实 live**（对抗 SIGUSR2 静默失效）：`./reload.sh` 后在真实流量上确认 `reassembled_split_frame`（只有新代码产生）出现、`dropped_truncated_frame` 随之停。本例实测 reload 后 claude-opus/sonnet 每流劈 1~4 帧全被拼回。

## 一句话

逐 chunk 的 SSE 解析器**天生**拼不回被网络 chunk 边界劈开的帧；凡是「代理逐块处理 SSE」的地方，帧重组必须是**流入口第一步**，早于任何事件/block 层的缓冲与改写。
