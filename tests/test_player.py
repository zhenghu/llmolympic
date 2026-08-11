"""Player failure isolation and cancellable LLM timeout tests."""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import threading
import time

import pytest

from llmolympic.core.player import (
    HumanPlayer,
    LLMPlayer,
    PlayerProviderError,
    PlayerTimeoutError,
)
from llmolympic.core.usage import (
    BudgetLimits,
    ProviderBudgetPolicy,
    RouteBudgetPolicy,
    TokenPrice,
    UsageBudget,
    UsageExceedsReservationError,
    UsageValidationError,
)
from llmolympic.providers.base import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    Provider,
    ProviderChatResult,
    ProviderUsage,
    UsageSupport,
)


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


class _UsageAsyncProvider(Provider):
    name = "usage"

    def __init__(self, result: ProviderChatResult) -> None:
        self.result = result
        self.calls = 0
        self.received_messages: list[dict] = []
        self.received_params: dict[str, object] = {}

    def chat(self, messages: list[dict], *, model: str, **params) -> str:
        raise AssertionError("usage test must use native async provider path")

    def usage_support_for(self, model: str) -> UsageSupport:
        return UsageSupport.REPORTED

    def resolve_output_token_cap(
        self,
        model: str,
        *,
        requested_cap: int | None,
        params: dict[str, object],
    ) -> int | None:
        if requested_cap is None:
            return None
        configured = params.get("max_tokens", requested_cap)
        assert isinstance(configured, int)
        return min(requested_cap, configured)

    async def achat(self, messages: list[dict], *, model: str, **params) -> str:
        raise AssertionError("usage test must use achat_with_usage")

    async def achat_with_usage(
        self,
        messages: list[dict],
        *,
        model: str,
        **params,
    ) -> ProviderChatResult:
        self.calls += 1
        self.received_messages = messages
        self.received_params = params
        return self.result


class _BlockingUsageProvider(_UsageAsyncProvider):
    def __init__(self) -> None:
        super().__init__(ProviderChatResult(text="never"))
        self.started = asyncio.Event()
        self.cancelled = False

    async def achat_with_usage(
        self,
        messages: list[dict],
        *,
        model: str,
        **params,
    ) -> ProviderChatResult:
        self.calls += 1
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


class _ProfileAsyncProvider(_ResponseAsyncProvider):
    name = "profile-provider"
    profile_id = "stable-profile"


class _InvalidRouteProvider(_ResponseAsyncProvider):
    def route_id_for(self, model: str) -> str:
        return "unsafe-route"


def _budget_policy(
    player: LLMPlayer,
    *,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    price: TokenPrice | None = None,
) -> ProviderBudgetPolicy:
    return ProviderBudgetPolicy(
        max_output_tokens_per_call=max_output_tokens,
        routes=(RouteBudgetPolicy(route_id=player.route_id, price=price),),
    )


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
    assert player.route_id.startswith("route:v1:")


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


def test_provider_route_identity_must_use_the_strict_opaque_format() -> None:
    with pytest.raises(ValueError, match="route_id"):
        LLMPlayer("invalid-route", _InvalidRouteProvider("A"), "model")


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


def test_complete_with_usage_preserves_metadata_while_text_apis_stay_compatible() -> None:
    usage = ProviderUsage(input_tokens=5, output_tokens=2, total_tokens=7)
    provider = _UsageAsyncProvider(
        ProviderChatResult(text="answer", usage=usage, finish_reason="stop")
    )
    player = LLMPlayer("usage:model", provider, "model", temperature=0.2)

    result = asyncio.run(player.complete_with_usage("prompt", system_prompt="system"))

    assert result == ProviderChatResult(text="answer", usage=usage, finish_reason="stop")
    assert provider.received_messages == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "prompt"},
    ]
    assert provider.received_params == {"request_timeout": 120.0, "temperature": 0.2}
    assert asyncio.run(player.complete("prompt")) == "answer"
    assert asyncio.run(player.get_move("prompt")) == "answer"
    assert provider.calls == 3


def test_complete_with_usage_wraps_legacy_async_provider_with_unknown_usage() -> None:
    player = LLMPlayer("legacy-async:model", _ResponseAsyncProvider("A"), "model")

    result = asyncio.run(player.complete_with_usage("prompt"))

    assert result == ProviderChatResult(text="A")


