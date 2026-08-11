import asyncio
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier, Lock

import pytest

from llmolympic.core.usage import (
    NANODOLLARS_PER_USD,
    SQLITE_INT_MAX,
    BudgetExceededError,
    BudgetLimits,
    BudgetPoisonedError,
    CallBounds,
    ProviderBudgetPolicy,
    ReservationStateError,
    RouteBudgetPolicy,
    TokenPrice,
    UsageBudget,
    UsageCounterOverflowError,
    UsageExceedsReservationError,
    UsageTotals,
    UsageValidationError,
    usd_per_million_to_nanodollars,
    usd_to_nanodollar_limit,
)

ZERO = UsageTotals(calls=0, input=0, output=0, estimated_cost=0)
ROUTE_A = f"route:v1:{'a' * 64}"
ROUTE_B = f"route:v1:{'b' * 64}"


def test_decimal_money_conversion_never_expands_authority_or_underprices() -> None:
    assert usd_to_nanodollar_limit(Decimal("1.0000000009")) == 1_000_000_000
    assert usd_per_million_to_nanodollars(Decimal("0.0000000001")) == 1
    assert usd_to_nanodollar_limit(Decimal("0.000000001999999999999999999999999999999")) == 1
    assert usd_per_million_to_nanodollars(Decimal("0.000000001000000000000000000000000000001")) == 2

    price = TokenPrice(input_nanos_per_million=1, output_nanos_per_million=2)
    assert price.estimate(input_tokens=1, output_tokens=0) == 1
    assert price.estimate(input_tokens=0, output_tokens=1) == 1
    assert price.estimate(input_tokens=1_000_000, output_tokens=1_000_000) == 3


def test_budget_policy_is_canonical_versioned_and_credential_free() -> None:
    price = TokenPrice(input_nanos_per_million=100, output_nanos_per_million=200)
    policy = ProviderBudgetPolicy(
        max_output_tokens_per_call=256,
        routes=(
            RouteBudgetPolicy(route_id=ROUTE_B),
            RouteBudgetPolicy(route_id=ROUTE_A, price=price),
        ),
    )

    encoded = policy.canonical_json()
    restored = ProviderBudgetPolicy.from_canonical_json(encoded)

    assert restored == policy
    assert restored.routes[0].route_id == ROUTE_A
    assert restored.price_for(ROUTE_A) == price
    assert restored.price_for(ROUTE_B) is None
    assert len(restored.digest) == 64
    assert "endpoint" not in encoded
    assert "api_key" not in encoded


def test_budget_policy_rejects_duplicate_routes_and_noncanonical_json() -> None:
    with pytest.raises(UsageValidationError, match="unique"):
        ProviderBudgetPolicy(
            max_output_tokens_per_call=1,
            routes=(RouteBudgetPolicy(ROUTE_A), RouteBudgetPolicy(ROUTE_A)),
        )

    policy = ProviderBudgetPolicy(
        max_output_tokens_per_call=1,
        routes=(RouteBudgetPolicy(ROUTE_A),),
    )
    with pytest.raises(UsageValidationError, match="canonical"):
        ProviderBudgetPolicy.from_canonical_json(f" {policy.canonical_json()}")


@pytest.mark.parametrize("value", [True, False, -1, 1.0, "1", SQLITE_INT_MAX + 1])
def test_usage_values_are_strict_non_negative_sqlite_integers(value: object) -> None:
    with pytest.raises(UsageValidationError) as raised:
        CallBounds(input=value)  # type: ignore[arg-type]

    assert raised.value.reason_code == "invalid_usage_value"


def test_budget_limits_are_independently_optional_and_cost_is_nanodollars() -> None:
    limits = BudgetLimits(estimated_cost=2 * NANODOLLARS_PER_USD)
    budget = UsageBudget(limits)

    reservation = budget.reserve(CallBounds(input=100, output=50, estimated_cost=1_500_000_000))

    assert reservation.state == "reserved"
    assert budget.spent == ZERO
    assert budget.reserved == UsageTotals(
        calls=1,
        input=100,
        output=50,
        estimated_cost=1_500_000_000,
    )


def test_reserve_many_is_atomic_when_any_limit_would_be_exceeded() -> None:
    budget = UsageBudget(BudgetLimits(calls=2, input=15, output=20, estimated_cost=30))
    first = budget.reserve(CallBounds(input=5, output=5, estimated_cost=5))

    with pytest.raises(BudgetExceededError) as raised:
        budget.reserve_many(
            (
                CallBounds(input=5, output=5, estimated_cost=5),
                CallBounds(input=6, output=5, estimated_cost=5),
            )
        )

    assert raised.value.reason_code == "budget_exceeded"
    assert raised.value.dimension == "calls"
    assert first.state == "reserved"
    assert budget.spent == ZERO
    assert budget.reserved == UsageTotals(calls=1, input=5, output=5, estimated_cost=5)


