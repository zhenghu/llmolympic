"""Resolve trusted configuration into one frozen Provider budget policy."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from llmolympic.config import ProviderBudgetSettings, ProviderTokenPrice
from llmolympic.core.player import LLMPlayer, Player
from llmolympic.core.usage import (
    BudgetLimits,
    ProviderBudgetPolicy,
    RouteBudgetPolicy,
    TokenPrice,
    UsageValidationError,
    usd_per_million_to_nanodollars,
    usd_to_nanodollar_limit,
)
from llmolympic.providers.base import DEFAULT_MAX_OUTPUT_TOKENS, UsageSupport

_DYNAMIC_OPENROUTER_MODELS = frozenset({"openrouter/auto", "openrouter/free"})


@dataclass(frozen=True, slots=True)
class ResolvedProviderBudget:
    """Limits and safe policy ready to bind to every LLM in one run."""

    limits: BudgetLimits
    policy: ProviderBudgetPolicy

    @property
    def canonical_json(self) -> str:
        payload = {
            "limits": {
                "calls": self.limits.calls,
                "estimated_cost": self.limits.estimated_cost,
                "input": self.limits.input,
                "output": self.limits.output,
            },
            "policy": json.loads(self.policy.canonical_json()),
            "version": 1,
        }
        return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            b"llmolympic-resolved-provider-budget-v1\0" + self.canonical_json.encode("ascii")
        ).hexdigest()

    def budget_id_for(self, scope_id: str) -> str:
        if not isinstance(scope_id, str) or not scope_id or len(scope_id) > 512:
            raise UsageValidationError("budget scope id must be a bounded non-empty string")
        digest = hashlib.sha256(
            b"llmolympic-provider-budget-scope-v1\0"
            + scope_id.encode("utf-8")
            + b"\0"
            + self.digest.encode("ascii")
        ).hexdigest()
        return f"budget:v1:{digest}"


def budget_is_enabled(settings: ProviderBudgetSettings) -> bool:
    """Return whether configuration changes or caps Provider dispatch."""

    if not isinstance(settings, ProviderBudgetSettings):
        raise UsageValidationError("settings must be ProviderBudgetSettings")
    return settings.max_output_tokens_per_call != DEFAULT_MAX_OUTPUT_TOKENS or any(
        value is not None
        for value in (
            settings.max_provider_calls,
            settings.max_input_tokens,
            settings.max_total_output_tokens,
            settings.max_estimated_cost_usd,
        )
    )


def _player_price_spec(player: LLMPlayer) -> str:
    if player.profile_id is not None:
        return f"profile:{player.profile_id}:{player.model}"
    return f"{player.provider.name}:{player.model}"


def _configured_price(value: ProviderTokenPrice) -> TokenPrice:
    if not isinstance(value, ProviderTokenPrice):
        raise UsageValidationError("pricing entries must be ProviderTokenPrice")
    return TokenPrice(
        input_nanos_per_million=usd_per_million_to_nanodollars(value.input_usd_per_million_tokens),
        output_nanos_per_million=usd_per_million_to_nanodollars(
            value.output_usd_per_million_tokens
        ),
    )


def _usage_support(player: LLMPlayer) -> UsageSupport:
    support_for = getattr(player.provider, "usage_support_for", None)
    if not callable(support_for):
        return UsageSupport.NONE
    value = support_for(player.model)
    try:
        return UsageSupport(value)
    except (TypeError, ValueError) as exc:
        raise UsageValidationError("Provider returned invalid usage support") from exc


def _is_dynamic_cost_route(player: LLMPlayer) -> bool:
    if player.provider.name != "openai":
        return False
    model = player.model.casefold()
    return model in _DYNAMIC_OPENROUTER_MODELS or model.startswith("openrouter/auto-")


def resolve_provider_budget(
    players: Iterable[Player],
    settings: ProviderBudgetSettings,
    pricing: Mapping[str, ProviderTokenPrice],
) -> ResolvedProviderBudget | None:
    """Resolve exact specs to route prices and reject ambiguous cost authority."""

    if not budget_is_enabled(settings):
        return None
    try:
        participants = tuple(players)
    except TypeError as exc:
        raise UsageValidationError("players must be iterable") from exc
    if not isinstance(pricing, Mapping):
        raise UsageValidationError("pricing must be a mapping")

    limits = BudgetLimits(
        calls=settings.max_provider_calls,
        input=settings.max_input_tokens,
        output=settings.max_total_output_tokens,
        estimated_cost=(
            None
            if settings.max_estimated_cost_usd is None
            else usd_to_nanodollar_limit(settings.max_estimated_cost_usd)
        ),
    )
    prices_by_route: dict[str, tuple[TokenPrice | None, bool]] = {}
    for player in participants:
        if not isinstance(player, LLMPlayer):
            continue
        if limits.estimated_cost is not None and _is_dynamic_cost_route(player):
            raise UsageValidationError(
                "dynamic OpenRouter routes cannot be used with an estimated-cost hard limit"
            )
        raw_spec = _player_price_spec(player)
        raw_price = pricing.get(raw_spec)
        if raw_price is not None:
            price: TokenPrice | None = _configured_price(raw_price)
        elif _usage_support(player) is UsageSupport.EXACT_ZERO or player.provider.name == "ollama":
            price = TokenPrice(0, 0)
        else:
            price = None

        if limits.estimated_cost is not None and price is None and raw_price is None:
            raise UsageValidationError(
                f"estimated-cost hard limit requires an explicit price for every cloud Provider route; "
                f"missing {raw_spec!r}"
            )

        route_id = player.route_id
        previous_price, previous_had_unpriced = prices_by_route.get(route_id, (None, False))
        if previous_price is not None and price is not None and previous_price != price:
            raise UsageValidationError("the same Provider route has conflicting token prices")
        if previous_had_unpriced and price is not None:
            raise UsageValidationError(
                "the same Provider route has mixed priced and unpriced specs"
            )
        if previous_price is None and price is not None:
            previous_price = price

        had_unpriced = previous_had_unpriced or (price is None and raw_price is None)
        prices_by_route[route_id] = (previous_price, had_unpriced)

    prices_by_route_canonical: dict[str, TokenPrice | None] = {}
    for route_id, (price, _) in prices_by_route.items():
        if limits.estimated_cost is not None and price is None:
            raise UsageValidationError(
                "the estimated-cost hard limit requires a price for every cloud Provider route"
            )
        prices_by_route_canonical[route_id] = price

    policy = ProviderBudgetPolicy(
        max_output_tokens_per_call=settings.max_output_tokens_per_call,
        routes=tuple(
            RouteBudgetPolicy(route_id=route_id, price=price)
            for route_id, price in prices_by_route_canonical.items()
        ),
    )
    return ResolvedProviderBudget(limits=limits, policy=policy)


__all__ = [
    "ResolvedProviderBudget",
    "budget_is_enabled",
    "resolve_provider_budget",
]
