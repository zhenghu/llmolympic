"""Player failure isolation and cancellable LLM timeout tests."""

from __future__ import annotations

import asyncio
import time

import pytest

from llmolympic.core.player import (
    HumanPlayer,
    LLMPlayer,
    PlayerProviderError,
    PlayerTimeoutError,
)
from llmolympic.providers.base import Provider


class _SlowAsyncProvider(Provider):
    name = "slow"

    def __init__(self) -> None:
        self.cancelled = False

    def chat(self, messages: list[dict], *, model: str, **params) -> str:
        raise AssertionError("timeout test must use native async provider path")

    async def achat(self, messages: list[dict], *, model: str, **params) -> str:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return "never"


class _FailingAsyncProvider(Provider):
    name = "failing"

    def chat(self, messages: list[dict], *, model: str, **params) -> str:
        raise AssertionError("failure test must use native async provider path")

    async def achat(self, messages: list[dict], *, model: str, **params) -> str:
        raise RuntimeError("sensitive-token-must-not-be-archived")


class _LegacySyncProvider(Provider):
    name = "legacy"

    def chat(self, messages: list[dict], *, model: str) -> str:
        return "A"


class _DuckSyncProvider:
    name = "duck"

    def chat(self, messages: list[dict], *, model: str) -> str:
        return "B"


class _SyncAchatProvider(Provider):
    name = "sync-achat"

    def chat(self, messages: list[dict], *, model: str, **params) -> str:
        return "A"

    def achat(self, messages: list[dict], *, model: str, **params) -> str:
        return "A"


class _ResponseAsyncProvider(Provider):
    name = "response"

    def __init__(self, response: object) -> None:
        self.response = response
        self.received_params: dict[str, object] = {}

    def chat(self, messages: list[dict], *, model: str, **params) -> str:
        raise AssertionError("response validation test must use native async provider path")

    async def achat(self, messages: list[dict], *, model: str, **params) -> str:
        self.received_params = params
        return self.response  # type: ignore[return-value]


class _ProfileAsyncProvider(_ResponseAsyncProvider):
    name = "profile-provider"
    profile_id = "stable-profile"


def test_native_async_provider_is_cancelled_at_llm_move_timeout() -> None:
    provider = _SlowAsyncProvider()
    player = LLMPlayer(
        "slow:model",
        provider,
        "model",
        move_timeout_seconds=0.01,
    )

    started = time.monotonic()
    with pytest.raises(PlayerTimeoutError) as raised:
        asyncio.run(player.get_move("prompt"))

    assert time.monotonic() - started < 0.5
    assert provider.cancelled
    assert raised.value.technical_loss
    assert raised.value.reason_code == "timeout"
    assert raised.value.details["timeout_seconds"] == 0.01


def test_provider_exception_is_wrapped_without_exposing_original_message() -> None:
    player = LLMPlayer("bad:model", _FailingAsyncProvider(), "model")

    with pytest.raises(PlayerProviderError) as raised:
        asyncio.run(player.get_move("prompt"))

    assert raised.value.technical_loss
    assert raised.value.reason_code == "provider_error"
    assert raised.value.details["error_type"] == "RuntimeError"
    assert "sensitive-token" not in str(raised.value)


def test_legacy_sync_provider_remains_available_without_hard_timeout() -> None:
    player = LLMPlayer(
        "legacy:model",
        _LegacySyncProvider(),
        "model",
        move_timeout_seconds=None,
    )

    assert asyncio.run(player.get_move("prompt")) == "A"


def test_chat_only_duck_provider_remains_available_without_hard_timeout() -> None:
    player = LLMPlayer(  # type: ignore[arg-type]
        "duck:model",
        _DuckSyncProvider(),
        "model",
        move_timeout_seconds=None,
    )

    assert asyncio.run(player.get_move("prompt")) == "B"


def test_legacy_sync_provider_cannot_claim_reliable_hard_timeout() -> None:
    with pytest.raises(ValueError, match="没有原生异步调用"):
        LLMPlayer(
            "legacy:model",
            _LegacySyncProvider(),
            "model",
            move_timeout_seconds=1.0,
        )

    with pytest.raises(ValueError, match="没有原生异步调用"):
        LLMPlayer(  # type: ignore[arg-type]
            "duck:model",
            _DuckSyncProvider(),
            "model",
            move_timeout_seconds=1.0,
        )


def test_sync_achat_implementation_cannot_claim_reliable_hard_timeout() -> None:
    with pytest.raises(ValueError, match="没有原生异步调用"):
        LLMPlayer("sync:model", _SyncAchatProvider(), "model")


def test_llm_player_defaults_to_safe_move_timeout() -> None:
    player = LLMPlayer("slow:model", _SlowAsyncProvider(), "model")

    assert player.move_timeout_seconds == 120.0


def test_reserved_provider_request_timeout_is_rejected_before_match() -> None:
    with pytest.raises(ValueError, match="内部保留参数"):
        LLMPlayer(
            "slow:model",
            _SlowAsyncProvider(),
            "model",
            request_timeout=1.0,
        )


def test_llm_response_limit_is_recorded_but_not_sent_to_provider() -> None:
    provider = _ResponseAsyncProvider("four")
    player = LLMPlayer(
        "response:model",
        provider,
        "model",
        max_response_chars=4,
        temperature=0.2,
    )

    assert asyncio.run(player.get_move("prompt")) == "four"
    assert player.describe()["max_response_chars"] == 4
    assert provider.received_params["temperature"] == 0.2
    assert "max_response_chars" not in provider.received_params