def test_reserve_many_materializes_and_validates_before_mutation() -> None:
    budget = UsageBudget(BudgetLimits(calls=3))

    with pytest.raises(UsageValidationError, match="CallBounds"):
        budget.reserve_many([CallBounds(), object()])  # type: ignore[list-item]

    assert budget.spent == ZERO
    assert budget.reserved == ZERO
    assert budget.reserve_many(()) == ()


def test_dispatch_and_settle_release_unused_reservation() -> None:
    budget = UsageBudget(BudgetLimits(calls=1, input=100, output=50, estimated_cost=1_000))
    reservation = budget.reserve(CallBounds(input=100, output=50, estimated_cost=1_000))
    actual = UsageTotals(calls=1, input=70, output=20, estimated_cost=650)

    assert reservation.dispatch().dispatch() is reservation
    assert reservation.settle(actual) == actual
    assert reservation.settle(actual) == actual
    assert reservation.state == "settled"
    assert reservation.actual == actual
    assert budget.reserved == ZERO
    assert budget.spent == actual


def test_conflicting_second_settlement_fails_without_mutation() -> None:
    budget = UsageBudget(BudgetLimits(calls=1))
    reservation = budget.reserve(CallBounds(input=10)).dispatch()
    actual = UsageTotals(calls=1, input=3, output=0, estimated_cost=0)
    reservation.settle(actual)

    with pytest.raises(ReservationStateError) as raised:
        reservation.settle(UsageTotals(calls=1, input=4, output=0, estimated_cost=0))

    assert raised.value.reason_code == "invalid_reservation_state"
    assert budget.spent == actual
    assert budget.reserved == ZERO


def test_release_is_only_allowed_before_dispatch_and_is_idempotent() -> None:
    budget = UsageBudget(BudgetLimits(calls=2))
    released = budget.reserve(CallBounds(input=10))
    released.release_pre_dispatch()
    released.release_pre_dispatch()

    dispatched = budget.reserve(CallBounds(input=10)).dispatch()
    with pytest.raises(ReservationStateError) as raised:
        dispatched.release_pre_dispatch()

    assert raised.value.reason_code == "invalid_reservation_state"
    assert released.state == "released_pre_dispatch"
    assert dispatched.state == "dispatched"
    assert budget.reserved == UsageTotals(calls=1, input=10, output=0, estimated_cost=0)


def test_unknown_usage_charges_the_complete_bound_idempotently() -> None:
    budget = UsageBudget(BudgetLimits(calls=1, input=10, output=20, estimated_cost=30))
    reservation = budget.reserve(CallBounds(input=10, output=20, estimated_cost=30)).dispatch()
    charged = UsageTotals(calls=1, input=10, output=20, estimated_cost=30)

    assert reservation.settle(None) == charged
    assert reservation.settle(None) == charged
    assert reservation.charge_unknown() == charged
    assert reservation.state == "charged_unknown"
    assert budget.spent == charged
    assert budget.reserved == ZERO


def test_malformed_usage_is_charged_in_full_before_error() -> None:
    budget = UsageBudget(BudgetLimits(calls=1, input=10))
    reservation = budget.reserve(CallBounds(input=10)).dispatch()

    with pytest.raises(UsageValidationError) as raised:
        reservation.settle(UsageTotals(calls=0, input=0, output=0, estimated_cost=0))

    assert raised.value.reason_code == "invalid_usage_value"
    assert reservation.state == "charged_unknown"
    assert budget.spent == UsageTotals(calls=1, input=10, output=0, estimated_cost=0)
    assert budget.reserved == ZERO


def test_exception_after_dispatch_is_charged_in_full() -> None:
    budget = UsageBudget(BudgetLimits(calls=1, input=10, estimated_cost=100))
    reservation = budget.reserve(CallBounds(input=10, estimated_cost=100))

    with pytest.raises(RuntimeError, match="provider failed"), reservation:
        raise RuntimeError("provider failed")

    assert reservation.state == "charged_unknown"
    assert budget.spent == UsageTotals(calls=1, input=10, output=0, estimated_cost=100)


def test_successful_context_without_usage_is_charged_in_full() -> None:
    budget = UsageBudget(BudgetLimits(calls=1, output=10))
    reservation = budget.reserve(CallBounds(output=10))

    with reservation:
        pass

    assert reservation.state == "charged_unknown"
    assert budget.spent == UsageTotals(calls=1, input=0, output=10, estimated_cost=0)