def test_usage_result_does_not_bypass_existing_response_size_error() -> None:
    provider = _UsageAsyncProvider(
        ProviderChatResult(
            text="sensitive-response",
            usage=ProviderUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        )
    )
    player = LLMPlayer("usage:model", provider, "model", max_response_chars=4)

    with pytest.raises(PlayerProviderError) as raised:
        asyncio.run(player.complete_with_usage("prompt"))

    assert raised.value.details["validation_error"] == "response_too_long"
    assert "sensitive-response" not in str(raised.value)


def test_budgeted_call_uses_canonical_byte_bound_exact_cap_and_ceiling_price() -> None:
    provider = _UsageAsyncProvider(
        ProviderChatResult(
            text="answer",
            usage=ProviderUsage(input_tokens=5, output_tokens=2, total_tokens=7),
        )
    )
    player = LLMPlayer("usage:model", provider, "model", max_tokens=19)
    budget = UsageBudget(BudgetLimits(calls=1, input=10_000, output=19, estimated_cost=1_000))
    price = TokenPrice(input_nanos_per_million=1, output_nanos_per_million=1)
    player.bind_usage_budget(
        budget,
        _budget_policy(player, max_output_tokens=23, price=price),
    )

    bound = player.call_bounds("雪", system_prompt="system")
    assert bound is not None
    canonical = json.dumps(
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "雪"},
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert bound.input == len(canonical)
    assert bound.output == 19
    assert bound.estimated_cost == 1
    assert bound.route_id == player.route_id

    result = asyncio.run(player.complete_with_usage("雪", system_prompt="system"))

    assert result.text == "answer"
    assert provider.received_params["output_token_cap"] == 19
    assert budget.reserved.calls == 0
    assert budget.spent.calls == 1
    assert budget.spent.input == 5
    assert budget.spent.output == 2
    assert budget.spent.estimated_cost == 1


def test_missing_usage_conservatively_charges_the_complete_bound() -> None:
    provider = _UsageAsyncProvider(ProviderChatResult(text="answer", usage=None))
    player = LLMPlayer("unknown-usage", provider, "model")
    budget = UsageBudget(BudgetLimits(calls=1, input=10_000, output=7))
    player.bind_usage_budget(
        budget,
        _budget_policy(player, max_output_tokens=7),
    )
    bound = player.call_bounds("prompt")
    assert bound is not None

    assert asyncio.run(player.complete("prompt")) == "answer"

    assert budget.reserved.calls == 0
    assert budget.spent == bound.as_totals()


def test_invalid_reported_counter_charges_bound_and_propagates_usage_error() -> None:
    provider = _UsageAsyncProvider(
        ProviderChatResult(
            text="answer",
            usage=ProviderUsage(
                input_tokens=2**63,
                output_tokens=0,
                total_tokens=2**63,
            ),
        )
    )
    player = LLMPlayer("invalid-counter", provider, "model")
    budget = UsageBudget(BudgetLimits(calls=1, input=10_000, output=7))
    player.bind_usage_budget(
        budget,
        _budget_policy(player, max_output_tokens=7),
    )
    bound = player.call_bounds("prompt")
    assert bound is not None

    with pytest.raises(UsageValidationError, match="SQLite"):
        asyncio.run(player.complete("prompt"))

    assert budget.spent == bound.as_totals()
    assert budget.reserved.calls == 0


def test_provider_failure_after_dispatch_charges_calls_only_bound() -> None:
    player = LLMPlayer("failed-call", _FailingAsyncProvider(), "model")
    budget = UsageBudget(BudgetLimits(calls=1))
    player.bind_usage_budget(budget, _budget_policy(player))

    with pytest.raises(PlayerProviderError):
        asyncio.run(player.complete("prompt"))

    assert budget.spent.calls == 1
    assert budget.reserved.calls == 0


def test_cancellation_after_dispatch_conservatively_charges_the_complete_bound() -> None:
    async def scenario() -> tuple[_BlockingUsageProvider, UsageBudget, object]:
        provider = _BlockingUsageProvider()
        player = LLMPlayer(
            "cancelled-usage",
            provider,
            "model",
            move_timeout_seconds=None,
        )
        budget = UsageBudget(BudgetLimits(calls=1, input=10_000, output=11))
        player.bind_usage_budget(
            budget,
            _budget_policy(player, max_output_tokens=11),
        )
        bound = player.call_bounds("prompt")
        assert bound is not None
        task = asyncio.create_task(player.complete("prompt"))
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return provider, budget, bound

    provider, budget, bound = asyncio.run(scenario())

    assert provider.cancelled
    assert budget.reserved.calls == 0
    assert budget.spent == bound.as_totals()  # type: ignore[union-attr]


