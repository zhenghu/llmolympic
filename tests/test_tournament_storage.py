"""SQLite v6 round-robin persistence, checkpoints, leases, and frozen-ELO tests."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from itertools import combinations

import pytest

from llmolympic.core.archive import MatchArchive
from llmolympic.core.events import EventType, MatchEvent
from llmolympic.core.series import series_from_legs
from llmolympic.core.storage import (
    SCHEMA_VERSION,
    BudgetPoisonedError,
    MatchIdCollisionError,
    ProviderBudgetPendingError,
    SeriesIdCollisionError,
    SQLiteStore,
    StorageError,
    TournamentCheckpointCollisionError,
    TournamentIdCollisionError,
    TournamentRunnerLeaseBusyError,
    TournamentRunnerLeaseLostError,
)
from llmolympic.core.tournament import (
    TournamentArchive,
    TournamentCheckpoint,
    round_robin_pair_seed,
    tournament_from_series,
)
from llmolympic.core.usage import (
    BudgetLimits,
    CallBounds,
    ProviderBudgetPolicy,
    RouteBudgetPolicy,
    UsageExceedsReservationError,
    UsageTotals,
)

STARTED = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
USAGE_ROUTE_ID = "route:v1:" + "2" * 64


def _usage_policy(*, max_output_tokens_per_call: int = 100) -> ProviderBudgetPolicy:
    return ProviderBudgetPolicy(
        max_output_tokens_per_call=max_output_tokens_per_call,
        routes=(RouteBudgetPolicy(route_id=USAGE_ROUTE_ID),),
    )


def _descriptor(name: str) -> dict:
    return {
        "name": name,
        "display_name": name,
        "entrant_id": f"test:{name}",
        "kind": "mock",
        "model": name,
    }


def _match(
    *,
    match_id: str,
    seed: int,
    players: tuple[dict, dict],
    winner: str,
    started_at: datetime,
    source: str,
) -> MatchArchive:
    scores = {
        descriptor["name"]: 1.0 if descriptor["name"] == winner else 0.0 for descriptor in players
    }
    return MatchArchive(
        schema_version=2,
        source=source,
        match_id=match_id,
        game="math_quiz",
        seed=seed,
        players=list(players),
        events=[
            MatchEvent(
                seq=0,
                type=EventType.MATCH_STARTED,
                timestamp=started_at,
                data={
                    "game": "math_quiz",
                    "seed": seed,
                    "game_config": {},
                    "players": list(players),
                },
            ),
            MatchEvent(
                seq=1,
                type=EventType.MATCH_FINISHED,
                timestamp=started_at + timedelta(seconds=1),
                data={"scores": scores},
            ),
        ],
        moves=[],
        scores=scores,
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=1),
    )


def _tournament(
    *,
    tournament_id: str = "tournament-1",
    names: tuple[str, ...] = ("A", "B", "C"),
    source: str = "local_engine",
    reverse_winners: bool = False,
) -> TournamentArchive:
    players = tuple(_descriptor(name) for name in names)
    series_archives = []
    for pairing_number, (first_index, second_index) in enumerate(
        combinations(range(len(players)), 2), start=1
    ):
        first = players[first_index]
        second = players[second_index]
        ordered_names = sorted((first["name"], second["name"]))
        winner = ordered_names[-1] if reverse_winners else ordered_names[0]
        seed = round_robin_pair_seed(
            42,
            first["entrant_id"],
            second["entrant_id"],
        )
        pairing_started = STARTED + timedelta(seconds=(pairing_number - 1) * 4)
        first_leg = _match(
            match_id=f"{tournament_id}-pair-{pairing_number}-leg-1",
            seed=seed,
            players=(first, second),
            winner=winner,
            started_at=pairing_started,
            source=source,
        )
        second_leg = _match(
            match_id=f"{tournament_id}-pair-{pairing_number}-leg-2",
            seed=seed,
            players=(second, first),
            winner=winner,
            started_at=pairing_started + timedelta(seconds=2),
            source=source,
        )
        series_archives.append(
            series_from_legs(
                first_leg,
                second_leg,
                series_id=f"{tournament_id}-series-{pairing_number}",
            )
        )
    return tournament_from_series(
        players,
        series_archives,
        seed=42,
        tournament_id=tournament_id,
    )


def _seed_unequal_ratings(store: SQLiteStore) -> None:
    for index, opponent in enumerate(("B", "C"), start=1):
        store.save_match(
            _match(
                match_id=f"warmup-{index}",
                seed=index,
                players=(_descriptor("A"), _descriptor(opponent)),
                winner="A",
                started_at=STARTED - timedelta(minutes=3 - index),
                source="local_engine",
            ),
            rating_source="engine",
        )


def _checkpoint(
    tournament: TournamentArchive,
    completed_count: int = 0,
    *,
    game_config: dict | None = None,
) -> TournamentCheckpoint:
    completed_series = tuple(pairing.series for pairing in tournament.pairings[:completed_count])
    created_at = tournament.started_at - timedelta(seconds=1)
    return TournamentCheckpoint(
        tournament_id=tournament.tournament_id,
        game=tournament.game,
        game_config={} if game_config is None else game_config,
        seed=tournament.seed,
        max_attempts=3,
        players=tournament.players,
        schedule=tuple(
            {
                "pairing_number": pairing.pairing_number,
                "player_indices": pairing.player_indices,
                "seed": pairing.seed,
            }
            for pairing in tournament.pairings
        ),
        completed_series=completed_series,
        created_at=created_at,
        updated_at=(created_at if not completed_series else completed_series[-1].finished_at),
    )


def test_tournament_save_round_trip_and_frozen_rating_ledger(tmp_path) -> None:
    path = tmp_path / "tournament.db"
    tournament = _tournament()
    store = SQLiteStore(path)

    result = store.save_tournament(tournament, rating_source="engine")

    assert result.inserted and result.rated
    assert result.pairing_count == 3
    assert result.match_count == 6
    assert len(result.rating_changes) == 6
    loaded = store.get_tournament(tournament.tournament_id)
    assert loaded is not None
    assert loaded.model_dump(mode="json") == tournament.model_dump(mode="json")
    summaries = store.list_matches(limit=10)
    assert len(summaries) == 6
    assert {summary.tournament_id for summary in summaries} == {tournament.tournament_id}
    assert {summary.pairing_number for summary in summaries} == {1, 2, 3}
    assert {summary.pairing_count for summary in summaries} == {3}
    assert all(entry.games_played == 4 for entry in store.leaderboard())

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("SELECT count(*) FROM tournament_archives").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM tournament_pairings").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM series_archives").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM matches").fetchone()[0] == 6
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 24
        assert (
            connection.execute("SELECT count(*) FROM tournament_rating_snapshots").fetchone()[0]
            == 6
        )
        assert (
            connection.execute("SELECT count(*) FROM tournament_rating_contributions").fetchone()[0]
            == 24
        )
        assert connection.execute(
            """
            SELECT rating_operation_seq, match_id, series_id, tournament_id
            FROM rating_operations
            """
        ).fetchone() == (1, None, None, tournament.tournament_id)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_checkpoint_prefix_reopens_finalizes_and_rates_exactly_once(tmp_path) -> None:
    path = tmp_path / "checkpoint.db"
    tournament = _tournament(tournament_id="checkpoint-tournament")
    store = SQLiteStore(path)

    created = store.save_tournament_checkpoint(_checkpoint(tournament))
    lease = store.claim_tournament_runner(tournament.tournament_id).lease

    assert created.inserted
    assert created.completed_pairing_count == 0
    assert created.pairing_count == 3
    assert store.get_tournament(tournament.tournament_id) is None
    assert store.leaderboard() == []
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM matches").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM series_archives").fetchone()[0] == 0
        config_json = connection.execute(
            "SELECT config_json FROM tournament_checkpoints"
        ).fetchone()[0]
        assert "completed_series" not in config_json

    for completed_count in range(1, 4):
        result = SQLiteStore(path, create=False).save_tournament_checkpoint(
            _checkpoint(tournament, completed_count),
            lease=lease,
        )
        assert result.inserted
        assert result.completed_pairing_count == completed_count
        reopened = SQLiteStore(path, create=False).get_tournament_checkpoint(
            tournament.tournament_id
        )
        assert reopened is not None
        assert len(reopened.completed_series) == completed_count
        assert reopened.model_dump(mode="json") == _checkpoint(
            tournament, completed_count
        ).model_dump(mode="json")

    finalized = SQLiteStore(path, create=False).finalize_tournament_checkpoint(
        tournament.tournament_id,
        lease=lease,
    )

    assert finalized.inserted and finalized.rated
    loaded = SQLiteStore(path, create=False).get_tournament(tournament.tournament_id)
    assert loaded is not None
    assert loaded.model_dump(mode="json") == tournament.model_dump(mode="json")
    with sqlite3.connect(path) as connection:
        counts_before = {
            table: connection.execute(
                f"SELECT count(*) FROM {table}"  # noqa: S608 - fixed allowlist
            ).fetchone()[0]
            for table in (
                "matches",
                "series_archives",
                "rating_history",
                "tournament_rating_snapshots",
                "tournament_rating_contributions",
            )
        }
        assert (
            connection.execute("SELECT status FROM tournament_checkpoints").fetchone()[0]
            == "finalized"
        )
        assert (
            connection.execute("SELECT count(*) FROM tournament_checkpoint_series").fetchone()[0]
            == 3
        )

    repeated = SQLiteStore(path, create=False).finalize_tournament_checkpoint(
        tournament.tournament_id
    )

    assert not repeated.inserted and repeated.rated
    with sqlite3.connect(path) as connection:
        assert {
            table: connection.execute(
                f"SELECT count(*) FROM {table}"  # noqa: S608 - fixed allowlist
            ).fetchone()[0]
            for table in counts_before
        } == counts_before


def test_runner_lease_claim_is_exclusive_fenced_and_secret(tmp_path) -> None:
    path = tmp_path / "runner-lease.db"
    tournament = _tournament(tournament_id="runner-lease")
    store = SQLiteStore(path)
    store.save_tournament_checkpoint(_checkpoint(tournament))

    first = store.claim_tournament_runner(tournament.tournament_id).lease

    assert first.generation == 1
    assert first.token not in repr(first)
    assert first.expires_at_epoch > first.renewed_at_epoch
    with pytest.raises(TournamentRunnerLeaseBusyError, match="另一个执行者"):
        SQLiteStore(path, create=False).claim_tournament_runner(tournament.tournament_id)
    with sqlite3.connect(path) as connection:
        generation, digest = connection.execute(
            """
            SELECT generation, token_digest FROM tournament_runner_leases
            WHERE tournament_id = ?
            """,
            (tournament.tournament_id,),
        ).fetchone()
    assert generation == 1
    assert isinstance(digest, bytes) and len(digest) == 32
    assert first.token.encode() not in path.read_bytes()

    store.save_tournament_checkpoint(_checkpoint(tournament, 1), lease=first)
    assert store.release_tournament_runner(first)
    assert not store.release_tournament_runner(first)
    second_claim = store.claim_tournament_runner(tournament.tournament_id)
    second = second_claim.lease
    assert len(second_claim.checkpoint.completed_series) == 1
    assert second.generation == first.generation + 1
    assert second.token != first.token
    assert store.release_tournament_runner(second)


def test_concurrent_runner_claims_have_exactly_one_winner(tmp_path) -> None:
    path = tmp_path / "concurrent-runner-lease.db"
    tournament = _tournament(tournament_id="concurrent-runner-lease")
    SQLiteStore(path).save_tournament_checkpoint(_checkpoint(tournament))

    def claim(_: int):
        try:
            return (
                SQLiteStore(path, create=False)
                .claim_tournament_runner(tournament.tournament_id)
                .lease
            )
        except TournamentRunnerLeaseBusyError:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(claim, range(8)))

    winners = [lease for lease in results if lease is not None]
    assert len(winners) == 1
    assert SQLiteStore(path, create=False).release_tournament_runner(winners[0])


def test_runner_lease_blocks_a_separate_process_until_release(tmp_path) -> None:
    path = tmp_path / "cross-process-runner-lease.db"
    tournament = _tournament(tournament_id="cross-process-runner-lease")
    store = SQLiteStore(path)
    store.save_tournament_checkpoint(_checkpoint(tournament))
    active = store.claim_tournament_runner(tournament.tournament_id).lease
    script = """
