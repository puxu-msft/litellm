"""litellm hook —— 稳定薄壳(shim)。

**本文件极简且尽量不改。** litellm 启动时加载一次。真正逻辑在 `hookpkg/` 包内。

热重载:改包内代码后发 `SIGUSR2` 给 litellm 进程,下一次请求时整个包按拓扑顺序 reload
(进行中的流不受影响)。触发:

    kill -USR2 $(pgrep -f "bin/python.*litellm")

或用 `reload.sh`。多文件包不再用 mtime 轮询。

⚠️ 仅限本地开发/调试/问题缓解用途。

注册(config.yaml)::

    litellm_settings:
      callbacks: hooks.proxy_handler_instance
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("litellm.hooks.shim")

# 确保包可被 import:litellm 从 cwd 加载 hooks 模块,包与本文件同目录。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import hookpkg  # noqa: E402
from hookpkg import reload as _reload  # noqa: E402

_reload.install_signal_handler()


def _impl():
    """返回实现包,先按需(收到 SIGUSR2 后)拓扑 reload。reload 失败保留旧模块。"""
    try:
        _reload.maybe_reload()
    except Exception as e:
        logger.warning("hookpkg reload check failed (%r)", e)
    return hookpkg


class HookShim(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data: dict, call_type: str) -> dict:
        impl = _impl()
        try:
            return impl.process(data, call_type)
        except Exception as e:
            logger.warning("hookpkg.process failed (%r), passing through", e)
            return data

    async def async_pre_call_deployment_hook(self, kwargs: dict, call_type: Any):
        impl = _impl()
        try:
            return impl.process_deployment(kwargs, call_type)
        except Exception as e:
            logger.warning("hookpkg.process_deployment failed (%r), passing through", e)
            return None

    async def async_post_call_failure_hook(self, request_data: dict, original_exception: Exception,
                                           user_api_key_dict: Any, traceback_str: Any = None):
        impl = _impl()
        try:
            impl.observe_failure(request_data, original_exception, traceback_str)
        except Exception as e:
            logger.warning("hookpkg.observe_failure failed (%r)", e)
        return None

    async def async_post_call_success_hook(self, data: dict, user_api_key_dict: Any, response: Any):
        impl = _impl()
        try:
            impl.observe_success(data, response)
        except Exception as e:
            logger.warning("hookpkg.observe_success failed (%r)", e)
        return None

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        """成功后打一行请求访问日志(model/provider/上下行 token/用时/结束原因/stream/call_type)。
        与 observe_success 不同:此钩子拿到 standard_logging_object(含聚合 usage)与真实用时,
        流式与非流式都在完成后触发一次。"""
        impl = _impl()
        try:
            impl.log_success(kwargs, response_obj, start_time, end_time)
        except Exception as e:
            logger.warning("hookpkg.log_success failed (%r)", e)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        impl = _impl()
        try:
            impl.log_failure(kwargs, response_obj, start_time, end_time)
        except Exception as e:
            logger.warning("hookpkg.log_failure failed (%r)", e)

    async def async_post_call_streaming_iterator_hook(self, user_api_key_dict: Any, response: Any,
                                                      request_data: dict):
        """流式响应迭代器。必须直接定义在本类上——litellm 靠 `vars(cls)` 检测覆写。"""
        impl = _impl()
        transform = getattr(impl, "stream_transform", None)
        if transform is None:
            async for chunk in response:
                yield chunk
            return
        try:
            async for chunk in transform(response, request_data):
                yield chunk
        except Exception as e:
            # transform 已消费的上游无法重放;不再触碰 response(见其内部收尾逻辑)。
            logger.warning("hookpkg.stream_transform failed (%r); stream ended by transform", e)
            return


proxy_handler_instance = HookShim()