def test_async_cancellation_after_dispatch_is_charged_in_full() -> None:
    async def cancel_after_dispatch() -> None:
        async with reservation:
            raise asyncio.CancelledError

    budget = UsageBudget(BudgetLimits(calls=1, output=10))
    reservation = budget.reserve(CallBounds(output=10))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancel_after_dispatch())

    assert reservation.state == "charged_unknown"
    assert budget.spent == UsageTotals(calls=1, input=0, output=10, estimated_cost=0)


def test_actual_usage_overrun_poison_budget_and_blocks_future_work() -> None:
    budget = UsageBudget(BudgetLimits(calls=3, input=100))
    overrun = budget.reserve(CallBounds(input=10)).dispatch()
    queued = budget.reserve(CallBounds(input=10))
    actual = UsageTotals(calls=1, input=11, output=0, estimated_cost=0)

    with pytest.raises(UsageExceedsReservationError) as raised:
        overrun.settle(actual)

    assert raised.value.reason_code == "usage_exceeds_reservation"
    assert budget.poisoned is True
    assert budget.poison_reason_code == "usage_exceeds_reservation"
    assert budget.spent == actual
    assert budget.reserved == UsageTotals(calls=1, input=10, output=0, estimated_cost=0)
    with pytest.raises(BudgetPoisonedError) as reserve_error:
        budget.reserve(CallBounds())
    with pytest.raises(BudgetPoisonedError):
        queued.dispatch()
    assert reserve_error.value.reason_code == "budget_poisoned"

    queued.release_pre_dispatch()
    assert budget.reserved == ZERO


def test_repeated_overrun_is_idempotent_but_still_reports_failure() -> None:
    budget = UsageBudget(BudgetLimits(calls=1, output=5))
    reservation = budget.reserve(CallBounds(output=5)).dispatch()
    actual = UsageTotals(calls=1, input=0, output=6, estimated_cost=0)

    for _ in range(2):
        with pytest.raises(UsageExceedsReservationError):
            reservation.settle(actual)

    assert budget.spent == actual
    assert budget.reserved == ZERO


def test_aggregate_overflow_fails_before_reserving_any_call() -> None:
    budget = UsageBudget(BudgetLimits())

    with pytest.raises(UsageCounterOverflowError) as raised:
        budget.reserve_many((CallBounds(input=SQLITE_INT_MAX), CallBounds(input=1)))

    assert raised.value.reason_code == "usage_counter_overflow"
    assert budget.spent == ZERO
    assert budget.reserved == ZERO


def test_terminal_counter_overflow_is_atomic_and_poisoned() -> None:
    budget = UsageBudget(BudgetLimits())
    large, small = budget.reserve_many((CallBounds(input=SQLITE_INT_MAX - 1), CallBounds(input=1)))
    large.dispatch()
    small.dispatch()
    with pytest.raises(UsageExceedsReservationError):
        small.settle(UsageTotals(calls=1, input=2, output=0, estimated_cost=0))

    before_spent = budget.spent
    before_reserved = budget.reserved
    with pytest.raises(UsageCounterOverflowError) as raised:
        large.charge_unknown()

    assert raised.value.reason_code == "usage_counter_overflow"
    assert budget.poison_reason_code == "usage_counter_overflow"
    assert budget.spent == before_spent
    assert budget.reserved == before_reserved
    assert large.state == "dispatched"


def test_concurrent_reservations_never_exceed_limit() -> None:
    workers = 40
    allowed = 7
    budget = UsageBudget(BudgetLimits(calls=allowed))
    barrier = Barrier(workers)
    result_lock = Lock()
    reservations = []
    failures = []

    def reserve_once() -> None:
        barrier.wait()
        try:
            reservation = budget.reserve(CallBounds())
        except BudgetExceededError as exc:
            with result_lock:
                failures.append(exc)
        else:
            with result_lock:
                reservations.append(reservation)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(reserve_once) for _ in range(workers)]
        for future in futures:
            future.result()

    assert len(reservations) == allowed
    assert len(failures) == workers - allowed
    assert {error.reason_code for error in failures} == {"budget_exceeded"}
    assert budget.spent == ZERO
    assert budget.reserved == UsageTotals(calls=allowed, input=0, output=0, estimated_cost=0)


def test_concurrent_terminal_calls_do_not_double_charge() -> None:
    budget = UsageBudget(BudgetLimits(calls=1, input=10))
    reservation = budget.reserve(CallBounds(input=10)).dispatch()

    with ThreadPoolExecutor(max_workers=8) as executor:
        charged = list(executor.map(lambda _: reservation.charge_unknown(), range(100)))

    assert set(charged) == {UsageTotals(calls=1, input=10, output=0, estimated_cost=0)}
    assert budget.spent == UsageTotals(calls=1, input=10, output=0, estimated_cost=0)
    assert budget.reserved == ZERO