import sys
from llmolympic.core.storage import SQLiteStore, TournamentRunnerLeaseBusyError

store = SQLiteStore(sys.argv[1], create=False)
try:
    claim = store.claim_tournament_runner(sys.argv[2])
except TournamentRunnerLeaseBusyError:
    print("busy")
    raise SystemExit(23)
else:
    print("claimed")
    store.release_tournament_runner(claim.lease)
"""

    blocked = subprocess.run(  # noqa: S603 - fixed interpreter and local test script
        [sys.executable, "-c", script, str(path), tournament.tournament_id],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert blocked.returncode == 23
    assert blocked.stdout.strip() == "busy"
    assert active.token not in blocked.stdout + blocked.stderr

    assert store.release_tournament_runner(active)
    claimed = subprocess.run(  # noqa: S603 - fixed interpreter and local test script
        [sys.executable, "-c", script, str(path), tournament.tournament_id],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert claimed.returncode == 0, claimed.stderr
    assert claimed.stdout.strip() == "claimed"


def test_killed_runner_expires_and_takeover_finalizes_exactly_once(tmp_path) -> None:
    path = tmp_path / "killed-runner-takeover.db"
    ready_path = tmp_path / "runner-ready"
    checkpoint_path = tmp_path / "first-checkpoint.json"
    tournament = _tournament(tournament_id="killed-runner-takeover")
    store = SQLiteStore(path)
    store.save_tournament_checkpoint(_checkpoint(tournament))
    checkpoint_path.write_text(
        _checkpoint(tournament, 1).model_dump_json(),
        encoding="utf-8",
    )
    script = """
import sys
import time
from pathlib import Path

from llmolympic.core.storage import SQLiteStore
from llmolympic.core.tournament import TournamentCheckpoint

store = SQLiteStore(sys.argv[1], create=False)
claim = store.claim_tournament_runner(sys.argv[2], lease_seconds=1)
checkpoint = TournamentCheckpoint.model_validate_json(
    Path(sys.argv[3]).read_text(encoding="utf-8")
)
store.save_tournament_checkpoint(checkpoint, lease=claim.lease, lease_seconds=1)
ready_path = Path(sys.argv[4])
ready_tmp_path = ready_path.with_suffix(".tmp")
ready_tmp_path.write_text(str(claim.lease.generation), encoding="ascii")
ready_tmp_path.replace(ready_path)
while True:
    time.sleep(60)
