"""Ollama 适配器：HTTP 调本地服务，跑开源模型零 API 成本。"""

from __future__ import annotations

import httpx

from llmolympic.config import get as cfg_get
from llmolympic.providers.base import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    Provider,
    ProviderChatResult,
    ProviderTimeoutError,
    ProviderUsage,
    UsageSupport,
    _endpoint_fingerprint,
    _stable_route_id,
    validate_base_url,
)

_DEFAULT_BASE_URL = "http://localhost:11434"


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(
        self,
        base_url: str | None = None,
        *,
        profile_id: str | None = None,
        use_legacy_config: bool = True,
    ) -> None:
        if use_legacy_config:
            url = base_url or cfg_get(
                "ollama", "base_url", _DEFAULT_BASE_URL, env="OLLAMA_BASE_URL"
            )
        else:
            url = base_url or _DEFAULT_BASE_URL
        self.base_url = validate_base_url(
            url,
            source=(
                f"Provider Profile {profile_id!r} 的 base_url"
                if profile_id is not None
                else "Ollama base_url"
            ),
        )
        self._route_endpoint_fingerprint = _endpoint_fingerprint(self.base_url)
        self.profile_id = profile_id

    def route_id_for(self, model: str) -> str:
        return _stable_route_id(
            family="ollama-chat-v1",
            target=self._route_endpoint_fingerprint,
            model=model,
        )

    def usage_support_for(self, model: str) -> UsageSupport:
        del model
        return UsageSupport.REPORTED

    @staticmethod
    def _strict_output_token_cap(value: object, *, source: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{source} 必须是正整数")
        return value

    def resolve_output_token_cap(
        self,
        model: str,
        *,
        requested_cap: int | None,
        params: dict[str, object],
    ) -> int:
        del self, model
        if requested_cap is not None:
            requested_cap = OllamaProvider._strict_output_token_cap(
                requested_cap,
                source="LLM 单次输出 Token 上限",
            )
        configured_cap = OllamaProvider._strict_output_token_cap(
            params.get("num_predict", DEFAULT_MAX_OUTPUT_TOKENS),
            source="num_predict",
        )
        return configured_cap if requested_cap is None else min(configured_cap, requested_cap)

    @staticmethod
    def _payload(
        messages: list[dict],
        model: str,
        params: dict,
        *,
        output_token_cap: int | None = None,
    ) -> dict:
        options = dict(params)
        if output_token_cap is None:
            options.setdefault("num_predict", DEFAULT_MAX_OUTPUT_TOKENS)
        else:
            options["num_predict"] = OllamaProvider._strict_output_token_cap(
                output_token_cap,
                source="Provider 输出 Token 上限",
            )
        return {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": options,
        }

    @staticmethod
    def _usage_from_payload(payload: dict) -> ProviderUsage | None:
        input_tokens = payload.get("prompt_eval_count")
        output_tokens = payload.get("eval_count")
        if input_tokens is None or output_tokens is None:
            return None
        return ProviderUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        )

    @classmethod
    def _result_from_payload(cls, payload: dict) -> ProviderChatResult:
        return ProviderChatResult(
            text=payload["message"]["content"].strip(),
            usage=cls._usage_from_payload(payload),
            finish_reason=payload.get("done_reason"),
        )

    def _sync_response_payload(
        self,
        messages: list[dict],
        *,
        model: str,
        request_timeout: float | None = None,
        output_token_cap: int | None = None,
        **params,
    ) -> dict:
        try:
            resp = httpx.post(
                f"{self.base_url}/api/chat",
                json=self._payload(
                    messages,
                    model,
                    params,
                    output_token_cap=output_token_cap,
                ),
                timeout=120.0 if request_timeout is None else request_timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("Ollama 请求超时") from exc
        resp.raise_for_status()
        return resp.json()

    async def _async_response_payload(
        self,
        messages: list[dict],
        *,
        model: str,
        request_timeout: float | None = None,
        output_token_cap: int | None = None,
        **params,
    ) -> dict:
        timeout = 120.0 if request_timeout is None else request_timeout
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/api/chat",
                    json=self._payload(
                        messages,
                        model,
                        params,
                        output_token_cap=output_token_cap,
                    ),
                )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("Ollama 请求超时") from exc
        resp.raise_for_status()
        return resp.json()

    def chat(
        self,
        messages: list[dict],
        *,
        model: str,
        request_timeout: float | None = None,
        **params,
    ) -> str:
        payload = self._sync_response_payload(
            messages,
            model=model,
            request_timeout=request_timeout,
            **params,
        )
        return payload["message"]["content"].strip()

    def chat_with_usage(
        self,
        messages: list[dict],
        *,
        model: str,
        request_timeout: float | None = None,
        output_token_cap: int | None = None,
        **params,
    ) -> ProviderChatResult:
        payload = self._sync_response_payload(
            messages,
            model=model,
            request_timeout=request_timeout,
            output_token_cap=output_token_cap,
            **params,
        )
        return self._result_from_payload(payload)

    async def achat(
        self,
        messages: list[dict],
        *,
        model: str,
        request_timeout: float | None = None,
        **params,
    ) -> str:
        payload = await self._async_response_payload(
            messages,
            model=model,
            request_timeout=request_timeout,
            **params,
        )
        return payload["message"]["content"].strip()

    async def achat_with_usage(
        self,
        messages: list[dict],
        *,
        model: str,
        request_timeout: float | None = None,
        output_token_cap: int | None = None,
        **params,
    ) -> ProviderChatResult:
        payload = await self._async_response_payload(
            messages,
            model=model,
            request_timeout=request_timeout,
            output_token_cap=output_token_cap,
            **params,
        )
        return self._result_from_payload(payload)
