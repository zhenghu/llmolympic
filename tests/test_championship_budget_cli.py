"""CLI integration coverage for durable championship Provider budgets."""

from __future__ import annotations

import asyncio
import os
import re

import pytest
from rich.text import Text
from typer.testing import CliRunner

from llmolympic import config
from llmolympic.cli.main import _prepare_championship, app
from llmolympic.config import ProviderBudgetSettings
from llmolympic.core.budget_config import resolve_provider_budget
from llmolympic.core.championship import prepare_championship, resume_championship
from llmolympic.core.player import LLMPlayer
from llmolympic.core.storage import SQLiteStore
from llmolympic.games import create_game
from llmolympic.providers.base import Provider
from llmolympic.providers.mock import MockProvider

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clear_config_cache_after_test():
    yield
    config.load_config.cache_clear()


def _championship_id(output: str) -> str:
    plain = Text.from_ansi(output).plain
    match = re.search(r"赛事 ID ([0-9a-f]{32})", plain)
    assert match is not None, plain
    return match.group(1)


def _compact_output(output: str) -> str:
    return "".join(Text.from_ansi(output).plain.split())


def _configure_profiles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *profile_ids: str,
) -> None:
    blocks = []
    for profile_id in profile_ids:
        blocks.append(
            "\n".join(
                (
                    f"[profiles.{profile_id}]",
                    'provider = "ollama"',
                    'default_model = "fixed-model"',
                    f'display_name = "Profile {profile_id}"',
                )
            )
        )
    path = tmp_path / "profiles.toml"
    path.write_text("\n\n".join(blocks), encoding="utf-8")
    if os.name == "posix":
        path.chmod(0o600)
    monkeypatch.setenv("LLMOLYMPIC_CONFIG", str(path))
    config.load_config.cache_clear()


def _use_fixed_profile_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    def create_test_profile_provider(profile: config.ProviderProfile) -> Provider:
        provider = MockProvider("fixed")
        provider.profile_id = profile.profile_id
        return provider

    monkeypatch.setattr(
        "llmolympic.cli.main.create_profile_provider",
        create_test_profile_provider,
    )


def _profile_player_spec() -> str:
    return ",".join(f"profile:p{index}" for index in range(1, 5))


def test_mock_championship_does_not_require_a_provider_budget(tmp_path) -> None:
    path = tmp_path / "mock-championship.db"

    result = runner.invoke(
        app,
        [
            "championship",
            "--game",
            "math_quiz",
            "--players",
            "mock:random,mock:fixed,mock:illegal,mock:balanced",
            "--rounds",
            "1",
            "--db",
            str(path),
        ],
    )

    assert result.exit_code == 0, result.output
    championship_id = _championship_id(result.output)
    store = SQLiteStore(path, create=False)
    assert store.get_championship(championship_id) is not None
    assert store.load_championship_provider_budget(championship_id) is None