"""
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter and local test script
        [
            sys.executable,
            "-c",
            script,
            str(path),
            tournament.tournament_id,
            str(checkpoint_path),
            str(ready_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready_deadline = time.monotonic() + 10
        while not ready_path.exists() and time.monotonic() < ready_deadline:
            assert process.poll() is None, process.stderr.read()
            time.sleep(0.02)
        assert ready_path.exists(), "runner subprocess did not acquire its lease in time"
        first_generation = int(ready_path.read_text(encoding="ascii"))
        assert process.poll() is None
    finally:
        if process.poll() is None:
            process.kill()
        _, stderr = process.communicate(timeout=5)

    assert process.returncode != 0, stderr
    orphaned_checkpoint = store.get_tournament_checkpoint(tournament.tournament_id)
    assert orphaned_checkpoint is not None
    assert len(orphaned_checkpoint.completed_series) == 1
    with sqlite3.connect(path) as connection:
        orphaned_generation, orphaned_digest = connection.execute(
            """
            SELECT generation, token_digest FROM tournament_runner_leases
            WHERE tournament_id = ?
            """,
            (tournament.tournament_id,),
        ).fetchone()
    assert orphaned_generation == first_generation
    assert orphaned_digest is not None

    takeover_deadline = time.monotonic() + 5
    while True:
        try:
            takeover = SQLiteStore(path, create=False).claim_tournament_runner(
                tournament.tournament_id
            )
            break
        except TournamentRunnerLeaseBusyError:
            if time.monotonic() >= takeover_deadline:
                pytest.fail("killed runner lease did not expire in time")
            time.sleep(0.05)

    assert takeover.lease.generation == first_generation + 1
    assert len(takeover.checkpoint.completed_series) == 1
    for completed_count in range(2, 4):
        store.save_tournament_checkpoint(
            _checkpoint(tournament, completed_count),
            lease=takeover.lease,
        )

    finalized = store.finalize_tournament_checkpoint(
        tournament.tournament_id,
        lease=takeover.lease,
    )

    assert finalized.inserted and finalized.rated
    archived = store.get_tournament(tournament.tournament_id)
    assert archived is not None
    assert archived.model_dump(mode="json") == tournament.model_dump(mode="json")
    with sqlite3.connect(path) as connection:
        counts_before = {
            table: connection.execute(
                f"SELECT count(*) FROM {table}"  # noqa: S608 - fixed allowlist
            ).fetchone()[0]
            for table in (
                "tournament_archives",
                "matches",
                "series_archives",
                "rating_history",
                "tournament_rating_snapshots",
                "tournament_rating_contributions",
            )
        }
        status = connection.execute(
            "SELECT status FROM tournament_checkpoints WHERE tournament_id = ?",
            (tournament.tournament_id,),
        ).fetchone()[0]
        lease_count = connection.execute(
            "SELECT count(*) FROM tournament_runner_leases WHERE tournament_id = ?",
            (tournament.tournament_id,),
        ).fetchone()[0]
    assert status == "finalized"
    assert lease_count == 0
    assert counts_before == {
        "tournament_archives": 1,
        "matches": 6,
        "series_archives": 3,
        "rating_history": 24,
        "tournament_rating_snapshots": 6,
        "tournament_rating_contributions": 24,
    }

    repeated = store.finalize_tournament_checkpoint(tournament.tournament_id)

    assert not repeated.inserted and repeated.rated
    with sqlite3.connect(path) as connection:
        assert {
            table: connection.execute(
                f"SELECT count(*) FROM {table}"  # noqa: S608 - fixed allowlist
            ).fetchone()[0]
            for table in counts_before
        } == counts_before


def test_expired_lease_takeover_fences_every_stale_write(tmp_path) -> None:
    path = tmp_path / "expired-runner-lease.db"
    tournament = _tournament(tournament_id="expired-runner-lease")
    store = SQLiteStore(path)
    empty = _checkpoint(tournament)
    store.save_tournament_checkpoint(empty)
    stale = store.claim_tournament_runner(tournament.tournament_id).lease
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE tournament_runner_leases
            SET acquired_at_epoch = 0, renewed_at_epoch = 0, expires_at_epoch = 1
            WHERE tournament_id = ?
            """,
            (tournament.tournament_id,),
        )

    active = SQLiteStore(path, create=False).claim_tournament_runner(tournament.tournament_id).lease

    assert active.generation == stale.generation + 1
    with pytest.raises(TournamentRunnerLeaseLostError):
        store.renew_tournament_runner(stale)
    with pytest.raises(TournamentRunnerLeaseLostError):
        store.save_tournament_checkpoint(empty, lease=stale)
    assert not store.release_tournament_runner(stale)

    for completed_count in range(1, 4):
        store.save_tournament_checkpoint(
            _checkpoint(tournament, completed_count),
            lease=active,
        )
    with pytest.raises(TournamentRunnerLeaseLostError):
        store.finalize_tournament_checkpoint(tournament.tournament_id, lease=stale)

    renewed = store.renew_tournament_runner(active)
    finalized = store.finalize_tournament_checkpoint(
        tournament.tournament_id,
        lease=renewed,
    )
    assert finalized.inserted and finalized.rated
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT count(*) FROM tournament_runner_leases").fetchone()[0] == 0
        )


def test_checkpoint_and_frozen_provider_budget_are_created_atomically(tmp_path) -> None:
    path = tmp_path / "atomic-checkpoint-budget.db"
    tournament = _tournament(tournament_id="atomic-checkpoint-budget")
    checkpoint = _checkpoint(tournament)
    policy = _usage_policy(max_output_tokens_per_call=64)
    store = SQLiteStore(path)

    checkpoint_result, budget = store.create_tournament_checkpoint_with_provider_budget(
        checkpoint,
        "tournament-budget",
        BudgetLimits(calls=10, output=100),
        policy,
    )

    assert checkpoint_result.inserted
    assert budget.tournament_id == tournament.tournament_id
    assert budget.policy == policy
    repeated, repeated_budget = store.create_tournament_checkpoint_with_provider_budget(
        checkpoint,
        "tournament-budget",
        BudgetLimits(calls=10, output=100),
        policy,
    )
    assert not repeated.inserted
    assert repeated_budget == budget
    assert store.load_tournament_provider_budget(tournament.tournament_id) == budget


