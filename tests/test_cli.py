"""End-to-end CLI persistence tests."""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3

import pytest
import typer
from rich.text import Text
from typer.testing import CliRunner

from llmolympic import config
from llmolympic.cli.main import (
    _prepare_round_robin,
    _restore_round_robin,
    _run_round_robin,
    _runner_lease_heartbeat,
    _validate_tournament_workload,
    app,
)
from llmolympic.core.events import EventType
from llmolympic.core.storage import (
    SQLiteStore,
    TournamentRunnerLeaseBusyError,
    TournamentRunnerLeaseLostError,
)
from llmolympic.core.tournament import (
    TournamentCheckpoint,
    checkpoint_with_series,
    prepare_round_robin,
    resume_round_robin,
)
from llmolympic.games import create_game
from llmolympic.providers.base import Provider
from llmolympic.providers.mock import MockProvider

runner = CliRunner()


def _configure_profiles(monkeypatch, tmp_path, content: str) -> None:
    config_path = tmp_path / "profiles.toml"
    config_path.write_text(content, encoding="utf-8")
    if os.name == "posix":
        config_path.chmod(0o600)
    monkeypatch.setenv("LLMOLYMPIC_CONFIG", str(config_path))
    config.load_config.cache_clear()


def _tournament_id_from_output(output: str) -> str:
    plain = Text.from_ansi(output).plain
    match = re.search(r"赛事 ID ([0-9a-f]{32})", plain)
    assert match is not None, plain
    return match.group(1)


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


