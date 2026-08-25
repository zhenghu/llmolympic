from decimal import Decimal

import pytest

from llmolympic.config import ProviderBudgetSettings, ProviderTokenPrice
from llmolympic.core.budget_config import budget_is_enabled, resolve_provider_budget
from llmolympic.core.player import LLMPlayer
from llmolympic.core.usage import UsageValidationError
from llmolympic.providers.mock import MockProvider
from llmolympic.providers.openai_provider import OpenAIProvider


def _settings(**values: object) -> ProviderBudgetSettings:
    return ProviderBudgetSettings(**values)  # type: ignore[arg-type]


def test_default_budget_is_disabled_but_changed_output_cap_enables_it() -> None:
    assert budget_is_enabled(ProviderBudgetSettings()) is False
    assert budget_is_enabled(_settings(max_output_tokens_per_call=256)) is True
    assert budget_is_enabled(_settings(max_provider_calls=0)) is True


def test_resolved_budget_freezes_zero_cost_mock_route_and_stable_scope_id() -> None:
    player = LLMPlayer("mock", MockProvider("fixed"), "fixed")

    resolved = resolve_provider_budget(
        [player],
        _settings(max_provider_calls=2, max_estimated_cost_usd=Decimal(0)),
        {},
    )

    assert resolved is not None
    assert resolved.limits.calls == 2
    assert resolved.limits.estimated_cost == 0
    assert (
        resolved.policy.price_for(player.route_id).estimate(  # type: ignore[union-attr]
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        == 0
    )
    first = resolved.budget_id_for("tournament", "shared-scope")
    assert first.startswith("budget:v2:")
    assert first == resolved.budget_id_for("tournament", "shared-scope")
    assert first == resolved.budget_id_for("shared-scope")
    assert first != resolved.budget_id_for("tournament", "other-scope")
    assert first != resolved.budget_id_for("championship", "shared-scope")


@pytest.mark.parametrize(
    ("scope_kind", "scope_id"),
    [
        ("", "scope"),
        ("Championship", "scope"),
        ("championship/unsafe", "scope"),
        ("championship", ""),
        ("championship", "x" * 513),
    ],
)
def test_budget_scope_rejects_ambiguous_or_unbounded_inputs(
    scope_kind: str,
    scope_id: str,
) -> None:
    player = LLMPlayer("mock", MockProvider("fixed"), "fixed")
    resolved = resolve_provider_budget(
        [player],
        _settings(max_provider_calls=1),
        {},
    )

    assert resolved is not None
    with pytest.raises(UsageValidationError, match="budget scope"):
        resolved.budget_id_for(scope_kind, scope_id)


def test_cloud_cost_budget_requires_exact_price() -> None:
    player = LLMPlayer(
        "cloud",
        OpenAIProvider(api_key="test-key", base_url="https://api.example/v1"),
        "model-a",
    )

    with pytest.raises(UsageValidationError, match="price for every cloud"):
        resolve_provider_budget(
            [player],
            _settings(max_estimated_cost_usd=Decimal(1)),
            {},
        )

    resolved = resolve_provider_budget(
        [player],
        _settings(max_estimated_cost_usd=Decimal(1)),
        {
            "openai:model-a": ProviderTokenPrice(
                input_usd_per_million_tokens=Decimal("0.0000000001"),
                output_usd_per_million_tokens=Decimal(2),
            )
        },
    )
    assert resolved is not None
    price = resolved.policy.price_for(player.route_id)
    assert price is not None
    assert price.input_nanos_per_million == 1
    assert price.output_nanos_per_million == 2_000_000_000


def test_same_route_aliases_cannot_supply_conflicting_prices() -> None:
    direct = LLMPlayer(
        "direct",
        OpenAIProvider(api_key="test-key", base_url="https://api.example/v1"),
        "model-a",
    )
    profile = LLMPlayer(
        "profile",
        OpenAIProvider(
            api_key="test-key",
            base_url="https://api.example/v1/",
            profile_id="judge",
        ),
        "model-a",
    )
    assert direct.route_id == profile.route_id

    with pytest.raises(UsageValidationError, match="conflicting"):
        resolve_provider_budget(
            [direct, profile],
            _settings(max_provider_calls=2),
            {
                "openai:model-a": ProviderTokenPrice(Decimal(1), Decimal(1)),
                "profile:judge:model-a": ProviderTokenPrice(Decimal(2), Decimal(2)),
            },
        )


def test_same_route_mixed_explicit_and_missing_price_is_rejected() -> None:
    direct = LLMPlayer(
        "direct",
        OpenAIProvider(api_key="test-key", base_url="https://api.example/v1"),
        "model-a",
    )
    profile = LLMPlayer(
        "profile",
        OpenAIProvider(
            api_key="test-key",
            base_url="https://api.example/v1/",
            profile_id="judge",
        ),
        "model-a",
    )

    with pytest.raises(UsageValidationError, match="explicit price"):
        resolve_provider_budget(
            [direct, profile],
            _settings(max_estimated_cost_usd=Decimal(1)),
            {"openai:model-a": ProviderTokenPrice(Decimal("0.01"), Decimal("0.02"))},
        )


@pytest.mark.parametrize(
    "model",
    ["openrouter/auto", "openrouter/auto-beta", "openrouter/free"],
)
def test_dynamic_openrouter_route_is_rejected_in_cost_mode(model: str) -> None:
    player = LLMPlayer(
        "dynamic",
        OpenAIProvider(api_key="test-key", base_url="https://openrouter.ai/api/v1"),
        model,
    )

    with pytest.raises(UsageValidationError, match="dynamic OpenRouter"):
        resolve_provider_budget(
            [player],
            _settings(max_estimated_cost_usd=Decimal(1)),
            {
                f"openai:{model}": ProviderTokenPrice(Decimal(1), Decimal(1)),
            },
        )