def test_v7_to_v8_migration_preserves_in_progress_checkpoint_and_active_lease(
    tmp_path,
) -> None:
    path = tmp_path / "v7-checkpoint-to-v8.db"
    tournament = _tournament(tournament_id="v7-checkpoint-to-v8")
    store = SQLiteStore(path)
    checkpoint = _checkpoint(tournament)
    store.save_tournament_checkpoint(checkpoint)
    lease = store.claim_tournament_runner(tournament.tournament_id).lease
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            DROP TABLE provider_call_attempts;
            DROP TABLE provider_budgets;
            PRAGMA user_version = 7;
            """
        )

    migrated = SQLiteStore(path, create=False)

    assert migrated.get_tournament_checkpoint(tournament.tournament_id) == checkpoint
    assert migrated.load_tournament_provider_budget(tournament.tournament_id) is None
    renewed = migrated.renew_tournament_runner(lease)
    assert renewed.generation == lease.generation
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_failed_atomic_checkpoint_budget_creation_rolls_back_both_rows(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "atomic-checkpoint-budget-rollback.db"
    tournament = _tournament(tournament_id="atomic-checkpoint-budget-rollback")
    store = SQLiteStore(path)

    def fail_budget_insert(*args, **kwargs):
        raise RuntimeError("injected budget insert failure")

    monkeypatch.setattr(store, "_insert_provider_budget_in_transaction", fail_budget_insert)
    with pytest.raises(RuntimeError, match="injected budget insert failure"):
        store.create_tournament_checkpoint_with_provider_budget(
            _checkpoint(tournament),
            "tournament-budget",
            BudgetLimits(calls=10),
            _usage_policy(),
        )

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM tournament_checkpoints").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM provider_budgets").fetchone()[0] == 0


def test_expired_lease_takeover_closes_old_generation_provider_attempts(tmp_path) -> None:
    path = tmp_path / "provider-ledger-takeover.db"
    tournament = _tournament(tournament_id="provider-ledger-takeover")
    store = SQLiteStore(path)
    store.save_tournament_checkpoint(_checkpoint(tournament))
    stale = store.claim_tournament_runner(tournament.tournament_id).lease
    store.create_provider_budget(
        "tournament-budget",
        BudgetLimits(calls=3, input=30, output=30),
        _usage_policy(),
        tournament_id=tournament.tournament_id,
        lease=stale,
    )
    dispatched, reserved = store.reserve_provider_call_batch(
        "tournament-budget",
        (
            CallBounds(input=4, output=5, route_id=USAGE_ROUTE_ID),
            CallBounds(input=7, output=8, route_id=USAGE_ROUTE_ID),
        ),
        attempt_ids=("old-dispatched", "old-reserved"),
        lease=stale,
    )
    store.mark_provider_call_dispatched(dispatched.attempt_id, lease=stale)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE tournament_runner_leases
            SET acquired_at_epoch = 0, renewed_at_epoch = 0, expires_at_epoch = 1
            WHERE tournament_id = ?
            """,
            (tournament.tournament_id,),
        )

    active = store.claim_tournament_runner(tournament.tournament_id).lease

    assert active.generation == stale.generation + 1
    old_dispatched = store.get_provider_call_attempt(dispatched.attempt_id)
    old_reserved = store.get_provider_call_attempt(reserved.attempt_id)
    assert old_dispatched is not None and old_dispatched.state == "charged_unknown"
    assert old_dispatched.charged == dispatched.bounds.as_totals()
    assert old_reserved is not None and old_reserved.state == "released_pre_dispatch"
    snapshot = store.load_provider_budget("tournament-budget")
    assert snapshot is not None
    assert snapshot.spent == dispatched.bounds.as_totals()
    assert snapshot.reserved == UsageTotals.zero()
    with pytest.raises(TournamentRunnerLeaseLostError):
        store.charge_provider_call_unknown(dispatched.attempt_id, lease=stale)

    (current,) = store.reserve_provider_call_batch(
        "tournament-budget",
        (CallBounds(route_id=USAGE_ROUTE_ID),),
        attempt_ids=("current",),
        lease=active,
    )
    assert current.runner_generation == active.generation
    store.release_provider_call_pre_dispatch(current.attempt_id, lease=active)


def test_checkpoint_finalize_rejects_pending_provider_attempt_and_seals_budget(
    tmp_path,
) -> None:
    path = tmp_path / "provider-ledger-finalize.db"
    tournament = _tournament(tournament_id="provider-ledger-finalize")
    store = SQLiteStore(path)
    store.save_tournament_checkpoint(_checkpoint(tournament))
    lease = store.claim_tournament_runner(tournament.tournament_id).lease
    store.create_provider_budget(
        "tournament-budget",
        BudgetLimits(calls=1, output=20),
        _usage_policy(max_output_tokens_per_call=20),
        tournament_id=tournament.tournament_id,
        lease=lease,
    )
    (attempt,) = store.reserve_provider_call_batch(
        "tournament-budget",
        (CallBounds(output=20, route_id=USAGE_ROUTE_ID),),
        attempt_ids=("pending",),
        lease=lease,
    )
    store.mark_provider_call_dispatched(attempt.attempt_id, lease=lease)
    for completed_count in range(1, 4):
        store.save_tournament_checkpoint(
            _checkpoint(tournament, completed_count),
            lease=lease,
        )

    with pytest.raises(ProviderBudgetPendingError, match="unresolved"):
        store.finalize_tournament_checkpoint(tournament.tournament_id, lease=lease)
    assert store.get_tournament(tournament.tournament_id) is None
    assert not store.load_provider_budget("tournament-budget").finalized

    store.charge_provider_call_unknown(attempt.attempt_id, lease=lease)
    result = store.finalize_tournament_checkpoint(
        tournament.tournament_id,
        lease=lease,
    )
    assert result.inserted
    budget = store.load_provider_budget("tournament-budget")
    assert budget is not None and budget.finalized
    assert budget.spent == UsageTotals(calls=1, input=0, output=20, estimated_cost=0)


def test_checkpoint_finalize_rejects_poisoned_provider_budget(tmp_path) -> None:
    path = tmp_path / "provider-ledger-poisoned-finalize.db"
    tournament = _tournament(tournament_id="provider-ledger-poison-finalize")
    store = SQLiteStore(path)
    store.save_tournament_checkpoint(_checkpoint(tournament))
    lease = store.claim_tournament_runner(tournament.tournament_id).lease
    store.create_provider_budget(
        "tournament-budget",
        BudgetLimits(calls=1, input=2),
        _usage_policy(),
        tournament_id=tournament.tournament_id,
        lease=lease,
    )
    (attempt,) = store.reserve_provider_call_batch(
        "tournament-budget",
        (CallBounds(input=2, route_id=USAGE_ROUTE_ID),),
        attempt_ids=("overrun",),
        lease=lease,
    )
    store.mark_provider_call_dispatched(attempt.attempt_id, lease=lease)
    with pytest.raises(UsageExceedsReservationError):
        store.settle_provider_call(
            attempt.attempt_id,
            UsageTotals(calls=1, input=4, output=0, estimated_cost=0),
            lease=lease,
        )
    for completed_count in range(1, 4):
        store.save_tournament_checkpoint(_checkpoint(tournament, completed_count), lease=lease)

    with pytest.raises(BudgetPoisonedError, match="poisoned"):
        store.finalize_tournament_checkpoint(tournament.tournament_id, lease=lease)


