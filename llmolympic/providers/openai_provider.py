"""OpenAI 适配器（官方 SDK）。

key / base_url 依次取自：构造参数 > 环境变量 > config.toml。
"""

from __future__ import annotations

import inspect

from llmolympic.config import get as cfg_get
from llmolympic.providers.base import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    Provider,
    ProviderChatResult,
    ProviderConfigurationError,
    ProviderTimeoutError,
    ProviderUsage,
    UsageSupport,
    _endpoint_fingerprint,
    _stable_route_id,
    validate_base_url,
)

_DEFAULT_BASE_URL = "https://api.openai.com/v1"


def openai_route_id(
    base_url: str | None,
    model: str,
    *,
    source: str = "OpenAI base_url",
) -> str:
    """Derive the credential-free route identity used by OpenAI Profiles."""

    resolved_base_url = validate_base_url(
        base_url or _DEFAULT_BASE_URL,
        source=source,
        require_https_for_remote=True,
    )
    return _stable_route_id(
        family="openai-chat-completions-v1",
        target=_endpoint_fingerprint(resolved_base_url),
        model=model,
    )


def _isolate_client(client):
    """Remove OpenAI SDK settings inherited from its global environment."""
    # Passing empty strings prevents the constructor from consulting the
    # environment. Reset them to None so no empty organization/project headers
    # are emitted. OPENAI_CUSTOM_HEADERS has no public opt-out, so clear the
    # SDK's copied mapping before the client can issue a request.
    client.organization = None
    client.project = None
    # Newer SDK releases also infer these credentials from the environment.
    # Clear them without passing version-specific constructor arguments so the
    # provider remains compatible with the project's full OpenAI SDK range.
    for attribute in ("admin_api_key", "webhook_secret"):
        if hasattr(client, attribute):
            setattr(client, attribute, None)
    if not hasattr(client, "_custom_headers"):
        raise ProviderConfigurationError("当前 OpenAI SDK 无法安全隔离请求头")
    client._custom_headers = {}
    return client


def _isolated_client_options(client_factory) -> dict[str, object]:
    """Build isolation options accepted by both early and current SDK v1/v2."""

    parameters = inspect.signature(client_factory).parameters.values()
    accepts_project = any(
        parameter.name == "project" or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )
    options: dict[str, object] = {
        "organization": "",
        "default_headers": {},
    }
    if accepts_project:
        options["project"] = ""
    return options


