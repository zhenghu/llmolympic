"""Ollama 适配器：HTTP 调本地服务，跑开源模型零 API 成本。"""

from __future__ import annotations

import httpx

from llmolympic.config import get as cfg_get
from llmolympic.providers.base import Provider

_DEFAULT_BASE_URL = "http://localhost:11434"


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(self, base_url: str | None = None) -> None:
        url = base_url or cfg_get("ollama", "base_url", _DEFAULT_BASE_URL, env="OLLAMA_BASE_URL")
        self.base_url = url.rstrip("/")

    def chat(self, messages: list[dict], *, model: str, **params) -> str:
        resp = httpx.post(
            f"{self.base_url}/api/chat",
            json={
                "model": model,
                "messages": messages,
                "stream": False,
                "options": params or {},
            },
            timeout=120.0,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip()