def test_expire_runner_leases_preserves_generation_for_next_claim(tmp_path) -> None:
    path = tmp_path / "expire-runner-lease.db"
    tournament = _tournament(tournament_id="expire-runner-lease")
    store = SQLiteStore(path)
    store.save_tournament_checkpoint(_checkpoint(tournament))
    first = store.claim_tournament_runner(tournament.tournament_id).lease
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE tournament_runner_leases
            SET acquired_at_epoch = 0, renewed_at_epoch = 0, expires_at_epoch = 1
            WHERE tournament_id = ?
            """,
            (tournament.tournament_id,),
        )

    assert store.expire_tournament_runner_leases() == 1
    assert store.expire_tournament_runner_leases() == 0
    with pytest.raises(TournamentRunnerLeaseLostError):
        store.renew_tournament_runner(first)
    second = store.claim_tournament_runner(tournament.tournament_id).lease
    assert second.generation == first.generation + 1
    assert store.release_tournament_runner(second)


def test_failed_checkpoint_append_rolls_back_lease_renewal(tmp_path) -> None:
    path = tmp_path / "lease-append-rollback.db"
    tournament = _tournament(tournament_id="lease-append-rollback")
    store = SQLiteStore(path)
    store.save_tournament_checkpoint(_checkpoint(tournament))
    lease = store.claim_tournament_runner(tournament.tournament_id).lease
    with sqlite3.connect(path) as connection:
        expires_before = connection.execute(
            "SELECT expires_at_epoch FROM tournament_runner_leases"
        ).fetchone()[0]

    with pytest.raises(TournamentCheckpointCollisionError, match="连续追加"):
        store.save_tournament_checkpoint(
            _checkpoint(tournament, 2),
            lease=lease,
            lease_seconds=3600,
        )

    with sqlite3.connect(path) as connection:
        expires_after = connection.execute(
            "SELECT expires_at_epoch FROM tournament_runner_leases"
        ).fetchone()[0]
    assert expires_after == expires_before
    assert store.release_tournament_runner(lease)


def test_checkpoint_append_is_idempotent_and_rejects_non_contiguous_progress(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "checkpoint-prefix.db")
    tournament = _tournament(tournament_id="prefix-checkpoint")
    empty = _checkpoint(tournament)

    assert store.save_tournament_checkpoint(empty).inserted
    with pytest.raises(TournamentRunnerLeaseLostError, match="有效的 runner lease"):
        store.save_tournament_checkpoint(_checkpoint(tournament, 1))
    lease = store.claim_tournament_runner(tournament.tournament_id).lease
    assert not store.save_tournament_checkpoint(empty, lease=lease).inserted
    with pytest.raises(TournamentCheckpointCollisionError, match="连续追加"):
        store.save_tournament_checkpoint(_checkpoint(tournament, 2), lease=lease)
    with pytest.raises(TournamentCheckpointCollisionError, match="另一份 checkpoint 配置"):
        store.save_tournament_checkpoint(
            _checkpoint(tournament, game_config={"rounds": 99}),
            lease=lease,
        )

    first = _checkpoint(tournament, 1)
    assert store.save_tournament_checkpoint(first, lease=lease).inserted
    assert not store.save_tournament_checkpoint(first, lease=lease).inserted
    with pytest.raises(MatchIdCollisionError, match="checkpoint 保留"):
        store.save_match(tournament.pairings[0].series.legs[0], rating_source="engine")
    with pytest.raises(SeriesIdCollisionError, match="checkpoint 保留"):
        store.save_series(tournament.pairings[0].series, rating_source="engine")
    reused_match_series = tournament.pairings[0].series.model_copy(
        update={"series_id": "different-series-with-reserved-match"}
    )
    with pytest.raises(MatchIdCollisionError, match="checkpoint 保留"):
        store.save_series(reused_match_series, rating_source="engine")
    with pytest.raises(TournamentIdCollisionError, match="checkpoint 保留"):
        store.save_tournament(tournament, rating_source="engine")
    assert store.get_tournament(tournament.tournament_id) is None
    assert store.leaderboard() == []
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM series_archives").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM matches").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 0


def test_incomplete_checkpoint_cannot_finalize(tmp_path) -> None:
    path = tmp_path / "incomplete-checkpoint.db"
    tournament = _tournament(tournament_id="incomplete-checkpoint")
    store = SQLiteStore(path)
    store.save_tournament_checkpoint(_checkpoint(tournament))
    lease = store.claim_tournament_runner(tournament.tournament_id).lease
    store.save_tournament_checkpoint(_checkpoint(tournament, 1), lease=lease)

    with pytest.raises(StorageError, match="尚未完成"):
        store.finalize_tournament_checkpoint(tournament.tournament_id, lease=lease)

    assert store.get_tournament(tournament.tournament_id) is None
    assert store.leaderboard() == []
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM matches").fetchone()[0] == 0
        assert (
            connection.execute("SELECT status FROM tournament_checkpoints").fetchone()[0]
            == "in_progress"
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM tournament_runner_leases WHERE token_digest IS NOT NULL"
            ).fetchone()[0]
            == 1
        )


def test_checkpoint_finalize_failure_rolls_back_archive_elo_and_status(tmp_path) -> None:
    class FailingStore(SQLiteStore):
        def _record_tournament_ratings(self, *args, **kwargs):
            super()._record_tournament_ratings(*args, **kwargs)
            raise RuntimeError("injected checkpoint finalize failure")

    path = tmp_path / "checkpoint-finalize-rollback.db"
    tournament = _tournament(tournament_id="rollback-checkpoint")
    store = FailingStore(path)
    store.save_tournament_checkpoint(_checkpoint(tournament))
    lease = store.claim_tournament_runner(tournament.tournament_id).lease
    for completed_count in range(1, 4):
        store.save_tournament_checkpoint(
            _checkpoint(tournament, completed_count),
            lease=lease,
        )

    with pytest.raises(RuntimeError, match="injected checkpoint finalize failure"):
        store.finalize_tournament_checkpoint(tournament.tournament_id, lease=lease)

    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT status FROM tournament_checkpoints").fetchone()[0]
            == "in_progress"
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM tournament_runner_leases WHERE token_digest IS NOT NULL"
            ).fetchone()[0]
            == 1
        )
        for table in (
            "tournament_archives",
            "tournament_entrants",
            "tournament_pairings",
            "series_archives",
            "matches",
            "rating_history",
            "tournament_rating_snapshots",
            "tournament_rating_contributions",
        ):
            assert (
                connection.execute(
                    f"SELECT count(*) FROM {table}"  # noqa: S608 - fixed allowlist
                ).fetchone()[0]
                == 0
            )

    recovered = SQLiteStore(path, create=False).finalize_tournament_checkpoint(
        tournament.tournament_id,
        lease=lease,
    )
    assert recovered.inserted and recovered.rated


def test_concurrent_checkpoint_finalize_rates_exactly_once(tmp_path) -> None:
    path = tmp_path / "concurrent-checkpoint-finalize.db"
    tournament = _tournament(tournament_id="concurrent-checkpoint")
    store = SQLiteStore(path)
    store.save_tournament_checkpoint(_checkpoint(tournament))
    lease = store.claim_tournament_runner(tournament.tournament_id).lease
    for completed_count in range(1, 4):
        store.save_tournament_checkpoint(
            _checkpoint(tournament, completed_count),
            lease=lease,
        )

    def finalize(_: int):
        return SQLiteStore(path, create=False).finalize_tournament_checkpoint(
            tournament.tournament_id,
            lease=lease,
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(finalize, range(4)))

    assert sum(result.inserted for result in results) == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 24


def test_checkpoint_finalize_freezes_ratings_at_finalization_time(tmp_path) -> None:
    path = tmp_path / "checkpoint-finalization-ratings.db"
    tournament = _tournament(tournament_id="finalization-ratings-checkpoint")
    store = SQLiteStore(path)
    store.save_tournament_checkpoint(_checkpoint(tournament))
    lease = store.claim_tournament_runner(tournament.tournament_id).lease
    for completed_count in range(1, 4):
        store.save_tournament_checkpoint(
            _checkpoint(tournament, completed_count),
            lease=lease,
        )

    store.save_match(
        _match(
            match_id="rated-between-checkpoint-and-finalize",
            seed=99,
            players=(_descriptor("A"), _descriptor("B")),
            winner="A",
            started_at=tournament.finished_at + timedelta(minutes=1),
            source="local_engine",
        ),
        rating_source="engine",
    )
    leaderboard_before_finalize = {entry.entrant_id: entry for entry in store.leaderboard()}
    ratings_before_finalize = {
        entrant_id: entry.rating for entrant_id, entry in leaderboard_before_finalize.items()
    }

    store.finalize_tournament_checkpoint(tournament.tournament_id, lease=lease)

    leaderboard_after_finalize = {entry.entrant_id: entry for entry in store.leaderboard()}
    for entrant_id, entry in leaderboard_before_finalize.items():
        assert leaderboard_after_finalize[entrant_id].updated_at >= entry.updated_at

    with sqlite3.connect(path) as connection:
        snapshot_rows = connection.execute(
            """
            SELECT entrant_id, rating_before
            FROM tournament_rating_snapshots
            WHERE tournament_id = ? AND rating_scope = 'overall' AND game = ''
            """,
            (tournament.tournament_id,),
        ).fetchall()
    assert dict(snapshot_rows) == pytest.approx(
        {
            descriptor["entrant_id"]: ratings_before_finalize.get(
                descriptor["entrant_id"],
                1500.0,
            )
            for descriptor in tournament.players
        }
    )


def test_corrupt_checkpoint_series_is_rejected_on_reopen(tmp_path) -> None:
    path = tmp_path / "corrupt-checkpoint.db"
    tournament = _tournament(tournament_id="corrupt-checkpoint")
    store = SQLiteStore(path)
    store.save_tournament_checkpoint(_checkpoint(tournament))
    lease = store.claim_tournament_runner(tournament.tournament_id).lease
    store.save_tournament_checkpoint(_checkpoint(tournament, 1), lease=lease)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE tournament_checkpoint_series
            SET series_json = '{"broken":true}'
            WHERE tournament_id = ?
            """,
            (tournament.tournament_id,),
        )

    with pytest.raises(StorageError, match="checkpoint 已损坏"):
        SQLiteStore(path, create=False).get_tournament_checkpoint(tournament.tournament_id)


