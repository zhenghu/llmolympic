"""End-to-end CLI persistence tests."""

from __future__ import annotations

import sqlite3

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
