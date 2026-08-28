"""Unit security tests for the local Web job-control primitives."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from llmolympic import control
from llmolympic.config import ProviderProfile, ProviderTokenPrice
from llmolympic.control import (
    CONTROL_SCHEMA_VERSION,
    ControlBudgetSpec,
    ControlError,
    ControlJob,
    ControlJobSpec,
    ControlParticipationLink,
    ControlPlayerSpec,
    JobStore,
    build_job_argv,
    control_catalog,
    derive_jobs_database_path,
    profile_configuration_digest,
    validate_job_spec,
)
from llmolympic.core.championship import prepare_championship, resume_championship
from llmolympic.core.player import LLMPlayer
from llmolympic.core.storage import SQLiteStore
from llmolympic.core.tournament import prepare_round_robin
from llmolympic.core.usage import (
    BudgetLimits,
    ProviderBudgetPolicy,
    RouteBudgetPolicy,
    TokenPrice,
)
from llmolympic.games import create_game
from llmolympic.providers import create_profile_provider
from llmolympic.providers.mock import MockProvider
from llmolympic.providers.ollama_provider import OllamaProvider


def _mock_spec(*, seed: str = "42", human_name: str | None = None) -> ControlJobSpec:
    first = (
        ControlPlayerSpec(kind="human", name=human_name)
        if human_name is not None
        else ControlPlayerSpec(kind="mock", strategy="random")
    )
    return ControlJobSpec(
        mode="play",
        game="math_quiz",
        players=(first, ControlPlayerSpec(kind="mock", strategy="fixed")),
        rounds=1,
        seed=seed,
        human_timeout_seconds=300.0,
        llm_timeout_seconds=120.0,
        budget=ControlBudgetSpec(
            max_provider_calls="64",
            max_input_tokens="200000",
            max_output_tokens_per_call="4096",
            max_total_output_tokens="65536",
        ),
    )


def _resume_spec(tournament_id: str) -> ControlJobSpec:
    return ControlJobSpec(
        mode="round_robin",
        resume_tournament_id=tournament_id,
    )


def _profile_spec(profile_id: str) -> ControlJobSpec:
    return ControlJobSpec(
        mode="play",
        game="math_quiz",
        players=(
            ControlPlayerSpec(kind="profile", profile_id=profile_id),
            ControlPlayerSpec(kind="mock", strategy="fixed"),
        ),
        rounds=1,
        seed="7",
        budget=ControlBudgetSpec(
            max_provider_calls="8",
            max_input_tokens="16000",
            max_output_tokens_per_call="512",
            max_total_output_tokens="4096",
            max_estimated_cost_usd="1.25",
        ),
    )


def _round_robin_spec() -> ControlJobSpec:
    return ControlJobSpec(
        mode="round_robin",
        game="math_quiz",
        players=tuple(
            ControlPlayerSpec(kind="mock", strategy=strategy)
            for strategy in ("random", "fixed", "illegal")
        ),
        rounds=2,
        seed="17",
    )


def _championship_spec() -> ControlJobSpec:
    return ControlJobSpec(
        mode="championship",
        game="math_quiz",
        players=tuple(
            ControlPlayerSpec(kind="mock", strategy=strategy)
            for strategy in ("random", "fixed", "illegal", "balanced")
        ),
        rounds=2,
        seed="23",
    )


def _resume_championship_spec(championship_id: str) -> ControlJobSpec:
    return ControlJobSpec(
        mode="championship",
        resume_championship_id=championship_id,
    )


def _championship_checkpoint_database(
    path: Path,
    *,
    championship_id: str = "resume-championship",
) -> Path:
    players = [
        LLMPlayer(
            name=f"Mock {strategy}",
            provider=MockProvider(strategy=strategy),
            model=strategy,
        )
        for strategy in ("random", "fixed", "illegal", "balanced")
    ]
    checkpoint = prepare_championship(
        create_game("math_quiz", mode="play", rounds=2),
        players,
        seed=23,
        championship_id=championship_id,
    )
    SQLiteStore(path).save_championship_checkpoint(checkpoint)
    return path


def _checkpoint_database(path: Path, *, tournament_id: str = "resume-tournament") -> Path:
    players = [
        LLMPlayer(
            name=name,
            provider=MockProvider(strategy=strategy),
            model=strategy,
        )
        for name, strategy in (("甲", "random"), ("乙", "fixed"), ("丙", "illegal"))
    ]
    checkpoint = prepare_round_robin(
        create_game("math_quiz", mode="round_robin", rounds=2),
        players,
        seed=17,
        tournament_id=tournament_id,
    )
    SQLiteStore(path).save_tournament_checkpoint(checkpoint)
    return path


def _profile_checkpoint_database(
    path: Path,
    profile: ProviderProfile,
    *,
    direct_provider: bool = False,
) -> Path:
    first_provider = (
        OllamaProvider(base_url=profile.base_url)
        if direct_provider
        else create_profile_provider(profile)
    )
    players = [
        LLMPlayer(name="Profile", provider=first_provider, model="frozen-model"),
        LLMPlayer(name="Mock A", provider=MockProvider(strategy="random"), model="random"),
        LLMPlayer(name="Mock B", provider=MockProvider(strategy="fixed"), model="fixed"),
    ]
    checkpoint = prepare_round_robin(
        create_game("math_quiz", mode="round_robin", rounds=1),
        players,
        seed=19,
        tournament_id="profile-resume",
    )
    policy = ProviderBudgetPolicy(
        max_output_tokens_per_call=128,
        routes=tuple(RouteBudgetPolicy(player.route_id) for player in players),
    )
    SQLiteStore(path).create_tournament_checkpoint_with_provider_budget(
        checkpoint,
        "profile-resume-budget",
        BudgetLimits(calls=100, input=100_000, output=100_000),
        policy,
    )
    return path


def _profile_championship_checkpoint_database(
    path: Path,
    profile: ProviderProfile,
    *,
    complete_budget: bool,
) -> Path:
    players = [
        LLMPlayer(
            name="Profile",
            provider=create_profile_provider(profile),
            model="frozen-model",
        ),
        *(
            LLMPlayer(
                name=f"Mock {strategy}",
                provider=MockProvider(strategy=strategy),
                model=strategy,
            )
            for strategy in ("random", "fixed", "balanced")
        ),
    ]
    checkpoint = prepare_championship(
        create_game("math_quiz", mode="play", rounds=1),
        players,
        seed=29,
        championship_id="profile-championship-resume",
    )
    price = TokenPrice(0, 0) if complete_budget else None
    policy = ProviderBudgetPolicy(
        max_output_tokens_per_call=128,
        routes=tuple(
            RouteBudgetPolicy(route_id=player.route_id, price=price)
            for player in players
        ),
    )
    SQLiteStore(path).create_championship_checkpoint_with_provider_budget(
        checkpoint,
        "profile-championship-budget",
        BudgetLimits(
            calls=100,
            input=100_000,
            output=100_000,
            estimated_cost=0 if complete_budget else None,
        ),
        policy,
    )
    return path


def test_championship_spec_preview_and_argv_are_unrated_and_fixed(tmp_path: Path) -> None:
    spec = _championship_spec()

    preview = validate_job_spec(spec)
    argv = build_job_argv(
        spec,
        archive_database=tmp_path / "championship.db",
        web_base_url="http://127.0.0.1:8765",
        python_executable="/fixed/python",
    )

    assert preview.player_count == 4
    assert preview.pairing_count == 3
    assert preview.match_count == 6
    assert preview.rated is False
    assert preview.requires_provider_budget is False
    assert argv[:4] == ("/fixed/python", "-m", "llmolympic", "championship")
    assert argv[argv.index("--players") + 1] == (
        "mock:random,mock:fixed,mock:illegal,mock:balanced"
    )


def test_championship_final_shape_requires_every_bracket_match(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "championship-final-shape.db")
    spec = _championship_spec()
    prepared = store.prepare(
        spec,
        validate_job_spec(spec),
        idempotency_key="championship-final-shape",
    )
    payload = prepared.model_dump(mode="json")
    payload.update(
        {
            "status": "finalizing",
            "championship_id": "championship-final",
            "final_kind": "championship",
            "final_id": "championship-final",
            "final_match_ids": [f"match-{index}" for index in range(4)],
        }
    )

    with pytest.raises(ValidationError, match="canonical bracket match"):
        ControlJob.model_validate(payload)


@pytest.mark.parametrize("player_count", [4, 8, 16])
def test_championship_accepts_only_power_of_two_bracket_sizes(player_count: int) -> None:
    players = tuple(
        ControlPlayerSpec(kind="profile", profile_id=f"entrant-{index}")
        for index in range(player_count)
    )

    assert ControlJobSpec(
        mode="championship",
        game="math_quiz",
        players=players,
    ).players == players


@pytest.mark.parametrize("player_count", [2, 3, 5, 6, 7, 9, 15])
def test_championship_rejects_non_bracket_sizes(player_count: int) -> None:
    players = tuple(
        ControlPlayerSpec(kind="profile", profile_id=f"entrant-{index}")
        for index in range(player_count)
    )

    with pytest.raises(ValidationError, match="4, 8, or 16"):
        ControlJobSpec(
            mode="championship",
            game="math_quiz",
            players=players,
        )


def test_championship_rejects_humans_and_games_without_play_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError, match="human players"):
        ControlJobSpec(
            mode="championship",
            game="math_quiz",
            players=(
                ControlPlayerSpec(kind="human", name="Human"),
                ControlPlayerSpec(kind="mock", strategy="random"),
                ControlPlayerSpec(kind="mock", strategy="fixed"),
                ControlPlayerSpec(kind="mock", strategy="balanced"),
            ),
        )

    base_game = control.GAME_REGISTRY["math_quiz"]

    class RoundRobinOnly(base_game):
        supported_modes = frozenset({"round_robin"})

    monkeypatch.setitem(control.GAME_REGISTRY, "round_robin_only", RoundRobinOnly)
    with pytest.raises(ValidationError, match="does not support"):
        ControlJobSpec(
            mode="championship",
            game="round_robin_only",
            players=_championship_spec().players,
        )


def test_championship_resume_preview_accepts_a_complete_checkpoint_read_only(
    tmp_path: Path,
) -> None:
    database = _championship_checkpoint_database(tmp_path / "complete-checkpoint.db")
    archive = SQLiteStore(database, create=False)
    checkpoint = archive.get_championship_checkpoint("resume-championship")
    assert checkpoint is not None
    game = create_game("math_quiz", mode="play", rounds=2)
    players = [
        LLMPlayer(
            name=f"Mock {strategy}",
            provider=MockProvider(strategy=strategy),
            model=strategy,
        )
        for strategy in ("random", "fixed", "illegal", "balanced")
    ]
    claim = archive.claim_championship_runner(checkpoint.championship_id)

    async def finish() -> None:
        await resume_championship(
            game,
            players,
            claim.checkpoint,
            on_checkpoint=lambda updated: archive.save_championship_checkpoint(
                updated,
                lease=claim.lease,
            ),
        )

    asyncio.run(finish())
    assert archive.release_championship_runner(claim.lease)
    completed = archive.get_championship_checkpoint(checkpoint.championship_id)
    assert completed is not None and completed.is_complete
    before = (database.stat().st_mtime_ns, database.read_bytes())

    preview = validate_job_spec(
        _resume_championship_spec(checkpoint.championship_id),
        archive_database=database,
    )
    argv = build_job_argv(
        _resume_championship_spec(checkpoint.championship_id),
        archive_database=database,
        web_base_url="http://127.0.0.1:8765",
        python_executable="/fixed/python",
    )

    assert preview.pairing_count == 3
    assert preview.match_count == 6
    assert preview.rated is False
    assert preview.frozen_players == tuple(player.name for player in players)
    assert argv == (
        "/fixed/python",
        "-m",
        "llmolympic",
        "championship",
        "--resume",
        checkpoint.championship_id,
        "--db",
        str(database),
    )
    assert (database.stat().st_mtime_ns, database.read_bytes()) == before


@pytest.mark.parametrize("complete_budget", [True, False])
def test_profile_championship_resume_requires_a_complete_persistent_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    complete_budget: bool,
) -> None:
    profile = ProviderProfile(
        profile_id="local",
        provider="ollama",
        default_model="current-model",
        base_url="http://127.0.0.1:11434/v1",
    )
    database = _profile_championship_checkpoint_database(
        tmp_path / f"profile-championship-{complete_budget}.db",
        profile,
        complete_budget=complete_budget,
    )
    monkeypatch.setattr(control, "load_profiles", lambda: {"local": profile})
    spec = _resume_championship_spec("profile-championship-resume")

    if not complete_budget:
        with pytest.raises(ControlError, match="resume_unavailable"):
            validate_job_spec(spec, archive_database=database)
        return

    preview = validate_job_spec(spec, archive_database=database)
    assert preview.requires_provider_budget is True
    assert preview.uses_frozen_budget is True
    assert preview.prepared_profiles[0].profile_id == "local"
    assert preview.prepared_profiles[0].effective_models == ("frozen-model",)


def test_resume_preview_hydrates_validated_frozen_configuration_read_only(
    tmp_path: Path,
) -> None:
    database = _checkpoint_database(tmp_path / "archive.db")
    before = (database.stat().st_mtime_ns, database.read_bytes())

    preview = validate_job_spec(
        _resume_spec("resume-tournament"),
        archive_database=database,
    )

    assert preview.player_count == 3
    assert preview.pairing_count == 3
    assert preview.match_count == 6
    assert preview.frozen_game == "math_quiz"
    assert preview.frozen_players == ("甲", "乙", "丙")
    assert preview.frozen_judges == ()
    assert preview.frozen_rounds == 2
    assert preview.frozen_seed == "17"
    assert preview.frozen_llm_timeout_seconds == 120.0
    assert preview.uses_frozen_budget is False
    assert preview.warnings == ("resume_uses_frozen_configuration",)
    assert (database.stat().st_mtime_ns, database.read_bytes()) == before


def test_resume_preview_fails_closed_for_missing_or_corrupt_checkpoint(
    tmp_path: Path,
) -> None:
    database = _checkpoint_database(tmp_path / "archive.db")
    with pytest.raises(ControlError, match="resume_unavailable"):
        validate_job_spec(
            _resume_spec("missing-tournament"),
            archive_database=database,
        )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE tournament_checkpoints SET players_json = '[]' "
            "WHERE tournament_id = 'resume-tournament'"
        )
    with pytest.raises(ControlError, match="resume_unavailable"):
        validate_job_spec(
            _resume_spec("resume-tournament"),
            archive_database=database,
        )


def test_resume_preview_binds_named_profile_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ProviderProfile(
        profile_id="cloud",
        provider="openai",
        default_model="model-a",
        base_url="https://provider.example/v1",
        api_key_env="RESUME_CLOUD_PROFILE_KEY",
        display_name="Cloud profile",
    )
    monkeypatch.setenv("RESUME_CLOUD_PROFILE_KEY", "test-only-placeholder")
    database = _profile_checkpoint_database(tmp_path / "profile-resume.db", profile)
    monkeypatch.setattr(control, "load_profiles", lambda: {"cloud": profile})
    monkeypatch.setattr(
        control,
        "load_provider_pricing",
        lambda: pytest.fail("resume must use its frozen budget, not current pricing"),
    )

    preview = validate_job_spec(
        _resume_spec("profile-resume"),
        archive_database=database,
        require_current_pricing=True,
    )

    assert preview.requires_provider_budget is True
    assert preview.uses_frozen_budget is True
    assert tuple(item.profile_id for item in preview.prepared_profiles) == ("cloud",)
    assert preview.prepared_profiles[0].default_model == "model-a"
    assert preview.prepared_profiles[0].effective_models == ("frozen-model",)
    assert preview.prepared_profiles[0].configuration_digest == (
        profile_configuration_digest(profile)
    )


def test_web_resume_rejects_legacy_direct_provider_checkpoint(
    tmp_path: Path,
) -> None:
    profile = ProviderProfile(
        profile_id="local",
        provider="ollama",
        default_model="model-a",
        base_url="http://127.0.0.1:11434/v1",
    )
    database = _profile_checkpoint_database(
        tmp_path / "direct-provider-resume.db",
        profile,
        direct_provider=True,
    )

    with pytest.raises(ControlError, match="resume_unavailable") as rejected:
        validate_job_spec(
            _resume_spec("profile-resume"),
            archive_database=database,
        )

    assert rejected.value.code == "resume_unavailable"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("budget", {"max_provider_calls": "1"}),
        ("human_timeout_seconds", 121.0),
        ("llm_timeout_seconds", 121.0),
        ("allow_large_tournament", True),
        ("seed", "1"),
    ),
)
def test_resume_spec_rejects_every_new_configuration_field(
    field: str,
    value: object,
) -> None:
    payload = _resume_spec("resume-tournament").model_dump(mode="json")
    payload[field] = value

    with pytest.raises(ValidationError, match="resume must not include"):
        ControlJobSpec.model_validate(payload)


@pytest.mark.parametrize("status", ["cancelled", "failed", "interrupted"])
def test_terminal_round_robin_job_is_resumable_only_with_a_valid_checkpoint(
    tmp_path: Path,
    status: str,
) -> None:
    database = _checkpoint_database(tmp_path / f"{status}.db")
    store = JobStore(database)
    spec = _round_robin_spec()
    prepared = store.prepare(
        spec,
        validate_job_spec(spec),
        idempotency_key=f"prepare-{status}",
    )
    store.transition(
        prepared.job_id,
        expected=("prepared",),
        status="running",
        tournament_id="resume-tournament",
    )

    terminal = store.transition(
        prepared.job_id,
        expected=("running",),
        status=status,
        failure_code="worker_failed" if status == "failed" else None,
    )

    assert terminal.resumable is True


def test_terminal_championship_job_is_resumable_with_a_valid_checkpoint(
    tmp_path: Path,
) -> None:
    database = _championship_checkpoint_database(tmp_path / "championship-resumable.db")
    store = JobStore(database)
    spec = _championship_spec()
    prepared = store.prepare(
        spec,
        validate_job_spec(spec),
        idempotency_key="prepare-championship-resumable",
    )
    store.transition(
        prepared.job_id,
        expected=("prepared",),
        status="running",
        championship_id="resume-championship",
    )

    terminal = store.transition(
        prepared.job_id,
        expected=("running",),
        status="interrupted",
        failure_code="worker_interrupted",
    )

    assert terminal.championship_id == "resume-championship"
    assert terminal.tournament_id is None
    assert terminal.resumable is True


def test_active_tournament_runner_lease_blocks_resume_and_resumable_flag(
    tmp_path: Path,
) -> None:
    database = _checkpoint_database(tmp_path / "active-lease.db")
    jobs = JobStore(database)
    spec = _round_robin_spec()
    prepared = jobs.prepare(
        spec,
        validate_job_spec(spec),
        idempotency_key="prepare-active-lease",
    )
    jobs.transition(
        prepared.job_id,
        expected=("prepared",),
        status="running",
        tournament_id="resume-tournament",
    )
    archive = SQLiteStore(database, create=False)
    claim = archive.claim_tournament_runner("resume-tournament")

    try:
        terminal = jobs.transition(
            prepared.job_id,
            expected=("running",),
            status="interrupted",
            failure_code="worker_interrupted",
        )
        assert terminal.resumable is False
        with pytest.raises(ControlError, match="resume_unavailable") as unavailable:
            validate_job_spec(
                _resume_spec("resume-tournament"),
                archive_database=database,
            )
        assert unavailable.value.code == "resume_unavailable"
    finally:
        archive.release_tournament_runner(claim.lease)

    assert jobs.get(prepared.job_id).resumable is True


def test_control_models_forbid_commands_routes_environment_and_model_overrides() -> None:
    baseline = _mock_spec().model_dump(mode="json")
    malicious: list[dict[str, object]] = []

    top_level = dict(baseline)
    top_level["command"] = "$(touch never)"
    malicious.append(top_level)

    for field in ("base_url", "endpoint", "env", "model", "path"):
        payload = _mock_spec().model_dump(mode="json")
        player = payload["players"][0]
        assert isinstance(player, dict)
        player[field] = f"untrusted-{field}"
        malicious.append(payload)

    for payload in malicious:
        with pytest.raises(ValidationError):
            ControlJobSpec.model_validate(payload)

    with pytest.raises(ValidationError):
        ControlJobSpec.model_validate({**baseline, "game": "unknown_game"})
    with pytest.raises(ValidationError):
        ControlJobSpec.model_validate({**baseline, "seed": "01"})
    with pytest.raises(ValidationError):
        ControlJobSpec.model_validate({**baseline, "seed": "1; touch never"})


@pytest.mark.parametrize("userinfo", ["user@", "user:secret@", ":secret@", "@"])
def test_participation_link_rejects_every_userinfo_form(userinfo: str) -> None:
    with pytest.raises(ValidationError, match="participation URL is invalid"):
        ControlParticipationLink(
            player_name="Human",
            url=f"http://{userinfo}localhost/participate/session/seat#capability={'a' * 43}",
        )


def test_profile_configuration_digest_binds_every_credential_free_field() -> None:
    profile = ProviderProfile(
        profile_id="private-profile",
        provider="openai",
        default_model="fixed-model",
        base_url="https://private-provider.example/v1",
        api_key_env="PRIVATE_CONTROL_API_KEY",
        display_name="Private profile",
    )
    baseline = profile_configuration_digest(profile)
    variants = (
        replace(profile, profile_id="other-profile"),
        replace(profile, provider="ollama"),
        replace(profile, default_model="other-model"),
        replace(profile, base_url="https://private-provider.example/v2"),
        replace(profile, api_key_env="OTHER_CONTROL_API_KEY"),
        replace(profile, display_name="Renamed profile"),
    )

    assert all(profile_configuration_digest(item) != baseline for item in variants)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://user:embedded-secret@private-provider.example/v1",
        "https://private-provider.example/v1?api_key=embedded-secret",
        "https://private-provider.example/v1#embedded-secret",
    ],
)
def test_web_prepare_rejects_credential_bearing_profile_url_before_hashing(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
) -> None:
    profile = ProviderProfile(
        profile_id="unsafe-profile",
        provider="openai",
        default_model="fixed-model",
        base_url=base_url,
        api_key_env="UNSAFE_CONTROL_API_KEY",
    )
    monkeypatch.setenv("UNSAFE_CONTROL_API_KEY", "environment-secret")
    monkeypatch.setattr(control, "load_profiles", lambda: {profile.profile_id: profile})
    monkeypatch.setattr(
        control.hashlib,
        "sha256",
        lambda *_args, **_kwargs: pytest.fail("unsafe URL reached the digest"),
    )

    with pytest.raises(ControlError, match="profile_unavailable") as rejected:
        validate_job_spec(_profile_spec(profile.profile_id))

    assert rejected.value.code == "profile_unavailable"
    assert "embedded-secret" not in str(rejected.value)


def test_cost_budget_matches_cli_maximum() -> None:
    accepted = ControlBudgetSpec(max_estimated_cost_usd="1000000")
    assert accepted.max_estimated_cost_usd == "1000000"

    with pytest.raises(ValidationError, match="between 0 and 1000000"):
        ControlBudgetSpec(max_estimated_cost_usd="1000001")


def test_round_robin_preview_is_rated() -> None:
    assert validate_job_spec(_round_robin_spec()).rated is True


def test_control_argv_is_a_fixed_vector_even_for_shell_metacharacters(tmp_path: Path) -> None:
    dangerous_name = "human; $(touch never) `id` --flag"
    database = tmp_path / "archive path; never.db"
    spec = _mock_spec(human_name=dangerous_name)

    argv = build_job_argv(
        spec,
        archive_database=database,
        web_base_url="http://127.0.0.1:8765",
        python_executable="/fixed/python",
    )

    assert isinstance(argv, tuple)
    assert argv[:4] == ("/fixed/python", "-m", "llmolympic", "play")
    assert "sh" not in argv
    assert "bash" not in argv
    assert "-c" not in argv
    players_index = argv.index("--players")
    assert argv[players_index + 1] == f"human:{dangerous_name},mock:fixed"
    database_index = argv.index("--db")
    assert argv[database_index + 1] == str(database)
    assert not (tmp_path / "never").exists()


def test_catalog_exposes_profile_availability_without_credentials_or_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ProviderProfile(
        profile_id="private-profile",
        provider="openai",
        default_model="safe-default-model",
        base_url="https://sensitive-endpoint.example/v1",
        api_key_env="PRIVATE_PROFILE_KEY",
        display_name="Private profile",
    )
    monkeypatch.setenv("PRIVATE_PROFILE_KEY", "secret-provider-value")
    monkeypatch.setattr(control, "load_profiles", lambda: {profile.profile_id: profile})

    payload = control_catalog().model_dump_json()

    assert "private-profile" in payload
    assert "safe-default-model" in payload
    assert '"credential_ready":true' in payload
    assert "PRIVATE_PROFILE_KEY" not in payload
    assert "secret-provider-value" not in payload
    assert "sensitive-endpoint.example" not in payload
    assert "base_url" not in payload
    assert "api_key_env" not in payload
    catalog = control_catalog()
    math_quiz = next(game for game in catalog.games if game.name == "math_quiz")
    assert "championship" in math_quiz.supported_modes
    assert math_quiz.supported_modes.count("championship") == 1
    assert "balanced" in catalog.mock_player_strategies


def test_profile_jobs_require_a_ready_allowlisted_profile_and_complete_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ProviderProfile(
        profile_id="allowed",
        provider="openai",
        default_model="fixed-model",
        base_url="https://provider.example/v1",
        api_key_env="ALLOWED_PROFILE_KEY",
    )
    monkeypatch.setenv("ALLOWED_PROFILE_KEY", "secret")
    monkeypatch.setattr(control, "load_profiles", lambda: {"allowed": profile})
    incomplete = ControlJobSpec(
        mode="play",
        game="math_quiz",
        players=(
            ControlPlayerSpec(kind="profile", profile_id="allowed"),
            ControlPlayerSpec(kind="mock", strategy="fixed"),
        ),
        rounds=1,
        seed="1",
    )

    with pytest.raises(ControlError, match="budget_required") as missing_budget:
        validate_job_spec(incomplete)
    assert missing_budget.value.code == "budget_required"

    unavailable = incomplete.model_copy(
        update={
            "players": (
                ControlPlayerSpec(kind="profile", profile_id="missing"),
                ControlPlayerSpec(kind="mock", strategy="fixed"),
            )
        }
    )
    with pytest.raises(ControlError, match="profile_unavailable") as missing_profile:
        validate_job_spec(unavailable)
    assert missing_profile.value.code == "profile_unavailable"


def test_new_profile_job_requires_exact_current_pricing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ProviderProfile(
        profile_id="priced-profile",
        provider="openai",
        default_model="fixed-model",
        base_url="https://provider.example/v1",
        api_key_env="PRICED_PROFILE_KEY",
    )
    monkeypatch.setenv("PRICED_PROFILE_KEY", "secret")
    monkeypatch.setattr(
        control,
        "load_profiles",
        lambda: {profile.profile_id: profile},
    )
    spec = _profile_spec(profile.profile_id)

    monkeypatch.setattr(
        control,
        "load_provider_pricing",
        lambda: {
            "profile:priced-profile:other-model": ProviderTokenPrice(
                Decimal(0),
                Decimal(0),
            )
        },
    )
    with pytest.raises(ControlError, match="provider_pricing_required") as missing:
        validate_job_spec(spec, require_current_pricing=True)
    assert missing.value.code == "provider_pricing_required"

    monkeypatch.setattr(
        control,
        "load_provider_pricing",
        lambda: {
            "profile:priced-profile:fixed-model": ProviderTokenPrice(
                Decimal(0),
                Decimal(0),
            )
        },
    )
    preview = validate_job_spec(spec, require_current_pricing=True)
    assert preview.prepared_profiles[0].effective_models == ("fixed-model",)

    dynamic = replace(profile, default_model="openrouter/auto")
    monkeypatch.setattr(
        control,
        "load_profiles",
        lambda: {dynamic.profile_id: dynamic},
    )
    monkeypatch.setattr(
        control,
        "load_provider_pricing",
        lambda: {
            "profile:priced-profile:openrouter/auto": ProviderTokenPrice(
                Decimal(0),
                Decimal(0),
            )
        },
    )
    with pytest.raises(ControlError, match="provider_pricing_required"):
        validate_job_spec(spec, require_current_pricing=True)


def test_new_profile_job_rejects_conflicting_prices_for_one_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles = {
        "first": ProviderProfile(
            profile_id="first",
            provider="openai",
            default_model="fixed-model",
            base_url="https://PROVIDER.example:443/v1/",
            api_key_env="FIRST_PROFILE_KEY",
        ),
        "second": ProviderProfile(
            profile_id="second",
            provider="openai",
            default_model="fixed-model",
            base_url="https://provider.example/v1",
            api_key_env="SECOND_PROFILE_KEY",
        ),
    }
    monkeypatch.setenv("FIRST_PROFILE_KEY", "first-placeholder")
    monkeypatch.setenv("SECOND_PROFILE_KEY", "second-placeholder")
    monkeypatch.setattr(control, "load_profiles", lambda: profiles)
    spec = ControlJobSpec(
        mode="play",
        game="math_quiz",
        players=tuple(
            ControlPlayerSpec(kind="profile", profile_id=profile_id)
            for profile_id in profiles
        ),
        rounds=1,
        budget=_profile_spec("first").budget,
    )
    first_price = ProviderTokenPrice(Decimal(1), Decimal(2))
    second_price = ProviderTokenPrice(Decimal(3), Decimal(4))
    monkeypatch.setattr(
        control,
        "load_provider_pricing",
        lambda: {
            "profile:first:fixed-model": first_price,
            "profile:second:fixed-model": second_price,
        },
    )

    with pytest.raises(ControlError, match="provider_pricing_required") as rejected:
        validate_job_spec(spec, require_current_pricing=True)
    assert rejected.value.code == "provider_pricing_required"

    monkeypatch.setattr(
        control,
        "load_provider_pricing",
        lambda: {
            "profile:first:fixed-model": first_price,
            "profile:second:fixed-model": first_price,
        },
    )
    preview = validate_job_spec(spec, require_current_pricing=True)
    assert tuple(item.profile_id for item in preview.prepared_profiles) == (
        "first",
        "second",
    )

    equivalent_first = ProviderTokenPrice(
        Decimal("1.0000000001"),
        Decimal("2.0000000001"),
    )
    equivalent_second = ProviderTokenPrice(
        Decimal("1.0000000002"),
        Decimal("2.0000000002"),
    )
    monkeypatch.setattr(
        control,
        "load_provider_pricing",
        lambda: {
            "profile:first:fixed-model": equivalent_first,
            "profile:second:fixed-model": equivalent_second,
        },
    )
    validate_job_spec(spec, require_current_pricing=True)


def test_invalid_current_pricing_has_a_stable_control_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ProviderProfile(
        profile_id="invalid-pricing",
        provider="openai",
        default_model="fixed-model",
        base_url="https://provider.example/v1",
        api_key_env="INVALID_PRICING_KEY",
    )
    monkeypatch.setenv("INVALID_PRICING_KEY", "secret")
    monkeypatch.setattr(
        control,
        "load_profiles",
        lambda: {profile.profile_id: profile},
    )

    def invalid_pricing() -> dict[str, object]:
        raise ValueError("private pricing parser detail")

    monkeypatch.setattr(control, "load_provider_pricing", invalid_pricing)
    with pytest.raises(ControlError, match="provider_pricing_required") as rejected:
        validate_job_spec(
            _profile_spec(profile.profile_id),
            require_current_pricing=True,
        )
    assert rejected.value.code == "provider_pricing_required"
    assert "private pricing parser detail" not in str(rejected.value)


def test_profile_projection_validation_errors_are_stable_control_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malformed = ProviderProfile(
        profile_id="malformed",
        provider="ollama",
        default_model="fixed-model",
        display_name="unsafe\nprofile",
    )
    monkeypatch.setattr(control, "load_profiles", lambda: {"malformed": malformed})
    spec = ControlJobSpec(
        mode="play",
        game="math_quiz",
        players=(
            ControlPlayerSpec(kind="profile", profile_id="malformed"),
            ControlPlayerSpec(kind="mock", strategy="fixed"),
        ),
        rounds=1,
        budget=ControlBudgetSpec(
            max_provider_calls="2",
            max_input_tokens="1000",
            max_output_tokens_per_call="128",
            max_total_output_tokens="256",
            max_estimated_cost_usd="0",
        ),
    )

    with pytest.raises(ControlError, match="profile_unavailable") as rejected:
        validate_job_spec(spec)

    assert rejected.value.code == "profile_unavailable"


@pytest.mark.parametrize(
    ("player_count", "rounds"),
    ((7, 1), (4, 100)),
)
def test_round_robin_confirmation_matches_cli_workload_limits(
    player_count: int,
    rounds: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles = {
        f"entrant-{index}": ProviderProfile(
            profile_id=f"entrant-{index}",
            provider="ollama",
            default_model="fixed-local-model",
        )
        for index in range(player_count)
    }
    monkeypatch.setattr(control, "load_profiles", lambda: profiles)
    spec = ControlJobSpec(
        mode="round_robin",
        game="math_quiz",
        players=tuple(
            ControlPlayerSpec(kind="profile", profile_id=profile_id)
            for profile_id in profiles
        ),
        rounds=rounds,
        seed="7",
        budget=ControlBudgetSpec(
            max_provider_calls="10000",
            max_input_tokens="1000000",
            max_output_tokens_per_call="1024",
            max_total_output_tokens="1000000",
            max_estimated_cost_usd="100",
        ),
    )

    with pytest.raises(
        ControlError,
        match="large_tournament_confirmation_required",
    ) as confirmation:
        validate_job_spec(spec)
    assert confirmation.value.code == "large_tournament_confirmation_required"

    preview = validate_job_spec(spec.model_copy(update={"allow_large_tournament": True}))
    assert preview.warnings == ("large_tournament",)


def test_job_store_is_private_idempotent_fenced_and_contains_no_secret_columns(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.db"
    store = JobStore(archive)
    assert store.path == derive_jobs_database_path(archive.resolve())
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600

    spec = _mock_spec()
    preview = validate_job_spec(spec)
    first = store.prepare(spec, preview, idempotency_key="prepare-one")
    duplicate = store.prepare(spec, preview, idempotency_key="prepare-one")
    assert duplicate == first

    with pytest.raises(ControlError, match="idempotency_conflict") as prepare_conflict:
        store.prepare(
            _mock_spec(seed="43"),
            validate_job_spec(_mock_spec(seed="43")),
            idempotency_key="prepare-one",
        )
    assert prepare_conflict.value.code == "idempotency_conflict"

    assert store.claim_operation(first.job_id, "start", "start-one") is True
    assert store.claim_operation(first.job_id, "start", "start-one") is False
    with pytest.raises(ControlError, match="idempotency_conflict") as operation_conflict:
        store.claim_operation(first.job_id, "cancel", "start-one")
    assert operation_conflict.value.code == "idempotency_conflict"

    starting = store.transition(
        first.job_id,
        expected=("prepared",),
        status="starting",
        child_pid=123,
    )
    assert starting.status == "starting"
    with pytest.raises(ControlError, match="job_conflict") as stale_transition:
        store.transition(first.job_id, expected=("prepared",), status="running")
    assert stale_transition.value.code == "job_conflict"

    with sqlite3.connect(store.path) as connection:
        columns = {
            row[1]
            for table in ("control_jobs", "control_operations")
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
    normalized_columns = " ".join(sorted(columns)).casefold()
    for forbidden in (
        "api_key",
        "authorization",
        "capability",
        "credential",
        "move",
        "owner_token",
        "password",
        "prompt",
        "secret",
    ):
        assert forbidden not in normalized_columns


def test_control_schema_v1_migrates_old_rows_and_idempotency_to_v2(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.db"
    original_store = JobStore(archive)
    spec = _mock_spec()
    preview = validate_job_spec(spec)
    original = original_store.prepare(
        spec,
        preview,
        idempotency_key="legacy-prepare",
    )
    with sqlite3.connect(original_store.path) as connection:
        raw_payload = json.loads(
            connection.execute(
                "SELECT spec_json FROM control_jobs WHERE job_id = ?",
                (original.job_id,),
            ).fetchone()[0]
        )
        assert raw_payload.pop("resume_championship_id") is None
        legacy_json = json.dumps(
            raw_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            "UPDATE control_jobs SET spec_json = ?, request_digest = ? WHERE job_id = ?",
            (
                legacy_json,
                control.hashlib.sha256(legacy_json.encode()).hexdigest(),
                original.job_id,
            ),
        )
        connection.execute("ALTER TABLE control_jobs DROP COLUMN championship_id")
        connection.execute("PRAGMA user_version=1")

    migrated_store = JobStore(archive)
    migrated = migrated_store.get(original.job_id)
    repeated = migrated_store.prepare(
        spec,
        preview,
        idempotency_key="legacy-prepare",
    )
    with sqlite3.connect(migrated_store.path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(control_jobs)")
        }

    assert version == CONTROL_SCHEMA_VERSION == 2
    assert "championship_id" in columns
    assert migrated.championship_id is None
    assert migrated.spec == spec
    assert repeated == migrated


def test_control_schema_v1_rejects_unknown_extensions_without_partial_migration(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.db"
    original = JobStore(archive)
    with sqlite3.connect(original.path) as connection:
        connection.execute("ALTER TABLE control_jobs DROP COLUMN championship_id")
        connection.execute("ALTER TABLE control_jobs ADD COLUMN unknown_extension TEXT")
        connection.execute("PRAGMA user_version=1")

    with pytest.raises(ControlError, match="control_unavailable"):
        JobStore(archive)

    with sqlite3.connect(original.path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(control_jobs)")
        }
    assert version == 1
    assert "unknown_extension" in columns
    assert "championship_id" not in columns


def test_job_store_rejects_a_symlink_without_touching_its_target(tmp_path: Path) -> None:
    archive = tmp_path / "archive.db"
    jobs = derive_jobs_database_path(archive)
    target = tmp_path / "target.txt"
    target.write_text("must stay unchanged", encoding="utf-8")
    jobs.symlink_to(target)

    with pytest.raises(ControlError, match="control_unavailable") as rejected:
        JobStore(archive)

    assert rejected.value.code == "control_unavailable"
    assert target.read_text(encoding="utf-8") == "must stay unchanged"


def test_job_store_rejects_path_replacement_after_construction(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "archive.db")
    original = tmp_path / "original-jobs.db"
    target = tmp_path / "replacement-target.txt"
    target.write_text("must stay unchanged", encoding="utf-8")
    store.path.rename(original)
    store.path.symlink_to(target)

    with pytest.raises(ControlError, match="control_unavailable") as rejected:
        store.list()

    assert rejected.value.code == "control_unavailable"
    assert target.read_text(encoding="utf-8") == "must stay unchanged"
    assert original.is_file()


def test_job_store_creation_race_is_a_stable_control_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs_path = derive_jobs_database_path((tmp_path / "archive.db").resolve())
    real_open = os.open
    attempts = 0

    def racing_open(path: str | bytes | Path, flags: int, mode: int = 0o777) -> int:
        nonlocal attempts
        if Path(path) == jobs_path:
            attempts += 1
            if attempts == 1:
                raise FileNotFoundError("simulated missing jobs file")
            if attempts == 2:
                raise FileExistsError("simulated competing creator")
        return real_open(path, flags, mode)

    monkeypatch.setattr(control.os, "open", racing_open)

    with pytest.raises(ControlError, match="control_unavailable") as rejected:
        JobStore(tmp_path / "archive.db")

    assert rejected.value.code == "control_unavailable"


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory modes")
def test_job_store_rejects_a_shared_nonsticky_writable_parent(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o777)
    shared.chmod(0o777)

    with pytest.raises(ControlError, match="control_unavailable") as rejected:
        JobStore(shared / "archive.db")

    assert rejected.value.code == "control_unavailable"


def test_job_store_corrupt_row_shape_is_control_unavailable(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "archive.db")
    job = store.prepare(
        _mock_spec(),
        validate_job_spec(_mock_spec()),
        idempotency_key="prepare-corrupt-row-shape",
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute("ALTER TABLE control_jobs DROP COLUMN spec_json")

    with pytest.raises(ControlError, match="control_unavailable") as rejected:
        store.get(job.job_id)

    assert rejected.value.code == "control_unavailable"


def test_job_store_never_persists_provider_credentials_or_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ProviderProfile(
        profile_id="private-profile",
        provider="openai",
        default_model="fixed-model",
        base_url="https://private-provider.example/v1",
        api_key_env="PRIVATE_CONTROL_API_KEY",
        display_name="Private profile",
    )
    credential_marker = "provider-value-that-must-not-reach-jobs-db"
    monkeypatch.setenv(profile.api_key_env or "", credential_marker)
    monkeypatch.setattr(control, "load_profiles", lambda: {profile.profile_id: profile})
    digest_inputs: list[bytes] = []
    real_sha256 = control.hashlib.sha256

    def capture_digest_input(value: bytes = b""):
        digest_inputs.append(value)
        return real_sha256(value)

    monkeypatch.setattr(control.hashlib, "sha256", capture_digest_input)
    spec = _profile_spec(profile.profile_id)
    store = JobStore(tmp_path / "archive.db")

    preview = validate_job_spec(spec)
    assert len(preview.prepared_profiles) == 1
    prepared_profile = preview.prepared_profiles[0]
    assert prepared_profile.model_dump(mode="json") == {
        "configuration_digest": profile_configuration_digest(profile),
        "default_model": "fixed-model",
        "display_name": "Private profile",
        "effective_models": ["fixed-model"],
        "profile_id": "private-profile",
        "provider": "openai",
    }

    job = store.prepare(
        spec,
        preview,
        idempotency_key="provider-secret-scan",
    )
    assert job.preview.prepared_profiles == (prepared_profile,)

    stored = store.path.read_bytes()
    public_snapshot = job.model_dump_json()
    assert prepared_profile.configuration_digest.encode("ascii") in stored
    assert digest_inputs
    assert all(credential_marker.encode() not in item for item in digest_inputs)
    assert any((profile.api_key_env or "").encode() in item for item in digest_inputs)
    assert any((profile.base_url or "").encode() in item for item in digest_inputs)
    for forbidden in (
        credential_marker,
        profile.api_key_env,
        profile.base_url,
    ):
        assert forbidden is not None
        assert forbidden.encode() not in stored
        assert forbidden not in public_snapshot


def test_stale_prepared_job_expires_and_releases_capacity(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "archive.db")
    first = store.prepare(
        _mock_spec(),
        validate_job_spec(_mock_spec()),
        idempotency_key="stale-prepared",
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE control_jobs SET created_at = ? WHERE job_id = ?",
            ("2000-01-01T00:00:00.000000Z", first.job_id),
        )

    replacement = store.prepare(
        _mock_spec(seed="43"),
        validate_job_spec(_mock_spec(seed="43")),
        idempotency_key="replacement-after-stale",
    )

    assert replacement.status == "prepared"
    assert store.get(first.job_id).status == "cancelled"