def test_tournament_and_owned_children_are_idempotent_and_deeply_verified(tmp_path) -> None:
    path = tmp_path / "idempotent-tournament.db"
    tournament = _tournament()
    store = SQLiteStore(path)
    store.save_tournament(tournament, rating_source="engine")

    assert not store.save_tournament(tournament, rating_source="engine").inserted
    assert not store.save_series(
        tournament.pairings[0].series,
        rating_source="engine",
    ).inserted
    assert not store.save_match(
        tournament.pairings[0].series.legs[0],
        rating_source="engine",
    ).inserted

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE tournament_rating_contributions
            SET rating_delta = rating_delta + 1
            WHERE tournament_id = ? AND sequence = 6
            """,
            (tournament.tournament_id,),
        )

    with pytest.raises(StorageError, match="ELO 历史已损坏"):
        store.save_match(tournament.pairings[0].series.legs[0])


def test_tournament_resave_verifies_latest_materialized_rating(tmp_path) -> None:
    path = tmp_path / "tampered-tournament-rating.db"
    tournament = _tournament()
    store = SQLiteStore(path)
    store.save_tournament(tournament, rating_source="engine")

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE ratings
            SET rating = rating + 5
            WHERE rating_scope = 'overall' AND game = '' AND entrant_id = 'test:A'
            """
        )

    with pytest.raises(StorageError, match="ELO 排行榜已损坏"):
        store.save_tournament(tournament, rating_source="engine")


def test_tournament_resave_rejects_malformed_materialized_timestamp(tmp_path) -> None:
    path = tmp_path / "malformed-tournament-rating-time.db"
    tournament = _tournament()
    store = SQLiteStore(path)
    store.save_tournament(tournament, rating_source="engine")

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE ratings
            SET updated_at = 'not-a-timestamp'
            WHERE rating_scope = 'overall' AND game = '' AND entrant_id = 'test:A'
            """
        )

    with pytest.raises(StorageError, match="ELO 排行榜已损坏"):
        store.save_tournament(tournament, rating_source="engine")


def test_historical_tournament_resave_rejects_tampered_materialized_counts(
    tmp_path,
) -> None:
    path = tmp_path / "tampered-tournament-rating-count.db"
    tournament = _tournament()
    store = SQLiteStore(path)
    store.save_tournament(tournament, rating_source="engine")
    store.save_match(
        _match(
            match_id="later-before-count-tamper",
            seed=100,
            players=(_descriptor("A"), _descriptor("B")),
            winner="B",
            started_at=tournament.finished_at + timedelta(minutes=1),
            source="local_engine",
        ),
        rating_source="engine",
    )

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE ratings
            SET games_played = games_played + 1
            WHERE rating_scope = 'overall' AND game = '' AND entrant_id = 'test:A'
            """
        )

    with pytest.raises(StorageError, match="ELO 排行榜已损坏"):
        store.save_tournament(tournament, rating_source="engine")


def test_historical_tournament_resave_allows_later_materialized_rating(tmp_path) -> None:
    path = tmp_path / "historical-tournament-rating.db"
    tournament = _tournament()
    store = SQLiteStore(path)
    store.save_tournament(tournament, rating_source="engine")
    later_match = _match(
        match_id="later-standalone",
        seed=99,
        players=(_descriptor("A"), _descriptor("B")),
        winner="B",
        started_at=tournament.finished_at - timedelta(seconds=1),
        source="local_engine",
    )

    store.save_match(later_match, rating_source="engine")

    result = store.save_tournament(tournament, rating_source="engine")
    assert not result.inserted
    assert result.rated


def test_historical_tournament_resave_allows_same_finished_at_later_tournament(
    tmp_path,
) -> None:
    path = tmp_path / "same-finished-at-tournaments.db"
    first = _tournament(tournament_id="same-time-first")
    later = _tournament(
        tournament_id="same-time-later",
        reverse_winners=True,
    )
    entrant_last_match = max(
        leg.finished_at
        for pairing in later.pairings
        for leg in pairing.series.legs
        if any(player["entrant_id"] == "test:A" for player in leg.players)
    )
    assert later.finished_at == first.finished_at
    assert entrant_last_match < later.finished_at
    store = SQLiteStore(path)
    store.save_tournament(first, rating_source="engine")
    first_rating = {entry.entrant_id: entry.rating for entry in store.leaderboard()}["test:A"]

    store.save_tournament(later, rating_source="engine")
    later_rating = {entry.entrant_id: entry.rating for entry in store.leaderboard()}["test:A"]
    assert later_rating != first_rating

    result = store.save_tournament(first, rating_source="engine")
    assert not result.inserted
    assert result.rated


def test_tournament_id_collision_and_preexisting_child_are_rejected(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "tournament-collision.db")
    tournament = _tournament()
    store.save_tournament(tournament, rating_source="engine")
    changed = _tournament(
        tournament_id=tournament.tournament_id,
        reverse_winners=True,
    )

    with pytest.raises(TournamentIdCollisionError):
        store.save_tournament(changed, rating_source="engine")

    other = _tournament(tournament_id="other-tournament")
    fresh_store = SQLiteStore(tmp_path / "child-collision.db")
    fresh_store.save_match(other.pairings[0].series.legs[0], rating_source="engine")
    with pytest.raises(MatchIdCollisionError, match="不能重复归入循环赛"):
        fresh_store.save_tournament(other, rating_source="engine")
    assert fresh_store.get_tournament(other.tournament_id) is None


def test_imported_tournament_is_unrated_and_cannot_be_upgraded(tmp_path) -> None:
    path = tmp_path / "imported-tournament.db"
    tournament = _tournament()
    store = SQLiteStore(path)

    result = store.save_tournament(tournament)

    assert result.inserted and not result.rated
    assert store.leaderboard() == []
    assert not store.save_series(tournament.pairings[0].series).rated
    with pytest.raises(TournamentIdCollisionError, match="不能通过幂等重存升级"):
        store.save_tournament(tournament, rating_source="engine")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 0
        assert (
            connection.execute("SELECT count(*) FROM tournament_rating_contributions").fetchone()[0]
            == 0
        )


