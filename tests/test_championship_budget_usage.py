"""Championship-scoped durable Provider budget integration tests."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from llmolympic.core.championship import (
    ChampionshipArchive,
    ChampionshipCheckpoint,
    prepare_championship,
    resume_championship,
)
from llmolympic.core.game import Game
from llmolympic.core.player import LLMPlayer
from llmolympic.core.storage import (
    ChampionshipRunnerLease,
    ChampionshipRunnerLeaseLostError,
    ProviderBudgetCollisionError,
    ProviderBudgetPendingError,
    SQLiteStore,
)
from llmolympic.core.usage import (
    BudgetLimits,
    CallBounds,
    ProviderBudgetPolicy,
    RouteBudgetPolicy,
    UsageTotals,
)
from llmolympic.games import create_game
from llmolympic.providers.mock import MockProvider

ROUTE_ID = "route:v1:" + "c" * 64


def _policy() -> ProviderBudgetPolicy:
    return ProviderBudgetPolicy(
        max_output_tokens_per_call=100,
        routes=(RouteBudgetPolicy(route_id=ROUTE_ID),),
    )


def _players() -> list[LLMPlayer]:
    return [
        LLMPlayer(name=f"p{index}", provider=MockProvider("fixed"), model="x")
        for index in range(4)
    ]


def _prepared(
    championship_id: str,
) -> tuple[Game, list[LLMPlayer], ChampionshipCheckpoint]:
    game = create_game("knowledge_quiz", rounds=1)
    players = _players()
    checkpoint = prepare_championship(
        game,
        players,
        seed=17,
        championship_id=championship_id,
    )
    return game, players, checkpoint


def _complete_checkpoint(
    store: SQLiteStore,
    game: Game,
    players: list[LLMPlayer],
    checkpoint: ChampionshipCheckpoint,
    lease: ChampionshipRunnerLease,
) -> ChampionshipArchive:
    def save(updated: ChampionshipCheckpoint) -> None:
        store.save_championship_checkpoint(updated, lease=lease)

    return asyncio.run(
        resume_championship(
            game,
            players,
            checkpoint,
            on_checkpoint=save,
        )
    )


def test_championship_checkpoint_budget_is_atomic_idempotent_and_bindable(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "championship-budget.db")
    _, _, checkpoint = _prepared("championship-budget")
    limits = BudgetLimits(calls=4, input=100, output=100)
    policy = _policy()

    created, budget = store.create_championship_checkpoint_with_provider_budget(
        checkpoint,
        "championship-budget-ledger",
        limits,
        policy,
    )

    assert created.inserted
    assert budget.tournament_id is None
    assert budget.championship_id == checkpoint.championship_id
    assert store.load_championship_provider_budget(checkpoint.championship_id) == budget
    repeated, repeated_budget = (
        store.create_championship_checkpoint_with_provider_budget(
            checkpoint,
            "championship-budget-ledger",
            limits,
            policy,
        )
    )
    assert not repeated.inserted
    assert repeated_budget == budget

    lease = store.claim_championship_runner(checkpoint.championship_id).lease
    bound = store.bind_championship_usage_budget(
        checkpoint.championship_id,
        lease=lease,
    )
    assert bound is not None
    assert bound.budget_id == budget.budget_id
    with pytest.raises(ValueError, match="both a tournament and a championship"):
        store.create_provider_budget(
            "invalid-double-scope",
            limits,
            policy,
            tournament_id="tournament",
            championship_id=checkpoint.championship_id,
            lease=lease,
        )
    with pytest.raises(ProviderBudgetCollisionError, match="already has"):
        store.create_provider_budget(
            "second-championship-budget",
            limits,
            policy,
            championship_id=checkpoint.championship_id,
            lease=lease,
        )


def test_failed_atomic_championship_budget_creation_rolls_back_both_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "championship-budget-create-rollback.db"
    store = SQLiteStore(path)
    _, _, checkpoint = _prepared("championship-budget-create-rollback")

    def fail_budget_insert(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected championship budget insert failure")

    monkeypatch.setattr(store, "_insert_provider_budget_in_transaction", fail_budget_insert)
    with pytest.raises(RuntimeError, match="injected championship budget insert failure"):
        store.create_championship_checkpoint_with_provider_budget(
            checkpoint,
            "championship-budget-ledger",
            BudgetLimits(calls=1),
            _policy(),
        )

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM championship_checkpoints").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM provider_budgets").fetchone()[0] == 0


def test_expired_championship_lease_takeover_reconciles_provider_attempts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "championship-budget-takeover.db"
    store = SQLiteStore(path)
    _, _, checkpoint = _prepared("championship-budget-takeover")
    store.create_championship_checkpoint_with_provider_budget(
        checkpoint,
        "championship-budget-ledger",
        BudgetLimits(calls=3, input=30, output=30),
        _policy(),
    )
    stale = store.claim_championship_runner(checkpoint.championship_id).lease
    budget = store.bind_championship_usage_budget(
        checkpoint.championship_id,
        lease=stale,
    )
    assert budget is not None
    dispatched, reserved = budget.reserve_many(
        (
            CallBounds(input=4, output=5, route_id=ROUTE_ID),
            CallBounds(input=7, output=8, route_id=ROUTE_ID),
        )
    )
    dispatched.dispatch()
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE championship_runner_leases
            SET acquired_at_epoch = 0, renewed_at_epoch = 0, expires_at_epoch = 1
            WHERE championship_id = ?
            """,
            (checkpoint.championship_id,),
        )

    active = store.claim_championship_runner(checkpoint.championship_id).lease

    assert active.generation == stale.generation + 1
    old_dispatched = store.get_provider_call_attempt(dispatched.reservation_id)
    old_reserved = store.get_provider_call_attempt(reserved.reservation_id)
    assert old_dispatched is not None and old_dispatched.state == "charged_unknown"
    assert old_dispatched.charged == dispatched.bounds.as_totals()
    assert old_reserved is not None and old_reserved.state == "released_pre_dispatch"
    snapshot = store.load_championship_provider_budget(checkpoint.championship_id)
    assert snapshot is not None
    assert snapshot.spent == dispatched.bounds.as_totals()
    assert snapshot.reserved == UsageTotals.zero()
    with pytest.raises(ChampionshipRunnerLeaseLostError):
        store.charge_provider_call_unknown(dispatched.reservation_id, lease=stale)

    current_budget = store.bind_championship_usage_budget(
        checkpoint.championship_id,
        lease=active,
    )
    assert current_budget is not None
    current = current_budget.reserve(CallBounds(route_id=ROUTE_ID))
    current_attempt = store.get_provider_call_attempt(current.reservation_id)
    assert current_attempt is not None
    assert current_attempt.runner_generation == active.generation
    current.release_pre_dispatch()


