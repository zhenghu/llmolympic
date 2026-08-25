"""单淘汰制锦标赛核心编排与持久化测试。"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from llmolympic.core.championship import (
    CHAMPIONSHIP_CHECKPOINT_SCHEMA_VERSION,
    CHAMPIONSHIP_SCHEMA_VERSION,
    championship_checkpoint_with_series,
    championship_from_series,
    championship_round_count,
    championship_round_seed,
    play_championship,
    prepare_championship,
    resume_championship,
    validate_championship_player_count,
)
from llmolympic.core.player import LLMPlayer
from llmolympic.core.storage import (
    SCHEMA_VERSION,
    ChampionshipRunnerLeaseBusyError,
    SeriesIdCollisionError,
    SQLiteStore,
)
from llmolympic.games import create_game
from llmolympic.providers.mock import MockProvider


def _players(count: int) -> list[LLMPlayer]:
    return [
        LLMPlayer(name=f"p{index}", provider=MockProvider("random"), model="x")
        for index in range(count)
    ]


def test_player_count_must_be_power_of_two() -> None:
    for count in (4, 8, 16):
        validate_championship_player_count(count)
    for count in (1, 2, 3, 5, 6, 7, 9, 17):
        with pytest.raises(ValueError):
            validate_championship_player_count(count)


def test_round_count_and_seed_derivation() -> None:
    assert championship_round_count(4) == 2
    assert championship_round_count(8) == 3
    assert championship_round_count(16) == 4
    assert championship_round_seed(42, 1) != championship_round_seed(42, 2)
    assert championship_round_seed(42, 1) == championship_round_seed(42, 1)


def test_four_player_championship_converges_to_one_champion() -> None:
    game = create_game("knowledge_quiz", rounds=2)
    players = _players(4)

    archive = asyncio.run(play_championship(game, players, seed=1))

    assert archive.schema_version == CHAMPIONSHIP_SCHEMA_VERSION
    assert archive.format == "single_elimination_two_leg"
    assert len(archive.pairings) == 3  # 2 in round 1 + 1 final
    names = [descriptor["name"] for descriptor in archive.players]
    assert archive.champion in names
    ranks = {standing.player: standing.rank for standing in archive.standings}
    assert ranks[archive.champion] == 1
    assert set(ranks.values()) == {1, 2, 3}
    # The champion played exactly championship_round_count(4) == 2 series.
    champion_standing = next(s for s in archive.standings if s.rank == 1)
    assert champion_standing.series_played == 2
    assert champion_standing.games_played == 4


def test_eight_player_all_draws_uses_deterministic_tiebreak() -> None:
    game = create_game("knowledge_quiz", rounds=1)
    # mock:fixed always answers the same choice, producing 1-1 series draws.
    players = [
        LLMPlayer(name=f"p{index}", provider=MockProvider("fixed"), model="x")
        for index in range(8)
    ]

    archive = asyncio.run(play_championship(game, players, seed=7))

    assert len(archive.pairings) == 7
    for pairing in archive.pairings:
        assert pairing.series.points[pairing.series.players[0]["name"]] == 1.0
        assert pairing.series.points[pairing.series.players[1]["name"]] == 1.0
    # Deterministic entrant_id tie-break still converges.
    assert archive.champion == "p0"


def test_championship_save_is_unrated_and_idempotent(tmp_path) -> None:
    game = create_game("knowledge_quiz", rounds=1)
    players = _players(4)
    archive = asyncio.run(play_championship(game, players, seed=3))

    store = SQLiteStore(tmp_path / "champ.db")
    first = store.save_championship(archive, rating_source="engine")
    assert first.inserted and not first.rated
    assert store.leaderboard() == []

    repeated = store.save_championship(archive, rating_source="engine")
    assert not repeated.inserted

    loaded = store.get_championship(archive.championship_id)
    assert loaded is not None
    assert loaded.model_dump(mode="json") == archive.model_dump(mode="json")

    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert (
            connection.execute(
                "SELECT count(*) FROM championship_archives WHERE rated = 1"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 0


def test_championship_child_series_cannot_be_reused_elsewhere(tmp_path) -> None:
    game = create_game("knowledge_quiz", rounds=1)
    players = _players(4)
    archive = asyncio.run(play_championship(game, players, seed=5))

    store = SQLiteStore(tmp_path / "champ-child.db")
    store.save_championship(archive, rating_source="engine")

    with pytest.raises(SeriesIdCollisionError):
        store.save_series(archive.pairings[0].series, rating_source="engine")


def test_championship_archive_rejects_wrong_champion() -> None:
    game = create_game("knowledge_quiz", rounds=1)
    players = _players(4)
    archive = asyncio.run(play_championship(game, players, seed=9))
    wrong = next(
        descriptor["name"]
        for descriptor in archive.players
        if descriptor["name"] != archive.champion
    )

    with pytest.raises(ValueError, match="champion"):
        championship_from_series(
            archive.players,
            [pairing.series for pairing in archive.pairings],
            seed=archive.seed,
            champion=wrong,
        )


def test_malformed_series_without_legs_reports_validation_error() -> None:
    game = create_game("knowledge_quiz", rounds=1)
    players = _players(4)
    archive = asyncio.run(play_championship(game, players, seed=11))
    series = [pairing.series for pairing in archive.pairings]

    empty_legs = series[0].model_copy(deep=True)
    empty_legs.legs = ()

    with pytest.raises(ValueError):
        championship_from_series(
            archive.players,
            [empty_legs, *series[1:]],
            seed=archive.seed,
            champion=archive.champion,
        )


def test_malformed_leg_without_events_reports_validation_error() -> None:
    game = create_game("knowledge_quiz", rounds=1)
    players = _players(4)
    archive = asyncio.run(play_championship(game, players, seed=13))
    series = [pairing.series for pairing in archive.pairings]

    empty_events = series[0].model_copy(deep=True)
    empty_events.legs[0].events = []

    with pytest.raises(ValueError):
        championship_from_series(
            archive.players,
            [empty_events, *series[1:]],
            seed=archive.seed,
            champion=archive.champion,
        )


def test_prepare_championship_freezes_identity_and_empty_progress() -> None:
    game = create_game("knowledge_quiz", rounds=2)
    players = _players(4)

    checkpoint = prepare_championship(
        game,
        players,
        seed=9,
        max_attempts=2,
        championship_id="champ-checkpoint-1",
    )

    assert checkpoint.schema_version == CHAMPIONSHIP_CHECKPOINT_SCHEMA_VERSION
    assert checkpoint.championship_id == "champ-checkpoint-1"
    assert checkpoint.game == "knowledge_quiz"
    assert checkpoint.seed == 9
    assert checkpoint.max_attempts == 2
    assert len(checkpoint.players) == 4
    assert len(checkpoint.schedule) == 3
    assert checkpoint.completed_series == ()
    assert not checkpoint.is_complete
    assert checkpoint.completed_rounds == 0
    assert checkpoint.next_pairing_number == 1


def test_play_championship_checkpoint_callback_captures_rounds() -> None:
    game = create_game("knowledge_quiz", rounds=1)
    players = _players(4)
    checkpoints: list[object] = []

    archive = asyncio.run(
        play_championship(game, players, seed=5, on_checkpoint=checkpoints.append)
    )

    assert len(checkpoints) == 2  # one per round for a 4-player bracket
    assert checkpoints[0].completed_rounds == 1
    assert checkpoints[1].completed_rounds == 2
    assert checkpoints[1].is_complete
    assert archive.championship_id == checkpoints[1].championship_id


def test_resume_championship_skips_completed_rounds() -> None:
    game = create_game("knowledge_quiz", rounds=1)
    players = _players(4)

    checkpoint = prepare_championship(
        game,
        players,
        seed=3,
        championship_id="resume-champ-1",
    )
    # Play only the opening round, then fold it into the checkpoint.
    from llmolympic.core.series import play_two_leg_series

    first_round_series = []
    for index in (0, 1):
        first_round_series.append(
            asyncio.run(
                play_two_leg_series(
                    game,
                    [players[2 * index], players[2 * index + 1]],
                    seed=championship_round_seed(3, 1),
                )
            )
        )
    checkpoint = championship_checkpoint_with_series(checkpoint, first_round_series)

    events: list[tuple[int, int]] = []
    checkpoints: list[object] = []

    resumed = asyncio.run(
        resume_championship(
            game,
            players,
            checkpoint,
            on_event=lambda pairing, leg, event: events.append((pairing, leg)),
            on_checkpoint=checkpoints.append,
        )
    )

    # Only the final round's pairing (pairing number 3) is replayed.
    assert all(pairing == 3 for pairing, _ in events)
    assert resumed.championship_id == "resume-champ-1"
    assert [c.completed_rounds for c in checkpoints] == [2]
    assert len(resumed.pairings) == 3


def test_championship_checkpoint_storage_round_trip_and_lease(tmp_path) -> None:
    game = create_game("knowledge_quiz", rounds=1)
    players = _players(4)

    store = SQLiteStore(tmp_path / "champ-checkpoint.db")
    checkpoint = prepare_championship(
        game,
        players,
        seed=7,
        championship_id="champ-checkpoint-1",
    )
    created = store.save_championship_checkpoint(checkpoint)
    assert created.inserted
    assert created.completed_pairing_count == 0
    assert created.pairing_count == 3

    lease = store.claim_championship_runner("champ-checkpoint-1").lease
    with pytest.raises(ChampionshipRunnerLeaseBusyError):
        store.claim_championship_runner("champ-checkpoint-1")

    # Run the full championship, persisting each whole round under the lease.
    def save(updated: object) -> None:
        store.save_championship_checkpoint(updated, lease=lease)

    archive = asyncio.run(
        resume_championship(
            game,
            players,
            checkpoint,
            on_checkpoint=save,
        )
    )
    assert archive.championship_id == "champ-checkpoint-1"

    loaded = store.get_championship_checkpoint("champ-checkpoint-1")
    assert loaded is not None
    assert loaded.is_complete
    assert len(loaded.completed_series) == 3

    # The checkpoint owner blocks reusing its child series elsewhere.
    with pytest.raises(SeriesIdCollisionError):
        store.save_series(loaded.completed_series[0], rating_source="engine")

    result = store.finalize_championship_checkpoint("champ-checkpoint-1", lease=lease)
    assert result.inserted and not result.rated

    formal = store.get_championship("champ-checkpoint-1")
    assert formal is not None
    assert formal.model_dump(mode="json") == archive.model_dump(mode="json")
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert (
            connection.execute(
                "SELECT status FROM championship_checkpoints"
            ).fetchone()[0]
            == "finalized"
        )
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 0
        assert store.leaderboard() == []


def test_championship_checkpoint_rejects_partial_round(tmp_path) -> None:
    game = create_game("knowledge_quiz", rounds=1)
    players = _players(4)

    store = SQLiteStore(tmp_path / "champ-partial.db")
    checkpoint = prepare_championship(
        game,
        players,
        seed=11,
        championship_id="champ-partial-1",
    )
    store.save_championship_checkpoint(checkpoint)
    lease = store.claim_championship_runner("champ-partial-1").lease

    # A single series is not a whole round; storage must reject it.
    from llmolympic.core.series import play_two_leg_series

    single = asyncio.run(
        play_two_leg_series(
            game,
            [players[0], players[1]],
            seed=championship_round_seed(11, 1),
        )
    )
    partial = championship_checkpoint_with_series(checkpoint, [single])
    with pytest.raises(Exception):  # noqa: B017 - validation raises StorageError subclasses
        store.save_championship_checkpoint(partial, lease=lease)


def test_eight_player_championship_checkpoint_per_round(tmp_path) -> None:
    game = create_game("knowledge_quiz", rounds=1)
    players = _players(8)

    store = SQLiteStore(tmp_path / "champ-8.db")
    checkpoint = prepare_championship(
        game,
        players,
        seed=23,
        championship_id="champ-8-player-1",
    )
    store.save_championship_checkpoint(checkpoint)
    lease = store.claim_championship_runner("champ-8-player-1").lease

    # Each round shrink the pairing count: 4 -> 2 -> 1.  Persisting every round
    # under the lease must succeed for all three round sizes.
    saved_rounds: list[int] = []

    def save(updated: object) -> None:
        store.save_championship_checkpoint(updated, lease=lease)
        saved_rounds.append(updated.completed_rounds)

    archive = asyncio.run(
        resume_championship(
            game,
            players,
            checkpoint,
            on_checkpoint=save,
        )
    )
    assert archive.championship_id == "champ-8-player-1"
    assert archive.champion in [descriptor["name"] for descriptor in archive.players]
    assert saved_rounds == [1, 2, 3]

    loaded = store.get_championship_checkpoint("champ-8-player-1")
    assert loaded is not None
    assert loaded.is_complete
    assert len(loaded.completed_series) == 7

    result = store.finalize_championship_checkpoint("champ-8-player-1", lease=lease)
    assert result.inserted and not result.rated
    formal = store.get_championship("champ-8-player-1")
    assert formal is not None
    assert formal.model_dump(mode="json") == archive.model_dump(mode="json")