def test_round_robin_persists_complete_tournament_and_query_context(tmp_path) -> None:
    path = tmp_path / "round-robin.db"
    result = runner.invoke(
        app,
        [
            "round-robin",
            "--game",
            "knowledge_quiz",
            "--players",
            "mock:fixed,mock:random,mock:illegal",
            "--rounds",
            "1",
            "--seed",
            "17",
            "--llm-timeout",
            "1",
            "--db",
            str(path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "循环赛检查点已就绪" in result.output
    assert "赛事 ID" in result.output
    assert "检查点已保存" in result.output
    assert "循环赛开始" in result.output
    assert "第 1/3 组 · 第 1/2 局" in result.output
    assert "第 3/3 组 · 第 2/2 局" in result.output
    assert "循环赛结果" in result.output
    assert "最终档案与 ELO 已原子封存" in result.output
    assert "3 组对阵 · 6 场对局" in result.output
    assert "循环赛 ELO 净变化" in result.output

    store = SQLiteStore(path)
    matches = store.list_matches(game="knowledge_quiz")
    assert len(matches) == 6
    tournament_ids = {row.tournament_id for row in matches}
    assert None not in tournament_ids
    assert len(tournament_ids) == 1
    tournament_id = next(iter(tournament_ids))
    assert {row.pairing_number for row in matches} == {1, 2, 3}
    assert {row.pairing_count for row in matches} == {3}
    assert len({row.series_id for row in matches}) == 3

    tournament = store.get_tournament(tournament_id)
    assert tournament is not None
    assert len(tournament.players) == 3
    assert len(tournament.pairings) == 3
    assert sum(len(pairing.series.legs) for pairing in tournament.pairings) == 6
    assert all(entry.games_played == 4 for entry in store.leaderboard())

    history = runner.invoke(app, ["history", "--db", str(path)])
    assert history.exit_code == 0
    assert tournament_id in history.output
    assert "第 1/3 组" in history.output
    assert "第 3/3 组" in history.output

    archive = runner.invoke(app, ["archive", tournament_id, "--db", str(path)])
    assert archive.exit_code == 0
    assert tournament_id in archive.output
    assert '"format": "round_robin_two_leg"' in archive.output
    assert '"pairings"' in archive.output


def test_round_robin_resume_rejects_active_lease_before_restoring_provider(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "active-runner.db"
    game, players = _prepare_round_robin(
        game_name="knowledge_quiz",
        player_spec="mock:fixed,mock:random,mock:illegal",
        rounds=1,
        llm_timeout=1,
        no_llm_timeout=False,
    )
    checkpoint = prepare_round_robin(game, players, seed=19)
    store = SQLiteStore(path)
    store.save_tournament_checkpoint(checkpoint)
    active = store.claim_tournament_runner(checkpoint.tournament_id).lease
    restored = False

    def fail_if_restored(*args, **kwargs):
        nonlocal restored
        restored = True
        raise AssertionError("active lease must fail before Provider reconstruction")

    monkeypatch.setattr("llmolympic.cli.main._restore_round_robin", fail_if_restored)
    result = runner.invoke(
        app,
        ["round-robin", "--resume", checkpoint.tournament_id, "--db", str(path)],
    )
    output = Text.from_ansi(result.output).plain

    assert result.exit_code == 1
    assert "无法取得循环赛执行权" in output
    assert "另一个执行者" in output
    assert active.token not in output
    assert not restored
    assert store.release_tournament_runner(active)


def test_round_robin_complete_checkpoint_finalizes_without_restoring_provider(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "complete-checkpoint.db"
    game, players = _prepare_round_robin(
        game_name="knowledge_quiz",
        player_spec="mock:fixed,mock:random,mock:illegal",
        rounds=1,
        llm_timeout=1,
        no_llm_timeout=False,
    )
    checkpoint = prepare_round_robin(game, players, seed=21)
    tournament = asyncio.run(resume_round_robin(game, players, checkpoint))
    store = SQLiteStore(path)
    store.save_tournament_checkpoint(checkpoint)
    lease = store.claim_tournament_runner(checkpoint.tournament_id).lease
    complete = checkpoint
    for pairing in tournament.pairings:
        complete = checkpoint_with_series(complete, pairing.series)
        store.save_tournament_checkpoint(complete, lease=lease)
    assert store.release_tournament_runner(lease)

    def fail_if_restored(*args, **kwargs):
        raise AssertionError("complete checkpoint must not reconstruct Provider")

    monkeypatch.setattr("llmolympic.cli.main._restore_round_robin", fail_if_restored)
    result = runner.invoke(
        app,
        ["round-robin", "--resume", checkpoint.tournament_id, "--db", str(path)],
    )

    assert result.exit_code == 0, result.output
    assert "最终档案与 ELO 已原子封存" in result.output
    assert store.get_verified_tournament(checkpoint.tournament_id) is not None
    assert all(entry.games_played == 4 for entry in store.leaderboard())
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT count(*) FROM tournament_runner_leases").fetchone()[0] == 0
        )
        history_count = connection.execute("SELECT count(*) FROM rating_history").fetchone()[0]

    repeated = runner.invoke(
        app,
        ["round-robin", "--resume", checkpoint.tournament_id, "--db", str(path)],
    )
    assert repeated.exit_code == 0, repeated.output
    assert "已完成，无需恢复" in repeated.output
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == history_count
        )


def test_runner_heartbeat_retries_transient_sqlite_busy(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "heartbeat-busy.db"
    game, players = _prepare_round_robin(
        game_name="knowledge_quiz",
        player_spec="mock:fixed,mock:random,mock:illegal",
        rounds=1,
        llm_timeout=1,
        no_llm_timeout=False,
    )
    checkpoint = prepare_round_robin(game, players, seed=22)
    store = SQLiteStore(path)
    store.save_tournament_checkpoint(checkpoint)
    lease = store.claim_tournament_runner(checkpoint.tournament_id).lease
    original_renew = store.renew_tournament_runner
    calls = 0

    async def exercise() -> None:
        nonlocal calls
        stop = asyncio.Event()

        def flaky_renew(handle):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.OperationalError("database is locked")
            renewed = original_renew(handle)
            stop.set()
            return renewed

        monkeypatch.setattr(store, "renew_tournament_runner", flaky_renew)
        monkeypatch.setattr(
            "llmolympic.cli.main.TOURNAMENT_RUNNER_HEARTBEAT_SECONDS",
            0.001,
        )
        monkeypatch.setattr(
            "llmolympic.cli.main.TOURNAMENT_RUNNER_BUSY_RETRY_SECONDS",
            0.001,
        )
        await asyncio.wait_for(_runner_lease_heartbeat(store, lease, stop), timeout=1)

    asyncio.run(exercise())

    assert calls == 2
    with pytest.raises(TournamentRunnerLeaseBusyError) as exc_info:
        SQLiteStore(path, create=False).claim_tournament_runner(checkpoint.tournament_id)
    assert "另一个执行者" in str(exc_info.value)
    assert store.release_tournament_runner(lease)


def test_real_runner_heartbeat_extends_short_initial_lease(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "heartbeat-renewal.db"
    game, players = _prepare_round_robin(
        game_name="knowledge_quiz",
        player_spec="mock:fixed,mock:random,mock:illegal",
        rounds=1,
        llm_timeout=1,
        no_llm_timeout=False,
    )
    checkpoint = prepare_round_robin(game, players, seed=24)
    store = SQLiteStore(path)
    store.save_tournament_checkpoint(checkpoint)
    lease = store.claim_tournament_runner(
        checkpoint.tournament_id,
        lease_seconds=2,
    ).lease
    monkeypatch.setattr(
        "llmolympic.cli.main.TOURNAMENT_RUNNER_HEARTBEAT_SECONDS",
        0.01,
    )

    async def exercise() -> None:
        stop = asyncio.Event()
        heartbeat = asyncio.create_task(_runner_lease_heartbeat(store, lease, stop))
        await asyncio.sleep(2.1)
        with pytest.raises(TournamentRunnerLeaseBusyError) as exc_info:
            SQLiteStore(path, create=False).claim_tournament_runner(checkpoint.tournament_id)
        assert "另一个执行者" in str(exc_info.value)
        stop.set()
        await heartbeat

    asyncio.run(exercise())

    assert store.release_tournament_runner(lease)


def test_runner_heartbeat_loss_cancels_tournament_before_checkpoint_write(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "lost-heartbeat.db"
    game, players = _prepare_round_robin(
        game_name="knowledge_quiz",
        player_spec="mock:fixed,mock:random,mock:illegal",
        rounds=1,
        llm_timeout=1,
        no_llm_timeout=False,
    )
    checkpoint = prepare_round_robin(game, players, seed=23)
    store = SQLiteStore(path)
    store.save_tournament_checkpoint(checkpoint)
    lease = store.claim_tournament_runner(checkpoint.tournament_id).lease
    started: asyncio.Event | None = None
    cancelled = False

    async def stalled_tournament(*args, **kwargs):
        nonlocal started, cancelled
        started = asyncio.Event()
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            raise

    async def lost_heartbeat(*args, **kwargs):
        while started is None:
            await asyncio.sleep(0)
        await started.wait()
        raise TournamentRunnerLeaseLostError("injected lease loss")

    monkeypatch.setattr("llmolympic.cli.main.resume_round_robin", stalled_tournament)
    monkeypatch.setattr("llmolympic.cli.main._runner_lease_heartbeat", lost_heartbeat)

    with pytest.raises(TournamentRunnerLeaseLostError, match="injected lease loss"):
        asyncio.run(_run_round_robin(game, players, checkpoint, store, lease))

    assert cancelled
    persisted = store.get_tournament_checkpoint(checkpoint.tournament_id)
    assert persisted is not None
    assert persisted.completed_series == ()
    assert store.get_tournament(checkpoint.tournament_id) is None
    assert store.release_tournament_runner(lease)


def test_round_robin_ctrl_c_saves_complete_prefix_and_resume_skips_it(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "resume-round-robin.db"
    original_save = SQLiteStore.save_tournament_checkpoint
    interrupted = False

    def save_then_interrupt(self, checkpoint, **kwargs):
        nonlocal interrupted
        result = original_save(self, checkpoint, **kwargs)
        if len(checkpoint.completed_series) == 1 and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return result

    with monkeypatch.context() as scoped:
        scoped.setattr(SQLiteStore, "save_tournament_checkpoint", save_then_interrupt)
        first = runner.invoke(
            app,
            [
                "round-robin",
                "--game",
                "knowledge_quiz",
                "--players",
                "mock:fixed,mock:random,mock:illegal",
                "--rounds",
                "1",
                "--seed",
                "17",
                "--llm-timeout",
                "1",
                "--db",
                str(path),
            ],
        )

    assert first.exit_code == 130, first.output
    assert "循环赛已中断" in first.output
    assert "已保存 1/3 组" in first.output
    assert "Traceback" not in first.output
    tournament_id = _tournament_id_from_output(first.output)
    store = SQLiteStore(path)
    checkpoint = store.get_tournament_checkpoint(tournament_id)
    assert checkpoint is not None
    assert len(checkpoint.completed_series) == 1
    assert store.list_matches() == []
    assert store.leaderboard() == []
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT token_digest FROM tournament_runner_leases WHERE tournament_id = ?",
                (tournament_id,),
            ).fetchone()[0]
            is None
        )

    resumed = runner.invoke(
        app,
        ["round-robin", "--resume", tournament_id, "--db", str(path)],
    )

    assert resumed.exit_code == 0, resumed.output
    assert "循环赛检查点已加载" in resumed.output
    assert "已保存 1/3 组" in resumed.output
    assert "第 1/3 组" not in resumed.output
    assert "第 2/3 组" in resumed.output
    assert "第 3/3 组" in resumed.output
    tournament = store.get_tournament(tournament_id)
    assert tournament is not None
    assert len(tournament.pairings) == 3
    assert all(entry.games_played == 4 for entry in store.leaderboard())

    repeated = runner.invoke(
        app,
        ["round-robin", "--resume", tournament_id, "--db", str(path)],
    )
    assert repeated.exit_code == 0, repeated.output
    assert "已完成，无需恢复" in repeated.output
    assert all(entry.games_played == 4 for entry in store.leaderboard())


def test_round_robin_resume_rebuilds_frozen_profile_model_game_and_timeout(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "resume-profile.db"
    _configure_profiles(
        monkeypatch,
        tmp_path,
        """
[profiles.local]
provider = "ollama"
default_model = "model-a"
display_name = "Original name"
""",
    )

    def create_test_profile_provider(profile: config.ProviderProfile) -> Provider:
        provider = MockProvider(strategy="fixed")
        provider.profile_id = profile.profile_id
        return provider

    async def interrupt_before_first_pairing(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "llmolympic.cli.main.create_profile_provider",
        create_test_profile_provider,
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(
            "llmolympic.cli.main.resume_round_robin",
            interrupt_before_first_pairing,
        )
        first = runner.invoke(
            app,
            [
                "round-robin",
                "--game",
                "knowledge_quiz",
                "--players",
                "profile:local,mock:random,mock:illegal",
                "--rounds",
                "1",
                "--llm-timeout",
                "0.5",
                "--db",
                str(path),
            ],
        )
    assert first.exit_code == 130, first.output
    tournament_id = _tournament_id_from_output(first.output)

    config_path = tmp_path / "profiles.toml"
    config_path.write_text(
        """
[profiles.local]
provider = "ollama"
default_model = "model-b"
display_name = "Renamed"
""",
        encoding="utf-8",
    )
    if os.name == "posix":
        config_path.chmod(0o600)
    monkeypatch.setenv("LLMOLYMPIC_LLM_TIMEOUT", "9")
    config.load_config.cache_clear()
    try:
        resumed = runner.invoke(
            app,
            ["round-robin", "--resume", tournament_id, "--db", str(path)],
        )
    finally:
        config.load_config.cache_clear()

    assert resumed.exit_code == 0, resumed.output
    tournament = SQLiteStore(path).get_tournament(tournament_id)
    assert tournament is not None
    assert tournament.game == "knowledge_quiz"
    assert tournament.players[0]["name"] == "Original name"
    assert tournament.players[0]["model"] == "model-a"
    assert tournament.players[0]["move_timeout_seconds"] == 0.5
    first_event = tournament.pairings[0].series.legs[0].events[0]
    assert first_event.data["game_config"]["rounds"] == 1


def test_round_robin_restores_legacy_checkpoint_without_route_snapshot() -> None:
    game, players = _prepare_round_robin(
        game_name="knowledge_quiz",
        player_spec="mock:fixed,mock:random,mock:illegal",
        rounds=1,
        llm_timeout=1,
        no_llm_timeout=False,
    )
    checkpoint = prepare_round_robin(game, players, seed=29)
    payload = checkpoint.model_dump(mode="python")
    for descriptor in payload["players"]:
        descriptor.pop("route_id")
    legacy_checkpoint = TournamentCheckpoint.model_validate(payload)

    restored_game, restored_players = _restore_round_robin(legacy_checkpoint)

    assert all(player.route_id.startswith("route:v1:") for player in restored_players)
    assert all("route_id" not in player.describe() for player in restored_players)
    tournament = asyncio.run(
        resume_round_robin(restored_game, restored_players, legacy_checkpoint)
    )
    assert all("route_id" not in descriptor for descriptor in tournament.players)
    assert all(
        "route_id" not in descriptor
        for pairing in tournament.pairings
        for leg in pairing.series.legs
        for descriptor in leg.players
    )


def test_round_robin_rejects_partial_or_changed_route_snapshot_before_play(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_profiles(
        monkeypatch,
        tmp_path,
        """
[profiles.local]
provider = "ollama"
default_model = "model"
base_url = "http://localhost:11434"
""",
    )
    game, players = _prepare_round_robin(
        game_name="knowledge_quiz",
        player_spec="profile:local,mock:random,mock:illegal",
        rounds=1,
        llm_timeout=1,
        no_llm_timeout=False,
    )
    checkpoint = prepare_round_robin(game, players, seed=30)

    partial = checkpoint.model_dump(mode="python")
    partial["players"][0].pop("route_id")
    with pytest.raises(ValueError, match="route_id 快照不完整"):
        _restore_round_robin(TournamentCheckpoint.model_validate(partial))

    config_path = tmp_path / "profiles.toml"
    config_path.write_text(
        """
[profiles.local]
provider = "ollama"
default_model = "model"
base_url = "http://localhost:22468"
""",
        encoding="utf-8",
    )
    if os.name == "posix":
        config_path.chmod(0o600)
    config.load_config.cache_clear()
    try:
        restored_game, restored_players = _restore_round_robin(checkpoint)
        with pytest.raises(ValueError, match="选手描述与 checkpoint 不一致"):
            asyncio.run(resume_round_robin(restored_game, restored_players, checkpoint))
    finally:
        config.load_config.cache_clear()


def test_round_robin_checkpoint_never_persists_profile_api_key(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "checkpoint-secret.db"
    sentinel = "checkpoint-sensitive-sentinel-6e5d1f"
    monkeypatch.setenv("CHECKPOINT_REMOTE_KEY", sentinel)
    _configure_profiles(
        monkeypatch,
        tmp_path,
        """
[profiles.remote]
provider = "openai"
default_model = "remote-model"
base_url = "https://remote.example/v1"
api_key_env = "CHECKPOINT_REMOTE_KEY"
display_name = "Remote"
""",
    )

    async def interrupt_before_first_pairing(*args, **kwargs):
        raise KeyboardInterrupt

    with monkeypatch.context() as scoped:
        scoped.setattr(
            "llmolympic.cli.main.resume_round_robin",
            interrupt_before_first_pairing,
        )
        try:
            result = runner.invoke(
                app,
                [
                    "round-robin",
                    "--players",
                    "profile:remote,mock:fixed,mock:illegal",
                    "--rounds",
                    "1",
                    "--llm-timeout",
                    "1",
                    "--db",
                    str(path),
                ],
            )
        finally:
            config.load_config.cache_clear()

    assert result.exit_code == 130, result.output
    tournament_id = _tournament_id_from_output(result.output)
    checkpoint = SQLiteStore(path).get_tournament_checkpoint(tournament_id)
    assert checkpoint is not None
    assert checkpoint.players[0]["profile_id"] == "remote"
    assert checkpoint.players[0]["model"] == "remote-model"
    assert sentinel not in checkpoint.model_dump_json()
    database_files = path.parent.glob(f"{path.name}*")
    assert all(
        sentinel.encode() not in database_file.read_bytes() for database_file in database_files
    )


@pytest.mark.parametrize(
    "conflict",
    [
        ["--game", "gomoku"],
        ["--players", "mock:fixed,mock:random,mock:illegal"],
        ["--rounds", "1"],
        ["--seed", "1"],
        ["--llm-timeout", "1"],
        ["--no-llm-timeout"],
        ["--allow-large-tournament"],
    ],
)
def test_round_robin_resume_rejects_new_tournament_configuration_before_opening_database(
    tmp_path,
    conflict: list[str],
) -> None:
    path = tmp_path / "resume-conflict.db"

    result = runner.invoke(
        app,
        ["round-robin", "--resume", "checkpoint-id", *conflict, "--db", str(path)],
    )

    output = Text.from_ansi(result.output).plain
    assert result.exit_code == 2
    assert "配置已由检查点冻结" in output
    assert "Traceback" not in output
    assert not path.exists()


def test_round_robin_board_game_roster_is_validated_as_two_player_pairs() -> None:
    game, players = _prepare_round_robin(
        game_name="gomoku",
        player_spec="mock:fixed,mock:random,mock:illegal",
        rounds=None,
        llm_timeout=None,
        no_llm_timeout=True,
    )

    assert game.name == "gomoku"
    assert [player.name for player in players] == [
        "mock:fixed",
        "mock:random",
        "mock:illegal",
    ]


def test_large_round_robin_requires_explicit_budget_override() -> None:
    game = create_game("knowledge_quiz", rounds=100)

    with pytest.raises(typer.BadParameter, match="allow-large-tournament"):
        _validate_tournament_workload(game, 16, allow_large=False)

    _validate_tournament_workload(game, 16, allow_large=True)


@pytest.mark.parametrize(
    "players,error",
    [
        ("mock:fixed,mock:random", "3 到 16"),
        (",".join(f"mock:entrant-{index}" for index in range(17)), "3 到 16"),
        ("human:我,mock:fixed,mock:random", "不支持人类"),
        ("mock:fixed,mock:fixed,mock:illegal", "稳定身份必须唯一"),
    ],
)
def test_round_robin_rejects_invalid_roster_before_database_creation(
    tmp_path, players: str, error: str
) -> None:
    path = tmp_path / "invalid-round-robin.db"

    result = runner.invoke(
        app,
        [
            "round-robin",
            "--players",
            players,
            "--llm-timeout",
            "1",
            "--db",
            str(path),
        ],
    )

    assert result.exit_code == 2
    assert error in result.output
    assert not path.exists()


@pytest.mark.parametrize(
    "args",
    [
        ["--game", "gomoku", "--rounds", "2"],
        ["--game", "unknown"],
        ["--seed", str(2**63)],
        ["--llm-timeout", "1", "--no-llm-timeout"],
    ],
)
def test_round_robin_rejects_invalid_options_before_database_creation(tmp_path, args) -> None:
    path = tmp_path / "invalid-round-robin-option.db"

    result = runner.invoke(app, ["round-robin", *args, "--db", str(path)])

    assert result.exit_code == 2
    assert not path.exists()


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
    config_path.write_text("[match]\nllm_timeout_seconds = 0.6\n", encoding="utf-8")
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


def test_named_profiles_support_two_compatible_endpoints_and_stable_entrant_ids(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "profiles.db"
    _configure_profiles(
        monkeypatch,
        tmp_path,
        """
[profiles.kimi]
provider = "openai"
default_model = "moonshot-v1"
base_url = "https://kimi.example/v1"
api_key_env = "KIMI_TEST_KEY"
display_name = "Kimi"

[profiles.deepseek]
provider = "openai"
default_model = "deepseek-chat"
base_url = "https://deepseek.example/v1"
api_key_env = "DEEPSEEK_TEST_KEY"
display_name = "DeepSeek"
""",
    )
    profiles_seen: list[config.ProviderProfile] = []

    def create_test_profile_provider(profile: config.ProviderProfile) -> Provider:
        profiles_seen.append(profile)
        strategy = "fixed" if profile.profile_id == "kimi" else "random"
        provider = MockProvider(strategy=strategy)
        provider.profile_id = profile.profile_id
        return provider

    monkeypatch.setattr("llmolympic.cli.main.create_profile_provider", create_test_profile_provider)
    try:
        result = runner.invoke(
            app,
            [
                "play",
                "--players",
                "profile:kimi,profile:deepseek:deepseek-reasoner",
                "--rounds",
                "1",
                "--db",
                str(path),
            ],
        )

        assert result.exit_code == 0, result.output
        assert [profile.base_url for profile in profiles_seen] == [
            "https://kimi.example/v1",
            "https://deepseek.example/v1",
        ]
        store = SQLiteStore(path)
        archive = store.get_match(store.list_matches()[0].match_id)
        assert archive is not None
        assert [player["name"] for player in archive.players] == ["Kimi", "DeepSeek"]
        assert [player["entrant_id"] for player in archive.players] == [
            "profile:kimi:moonshot-v1",
            "profile:deepseek:deepseek-reasoner",
        ]
        assert "KIMI_TEST_KEY" not in archive.to_json()
        assert "DEEPSEEK_TEST_KEY" not in archive.to_json()
    finally:
        config.load_config.cache_clear()


def test_profile_model_override_preserves_additional_colons(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_profiles(
        monkeypatch,
        tmp_path,
        """
[profiles.local]
provider = "ollama"
default_model = "fallback"
""",
    )
    models: list[str] = []

    def create_test_profile_provider(profile: config.ProviderProfile) -> Provider:
        provider = MockProvider(strategy="fixed")
        provider.profile_id = profile.profile_id
        return provider

    monkeypatch.setattr("llmolympic.cli.main.create_profile_provider", create_test_profile_provider)
    monkeypatch.setattr(
        "llmolympic.cli.main.create_provider",
        lambda kind, model="": MockProvider(strategy="random"),
    )
    from llmolympic.cli.main import _parse_players

    try:
        players = _parse_players(
            "profile:local:llama3.1:8b,mock:random",
            human_timeout=60.0,
            llm_timeout=1.0,
        )
        models.extend(player.model for player in players if hasattr(player, "model"))
    finally:
        config.load_config.cache_clear()

    assert models == ["llama3.1:8b", "random"]
    assert players[0].entrant_id == "profile:local:llama3.1:8b"


def test_profile_models_with_the_same_display_name_are_disambiguated(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_profiles(
        monkeypatch,
        tmp_path,
        """
[profiles.shared]
provider = "ollama"
default_model = "model-a"
display_name = "Same"
""",
    )

    def create_test_profile_provider(profile: config.ProviderProfile) -> Provider:
        provider = MockProvider(strategy="fixed")
        provider.profile_id = profile.profile_id
        return provider

    monkeypatch.setattr("llmolympic.cli.main.create_profile_provider", create_test_profile_provider)
    from llmolympic.cli.main import _parse_players

    try:
        players = _parse_players(
            "profile:shared:model-a,profile:shared:model-b",
            human_timeout=60.0,
            llm_timeout=1.0,
        )
    finally:
        config.load_config.cache_clear()

    assert [player.name for player in players] == [
        "Same [shared:model-a]",
        "Same [shared:model-b]",
    ]
    assert len({player.entrant_id for player in players}) == 2


def test_profile_disambiguation_avoids_an_existing_player_name(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_profiles(
        monkeypatch,
        tmp_path,
        """
[profiles.shared]
provider = "ollama"
default_model = "model-a"
display_name = "Same"
""",
    )

    def create_test_profile_provider(profile: config.ProviderProfile) -> Provider:
        provider = MockProvider(strategy="fixed")
        provider.profile_id = profile.profile_id
        return provider

    monkeypatch.setattr("llmolympic.cli.main.create_profile_provider", create_test_profile_provider)
    from llmolympic.cli.main import _parse_players

    try:
        players = _parse_players(
            "profile:shared:model-a,profile:shared:model-b,human:Same [shared:model-a]",
            human_timeout=60.0,
            llm_timeout=1.0,
        )
    finally:
        config.load_config.cache_clear()

    assert [player.name for player in players] == [
        "Same [shared:model-a] #2",
        "Same [shared:model-b]",
        "Same [shared:model-a]",
    ]
    assert len({player.name for player in players}) == 3


def test_profile_rejects_the_same_stable_entrant_twice(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_profiles(
        monkeypatch,
        tmp_path,
        '[profiles.local]\nprovider = "ollama"\ndefault_model = "model"\n',
    )

    def create_test_profile_provider(profile: config.ProviderProfile) -> Provider:
        provider = MockProvider(strategy="fixed")
        provider.profile_id = profile.profile_id
        return provider

    monkeypatch.setattr("llmolympic.cli.main.create_profile_provider", create_test_profile_provider)
    from llmolympic.cli.main import _parse_players

    try:
        with pytest.raises(typer.BadParameter, match="稳定身份必须唯一"):
            _parse_players(
                "profile:local,profile:local",
                human_timeout=60.0,
                llm_timeout=1.0,
            )
    finally:
        config.load_config.cache_clear()


@pytest.mark.parametrize(
    ("players", "error"),
    [
        ("profile:missing,mock:fixed", "未找到 Provider Profile"),
        ("profile:,mock:fixed", "profile:<id>"),
        ("profile:local:,mock:fixed", "模型名不能为空"),
    ],
)
def test_invalid_profile_player_is_reported_before_database_creation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    players: str,
    error: str,
) -> None:
    path = tmp_path / "invalid-profile.db"
    _configure_profiles(
        monkeypatch,
        tmp_path,
        '[profiles.local]\nprovider = "ollama"\ndefault_model = "model"\n',
    )
    try:
        result = runner.invoke(app, ["play", "--players", players, "--db", str(path)])
    finally:
        config.load_config.cache_clear()

    output = Text.from_ansi(result.output).plain
    assert result.exit_code == 2
    assert error in output
    assert "Traceback" not in output
    assert not path.exists()


def test_profile_missing_key_environment_is_a_clean_cli_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "missing-profile-key.db"
    monkeypatch.delenv("MISSING_PROFILE_KEY", raising=False)
    _configure_profiles(
        monkeypatch,
        tmp_path,
        """
[profiles.remote]
provider = "openai"
default_model = "model"
base_url = "https://remote.example/v1"
api_key_env = "MISSING_PROFILE_KEY"
""",
    )
    try:
        result = runner.invoke(
            app,
            ["play", "--players", "profile:remote,mock:fixed", "--db", str(path)],
        )
    finally:
        config.load_config.cache_clear()

    assert result.exit_code == 2
    assert "MISSING_PROFILE_KEY" in result.output
    assert "Traceback" not in result.output
    assert not path.exists()


def test_malformed_profile_table_is_a_clean_cli_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "malformed-profile.db"
    _configure_profiles(
        monkeypatch,
        tmp_path,
        '[profiles]\nbroken = "not-a-table"\n',
    )
    try:
        result = runner.invoke(
            app,
            ["play", "--players", "profile:broken,mock:fixed", "--db", str(path)],
        )
    finally:
        config.load_config.cache_clear()

    assert result.exit_code == 2
    assert "必须是 TOML 表" in result.output
    assert "Traceback" not in result.output
    assert not path.exists()


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
