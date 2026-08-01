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
