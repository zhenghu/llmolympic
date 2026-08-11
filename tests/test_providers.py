"""Provider adapter timeout conversion tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import openai
import pytest
from openai import APITimeoutError

from llmolympic.config import ProviderProfile
from llmolympic.providers import create_profile_provider
from llmolympic.providers.base import (
    Provider,
    ProviderChatResult,
    ProviderConfigurationError,
    ProviderTimeoutError,
    ProviderUsage,
    UsageSupport,
    validate_route_id,
)
from llmolympic.providers.mock import MockProvider
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


class _SyncResponseCompletions:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls = 0
        self.last_params: dict[str, object] = {}

    def create(self, **params) -> object:
        self.calls += 1
        self.last_params = params
        return self.response


class _AsyncResponseCompletions(_SyncResponseCompletions):
    async def create(self, **params) -> object:
        self.calls += 1
        self.last_params = params
        return self.response


def _openai_response(*, usage: object | None) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="  accounted response  "),
                finish_reason="stop",
            )
        ],
        usage=usage,
    )


class _FallbackRouteProvider(Provider):
    name = "fallback"

    def __init__(self, *, name: str, api_key: str, base_url: str) -> None:
        self.name = name
        self.api_key = api_key
        self.base_url = base_url

    def chat(self, messages: list[dict], *, model: str, **params) -> str:
        return "ok"


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


def test_provider_usage_is_strict_and_chat_result_metadata_is_typed() -> None:
    usage = ProviderUsage(input_tokens=2, output_tokens=3, total_tokens=5)

    assert ProviderChatResult("ok", usage, "stop").usage == usage
    with pytest.raises(TypeError, match="input_tokens"):
        ProviderUsage(input_tokens=True, output_tokens=0, total_tokens=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="output_tokens"):
        ProviderUsage(input_tokens=0, output_tokens=1.5, total_tokens=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="input_tokens"):
        ProviderUsage(input_tokens=-1, output_tokens=1, total_tokens=0)
    with pytest.raises(ValueError, match="total_tokens"):
        ProviderUsage(input_tokens=2, output_tokens=3, total_tokens=6)
    with pytest.raises(TypeError, match="usage"):
        ProviderChatResult("ok", usage={})  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="finish_reason"):
        ProviderChatResult("ok", finish_reason=1)  # type: ignore[arg-type]


def test_default_usage_wrappers_preserve_legacy_provider_contract() -> None:
    provider = _FallbackRouteProvider(
        name="fallback",
        api_key="sensitive-key",
        base_url="https://sensitive.example/v1",
    )

    sync_result = provider.chat_with_usage([], model="model")
    async_result = asyncio.run(provider.achat_with_usage([], model="model"))

    assert provider.chat([], model="model") == "ok"
    assert sync_result == ProviderChatResult(text="ok")
    assert async_result == ProviderChatResult(text="ok")
    assert provider.usage_support_for("model") is UsageSupport.NONE
    assert (
        provider.resolve_output_token_cap("model", requested_cap=64, params={}) is None
    )
    with pytest.raises(ProviderConfigurationError, match="Token 上限"):
        provider.chat_with_usage([], model="model", output_token_cap=64)


@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
def test_openai_usage_is_captured_from_the_same_response_and_retry_is_disabled(
    async_mode: bool,
) -> None:
    response = _openai_response(
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18)
    )
    completions = (
        _AsyncResponseCompletions(response) if async_mode else _SyncResponseCompletions(response)
    )
    client = _OpenAIClient(completions)
    provider = object.__new__(OpenAIProvider)
    if async_mode:
        provider._async_client = client
        result = asyncio.run(provider.achat_with_usage([], model="test-model"))
    else:
        provider._client = client
        result = provider.chat_with_usage([], model="test-model")

    assert result == ProviderChatResult(
        text="accounted response",
        usage=ProviderUsage(input_tokens=11, output_tokens=7, total_tokens=18),
        finish_reason="stop",
    )
    assert completions.calls == 1
    assert client.options == {"max_retries": 0}
    assert provider.usage_support_for("test-model") is UsageSupport.REPORTED


def test_openai_chat_still_returns_text_and_missing_usage_is_not_fabricated() -> None:
    completions = _SyncResponseCompletions(_openai_response(usage=None))
    provider = object.__new__(OpenAIProvider)
    provider._client = _OpenAIClient(completions)

    assert provider.chat([], model="test-model") == "accounted response"
    result = provider.chat_with_usage([], model="test-model")

    assert result.text == "accounted response"
    assert result.usage is None
    assert completions.calls == 2


def test_openai_incomplete_usage_is_none_but_invalid_usage_is_rejected() -> None:
    incomplete = _openai_response(
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=None, total_tokens=2)
    )
    invalid = _openai_response(
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3, total_tokens=4)
    )

    assert OpenAIProvider._result_from_response(incomplete).usage is None
    with pytest.raises(ValueError, match="total_tokens"):
        OpenAIProvider._result_from_response(invalid)


def test_openai_output_cap_is_negotiated_and_sent_exactly() -> None:
    response = _openai_response(
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3, total_tokens=5)
    )
    completions = _SyncResponseCompletions(response)
    provider = object.__new__(OpenAIProvider)
    provider._client = _OpenAIClient(completions)

    assert provider.resolve_output_token_cap(
        "gpt-4o-mini",
        requested_cap=64,
        params={"max_tokens": 32},
    ) == 32
    result = provider.chat_with_usage(
        [],
        model="gpt-4o-mini",
        output_token_cap=32,
        max_tokens=128,
    )

    assert result.text == "accounted response"
    assert completions.last_params["max_tokens"] == 32
    assert "max_completion_tokens" not in completions.last_params


def test_openai_budget_cap_rejects_ambiguous_or_multi_choice_params() -> None:
    provider = object.__new__(OpenAIProvider)

    with pytest.raises(ProviderConfigurationError, match="n=1"):
        provider.resolve_output_token_cap("model", requested_cap=64, params={"n": 2})
    with pytest.raises(ProviderConfigurationError, match="不能同时"):
        provider.resolve_output_token_cap(
            "model",
            requested_cap=64,
            params={"max_tokens": 32, "max_completion_tokens": 32},
        )


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
    hard_cap = OllamaProvider._payload(
        [],
        "model",
        {"num_predict": 256},
        output_token_cap=32,
    )

    assert default["options"]["num_predict"] == 1024
    assert explicit["options"]["num_predict"] == 64
    assert hard_cap["options"]["num_predict"] == 32


def test_ollama_sync_usage_is_captured_from_one_json_response(monkeypatch) -> None:
    class Response:
        def __init__(self) -> None:
            self.json_calls = 0

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            self.json_calls += 1
            return {
                "message": {"content": "  local response  "},
                "prompt_eval_count": 9,
                "eval_count": 4,
                "done_reason": "stop",
            }

    response = Response()
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: response)
    provider = OllamaProvider("http://localhost:11434")

    assert provider.resolve_output_token_cap(
        "model",
        requested_cap=64,
        params={"num_predict": 32},
    ) == 32
    result = provider.chat_with_usage([], model="model", output_token_cap=32)

    assert result == ProviderChatResult(
        text="local response",
        usage=ProviderUsage(input_tokens=9, output_tokens=4, total_tokens=13),
        finish_reason="stop",
    )
    assert response.json_calls == 1
    assert provider.usage_support_for("model") is UsageSupport.REPORTED


def test_ollama_async_usage_is_captured_and_missing_counts_are_none(monkeypatch) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"message": {"content": "local response"}, "done_reason": "load"}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            return None

        async def post(self, *args, **kwargs) -> Response:
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: Client())
    provider = OllamaProvider("http://localhost:11434")

    result = asyncio.run(provider.achat_with_usage([], model="model"))

    assert result == ProviderChatResult(
        text="local response",
        usage=None,
        finish_reason="load",
    )


def test_ollama_usage_rejects_invalid_counts() -> None:
    with pytest.raises(TypeError, match="input_tokens"):
        OllamaProvider._usage_from_payload(
            {"prompt_eval_count": "2", "eval_count": "3"}
        )
    with pytest.raises(ValueError, match="output_tokens"):
        OllamaProvider._usage_from_payload({"prompt_eval_count": 2, "eval_count": -1})


def test_mock_reports_exact_zero_usage_without_changing_text_contract() -> None:
    provider = MockProvider("fixed")
    messages = [{"role": "user", "content": "answer this"}]

    result = asyncio.run(provider.achat_with_usage(messages, model="ignored"))

    assert provider.chat(messages, model="ignored") == "42"
    assert result == ProviderChatResult(
        text="42",
        usage=ProviderUsage(input_tokens=0, output_tokens=0, total_tokens=0),
    )
    assert provider.usage_support_for("ignored") is UsageSupport.EXACT_ZERO
    assert provider.resolve_output_token_cap(
        "ignored", requested_cap=64, params={}
    ) == 0


def test_default_route_identity_is_stable_without_inspecting_instance_attributes() -> None:
    first = _FallbackRouteProvider(
        name="first",
        api_key="first-sensitive-key",
        base_url="https://first-sensitive.example/v1",
    )
    second = _FallbackRouteProvider(
        name="second",
        api_key="second-sensitive-key",
        base_url="https://second-sensitive.example/v1",
    )

    first_route = first.route_id_for("exact-model")
    second_route = second.route_id_for("exact-model")

    assert validate_route_id(first_route) == first_route
    assert first_route == second_route
    assert first.route_id_for("other-model") != first_route
    assert "sensitive" not in first_route


def test_mock_route_identity_uses_strategy_and_ignores_model_and_seed() -> None:
    fixed = MockProvider("fixed", seed=1)
    fixed_relabelled = MockProvider("fixed", seed=999)
    random = MockProvider("random", seed=1)

    assert fixed.route_id_for("fake-model-a") == fixed_relabelled.route_id_for(
        "fake-model-b"
    )
    assert fixed.route_id_for("anything") != random.route_id_for("anything")


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


def test_openai_route_identity_matches_direct_and_profile_canonical_endpoint() -> None:
    direct = OpenAIProvider(
        api_key="direct-sensitive-key",
        base_url="HTTPS://API.OPENAI.COM:443/v1///",
    )
    profile = OpenAIProvider(
        api_key="profile-sensitive-key",
        base_url="https://api.openai.com/v1",
        profile_id="official-profile",
        use_legacy_config=False,
    )
    renamed_profile = OpenAIProvider(
        api_key="rotated-sensitive-key",
        base_url="https://API.OPENAI.COM:443/v1/",
        profile_id="renamed-profile",
        use_legacy_config=False,
    )

    route_id = direct.route_id_for("gpt-test")

    assert route_id == profile.route_id_for("gpt-test")
    assert route_id == renamed_profile.route_id_for("gpt-test")
    assert route_id != direct.route_id_for("gpt-test-2")
    assert route_id != OpenAIProvider(
        api_key="direct-sensitive-key",
        base_url="https://gateway.example/v1",
    ).route_id_for("gpt-test")
    assert "api.openai.com" not in route_id
    assert "sensitive-key" not in route_id


def test_openai_official_defaults_share_route_across_direct_and_profile(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setattr(
        "llmolympic.providers.openai_provider.cfg_get",
        lambda *args, **kwargs: None,
    )
    direct = OpenAIProvider(api_key="direct-key")
    profile = OpenAIProvider(
        api_key="profile-key",
        profile_id="official",
        use_legacy_config=False,
    )

    assert direct.route_id_for("gpt-test") == profile.route_id_for("gpt-test")


def test_endpoint_route_identity_normalizes_ollama_spelling_but_not_protocol() -> None:
    upper = OllamaProvider(
        "HTTP://LOCALHOST:80/api///",
        profile_id="upper",
        use_legacy_config=False,
    )
    lower = OllamaProvider(
        "http://localhost/api",
        profile_id="lower",
        use_legacy_config=False,
    )
    openai_compatible = OpenAIProvider(
        api_key="test-key",
        base_url="http://localhost:80/api",
    )

    assert upper.route_id_for("exact-model") == lower.route_id_for("exact-model")
    assert upper.route_id_for("exact-model") != lower.route_id_for("other-model")
    assert upper.route_id_for("exact-model") != openai_compatible.route_id_for(
        "exact-model"
    )


def test_endpoint_route_identity_canonicalizes_ip_literals_without_dns_resolution() -> None:
    expanded_ipv6 = OllamaProvider("http://[0:0:0:0:0:0:0:1]:80/api/")
    compressed_ipv6 = OllamaProvider("http://[::1]/api")
    localhost = OllamaProvider("http://localhost/api")
    ipv4_loopback = OllamaProvider("http://127.0.0.1/api")
    case_sensitive_path = OllamaProvider("http://localhost/API")

    assert expanded_ipv6.route_id_for("model") == compressed_ipv6.route_id_for("model")
    assert localhost.route_id_for("model") != ipv4_loopback.route_id_for("model")
    assert localhost.route_id_for("model") != case_sensitive_path.route_id_for("model")


def test_endpoint_route_identity_canonicalizes_idna_and_dns_root_dot() -> None:
    unicode_host = OllamaProvider("http://BÜCHER.example/api")
    ascii_host = OllamaProvider("http://xn--bcher-kva.example/api")
    idna_2008_host = OllamaProvider("http://faß.de/api")
    idna_2008_ascii = OllamaProvider("http://xn--fa-hia.de/api")
    rooted_host = OllamaProvider("http://EXAMPLE.com./api")
    unicode_rooted_host = OllamaProvider("http://example.com。/api")
    plain_host = OllamaProvider("http://example.com/api")

    assert unicode_host.route_id_for("model") == ascii_host.route_id_for("model")
    assert idna_2008_host.route_id_for("model") == idna_2008_ascii.route_id_for("model")
    assert rooted_host.route_id_for("model") == plain_host.route_id_for("model")
    assert unicode_rooted_host.route_id_for("model") == plain_host.route_id_for("model")


def test_endpoint_route_identity_canonicalizes_utf8_and_percent_encoded_paths() -> None:
    unicode_path = OllamaProvider("http://localhost/café/v1")
    encoded_path = OllamaProvider("http://localhost/caf%C3%A9/v1")
    lowercase_escape = OllamaProvider("http://localhost/caf%c3%a9/v1")
    literal_unreserved = OllamaProvider("http://localhost/a~b/v1")
    encoded_unreserved = OllamaProvider("http://localhost/a%7eb/v1")

    assert unicode_path.route_id_for("model") == encoded_path.route_id_for("model")
    assert unicode_path.route_id_for("model") == lowercase_escape.route_id_for("model")
    assert literal_unreserved.route_id_for("model") == encoded_unreserved.route_id_for("model")


@pytest.mark.parametrize(
    "provider_mode",
    ["legacy-compatible", "legacy-official", "profile-official"],
)
@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("request_timeout", [None, 0.25], ids=["direct", "with-options"])
def test_openai_sdk_environment_is_isolated_from_compatible_endpoints(
    monkeypatch,
    provider_mode: str,
    async_mode: bool,
    request_timeout: float | None,
) -> None:
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
    monkeypatch.setenv("OPENAI_ORG_ID", "global-organization")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "global-project")
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "global-admin-key")
    monkeypatch.setenv("OPENAI_WEBHOOK_SECRET", "global-webhook-secret")

    isolated = provider_mode != "legacy-official"
    if isolated:
        monkeypatch.setenv(
            "OPENAI_CUSTOM_HEADERS",
            "Authorization: Bearer global-key\nX-Global-Leak: inherited-value",
        )
    else:
        monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "X-Official-Compatible: retained-value")

    if provider_mode == "profile-official":
        expected_api_key = "profile-test-key"
        expected_base_url = "https://api.openai.com/v1/"
        monkeypatch.setenv("OPENAI_BASE_URL", "https://attacker.example/v1")
        provider = OpenAIProvider(
            api_key=expected_api_key,
            profile_id="isolated",
            use_legacy_config=False,
        )
    else:
        expected_api_key = "legacy-test-key"
        expected_base_url = (
            "https://compatible.example/v1/"
            if provider_mode == "legacy-compatible"
            else "https://api.openai.com/v1/"
        )
        monkeypatch.setenv("OPENAI_API_KEY", expected_api_key)
        monkeypatch.setenv("OPENAI_BASE_URL", expected_base_url)
        provider = OpenAIProvider()

    assert str(provider._client.base_url) == expected_base_url
    assert str(provider._async_client.base_url) == expected_base_url
    assert provider._isolate_sdk_environment is isolated
    if isolated:
        for client in (provider._client, provider._async_client):
            assert client.organization is None
            assert client.project is None
            assert client.admin_api_key is None
            assert client.webhook_secret is None
            assert client._custom_headers == {}
    else:
        for client in (provider._client, provider._async_client):
            assert client.organization == "global-organization"
            assert client.project == "global-project"
            assert client.admin_api_key == "global-admin-key"
            assert client.webhook_secret == "global-webhook-secret"  # noqa: S105 - synthetic value
            assert client._custom_headers == {"X-Official-Compatible": "retained-value"}

    if async_mode:
        result = asyncio.run(
            provider.achat([], model="test-model", request_timeout=request_timeout)
        )
    else:
        result = provider.chat([], model="test-model", request_timeout=request_timeout)
    assert result == "ok"

    requests = [*sync_requests, *async_requests]
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url).startswith(expected_base_url)
    assert request.headers["Authorization"] == f"Bearer {expected_api_key}"
    if isolated:
        assert "X-Global-Leak" not in request.headers
        assert "OpenAI-Organization" not in request.headers
        assert "OpenAI-Project" not in request.headers
        assert "global-key" not in str(request.headers)
        assert "inherited-value" not in str(request.headers)
    else:
        assert request.headers["X-Official-Compatible"] == "retained-value"
        assert request.headers["OpenAI-Organization"] == "global-organization"
        assert request.headers["OpenAI-Project"] == "global-project"


def test_openai_profile_remains_compatible_with_sdk_without_project_argument(
    monkeypatch,
) -> None:
    real_sync_client = openai.OpenAI
    real_async_client = openai.AsyncOpenAI

    def legacy_sync_client(*, api_key, organization=None, base_url=None, default_headers=None):
        return real_sync_client(
            api_key=api_key,
            organization=organization,
            base_url=base_url,
            default_headers=default_headers,
        )

    def legacy_async_client(*, api_key, organization=None, base_url=None, default_headers=None):
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
        "https://example.test/v1/\x00hidden",
        "https://example.test/v1/\u202ehidden",
        "https://example.test/v1/../chat",
        "https://example.test/v1/%2e%2e/chat",
        "https://example.test/v1/%2Fchat",
        "https://example.test/v1/%5cchat",
        "https://example.test:99999/v1",
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
