"""单淘汰制锦标赛核心编排与持久化测试。"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from llmolympic.core.championship import (
    CHAMPIONSHIP_SCHEMA_VERSION,
    championship_from_series,
    championship_round_count,
    championship_round_seed,
    play_championship,
    validate_championship_player_count,
)
from llmolympic.core.player import LLMPlayer
from llmolympic.core.storage import SeriesIdCollisionError, SQLiteStore
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
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
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