def test_championship_finalize_budget_is_pending_safe_and_transactional(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "championship-budget-finalize.db"
    store = SQLiteStore(path)
    game, players, checkpoint = _prepared("championship-budget-finalize")
    store.create_championship_checkpoint_with_provider_budget(
        checkpoint,
        "championship-budget-ledger",
        BudgetLimits(calls=1, output=20),
        _policy(),
    )
    lease = store.claim_championship_runner(checkpoint.championship_id).lease
    budget = store.bind_championship_usage_budget(
        checkpoint.championship_id,
        lease=lease,
    )
    assert budget is not None
    pending = budget.reserve(CallBounds(output=20, route_id=ROUTE_ID)).dispatch()
    archive = _complete_checkpoint(store, game, players, checkpoint, lease)

    with pytest.raises(ProviderBudgetPendingError, match="unresolved"):
        store.finalize_championship_checkpoint(checkpoint.championship_id, lease=lease)
    assert store.get_championship(checkpoint.championship_id) is None
    open_budget = store.load_championship_provider_budget(checkpoint.championship_id)
    assert open_budget is not None and not open_budget.finalized

    pending.charge_unknown()
    with monkeypatch.context() as patcher:
        def fail_archive_insert(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("injected championship archive failure")

        patcher.setattr(store, "_save_championship_in_transaction", fail_archive_insert)
        with pytest.raises(RuntimeError, match="injected championship archive failure"):
            store.finalize_championship_checkpoint(checkpoint.championship_id, lease=lease)

    rolled_back = store.load_championship_provider_budget(checkpoint.championship_id)
    assert rolled_back is not None and not rolled_back.finalized
    assert store.get_championship(checkpoint.championship_id) is None
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT status FROM championship_checkpoints WHERE championship_id = ?",
            (checkpoint.championship_id,),
        ).fetchone()[0] == "in_progress"

    result = store.finalize_championship_checkpoint(
        checkpoint.championship_id,
        lease=lease,
    )
    assert result.inserted and not result.rated
    assert store.get_championship(checkpoint.championship_id) == archive
    finalized = store.load_championship_provider_budget(checkpoint.championship_id)
    assert finalized is not None and finalized.finalized
    assert finalized.spent == UsageTotals(calls=1, input=0, output=20, estimated_cost=0)
