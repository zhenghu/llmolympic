"""Provider adapter timeout conversion tests."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from openai import APITimeoutError

from llmolympic.providers.base import ProviderTimeoutError
from llmolympic.providers.ollama_provider import OllamaProvider
from llmolympic.providers.openai_provider import OpenAIProvider


class _SyncTimeoutCompletions:
    def create(self, **params) -> None:
        raise APITimeoutError(httpx.Request("POST", "https://example.test/chat"))


class _AsyncTimeoutCompletions:
    async def create(self, **params) -> None:
        raise APITimeoutError(httpx.Request("POST", "https://example.test/chat"))


class _OpenAIClient:
    def __init__(self, completions: object) -> None:
        self.chat = type("Chat", (), {"completions": completions})()
        self.options: dict = {}

    def with_options(self, **options):
        self.options = options
        return self


def _openai_provider(*, async_client: bool = False) -> tuple[OpenAIProvider, _OpenAIClient]:
    provider = object.__new__(OpenAIProvider)
    client = _OpenAIClient(
        _AsyncTimeoutCompletions() if async_client else _SyncTimeoutCompletions()
    )
    if async_client:
        provider._async_client = client
    else:
        provider._client = client
    return provider, client


def test_openai_sync_timeout_is_converted_to_stable_provider_error() -> None:
    provider, client = _openai_provider()

    with pytest.raises(ProviderTimeoutError) as raised:
        provider.chat([], model="model", request_timeout=0.25)

    assert isinstance(raised.value.__cause__, APITimeoutError)
    assert client.options == {"timeout": 0.25, "max_retries": 0}


def test_openai_async_timeout_is_converted_to_stable_provider_error() -> None:
    provider, client = _openai_provider(async_client=True)

    with pytest.raises(ProviderTimeoutError) as raised:
        asyncio.run(provider.achat([], model="model", request_timeout=0.25))

    assert isinstance(raised.value.__cause__, APITimeoutError)
    assert client.options == {"timeout": 0.25, "max_retries": 0}


def test_ollama_sync_timeout_is_converted_to_stable_provider_error(monkeypatch) -> None:
    captured: dict = {}

    def timeout(*args, **kwargs) -> None:
        captured.update(kwargs)
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(httpx, "post", timeout)
    provider = OllamaProvider("http://localhost:11434")

    with pytest.raises(ProviderTimeoutError) as raised:
        provider.chat([], model="model", request_timeout=0.25)

    assert isinstance(raised.value.__cause__, httpx.ReadTimeout)
    assert captured["timeout"] == 0.25


def test_ollama_async_timeout_is_converted_to_stable_provider_error(monkeypatch) -> None:
    captured: dict = {}

    class TimeoutAsyncClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            return None

        async def post(self, *args, **kwargs) -> None:
            raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(httpx, "AsyncClient", TimeoutAsyncClient)
    provider = OllamaProvider("http://localhost:11434")

    with pytest.raises(ProviderTimeoutError) as raised:
        asyncio.run(provider.achat([], model="model", request_timeout=0.25))

    assert isinstance(raised.value.__cause__, httpx.ReadTimeout)
    assert captured["timeout"] == 0.25
