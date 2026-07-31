"""Provider 工厂。"""

from __future__ import annotations

from llmolympic.providers.base import Provider
from llmolympic.providers.mock import MockProvider
from llmolympic.providers.ollama_provider import OllamaProvider
from llmolympic.providers.openai_provider import OpenAIProvider


def create_provider(kind: str, model: str = "") -> Provider:
    """按 CLI 选手规格里的类型名创建 provider。

    ``mock`` 的 ``model`` 段被解释为策略名（random/fixed/illegal）。
    """
    if kind == "mock":
        return MockProvider(strategy=model or "random")
    if kind == "openai":
        return OpenAIProvider()
    if kind == "ollama":
        return OllamaProvider()
    raise ValueError(f"未知 provider {kind!r}，可选: mock, openai, ollama, human")
