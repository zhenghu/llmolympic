"""OpenAI 适配器（官方 SDK）。

key / base_url 依次取自：构造参数 > 环境变量 > config.toml。
"""

from __future__ import annotations

from llmolympic.config import get as cfg_get
from llmolympic.providers.base import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    Provider,
    ProviderTimeoutError,
)


class OpenAIProvider(Provider):
    name = "openai"

    _COMPLETION_TOKEN_MODELS = ("o1", "o3", "o4", "gpt-5")

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        # 延迟导入，无 key 的环境也能加载其他 provider
        from openai import AsyncOpenAI, OpenAI

        resolved_api_key = api_key or cfg_get("openai", "api_key", env="OPENAI_API_KEY")
        resolved_base_url = base_url or cfg_get("openai", "base_url", env="OPENAI_BASE_URL")
        self._client = OpenAI(api_key=resolved_api_key, base_url=resolved_base_url)
        self._async_client = AsyncOpenAI(api_key=resolved_api_key, base_url=resolved_base_url)

    @classmethod
    def _completion_params(cls, params: dict, *, model: str) -> dict:
        limited = dict(params)
        if "max_tokens" not in limited and "max_completion_tokens" not in limited:
            model_name = model.rsplit("/", 1)[-1].lower()
            uses_completion_tokens = any(
                model_name == prefix
                or model_name.startswith((f"{prefix}-", f"{prefix}."))
                for prefix in cls._COMPLETION_TOKEN_MODELS
            )
            limit_key = "max_completion_tokens" if uses_completion_tokens else "max_tokens"
            limited[limit_key] = DEFAULT_MAX_OUTPUT_TOKENS
        return limited

    def chat(
        self,
        messages: list[dict],
        *,
        model: str,
        request_timeout: float | None = None,
        **params,
    ) -> str:
        from openai import APITimeoutError

        client = self._client
        if request_timeout is not None:
            client = client.with_options(timeout=request_timeout, max_retries=0)
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                **self._completion_params(params, model=model),
            )
        except APITimeoutError as exc:
            raise ProviderTimeoutError("OpenAI 请求超时") from exc
        return (resp.choices[0].message.content or "").strip()

    async def achat(
        self,
        messages: list[dict],
        *,
        model: str,
        request_timeout: float | None = None,
        **params,
    ) -> str:
        from openai import APITimeoutError

        client = self._async_client
        if request_timeout is not None:
            client = client.with_options(timeout=request_timeout, max_retries=0)
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=messages,
                **self._completion_params(params, model=model),
            )
        except APITimeoutError as exc:
            raise ProviderTimeoutError("OpenAI 请求超时") from exc
        return (resp.choices[0].message.content or "").strip()
