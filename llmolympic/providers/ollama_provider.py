"""Ollama 适配器：HTTP 调本地服务，跑开源模型零 API 成本。"""

from __future__ import annotations

import httpx

from llmolympic.config import get as cfg_get
from llmolympic.providers.base import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    Provider,
    ProviderTimeoutError,
)

_DEFAULT_BASE_URL = "http://localhost:11434"


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(self, base_url: str | None = None) -> None:
        url = base_url or cfg_get("ollama", "base_url", _DEFAULT_BASE_URL, env="OLLAMA_BASE_URL")
        self.base_url = url.rstrip("/")

    @staticmethod
    def _payload(messages: list[dict], model: str, params: dict) -> dict:
        options = dict(params)
        options.setdefault("num_predict", DEFAULT_MAX_OUTPUT_TOKENS)
        return {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": options,
        }

    def chat(
        self,
        messages: list[dict],
        *,
        model: str,
        request_timeout: float | None = None,
        **params,
    ) -> str:
        try:
            resp = httpx.post(
                f"{self.base_url}/api/chat",
                json=self._payload(messages, model, params),
                timeout=120.0 if request_timeout is None else request_timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("Ollama 请求超时") from exc
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip()

    async def achat(
        self,
        messages: list[dict],
        *,
        model: str,
        request_timeout: float | None = None,
        **params,
    ) -> str:
        timeout = 120.0 if request_timeout is None else request_timeout
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/api/chat",
                    json=self._payload(messages, model, params),
                )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("Ollama 请求超时") from exc
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip()
