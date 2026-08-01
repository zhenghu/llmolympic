"""End-to-end CLI persistence tests."""

from __future__ import annotations

import sqlite3

import pytest
from typer.testing import CliRunner

from llmolympic.cli.main import app
from llmolympic.core.storage import SQLiteStore

runner = CliRunner()


def test_play_persists_once_and_query_commands_read_same_database(tmp_path) -> None:
    path = tmp_path / "cli.db"
    result = runner.invoke(
        app,
        [
            "play",
            "--game",
            "math_quiz",
            "--players",
            "mock:random,mock:fixed",
            "--rounds",
            "2",
            "--seed",
            "3",
            "--db",
            str(path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "对局已存档" in result.output
    assert "ELO 更新" in result.output
    store = SQLiteStore(path)
    matches = store.list_matches()
    assert len(matches) == 1
    assert len(store.leaderboard()) == 2

    history = runner.invoke(app, ["history", "--db", str(path)])
    assert history.exit_code == 0
    assert matches[0].match_id in history.output
    assert "math_quiz" in history.output

    leaderboard = runner.invoke(app, ["leaderboard", "--game", "math_quiz", "--db", str(path)])
    assert leaderboard.exit_code == 0
    assert "mock:random" in leaderboard.output
    assert "mock:fixed" in leaderboard.output

    archive = runner.invoke(app, ["archive", matches[0].match_id, "--db", str(path)])
    assert archive.exit_code == 0
    assert matches[0].match_id in archive.output
    assert "match_finished" in archive.output


def test_gomoku_play_persists_match_and_updates_project_elo(tmp_path) -> None:
    path = tmp_path / "gomoku.db"
    result = runner.invoke(
        app,
        [
            "play",
            "--game",
            "gomoku",
            "--players",
            "mock:fixed,mock:illegal",
            "--seed",
            "5",
            "--db",
            str(path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "对局已存档" in result.output
    store = SQLiteStore(path)
    matches = store.list_matches(game="gomoku")
    assert len(matches) == 1
    archive = store.get_match(matches[0].match_id)
    assert archive is not None
    assert archive.game == "gomoku"
    assert archive.scores == {"mock:fixed": 1.0, "mock:illegal": 0.0}
    assert [move.move for move in archive.moves if move.accepted] == ["H8"]
    assert len([move for move in archive.moves if not move.accepted]) == 3

    leaderboard = store.leaderboard(game="gomoku")
    assert [entry.player for entry in leaderboard] == ["mock:fixed", "mock:illegal"]
    assert [entry.rating for entry in leaderboard] == [1516.0, 1484.0]
    assert [entry.player for entry in store.leaderboard()] == ["mock:fixed", "mock:illegal"]
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 4


@pytest.mark.parametrize(
    "players",
    ["mock:fixed", "mock:fixed,mock:random,mock:illegal"],
)
def test_gomoku_rejects_non_two_player_match_before_creating_database(
    tmp_path, players: str
) -> None:
    path = tmp_path / "wrong-player-count.db"
    result = runner.invoke(
        app,
        ["play", "--game", "gomoku", "--players", players, "--db", str(path)],
    )

    assert result.exit_code == 2
    assert "恰好 2 名选手" in result.output
    assert not path.exists()


def test_gomoku_player_count_is_checked_before_provider_creation(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "provider-must-not-open.db"

    def fail_if_called(*args, **kwargs):
        raise AssertionError("provider should not be created")

    monkeypatch.setattr("llmolympic.cli.main.create_provider", fail_if_called)
    result = runner.invoke(
        app,
        ["play", "--game", "gomoku", "--players", "openai:gpt", "--db", str(path)],
    )

    assert result.exit_code == 2
    assert "恰好 2 名选手" in result.output
    assert not path.exists()


def test_gomoku_rejects_rounds_before_creating_database(tmp_path) -> None:
    path = tmp_path / "gomoku-rounds.db"
    result = runner.invoke(
        app,
        ["play", "--game", "gomoku", "--rounds", "3", "--db", str(path)],
    )

    assert result.exit_code == 2
    assert "不支持参数: rounds" in result.output
    assert not path.exists()


def test_games_lists_gomoku_but_not_chess() -> None:
    result = runner.invoke(app, ["games"])

    assert result.exit_code == 0
    assert "gomoku" in result.output
    assert "chess" not in result.output


def test_play_rejects_zero_rounds_without_creating_database(tmp_path) -> None:
    path = tmp_path / "should-not-exist.db"
    result = runner.invoke(app, ["play", "--rounds", "0", "--db", str(path)])

    assert result.exit_code == 2
    assert not path.exists()


def test_play_rejects_unknown_game_before_creating_database(tmp_path) -> None:
    path = tmp_path / "invalid-game.db"
    result = runner.invoke(app, ["play", "--game", "chess", "--db", str(path)])

    assert result.exit_code == 2
    assert "未知项目" in result.output
    assert "Traceback" not in result.output
    assert not path.exists()


def test_play_rejects_invalid_player_before_creating_database(tmp_path) -> None:
    path = tmp_path / "invalid-player.db"
    result = runner.invoke(
        app,
        ["play", "--players", "mock:not-a-strategy,mock:fixed", "--db", str(path)],
    )

    assert result.exit_code == 2
    assert "未知 mock 策略" in result.output
    assert "Traceback" not in result.output
    assert not path.exists()


def test_read_commands_do_not_create_a_missing_database(tmp_path) -> None:
    path = tmp_path / "typo.db"
    result = runner.invoke(app, ["history", "--db", str(path)])

    assert result.exit_code == 1
    assert "数据库不存在" in result.output
    assert "Traceback" not in result.output
    assert not path.exists()


def test_corrupt_schema_is_reported_without_traceback(tmp_path) -> None:
    path = tmp_path / "corrupt.db"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 1")

    result = runner.invoke(app, ["leaderboard", "--db", str(path)])

    assert result.exit_code == 1
    assert "数据库结构不完整" in result.output
    assert "Traceback" not in result.output


def test_play_rejects_seed_outside_sqlite_range_before_running(tmp_path) -> None:
    path = tmp_path / "oversized-seed.db"
    result = runner.invoke(app, ["play", "--seed", str(2**63), "--db", str(path)])

    assert result.exit_code == 2
    assert not path.exists()