def test_llm_response_over_limit_is_a_sanitized_technical_loss() -> None:
    player = LLMPlayer(
        "response:model",
        _ResponseAsyncProvider("sensitive-response"),
        "model",
        max_response_chars=4,
    )

    with pytest.raises(PlayerProviderError) as raised:
        asyncio.run(player.get_move("prompt"))

    assert raised.value.technical_loss
    assert raised.value.reason_code == "provider_error"
    assert raised.value.details["validation_error"] == "response_too_long"
    assert "sensitive-response" not in str(raised.value)
    assert "sensitive-response" not in repr(raised.value.details)


def test_non_string_llm_response_is_a_sanitized_technical_loss() -> None:
    player = LLMPlayer(
        "response:model",
        _ResponseAsyncProvider({"secret": "must-not-escape"}),
        "model",
    )

    with pytest.raises(PlayerProviderError) as raised:
        asyncio.run(player.get_move("prompt"))

    assert raised.value.technical_loss
    assert raised.value.details["validation_error"] == "non_string_response"
    assert "must-not-escape" not in str(raised.value)
    assert "must-not-escape" not in repr(raised.value.details)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, 4097])
def test_llm_response_limit_must_be_within_platform_bounds(limit: object) -> None:
    with pytest.raises(ValueError, match="1 到 4096"):
        LLMPlayer(
            "response:model",
            _ResponseAsyncProvider("A"),
            "model",
            max_response_chars=limit,  # type: ignore[arg-type]
        )


def test_sampling_params_are_recursively_redacted_only_in_archive_description() -> None:
    provider = _ResponseAsyncProvider("A")
    extra_headers = {
        "Authorization": "Bearer provider-secret",
        "X-Trace": "safe-trace",
    }
    player = LLMPlayer(
        "response:model",
        provider,
        "model",
        max_tokens=64,
        extra_headers=extra_headers,
        auth="Bearer auth-secret",
        jwt="jwt-secret",
        metadata={
            "nested": [
                {"api_key": "nested-secret", "label": "safe-label"},
                {"access-token": "token-secret"},
            ]
        },
    )

    assert asyncio.run(player.get_move("prompt")) == "A"
    description = player.describe()

    assert provider.received_params["extra_headers"] is extra_headers
    assert provider.received_params["metadata"]["nested"][0]["api_key"] == "nested-secret"
    assert description["sampling_params"] == {
        "max_tokens": 64,
        "extra_headers": "[REDACTED]",
        "auth": "[REDACTED]",
        "jwt": "[REDACTED]",
        "metadata": "[REDACTED]",
    }


def test_direct_llm_identity_is_stable_order_independent_and_disambiguates_name() -> None:
    first = LLMPlayer(
        "first",
        _ResponseAsyncProvider("A"),
        "model",
        seed=7,
        temperature=0.2,
    )
    reordered = LLMPlayer(
        "first",
        _ResponseAsyncProvider("A"),
        "model",
        temperature=0.2,
        seed=7,
    )
    renamed = LLMPlayer(
        "second",
        _ResponseAsyncProvider("A"),
        "model",
        seed=7,
        temperature=0.2,
    )
    resampled = LLMPlayer(
        "first",
        _ResponseAsyncProvider("A"),
        "model",
        seed=8,
        temperature=0.2,
    )

    assert first.entrant_id == reordered.entrant_id
    assert first.entrant_id != renamed.entrant_id
    assert first.entrant_id != resampled.entrant_id


def test_profile_llm_identity_uses_documented_profile_and_model_key() -> None:
    first = LLMPlayer(
        "old display",
        _ProfileAsyncProvider("A"),
        "model",
        seed=7,
        temperature=0.2,
    )
    renamed = LLMPlayer(
        "new display",
        _ProfileAsyncProvider("A"),
        "model",
        temperature=0.2,
        seed=7,
    )
    resampled = LLMPlayer(
        "old display",
        _ProfileAsyncProvider("A"),
        "model",
        seed=8,
        temperature=0.2,
    )

    assert first.entrant_id == renamed.entrant_id == resampled.entrant_id
    assert first.entrant_id == "profile:stable-profile:model"
    assert first.describe()["profile_id"] == "stable-profile"


def test_explicit_entrant_identity_and_readonly_display_compatibility() -> None:
    player = HumanPlayer("Display", entrant_id="profile:human")

    assert player.entrant_id == "profile:human"
    assert player.display_name == player.name == "Display"
    assert player.describe() == {
        "name": "Display",
        "display_name": "Display",
        "entrant_id": "profile:human",
        "kind": "human",
    }
    with pytest.raises(AttributeError):
        player.display_name = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "entrant_id",
    ["", "x" * 257, "bad\nvalue", "bad\u0085value", "bad\u202evalue"],
)
def test_entrant_id_rejects_length_control_and_bidi_hazards(entrant_id: str) -> None:
    with pytest.raises(ValueError, match="entrant_id"):
        HumanPlayer("human", entrant_id=entrant_id)


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), float("inf")])
def test_llm_move_timeout_must_be_positive_and_finite(timeout: float) -> None:
    with pytest.raises(ValueError, match="有限秒数"):
        LLMPlayer(
            "slow:model",
            _SlowAsyncProvider(),
            "model",
            move_timeout_seconds=timeout,
        )


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), float("inf")])
def test_human_timeout_must_be_positive_and_finite(timeout: float) -> None:
    with pytest.raises(ValueError, match="有限秒数"):
        HumanPlayer("human", timeout=timeout)
