"""SQLite v8 durable Provider-budget ledger tests."""

from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from llmolympic.core.storage import (
    SCHEMA_VERSION,
    ProviderBudgetCollisionError,
    ProviderBudgetPendingError,
    ProviderCallAttemptCollisionError,
    SQLiteStore,
    SQLiteUsageReservation,
    StorageError,
    inspect_database,
)
from llmolympic.core.usage import (
    BudgetExceededError,
    BudgetLimits,
    BudgetPoisonedError,
    CallBounds,
    ProviderBudgetPolicy,
    ReservationStateError,
    RouteBudgetPolicy,
    TokenPrice,
    UsageCounterOverflowError,
    UsageExceedsReservationError,
    UsageTotals,
    UsageValidationError,
)

ROUTE_ID = "route:v1:" + "1" * 64


def _policy(
    *,
    max_output_tokens_per_call: int = 10_000,
    priced: bool = False,
) -> ProviderBudgetPolicy:
    return ProviderBudgetPolicy(
        max_output_tokens_per_call=max_output_tokens_per_call,
        routes=(
            RouteBudgetPolicy(
                route_id=ROUTE_ID,
                price=(
                    TokenPrice(
                        input_nanos_per_million=1_000_000,
                        output_nanos_per_million=1_000_000,
                    )
                    if priced
                    else None
                ),
            ),
        ),
    )


def _bound(
    *,
    input: int = 0,
    output: int = 0,
    estimated_cost: int = 0,
) -> CallBounds:
    return CallBounds(
        input=input,
        output=output,
        estimated_cost=estimated_cost,
        route_id=ROUTE_ID,
    )


def test_fresh_v8_usage_schema_is_strict_and_credential_free(tmp_path: Path) -> None:
    path = tmp_path / "usage-schema.db"
    SQLiteStore(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        table_flags = {
            row[1]: row[5]
            for row in connection.execute("PRAGMA table_list")
            if row[1] in {"provider_budgets", "provider_call_attempts"}
        }
        assert table_flags == {
            "provider_budgets": 1,
            "provider_call_attempts": 1,
        }
        columns = {
            row[1]
            for table in table_flags
            for row in connection.execute(f"PRAGMA table_info({table})")
        }

    assert not columns & {
        "api_key",
        "authorization",
        "base_url",
        "endpoint",
        "error",
        "headers",
        "model",
        "prompt",
        "request",
        "response",
        "text",
    }


def test_provider_budget_lifecycle_preserves_exact_integer_aggregates(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "usage-lifecycle.db")
    limits = BudgetLimits(calls=4, input=100, output=80, estimated_cost=10_000)
    policy = _policy(priced=True)
    created = store.create_provider_budget("budget-1", limits, policy)
    assert created.limits == limits
    assert created.spent == UsageTotals.zero()
    assert created.reserved == UsageTotals.zero()

    first, second, third = store.reserve_provider_call_batch(
        "budget-1",
        (
            _bound(input=10, output=20, estimated_cost=30),
            _bound(input=11, output=21, estimated_cost=32),
            _bound(input=12, output=22, estimated_cost=34),
        ),
        attempt_ids=("attempt-1", "attempt-2", "attempt-3"),
    )
    assert (first.state, second.state, third.state) == (
        "reserved",
        "reserved",
        "reserved",
    )
    assert store.load_provider_budget("budget-1").reserved == UsageTotals(
        calls=3,
        input=33,
        output=63,
        estimated_cost=96,
    )

    assert store.mark_provider_call_dispatched(first.attempt_id).state == "dispatched"
    settled = store.settle_provider_call(
        first.attempt_id,
        UsageTotals(calls=1, input=7, output=13, estimated_cost=20),
    )
    assert settled.state == "settled"
    assert settled.actual == UsageTotals(calls=1, input=7, output=13, estimated_cost=20)
    assert store.release_provider_call_pre_dispatch(second.attempt_id).state == (
        "released_pre_dispatch"
    )
    unknown = store.charge_provider_call_unknown(third.attempt_id)
    assert unknown.state == "charged_unknown"
    assert unknown.actual is None
    assert unknown.charged == third.bounds.as_totals()

    snapshot = store.load_provider_budget("budget-1")
    assert snapshot is not None
    assert snapshot.reserved == UsageTotals.zero()
    assert snapshot.spent == UsageTotals(
        calls=2,
        input=19,
        output=35,
        estimated_cost=54,
    )
    assert store.finalize_provider_budget("budget-1").finalized
    assert store.finalize_provider_budget("budget-1").spent == snapshot.spent
    with pytest.raises(ReservationStateError, match="finalized"):
        store.reserve_provider_call_batch(
            "budget-1",
            (_bound(),),
            attempt_ids=("too-late",),
        )


def test_sqlite_usage_adapter_satisfies_reservation_protocol(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "usage-adapter.db")
    policy = _policy(priced=True)
    store.create_provider_budget("budget", BudgetLimits(calls=1), policy)
    budget = store.bind_provider_usage_budget("budget")
    bound = _bound(input=2, output=3, estimated_cost=5)

    reservation = budget.reserve(bound)

    assert reservation.bounds == bound
    assert reservation.state == "reserved"
    assert reservation.dispatch() is reservation
    assert reservation.state == "dispatched"
    assert reservation.settle(
        UsageTotals(calls=1, input=2, output=2, estimated_cost=4)
    ) == UsageTotals(calls=1, input=2, output=2, estimated_cost=4)
    assert reservation.state == "settled"
    assert budget.spent == UsageTotals(calls=1, input=2, output=2, estimated_cost=4)


def test_sqlite_adapter_rejects_cross_budget_and_forged_attempt_handles(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "usage-adapter-capability.db")
    policy = _policy()
    store.create_provider_budget("budget-a", BudgetLimits(calls=1), policy)
    store.create_provider_budget("budget-b", BudgetLimits(calls=1), policy)
    budget_a = store.bind_provider_usage_budget("budget-a")
    budget_b = store.bind_provider_usage_budget("budget-b")
    reservation = budget_a.reserve(_bound())
    attempt = store.get_provider_call_attempt(reservation.reservation_id)
    assert attempt is not None

    with pytest.raises(ReservationStateError, match="different durable budget"):
        SQLiteUsageReservation(budget_b, attempt)
    forged = replace(
        attempt,
        bounds=CallBounds(route_id=ROUTE_ID, output=1),
    )
    with pytest.raises(ReservationStateError, match="durable route and bounds"):
        SQLiteUsageReservation(budget_a, forged)
    with pytest.raises(AttributeError):
        budget_a.budget_id = "budget-b"  # type: ignore[misc]
    assert reservation.state == "reserved"


def test_sqlite_reservation_context_preserves_original_base_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteStore(tmp_path / "usage-context-error.db")
    store.create_provider_budget("budget", BudgetLimits(calls=3), _policy())
    budget = store.bind_provider_usage_budget("budget")
    first, second, third = budget.reserve_many((_bound(), _bound(), _bound()))

    def fail_charge(*_args, **_kwargs):
        raise StorageError("injected accounting failure")

    monkeypatch.setattr(store, "charge_provider_call_unknown", fail_charge)
    with pytest.raises(ValueError, match="original"), first:
        raise ValueError("original")
    assert first.state == "dispatched"

    with pytest.raises(StorageError, match="injected accounting failure"), second:
        pass
    assert second.state == "dispatched"

    async def cancel_inside_context() -> None:
        async with third:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancel_inside_context())
    assert third.state == "dispatched"


