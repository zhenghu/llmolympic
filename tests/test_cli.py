"""End-to-end CLI persistence tests."""

from __future__ import annotations

import asyncio
import os
import sqlite3

import pytest
from typer.testing import CliRunner

from llmolympic import config
from llmolympic.cli.main import app
from llmolympic.core.events import EventType
from llmolympic.core.storage import SQLiteStore
from llmolympic.providers.base import Provider
from llmolympic.providers.mock import MockProvider

runner = CliRunner()


class _FailingProvider(Provider):
    name = "openai"

    def chat(self, messages: list[dict], *, model: str, **params) -> str:
        raise AssertionError("CLI test must use native async provider path")

    async def achat(self, messages: list[dict], *, model: str, **params) -> str:
        raise RuntimeError("sensitive-token-must-not-be-archived")


class _LegacySyncProvider(Provider):
    name = "legacy"

    def chat(self, messages: list[dict], *, model: str) -> str:
        return "42"


class _SlowProvider(Provider):
    name = "openai"

    def chat(self, messages: list[dict], *, model: str, **params) -> str:
        raise AssertionError("CLI timeout test must use native async provider path")

    async def achat(self, messages: list[dict], *, model: str, **params) -> str:
        await asyncio.sleep(10)
        return "never"


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


def test_chess_play_persists_match_and_updates_project_elo(tmp_path) -> None:
    path = tmp_path / "chess.db"
    result = runner.invoke(
        app,
        [
            "play",
            "--game",
            "chess",
            "--players",
            "mock:fixed,mock:illegal",
            "--seed",
            "7",
            "--db",
            str(path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "对局已存档" in result.output
    store = SQLiteStore(path)
    matches = store.list_matches(game="chess")
    assert len(matches) == 1
    archive = store.get_match(matches[0].match_id)
    assert archive is not None
    assert archive.game == "chess"
    assert archive.scores == {"mock:fixed": 1.0, "mock:illegal": 0.0}
    assert [move.move for move in archive.moves if move.accepted] == ["e2e4"]
    assert len([move for move in archive.moves if not move.accepted]) == 3

    leaderboard = store.leaderboard(game="chess")
    assert [entry.player for entry in leaderboard] == ["mock:fixed", "mock:illegal"]
    assert [entry.rating for entry in leaderboard] == [1516.0, 1484.0]


def test_gomoku_series_swaps_colors_and_persists_one_fair_elo_batch(tmp_path) -> None:
    path = tmp_path / "gomoku-series.db"
    result = runner.invoke(
        app,
        [
            "series",
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
    assert "第 1/2 局" in result.output
    assert "第 2/2 局" in result.output
    assert "双局赛结果" in result.output
    assert "两局已原子存档" in result.output
    assert "系列赛 ELO 净变化" in result.output

    store = SQLiteStore(path)
    matches = store.list_matches(game="gomoku")
    assert len(matches) == 2
    archives = [store.get_match(row.match_id) for row in matches]
    assert all(archive is not None for archive in archives)
    assert {tuple(player["name"] for player in archive.players) for archive in archives} == {
        ("mock:fixed", "mock:illegal"),
        ("mock:illegal", "mock:fixed"),
    }
    assert {archive.seed for archive in archives} == {5}

    board = {entry.player: entry for entry in store.leaderboard(game="gomoku")}
    assert board["mock:fixed"].rating == pytest.approx(1532.0)
    assert board["mock:illegal"].rating == pytest.approx(1468.0)
    assert board["mock:fixed"].games_played == 2
    assert (board["mock:fixed"].wins, board["mock:fixed"].losses) == (2, 0)

    with sqlite3.connect(path) as connection:
        series_id = connection.execute("SELECT series_id FROM series_archives").fetchone()[0]
        assert connection.execute("SELECT count(*) FROM series_matches").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 8
    series_archive = store.get_series(series_id)
    assert series_archive is not None
    assert series_archive.points == {"mock:fixed": 2.0, "mock:illegal": 0.0}

    history = runner.invoke(app, ["history", "--game", "gomoku", "--db", str(path)])
    assert history.exit_code == 0
    assert all(row.match_id in history.output for row in matches)
    assert series_id in history.output

    archive_result = runner.invoke(app, ["archive", series_id, "--db", str(path)])
    assert archive_result.exit_code == 0
    assert series_id in archive_result.output
    assert '"legs"' in archive_result.output


def test_chess_series_swaps_colors_and_persists_one_fair_elo_batch(tmp_path) -> None:
    path = tmp_path / "chess-series.db"
    result = runner.invoke(
        app,
        [
            "series",
            "--game",
            "chess",
            "--players",
            "mock:fixed,mock:illegal",
            "--seed",
            "11",
            "--db",
            str(path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "mock:fixed（白） vs mock:illegal（黑）" in result.output
    assert "mock:illegal（白） vs mock:fixed（黑）" in result.output
    assert "两局已原子存档" in result.output

    store = SQLiteStore(path)
    matches = store.list_matches(game="chess")
    assert len(matches) == 2
    assert {tuple(row.players) for row in matches} == {
        ("mock:fixed", "mock:illegal"),
        ("mock:illegal", "mock:fixed"),
    }
    series_ids = {row.series_id for row in matches}
    assert None not in series_ids
    assert len(series_ids) == 1
    series_archive = store.get_series(next(iter(series_ids)))
    assert series_archive is not None
    assert series_archive.game == "chess"
    assert series_archive.points == {"mock:fixed": 2.0, "mock:illegal": 0.0}

    board = {entry.player: entry for entry in store.leaderboard(game="chess")}
    assert board["mock:fixed"].rating == pytest.approx(1532.0)
    assert board["mock:illegal"].rating == pytest.approx(1468.0)
    assert (board["mock:fixed"].wins, board["mock:fixed"].losses) == (2, 0)


@pytest.mark.parametrize(
    "players,error",
    [
        ("mock:fixed", "恰好 2"),
        ("mock:fixed,mock:random,mock:illegal", "恰好 2"),
        ("human:我,mock:fixed", "LLM/mock"),
    ],
)
def test_series_rejects_unsafe_or_invalid_players_before_database_creation(
    tmp_path, players: str, error: str
) -> None:
    path = tmp_path / "invalid-series.db"

    result = runner.invoke(
        app,
        ["series", "--game", "gomoku", "--players", players, "--db", str(path)],
    )

    assert result.exit_code == 2
    assert error in result.output
    assert not path.exists()


@pytest.mark.parametrize(
    "args",
    [
        ["--game", "gomoku", "--rounds", "2"],
        ["--game", "chess", "--rounds", "2"],
        ["--seed", str(2**63)],
        ["--llm-timeout", "1", "--no-llm-timeout"],
        ["--timeout", "1"],
    ],
)
def test_series_rejects_invalid_options_before_database_creation(tmp_path, args) -> None:
    path = tmp_path / "invalid-series-option.db"

    result = runner.invoke(app, ["series", *args, "--db", str(path)])

    assert result.exit_code == 2
    assert not path.exists()


def test_provider_failure_is_persisted_and_updates_elo(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "technical-loss.db"

    def create_test_provider(kind: str, model: str = "") -> Provider:
        if kind == "openai":
            return _FailingProvider()
        if kind == "mock":
            return MockProvider(strategy=model)
        raise AssertionError(f"unexpected provider: {kind}")

    monkeypatch.setattr("llmolympic.cli.main.create_provider", create_test_provider)
    result = runner.invoke(
        app,
        [
            "play",
            "--game",
            "math_quiz",
            "--players",
            "openai:broken,mock:fixed",
            "--rounds",
            "5",
            "--llm-timeout",
            "0.25",
            "--db",
            str(path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "技术负" in result.output
    assert "对局已存档" in result.output
    assert "ELO 更新" in result.output
    store = SQLiteStore(path)
    summary = store.list_matches()[0]
    archive = store.get_match(summary.match_id)
    assert archive is not None
    assert archive.scores == {"openai:broken": 0.0, "mock:fixed": 1.0}
    assert all(player["move_timeout_seconds"] == 0.25 for player in archive.players)
    rejected = next(event for event in archive.events if event.type == EventType.MOVE_REJECTED)
    assert rejected.data["reason_code"] == "provider_error"
    assert archive.events[-1].data["termination"] == "technical_loss"
    assert "sensitive-token" not in archive.to_json()
    leaderboard = store.leaderboard(game="math_quiz")
    assert [entry.player for entry in leaderboard] == ["mock:fixed", "openai:broken"]
    assert [entry.rating for entry in leaderboard] == [1516.0, 1484.0]
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 4


def test_llm_timeout_is_persisted_as_technical_loss_and_updates_elo(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "timeout-technical-loss.db"

    def create_test_provider(kind: str, model: str = "") -> Provider:
        if kind == "openai":
            return _SlowProvider()
        if kind == "mock":
            return MockProvider(strategy=model)
        raise AssertionError(f"unexpected provider: {kind}")

    monkeypatch.setattr("llmolympic.cli.main.create_provider", create_test_provider)
    result = runner.invoke(
        app,
        [
            "play",
            "--game",
            "math_quiz",
            "--players",
            "openai:slow,mock:fixed",
            "--rounds",
            "5",
            "--llm-timeout",
            "0.01",
            "--db",
            str(path),
        ],
    )

    assert result.exit_code == 0, result.output
    store = SQLiteStore(path)
    archive = store.get_match(store.list_matches()[0].match_id)
    assert archive is not None
    assert archive.scores == {"openai:slow": 0.0, "mock:fixed": 1.0}
    rejected = next(event for event in archive.events if event.type == EventType.MOVE_REJECTED)
    assert rejected.data["reason_code"] == "timeout"
    assert rejected.data["failure_details"]["timeout_seconds"] == 0.01
    finished = archive.events[-1]
    assert finished.data["termination"] == "technical_loss"
    assert finished.data["cause_event_seq"] == rejected.seq
    assert [entry.rating for entry in store.leaderboard(game="math_quiz")] == [
        1516.0,
        1484.0,
    ]


def test_llm_timeout_environment_default_is_recorded(tmp_path, monkeypatch) -> None:
    path = tmp_path / "environment-timeout.db"
    monkeypatch.setenv("LLMOLYMPIC_LLM_TIMEOUT", "0.75")

    result = runner.invoke(
        app,
        [
            "play",
            "--game",
            "math_quiz",
            "--players",
            "mock:fixed,mock:random",
            "--rounds",
            "1",
            "--db",
            str(path),
        ],
    )

    assert result.exit_code == 0, result.output
    archive = SQLiteStore(path).get_match(SQLiteStore(path).list_matches()[0].match_id)
    assert archive is not None
    assert all(player["move_timeout_seconds"] == 0.75 for player in archive.players)


def test_llm_timeout_config_default_is_recorded(tmp_path, monkeypatch) -> None:
    path = tmp_path / "config-timeout.db"
    config_path = tmp_path / "config.toml"
    config_path.write_text('[match]\nllm_timeout_seconds = 0.6\n', encoding="utf-8")
    if os.name == "posix":
        config_path.chmod(0o600)
    monkeypatch.setenv("LLMOLYMPIC_CONFIG", str(config_path))
    config.load_config.cache_clear()
    try:
        result = runner.invoke(
            app,
            [
                "play",
                "--players",
                "mock:fixed,mock:random",
                "--rounds",
                "1",
                "--db",
                str(path),
            ],
        )

        assert result.exit_code == 0, result.output
        store = SQLiteStore(path)
        archive = store.get_match(store.list_matches()[0].match_id)
        assert archive is not None
        assert all(player["move_timeout_seconds"] == 0.6 for player in archive.players)
    finally:
        config.load_config.cache_clear()


def test_explicit_llm_timeout_overrides_environment(tmp_path, monkeypatch) -> None:
    path = tmp_path / "explicit-timeout.db"
    monkeypatch.setenv("LLMOLYMPIC_LLM_TIMEOUT", "0.75")

    result = runner.invoke(
        app,
        [
            "play",
            "--players",
            "mock:fixed,mock:random",
            "--rounds",
            "1",
            "--llm-timeout",
            "0.2",
            "--db",
            str(path),
        ],
    )

    assert result.exit_code == 0, result.output
    store = SQLiteStore(path)
    archive = store.get_match(store.list_matches()[0].match_id)
    assert archive is not None
    assert all(player["move_timeout_seconds"] == 0.2 for player in archive.players)


def test_no_llm_timeout_keeps_legacy_sync_provider_usable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "legacy-provider.db"
    monkeypatch.setenv("LLMOLYMPIC_LLM_TIMEOUT", "not-a-number")

    def create_test_provider(kind: str, model: str = "") -> Provider:
        if kind == "openai":
            return _LegacySyncProvider()
        if kind == "mock":
            return MockProvider(strategy=model)
        raise AssertionError(f"unexpected provider: {kind}")

    monkeypatch.setattr("llmolympic.cli.main.create_provider", create_test_provider)
    result = runner.invoke(
        app,
        [
            "play",
            "--players",
            "openai:legacy,mock:fixed",
            "--rounds",
            "1",
            "--no-llm-timeout",
            "--db",
            str(path),
        ],
    )

    assert result.exit_code == 0, result.output
    store = SQLiteStore(path)
    archive = store.get_match(store.list_matches()[0].match_id)
    assert archive is not None
    assert all("move_timeout_seconds" not in player for player in archive.players)


def test_llm_timeout_and_disable_flag_are_mutually_exclusive(tmp_path) -> None:
    path = tmp_path / "conflicting-timeout.db"

    result = runner.invoke(
        app,
        [
            "play",
            "--llm-timeout",
            "1",
            "--no-llm-timeout",
            "--db",
            str(path),
        ],
    )

    assert result.exit_code == 2
    assert "不能与" in result.output
    assert not path.exists()


@pytest.mark.parametrize("value", ["nan", "inf", "0", "not-a-number"])
def test_invalid_llm_timeout_environment_is_rejected_before_database_creation(
    tmp_path, monkeypatch, value: str
) -> None:
    path = tmp_path / "invalid-timeout.db"
    monkeypatch.setenv("LLMOLYMPIC_LLM_TIMEOUT", value)

    result = runner.invoke(app, ["play", "--db", str(path)])

    assert result.exit_code == 2
    assert "LLM 单步超时" in result.output
    assert not path.exists()


def test_invalid_llm_timeout_environment_does_not_block_human_only_match(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "human-only.db"
    monkeypatch.setenv("LLMOLYMPIC_LLM_TIMEOUT", "not-a-number")

    async def skip_interactive_match(game, players, seed, store) -> None:
        assert all(player.kind == "human" for player in players)

    monkeypatch.setattr("llmolympic.cli.main._run", skip_interactive_match)
    result = runner.invoke(
        app,
        [
            "play",
            "--game",
            "gomoku",
            "--players",
            "human:a,human:b",
            "--db",
            str(path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "LLM 单步超时" not in result.output


def test_human_only_match_still_rejects_conflicting_explicit_llm_timeout_flags(
    tmp_path,
) -> None:
    path = tmp_path / "human-conflicting-timeout.db"
    result = runner.invoke(
        app,
        [
            "play",
            "--game",
            "gomoku",
            "--players",
            "human:a,human:b",
            "--llm-timeout",
            "1",
            "--no-llm-timeout",
            "--db",
            str(path),
        ],
    )

    assert result.exit_code == 2
    assert "不能与" in result.output
    assert not path.exists()


def test_human_only_match_still_rejects_invalid_explicit_llm_timeout(tmp_path) -> None:
    path = tmp_path / "human-invalid-timeout.db"
    result = runner.invoke(
        app,
        [
            "play",
            "--game",
            "gomoku",
            "--players",
            "human:a,human:b",
            "--llm-timeout",
            "nan",
            "--db",
            str(path),
        ],
    )

    assert result.exit_code == 2
    assert "LLM 单步超时" in result.output
    assert not path.exists()


@pytest.mark.parametrize("value", ["nan", "inf"])
def test_non_finite_human_timeout_is_rejected_before_database_creation(
    tmp_path, value: str
) -> None:
    path = tmp_path / "invalid-human-timeout.db"

    result = runner.invoke(
        app,
        [
            "play",
            "--players",
            "human:h",
            "--timeout",
            value,
            "--db",
            str(path),
        ],
    )

    assert result.exit_code == 2
    assert "人类行动超时" in result.output
    assert not path.exists()


@pytest.mark.parametrize("game", ["gomoku", "chess"])
@pytest.mark.parametrize(
    "players",
    ["mock:fixed", "mock:fixed,mock:random,mock:illegal"],
)
def test_board_games_reject_non_two_player_match_before_creating_database(
    tmp_path, game: str, players: str
) -> None:
    path = tmp_path / f"{game}-wrong-player-count.db"
    result = runner.invoke(
        app,
        ["play", "--game", game, "--players", players, "--db", str(path)],
    )

    assert result.exit_code == 2
    assert "恰好 2 名选手" in result.output
    assert not path.exists()


@pytest.mark.parametrize("game", ["gomoku", "chess"])
def test_board_game_player_count_is_checked_before_provider_creation(
    tmp_path, monkeypatch, game: str
) -> None:
    path = tmp_path / f"{game}-provider-must-not-open.db"

    def fail_if_called(*args, **kwargs):
        raise AssertionError("provider should not be created")

    monkeypatch.setattr("llmolympic.cli.main.create_provider", fail_if_called)
    result = runner.invoke(
        app,
        ["play", "--game", game, "--players", "openai:gpt", "--db", str(path)],
    )

    assert result.exit_code == 2
    assert "恰好 2 名选手" in result.output
    assert not path.exists()


@pytest.mark.parametrize("game", ["gomoku", "chess"])
def test_board_games_reject_rounds_before_creating_database(tmp_path, game: str) -> None:
    path = tmp_path / f"{game}-rounds.db"
    result = runner.invoke(
        app,
        ["play", "--game", game, "--rounds", "3", "--db", str(path)],
    )

    assert result.exit_code == 2
    assert "不支持参数: rounds" in result.output
    assert not path.exists()


def test_games_lists_both_board_games() -> None:
    result = runner.invoke(app, ["games"])

    assert result.exit_code == 0
    assert "gomoku" in result.output
    assert "chess" in result.output


def test_play_rejects_zero_rounds_without_creating_database(tmp_path) -> None:
    path = tmp_path / "should-not-exist.db"
    result = runner.invoke(app, ["play", "--rounds", "0", "--db", str(path)])

    assert result.exit_code == 2
    assert not path.exists()


def test_play_rejects_unknown_game_before_creating_database(tmp_path) -> None:
    path = tmp_path / "invalid-game.db"
    result = runner.invoke(app, ["play", "--game", "not-a-game", "--db", str(path)])

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
