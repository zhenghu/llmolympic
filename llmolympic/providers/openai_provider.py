"""OpenAI 适配器（官方 SDK）。

key / base_url 依次取自：构造参数 > 环境变量 > config.toml。
"""

from __future__ import annotations

from llmolympic.config import get as cfg_get
from llmolympic.providers.base import Provider, ProviderTimeoutError


class OpenAIProvider(Provider):
    name = "openai"

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        # 延迟导入，无 key 的环境也能加载其他 provider
        from openai import AsyncOpenAI, OpenAI

        resolved_api_key = api_key or cfg_get("openai", "api_key", env="OPENAI_API_KEY")
        resolved_base_url = base_url or cfg_get("openai", "base_url", env="OPENAI_BASE_URL")
        self._client = OpenAI(api_key=resolved_api_key, base_url=resolved_base_url)
        self._async_client = AsyncOpenAI(api_key=resolved_api_key, base_url=resolved_base_url)

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
            resp = client.chat.completions.create(model=model, messages=messages, **params)
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
            resp = await client.chat.completions.create(model=model, messages=messages, **params)
        except APITimeoutError as exc:
            raise ProviderTimeoutError("OpenAI 请求超时") from exc
        return (resp.choices[0].message.content or "").strip()