def test_budget_policy_is_frozen_and_idempotency_rejects_changed_cap(tmp_path: Path) -> None:
    path = tmp_path / "frozen-policy.db"
    store = SQLiteStore(path)
    policy = _policy(max_output_tokens_per_call=40, priced=True)
    first = store.create_provider_budget("budget", BudgetLimits(calls=2), policy)

    assert first.policy == policy
    assert store.create_provider_budget("budget", BudgetLimits(calls=2), policy) == first
    with pytest.raises(ProviderBudgetCollisionError):
        store.create_provider_budget(
            "budget",
            BudgetLimits(calls=2),
            _policy(max_output_tokens_per_call=39, priced=True),
        )
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT policy_json, policy_digest FROM provider_budgets"
        ).fetchone()
    assert row == (policy.canonical_json(), policy.digest)


def test_reservation_requires_frozen_route_cap_and_exact_price(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "route-policy.db")
    policy = _policy(max_output_tokens_per_call=8, priced=True)
    store.create_provider_budget("budget", BudgetLimits(calls=2), policy)

    with pytest.raises(BudgetExceededError) as cap_error:
        store.reserve_provider_call_batch(
            "budget",
            (_bound(output=9, estimated_cost=9),),
            attempt_ids=("over-cap",),
        )
    assert cap_error.value.dimension == "output_per_call"
    with pytest.raises(UsageValidationError, match="frozen route price"):
        store.reserve_provider_call_batch(
            "budget",
            (_bound(input=2, output=3, estimated_cost=4),),
            attempt_ids=("underpriced",),
        )
    with pytest.raises(UsageValidationError, match="route_id"):
        store.reserve_provider_call_batch(
            "budget",
            (CallBounds(route_id="route:v1:" + "f" * 64),),
            attempt_ids=("wrong-route",),
        )
    assert store.load_provider_budget("budget").reserved == UsageTotals.zero()
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM provider_call_attempts").fetchone()[0] == 0


