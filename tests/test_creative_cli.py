"""Creative-writing CLI integration and mode-boundary tests."""

from __future__ import annotations

import json
import os
import sqlite3

import pytest
from rich.text import Text
from typer.testing import CliRunner

from llmolympic import config
from llmolympic.cli.main import app
from llmolympic.core.judge import LLMJudgePanel
from llmolympic.core.player import LLMPlayer
from llmolympic.core.storage import (
    SQLiteStore,
    TournamentAuditError,
    audit_tournament,
)
from llmolympic.core.tournament import prepare_round_robin
from llmolympic.games.creative_writing import CreativeWriting
from llmolympic.providers.mock import MockProvider
from llmolympic.providers.openai_provider import OpenAIProvider

runner = CliRunner()


def _plain(output: str) -> str:
    return Text.from_ansi(output).plain


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


def _configure_same_route_profiles(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "profiles.toml"
    config_path.write_text(
        """
[profiles.contestant]
provider = "openai"
default_model = "shared-model"
base_url = "https://shared-gateway.example/v1"
api_key_env = "CREATIVE_ROUTE_TEST_KEY"

[profiles.judge_a]
provider = "openai"
default_model = "shared-model"
base_url = "https://shared-gateway.example/v1/"
api_key_env = "CREATIVE_ROUTE_TEST_KEY"

[profiles.judge_b]
provider = "openai"
default_model = "shared-model"
base_url = "https://SHARED-GATEWAY.EXAMPLE:443/v1"
api_key_env = "CREATIVE_ROUTE_TEST_KEY"
""",
        encoding="utf-8",
    )
    if os.name == "posix":
        config_path.chmod(0o600)
    monkeypatch.setenv("LLMOLYMPIC_CONFIG", str(config_path))
    monkeypatch.setenv("CREATIVE_ROUTE_TEST_KEY", "must-not-appear-in-output")
    config.load_config.cache_clear()


def test_creative_play_renders_judging_and_persists(tmp_path) -> None:
    path = tmp_path / "creative-cli.db"

    result = runner.invoke(app, _creative_args(path))
    output = _plain(result.output)

    assert result.exit_code == 0, result.output
    assert "匿名评审完成：3/3 名有效评委" in output
    assert "对局已存档" in output
    assert "creative_writing" in output
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
    output = _plain(result.output)

    assert result.exit_code == 2
    assert "需要至少 3 个 --judge" in output
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
    output = _plain(result.output)

    assert result.exit_code == 2
    assert "不使用 LLM 评审团" in output
    assert not path.exists()


def test_creative_rejects_self_judging_before_database_is_opened(tmp_path) -> None:
    path = tmp_path / "must-not-exist.db"
    args = _creative_args(path)
    strict_index = args.index("mock:strict")
    args[strict_index] = "mock:random"

    result = runner.invoke(app, args)
    output = _plain(result.output)

    assert result.exit_code == 2
    assert "不能同时担任参赛者和评委" in output
    assert not path.exists()


def test_creative_rejects_duplicate_profile_routes_before_database_or_network(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "duplicate-route-must-not-exist.db"
    _configure_same_route_profiles(monkeypatch, tmp_path)
    calls = 0

    async def unexpected_call(self, messages, *, model, **params):
        nonlocal calls
        calls += 1
        raise AssertionError("route gate must run before provider calls")

    monkeypatch.setattr(OpenAIProvider, "achat", unexpected_call)
    try:
        result = runner.invoke(
            app,
            [
                "play",
                "--game",
                "creative_writing",
                "--players",
                "mock:random,mock:fixed",
                "--judge",
                "profile:judge_a",
                "--judge",
                "profile:judge_b",
                "--judge",
                "mock:strict",
                "--db",
                str(path),
            ],
        )
    finally:
        config.load_config.cache_clear()

    output = _plain(result.output)
    assert result.exit_code == 2
    assert "评委路由身份必须唯一" in output
    assert "must-not-appear-in-output" not in output
    assert calls == 0
    assert not path.exists()


def test_creative_rejects_profile_route_self_judging_before_database_or_network(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "self-route-must-not-exist.db"
    _configure_same_route_profiles(monkeypatch, tmp_path)
    calls = 0

    async def unexpected_call(self, messages, *, model, **params):
        nonlocal calls
        calls += 1
        raise AssertionError("self-route gate must run before provider calls")

    monkeypatch.setattr(OpenAIProvider, "achat", unexpected_call)
    try:
        result = runner.invoke(
            app,
            [
                "play",
                "--game",
                "creative_writing",
                "--players",
                "profile:contestant,mock:fixed",
                "--judge",
                "profile:judge_a",
                "--judge",
                "mock:strict",
                "--judge",
                "mock:lenient",
                "--db",
                str(path),
            ],
        )
    finally:
        config.load_config.cache_clear()

    output = _plain(result.output)
    assert result.exit_code == 2
    assert "同一模型路由不能同时担任参赛者和评委" in output
    assert "must-not-appear-in-output" not in output
    assert calls == 0
    assert not path.exists()


def test_creative_series_and_round_robin_require_judges_before_opening_database(
    tmp_path,
) -> None:
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
    series_output = _plain(series.output)
    tournament_output = _plain(tournament.output)

    assert series.exit_code == 2
    assert "需要至少 3 个 --judge" in series_output
    assert tournament.exit_code == 2
    assert "需要至少 3 个 --judge" in tournament_output
    assert not (tmp_path / "series.db").exists()
    assert not (tmp_path / "round-robin.db").exists()


def test_creative_series_and_round_robin_complete_with_frozen_panel(tmp_path) -> None:
    judge_args = [
        "--judge",
        "mock:strict",
        "--judge",
        "mock:balanced",
        "--judge",
        "mock:lenient",
    ]
    series_path = tmp_path / "series.db"
    tournament_path = tmp_path / "round-robin.db"

    series = runner.invoke(
        app,
        [
            "series",
            "--game",
            "creative_writing",
            "--players",
            "mock:random,mock:fixed",
            *judge_args,
            "--seed",
            "42",
            "--db",
            str(series_path),
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
            *judge_args,
            "--seed",
            "42",
            "--max-provider-calls",
            "100",
            "--db",
            str(tournament_path),
        ],
    )

    assert series.exit_code == 0, series.output
    assert tournament.exit_code == 0, tournament.output
    assert "两局已原子存档" in _plain(series.output)
    assert "最终档案与 ELO 已原子封存" in _plain(tournament.output)

    series_store = SQLiteStore(series_path)
    series_matches = series_store.list_matches(game="creative_writing")
    assert len(series_matches) == 2
    with sqlite3.connect(tournament_path) as connection:
        row = connection.execute(
            "SELECT tournament_id FROM tournament_archives"
        ).fetchone()
    assert row is not None
    archived = SQLiteStore(tournament_path).get_verified_tournament(row[0])
    assert archived is not None
    assert archived.judge_panel is not None
    assert len(archived.pairings) == 3
    assert all(
        pairing.series.judge_panel == archived.judge_panel
        for pairing in archived.pairings
    )
    report = audit_tournament(row[0], tournament_path)
    assert report.state == "finalized"
    assert report.game == "creative_writing"
    budget = SQLiteStore(tournament_path).load_tournament_provider_budget(row[0])
    assert budget is not None
    assert budget.finalized is True
    assert budget.limits.calls == 100
    assert budget.spent.calls >= 24

    with sqlite3.connect(tournament_path) as connection:
        raw = connection.execute(
            "SELECT tournament_json FROM tournament_archives WHERE tournament_id = ?",
            (row[0],),
        ).fetchone()[0]
        payload = json.loads(raw)
        payload["judge_panel"]["panel"][0]["route_id"] = "route:v1:" + "f" * 64
        connection.execute(
            "UPDATE tournament_archives SET tournament_json = ? WHERE tournament_id = ?",
            (json.dumps(payload, ensure_ascii=False), row[0]),
        )
    with pytest.raises(TournamentAuditError) as caught:
        audit_tournament(row[0], tournament_path)
    assert caught.value.code == "tournament_inconsistent"


def test_creative_series_budget_exhaustion_saves_no_partial_leg_or_elo(tmp_path) -> None:
    database = tmp_path / "creative-series-budget.db"

    result = runner.invoke(
        app,
        [
            "series",
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
            "--max-provider-calls",
            "15",
            "--db",
            str(database),
        ],
    )

    assert result.exit_code == 1
    output = _plain(result.output)
    assert "Provider 预算中止" in output
    assert "本次结果未存档且未更新 ELO" in output
    store = SQLiteStore(database)
    assert store.list_matches(game="creative_writing") == []
    assert store.leaderboard() == []
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM series_archives").fetchone() == (0,)


def test_creative_round_robin_resumes_frozen_panel_from_sqlite(tmp_path) -> None:
    database = tmp_path / "creative-resume.db"
    contestants = [
        LLMPlayer(f"mock:{strategy}", MockProvider(strategy), strategy)
        for strategy in ("random", "fixed", "illegal")
    ]
    judges = [
        LLMPlayer(f"mock:{strategy}", MockProvider(strategy), strategy)
        for strategy in ("strict", "balanced", "lenient")
    ]
    checkpoint = prepare_round_robin(
        CreativeWriting(),
        contestants,
        seed=42,
        judge_panel=LLMJudgePanel(judges),
    )
    SQLiteStore(database).save_tournament_checkpoint(checkpoint)

    result = runner.invoke(
        app,
        [
            "round-robin",
            "--resume",
            checkpoint.tournament_id,
            "--db",
            str(database),
        ],
    )

    assert result.exit_code == 0, result.output
    output = _plain(result.output)
    assert "循环赛检查点已加载" in output
    assert "最终档案与 ELO 已原子封存" in output
    archived = SQLiteStore(database).get_verified_tournament(checkpoint.tournament_id)
    assert archived is not None
    assert archived.judge_panel == checkpoint.judge_panel
