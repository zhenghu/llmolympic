"""新题目型项目的 CLI、双局赛、存档与 ELO 集成测试。"""

from __future__ import annotations

import sqlite3

import pytest
from typer.testing import CliRunner

from llmolympic.cli.main import app
from llmolympic.core.events import EventType
from llmolympic.core.storage import SQLiteStore

runner = CliRunner()


@pytest.mark.parametrize("game", ["reasoning_quiz", "riddle_quiz"])
def test_new_quiz_play_persists_and_updates_project_elo(tmp_path, game: str) -> None:
    path = tmp_path / f"{game}.db"

    result = runner.invoke(
        app,
        [
            "play",
            "--game",
            game,
            "--players",
            "mock:random,mock:fixed",
            "--rounds",
            "3",
            "--seed",
            "42",
            "--db",
            str(path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "对局已存档" in result.output
    store = SQLiteStore(path)
    rows = store.list_matches(game=game)
    assert len(rows) == 1
    archive = store.get_match(rows[0].match_id)
    assert archive is not None
    assert archive.game == game
    expected_config = {
        "rounds": 3,
        "source": "generated",
        "generator_version": 1,
    }
    if game == "riddle_quiz":
        expected_config = {
            "rounds": 3,
            "source": "generated_from_structured_bank",
            "bank_version": 1,
            "generator_version": 1,
        }
    assert archive.events[0].data["game_config"] == expected_config
    assert len(store.leaderboard(game=game)) == 2


@pytest.mark.parametrize("game", ["reasoning_quiz", "riddle_quiz"])
def test_new_quiz_series_swaps_seats_but_keeps_identical_questions(
    tmp_path, game: str
) -> None:
    path = tmp_path / f"{game}-series.db"

    result = runner.invoke(
        app,
        [
            "series",
            "--game",
            game,
            "--players",
            "mock:random,mock:fixed",
            "--rounds",
            "2",
            "--seed",
            "9",
            "--db",
            str(path),
        ],
    )

    assert result.exit_code == 0, result.output
    with sqlite3.connect(path) as connection:
        series_id = connection.execute("SELECT series_id FROM series_archives").fetchone()[0]
    series = SQLiteStore(path).get_series(series_id)
    assert series is not None
    assert [[player["name"] for player in leg.players] for leg in series.legs] == [
        ["mock:random", "mock:fixed"],
        ["mock:fixed", "mock:random"],
    ]

    def prompts_for(leg, player: str) -> list[str]:
        return [
            event.data["prompt"]
            for event in leg.events
            if event.type == EventType.TURN_PROMPT and event.player == player
        ]

    for player in ("mock:random", "mock:fixed"):
        assert prompts_for(series.legs[0], player) == prompts_for(series.legs[1], player)


def test_games_command_lists_both_new_projects() -> None:
    result = runner.invoke(app, ["games"])

    assert result.exit_code == 0
    assert "reasoning_quiz" in result.output
    assert "riddle_quiz" in result.output
