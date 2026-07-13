"""GitHub Copilot CountTokens handler + token counter + model info.

让 github_copilot/claude-* 的 /v1/messages/count_tokens 打到 copilot 原生端点
(实测存在:https://api.githubcopilot.com/v1/messages/count_tokens 返回 {"input_tokens":N}),
得到精确 token 数,而非本地 tokenizer 估算。

复用 copilot 现成的 Authenticator + get_copilot_default_headers。
"""
import os
from typing import Any, Dict, List, Optional

import httpx

from litellm._logging import verbose_logger
from litellm.llms.base_llm.base_utils import BaseLLMModelInfo, BaseTokenCounter
from litellm.llms.custom_httpx.http_handler import get_async_httpx_client
from litellm.llms.github_copilot.authenticator import Authenticator
from litellm.llms.github_copilot.common_utils import (
    DEFAULT_GITHUB_COPILOT_API_BASE,
    get_copilot_default_headers,
)
from litellm.types.utils import LlmProviders, TokenCountResponse

_authenticator = Authenticator()


def _copilot_count_tokens_url(api_base: Optional[str] = None) -> str:
    base = api_base
    if not base:
        try:
            base = _authenticator.get_api_base()
        except Exception:
            base = None
    if not base:
        base = DEFAULT_GITHUB_COPILOT_API_BASE
    base = base.rstrip("/")
    if base.endswith("/v1/messages/count_tokens"):
        return base
    if base.endswith("/v1/messages"):
        return base + "/count_tokens"
    return base + "/v1/messages/count_tokens"


class GitHubCopilotCountTokensHandler:
    """打 copilot 原生 /v1/messages/count_tokens 端点。"""

    async def handle_count_tokens_request(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[Any] = None,
    ) -> Dict[str, Any]:
        try:
            copilot_api_key = api_key or _authenticator.get_api_key()
        except Exception as e:
            raise RuntimeError(f"GitHub Copilot count_tokens: no api key ({e})")

        headers = get_copilot_default_headers(copilot_api_key)
        headers.setdefault("anthropic-version", "2023-06-01")

        body: Dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            body["tools"] = tools
        if system is not None:
            body["system"] = system

        url = _copilot_count_tokens_url(api_base)
        client = get_async_httpx_client(llm_provider=LlmProviders.GITHUB_COPILOT)
        resp = await client.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            raise RuntimeError(
                f"GitHub Copilot count_tokens HTTP {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json()


_handler = GitHubCopilotCountTokensHandler()


class GitHubCopilotTokenCounter(BaseTokenCounter):
    """用 copilot 原生 count_tokens 端点计数。"""

    def should_use_token_counting_api(self, custom_llm_provider: Optional[str] = None) -> bool:
        return custom_llm_provider == LlmProviders.GITHUB_COPILOT.value

    async def count_tokens(
        self,
        model_to_use: str,
        messages: Optional[List[Dict[str, Any]]],
        contents: Optional[List[Dict[str, Any]]],
        deployment: Optional[Dict[str, Any]] = None,
        request_model: str = "",
        tools: Optional[List[Dict[str, Any]]] = None,
        system: Optional[Any] = None,
    ) -> Optional[TokenCountResponse]:
        if not messages:
            return None
        litellm_params = (deployment or {}).get("litellm_params", {})
        api_key = litellm_params.get("api_key")
        api_base = litellm_params.get("api_base")
        try:
            result = await _handler.handle_count_tokens_request(
                model=model_to_use, messages=messages, api_key=api_key,
                api_base=api_base, tools=tools, system=system,
            )
            if result is not None:
                return TokenCountResponse(
                    total_tokens=result.get("input_tokens", 0),
                    request_model=request_model,
                    model_used=model_to_use,
                    tokenizer_type="github_copilot_api",
                    original_response=result,
                )
        except Exception as e:
            verbose_logger.warning(f"GitHub Copilot CountTokens error: {e}")
            return TokenCountResponse(
                total_tokens=0, request_model=request_model, model_used=model_to_use,
                tokenizer_type="github_copilot_api", error=True,
                error_message=str(e), status_code=500,
            )
        return None


class GitHubCopilotModelInfo(BaseLLMModelInfo):
    """最小 ModelInfo:主要为提供 get_token_counter。其余抽象方法委托 copilot chat config
    (GithubCopilotConfig 继承 OpenAIGPTConfig,已实现 get_models/get_api_key/get_api_base/
    get_base_model/validate_environment)。"""

    def _chat(self):
        from litellm.llms.github_copilot.chat.transformation import GithubCopilotConfig
        return GithubCopilotConfig()

    def get_models(self, api_key: Optional[str] = None, api_base: Optional[str] = None) -> List[str]:
        try:
            return self._chat().get_models(api_key=api_key, api_base=api_base)
        except Exception:
            return []

    @staticmethod
    def get_api_key(api_key: Optional[str] = None) -> Optional[str]:
        if api_key:
            return api_key
        try:
            return _authenticator.get_api_key()
        except Exception:
            return None

    @staticmethod
    def get_api_base(api_base: Optional[str] = None) -> Optional[str]:
        if api_base:
            return api_base
        try:
            return _authenticator.get_api_base()
        except Exception:
            return DEFAULT_GITHUB_COPILOT_API_BASE

    def validate_environment(self, headers: dict, model: str, messages: List[Any],
                             optional_params: dict, litellm_params: dict,
                             api_key: Optional[str] = None, api_base: Optional[str] = None) -> dict:
        try:
            return self._chat().validate_environment(
                headers=headers, model=model, messages=messages,
                optional_params=optional_params, litellm_params=litellm_params,
                api_key=api_key, api_base=api_base)
        except Exception:
            return headers

    @staticmethod
    def get_base_model(model: Optional[str] = None) -> Optional[str]:
        if not model:
            return None
        return model.split("/", 1)[1] if "/" in model else model

    def get_token_counter(self) -> Optional[BaseTokenCounter]:
        return GitHubCopilotTokenCounter()