class OpenAIProvider(Provider):
    name = "openai"

    _COMPLETION_TOKEN_MODELS = ("o1", "o3", "o4", "gpt-5")

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        profile_id: str | None = None,
        use_legacy_config: bool = True,
    ) -> None:
        # 延迟导入，无 key 的环境也能加载其他 provider
        from openai import AsyncOpenAI, OpenAI

        resolved_api_key = api_key
        resolved_base_url = base_url
        if use_legacy_config:
            resolved_api_key = resolved_api_key or cfg_get(
                "openai", "api_key", env="OPENAI_API_KEY"
            )
            resolved_base_url = resolved_base_url or cfg_get(
                "openai", "base_url", env="OPENAI_BASE_URL"
            )
        if not resolved_api_key:
            source = (
                f"Provider Profile {profile_id!r} 的 api_key_env"
                if profile_id is not None
                else "OPENAI_API_KEY 或 [openai].api_key"
            )
            raise ProviderConfigurationError(f"未配置 OpenAI API Key；请设置 {source}")
        # Resolve the official endpoint explicitly so the SDK cannot choose a
        # different endpoint after the isolation decision below.
        resolved_base_url = validate_base_url(
            resolved_base_url or _DEFAULT_BASE_URL,
            source=(
                f"Provider Profile {profile_id!r} 的 base_url"
                if profile_id is not None
                else "OpenAI base_url"
            ),
            require_https_for_remote=True,
        )
        self.profile_id = profile_id
        self._route_base_url = resolved_base_url
        # Profiles always bind a key to one isolated endpoint. Preserve the
        # documented legacy SDK behavior for the exact official endpoint, but
        # never forward ambient OpenAI organization, project, admin, webhook,
        # or custom headers to a third-party compatible endpoint.
        self._isolate_sdk_environment = (
            not use_legacy_config or resolved_base_url != _DEFAULT_BASE_URL
        )
        sync_client_options = (
            _isolated_client_options(OpenAI) if self._isolate_sdk_environment else {}
        )
        async_client_options = (
            _isolated_client_options(AsyncOpenAI) if self._isolate_sdk_environment else {}
        )
        self._client = OpenAI(
            api_key=resolved_api_key,
            base_url=resolved_base_url,
            **sync_client_options,
        )
        self._async_client = AsyncOpenAI(
            api_key=resolved_api_key,
            base_url=resolved_base_url,
            **async_client_options,
        )
        if self._isolate_sdk_environment:
            # default_headers={} alone does not suppress
            # OPENAI_CUSTOM_HEADERS, so scrub each real client too.
            self._client = _isolate_client(self._client)
            self._async_client = _isolate_client(self._async_client)

    def route_id_for(self, model: str) -> str:
        return openai_route_id(self._route_base_url, model)

    def usage_support_for(self, model: str) -> UsageSupport:
        del model
        return UsageSupport.REPORTED

    @staticmethod
    def _strict_output_token_cap(value: object, *, source: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ProviderConfigurationError(f"{source} 必须是正整数")
        return value

    def resolve_output_token_cap(
        self,
        model: str,
        *,
        requested_cap: int | None,
        params: dict[str, object],
    ) -> int:
        del self
        if requested_cap is not None:
            requested_cap = OpenAIProvider._strict_output_token_cap(
                requested_cap,
                source="LLM 单次输出 Token 上限",
            )
        n = params.get("n", 1)
        if isinstance(n, bool) or not isinstance(n, int) or n != 1:
            raise ProviderConfigurationError("Provider 用量预算模式仅支持 n=1")
        configured = [key for key in ("max_tokens", "max_completion_tokens") if key in params]
        if len(configured) > 1:
            raise ProviderConfigurationError(
                "max_tokens 与 max_completion_tokens 不能同时设置"
            )
        configured_cap = (
            DEFAULT_MAX_OUTPUT_TOKENS
            if not configured
            else OpenAIProvider._strict_output_token_cap(
                params[configured[0]],
                source=configured[0],
            )
        )
        return configured_cap if requested_cap is None else min(configured_cap, requested_cap)

    @classmethod
    def _completion_params(
        cls,
        params: dict,
        *,
        model: str,
        output_token_cap: int | None = None,
    ) -> dict:
        limited = dict(params)
        if output_token_cap is not None:
            cap = cls._strict_output_token_cap(
                output_token_cap,
                source="Provider 输出 Token 上限",
            )
            n = limited.get("n", 1)
            if isinstance(n, bool) or not isinstance(n, int) or n != 1:
                raise ProviderConfigurationError("Provider 用量预算模式仅支持 n=1")
            configured = [
                key for key in ("max_tokens", "max_completion_tokens") if key in limited
            ]
            if len(configured) > 1:
                raise ProviderConfigurationError(
                    "max_tokens 与 max_completion_tokens 不能同时设置"
                )
            limit_key = configured[0] if configured else cls._completion_limit_key(model)
            limited.pop("max_tokens", None)
            limited.pop("max_completion_tokens", None)
            limited[limit_key] = cap
            return limited
        if "max_tokens" not in limited and "max_completion_tokens" not in limited:
            limited[cls._completion_limit_key(model)] = DEFAULT_MAX_OUTPUT_TOKENS
        return limited

    @classmethod
    def _completion_limit_key(cls, model: str) -> str:
        model_name = model.rsplit("/", 1)[-1].lower()
        uses_completion_tokens = any(
            model_name == prefix or model_name.startswith((f"{prefix}-", f"{prefix}."))
            for prefix in cls._COMPLETION_TOKEN_MODELS
        )
        return "max_completion_tokens" if uses_completion_tokens else "max_tokens"

    @staticmethod
    def _usage_from_response(response: object) -> ProviderUsage | None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None

        def field(name: str) -> object:
            if isinstance(usage, dict):
                return usage.get(name)
            return getattr(usage, name, None)

        input_tokens = field("prompt_tokens")
        output_tokens = field("completion_tokens")
        total_tokens = field("total_tokens")
        if input_tokens is None or output_tokens is None or total_tokens is None:
            return None
        return ProviderUsage(
            input_tokens=input_tokens,  # type: ignore[arg-type]
            output_tokens=output_tokens,  # type: ignore[arg-type]
            total_tokens=total_tokens,  # type: ignore[arg-type]
        )

    @classmethod
    def _result_from_response(cls, response: object) -> ProviderChatResult:
        choice = response.choices[0]  # type: ignore[attr-defined]
        return ProviderChatResult(
            text=(choice.message.content or "").strip(),
            usage=cls._usage_from_response(response),
            finish_reason=getattr(choice, "finish_reason", None),
        )

    def _sync_completion(
        self,
        messages: list[dict],
        *,
        model: str,
        request_timeout: float | None = None,
        output_token_cap: int | None = None,
        **params,
    ) -> object:
        from openai import APITimeoutError

        options: dict[str, object] = {"max_retries": 0}
        if request_timeout is not None:
            options["timeout"] = request_timeout
        client = self._client.with_options(**options)
        if getattr(self, "_isolate_sdk_environment", False):
            client = _isolate_client(client)
        try:
            return client.chat.completions.create(
                model=model,
                messages=messages,
                **self._completion_params(
                    params,
                    model=model,
                    output_token_cap=output_token_cap,
                ),
            )
        except APITimeoutError as exc:
            raise ProviderTimeoutError("OpenAI 请求超时") from exc

    async def _async_completion(
        self,
        messages: list[dict],
        *,
        model: str,
        request_timeout: float | None = None,
        output_token_cap: int | None = None,
        **params,
    ) -> object:
        from openai import APITimeoutError

        options: dict[str, object] = {"max_retries": 0}
        if request_timeout is not None:
            options["timeout"] = request_timeout
        client = self._async_client.with_options(**options)
        if getattr(self, "_isolate_sdk_environment", False):
            client = _isolate_client(client)
        try:
            return await client.chat.completions.create(
                model=model,
                messages=messages,
                **self._completion_params(
                    params,
                    model=model,
                    output_token_cap=output_token_cap,
                ),
            )
        except APITimeoutError as exc:
            raise ProviderTimeoutError("OpenAI 请求超时") from exc

    def chat(
        self,
        messages: list[dict],
        *,
        model: str,
        request_timeout: float | None = None,
        **params,
    ) -> str:
        response = self._sync_completion(
            messages,
            model=model,
            request_timeout=request_timeout,
            **params,
        )
        choice = response.choices[0]  # type: ignore[attr-defined]
        return (choice.message.content or "").strip()

    def chat_with_usage(
        self,
        messages: list[dict],
        *,
        model: str,
        request_timeout: float | None = None,
        output_token_cap: int | None = None,
        **params,
    ) -> ProviderChatResult:
        response = self._sync_completion(
            messages,
            model=model,
            request_timeout=request_timeout,
            output_token_cap=output_token_cap,
            **params,
        )
        return self._result_from_response(response)

    async def achat(
        self,
        messages: list[dict],
        *,
        model: str,
        request_timeout: float | None = None,
        **params,
    ) -> str:
        response = await self._async_completion(
            messages,
            model=model,
            request_timeout=request_timeout,
            **params,
        )
        choice = response.choices[0]  # type: ignore[attr-defined]
        return (choice.message.content or "").strip()

    async def achat_with_usage(
        self,
        messages: list[dict],
        *,
        model: str,
        request_timeout: float | None = None,
        output_token_cap: int | None = None,
        **params,
    ) -> ProviderChatResult:
        response = await self._async_completion(
            messages,
            model=model,
            request_timeout=request_timeout,
            output_token_cap=output_token_cap,
            **params,
        )
        return self._result_from_response(response)
