"""OpenAI 适配器（官方 SDK）。

key / base_url 依次取自：构造参数 > 环境变量 > config.toml。
"""

from __future__ import annotations

import inspect

from llmolympic.config import get as cfg_get
from llmolympic.providers.base import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    Provider,
    ProviderConfigurationError,
    ProviderTimeoutError,
    validate_base_url,
)

_DEFAULT_BASE_URL = "https://api.openai.com/v1"


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

    @classmethod
    def _completion_params(cls, params: dict, *, model: str) -> dict:
        limited = dict(params)
        if "max_tokens" not in limited and "max_completion_tokens" not in limited:
            model_name = model.rsplit("/", 1)[-1].lower()
            uses_completion_tokens = any(
                model_name == prefix or model_name.startswith((f"{prefix}-", f"{prefix}."))
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
            if getattr(self, "_isolate_sdk_environment", False):
                client = _isolate_client(client)
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
            if getattr(self, "_isolate_sdk_environment", False):
                client = _isolate_client(client)
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=messages,
                **self._completion_params(params, model=model),
            )
        except APITimeoutError as exc:
            raise ProviderTimeoutError("OpenAI 请求超时") from exc
        return (resp.choices[0].message.content or "").strip()