def test_reported_overrun_propagates_usage_error_without_provider_wrapping() -> None:
    provider = _UsageAsyncProvider(
        ProviderChatResult(
            text="answer",
            usage=ProviderUsage(input_tokens=10_000, output_tokens=1, total_tokens=10_001),
        )
    )
    player = LLMPlayer("overrun", provider, "model")
    budget = UsageBudget(BudgetLimits())
    player.bind_usage_budget(
        budget,
        _budget_policy(player, max_output_tokens=4),
    )

    with pytest.raises(UsageExceedsReservationError):
        asyncio.run(player.complete("prompt"))

    assert provider.calls == 1
    assert budget.poisoned
    assert budget.reserved.calls == 0
    assert budget.spent.input == 10_000


def test_legacy_provider_only_allows_default_calls_only_budget_mode() -> None:
    player = LLMPlayer("legacy-calls", _ResponseAsyncProvider("A"), "model")
    calls_only = UsageBudget(BudgetLimits(calls=1))
    player.bind_usage_budget(calls_only, _budget_policy(player))

    assert asyncio.run(player.complete("prompt")) == "A"
    assert calls_only.spent.calls == 1

    token_limited = LLMPlayer("legacy-token", _ResponseAsyncProvider("A"), "model")
    with pytest.raises(UsageValidationError, match="does not report usage"):
        token_limited.bind_usage_budget(
            UsageBudget(BudgetLimits(input=100)),
            _budget_policy(token_limited),
        )

    custom_cap = LLMPlayer("legacy-cap", _ResponseAsyncProvider("A"), "model")
    with pytest.raises(UsageValidationError, match="does not report usage"):
        custom_cap.bind_usage_budget(
            UsageBudget(BudgetLimits(calls=1)),
            _budget_policy(custom_cap, max_output_tokens=17),
        )


def test_budget_binding_is_one_shot_and_must_precede_every_llm_call() -> None:
    first = LLMPlayer("one-shot", _UsageAsyncProvider(ProviderChatResult("A")), "model")
    budget = UsageBudget(BudgetLimits(calls=1))
    policy = _budget_policy(first)
    first.bind_usage_budget(budget, policy)

    with pytest.raises(UsageValidationError, match="already bound"):
        first.bind_usage_budget(budget, policy)

    late = LLMPlayer("late-bind", _UsageAsyncProvider(ProviderChatResult("A")), "model")
    asyncio.run(late.complete("prompt"))
    with pytest.raises(UsageValidationError, match="after an LLM call started"):
        late.bind_usage_budget(
            UsageBudget(BudgetLimits(calls=1)),
            _budget_policy(late),
        )


def test_budgeted_call_rejects_request_mutation_after_binding() -> None:
    provider = _UsageAsyncProvider(ProviderChatResult(text="answer"))
    player = LLMPlayer("mutating", provider, "model", temperature=0.2)
    budget = UsageBudget(BudgetLimits(calls=1))
    policy = _budget_policy(player)
    player.bind_usage_budget(budget, policy)

    player.model = "altered-model"
    with pytest.raises(UsageValidationError, match="changed after binding"):
        asyncio.run(player.get_move("prompt"))

    player.model = "model"
    player.sampling_params["max_tokens"] = 7
    with pytest.raises(UsageValidationError, match="changed after binding"):
        asyncio.run(player.get_move("prompt"))
    assert provider.calls == 0


def test_budgeted_call_rejects_new_forbidden_sampling_params_after_binding() -> None:
    provider = _UsageAsyncProvider(ProviderChatResult(text="answer"))
    player = LLMPlayer("forbidden", provider, "model")
    budget = UsageBudget(BudgetLimits(calls=1))
    player.bind_usage_budget(budget, _budget_policy(player))

    player.sampling_params["extra_body"] = {"max_tokens": 8}
    with pytest.raises(UsageValidationError, match="changed after binding"):
        asyncio.run(player.get_move("prompt"))
    assert provider.calls == 0


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
    other_model = LLMPlayer(
        "first",
        _ResponseAsyncProvider("A"),
        "other-model",
        seed=7,
        temperature=0.2,
    )

    assert first.entrant_id == reordered.entrant_id
    assert first.entrant_id != renamed.entrant_id
    assert first.entrant_id != resampled.entrant_id
    assert first.route_id == reordered.route_id == renamed.route_id == resampled.route_id
    assert first.route_id != other_model.route_id


