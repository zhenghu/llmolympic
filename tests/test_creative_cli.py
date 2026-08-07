"""Creative-writing CLI integration and mode-boundary tests."""

from __future__ import annotations

import os

import pytest
from rich.text import Text
from typer.testing import CliRunner

from llmolympic import config
from llmolympic.cli.main import app
from llmolympic.core.storage import SQLiteStore
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
    series_output = _plain(series.output)
    tournament_output = _plain(tournament.output)

    assert series.exit_code == 2
    assert "不支持比赛模式 'series'" in series_output
    assert tournament.exit_code == 2
    assert "不支持比赛模式" in tournament_output
    assert "round_robin" in tournament_output
    assert not (tmp_path / "series.db").exists()
    assert not (tmp_path / "round-robin.db").exists()
