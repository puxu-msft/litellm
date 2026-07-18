# 流式生命周期与掐断上游 / Stream Lifecycle & Upstream Cancellation

**一句话**:在本代理的 hook 层(`stream_transform`)**无法**靠 `return`/`break`/`aclose()` 掐断到 GitHub Copilot 后端的上游 HTTP 流——Python async generator 的 `aclose()` **不向下级联**,而整条包装链没有一层 `finally: await owner.aclose()`。要真掐断(取消/超时/退化早停以省 token)**必须动 litellm 引擎**、拿到真正的 owner(`httpx.Response`)显式关闭。

本结论来自一次 PoC + 独立复现(`~/refs/ai-agents/litellm/exp/degen-cutoff-abort/`,`ASSERTIONS: PASS`)。任何将来想在响应侧「提前结束流 / 取消上游 / 按预算截断」的工作,先读这篇,别重复踩坑。

## 为什么从 hook 掐不断(核心机制)

opus/claude 经 copilot 走的是**原生 anthropic messages 路径**(`GithubCopilotAnthropicMessagesConfig(AnthropicMessagesConfig)` + `BaseLLMHTTPHandler`),真正持有网络连接的 owner 是 `httpx.Response`。我们的 `stream_transform` 只拿到最外层的包装 generator(`chunk_processor()` 的产物,再经 iterator-hook 链),**够不到那个 owner**。

链路逐层都是 `async for chunk in <上一环>: yield ...` 的纯透传,**没有任何一层**在收尾 `await <上一环/owner>.aclose()`:

| 环 | 源码(fork) | 关闭传播 |
|---|---|---|
| 我们的 hook | `hookpkg/stream.py` `async for chunk in response` | `except` 不接 GeneratorExit;无 `finally` 关 response |
| iterator-hook 链 | `litellm/proxy/utils.py` `async_post_call_streaming_iterator_hook` | 纯 `async for`,不关上一环 |
| 原生流处理 | `litellm/proxy/pass_through_endpoints/streaming_handler.py` `chunk_processor` | `finally` 只调度日志,**不 `response.aclose()`** |
| Anthropic SSE 包装(adapter 路径) | `.../adapters/streaming_iterator.py` `async_anthropic_sse_wrapper` | 无 `finally` close |
| httpx | `aiter_bytes/aiter_raw` | 只在**正常读到底**时末尾 `aclose()`;提前关外层 gen 不触发它 |

**Python 语义铁律(PoC 实测)**:对一个 `async for x in inner: yield f(x)` 形态的 async generator 调用 `gen.aclose()`,会向 gen 抛 `GeneratorExit`,但**不会**自动 `inner.aclose()`。故关最外层 = 不关底层。实测:`stream_transform` return 后 `httpx_response_is_closed == False`,连接仍开、copilot 可能续生成、token 照烧。只有显式 `httpx.Response.aclose()`(或 adapter/openai-sdk 路径的 `CustomStreamWrapper.aclose()`)才实测关闭。

> 佐证:litellm 自己在 `common_request_processing.py` 的注释也写「嵌套 iterator hook 只在 GC 时才见到 GeneratorExit」——即靠 GC 关,时机不确定。

## 两条连接、两种行为(别混)

- **客户端↔代理**(Claude Code ↔ litellm):`stream_transform` `return` → 外层 `async for` 结束 → StreamingResponse 完成 → **关客户端连接(EOF)**。所以 hook 里 `return` 能确定让**客户端**收尾/解冻,无论客户端按 `message_stop` 还是按 EOF 结束。litellm **不会**在我们 return 后追加 `[DONE]`/`message_stop`——终止帧要我们自己发,且不会重复。
- **代理↔copilot**(litellm ↔ 上游):hook return **不关**这条,靠请求结束时的 GC/teardown 收。但 **TCP 背压**在此帮忙:一旦我们停止读取,copilot 的写阻塞、生成随之停滞(填满 socket 缓冲后卡住,不是瞬停)。所以「停止读取」能顺带压制大部分 token 生成,只是不确定、不即时。

## 要真掐断怎么做(Phase 2 蓝图)

不是「hook 里 return」,而是**引擎侧改动**把 owner 的关闭能力接到 hook:

1. 让原生路径的 `chunk_processor`(或其上一环)把 `httpx.Response` owner 暴露给 hook(contextvar / `request_data` 挂载 / 加一层 `finally: await response.aclose()` 的关闭传播),或
2. hook 检测到该掐断时,直接 `await owner.aclose()`。

本仓是 fork(永不上游、GHC-only),直接改引擎即可。此路**需单独 PoC**验证「owner 引用怎么穿到 hook」+ 覆盖原生 messages 路径的集成测试。**注意 block_audit 污染**:主动 abort 后入站看不到真实 `content_block_stop`,会被判 `unclosed`——需传 `termination="abort"` 之类标记把 intentional abort 与真故障区分。

## 与「退化早停」的关系(为什么最终没做 abort)

退化重复(`court\n\n×`几万)最初想「检测到就掐断上游省 token」。但**上游模型会自己恢复、产出有效数据**,掐断会丢恢复数据——与「保留有效内容」冲突。故最终选**去重不掐断**(buffered fold / live 去重,见 `degeneration-cutoff.md` 决策记录),token 那块搁置。abort 蓝图仅在「已确认不会恢复」的未来场景才重启。
