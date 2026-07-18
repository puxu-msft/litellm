# Degeneration cutoff abort PoC

## 假设与判据

假设 H：`stream_transform` 停止消费并结束后，关闭动作能逐层抵达底层 HTTP 流，令 GitHub Copilot 停止继续生成 token

成功判据：在真实 LiteLLM 包装层中，提前停止后哨兵 `produced << total`，且无需额外拿到底层对象显式调用 `aclose()`，底层哨兵已观察到 `GeneratorExit`／close，`httpx.Response.is_closed` 为 `True`。正常读到底作为正样本，必须得到 `produced == total`

## 运行

```bash
cd /home/xp/refs/ai-agents/litellm
uv run python /home/xp/refs/ai-agents/litellm/exp/degen-cutoff-abort/probe.py
```

探针固定使用 100 个哨兵 chunk，并在第 5 个外层 chunk 停止。覆盖三条路径：

1. `CustomStreamWrapper -> AnthropicStreamWrapper -> async_anthropic_sse_wrapper -> stream_transform`
2. 真实 `openai.AsyncStream -> CustomStreamWrapper -> AnthropicStreamWrapper -> async_anthropic_sse_wrapper -> stream_transform`
3. Copilot 原生 Messages 路径的 `httpx.Response -> PassThroughStreamingHandler.chunk_processor -> stream_transform`

## 实测结论

正常读到底时，adapter 与 native HTTP 两组都得到 `produced=100, closed=True`，证明对照能识别“读到底”

提前停止时，在尚未显式关闭实际网络 owner 前，三组均仅拉取 3 或 5 个 chunk，但均为 `closed=False`。尤其 `await async_anthropic_sse_wrapper.aclose()` 以及 `await PassThroughStreamingHandler.chunk_processor(...).aclose()` 后，底层仍为 `closed=False`；因此 Python 的嵌套 `async for`／async generator `aclose()` 不会自动级联关闭被迭代对象

显式调用网络 owner 后才关闭：adapter/OpenAI SDK 路径调用 `CustomStreamWrapper.aclose()` 后，哨兵收到 `GeneratorExit` 且真实 `httpx.Response.is_closed=True`；原生 Messages 路径调用 `httpx.Response.aclose()` 后达到同样结果。所有提前停止用例均保持 `produced << 100`，没有 drain 或 prefetch 到结束

## H 判定

H 对“`stream_transform` 自己停止／它的生成器被 `aclose()` 会自然一路传播到底层”这一命题不成立。阻断不是 drain、prefetch、`GeneratorExit` 被捕获或 `asyncio.shield`，而是 Python 异步迭代协议本身不拥有下层资源：多个 `async for` 透传层没有 `finally: await upstream.aclose()`，所以关闭外层 generator 只关闭外层

当前 proxy 的外层清理虽会对传入 proxy generator 的 `response` 调用 `aclose()`，但本链路中的该对象不是网络 owner：adapter 路径拿到的是 `async_anthropic_sse_wrapper()` generator，原生 Messages 路径拿到的是 `chunk_processor()` generator。探针分别显式关闭这两个对象，底层仍未关闭。因此现有 proxy cleanup 不能补偿这两个缺口；只有直接调用 `CustomStreamWrapper.aclose()` 或原始 `httpx.Response.aclose()` 才成功关闭

## 风险与建议

不要依赖 `GeneratorExit` 自然级联。正式实现应把“检测退化”建模为显式 abort，并确保资源 owner 的 `aclose()` 在 shielded `finally` 中被调用；至少增加一条 proxy 集成测试，直接断言底层 `httpx.Response.is_closed`。保留 drain 仅作关闭失败时的兼容回退，不应作为默认策略
