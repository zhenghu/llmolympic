"""Creative-writing CLI integration and mode-boundary tests."""

from __future__ import annotations

from typer.testing import CliRunner

from llmolympic.cli.main import app
from llmolympic.core.storage import SQLiteStore

runner = CliRunner()


def _creative_args(path) -> list[str]:
    return [
        "play",
        "--game",
        "creative_writing",
        "--players",
        "mock:random,mock:fixed",
        "--judge",
        "mock:strict",
        "--judge",
        "mock:balanced",
        "--judge",
        "mock:lenient",
        "--seed",
        "42",
        "--db",
        str(path),
    ]


def test_creative_play_renders_judging_and_persists(tmp_path) -> None:
    path = tmp_path / "creative-cli.db"

    result = runner.invoke(app, _creative_args(path))

    assert result.exit_code == 0, result.output
    assert "匿名评审完成：3/3 名有效评委" in result.output
    assert "对局已存档" in result.output
    assert "creative_writing" in result.output
    store = SQLiteStore(path)
    matches = store.list_matches(game="creative_writing")
    assert len(matches) == 1
    assert len(store.leaderboard(game="creative_writing")) == 2


def test_creative_requires_judges_before_database_is_opened(tmp_path) -> None:
    path = tmp_path / "must-not-exist.db"

    result = runner.invoke(
        app,
        [
            "play",
            "--game",
            "creative_writing",
            "--players",
            "mock:random,mock:fixed",
            "--db",
            str(path),
        ],
    )

    assert result.exit_code != 0
    assert "需要至少 3 个 --judge" in result.output
    assert not path.exists()


def test_objective_game_rejects_judges_before_database_is_opened(tmp_path) -> None:
    path = tmp_path / "must-not-exist.db"

    result = runner.invoke(
        app,
        [
            "play",
            "--game",
            "math_quiz",
            "--players",
            "mock:random,mock:fixed",
            "--judge",
            "mock:strict",
            "--db",
            str(path),
        ],
    )

    assert result.exit_code != 0
    assert "不使用 LLM 评审团" in result.output
    assert not path.exists()


def test_creative_rejects_self_judging_before_database_is_opened(tmp_path) -> None:
    path = tmp_path / "must-not-exist.db"
    args = _creative_args(path)
    strict_index = args.index("mock:strict")
    args[strict_index] = "mock:random"

    result = runner.invoke(app, args)

    assert result.exit_code != 0
    assert "不能同时担任参赛者和评委" in result.output
    assert not path.exists()


def test_creative_is_explicitly_unavailable_in_series_and_round_robin(tmp_path) -> None:
    series = runner.invoke(
        app,
        [
            "series",
            "--game",
            "creative_writing",
            "--players",
            "mock:random,mock:fixed",
            "--db",
            str(tmp_path / "series.db"),
        ],
    )
    tournament = runner.invoke(
        app,
        [
            "round-robin",
            "--game",
            "creative_writing",
            "--players",
            "mock:random,mock:fixed,mock:illegal",
            "--db",
            str(tmp_path / "round-robin.db"),
        ],
    )

    assert series.exit_code != 0
    assert "不支持比赛模式 'series'" in series.output
    assert tournament.exit_code != 0
    assert "不支持比赛模式" in tournament.output
    assert "round_robin" in tournament.output
    assert not (tmp_path / "series.db").exists()
    assert not (tmp_path / "round-robin.db").exists()