def test_new_profile_championship_requires_an_enabled_budget(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "profile-without-budget.db"
    _configure_profiles(monkeypatch, tmp_path, "p1", "p2", "p3", "p4")
    _use_fixed_profile_provider(monkeypatch)

    result = runner.invoke(
        app,
        [
            "championship",
            "--game",
            "math_quiz",
            "--players",
            _profile_player_spec(),
            "--rounds",
            "1",
            "--db",
            str(path),
        ],
    )

    assert result.exit_code != 0
    compact = _compact_output(result.output)
    assert "Profile锦标赛必须显式启用Provider" in compact
    assert "硬预算" in compact
    assert not path.exists()


def test_profile_championship_atomically_freezes_and_finalizes_one_budget(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "profile-budget.db"
    _configure_profiles(monkeypatch, tmp_path, "p1", "p2", "p3", "p4")
    _use_fixed_profile_provider(monkeypatch)

    result = runner.invoke(
        app,
        [
            "championship",
            "--game",
            "math_quiz",
            "--players",
            _profile_player_spec(),
            "--rounds",
            "1",
            "--max-provider-calls",
            "100",
            "--db",
            str(path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Provider 预算" in result.output
    championship_id = _championship_id(result.output)
    budget = SQLiteStore(path, create=False).load_championship_provider_budget(championship_id)
    assert budget is not None
    assert budget.budget_id.startswith("budget:v2:")
    assert budget.tournament_id is None
    assert budget.championship_id == championship_id
    assert budget.finalized is True
    assert budget.reserved.calls == 0
    assert 1 <= budget.spent.calls <= 100
    assert budget.limits.calls == 100


def test_profile_championship_resume_reuses_the_same_durable_ledger(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "profile-budget-resume.db"
    _configure_profiles(monkeypatch, tmp_path, "p1", "p2", "p3", "p4")
    _use_fixed_profile_provider(monkeypatch)

    first = runner.invoke(
        app,
        [
            "championship",
            "--game",
            "knowledge_quiz",
            "--players",
            _profile_player_spec(),
            "--rounds",
            "1",
            "--max-provider-calls",
            "8",
            "--db",
            str(path),
        ],
    )

    assert first.exit_code == 1, first.output
    assert "Provider 预算中止" in first.output
    championship_id = _championship_id(first.output)
    store = SQLiteStore(path, create=False)
    checkpoint = store.get_championship_checkpoint(championship_id)
    assert checkpoint is not None
    assert len(checkpoint.completed_series) == 2
    before = store.load_championship_provider_budget(championship_id)
    assert before is not None
    assert before.spent.calls == 8
    assert before.finalized is False

    resumed = runner.invoke(
        app,
        ["championship", "--resume", championship_id, "--db", str(path)],
    )

    assert resumed.exit_code == 1, resumed.output
    assert "Provider 预算中止" in resumed.output
    after = store.load_championship_provider_budget(championship_id)
    assert after is not None
    assert after.budget_id == before.budget_id
    assert after.spent == before.spent
    assert after.reserved.calls == 0
    assert store.get_championship(championship_id) is None


def test_profile_checkpoint_without_a_ledger_fails_closed_before_restore(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "legacy-profile-checkpoint.db"
    _configure_profiles(monkeypatch, tmp_path, "p1")
    _use_fixed_profile_provider(monkeypatch)
    game, players = _prepare_championship(
        game_name="knowledge_quiz",
        player_spec="profile:p1,mock:random,mock:fixed,mock:balanced",
        rounds=1,
        llm_timeout=1,
        no_llm_timeout=False,
    )
    checkpoint = prepare_championship(game, players, seed=17)
    store = SQLiteStore(path)
    store.save_championship_checkpoint(checkpoint)
    restored = False

    def fail_if_restored(*_args, **_kwargs):
        nonlocal restored
        restored = True
        raise AssertionError("missing legacy budget must fail before Provider restore")

    monkeypatch.setattr(
        "llmolympic.cli.main._restore_championship",
        fail_if_restored,
    )
    result = runner.invoke(
        app,
        [
            "championship",
            "--resume",
            checkpoint.championship_id,
            "--db",
            str(path),
        ],
    )

    assert result.exit_code == 1
    assert "无法恢复锦标赛 Provider 预算" in result.output
    assert "缺少冻结Provider预算" in _compact_output(result.output)
    assert restored is False
    replacement = store.claim_championship_runner(checkpoint.championship_id).lease
    assert store.release_championship_runner(replacement)


def test_complete_checkpoint_and_completed_archive_fast_paths_finalize_once(
    tmp_path,
) -> None:
    path = tmp_path / "complete-checkpoint.db"
    game = create_game("knowledge_quiz", mode="play", rounds=1)
    players = [
        LLMPlayer(f"fixed-{index}", MockProvider("fixed"), f"fixed-{index}") for index in range(4)
    ]
    checkpoint = prepare_championship(
        game,
        players,
        seed=31,
        championship_id="complete-championship",
    )
    resolved = resolve_provider_budget(
        players,
        ProviderBudgetSettings(max_provider_calls=100),
        {},
    )
    assert resolved is not None
    store = SQLiteStore(path)
    store.create_championship_checkpoint_with_provider_budget(
        checkpoint,
        resolved.budget_id_for("championship", checkpoint.championship_id),
        resolved.limits,
        resolved.policy,
    )
    claim = store.claim_championship_runner(checkpoint.championship_id)
    ledger = store.bind_championship_usage_budget(
        checkpoint.championship_id,
        lease=claim.lease,
    )
    assert ledger is not None
    for player in players:
        player.bind_usage_budget(ledger, resolved.policy)

    async def finish_checkpoint() -> None:
        await resume_championship(
            game,
            players,
            checkpoint,
            on_checkpoint=lambda updated: store.save_championship_checkpoint(
                updated,
                lease=claim.lease,
            ),
        )

    asyncio.run(finish_checkpoint())
    assert store.release_championship_runner(claim.lease)
    complete = store.get_championship_checkpoint(checkpoint.championship_id)
    assert complete is not None and complete.is_complete
    open_budget = store.load_championship_provider_budget(checkpoint.championship_id)
    assert open_budget is not None and not open_budget.finalized

    resumed = runner.invoke(
        app,
        [
            "championship",
            "--resume",
            checkpoint.championship_id,
            "--db",
            str(path),
        ],
    )
    assert resumed.exit_code == 0, resumed.output
    finalized = store.load_championship_provider_budget(checkpoint.championship_id)
    assert finalized is not None and finalized.finalized
    assert store.get_championship(checkpoint.championship_id) is not None

    repeated = runner.invoke(
        app,
        [
            "championship",
            "--resume",
            checkpoint.championship_id,
            "--db",
            str(path),
        ],
    )
    assert repeated.exit_code == 0, repeated.output
    assert "已完成，无需恢复" in repeated.output
    assert store.load_championship_provider_budget(checkpoint.championship_id) == finalized
