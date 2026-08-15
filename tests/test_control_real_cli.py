"""Fast local integration tests for the Web controller and the real CLI."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from llmolympic.control import (
    ControlJob,
    ControlJobSpec,
    ControlPlayerSpec,
    JobStore,
    validate_job_spec,
)
from llmolympic.control_runner import ControlJobManager
from llmolympic.core.storage import SQLiteStore

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_CREDENTIAL_MARKER = "real-cli-control-secret-must-not-persist"
_ROUTE_MARKER = "https://real-cli-control-secret.invalid/v1"


async def _wait_for_terminal_job(
    manager: ControlJobManager,
    job_id: str,
    *,
    timeout: float = 15.0,
) -> ControlJob:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        job = manager.public_job(job_id)
        if job.status in {"cancelled", "completed", "failed", "interrupted"}:
            return job
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"real CLI job stayed in {job.status!r}")
        await asyncio.sleep(0.01)


async def _run_real_cli_job(store: JobStore, prepared: ControlJob) -> tuple[ControlJob, ControlJob]:
    manager = ControlJobManager(
        store,
        python_executable=sys.executable,
        working_directory=_REPOSITORY_ROOT,
        environment={
            "OPENAI_API_KEY": _CREDENTIAL_MARKER,
            "UNTRUSTED_PROVIDER_ROUTE": _ROUTE_MARKER,
        },
    )
    try:
        started = await manager.start(
            prepared.job_id,
            idempotency_key=f"start-{prepared.job_id}",
            web_base_url="http://127.0.0.1:8765",
        )
        completed = await _wait_for_terminal_job(manager, prepared.job_id)
        return started, completed
    finally:
        await manager.shutdown()


def _assert_no_credentials_in_job_or_databases(
    tmp_path: Path,
    final: ControlJob,
) -> None:
    forbidden = (_CREDENTIAL_MARKER.encode(), _ROUTE_MARKER.encode())
    serialized = final.model_dump_json().encode()
    for marker in forbidden:
        assert marker not in serialized
    for path in tmp_path.rglob("*"):
        if path.is_file():
            raw = path.read_bytes()
            for marker in forbidden:
                assert marker not in raw, path


@pytest.mark.parametrize(
    ("mode", "players", "final_kind", "match_count", "games_per_player"),
    [
        (
            "series",
            ("fixed", "illegal"),
            "series",
            2,
            2,
        ),
        (
            "round_robin",
            ("fixed", "random", "illegal"),
            "tournament",
            6,
            4,
        ),
    ],
)
def test_manager_runs_mock_competitions_through_the_real_cli(
    tmp_path: Path,
    mode: str,
    players: tuple[str, ...],
    final_kind: str,
    match_count: int,
    games_per_player: int,
) -> None:
    archive_database = tmp_path / f"{mode}.db"
    store = JobStore(archive_database)
    spec = ControlJobSpec(
        mode=mode,
        game="math_quiz",
        players=tuple(
            ControlPlayerSpec(kind="mock", strategy=strategy) for strategy in players
        ),
        rounds=1,
        seed="17",
    )
    prepared = store.prepare(
        spec,
        validate_job_spec(spec),
        idempotency_key=f"prepare-{mode}",
    )

    assert prepared.status == "prepared"
    started, final = asyncio.run(_run_real_cli_job(store, prepared))

    assert started.started_at is not None
    assert final.status == "completed"
    assert final.failure_code is None
    assert final.final_kind == final_kind
    assert final.final_id is not None
    assert len(final.final_match_ids) == match_count
    assert len(set(final.final_match_ids)) == match_count

    archive = SQLiteStore(archive_database, create=False)
    matches = archive.list_matches(limit=match_count)
    assert {match.match_id for match in matches} == set(final.final_match_ids)
    assert all(match.rated for match in matches)
    leaderboard = archive.leaderboard(game="math_quiz")
    assert {entry.player for entry in leaderboard} == {
        f"mock:{strategy}" for strategy in players
    }
    assert all(entry.games_played == games_per_player for entry in leaderboard)

    if mode == "series":
        formal = archive.get_series(final.final_id)
        assert formal is not None
        formal_match_ids = tuple(leg.match_id for leg in formal.legs)
    else:
        formal = archive.get_verified_tournament(final.final_id)
        assert formal is not None
        formal_match_ids = tuple(
            leg.match_id
            for pairing in formal.pairings
            for leg in pairing.series.legs
        )
    assert final.final_match_ids == formal_match_ids
    _assert_no_credentials_in_job_or_databases(tmp_path, final)