def test_invalid_settlement_cost_is_charged_unknown_and_cannot_undercharge(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "settlement-price.db")
    store.create_provider_budget("budget", BudgetLimits(calls=1), _policy(priced=True))
    (attempt,) = store.reserve_provider_call_batch(
        "budget",
        (_bound(input=3, output=4, estimated_cost=7),),
        attempt_ids=("attempt",),
    )
    store.mark_provider_call_dispatched(attempt.attempt_id)

    with pytest.raises(UsageValidationError, match="frozen route price"):
        store.settle_provider_call(
            attempt.attempt_id,
            UsageTotals(calls=1, input=2, output=3, estimated_cost=4),
        )
    durable = store.get_provider_call_attempt(attempt.attempt_id)
    assert durable is not None and durable.state == "charged_unknown"
    assert durable.charged == attempt.bounds.as_totals()


def test_batch_reservation_is_all_or_none_for_every_limit(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "batch-limit.db")
    store.create_provider_budget(
        "budget",
        BudgetLimits(calls=2, input=10, output=10),
        _policy(),
    )

    with pytest.raises(BudgetExceededError) as caught:
        store.reserve_provider_call_batch(
            "budget",
            (
                _bound(input=4, output=4),
                _bound(input=7, output=4),
            ),
            attempt_ids=("first", "second"),
        )
    assert caught.value.dimension == "input"
    assert store.load_provider_budget("budget").reserved == UsageTotals.zero()
    assert store.get_provider_call_attempt("first") is None
    assert store.get_provider_call_attempt("second") is None


def test_duplicate_attempt_in_batch_rolls_back_all_reservations(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "attempt-collision.db")
    store.create_provider_budget("budget", BudgetLimits(calls=3), _policy())
    store.reserve_provider_call_batch(
        "budget",
        (_bound(),),
        attempt_ids=("existing",),
    )

    with pytest.raises(ProviderCallAttemptCollisionError):
        store.reserve_provider_call_batch(
            "budget",
            (_bound(), _bound()),
            attempt_ids=("new", "existing"),
        )
    assert store.get_provider_call_attempt("new") is None
    assert store.load_provider_budget("budget").reserved.calls == 1


def test_concurrent_batches_cannot_oversubscribe_one_budget(tmp_path: Path) -> None:
    path = tmp_path / "concurrent-budget.db"
    SQLiteStore(path).create_provider_budget("budget", BudgetLimits(calls=2), _policy())

    def reserve(batch: int) -> str:
        try:
            SQLiteStore(path, create=False).reserve_provider_call_batch(
                "budget",
                (_bound(), _bound()),
                attempt_ids=(f"batch-{batch}-1", f"batch-{batch}-2"),
            )
        except BudgetExceededError:
            return "rejected"
        return "reserved"

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(reserve, range(8)))

    assert outcomes.count("reserved") == 1
    assert outcomes.count("rejected") == 7
    snapshot = SQLiteStore(path, create=False).load_provider_budget("budget")
    assert snapshot is not None
    assert snapshot.reserved.calls == 2
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM provider_call_attempts").fetchone()[0] == 2


def test_usage_overrun_is_committed_as_violation_and_poisons_budget(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "usage-overrun.db")
    store.create_provider_budget(
        "budget",
        BudgetLimits(calls=2, input=5, output=5),
        _policy(),
    )
    (attempt,) = store.reserve_provider_call_batch(
        "budget",
        (_bound(input=5, output=5),),
        attempt_ids=("attempt",),
    )
    store.mark_provider_call_dispatched(attempt.attempt_id)

    actual = UsageTotals(calls=1, input=6, output=5, estimated_cost=0)
    with pytest.raises(UsageExceedsReservationError):
        store.settle_provider_call(attempt.attempt_id, actual)

    durable = store.get_provider_call_attempt(attempt.attempt_id)
    assert durable is not None
    assert durable.state == "violation"
    assert durable.actual == actual
    assert durable.charged == actual
    snapshot = store.load_provider_budget("budget")
    assert snapshot is not None
    assert snapshot.poison_reason_code == "usage_exceeds_reservation"
    assert snapshot.reserved == UsageTotals.zero()
    assert snapshot.spent == actual
    with pytest.raises(BudgetPoisonedError):
        store.reserve_provider_call_batch(
            "budget",
            (_bound(),),
            attempt_ids=("blocked",),
        )