def test_tournament_failure_rolls_back_every_child_and_rating(tmp_path) -> None:
    class FailingStore(SQLiteStore):
        def _record_tournament_ratings(self, *args, **kwargs):
            super()._record_tournament_ratings(*args, **kwargs)
            raise RuntimeError("injected tournament failure")

    path = tmp_path / "tournament-rollback.db"
    store = FailingStore(path)

    with pytest.raises(RuntimeError, match="injected tournament failure"):
        store.save_tournament(_tournament(), rating_source="engine")

    assert store.get_tournament("tournament-1") is None
    assert store.list_matches() == []
    assert store.leaderboard() == []
    with sqlite3.connect(path) as connection:
        for count_query in (
            "SELECT count(*) FROM tournament_archives",
            "SELECT count(*) FROM tournament_entrants",
            "SELECT count(*) FROM tournament_pairings",
            "SELECT count(*) FROM series_archives",
            "SELECT count(*) FROM matches",
            "SELECT count(*) FROM rating_history",
            "SELECT count(*) FROM tournament_rating_snapshots",
            "SELECT count(*) FROM tournament_rating_contributions",
            "SELECT count(*) FROM rating_operations",
        ):
            assert connection.execute(count_query).fetchone()[0] == 0


def test_external_tournament_cannot_be_rated(tmp_path) -> None:
    tournament = _tournament(source="external")
    store = SQLiteStore(tmp_path / "external-tournament.db")

    result = store.save_tournament(tournament, rating_source="engine")

    assert result.inserted and not result.rated
    assert store.leaderboard() == []


def test_frozen_tournament_elo_is_independent_of_pairing_execution_order(tmp_path) -> None:
    first_store = SQLiteStore(tmp_path / "first-order.db")
    second_store = SQLiteStore(tmp_path / "second-order.db")
    _seed_unequal_ratings(first_store)
    _seed_unequal_ratings(second_store)
    assert len({entry.rating for entry in first_store.leaderboard()}) > 1
    first_store.save_tournament(
        _tournament(tournament_id="first-order"),
        rating_source="engine",
    )
    second_store.save_tournament(
        _tournament(
            tournament_id="second-order",
            names=("C", "B", "A"),
        ),
        rating_source="engine",
    )

    first_ratings = {entry.entrant_id: entry.rating for entry in first_store.leaderboard()}
    second_ratings = {entry.entrant_id: entry.rating for entry in second_store.leaderboard()}
    assert first_ratings == pytest.approx(second_ratings)


def test_concurrent_duplicate_tournament_saves_rate_exactly_once(tmp_path) -> None:
    path = tmp_path / "concurrent-tournament.db"
    tournament = _tournament()

    def save(_: int):
        return SQLiteStore(path).save_tournament(
            tournament,
            rating_source="engine",
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(save, range(4)))

    assert sum(result.inserted for result in results) == 1
    store = SQLiteStore(path)
    assert all(entry.games_played == 4 for entry in store.leaderboard())
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 24