def test_llm_route_identity_is_readonly_and_legacy_description_switch_is_one_way() -> None:
    player = LLMPlayer("player", _ResponseAsyncProvider("A"), "model")
    route_id = player.route_id

    assert len(route_id) == len("route:v1:") + 64
    assert player.describe()["route_id"] == route_id
    with pytest.raises(AttributeError):
        player.route_id = "route:v1:" + "0" * 64  # type: ignore[misc]

    player._use_legacy_route_description()

    assert player.route_id == route_id
    assert "route_id" not in player.describe()


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
    assert first.route_id == renamed.route_id == resampled.route_id
    assert first.describe()["route_id"] == first.route_id
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


@pytest.mark.skipif(os.name != "posix", reason="requires a selectable POSIX stdin fd")
def test_human_timeout_removes_stdin_reader_before_the_next_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    read_stream = os.fdopen(read_fd, "r", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", read_stream)

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        removed_fds: list[int] = []
        remove_reader = loop.remove_reader

        def track_remove_reader(fd: int) -> bool:
            removed_fds.append(fd)
            return remove_reader(fd)

        monkeypatch.setattr(loop, "remove_reader", track_remove_reader)
        with pytest.raises(PlayerTimeoutError):
            await HumanPlayer("first", timeout=0.02).get_move("prompt")
        assert removed_fds == [read_fd]

        os.write(write_fd, b"next-after-timeout\n")
        await asyncio.sleep(0.02)
        assert await HumanPlayer("second", timeout=0.5).get_move("prompt") == "next-after-timeout"
        assert removed_fds == [read_fd, read_fd]

    try:
        asyncio.run(scenario())
    finally:
        os.close(write_fd)
        read_stream.close()


@pytest.mark.skipif(os.name != "posix", reason="requires a selectable POSIX stdin fd")
def test_human_cancellation_removes_stdin_reader_before_the_next_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    read_stream = os.fdopen(read_fd, "r", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", read_stream)

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        removed_fds: list[int] = []
        remove_reader = loop.remove_reader

        def track_remove_reader(fd: int) -> bool:
            removed_fds.append(fd)
            return remove_reader(fd)

        monkeypatch.setattr(loop, "remove_reader", track_remove_reader)
        pending = asyncio.create_task(HumanPlayer("first", timeout=None).get_move("prompt"))
        for _ in range(3):
            await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert removed_fds == [read_fd]

        os.write(write_fd, b"next-after-cancel\n")
        await asyncio.sleep(0.02)
        assert await HumanPlayer("second", timeout=0.5).get_move("prompt") == "next-after-cancel"
        assert removed_fds == [read_fd, read_fd]

    try:
        asyncio.run(scenario())
    finally:
        os.close(write_fd)
        read_stream.close()


@pytest.mark.skipif(os.name != "posix", reason="requires a selectable POSIX stdin fd")
def test_partial_pipe_input_does_not_block_cancellation_and_is_not_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    read_stream = os.fdopen(read_fd, "r", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", read_stream)
    finish_line = threading.Timer(0.2, os.write, args=(write_fd, b"-rest\n"))

    async def scenario() -> None:
        pending = asyncio.create_task(HumanPlayer("first", timeout=None).get_move("prompt"))
        for _ in range(3):
            await asyncio.sleep(0)

        os.write(write_fd, b"partial")
        finish_line.start()
        await asyncio.sleep(0.02)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

        assert await HumanPlayer("second", timeout=0.5).get_move("prompt") == "partial-rest"

    try:
        asyncio.run(scenario())
    finally:
        finish_line.cancel()
        finish_line.join()
        os.close(write_fd)
        read_stream.close()


@pytest.mark.skipif(os.name != "posix", reason="requires a selectable POSIX stdin fd")
def test_split_multibyte_pipe_input_waits_for_complete_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    read_stream = os.fdopen(read_fd, "r", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", read_stream)
    encoded = "答案\n".encode()

    async def scenario() -> None:
        pending = asyncio.create_task(HumanPlayer("human", timeout=0.5).get_move("prompt"))
        for _ in range(3):
            await asyncio.sleep(0)
        os.write(write_fd, encoded[:2])
        await asyncio.sleep(0.02)
        assert not pending.done()
        os.write(write_fd, encoded[2:])
        assert await pending == "答案"

    try:
        asyncio.run(scenario())
    finally:
        os.close(write_fd)
        read_stream.close()


@pytest.mark.skipif(os.name != "posix", reason="requires a POSIX pseudo-terminal")
@pytest.mark.parametrize(
    ("payload", "expected"),
    ((b"\x04", None), (b"partial-tty\x04", "partial-tty")),
    ids=("empty-eof", "partial-line-eof"),
)
def test_tty_ctrl_d_completes_without_hanging(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    expected: str | None,
) -> None:
    import pty

    master_fd, slave_fd = pty.openpty()
    slave_stream = os.fdopen(slave_fd, "r", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", slave_stream)

    async def scenario() -> None:
        pending = asyncio.create_task(HumanPlayer("human", timeout=0.5).get_move("prompt"))
        for _ in range(3):
            await asyncio.sleep(0)
        os.write(master_fd, payload)
        if expected is None:
            with pytest.raises(EOFError):
                await pending
        else:
            assert await pending == expected

    try:
        asyncio.run(scenario())
    finally:
        os.close(master_fd)
        slave_stream.close()


@pytest.mark.skipif(os.name != "posix", reason="requires a selectable POSIX stdin fd")
def test_invalid_stdin_encoding_is_not_silently_discarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    read_stream = os.fdopen(read_fd, "r", encoding="utf-8", errors="strict")
    monkeypatch.setattr(sys, "stdin", read_stream)

    async def scenario() -> None:
        pending = asyncio.create_task(HumanPlayer("human", timeout=0.5).get_move("prompt"))
        for _ in range(3):
            await asyncio.sleep(0)
        os.write(write_fd, b"\xff")
        with pytest.raises(UnicodeDecodeError):
            await pending

    try:
        asyncio.run(scenario())
    finally:
        os.close(write_fd)
        read_stream.close()


@pytest.mark.skipif(os.name != "posix", reason="requires a selectable POSIX stdin fd")
def test_extra_pipe_lines_are_buffered_for_later_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    read_stream = os.fdopen(read_fd, "r", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", read_stream)

    async def scenario() -> None:
        os.write(write_fd, b"first-line\nsecond-line\n")

        assert await HumanPlayer("first", timeout=0.5).get_move("prompt") == "first-line"
        assert await HumanPlayer("second", timeout=0.5).get_move("prompt") == "second-line"

    try:
        asyncio.run(scenario())
    finally:
        os.close(write_fd)
        read_stream.close()


@pytest.mark.skipif(os.name != "posix", reason="requires a selectable POSIX stdin fd")
def test_human_reads_a_line_already_prefetched_by_text_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    read_stream = os.fdopen(read_fd, "r", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", read_stream)

    try:
        os.write(write_fd, b"consumed-first\nprefetched-second\n")
        assert read_stream.readline() == "consumed-first\n"

        # BufferedReader normally fetched both lines in its first raw read, so
        # the descriptor itself is no longer readable.  HumanPlayer must still
        # drain TextIOWrapper's read-ahead instead of waiting for new fd bytes.
        assert (
            asyncio.run(HumanPlayer("human", timeout=0.1).get_move("prompt")) == "prefetched-second"
        )
    finally:
        os.close(write_fd)
        read_stream.close()


def test_human_custom_input_function_remains_supported() -> None:
    prompts: list[str] = []

    def custom_input(prompt: str) -> str:
        prompts.append(prompt)
        return "custom-answer"

    player = HumanPlayer("human", timeout=0.5, input_fn=custom_input)

    assert asyncio.run(player.get_move("ignored game prompt")) == "custom-answer"
    assert prompts == ["human，请输入你的答案 > "]


def test_human_default_input_falls_back_when_stdin_has_no_file_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("fallback-answer\n"))

    assert asyncio.run(HumanPlayer("human", timeout=0.5).get_move("prompt")) == "fallback-answer"
