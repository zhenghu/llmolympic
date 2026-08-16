"""_ProviderUsageMixin mixin for SQLiteStore."""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from typing import Literal

from llmolympic.core._storage_types import (
    ProviderBudgetCollisionError,
    ProviderBudgetPendingError,
    ProviderBudgetSnapshot,
    ProviderCallAttempt,
    ProviderCallAttemptCollisionError,
    SQLiteUsageBudget,
    StorageError,
    TournamentRunnerLease,
    TournamentRunnerLeaseLostError,
    _checked_usage_add,
    _checked_usage_subtract,
    _sum_call_bounds,
    _usage_from_bounds,
    _validate_durable_budget_definition,
    _validate_usage_ledger_id,
)
from llmolympic.core.usage import (
    BudgetExceededError,
    BudgetLimits,
    BudgetPoisonedError,
    CallBounds,
    ProviderBudgetPolicy,
    ReservationStateError,
    UsageCounterOverflowError,
    UsageExceedsReservationError,
    UsageTotals,
    UsageValidationError,
)
from llmolympic.providers.base import validate_route_id


class _ProviderUsageMixin:
    @staticmethod
    def _usage_totals_from_columns(
        row: sqlite3.Row,
        *,
        calls: str,
        input_tokens: str,
        output_tokens: str,
        estimated_cost_nanos: str,
        optional: bool = False,
    ) -> UsageTotals | None:
        values = (
            row[calls],
            row[input_tokens],
            row[output_tokens],
            row[estimated_cost_nanos],
        )
        if optional and all(value is None for value in values):
            return None
        if any(value is None for value in values):
            raise StorageError("Provider usage ledger has partial aggregate values")
        try:
            return UsageTotals(
                calls=values[0],
                input=values[1],
                output=values[2],
                estimated_cost=values[3],
            )
        except UsageValidationError as exc:
            raise StorageError("Provider usage ledger contains invalid integer values") from exc

    @staticmethod
    def _provider_attempt_from_row(row: sqlite3.Row) -> ProviderCallAttempt:
        try:
            attempt_id = _validate_usage_ledger_id(row["attempt_id"], "attempt_id")
            budget_id = _validate_usage_ledger_id(row["budget_id"], "budget_id")
            route_id = validate_route_id(row["route_id"])
            bounds = CallBounds(
                input=row["bound_input_tokens"],
                output=row["bound_output_tokens"],
                estimated_cost=row["bound_estimated_cost_nanos"],
                route_id=route_id,
            )
        except (UsageValidationError, ValueError) as exc:
            raise StorageError("Provider call-attempt ledger row is invalid") from exc
        if row["bound_calls"] != 1:
            raise StorageError("Provider call-attempt ledger row has an invalid call bound")
        state = row["state"]
        if state not in {
            "reserved",
            "dispatched",
            "settled",
            "released_pre_dispatch",
            "charged_unknown",
            "violation",
        }:
            raise StorageError("Provider call-attempt ledger row has an invalid state")
        actual = _ProviderUsageMixin._usage_totals_from_columns(
            row,
            calls="actual_calls",
            input_tokens="actual_input_tokens",
            output_tokens="actual_output_tokens",
            estimated_cost_nanos="actual_estimated_cost_nanos",
            optional=True,
        )
        charged = _ProviderUsageMixin._usage_totals_from_columns(
            row,
            calls="charged_calls",
            input_tokens="charged_input_tokens",
            output_tokens="charged_output_tokens",
            estimated_cost_nanos="charged_estimated_cost_nanos",
            optional=True,
        )
        generation = row["runner_generation"]
        if generation is not None and (
            isinstance(generation, bool) or not isinstance(generation, int) or generation < 1
        ):
            raise StorageError("Provider call-attempt ledger row has invalid fencing")
        timestamps = (
            row["created_at_epoch"],
            row["dispatched_at_epoch"],
            row["finished_at_epoch"],
        )
        if timestamps[0] is None or any(
            value is not None
            and (isinstance(value, bool) or not isinstance(value, int) or value < 0)
            for value in timestamps
        ):
            raise StorageError("Provider call-attempt ledger row has invalid timestamps")
        created_at, dispatched_at, finished_at = timestamps
        bound_totals = _usage_from_bounds(bounds)
        lifecycle_valid = False
        if state == "reserved":
            lifecycle_valid = (
                dispatched_at is None
                and finished_at is None
                and actual is None
                and charged is None
            )
        elif state == "dispatched":
            lifecycle_valid = (
                dispatched_at is not None
                and dispatched_at >= created_at
                and finished_at is None
                and actual is None
                and charged is None
            )
        elif state == "released_pre_dispatch":
            lifecycle_valid = (
                dispatched_at is None
                and finished_at is not None
                and finished_at >= created_at
                and actual is None
                and charged is None
            )
        elif state == "settled" and actual is not None:
            lifecycle_valid = (
                dispatched_at is not None
                and finished_at is not None
                and created_at <= dispatched_at <= finished_at
                and actual.calls == 1
                and all(
                    getattr(actual, dimension) <= getattr(bound_totals, dimension)
                    for dimension in ("calls", "input", "output", "estimated_cost")
                )
                and charged == actual
            )
        elif state == "charged_unknown":
            lifecycle_valid = (
                finished_at is not None
                and finished_at >= created_at
                and (dispatched_at is None or created_at <= dispatched_at <= finished_at)
                and actual is None
                and charged == bound_totals
            )
        elif state == "violation" and actual is not None and charged is not None:
            lifecycle_valid = (
                dispatched_at is not None
                and finished_at is not None
                and created_at <= dispatched_at <= finished_at
                and any(
                    getattr(actual, dimension) > getattr(bound_totals, dimension)
                    for dimension in ("calls", "input", "output", "estimated_cost")
                )
                and charged in (actual, bound_totals)
            )
        if not lifecycle_valid:
            raise StorageError("Provider call-attempt lifecycle invariant failed")
        return ProviderCallAttempt(
            attempt_id=attempt_id,
            budget_id=budget_id,
            route_id=route_id,
            bounds=bounds,
            state=state,
            actual=actual,
            charged=charged,
            runner_generation=generation,
            created_at_epoch=created_at,
            dispatched_at_epoch=dispatched_at,
            finished_at_epoch=finished_at,
        )

    @staticmethod
    def _provider_budget_snapshot_in_transaction(
        connection: sqlite3.Connection,
        budget_id: str,
    ) -> ProviderBudgetSnapshot | None:
        row = connection.execute(
            "SELECT * FROM provider_budgets WHERE budget_id = ?",
            (budget_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            stored_budget_id = _validate_usage_ledger_id(row["budget_id"], "budget_id")
            limits = BudgetLimits(
                calls=row["limit_calls"],
                input=row["limit_input_tokens"],
                output=row["limit_output_tokens"],
                estimated_cost=row["limit_estimated_cost_nanos"],
            )
            policy = ProviderBudgetPolicy.from_canonical_json(row["policy_json"])
        except UsageValidationError as exc:
            raise StorageError("Provider budget ledger row is invalid") from exc
        if row["policy_digest"] != policy.digest:
            raise StorageError("Provider budget policy digest does not match its payload")
        if not policy.routes:
            raise StorageError("Provider budget policy has no frozen routes")
        if limits.estimated_cost is not None and any(
            route.price is None for route in policy.routes
        ):
            raise StorageError("Cost-limited Provider budget contains an unpriced route")
        stored_spent = _ProviderUsageMixin._usage_totals_from_columns(
            row,
            calls="spent_calls",
            input_tokens="spent_input_tokens",
            output_tokens="spent_output_tokens",
            estimated_cost_nanos="spent_estimated_cost_nanos",
        )
        stored_reserved = _ProviderUsageMixin._usage_totals_from_columns(
            row,
            calls="reserved_calls",
            input_tokens="reserved_input_tokens",
            output_tokens="reserved_output_tokens",
            estimated_cost_nanos="reserved_estimated_cost_nanos",
        )
        if stored_spent is None or stored_reserved is None:
            raise StorageError("Provider budget ledger has missing aggregates")

        tournament_id = row["tournament_id"]
        if tournament_id is not None and (
            not isinstance(tournament_id, str) or not tournament_id.strip()
        ):
            raise StorageError("Provider budget ledger has invalid tournament scope")
        poison_reason = row["poison_reason_code"]
        if poison_reason not in {
            None,
            UsageExceedsReservationError.reason_code,
            UsageCounterOverflowError.reason_code,
        }:
            raise StorageError("Provider budget ledger has an invalid poison reason")
        created_at = row["created_at_epoch"]
        finalized_at = row["finalized_at_epoch"]
        if (
            isinstance(created_at, bool)
            or not isinstance(created_at, int)
            or created_at < 0
            or (
                finalized_at is not None
                and (
                    isinstance(finalized_at, bool)
                    or not isinstance(finalized_at, int)
                    or finalized_at < created_at
                )
            )
        ):
            raise StorageError("Provider budget ledger has invalid timestamps")

        calculated_spent = UsageTotals.zero()
        calculated_reserved = UsageTotals.zero()
        unresolved = 0
        saw_violation = False
        attempt_rows = connection.execute(
            "SELECT * FROM provider_call_attempts WHERE budget_id = ? ORDER BY attempt_id",
            (stored_budget_id,),
        ).fetchall()
        try:
            for attempt_row in attempt_rows:
                attempt = _ProviderUsageMixin._provider_attempt_from_row(attempt_row)
                try:
                    price = policy.price_for(attempt.route_id)
                except UsageValidationError as exc:
                    raise StorageError(
                        "Provider call attempt uses a route outside its frozen policy"
                    ) from exc
                if attempt.bounds.output > policy.max_output_tokens_per_call:
                    raise StorageError(
                        "Provider call attempt exceeds its frozen per-call output cap"
                    )
                try:
                    expected_cost = (
                        0
                        if price is None
                        else price.estimate(
                            input_tokens=attempt.bounds.input,
                            output_tokens=attempt.bounds.output,
                        )
                    )
                except (UsageCounterOverflowError, UsageValidationError) as exc:
                    raise StorageError(
                        "Provider call attempt cannot be priced by its frozen policy"
                    ) from exc
                if attempt.bounds.estimated_cost != expected_cost:
                    raise StorageError(
                        "Provider call attempt cost differs from its frozen price policy"
                    )
                if (tournament_id is None) != (attempt.runner_generation is None):
                    raise StorageError(
                        "Provider call-attempt fencing does not match its budget scope"
                    )
                if attempt.state in {"reserved", "dispatched"}:
                    calculated_reserved = _checked_usage_add(
                        calculated_reserved,
                        _usage_from_bounds(attempt.bounds),
                    )
                    unresolved += 1
                elif attempt.state in {"settled", "charged_unknown", "violation"}:
                    if attempt.charged is None:
                        raise StorageError("Provider terminal attempt has no durable charge")
                    calculated_spent = _checked_usage_add(calculated_spent, attempt.charged)
                if attempt.state == "violation":
                    saw_violation = True
        except (ReservationStateError, UsageCounterOverflowError) as exc:
            raise StorageError("Provider usage ledger aggregates overflow") from exc

        if calculated_spent != stored_spent or calculated_reserved != stored_reserved:
            raise StorageError("Provider usage ledger aggregate invariant failed")
        if saw_violation != (poison_reason is not None):
            raise StorageError("Provider usage ledger poison invariant failed")
        if finalized_at is not None and unresolved:
            raise StorageError("Finalized Provider budget contains unresolved attempts")
        if poison_reason is None:
            try:
                committed = _checked_usage_add(stored_spent, stored_reserved)
            except UsageCounterOverflowError as exc:
                raise StorageError("Provider usage ledger aggregates overflow") from exc
            for dimension in ("calls", "input", "output", "estimated_cost"):
                limit = getattr(limits, dimension)
                if limit is not None and getattr(committed, dimension) > limit:
                    raise StorageError("Provider usage ledger exceeds an unpoisoned limit")

        return ProviderBudgetSnapshot(
            budget_id=stored_budget_id,
            limits=limits,
            policy=policy,
            spent=stored_spent,
            reserved=stored_reserved,
            tournament_id=tournament_id,
            created_at_epoch=created_at,
            finalized_at_epoch=finalized_at,
            poison_reason_code=poison_reason,
        )

    @staticmethod
    def _update_provider_budget_counters(
        connection: sqlite3.Connection,
        snapshot: ProviderBudgetSnapshot,
        *,
        spent: UsageTotals,
        reserved: UsageTotals,
        poison_reason_code: str | None = None,
    ) -> None:
        _checked_usage_add(spent, reserved)
        updated = connection.execute(
            """
            UPDATE provider_budgets
            SET spent_calls = ?, spent_input_tokens = ?, spent_output_tokens = ?,
                spent_estimated_cost_nanos = ?, reserved_calls = ?,
                reserved_input_tokens = ?, reserved_output_tokens = ?,
                reserved_estimated_cost_nanos = ?, poison_reason_code = ?
            WHERE budget_id = ?
            """,
            (
                spent.calls,
                spent.input,
                spent.output,
                spent.estimated_cost,
                reserved.calls,
                reserved.input,
                reserved.output,
                reserved.estimated_cost,
                poison_reason_code,
                snapshot.budget_id,
            ),
        )
        if updated.rowcount != 1:
            raise StorageError("Provider budget disappeared during an atomic update")

    @staticmethod
    def _load_provider_attempt_in_transaction(
        connection: sqlite3.Connection,
        attempt_id: str,
    ) -> ProviderCallAttempt | None:
        row = connection.execute(
            "SELECT * FROM provider_call_attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        return None if row is None else _ProviderUsageMixin._provider_attempt_from_row(row)

    def _require_provider_attempt_fence(
        self,
        connection: sqlite3.Connection,
        snapshot: ProviderBudgetSnapshot,
        attempt: ProviderCallAttempt,
        lease: TournamentRunnerLease | None,
    ) -> None:
        if snapshot.tournament_id is None:
            if lease is not None:
                raise ValueError("non-tournament Provider budget does not accept a runner lease")
            if attempt.runner_generation is not None:
                raise StorageError("Provider call-attempt fencing is invalid")
            return
        active = self._require_active_tournament_runner(
            connection,
            snapshot.tournament_id,
            lease,
        )
        if attempt.runner_generation != active.generation:
            raise TournamentRunnerLeaseLostError(
                "Provider call attempt belongs to a stale runner generation"
            )

    def _insert_provider_budget_in_transaction(
        self,
        connection: sqlite3.Connection,
        budget_id: str,
        limits: BudgetLimits,
        policy: ProviderBudgetPolicy,
        *,
        tournament_id: str | None,
    ) -> ProviderBudgetSnapshot:
        now = self._database_epoch(connection)
        connection.execute(
            """
            INSERT INTO provider_budgets (
                budget_id, tournament_id, policy_json, policy_digest,
                limit_calls, limit_input_tokens,
                limit_output_tokens, limit_estimated_cost_nanos,
                spent_calls, spent_input_tokens, spent_output_tokens,
                spent_estimated_cost_nanos, reserved_calls,
                reserved_input_tokens, reserved_output_tokens,
                reserved_estimated_cost_nanos, poison_reason_code,
                created_at_epoch, finalized_at_epoch
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, 0, 0, 0, NULL, ?, NULL)
            """,
            (
                budget_id,
                tournament_id,
                policy.canonical_json(),
                policy.digest,
                limits.calls,
                limits.input,
                limits.output,
                limits.estimated_cost,
                now,
            ),
        )
        snapshot = self._provider_budget_snapshot_in_transaction(connection, budget_id)
        if snapshot is None:
            raise StorageError("Provider budget was not durably created")
        return snapshot

    def create_provider_budget(
        self,
        budget_id: str,
        limits: BudgetLimits,
        policy: ProviderBudgetPolicy,
        *,
        tournament_id: str | None = None,
        lease: TournamentRunnerLease | None = None,
    ) -> ProviderBudgetSnapshot:
        """Create one durable hard-budget scope, idempotently for identical inputs."""

        budget_id = _validate_usage_ledger_id(budget_id, "budget_id")
        limits, policy = _validate_durable_budget_definition(limits, policy)
        if tournament_id is not None and (
            not isinstance(tournament_id, str) or not tournament_id.strip()
        ):
            raise ValueError("tournament_id must be a non-empty string")

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._provider_budget_snapshot_in_transaction(connection, budget_id)
            if existing is not None:
                if (
                    existing.limits != limits
                    or existing.policy != policy
                    or existing.tournament_id != tournament_id
                ):
                    raise ProviderBudgetCollisionError(
                        "budget_id is already attached to a different budget"
                    )
                connection.commit()
                return existing
            if tournament_id is not None:
                checkpoint = connection.execute(
                    "SELECT status FROM tournament_checkpoints WHERE tournament_id = ?",
                    (tournament_id,),
                ).fetchone()
                if checkpoint is None or checkpoint["status"] != "in_progress":
                    raise StorageError(
                        "tournament Provider budget requires an in-progress checkpoint"
                    )
                self._require_active_tournament_runner(
                    connection,
                    tournament_id,
                    lease,
                )
                collision = connection.execute(
                    "SELECT 1 FROM provider_budgets WHERE tournament_id = ?",
                    (tournament_id,),
                ).fetchone()
                if collision is not None:
                    raise ProviderBudgetCollisionError(
                        "tournament checkpoint already has a Provider budget"
                    )
            snapshot = self._insert_provider_budget_in_transaction(
                connection,
                budget_id,
                limits,
                policy,
                tournament_id=tournament_id,
            )
            connection.commit()
            return snapshot
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def load_provider_budget(self, budget_id: str) -> ProviderBudgetSnapshot | None:
        """Load and recompute one budget, rejecting any aggregate drift."""

        budget_id = _validate_usage_ledger_id(budget_id, "budget_id")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            try:
                snapshot = self._provider_budget_snapshot_in_transaction(connection, budget_id)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return snapshot

    def get_provider_budget_snapshot(self, budget_id: str) -> ProviderBudgetSnapshot | None:
        """Alias for :meth:`load_provider_budget`."""

        return self.load_provider_budget(budget_id)

    def load_tournament_provider_budget(
        self,
        tournament_id: str,
    ) -> ProviderBudgetSnapshot | None:
        """Load the unique frozen budget policy owned by a tournament checkpoint."""

        if not isinstance(tournament_id, str) or not tournament_id.strip():
            raise ValueError("tournament_id must be a non-empty string")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            try:
                row = connection.execute(
                    "SELECT budget_id FROM provider_budgets WHERE tournament_id = ?",
                    (tournament_id,),
                ).fetchone()
                snapshot = (
                    None
                    if row is None
                    else self._provider_budget_snapshot_in_transaction(
                        connection,
                        row["budget_id"],
                    )
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return snapshot

    def bind_provider_usage_budget(
        self,
        budget_id: str,
        *,
        lease: TournamentRunnerLease | None = None,
    ) -> SQLiteUsageBudget:
        """Return the protocol adapter used by budget-aware Player calls."""

        return SQLiteUsageBudget(self, budget_id, lease=lease)

    def bind_tournament_usage_budget(
        self,
        tournament_id: str,
        *,
        lease: TournamentRunnerLease,
    ) -> SQLiteUsageBudget | None:
        """Bind the frozen durable budget for one claimed tournament, if configured."""

        snapshot = self.load_tournament_provider_budget(tournament_id)
        if snapshot is None:
            return None
        if lease.tournament_id != tournament_id:
            raise ValueError("runner lease does not belong to this tournament")
        return SQLiteUsageBudget(self, snapshot.budget_id, lease=lease)

    def reserve_provider_call_batch(
        self,
        budget_id: str,
        bounds: Iterable[CallBounds],
        *,
        route_ids: Iterable[str] | None = None,
        attempt_ids: Iterable[str] | None = None,
        lease: TournamentRunnerLease | None = None,
    ) -> tuple[ProviderCallAttempt, ...]:
        """Reserve an entire call batch under ``BEGIN IMMEDIATE``, or reserve none."""

        budget_id = _validate_usage_ledger_id(budget_id, "budget_id")
        try:
            requested = tuple(bounds)
        except TypeError as exc:
            raise UsageValidationError("bounds must be an iterable of CallBounds") from exc
        for bound in requested:
            if not isinstance(bound, CallBounds):
                raise UsageValidationError("bounds must contain only CallBounds")
            if bound.route_id is None:
                raise UsageValidationError(
                    "durable call bounds must contain a frozen route_id"
                )
        batch = _sum_call_bounds(requested)
        if route_ids is None:
            requested_route_ids = None
        else:
            try:
                requested_route_ids = tuple(validate_route_id(value) for value in route_ids)
            except (TypeError, ValueError) as exc:
                raise UsageValidationError(
                    "route_ids must be an iterable of valid route_id values"
                ) from exc
            if len(requested_route_ids) != len(requested):
                raise UsageValidationError("route_ids must have the same length as bounds")
        if attempt_ids is None:
            ids = tuple(secrets.token_hex(16) for _ in requested)
        else:
            try:
                ids = tuple(
                    _validate_usage_ledger_id(value, "attempt_id") for value in attempt_ids
                )
            except TypeError as exc:
                raise UsageValidationError("attempt_ids must be an iterable of strings") from exc
            if len(ids) != len(requested):
                raise UsageValidationError("attempt_ids must have the same length as bounds")
        if len(set(ids)) != len(ids):
            raise ProviderCallAttemptCollisionError("attempt ids must be unique within a batch")
        if not requested:
            return ()

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            snapshot = self._provider_budget_snapshot_in_transaction(connection, budget_id)
            if snapshot is None:
                raise StorageError("Provider budget does not exist")
            if snapshot.finalized:
                raise ReservationStateError("cannot reserve against a finalized budget")
            if snapshot.poisoned:
                raise BudgetPoisonedError(
                    f"usage budget is poisoned by {snapshot.poison_reason_code}"
                )
            bound_route_ids = tuple(bound.route_id for bound in requested)
            if requested_route_ids is not None and requested_route_ids != bound_route_ids:
                raise UsageValidationError(
                    "route_ids must exactly match the route_id frozen in each CallBounds"
                )
            resolved_route_ids = bound_route_ids
            for route_id, bound in zip(resolved_route_ids, requested, strict=True):
                price = snapshot.policy.price_for(route_id)
                if bound.output > snapshot.policy.max_output_tokens_per_call:
                    raise BudgetExceededError("output_per_call")
                expected_cost = (
                    0
                    if price is None
                    else price.estimate(
                        input_tokens=bound.input,
                        output_tokens=bound.output,
                    )
                )
                if bound.estimated_cost != expected_cost:
                    raise UsageValidationError(
                        "call bound estimated_cost must match the frozen route price"
                    )
            if snapshot.tournament_id is None:
                if lease is not None:
                    raise ValueError(
                        "non-tournament Provider budget does not accept a runner lease"
                    )
                generation = None
            else:
                active = self._require_active_tournament_runner(
                    connection,
                    snapshot.tournament_id,
                    lease,
                )
                generation = active.generation

            committed = _checked_usage_add(snapshot.spent, snapshot.reserved)
            prospective = _checked_usage_add(committed, batch)
            for dimension in ("calls", "input", "output", "estimated_cost"):
                limit = getattr(snapshot.limits, dimension)
                if limit is not None and getattr(prospective, dimension) > limit:
                    raise BudgetExceededError(dimension)
            if any(
                connection.execute(
                    "SELECT 1 FROM provider_call_attempts WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchone()
                for attempt_id in ids
            ):
                raise ProviderCallAttemptCollisionError("attempt_id is already present")

            new_reserved = _checked_usage_add(snapshot.reserved, batch)
            self._update_provider_budget_counters(
                connection,
                snapshot,
                spent=snapshot.spent,
                reserved=new_reserved,
                poison_reason_code=snapshot.poison_reason_code,
            )
            now = self._database_epoch(connection)
            connection.executemany(
                """
                INSERT INTO provider_call_attempts (
                    attempt_id, budget_id, route_id, state, runner_generation,
                    bound_calls, bound_input_tokens, bound_output_tokens,
                    bound_estimated_cost_nanos, actual_calls,
                    actual_input_tokens, actual_output_tokens,
                    actual_estimated_cost_nanos, charged_calls,
                    charged_input_tokens, charged_output_tokens,
                    charged_estimated_cost_nanos, created_at_epoch,
                    dispatched_at_epoch, finished_at_epoch
                ) VALUES (?, ?, ?, 'reserved', ?, 1, ?, ?, ?, NULL, NULL, NULL,
                          NULL, NULL, NULL, NULL, NULL, ?, NULL, NULL)
                """,
                [
                    (
                        attempt_id,
                        budget_id,
                        route_id,
                        generation,
                        bound.input,
                        bound.output,
                        bound.estimated_cost,
                        now,
                    )
                    for attempt_id, route_id, bound in zip(
                        ids,
                        resolved_route_ids,
                        requested,
                        strict=True,
                    )
                ],
            )
            result = tuple(
                self._load_provider_attempt_in_transaction(connection, attempt_id)
                for attempt_id in ids
            )
            if any(attempt is None for attempt in result):
                raise StorageError("Provider call reservations were not durably created")
            self._provider_budget_snapshot_in_transaction(connection, budget_id)
            connection.commit()
            return result  # type: ignore[return-value]
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _finish_provider_attempt_in_transaction(
        connection: sqlite3.Connection,
        snapshot: ProviderBudgetSnapshot,
        attempt: ProviderCallAttempt,
        *,
        state: Literal["settled", "charged_unknown", "violation"],
        charged: UsageTotals,
        actual: UsageTotals | None,
        poison_reason_code: str | None,
        finished_at_epoch: int,
    ) -> ProviderCallAttempt:
        finished_at_epoch = max(
            finished_at_epoch,
            attempt.created_at_epoch,
            attempt.dispatched_at_epoch or 0,
        )
        bound = _usage_from_bounds(attempt.bounds)
        reserved = _checked_usage_subtract(snapshot.reserved, bound)
        spent = _checked_usage_add(snapshot.spent, charged)
        _ProviderUsageMixin._update_provider_budget_counters(
            connection,
            snapshot,
            spent=spent,
            reserved=reserved,
            poison_reason_code=poison_reason_code,
        )
        actual_values: tuple[int | None, ...]
        if actual is None:
            actual_values = (None, None, None, None)
        else:
            actual_values = (
                actual.calls,
                actual.input,
                actual.output,
                actual.estimated_cost,
            )
        updated = connection.execute(
            """
            UPDATE provider_call_attempts
            SET state = ?, actual_calls = ?, actual_input_tokens = ?,
                actual_output_tokens = ?, actual_estimated_cost_nanos = ?,
                charged_calls = ?, charged_input_tokens = ?,
                charged_output_tokens = ?, charged_estimated_cost_nanos = ?,
                finished_at_epoch = ?
            WHERE attempt_id = ? AND state IN ('reserved', 'dispatched')
            """,
            (
                state,
                *actual_values,
                charged.calls,
                charged.input,
                charged.output,
                charged.estimated_cost,
                finished_at_epoch,
                attempt.attempt_id,
            ),
        )
        if updated.rowcount != 1:
            raise ReservationStateError("Provider call attempt changed concurrently")
        result = _ProviderUsageMixin._load_provider_attempt_in_transaction(
            connection,
            attempt.attempt_id,
        )
        if result is None:
            raise StorageError("Provider call attempt disappeared during settlement")
        return result

    @staticmethod
    def _provider_violation_charge(
        snapshot: ProviderBudgetSnapshot,
        bound: UsageTotals,
        actual: UsageTotals,
    ) -> tuple[UsageTotals, str]:
        """Choose an exact charge unless all live aggregates would overflow SQLite."""

        try:
            remaining_reserved = _checked_usage_subtract(snapshot.reserved, bound)
            spent = _checked_usage_add(snapshot.spent, actual)
            _checked_usage_add(spent, remaining_reserved)
        except (ReservationStateError, UsageCounterOverflowError):
            return bound, UsageCounterOverflowError.reason_code
        return actual, UsageExceedsReservationError.reason_code

    @staticmethod
    def _provider_policy_cost(
        policy: ProviderBudgetPolicy,
        route_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
    ) -> int:
        price = policy.price_for(route_id)
        if price is None:
            return 0
        return price.estimate(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def get_provider_call_attempt(self, attempt_id: str) -> ProviderCallAttempt | None:
        """Load one opaque call-attempt record without any Provider content."""

        attempt_id = _validate_usage_ledger_id(attempt_id, "attempt_id")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            try:
                attempt = self._load_provider_attempt_in_transaction(connection, attempt_id)
                if attempt is not None:
                    self._provider_budget_snapshot_in_transaction(
                        connection,
                        attempt.budget_id,
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return attempt

    def mark_provider_call_dispatched(
        self,
        attempt_id: str,
        *,
        lease: TournamentRunnerLease | None = None,
    ) -> ProviderCallAttempt:
        """Durably mark a reservation dispatched before any network I/O."""

        attempt_id = _validate_usage_ledger_id(attempt_id, "attempt_id")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            attempt = self._load_provider_attempt_in_transaction(connection, attempt_id)
            if attempt is None:
                raise StorageError("Provider call attempt does not exist")
            snapshot = self._provider_budget_snapshot_in_transaction(
                connection,
                attempt.budget_id,
            )
            if snapshot is None:
                raise StorageError("Provider budget does not exist")
            self._require_provider_attempt_fence(
                connection,
                snapshot,
                attempt,
                lease,
            )
            if attempt.state == "dispatched":
                connection.commit()
                return attempt
            if attempt.state != "reserved":
                raise ReservationStateError(
                    f"cannot dispatch reservation in state {attempt.state}"
                )
            if snapshot.finalized:
                raise ReservationStateError("cannot dispatch from a finalized budget")
            if snapshot.poisoned:
                raise BudgetPoisonedError(
                    f"usage budget is poisoned by {snapshot.poison_reason_code}"
                )
            now = max(self._database_epoch(connection), attempt.created_at_epoch)
            updated = connection.execute(
                """
                UPDATE provider_call_attempts
                SET state = 'dispatched', dispatched_at_epoch = ?
                WHERE attempt_id = ? AND state = 'reserved'
                """,
                (now, attempt_id),
            )
            if updated.rowcount != 1:
                raise ReservationStateError("Provider call attempt changed concurrently")
            result = self._load_provider_attempt_in_transaction(connection, attempt_id)
            if result is None:
                raise StorageError("Provider call attempt disappeared during dispatch")
            self._provider_budget_snapshot_in_transaction(connection, attempt.budget_id)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def charge_provider_call_unknown(
        self,
        attempt_id: str,
        *,
        lease: TournamentRunnerLease | None = None,
    ) -> ProviderCallAttempt:
        """Conservatively charge the complete bound when actual usage is unknown."""

        attempt_id = _validate_usage_ledger_id(attempt_id, "attempt_id")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            attempt = self._load_provider_attempt_in_transaction(connection, attempt_id)
            if attempt is None:
                raise StorageError("Provider call attempt does not exist")
            snapshot = self._provider_budget_snapshot_in_transaction(
                connection,
                attempt.budget_id,
            )
            if snapshot is None:
                raise StorageError("Provider budget does not exist")
            self._require_provider_attempt_fence(
                connection,
                snapshot,
                attempt,
                lease,
            )
            if attempt.state == "charged_unknown":
                connection.commit()
                return attempt
            if attempt.state not in {"reserved", "dispatched"}:
                raise ReservationStateError(
                    f"cannot charge unknown reservation in state {attempt.state}"
                )
            now = max(self._database_epoch(connection), attempt.created_at_epoch)
            result = self._finish_provider_attempt_in_transaction(
                connection,
                snapshot,
                attempt,
                state="charged_unknown",
                charged=_usage_from_bounds(attempt.bounds),
                actual=None,
                poison_reason_code=snapshot.poison_reason_code,
                finished_at_epoch=now,
            )
            self._provider_budget_snapshot_in_transaction(connection, attempt.budget_id)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def release_provider_call_pre_dispatch(
        self,
        attempt_id: str,
        *,
        lease: TournamentRunnerLease | None = None,
    ) -> ProviderCallAttempt:
        """Release only a reservation proven not to have reached network dispatch."""

        attempt_id = _validate_usage_ledger_id(attempt_id, "attempt_id")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            attempt = self._load_provider_attempt_in_transaction(connection, attempt_id)
            if attempt is None:
                raise StorageError("Provider call attempt does not exist")
            snapshot = self._provider_budget_snapshot_in_transaction(
                connection,
                attempt.budget_id,
            )
            if snapshot is None:
                raise StorageError("Provider budget does not exist")
            self._require_provider_attempt_fence(
                connection,
                snapshot,
                attempt,
                lease,
            )
            if attempt.state == "released_pre_dispatch":
                connection.commit()
                return attempt
            if attempt.state != "reserved":
                raise ReservationStateError(
                    f"cannot release reservation in state {attempt.state}"
                )
            reserved = _checked_usage_subtract(
                snapshot.reserved,
                _usage_from_bounds(attempt.bounds),
            )
            self._update_provider_budget_counters(
                connection,
                snapshot,
                spent=snapshot.spent,
                reserved=reserved,
                poison_reason_code=snapshot.poison_reason_code,
            )
            now = max(
                self._database_epoch(connection),
                attempt.created_at_epoch,
                attempt.dispatched_at_epoch or 0,
            )
            updated = connection.execute(
                """
                UPDATE provider_call_attempts
                SET state = 'released_pre_dispatch', finished_at_epoch = ?
                WHERE attempt_id = ? AND state = 'reserved'
                """,
                (now, attempt_id),
            )
            if updated.rowcount != 1:
                raise ReservationStateError("Provider call attempt changed concurrently")
            result = self._load_provider_attempt_in_transaction(connection, attempt_id)
            if result is None:
                raise StorageError("Provider call attempt disappeared during release")
            self._provider_budget_snapshot_in_transaction(connection, attempt.budget_id)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def settle_provider_call(
        self,
        attempt_id: str,
        usage: UsageTotals | None,
        *,
        lease: TournamentRunnerLease | None = None,
    ) -> ProviderCallAttempt:
        """Settle reported usage, or charge the full bound when it is unknown."""

        attempt_id = _validate_usage_ledger_id(attempt_id, "attempt_id")
        connection = self._connect()
        deferred_error: Exception | None = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            attempt = self._load_provider_attempt_in_transaction(connection, attempt_id)
            if attempt is None:
                raise StorageError("Provider call attempt does not exist")
            snapshot = self._provider_budget_snapshot_in_transaction(
                connection,
                attempt.budget_id,
            )
            if snapshot is None:
                raise StorageError("Provider budget does not exist")
            self._require_provider_attempt_fence(
                connection,
                snapshot,
                attempt,
                lease,
            )
            if attempt.state == "settled":
                if usage == attempt.actual:
                    connection.commit()
                    return attempt
                raise ReservationStateError("settlement conflicts with settled usage")
            if attempt.state == "charged_unknown":
                if usage is None:
                    connection.commit()
                    return attempt
                raise ReservationStateError("unknown usage charge cannot be replaced")
            if attempt.state == "violation":
                if usage == attempt.actual:
                    raise UsageExceedsReservationError("usage exceeded reservation")
                raise ReservationStateError("settlement conflicts with recorded overrun")
            if attempt.state != "dispatched":
                raise ReservationStateError(
                    f"cannot settle reservation in state {attempt.state}"
                )

            now = self._database_epoch(connection)
            bound = _usage_from_bounds(attempt.bounds)
            if usage is None:
                result = self._finish_provider_attempt_in_transaction(
                    connection,
                    snapshot,
                    attempt,
                    state="charged_unknown",
                    charged=bound,
                    actual=None,
                    poison_reason_code=snapshot.poison_reason_code,
                    finished_at_epoch=now,
                )
            elif not isinstance(usage, UsageTotals):
                result = self._finish_provider_attempt_in_transaction(
                    connection,
                    snapshot,
                    attempt,
                    state="charged_unknown",
                    charged=bound,
                    actual=None,
                    poison_reason_code=snapshot.poison_reason_code,
                    finished_at_epoch=now,
                )
                deferred_error = UsageValidationError(
                    "settled usage must be UsageTotals or None"
                )
            elif usage.calls != 1:
                if usage.calls == 0:
                    result = self._finish_provider_attempt_in_transaction(
                        connection,
                        snapshot,
                        attempt,
                        state="charged_unknown",
                        charged=bound,
                        actual=None,
                        poison_reason_code=snapshot.poison_reason_code,
                        finished_at_epoch=now,
                    )
                    deferred_error = UsageValidationError(
                        "settled usage calls must equal one"
                    )
                else:
                    charged, poison_reason = self._provider_violation_charge(
                        snapshot,
                        bound,
                        usage,
                    )
                    result = self._finish_provider_attempt_in_transaction(
                        connection,
                        snapshot,
                        attempt,
                        state="violation",
                        charged=charged,
                        actual=usage,
                        poison_reason_code=poison_reason,
                        finished_at_epoch=now,
                    )
                    deferred_error = UsageExceedsReservationError(
                        "reported call count exceeded reservation"
                    )
            elif usage.estimated_cost != self._provider_policy_cost(
                snapshot.policy,
                attempt.route_id,
                input_tokens=usage.input,
                output_tokens=usage.output,
            ):
                result = self._finish_provider_attempt_in_transaction(
                    connection,
                    snapshot,
                    attempt,
                    state="charged_unknown",
                    charged=bound,
                    actual=None,
                    poison_reason_code=snapshot.poison_reason_code,
                    finished_at_epoch=now,
                )
                deferred_error = UsageValidationError(
                    "settled estimated_cost must match the frozen route price"
                )
            elif any(
                getattr(usage, dimension) > getattr(bound, dimension)
                for dimension in ("calls", "input", "output", "estimated_cost")
            ):
                charged, poison_reason = self._provider_violation_charge(
                    snapshot,
                    bound,
                    usage,
                )
                result = self._finish_provider_attempt_in_transaction(
                    connection,
                    snapshot,
                    attempt,
                    state="violation",
                    charged=charged,
                    actual=usage,
                    poison_reason_code=poison_reason,
                    finished_at_epoch=now,
                )
                deferred_error = UsageExceedsReservationError(
                    "reported usage exceeded reservation"
                )
            else:
                result = self._finish_provider_attempt_in_transaction(
                    connection,
                    snapshot,
                    attempt,
                    state="settled",
                    charged=usage,
                    actual=usage,
                    poison_reason_code=snapshot.poison_reason_code,
                    finished_at_epoch=now,
                )
            self._provider_budget_snapshot_in_transaction(connection, attempt.budget_id)
            connection.commit()
            if deferred_error is not None:
                raise deferred_error
            return result
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def finalize_provider_budget(
        self,
        budget_id: str,
        *,
        lease: TournamentRunnerLease | None = None,
    ) -> ProviderBudgetSnapshot:
        """Seal a budget only after every reservation reaches a terminal state."""

        budget_id = _validate_usage_ledger_id(budget_id, "budget_id")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            snapshot = self._provider_budget_snapshot_in_transaction(connection, budget_id)
            if snapshot is None:
                raise StorageError("Provider budget does not exist")
            if snapshot.finalized:
                connection.commit()
                return snapshot
            if snapshot.tournament_id is None:
                if lease is not None:
                    raise ValueError(
                        "non-tournament Provider budget does not accept a runner lease"
                    )
            else:
                self._require_active_tournament_runner(
                    connection,
                    snapshot.tournament_id,
                    lease,
                )
            pending = connection.execute(
                """
                SELECT count(*) AS count
                FROM provider_call_attempts
                WHERE budget_id = ? AND state IN ('reserved', 'dispatched')
                """,
                (budget_id,),
            ).fetchone()["count"]
            if pending:
                raise ProviderBudgetPendingError(
                    "Provider budget has unresolved call attempts"
                )
            now = self._database_epoch(connection)
            updated = connection.execute(
                """
                UPDATE provider_budgets
                SET finalized_at_epoch = ?
                WHERE budget_id = ? AND finalized_at_epoch IS NULL
                """,
                (now, budget_id),
            )
            if updated.rowcount != 1:
                raise StorageError("Provider budget changed concurrently during finalization")
            result = self._provider_budget_snapshot_in_transaction(connection, budget_id)
            if result is None:
                raise StorageError("Provider budget disappeared during finalization")
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _close_stale_provider_attempts(
        self,
        connection: sqlite3.Connection,
        tournament_id: str,
        generation: int,
        *,
        finished_at_epoch: int,
    ) -> None:
        """Resolve every old-generation reservation before a lease takeover."""

        budget_rows = connection.execute(
            "SELECT budget_id FROM provider_budgets WHERE tournament_id = ?",
            (tournament_id,),
        ).fetchall()
        for budget_row in budget_rows:
            budget_id = budget_row["budget_id"]
            snapshot = self._provider_budget_snapshot_in_transaction(connection, budget_id)
            if snapshot is None:
                raise StorageError("Tournament Provider budget disappeared during takeover")
            attempt_rows = connection.execute(
                """
                SELECT * FROM provider_call_attempts
                WHERE budget_id = ? AND runner_generation = ?
                  AND state IN ('reserved', 'dispatched')
                ORDER BY attempt_id
                """,
                (budget_id, generation),
            ).fetchall()
            for attempt_row in attempt_rows:
                attempt = self._provider_attempt_from_row(attempt_row)
                snapshot = self._provider_budget_snapshot_in_transaction(
                    connection,
                    budget_id,
                )
                if snapshot is None:
                    raise StorageError("Tournament Provider budget disappeared during takeover")
                if attempt.state == "reserved":
                    reserved = _checked_usage_subtract(
                        snapshot.reserved,
                        _usage_from_bounds(attempt.bounds),
                    )
                    self._update_provider_budget_counters(
                        connection,
                        snapshot,
                        spent=snapshot.spent,
                        reserved=reserved,
                        poison_reason_code=snapshot.poison_reason_code,
                    )
                    updated = connection.execute(
                        """
                        UPDATE provider_call_attempts
                        SET state = 'released_pre_dispatch', finished_at_epoch = ?
                        WHERE attempt_id = ? AND state = 'reserved'
                        """,
                        (
                            max(finished_at_epoch, attempt.created_at_epoch),
                            attempt.attempt_id,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise StorageError(
                            "Provider reservation changed during runner takeover"
                        )
                else:
                    self._finish_provider_attempt_in_transaction(
                        connection,
                        snapshot,
                        attempt,
                        state="charged_unknown",
                        charged=_usage_from_bounds(attempt.bounds),
                        actual=None,
                        poison_reason_code=snapshot.poison_reason_code,
                        finished_at_epoch=finished_at_epoch,
                    )
            self._provider_budget_snapshot_in_transaction(connection, budget_id)

    def _finalize_tournament_provider_budgets(
        self,
        connection: sqlite3.Connection,
        tournament_id: str,
    ) -> None:
        """Audit and seal all budgets owned by a checkpoint in the caller's transaction."""

        budget_rows = connection.execute(
            "SELECT budget_id FROM provider_budgets WHERE tournament_id = ? ORDER BY budget_id",
            (tournament_id,),
        ).fetchall()
        for row in budget_rows:
            snapshot = self._provider_budget_snapshot_in_transaction(
                connection,
                row["budget_id"],
            )
            if snapshot is None:
                raise StorageError("Tournament Provider budget disappeared during finalization")
            if snapshot.poisoned:
                raise BudgetPoisonedError(
                    f"Tournament Provider budget {row['budget_id']} is poisoned by "
                    f"{snapshot.poison_reason_code}"
                )
            if snapshot.reserved != UsageTotals.zero():
                raise ProviderBudgetPendingError(
                    "Tournament has unresolved Provider call attempts"
                )
        if budget_rows:
            now = self._database_epoch(connection)
            connection.execute(
                """
                UPDATE provider_budgets
                SET finalized_at_epoch = coalesce(finalized_at_epoch, ?)
                WHERE tournament_id = ?
                """,
                (now, tournament_id),
            )
            for row in budget_rows:
                self._provider_budget_snapshot_in_transaction(
                    connection,
                    row["budget_id"],
                )

