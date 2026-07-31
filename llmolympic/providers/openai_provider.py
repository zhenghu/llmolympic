"""OpenAI 适配器（官方 SDK）。

key / base_url 依次取自：构造参数 > 环境变量 > config.toml。
"""

from __future__ import annotations

from llmolympic.config import get as cfg_get
from llmolympic.providers.base import Provider


class OpenAIProvider(Provider):
    name = "openai"

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        from openai import OpenAI  # 延迟导入，无 key 的环境也能加载其他 provider

        self._client = OpenAI(
            api_key=api_key or cfg_get("openai", "api_key", env="OPENAI_API_KEY"),
            base_url=base_url or cfg_get("openai", "base_url", env="OPENAI_BASE_URL"),
        )

    def chat(self, messages: list[dict], *, model: str, **params) -> str:
        resp = self._client.chat.completions.create(model=model, messages=messages, **params)
        return (resp.choices[0].message.content or "").strip()
