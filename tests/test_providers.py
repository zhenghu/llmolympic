"""Provider adapter timeout conversion tests."""

from __future__ import annotations

import asyncio

import httpx
import openai
import pytest
from openai import APITimeoutError

from llmolympic.config import ProviderProfile
from llmolympic.providers import create_profile_provider
from llmolympic.providers.base import ProviderConfigurationError, ProviderTimeoutError
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


def test_openai_requests_have_a_default_output_token_budget() -> None:
    assert OpenAIProvider._completion_params({}, model="gpt-4o-mini") == {"max_tokens": 1024}
    assert OpenAIProvider._completion_params({}, model="o3-mini") == {"max_completion_tokens": 1024}
    assert OpenAIProvider._completion_params({}, model="openai/gpt-5.6-terra") == {
        "max_completion_tokens": 1024
    }
    assert OpenAIProvider._completion_params({"max_tokens": 64}, model="o3") == {"max_tokens": 64}
    assert OpenAIProvider._completion_params(
        {"max_completion_tokens": 32}, model="deepseek-chat"
    ) == {"max_completion_tokens": 32}


def test_ollama_requests_have_a_default_output_token_budget() -> None:
    default = OllamaProvider._payload([], "model", {})
    explicit = OllamaProvider._payload([], "model", {"num_predict": 64})

    assert default["options"]["num_predict"] == 1024
    assert explicit["options"]["num_predict"] == 64


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


def test_two_openai_profiles_use_isolated_endpoints_and_safe_ids(monkeypatch) -> None:
    monkeypatch.setenv("KIMI_TEST_KEY", "kimi-test-secret")
    monkeypatch.setenv("DEEPSEEK_TEST_KEY", "deepseek-test-secret")
    kimi = ProviderProfile(
        profile_id="kimi",
        provider="openai",
        default_model="moonshot-v1",
        base_url="https://kimi.example/v1",
        api_key_env="KIMI_TEST_KEY",
        display_name="Kimi",
    )
    deepseek = ProviderProfile(
        profile_id="deepseek",
        provider="openai",
        default_model="deepseek-chat",
        base_url="https://deepseek.example/v1",
        api_key_env="DEEPSEEK_TEST_KEY",
        display_name="DeepSeek",
    )

    kimi_provider = create_profile_provider(kimi)
    deepseek_provider = create_profile_provider(deepseek)

    assert kimi_provider.profile_id == "kimi"
    assert deepseek_provider.profile_id == "deepseek"
    assert str(kimi_provider._client.base_url) == "https://kimi.example/v1/"
    assert str(deepseek_provider._client.base_url) == "https://deepseek.example/v1/"
    assert not hasattr(kimi_provider, "api_key")
    assert not hasattr(deepseek_provider, "api_key")


def test_profile_does_not_fall_back_to_an_unrelated_global_api_key(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "global-key-must-not-be-reused")
    monkeypatch.delenv("MISSING_PROFILE_KEY", raising=False)
    profile = ProviderProfile(
        profile_id="isolated",
        provider="openai",
        default_model="model",
        base_url="https://isolated.example/v1",
        api_key_env="MISSING_PROFILE_KEY",
    )

    with pytest.raises(ProviderConfigurationError, match="MISSING_PROFILE_KEY") as raised:
        create_profile_provider(profile)

    assert "global-key" not in str(raised.value)


def test_openai_profile_without_base_url_does_not_inherit_global_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("PROFILE_TEST_KEY", "profile-test-secret")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://attacker.example/v1")
    profile = ProviderProfile(
        profile_id="official",
        provider="openai",
        default_model="gpt-4o-mini",
        api_key_env="PROFILE_TEST_KEY",
    )

    provider = create_profile_provider(profile)

    assert provider.profile_id == "official"
    assert str(provider._client.base_url) == "https://api.openai.com/v1/"
    assert "attacker.example" not in str(provider._client.base_url)


def test_openai_profile_clients_do_not_inherit_global_sdk_headers(monkeypatch) -> None:
    sync_requests: list[httpx.Request] = []
    async_requests: list[httpx.Request] = []
    real_sync_client = openai.OpenAI
    real_async_client = openai.AsyncOpenAI

    def response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "test-completion",
                "object": "chat.completion",
                "created": 0,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    def sync_transport(request: httpx.Request) -> httpx.Response:
        sync_requests.append(request)
        return response(request)

    def async_transport(request: httpx.Request) -> httpx.Response:
        async_requests.append(request)
        return response(request)

    def sync_client(**kwargs):
        return real_sync_client(
            **kwargs,
            http_client=httpx.Client(transport=httpx.MockTransport(sync_transport)),
        )

    def async_client(**kwargs):
        return real_async_client(
            **kwargs,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(async_transport)),
        )

    monkeypatch.setattr(openai, "OpenAI", sync_client)
    monkeypatch.setattr(openai, "AsyncOpenAI", async_client)
    monkeypatch.setenv(
        "OPENAI_CUSTOM_HEADERS",
        "Authorization: Bearer global-key\nX-Global-Leak: inherited-value",
    )
    monkeypatch.setenv("OPENAI_ORG_ID", "global-organization")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "global-project")
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "global-admin-key")
    monkeypatch.setenv("OPENAI_WEBHOOK_SECRET", "global-webhook-secret")

    provider = OpenAIProvider(
        api_key="profile-test-key",
        base_url="https://profile.example/v1",
        profile_id="isolated",
        use_legacy_config=False,
    )

    assert provider._client.organization is None
    assert provider._client.project is None
    assert provider._client.admin_api_key is None
    assert provider._client.webhook_secret is None
    assert provider._client._custom_headers == {}
    assert provider._async_client.organization is None
    assert provider._async_client.project is None
    assert provider._async_client.admin_api_key is None
    assert provider._async_client.webhook_secret is None
    assert provider._async_client._custom_headers == {}
    assert provider.chat([], model="test-model", request_timeout=0.25) == "ok"
    assert (
        asyncio.run(provider.achat([], model="test-model", request_timeout=0.25))
        == "ok"
    )

    for request in [*sync_requests, *async_requests]:
        assert request.headers["Authorization"] == "Bearer profile-test-key"
        assert "X-Global-Leak" not in request.headers
        assert "OpenAI-Organization" not in request.headers
        assert "OpenAI-Project" not in request.headers
        assert "global-key" not in str(request.headers)
        assert "inherited-value" not in str(request.headers)