def test_counter_overflow_rejects_batch_without_partial_write(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "usage-overflow.db")
    store.create_provider_budget("budget", BudgetLimits(), _policy())
    store.reserve_provider_call_batch(
        "budget",
        (_bound(input=2**63 - 1),),
        attempt_ids=("max",),
    )

    with pytest.raises(UsageCounterOverflowError):
        store.reserve_provider_call_batch(
            "budget",
            (_bound(input=1),),
            attempt_ids=("overflow",),
        )
    assert store.get_provider_call_attempt("overflow") is None
    assert store.load_provider_budget("budget").reserved == UsageTotals(
        calls=1,
        input=2**63 - 1,
        output=0,
        estimated_cost=0,
    )


def test_finalize_rejects_pending_and_accepts_unknown_terminal_charge(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "pending-finalize.db")
    store.create_provider_budget("budget", BudgetLimits(calls=1), _policy())
    (attempt,) = store.reserve_provider_call_batch(
        "budget",
        (_bound(output=9),),
        attempt_ids=("attempt",),
    )
    store.mark_provider_call_dispatched(attempt.attempt_id)

    with pytest.raises(ProviderBudgetPendingError, match="unresolved"):
        store.finalize_provider_budget("budget")
    assert not store.load_provider_budget("budget").finalized
    store.charge_provider_call_unknown(attempt.attempt_id)
    assert store.finalize_provider_budget("budget").finalized


def test_aggregate_tampering_is_detected_before_next_write(tmp_path: Path) -> None:
    path = tmp_path / "aggregate-tamper.db"
    store = SQLiteStore(path)
    store.create_provider_budget("budget", BudgetLimits(calls=2), _policy())
    store.reserve_provider_call_batch(
        "budget",
        (_bound(),),
        attempt_ids=("attempt",),
    )
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE provider_budgets SET reserved_calls = 2")

    with pytest.raises(StorageError, match="aggregate invariant"):
        store.load_provider_budget("budget")
    with pytest.raises(StorageError, match="aggregate invariant"):
        store.reserve_provider_call_batch(
            "budget",
            (_bound(),),
            attempt_ids=("second",),
        )
    assert store.get_provider_call_attempt("second") is None


def test_policy_digest_and_attempt_route_tampering_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "policy-tamper.db"
    store = SQLiteStore(path)
    policy = _policy()
    store.create_provider_budget("budget", BudgetLimits(calls=1), policy)
    store.reserve_provider_call_batch(
        "budget",
        (_bound(),),
        attempt_ids=("attempt",),
    )
    changed_policy = _policy(max_output_tokens_per_call=9_999)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE provider_budgets SET policy_json = ? WHERE budget_id = 'budget'",
            (changed_policy.canonical_json(),),
        )
    with pytest.raises(StorageError, match="digest"):
        store.load_provider_budget("budget")

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE provider_budgets SET policy_json = ? WHERE budget_id = 'budget'",
            (policy.canonical_json(),),
        )
        connection.execute(
            "UPDATE provider_call_attempts SET route_id = ? WHERE attempt_id = 'attempt'",
            ("route:v1:" + "e" * 64,),
        )
    with pytest.raises(StorageError, match="outside its frozen policy"):
        store.load_provider_budget("budget")


def test_v7_to_v8_migration_is_additive_and_inspect_is_read_only(tmp_path: Path) -> None:
    path = tmp_path / "v7-to-v8.db"
    SQLiteStore(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            DROP TABLE provider_call_attempts;
            DROP TABLE provider_budgets;
            DROP TABLE championship_pairings;
            DROP TABLE championship_entrants;
            DROP TABLE championship_archives;
            PRAGMA user_version = 7;
            """
        )

    inspection = inspect_database(path)
    assert inspection.schema_version == 7
    assert inspection.migration_required
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_schema WHERE name = 'provider_budgets'"
            ).fetchone()[0]
            == 0
        )

    SQLiteStore(path, create=False)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert {
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_schema
                WHERE type = 'table' AND name LIKE 'provider_%'
                """
            )
        } == {"provider_budgets", "provider_call_attempts"}


def test_failed_v7_to_v8_migration_rolls_back_schema_and_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "v8-migration-rollback.db"
    SQLiteStore(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            DROP TABLE provider_call_attempts;
            DROP TABLE provider_budgets;
            DROP TABLE championship_pairings;
            DROP TABLE championship_entrants;
            DROP TABLE championship_archives;
            PRAGMA user_version = 7;
            """
        )
    original = SQLiteStore._create_provider_usage_schema

    def fail_after_create(connection: sqlite3.Connection) -> None:
        original(connection)
        raise RuntimeError("injected v8 migration failure")

    monkeypatch.setattr(
        SQLiteStore,
        "_create_provider_usage_schema",
        staticmethod(fail_after_create),
    )
    with pytest.raises(RuntimeError, match="injected v8 migration failure"):
        SQLiteStore(path, create=False)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_schema WHERE name LIKE 'provider_%'"
            ).fetchone()[0]
            == 0
        )
