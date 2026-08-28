"""Independent security tests for the local Web control worker boundary."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import textwrap
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from llmolympic import control, control_runner
from llmolympic.config import ProviderProfile, ProviderTokenPrice
from llmolympic.control import (
    ControlBudgetSpec,
    ControlError,
    ControlJob,
    ControlJobSpec,
    ControlPlayerSpec,
    ControlPreparedProfile,
    ControlPreview,
    JobStore,
    profile_configuration_digest,
    validate_job_spec,
)
from llmolympic.control_runner import ControlJobManager
from llmolympic.core.championship import play_championship
from llmolympic.core.player import LLMPlayer
from llmolympic.core.storage import SCHEMA_VERSION, SQLiteStore
from llmolympic.games import create_game
from llmolympic.providers.mock import MockProvider


def _spec(*, human_name: str | None = None) -> ControlJobSpec:
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
        seed="42",
        budget=ControlBudgetSpec(
            max_provider_calls="16",
            max_input_tokens="32000",
            max_output_tokens_per_call="1024",
            max_total_output_tokens="8192",
        ),
    )


def _championship_spec() -> ControlJobSpec:
    return ControlJobSpec(
        mode="championship",
        game="math_quiz",
        players=tuple(
            ControlPlayerSpec(kind="mock", strategy=strategy)
            for strategy in ("random", "fixed", "illegal", "balanced")
        ),
        rounds=1,
        seed="37",
    )


def _prepare(store: JobStore, spec: ControlJobSpec, *, key: str) -> ControlJob:
    return store.prepare(spec, validate_job_spec(spec), idempotency_key=key)


def _write_fake_cli(root: Path, source: str) -> Path:
    package = root / "llmolympic"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        textwrap.dedent(source),
        encoding="utf-8",
    )
    return root


def _write_match_archive(path: Path, match_id: str) -> bytes:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE matches (match_id TEXT PRIMARY KEY)")
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.execute(
            "INSERT INTO matches (match_id) VALUES (?)",
            (match_id,),
        )
    return path.read_bytes()


def _write_championship_archive(
    path: Path,
    championship_id: str,
) -> tuple[str, ...]:
    players = [
        LLMPlayer(
            name=f"Mock {strategy}",
            provider=MockProvider(strategy=strategy),
            model=strategy,
        )
        for strategy in ("random", "fixed", "illegal", "balanced")
    ]
    archive = asyncio.run(
        play_championship(
            create_game("math_quiz", mode="play", rounds=1),
            players,
            seed=37,
            championship_id=championship_id,
        )
    )
    SQLiteStore(path).save_championship(archive, rating_source="engine")
    return tuple(
        leg.match_id
        for pairing in archive.pairings
        for leg in pairing.series.legs
    )


async def _wait_for_job(
    manager: ControlJobManager,
    job_id: str,
    *,
    statuses: set[str],
    timeout: float = 3.0,
) -> ControlJob:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        job = manager.public_job(job_id)
        if job.status in statuses:
            return job
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"job stayed in {job.status!r}")
        await asyncio.sleep(0.01)


def test_runner_executes_a_fixed_argv_without_shell_or_untrusted_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JobStore(tmp_path / "archive path; untouched.db")
    dangerous_name = "human; $(touch never) `id` --pretend-option"
    job = _prepare(store, _spec(human_name=dangerous_name), key="prepare-fixed-argv")
    captured: dict[str, object] = {}

    async def reject_spawn(*argv: str, **kwargs: object):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        raise OSError("deliberate spawn failure")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", reject_spawn)
    manager = ControlJobManager(
        store,
        python_executable=sys.executable,
        working_directory=tmp_path,
        environment={
            "ADMIN_TOKEN": "must-not-be-inherited",
            "LANG": "C",
            "UNTRUSTED_ROUTE": "https://evil.example",
        },
    )

    async def exercise() -> None:
        with pytest.raises(ControlError, match="worker_start_failed"):
            await manager.start(
                job.job_id,
                idempotency_key="start-fixed-argv",
                web_base_url="http://localhost:8765",
            )
        await manager.shutdown()

    asyncio.run(exercise())

    argv = captured["argv"]
    kwargs = captured["kwargs"]
    assert isinstance(argv, tuple)
    assert isinstance(kwargs, dict)
    assert argv[:4] == (sys.executable, "-m", "llmolympic", "play")
    players_index = argv.index("--players")
    assert argv[players_index + 1] == f"human:{dangerous_name},mock:fixed"
    assert "shell" not in kwargs
    assert kwargs.get("start_new_session") is (os.name == "posix")
    assert kwargs["stdin"] is asyncio.subprocess.PIPE
    assert kwargs["stdout"] is asyncio.subprocess.PIPE
    assert kwargs["stderr"] is asyncio.subprocess.PIPE
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert environment["LANG"] == "C"
    assert environment["LLMOLYMPIC_CONTROL_JOB_ID"] == job.job_id
    assert environment["LLMOLYMPIC_CONTROL_PARENT_WATCHDOG"] == "1"
    assert environment["LLMOLYMPIC_CONTROL_PROFILE_SNAPSHOT"] == "{}"
    assert len(environment["LLMOLYMPIC_CONTROL_PROTOCOL_TOKEN"]) == 43
    assert "ADMIN_TOKEN" not in environment
    assert "UNTRUSTED_ROUTE" not in environment
    assert not (tmp_path / "never").exists()
    assert store.get(job.job_id).status == "failed"


def test_runner_refuses_a_profile_changed_after_prepare_without_claiming_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared_profile = ProviderProfile(
        profile_id="trusted",
        provider="ollama",
        default_model="confirmed-model",
        base_url="http://127.0.0.1:11434/v1",
        display_name="Trusted local profile",
    )
    changed_profile = replace(
        prepared_profile,
        default_model="changed-after-confirmation",
    )
    monkeypatch.setattr(control, "load_profiles", lambda: {"trusted": prepared_profile})
    spec = ControlJobSpec(
        mode="play",
        game="math_quiz",
        players=(
            ControlPlayerSpec(kind="profile", profile_id="trusted"),
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
    store = JobStore(tmp_path / "archive.db")
    job = _prepare(store, spec, key="prepare-profile-snapshot")
    monkeypatch.setattr(
        control_runner,
        "load_profiles",
        lambda: {"trusted": changed_profile},
    )

    async def exercise() -> None:
        manager = ControlJobManager(store, environment={})
        try:
            with pytest.raises(ControlError, match="worker_start_failed"):
                await manager.start(
                    job.job_id,
                    idempotency_key="start-profile-snapshot",
                    web_base_url="http://localhost:8765",
                )
        finally:
            await manager.shutdown()

    asyncio.run(exercise())

    assert store.get(job.job_id).status == "prepared"
    with sqlite3.connect(store.path) as connection:
        operations = connection.execute(
            "SELECT count(*) FROM control_operations WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()[0]
    assert operations == 0


def test_runtime_profile_credential_is_scoped_to_used_child_and_not_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensitive_marker = "runtime-child-secret-sentinel"
    profile = ProviderProfile(
        profile_id="runtime",
        provider="openai",
        default_model="fixed-model",
        base_url="https://provider.example/v1",
        api_key_env="RUNTIME_PROFILE_KEY",
    )
    unused = ProviderProfile(
        profile_id="unused",
        provider="openai",
        default_model="unused-model",
        base_url="https://unused.example/v1",
        api_key_env="UNUSED_PROFILE_KEY",
    )
    profiles = {profile.profile_id: profile, unused.profile_id: unused}
    monkeypatch.setattr(control, "load_profiles", lambda: profiles)
    monkeypatch.setattr(control_runner, "load_profiles", lambda: profiles)
    monkeypatch.setattr(
        control,
        "load_provider_pricing",
        lambda: {
            "profile:runtime:fixed-model": ProviderTokenPrice(
                Decimal(0),
                Decimal(0),
            )
        },
    )
    monkeypatch.setenv(profile.api_key_env, "prepare-only-placeholder")
    spec = ControlJobSpec(
        mode="play",
        game="math_quiz",
        players=(
            ControlPlayerSpec(kind="profile", profile_id=profile.profile_id),
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
    store = JobStore(tmp_path / "runtime-credential.db")
    job = _prepare(store, spec, key="prepare-runtime-credential")
    monkeypatch.delenv(profile.api_key_env)
    captured: dict[str, object] = {}

    async def reject_spawn(*argv: str, **kwargs: object):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        raise OSError("deliberate spawn failure")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", reject_spawn)

    async def exercise() -> None:
        manager = ControlJobManager(
            store,
            environment={"UNUSED_PROFILE_KEY": "ambient-unused-secret"},
        )
        try:
            await manager.set_profile_credential(profile.profile_id, sensitive_marker)
            with pytest.raises(ControlError, match="worker_start_failed"):
                await manager.start(
                    job.job_id,
                    idempotency_key="start-runtime-credential",
                    web_base_url="http://localhost:8765",
                )
        finally:
            await manager.shutdown()
        assert sensitive_marker not in repr(vars(manager))

    asyncio.run(exercise())

    argv = captured["argv"]
    kwargs = captured["kwargs"]
    assert isinstance(argv, tuple)
    assert isinstance(kwargs, dict)
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert environment[profile.api_key_env] == sensitive_marker
    assert unused.api_key_env not in environment
    assert sensitive_marker not in repr(argv)
    assert sensitive_marker.encode() not in store.path.read_bytes()


def test_clearing_runtime_profile_credential_makes_later_start_fail_before_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ProviderProfile(
        profile_id="runtime-clear",
        provider="openai",
        default_model="fixed-model",
        base_url="https://provider.example/v1",
        api_key_env="RUNTIME_CLEAR_KEY",
    )
    monkeypatch.setattr(control, "load_profiles", lambda: {profile.profile_id: profile})
    monkeypatch.setattr(
        control_runner, "load_profiles", lambda: {profile.profile_id: profile}
    )
    monkeypatch.setenv(profile.api_key_env, "prepare-only-placeholder")
    spec = ControlJobSpec(
        mode="play",
        game="math_quiz",
        players=(
            ControlPlayerSpec(kind="profile", profile_id=profile.profile_id),
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
    store = JobStore(tmp_path / "runtime-clear.db")
    job = _prepare(store, spec, key="prepare-runtime-clear")
    monkeypatch.delenv(profile.api_key_env)

    async def exercise() -> None:
        manager = ControlJobManager(store, environment={})
        try:
            await manager.set_profile_credential(profile.profile_id, "cleared-secret")
            await manager.clear_profile_credential(profile.profile_id)
            with pytest.raises(ControlError, match="worker_start_failed"):
                await manager.start(
                    job.job_id,
                    idempotency_key="start-after-clear",
                    web_base_url="http://localhost:8765",
                )
        finally:
            await manager.shutdown()

    asyncio.run(exercise())

    assert store.get(job.job_id).status == "prepared"
    with sqlite3.connect(store.path) as connection:
        claimed = connection.execute(
            "SELECT count(*) FROM control_operations WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()[0]
    assert claimed == 0
    assert b"cleared-secret" not in store.path.read_bytes()


@pytest.mark.parametrize("profile_change", ["removed", "invalid"])
def test_clearing_stored_credential_succeeds_after_profile_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile_change: str,
) -> None:
    profile = ProviderProfile(
        profile_id="runtime-removed",
        provider="openai",
        default_model="fixed-model",
        base_url="https://provider.example/v1",
        api_key_env="RUNTIME_REMOVED_KEY",
    )
    profiles = {profile.profile_id: profile}
    monkeypatch.setattr(control_runner, "load_profiles", lambda: profiles)
    manager = ControlJobManager(JobStore(tmp_path / "runtime-removed.db"), environment={})

    async def exercise() -> None:
        try:
            await manager.set_profile_credential(profile.profile_id, "removed-secret")
            if profile_change == "removed":
                profiles.clear()
            else:
                profiles[profile.profile_id] = replace(
                    profile,
                    provider="ollama",
                    api_key_env=None,
                )
            await manager.clear_profile_credential(profile.profile_id)
            assert not manager._runtime_profile_credentials
            with pytest.raises(ControlError, match="profile_unavailable"):
                await manager.clear_profile_credential(profile.profile_id)
        finally:
            await manager.shutdown()

    asyncio.run(exercise())


def test_runtime_profile_credential_is_invalidated_by_profile_configuration_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = ProviderProfile(
        profile_id="runtime-drift",
        provider="openai",
        default_model="fixed-model",
        base_url="https://provider.example/v1",
        api_key_env="RUNTIME_DRIFT_KEY",
    )
    profiles = {original.profile_id: original}
    monkeypatch.setattr(control_runner, "load_profiles", lambda: profiles)
    manager = ControlJobManager(JobStore(tmp_path / "runtime-drift.db"), environment={})

    async def exercise() -> None:
        try:
            await manager.set_profile_credential(original.profile_id, "drift-secret")
            assert manager.profile_credential_ready(original)
            changed = ProviderProfile(
                profile_id=original.profile_id,
                provider="openai",
                default_model=original.default_model,
                base_url="https://different-provider.example/v1",
                api_key_env=original.api_key_env,
            )
            profiles[original.profile_id] = changed
            assert not manager.profile_credential_ready(changed)
            assert original.profile_id not in manager._runtime_profile_credentials
        finally:
            await manager.shutdown()

    asyncio.run(exercise())


def test_runtime_profile_credential_rejects_process_control_environment_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsafe = ProviderProfile(
        profile_id="runtime-unsafe-env",
        provider="openai",
        default_model="fixed-model",
        base_url="https://provider.example/v1",
        api_key_env="PYTHONPATH",
    )
    monkeypatch.setattr(
        control_runner,
        "load_profiles",
        lambda: {unsafe.profile_id: unsafe},
    )
    manager = ControlJobManager(
        JobStore(tmp_path / "runtime-unsafe-env.db"),
        environment={"PYTHONPATH": "trusted-parent-path"},
    )

    async def exercise() -> None:
        try:
            assert not manager.profile_credential_ready(unsafe)
            with pytest.raises(ControlError, match="profile_unavailable"):
                await manager.set_profile_credential(unsafe.profile_id, "attacker-path")
            assert not manager._runtime_profile_credentials
        finally:
            await manager.shutdown()

    asyncio.run(exercise())


def test_runner_binds_resume_profiles_from_the_frozen_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ProviderProfile(
        profile_id="resume-profile",
        provider="ollama",
        default_model="confirmed-model",
        base_url="http://127.0.0.1:11434/v1",
    )
    prepared_profile = ControlPreparedProfile(
        profile_id=profile.profile_id,
        display_name=profile.profile_id,
        provider="ollama",
        default_model="confirmed-model",
        effective_models=("frozen-model",),
        configuration_digest=profile_configuration_digest(profile),
    )
    spec = ControlJobSpec(
        mode="round_robin",
        resume_tournament_id="resume-profile-tournament",
    )
    preview = ControlPreview(
        player_count=3,
        human_count=0,
        match_count=6,
        pairing_count=3,
        rated=True,
        requires_provider_budget=True,
        frozen_game="math_quiz",
        frozen_players=("Profile", "Mock A", "Mock B"),
        frozen_rounds=1,
        frozen_seed="19",
        uses_frozen_budget=True,
        prepared_profiles=(prepared_profile,),
        warnings=("resume_uses_frozen_configuration",),
    )
    store = JobStore(tmp_path / "archive.db")
    job = store.prepare(spec, preview, idempotency_key="prepare-resume-profile")
    monkeypatch.setattr(
        control_runner,
        "load_profiles",
        lambda: {
            profile.profile_id: replace(profile, default_model="changed-model")
        },
    )

    async def exercise() -> None:
        manager = ControlJobManager(store, environment={})
        try:
            with pytest.raises(ControlError, match="worker_start_failed"):
                await manager.start(
                    job.job_id,
                    idempotency_key="start-resume-profile",
                    web_base_url="http://localhost:8765",
                )
        finally:
            await manager.shutdown()

    asyncio.run(exercise())

    assert store.get(job.job_id).status == "prepared"


def test_authenticated_protocol_is_bounded_and_human_start_returns_its_link(
    tmp_path: Path,
) -> None:
    fake_root = _write_fake_cli(
        tmp_path / "fake-cli",
        r'''
        import base64
        import json
        import os
        import sys
        import time

        prefix = "@@LLMOLYMPIC_CONTROL_V1:"
        token = os.environ["LLMOLYMPIC_CONTROL_PROTOCOL_TOKEN"]
        job_id = os.environ["LLMOLYMPIC_CONTROL_JOB_ID"]

        def emit(payload, *, supplied_token=token):
            payload = {"job_id": job_id, **payload}
            raw = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
            print(f"{prefix}{supplied_token}:{encoded}", flush=True)

        print("ordinary-stdout-private-marker", flush=True)
        print("ordinary-stderr-private-marker", file=sys.stderr, flush=True)
        emit(
            {
                "type": "completed",
                "final_kind": "match",
                "final_id": "forged-match",
                "final_match_ids": ["forged-match"],
            },
            supplied_token="z" * 43,
        )
        emit(
            {
                "type": "participation",
                "player_name": "浏览器选手",
                "url": "http://localhost:8765/participate/session/seat"
                "#capability=" + "c" * 43,
            }
        )
        time.sleep(0.15)
        emit(
            {
                "type": "finalizing",
                "final_kind": "match",
                "final_id": "real-match",
                "final_match_ids": ["real-match"],
            }
        )
        emit(
            {
                "type": "completed",
                "final_kind": "match",
                "final_id": "real-match",
                "final_match_ids": ["real-match"],
            }
        )
        ''',
    )
    store = JobStore(tmp_path / "archive.db")
    prepared = _prepare(
        store,
        _spec(human_name="浏览器选手"),
        key="prepare-human-protocol",
    )
    manager = ControlJobManager(
        store,
        python_executable=sys.executable,
        working_directory=fake_root,
        environment={},
    )

    async def exercise() -> tuple[ControlJob, ControlJob]:
        try:
            started = await manager.start(
                prepared.job_id,
                idempotency_key="start-human-protocol",
                web_base_url="http://localhost:8765",
            )
            completed = await _wait_for_job(
                manager,
                prepared.job_id,
                statuses={"completed", "failed", "interrupted"},
            )
            return started, completed
        finally:
            await manager.shutdown()

    started, completed = asyncio.run(exercise())

    assert len(started.participation_links) == 1
    assert started.participation_links[0].player_name == "浏览器选手"
    assert started.participation_links[0].url.endswith("#capability=" + "c" * 43)
    assert completed.status == "completed"
    assert completed.final_id == "real-match"
    assert completed.final_match_ids == ("real-match",)
    serialized = completed.model_dump_json()
    assert "forged-match" not in serialized
    assert "ordinary-stdout-private-marker" not in serialized
    assert "ordinary-stderr-private-marker" not in serialized
    raw_jobs = store.path.read_bytes()
    for forbidden in (
        b"ordinary-stdout-private-marker",
        b"ordinary-stderr-private-marker",
        b"#capability=ccccccccccccccccccccccccccccccccccccccccccc",
    ):
        assert forbidden not in raw_jobs


def test_forged_malformed_and_oversized_protocol_output_fails_closed(
    tmp_path: Path,
) -> None:
    fake_root = _write_fake_cli(
        tmp_path / "fake-cli",
        r'''
        import base64
        import json
        import os
        import sys

        prefix = "@@LLMOLYMPIC_CONTROL_V1:"
        token = os.environ["LLMOLYMPIC_CONTROL_PROTOCOL_TOKEN"]
        job_id = os.environ["LLMOLYMPIC_CONTROL_JOB_ID"]

        def encoded(payload):
            raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

        forged = {
            "type": "completed",
            "job_id": job_id,
            "final_kind": "match",
            "final_id": "forged-match",
            "final_match_ids": ["forged-match"],
        }
        print(f"{prefix}{'x' * 43}:{encoded(forged)}", flush=True)
        forged["raw_credential"] = "must-not-leak-from-child-output"
        print(f"{prefix}{token}:{encoded(forged)}", flush=True)
        oversized = prefix.encode() + token.encode() + b":" + b"A" * 70000 + b"\n"
        sys.stdout.buffer.write(oversized)
        sys.stdout.buffer.flush()
        print("raw-stdout-must-not-leak", flush=True)
        print("raw-stderr-must-not-leak", file=sys.stderr, flush=True)
        ''',
    )
    store = JobStore(tmp_path / "archive.db")
    prepared = _prepare(store, _spec(), key="prepare-forged-protocol")
    manager = ControlJobManager(
        store,
        python_executable=sys.executable,
        working_directory=fake_root,
        environment={},
    )

    async def exercise() -> ControlJob:
        try:
            await manager.start(
                prepared.job_id,
                idempotency_key="start-forged-protocol",
                web_base_url="http://localhost:8765",
            )
            return await _wait_for_job(
                manager,
                prepared.job_id,
                statuses={"completed", "failed", "interrupted"},
            )
        finally:
            await manager.shutdown()

    final = asyncio.run(exercise())

    assert final.status == "failed"
    assert final.failure_code == "worker_protocol_incomplete"
    assert final.final_id is None
    serialized = final.model_dump_json()
    raw_jobs = store.path.read_bytes()
    for forbidden in (
        "forged-match",
        "must-not-leak-from-child-output",
        "raw-stdout-must-not-leak",
        "raw-stderr-must-not-leak",
    ):
        assert forbidden not in serialized
        assert forbidden.encode() not in raw_jobs


def test_protocol_frames_require_the_exact_managed_job_id(tmp_path: Path) -> None:
    fake_root = _write_fake_cli(
        tmp_path / "fake-cli",
        r'''
        import base64
        import json
        import os

        prefix = "@@LLMOLYMPIC_CONTROL_V1:"
        token = os.environ["LLMOLYMPIC_CONTROL_PROTOCOL_TOKEN"]
        job_id = os.environ["LLMOLYMPIC_CONTROL_JOB_ID"]

        def emit(payload):
            raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
            print(f"{prefix}{token}:{encoded}", flush=True)

        emit(
            {
                "type": "completed",
                "final_kind": "match",
                "final_id": "missing-job-id-final",
                "final_match_ids": ["missing-job-id-final"],
            }
        )
        emit(
            {
                "type": "completed",
                "job_id": job_id + "-other",
                "final_kind": "match",
                "final_id": "wrong-job-id-final",
                "final_match_ids": ["wrong-job-id-final"],
            }
        )
        ''',
    )
    store = JobStore(tmp_path / "archive.db")
    prepared = _prepare(store, _spec(), key="prepare-job-binding")
    manager = ControlJobManager(
        store,
        python_executable=sys.executable,
        working_directory=fake_root,
        environment={},
    )

    async def exercise() -> ControlJob:
        try:
            await manager.start(
                prepared.job_id,
                idempotency_key="start-job-binding",
                web_base_url="http://localhost:8765",
            )
            return await _wait_for_job(
                manager,
                prepared.job_id,
                statuses={"completed", "failed", "interrupted"},
            )
        finally:
            await manager.shutdown()

    final = asyncio.run(exercise())

    assert final.status == "failed"
    assert final.failure_code == "worker_protocol_incomplete"
    assert final.final_kind is None
    assert final.final_id is None
    raw_jobs = store.path.read_bytes()
    assert b"missing-job-id-final" not in raw_jobs
    assert b"wrong-job-id-final" not in raw_jobs


def test_invalid_final_shape_cannot_poison_the_persisted_job(tmp_path: Path) -> None:
    fake_root = _write_fake_cli(
        tmp_path / "fake-cli",
        r'''
        import base64
        import json
        import os

        prefix = "@@LLMOLYMPIC_CONTROL_V1:"
        token = os.environ["LLMOLYMPIC_CONTROL_PROTOCOL_TOKEN"]
        job_id = os.environ["LLMOLYMPIC_CONTROL_JOB_ID"]

        def emit(payload):
            raw = json.dumps(
                {"job_id": job_id, **payload},
                separators=(",", ":"),
            ).encode("utf-8")
            encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
            print(f"{prefix}{token}:{encoded}", flush=True)

        emit({"type": "running"})
        emit(
            {
                "type": "finalizing",
                "final_kind": "series",
                "final_id": "wrong-kind-series",
                "final_match_ids": ["wrong-kind-leg-1", "wrong-kind-leg-2"],
            }
        )
        emit(
            {
                "type": "completed",
                "final_kind": "match",
                "final_id": "mismatched-final-id",
                "final_match_ids": ["different-match-id"],
            }
        )
        ''',
    )
    store = JobStore(tmp_path / "archive.db")
    prepared = _prepare(store, _spec(), key="prepare-invalid-final-shape")
    manager = ControlJobManager(
        store,
        python_executable=sys.executable,
        working_directory=fake_root,
        environment={},
    )

    async def exercise() -> ControlJob:
        try:
            await manager.start(
                prepared.job_id,
                idempotency_key="start-invalid-final-shape",
                web_base_url="http://localhost:8765",
            )
            return await _wait_for_job(
                manager,
                prepared.job_id,
                statuses={"completed", "failed", "interrupted"},
            )
        finally:
            await manager.shutdown()

    final = asyncio.run(exercise())

    assert final.status == "failed"
    assert final.failure_code == "worker_protocol_incomplete"
    assert final.final_kind is None
    assert final.final_id is None
    assert final.final_match_ids == ()
    raw_jobs = store.path.read_bytes()
    for forbidden in (
        b"wrong-kind-series",
        b"wrong-kind-leg-1",
        b"wrong-kind-leg-2",
        b"mismatched-final-id",
        b"different-match-id",
    ):
        assert forbidden not in raw_jobs


def test_championship_protocol_and_archive_reconciliation_require_canonical_matches(
    tmp_path: Path,
) -> None:
    archive_database = tmp_path / "championship.db"
    championship_id = "managed-championship"
    match_ids = _write_championship_archive(archive_database, championship_id)
    assert len(match_ids) == 6
    archive_before = archive_database.read_bytes()
    fake_root = _write_fake_cli(
        tmp_path / "fake-championship-cli",
        f'''
        import base64
        import json
        import os

        prefix = "@@LLMOLYMPIC_CONTROL_V1:"
        token = os.environ["LLMOLYMPIC_CONTROL_PROTOCOL_TOKEN"]
        job_id = os.environ["LLMOLYMPIC_CONTROL_JOB_ID"]
        championship_id = {championship_id!r}
        match_ids = {list(match_ids)!r}

        def emit(payload):
            raw = json.dumps(
                {{"job_id": job_id, **payload}},
                separators=(",", ":"),
            ).encode("utf-8")
            encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
            print(f"{{prefix}}{{token}}:{{encoded}}", flush=True)

        emit({{"type": "running", "tournament_id": championship_id}})
        emit({{"type": "running", "championship_id": championship_id}})
        emit(
            {{
                "type": "finalizing",
                "final_kind": "championship",
                "final_id": championship_id,
                "final_match_ids": match_ids,
            }}
        )
        ''',
    )
    store = JobStore(archive_database)
    prepared = _prepare(
        store,
        _championship_spec(),
        key="prepare-championship-protocol",
    )
    manager = ControlJobManager(
        store,
        python_executable=sys.executable,
        working_directory=fake_root,
        environment={},
    )

    async def exercise() -> tuple[ControlJob, ControlJob]:
        try:
            started = await manager.start(
                prepared.job_id,
                idempotency_key="start-championship-protocol",
                web_base_url="http://localhost:8765",
            )
            final = await _wait_for_job(
                manager,
                prepared.job_id,
                statuses={"completed", "failed", "interrupted"},
            )
            return started, final
        finally:
            await manager.shutdown()

    started, final = asyncio.run(exercise())

    assert started.championship_id == championship_id
    assert started.tournament_id is None
    assert final.status == "completed"
    assert final.final_kind == "championship"
    assert final.final_id == championship_id
    assert final.final_match_ids == match_ids
    assert archive_database.read_bytes() == archive_before

    forged_payload = final.model_dump(mode="json")
    forged_payload["status"] = "finalizing"
    forged_payload["finished_at"] = None
    forged_payload["final_match_ids"] = list(reversed(match_ids))
    forged = ControlJob.model_validate(forged_payload)
    assert (
        control_runner._formal_archive_status(forged, archive_database)
        == "absent"
    )


@pytest.mark.parametrize(
    ("archive_exists", "expected_status", "expected_failure"),
    [
        (True, "completed", None),
        (False, "failed", "worker_protocol_incomplete"),
    ],
)
def test_finalizing_reconciles_only_against_a_matching_formal_archive(
    tmp_path: Path,
    archive_exists: bool,
    expected_status: str,
    expected_failure: str | None,
) -> None:
    archive_database = tmp_path / "archive.db"
    with sqlite3.connect(archive_database) as connection:
        connection.execute("CREATE TABLE matches (match_id TEXT PRIMARY KEY)")
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        if archive_exists:
            connection.execute(
                "INSERT INTO matches (match_id) VALUES (?)",
                ("reconciled-match",),
            )
    archive_before = archive_database.read_bytes()
    fake_root = _write_fake_cli(
        tmp_path / "fake-cli",
        r'''
        import base64
        import json
        import os

        prefix = "@@LLMOLYMPIC_CONTROL_V1:"
        token = os.environ["LLMOLYMPIC_CONTROL_PROTOCOL_TOKEN"]
        job_id = os.environ["LLMOLYMPIC_CONTROL_JOB_ID"]

        def emit(payload):
            raw = json.dumps(
                {"job_id": job_id, **payload},
                separators=(",", ":"),
            ).encode("utf-8")
            encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
            print(f"{prefix}{token}:{encoded}", flush=True)

        emit({"type": "running"})
        emit(
            {
                "type": "finalizing",
                "final_kind": "match",
                "final_id": "reconciled-match",
                "final_match_ids": ["reconciled-match"],
            }
        )
        ''',
    )
    store = JobStore(archive_database)
    prepared = _prepare(store, _spec(), key=f"prepare-reconcile-{archive_exists}")
    manager = ControlJobManager(
        store,
        python_executable=sys.executable,
        working_directory=fake_root,
        environment={},
    )

    async def exercise() -> ControlJob:
        try:
            await manager.start(
                prepared.job_id,
                idempotency_key=f"start-reconcile-{archive_exists}",
                web_base_url="http://localhost:8765",
            )
            return await _wait_for_job(
                manager,
                prepared.job_id,
                statuses={"completed", "failed", "interrupted"},
            )
        finally:
            await manager.shutdown()

    final = asyncio.run(exercise())

    assert final.status == expected_status
    assert final.failure_code == expected_failure
    assert final.final_id == "reconciled-match"
    assert final.final_match_ids == ("reconciled-match",)
    assert archive_database.read_bytes() == archive_before


def test_locked_archive_remains_recoverable_and_completes_after_unlock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_database = tmp_path / "archive.db"
    archive_before = _write_match_archive(archive_database, "locked-match")
    fake_root = _write_fake_cli(
        tmp_path / "fake-cli",
        r'''
        import base64
        import json
        import os

        prefix = "@@LLMOLYMPIC_CONTROL_V1:"
        token = os.environ["LLMOLYMPIC_CONTROL_PROTOCOL_TOKEN"]
        job_id = os.environ["LLMOLYMPIC_CONTROL_JOB_ID"]

        def emit(payload):
            raw = json.dumps(
                {"job_id": job_id, **payload},
                separators=(",", ":"),
            ).encode("utf-8")
            encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
            print(f"{prefix}{token}:{encoded}", flush=True)

        emit({"type": "running"})
        emit(
            {
                "type": "finalizing",
                "final_kind": "match",
                "final_id": "locked-match",
                "final_match_ids": ["locked-match"],
            }
        )
        ''',
    )
    store = JobStore(archive_database)
    prepared = _prepare(store, _spec(), key="prepare-locked-archive")
    manager = ControlJobManager(
        store,
        python_executable=sys.executable,
        working_directory=fake_root,
        environment={},
    )
    monkeypatch.setattr(control_runner, "_ARCHIVE_RECONCILE_ATTEMPTS", 2)
    monkeypatch.setattr(control_runner, "_ARCHIVE_RECONCILE_RETRY_SECONDS", 0.001)
    monkeypatch.setattr(control_runner, "_ARCHIVE_SQLITE_TIMEOUT_SECONDS", 0.01)
    blocker = sqlite3.connect(archive_database, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")

    async def exercise() -> ControlJob:
        try:
            await manager.start(
                prepared.job_id,
                idempotency_key="start-locked-archive",
                web_base_url="http://localhost:8765",
            )
            return await _wait_for_job(
                manager,
                prepared.job_id,
                statuses={"completed", "failed", "interrupted"},
            )
        finally:
            await manager.shutdown()

    try:
        unavailable = asyncio.run(exercise())
        assert unavailable.status == "interrupted"
        assert unavailable.failure_code is None
        assert unavailable.final_id == "locked-match"
        assert unavailable.final_match_ids == ("locked-match",)
    finally:
        blocker.rollback()
        blocker.close()

    recovered_manager = ControlJobManager(store, environment={})
    recovered = store.get(prepared.job_id)
    asyncio.run(recovered_manager.shutdown())

    assert recovered.status == "completed"
    assert recovered.failure_code is None
    assert archive_database.read_bytes() == archive_before


def test_startup_archive_lock_does_not_permanently_fail_finalizing_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_database = tmp_path / "archive.db"
    archive_before = _write_match_archive(archive_database, "restart-locked-match")
    store = JobStore(archive_database)
    prepared = _prepare(store, _spec(), key="prepare-restart-locked-archive")
    store.transition(
        prepared.job_id,
        expected=("prepared",),
        status="finalizing",
        started_at="2026-08-15T00:00:00.000000Z",
        final_kind="match",
        final_id="restart-locked-match",
        final_match_ids=("restart-locked-match",),
    )
    monkeypatch.setattr(control_runner, "_ARCHIVE_RECONCILE_ATTEMPTS", 2)
    monkeypatch.setattr(control_runner, "_ARCHIVE_RECONCILE_RETRY_SECONDS", 0.001)
    monkeypatch.setattr(control_runner, "_ARCHIVE_SQLITE_TIMEOUT_SECONDS", 0.01)
    blocker = sqlite3.connect(archive_database, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")

    try:
        unavailable_manager = ControlJobManager(store, environment={})
        unavailable = store.get(prepared.job_id)
        asyncio.run(unavailable_manager.shutdown())
        assert unavailable.status == "interrupted"
        assert unavailable.failure_code is None
    finally:
        blocker.rollback()
        blocker.close()

    recovered_manager = ControlJobManager(store, environment={})
    recovered = store.get(prepared.job_id)
    asyncio.run(recovered_manager.shutdown())

    assert recovered.status == "completed"
    assert recovered.failure_code is None
    assert archive_database.read_bytes() == archive_before


def test_cancel_never_signals_a_pid_that_the_manager_does_not_own(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JobStore(tmp_path / "archive.db")
    prepared = _prepare(store, _spec(), key="prepare-unowned-pid")
    manager = ControlJobManager(store, environment={})
    store.transition(
        prepared.job_id,
        expected=("prepared",),
        status="running",
        child_pid=os.getpid(),
    )
    signalled: list[int | None] = []

    def record_signal(process) -> None:
        signalled.append(getattr(process, "pid", None))

    monkeypatch.setattr(
        ControlJobManager,
        "_interrupt_process",
        staticmethod(record_signal),
    )

    async def exercise() -> ControlJob:
        try:
            return await manager.cancel(
                prepared.job_id,
                idempotency_key="cancel-unowned-pid",
            )
        finally:
            await manager.shutdown()

    cancelled = asyncio.run(exercise())

    assert signalled == []
    assert cancelled.status == "interrupted"
    assert cancelled.failure_code == "worker_missing"


def test_manager_startup_reconciles_stale_jobs_without_signalling_stored_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JobStore(tmp_path / "archive.db")
    prepared = _prepare(store, _spec(), key="prepare-stale-job")
    store.transition(
        prepared.job_id,
        expected=("prepared",),
        status="running",
        child_pid=424_242,
    )
    liveness_checks: list[int] = []
    signalled: list[tuple[str, int]] = []

    def report_missing_process(pid: int, signal_number: int) -> None:
        assert signal_number == 0
        liveness_checks.append(pid)
        raise ProcessLookupError

    monkeypatch.setattr(
        os,
        "kill",
        report_missing_process,
    )
    if hasattr(os, "killpg"):
        monkeypatch.setattr(
            os,
            "killpg",
            lambda pid, sig: signalled.append(("killpg", pid)),
        )

    manager = ControlJobManager(store, environment={})
    reconciled = store.get(prepared.job_id)
    asyncio.run(manager.shutdown())

    assert liveness_checks == [424_242]
    assert signalled == []
    assert reconciled.status == "interrupted"


def test_manager_startup_keeps_capacity_reserved_for_a_live_or_reused_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JobStore(tmp_path / "archive.db")
    prepared = _prepare(store, _spec(), key="prepare-live-prior-pid")
    store.transition(
        prepared.job_id,
        expected=("prepared",),
        status="running",
        child_pid=424_242,
    )
    liveness_checks: list[tuple[int, int]] = []
    monkeypatch.setattr(control_runner, "_PRIOR_CHILD_EXIT_ATTEMPTS", 2)
    monkeypatch.setattr(control_runner, "_PRIOR_CHILD_EXIT_RETRY_SECONDS", 0.0)
    monkeypatch.setattr(
        os,
        "kill",
        lambda pid, signal_number: liveness_checks.append((pid, signal_number)),
    )

    manager = ControlJobManager(store, environment={})
    preserved = store.get(prepared.job_id)

    async def exercise() -> None:
        try:
            with pytest.raises(ControlError, match="job_capacity"):
                other = _prepare(store, _spec(), key="prepare-behind-live-prior-pid")
                await manager.start(
                    other.job_id,
                    idempotency_key="start-behind-live-prior-pid",
                    web_base_url="http://localhost:8765",
                )
        finally:
            await manager.shutdown()

    asyncio.run(exercise())

    assert liveness_checks == [(424_242, 0), (424_242, 0)]
    assert preserved.status == "running"


def test_manager_lease_has_a_windows_locking_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int]] = []

    class FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        @staticmethod
        def locking(_descriptor: int, mode: int, length: int) -> None:
            calls.append((mode, length))

    monkeypatch.setattr(control_runner, "fcntl", None)
    monkeypatch.setattr(control_runner, "msvcrt", FakeMsvcrt)
    manager = ControlJobManager(JobStore(tmp_path / "archive.db"), environment={})

    asyncio.run(manager.shutdown())

    assert calls == [(FakeMsvcrt.LK_NBLCK, 1), (FakeMsvcrt.LK_UNLCK, 1)]


def test_start_cannot_revive_an_expired_prepared_job(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "archive.db")
    prepared = _prepare(store, _spec(), key="prepare-that-expires")
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE control_jobs SET created_at = ? WHERE job_id = ?",
            ("2000-01-01T00:00:00.000000Z", prepared.job_id),
        )
    manager = ControlJobManager(store, environment={})

    async def exercise() -> ControlJob:
        try:
            return await manager.start(
                prepared.job_id,
                idempotency_key="start-expired-draft",
                web_base_url="http://localhost:8765",
            )
        finally:
            await manager.shutdown()

    expired = asyncio.run(exercise())

    assert expired.status == "cancelled"
    assert expired.started_at is None
