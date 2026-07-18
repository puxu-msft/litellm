# 官方 Hook 点索引 / Official Hook Points

litellm 版本参照 1.91.3（`uv tool` 装于 `~/.local/share/uv/tools/litellm/lib/python3.13/site-packages/litellm/`）。行号随版本漂移，用符号名定位更稳。

## 注册（config.yaml）

```yaml
litellm_settings:
  callbacks: hooks.proxy_handler_instance
```

`proxy_handler_instance` 是一个 `CustomLogger` 子类实例。这是**官方文档机制**，不是野路子。

## 五个 hook 的签名与触发点

全部从 `litellm/integrations/custom_logger.py` 核实。

### 1. `async_pre_call_hook`（转换前）
```python
async def async_pre_call_hook(self, user_api_key_dict, cache, data: dict, call_type) -> Optional[dict]
```
- 定义：`custom_logger.py:357`
- 触发：`proxy/utils.py` 的 `pre_call_hook`，遍历 `litellm.callbacks` 里 `isinstance(cb, CustomLogger)` 且 `vars(cls)` 含本方法者（`proxy/utils.py:~1421`）。
- `data` 是**转换前**的原始载荷。`anthropic_messages` 路由下是 Anthropic content 块格式。
- 返回非 None 替换 `data`；返回 dict 也可（`common_request_processing.py:1149` 把返回值赋回 `self.data`，一路传到 `route_request`）。

### 2. `async_pre_call_deployment_hook`（转换后/发出前）★覆盖盲区
```python
async def async_pre_call_deployment_hook(self, kwargs: dict, call_type) -> Optional[dict]
```
- 定义：`custom_logger.py:262`
- 触发：`utils.py` 顶层 `async_pre_call_deployment_hook`（约 1139-1157）里 `for callback in litellm.callbacks: if isinstance(callback, CustomLogger): result = await callback.async_pre_call_deployment_hook(modified_kwargs, typed_call_type)`；**位置参数**调用。返回非 None 替换 kwargs。
- 上游触发点在 `wrapper_async`（`utils.py:~1606`，`@client` 装饰 `acompletion`）。转换 handler 内部 `await litellm.acompletion(**completion_kwargs)`（`adapters/handler.py:587`）会走到这。
- `kwargs["messages"]` 是**转换后**的 OpenAI 格式（`tool_calls` + `role:"tool"`）。**这是 pre_call_hook 看不到的盲区。**
- 重试（`completion_with_retries`）会对同一 `litellm_call_id` 重跑本 hook → 用 call_id 去重避免重复观测。

### 3. `async_post_call_failure_hook`（失败时）
```python
async def async_post_call_failure_hook(self, request_data, original_exception, user_api_key_dict, traceback_str=None) -> Optional[HTTPException]
```
- 定义：`custom_logger.py:396`
- 触发：`proxy/utils.py:~2076`，**关键字**调用。
- 返回 `HTTPException` 才替换错误响应；返回 None 用原始异常（观测型安全）。
- `request_data` 是**转换前** Anthropic 载荷（用 content 块 + tool_use/tool_result，不是 tool_calls）——观测孤儿时要用 Anthropic 版检测，别用 OpenAI 版（否则恒空）。

### 4. `async_post_call_success_hook`（成功后）
```python
async def async_post_call_success_hook(self, data, user_api_key_dict, response) -> Any
```
- 定义：`custom_logger.py:418`；触发：`proxy/utils.py:~2318` 关键字调用；返回非 None 替换 response。
- **流式响应通常不走这里**（走 streaming hook），故对流式基本抓不到数据。

### 5. `async_post_call_streaming_iterator_hook`（流式响应）★改流
```python
async def async_post_call_streaming_iterator_hook(self, user_api_key_dict, response, request_data: dict)
```
- 定义：`custom_logger.py:449`（默认体是 `async for item in response: yield item`）。
- 触发：`proxy/utils.py:2494` 的同名方法，**先检查 `caps.iterator_overrides`**（`proxy/utils.py:2513` `if not caps.iterator_overrides:` 走 fast-path 直接透传）。
- **能力检测靠 `vars(cls)`**：`proxy/utils.py:1650` `if "async_post_call_streaming_iterator_hook" in cls.__dict__: has_iterator_override = True`。
- **⚠️ 陷阱：必须把本方法直接定义在 hook 类上**（不能只继承）。否则 `cls.__dict__` 检测不到，走 fast-path，你的代码永不执行。
- `anthropic_messages` 流式路由经 `async_sse_data_generator`（`common_request_processing.py:1560`）→ 内部 `async for chunk in proxy_logging_obj.async_post_call_streaming_iterator_hook(...)`（`common_request_processing.py:2465`）。确认覆盖。
- 迭代的 chunk 是 **Anthropic 事件 dict**：`content_block_start`/`content_block_delta`(delta.type=input_json_delta, partial_json 分片)/`content_block_stop`/message_start/stop。

## 能力检测缓存

`ProxyLogging._callback_capabilities()`（`proxy/utils.py:1600`）缓存键是 `(len(callbacks), tuple(id(c) for c in callbacks))`。同一实例（`proxy_handler_instance`）id 不变 → 缓存稳定命中。热重载(reload hookpkg)不改实例 id/类，能力检测不受影响。

## 薄壳 + 热重载模式

- `hooks.py`：稳定薄壳，`HookShim(CustomLogger)`，委托到 `hookpkg` 包。每个 hook 方法调 `hookpkg.<fn>`。
- `hookpkg/`：多文件包,真正逻辑。改后发 **SIGUSR2**(`./reload.sh`)→ 下次请求拓扑 reload 生效(进行中的流不受影响)。详见 `package-architecture.md`。
- **但**：在 `hooks.py` 薄壳里**新增 hook 方法**（如首次加 streaming_iterator_hook）**必须重启 litellm**——litellm 启动时只加载一次薄壳类，`vars(cls)` 在那时固定。
- 历史注:早期是单文件 `hook_impl.py` + mtime 热重载,后重构成多文件包 + SIGUSR2(mtime 不递归子模块)。