def test_openai_profile_remains_compatible_with_sdk_without_project_argument(
    monkeypatch,
) -> None:
    real_sync_client = openai.OpenAI
    real_async_client = openai.AsyncOpenAI

    def legacy_sync_client(
        *, api_key, organization=None, base_url=None, default_headers=None
    ):
        return real_sync_client(
            api_key=api_key,
            organization=organization,
            base_url=base_url,
            default_headers=default_headers,
        )

    def legacy_async_client(
        *, api_key, organization=None, base_url=None, default_headers=None
    ):
        return real_async_client(
            api_key=api_key,
            organization=organization,
            base_url=base_url,
            default_headers=default_headers,
        )

    monkeypatch.setattr(openai, "OpenAI", legacy_sync_client)
    monkeypatch.setattr(openai, "AsyncOpenAI", legacy_async_client)
    monkeypatch.setenv("OPENAI_PROJECT_ID", "must-not-break-old-sdk")

    provider = OpenAIProvider(
        api_key="profile-test-key",
        base_url="https://profile.example/v1",
        profile_id="old-sdk",
        use_legacy_config=False,
    )

    assert provider.profile_id == "old-sdk"
    assert provider._client.project is None
    assert provider._async_client.project is None


def test_ollama_profile_uses_its_endpoint_and_exposes_profile_id() -> None:
    profile = ProviderProfile(
        profile_id="local",
        provider="ollama",
        default_model="llama3.1:8b",
        base_url="http://127.0.0.1:11434",
        display_name="Local Llama",
    )

    provider = create_profile_provider(profile)

    assert isinstance(provider, OllamaProvider)
    assert provider.profile_id == "local"
    assert provider.base_url == "http://127.0.0.1:11434"
    assert not hasattr(provider, "api_key")


@pytest.mark.parametrize(
    "base_url",
    [
        "not-a-url",
        "file:///tmp/socket",
        "https://user:password@example.test/v1",
        "https://example.test/v1?api_key=query-secret",
        "https://example.test/v1#credential-fragment",
    ],
)
def test_profile_provider_rejects_invalid_or_credential_bearing_url(
    monkeypatch, base_url: str
) -> None:
    monkeypatch.setenv("PROFILE_TEST_KEY", "test-secret")
    profile = ProviderProfile(
        profile_id="invalid-url",
        provider="openai",
        default_model="model",
        base_url=base_url,
        api_key_env="PROFILE_TEST_KEY",
    )

    with pytest.raises(ProviderConfigurationError):
        create_profile_provider(profile)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://remote.example/v1",
        "http://192.0.2.10:8000/v1",
    ],
)
def test_openai_profile_rejects_plain_http_for_remote_endpoints(monkeypatch, base_url: str) -> None:
    monkeypatch.setenv("PROFILE_TEST_KEY", "test-secret")
    profile = ProviderProfile(
        profile_id="remote-http",
        provider="openai",
        default_model="model",
        base_url=base_url,
        api_key_env="PROFILE_TEST_KEY",
    )

    with pytest.raises(ProviderConfigurationError, match="https://"):
        create_profile_provider(profile)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:8000/v1",
        "http://127.42.0.1:8000/v1",
        "http://[::1]:8000/v1",
    ],
)
def test_openai_profile_allows_plain_http_only_for_loopback(monkeypatch, base_url: str) -> None:
    monkeypatch.setenv("PROFILE_TEST_KEY", "test-secret")
    profile = ProviderProfile(
        profile_id="local-http",
        provider="openai",
        default_model="model",
        base_url=base_url,
        api_key_env="PROFILE_TEST_KEY",
    )

    provider = create_profile_provider(profile)

    assert provider.profile_id == "local-http"


def test_legacy_openai_configuration_also_rejects_remote_plain_http() -> None:
    with pytest.raises(ProviderConfigurationError, match="https://"):
        OpenAIProvider(
            api_key="test-key",
            base_url="http://remote.example/v1",
        )


def test_legacy_openai_configuration_keeps_loopback_http_compatible() -> None:
    provider = OpenAIProvider(
        api_key="test-key",
        base_url="http://127.0.0.1:8000/v1",
    )

    assert str(provider._client.base_url) == "http://127.0.0.1:8000/v1/"