def test_v5_database_migrates_to_v6_without_rewriting_checkpoint(tmp_path) -> None:
    path = tmp_path / "migrate-v5.db"
    tournament = _tournament(tournament_id="before-v6")
    store = SQLiteStore(path)
    store.save_tournament_checkpoint(_checkpoint(tournament))
    lease = store.claim_tournament_runner(tournament.tournament_id).lease
    checkpoint = _checkpoint(tournament, 1)
    store.save_tournament_checkpoint(checkpoint, lease=lease)
    assert store.release_tournament_runner(lease)
    with sqlite3.connect(path) as connection:
        config_before = connection.execute(
            "SELECT config_json FROM tournament_checkpoints"
        ).fetchone()[0]
        prefix_before = connection.execute(
            """
            SELECT pairing_number, series_id, match_1_id, match_2_id,
                   completed_at, series_json
            FROM tournament_checkpoint_series
            ORDER BY pairing_number
            """
        ).fetchall()
        connection.executescript(
            """
            DROP TABLE tournament_runner_leases;
            PRAGMA user_version = 5;
            """
        )

    migrated = SQLiteStore(path, create=False)

    loaded = migrated.get_tournament_checkpoint(tournament.tournament_id)
    assert loaded is not None
    assert loaded.model_dump(mode="json") == checkpoint.model_dump(mode="json")
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert (
            connection.execute("SELECT config_json FROM tournament_checkpoints").fetchone()[0]
            == config_before
        )
        assert (
            connection.execute(
                """
                SELECT pairing_number, series_id, match_1_id, match_2_id,
                       completed_at, series_json
                FROM tournament_checkpoint_series
                ORDER BY pairing_number
                """
            ).fetchall()
            == prefix_before
        )
        assert (
            connection.execute("SELECT count(*) FROM tournament_runner_leases").fetchone()[0] == 0
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_failed_v5_to_v6_migration_rolls_back_schema_and_version(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "failed-v5-migration.db"
    SQLiteStore(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            DROP TABLE tournament_runner_leases;
            PRAGMA user_version = 5;
            """
        )
    original_create = SQLiteStore._create_runner_lease_schema

    def fail_after_create(connection: sqlite3.Connection) -> None:
        original_create(connection)
        raise RuntimeError("injected runner lease migration failure")

    monkeypatch.setattr(
        SQLiteStore,
        "_create_runner_lease_schema",
        staticmethod(fail_after_create),
    )

    with pytest.raises(RuntimeError, match="injected runner lease migration failure"):
        SQLiteStore(path, create=False)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert (
            connection.execute(
                """
                SELECT count(*) FROM sqlite_master
                WHERE type = 'table' AND name = 'tournament_runner_leases'
                """
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    ("lease_schema", "extra_schema", "message"),
    (
        (
            """
            CREATE TABLE tournament_runner_leases (
                tournament_id TEXT UNIQUE REFERENCES tournament_checkpoints(tournament_id)
                    ON DELETE RESTRICT,
                generation INTEGER NOT NULL,
                token_digest BLOB UNIQUE,
                acquired_at_epoch INTEGER,
                renewed_at_epoch INTEGER,
                expires_at_epoch INTEGER
            )
            """,
            None,
            "column definitions",
        ),
        (
            """
            CREATE TABLE tournament_runner_leases (
                tournament_id TEXT PRIMARY KEY,
                generation INTEGER NOT NULL,
                token_digest BLOB UNIQUE,
                acquired_at_epoch INTEGER,
                renewed_at_epoch INTEGER,
                expires_at_epoch INTEGER
            )
            """,
            None,
            "foreign keys",
        ),
        (
            """
            CREATE TABLE tournament_runner_leases (
                tournament_id TEXT PRIMARY KEY
                    REFERENCES tournament_checkpoints(tournament_id) ON DELETE RESTRICT,
                generation INTEGER NOT NULL,
                token_digest BLOB,
                acquired_at_epoch INTEGER,
                renewed_at_epoch INTEGER,
                expires_at_epoch INTEGER
            )
            """,
            None,
            "unique constraints",
        ),
        (
            """
            CREATE TABLE tournament_runner_leases (
                tournament_id TEXT PRIMARY KEY
                    REFERENCES tournament_checkpoints(tournament_id) ON DELETE RESTRICT,
                generation INTEGER NOT NULL,
                token_digest BLOB,
                acquired_at_epoch INTEGER,
                renewed_at_epoch INTEGER,
                expires_at_epoch INTEGER
            )
            """,
            """
            CREATE UNIQUE INDEX partial_runner_token_unique
            ON tournament_runner_leases(token_digest)
            WHERE token_digest IS NULL
            """,
            "unique constraints",
        ),
    ),
)
def test_v7_manifest_rejects_runner_lease_table_without_fencing_constraints(
    tmp_path,
    lease_schema: str,
    extra_schema: str | None,
    message: str,
) -> None:
    path = tmp_path / "invalid-runner-lease-schema.db"
    SQLiteStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE tournament_runner_leases")
        connection.execute(lease_schema)
        if extra_schema is not None:
            connection.execute(extra_schema)

    with pytest.raises(StorageError, match=message):
        SQLiteStore(path, create=False)


def test_v3_database_migrates_to_v6_without_rewriting_existing_data(tmp_path) -> None:
    path = tmp_path / "migrate-v3.db"
    store = SQLiteStore(path)
    archive = _match(
        match_id="before-v4",
        seed=42,
        players=(_descriptor("A"), _descriptor("B")),
        winner="A",
        started_at=STARTED,
        source="local_engine",
    )
    store.save_match(archive, rating_source="engine")
    with sqlite3.connect(path) as connection:
        raw_archive = connection.execute(
            "SELECT archive_json FROM matches WHERE match_id = ?",
            (archive.match_id,),
        ).fetchone()[0]
        ratings_before = connection.execute(
            "SELECT * FROM ratings ORDER BY rating_scope, game, entrant_id"
        ).fetchall()
        history_before = connection.execute(
            """
            SELECT * FROM rating_history
            ORDER BY match_id, rating_scope, game, entrant_id
            """
        ).fetchall()
        connection.executescript(
            """
            DROP TABLE tournament_runner_leases;
            DROP TABLE tournament_checkpoint_series;
            DROP TABLE tournament_checkpoints;
            DROP TABLE tournament_rating_contributions;
            DROP TABLE tournament_rating_snapshots;
            DROP TABLE tournament_pairings;
            DROP TABLE tournament_entrants;
            DROP TABLE tournament_archives;
            PRAGMA user_version = 3;
            """
        )

    migrated = SQLiteStore(path, create=False)

    assert migrated.get_match(archive.match_id) is not None
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert (
            connection.execute(
                "SELECT archive_json FROM matches WHERE match_id = ?",
                (archive.match_id,),
            ).fetchone()[0]
            == raw_archive
        )
        assert (
            connection.execute(
                "SELECT * FROM ratings ORDER BY rating_scope, game, entrant_id"
            ).fetchall()
            == ratings_before
        )
        assert (
            connection.execute(
                """
            SELECT * FROM rating_history
            ORDER BY match_id, rating_scope, game, entrant_id
            """
            ).fetchall()
            == history_before
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v4_database_migrates_to_v6_without_rewriting_tournament_or_elo(
    tmp_path,
) -> None:
    path = tmp_path / "migrate-v4.db"
    tournament = _tournament(tournament_id="before-v5")
    SQLiteStore(path).save_tournament(tournament, rating_source="engine")
    with sqlite3.connect(path) as connection:
        tournament_json = connection.execute(
            "SELECT tournament_json FROM tournament_archives"
        ).fetchone()[0]
        ratings_before = connection.execute(
            "SELECT * FROM ratings ORDER BY rating_scope, game, entrant_id"
        ).fetchall()
        history_before = connection.execute(
            """
            SELECT * FROM rating_history
            ORDER BY match_id, rating_scope, game, entrant_id
            """
        ).fetchall()
        connection.executescript(
            """
            DROP TABLE tournament_runner_leases;
            DROP TABLE tournament_checkpoint_series;
            DROP TABLE tournament_checkpoints;
            PRAGMA user_version = 4;
            """
        )

    migrated = SQLiteStore(path, create=False)

    assert migrated.get_tournament(tournament.tournament_id) is not None
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert (
            connection.execute("SELECT tournament_json FROM tournament_archives").fetchone()[0]
            == tournament_json
        )
        assert (
            connection.execute(
                "SELECT * FROM ratings ORDER BY rating_scope, game, entrant_id"
            ).fetchall()
            == ratings_before
        )
        assert (
            connection.execute(
                """
            SELECT * FROM rating_history
            ORDER BY match_id, rating_scope, game, entrant_id
            """
            ).fetchall()
            == history_before
        )
        assert connection.execute("SELECT count(*) FROM tournament_checkpoints").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_failed_v4_to_v6_migration_rolls_back_schema_and_version(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "failed-v4-migration.db"
    SQLiteStore(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            DROP TABLE tournament_runner_leases;
            DROP TABLE tournament_checkpoint_series;
            DROP TABLE tournament_checkpoints;
            PRAGMA user_version = 4;
            """
        )
    original_create = SQLiteStore._create_checkpoint_schema

    def fail_after_create(connection: sqlite3.Connection) -> None:
        original_create(connection)
        raise RuntimeError("injected checkpoint migration failure")

    monkeypatch.setattr(
        SQLiteStore,
        "_create_checkpoint_schema",
        staticmethod(fail_after_create),
    )

    with pytest.raises(RuntimeError, match="injected checkpoint migration failure"):
        SQLiteStore(path, create=False)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        for table in ("tournament_checkpoints", "tournament_checkpoint_series"):
            assert (
                connection.execute(
                    """
                SELECT count(*) FROM sqlite_master
                WHERE type = 'table' AND name = ?
                """,
                    (table,),
                ).fetchone()[0]
                == 0
            )


def test_failed_v3_to_v6_migration_rolls_back_schema_and_version(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "failed-v3-migration.db"
    SQLiteStore(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            DROP TABLE tournament_runner_leases;
            DROP TABLE tournament_checkpoint_series;
            DROP TABLE tournament_checkpoints;
            DROP TABLE tournament_rating_contributions;
            DROP TABLE tournament_rating_snapshots;
            DROP TABLE tournament_pairings;
            DROP TABLE tournament_entrants;
            DROP TABLE tournament_archives;
            PRAGMA user_version = 3;
            """
        )
    original_create = SQLiteStore._create_tournament_schema

    def fail_after_create(connection: sqlite3.Connection) -> None:
        original_create(connection)
        raise RuntimeError("injected v6 migration failure")

    monkeypatch.setattr(
        SQLiteStore,
        "_create_tournament_schema",
        staticmethod(fail_after_create),
    )

    with pytest.raises(RuntimeError, match="injected v6 migration failure"):
        SQLiteStore(path, create=False)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert (
            connection.execute(
                """
                SELECT count(*) FROM sqlite_master
                WHERE type = 'table' AND name = 'tournament_archives'
                """
            ).fetchone()[0]
            == 0
        )


def test_opening_v6_database_rejects_broken_foreign_keys(tmp_path) -> None:
    path = tmp_path / "broken-v6-foreign-key.db"
    tournament = _tournament()
    SQLiteStore(path).save_tournament(tournament, rating_source="engine")
    contribution = tournament.pairings[0].series.legs[0]

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            DELETE FROM rating_history
            WHERE match_id = ? AND rating_scope = 'overall'
              AND game = '' AND entrant_id = 'test:A'
            """,
            (contribution.match_id,),
        )

    with pytest.raises(StorageError, match="外键完整性检查失败"):
        SQLiteStore(path, create=False)
