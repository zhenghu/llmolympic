"""Security contract tests for the loopback-only Web control plane."""

from __future__ import annotations

import copy
import hashlib
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from llmolympic import control
from llmolympic.core.player import LLMPlayer
from llmolympic.core.storage import SQLiteStore
from llmolympic.core.tournament import prepare_round_robin
from llmolympic.games import create_game
from llmolympic.human_input import InputSessionStore
from llmolympic.providers.mock import MockProvider
from llmolympic.web.app import create_app

ADMIN_TOKEN = "a" * 43
WRONG_TOKEN = "b" * 43
ORIGIN = "http://localhost:8000"


def _key(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()[:32]


def _admin_headers(*, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {ADMIN_TOKEN}",
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-origin",
    }
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _client(database: Path) -> TestClient:
    return TestClient(
        create_app(database, control_token=ADMIN_TOKEN),
        base_url=ORIGIN,
    )


@pytest.fixture
def client_factory() -> Iterator[Callable[[Path], TestClient]]:
    with ExitStack() as clients:
        yield lambda database: clients.enter_context(_client(database))


def _valid_prepare_payload(*, human: bool = False) -> dict[str, object]:
    first_player: dict[str, str]
    if human:
        first_player = {"kind": "human", "name": "浏览器选手"}
    else:
        first_player = {"kind": "mock", "strategy": "random"}
    return {
        "allow_large_tournament": False,
        "budget": {
            "max_estimated_cost_usd": None,
            "max_input_tokens": "200000",
            "max_output_tokens_per_call": "4096",
            "max_provider_calls": "64",
            "max_total_output_tokens": "65536",
        },
        "game": "math_quiz",
        "human_timeout_seconds": 300.0,
        "judges": [],
        "llm_timeout_seconds": 120.0,
        "mode": "play",
        "players": [first_player, {"kind": "mock", "strategy": "fixed"}],
        "rounds": 1,
        "seed": "42",
    }


def _resume_database(path: Path) -> str:
    tournament_id = "web-resume-tournament"
    players = [
        LLMPlayer(
            name=name,
            provider=MockProvider(strategy=strategy),
            model=strategy,
        )
        for name, strategy in (("甲", "random"), ("乙", "fixed"), ("丙", "illegal"))
    ]
    checkpoint = prepare_round_robin(
        create_game("math_quiz", mode="round_robin", rounds=3),
        players,
        seed=23,
        tournament_id=tournament_id,
    )
    SQLiteStore(path).save_tournament_checkpoint(checkpoint)
    return tournament_id


def _resume_payload(tournament_id: str) -> dict[str, object]:
    return {
        "allow_large_tournament": False,
        "budget": {
            "max_estimated_cost_usd": None,
            "max_input_tokens": None,
            "max_output_tokens_per_call": None,
            "max_provider_calls": None,
            "max_total_output_tokens": None,
        },
        "game": "",
        "human_timeout_seconds": 120.0,
        "judges": [],
        "llm_timeout_seconds": None,
        "mode": "round_robin",
        "players": [],
        "resume_tournament_id": tournament_id,
        "rounds": None,
        "seed": "0",
    }


def test_control_resume_prepare_persists_safe_frozen_summary(
    tmp_path: Path,
    client_factory: Callable[[Path], TestClient],
) -> None:
    database = tmp_path / "resume.db"
    tournament_id = _resume_database(database)
    client = client_factory(database)

    response = client.post(
        "/api/v1/control/jobs",
        headers=_admin_headers(idempotency_key=_key("prepare-resume")),
        json=_resume_payload(tournament_id),
    )

    assert response.status_code == 201
    job = _job(response)
    assert job["spec"]["game"] == ""
    assert job["spec"]["players"] == []
    assert job["preview"] == {
        "frozen_game": "math_quiz",
        "frozen_judges": [],
        "frozen_llm_timeout_seconds": 120.0,
        "frozen_players": ["甲", "乙", "丙"],
        "frozen_rounds": 3,
        "frozen_seed": "23",
        "human_count": 0,
        "match_count": 6,
        "pairing_count": 3,
        "player_count": 3,
        "prepared_profiles": [],
        "rated": True,
        "requires_provider_budget": False,
        "uses_frozen_budget": False,
        "warnings": ["resume_uses_frozen_configuration"],
    }
    loaded = client.get(
        f"/api/v1/control/jobs/{job['job_id']}",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    assert loaded.status_code == 200
    assert _job(loaded)["preview"] == job["preview"]


def test_control_resume_prepare_rejects_unknown_checkpoint(
    tmp_path: Path,
    client_factory: Callable[[Path], TestClient],
) -> None:
    database = tmp_path / "resume.db"
    _resume_database(database)
    response = client_factory(database).post(
        "/api/v1/control/jobs",
        headers=_admin_headers(idempotency_key=_key("missing-resume")),
        json=_resume_payload("missing-tournament"),
    )
    assert response.status_code == 409
    assert response.json() == {"error": {"code": "resume_unavailable"}}


def test_control_resume_prepare_rejects_an_active_tournament_runner(
    tmp_path: Path,
    client_factory: Callable[[Path], TestClient],
) -> None:
    database = tmp_path / "active-resume.db"
    tournament_id = _resume_database(database)
    archive = SQLiteStore(database, create=False)
    claim = archive.claim_tournament_runner(tournament_id)

    try:
        response = client_factory(database).post(
            "/api/v1/control/jobs",
            headers=_admin_headers(idempotency_key=_key("active-resume")),
            json=_resume_payload(tournament_id),
        )
    finally:
        archive.release_tournament_runner(claim.lease)

    assert response.status_code == 409
    assert response.json() == {"error": {"code": "resume_unavailable"}}


def _job(response) -> dict[str, object]:
    payload = response.json()
    assert isinstance(payload, dict)
    value = payload.get("job", payload)
    assert isinstance(value, dict)
    return value


def _job_id(response) -> str:
    value = _job(response).get("job_id")
    assert isinstance(value, str) and value
    return value


def test_control_gets_require_the_admin_token_and_reject_a_seat_token(
    tmp_path: Path,
    client_factory: Callable[[Path], TestClient],
) -> None:
    database = tmp_path / "control.db"
    seat = InputSessionStore(
        database,
        game="math_quiz",
        player_name="浏览器席位",
        players=("浏览器席位", "对手"),
    )
    client = client_factory(database)
    paths = (
        "/api/v1/control/catalog",
        "/api/v1/control/jobs",
        "/api/v1/control/jobs/not-found",
    )
    try:
        for path in paths:
            missing = client.get(path)
            assert missing.status_code == 401
            assert missing.json() == {"error": {"code": "control_unauthorized"}}

            malformed = client.get(path, headers={"Authorization": "Bearer short"})
            assert malformed.status_code == 401
            assert malformed.json() == {"error": {"code": "control_unauthorized"}}

            wrong = client.get(
                path,
                headers={"Authorization": f"Bearer {WRONG_TOKEN}"},
            )
            assert wrong.status_code == 401
            assert wrong.json() == {"error": {"code": "control_unauthorized"}}

            seat_scoped = client.get(
                path,
                headers={"Authorization": f"Bearer {seat.capability}"},
            )
            assert seat_scoped.status_code == 401
            assert seat_scoped.json() == {"error": {"code": "control_unauthorized"}}

        catalog = client.get(
            "/api/v1/control/catalog",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert catalog.status_code == 200
        assert ADMIN_TOKEN not in catalog.text
        assert seat.capability not in catalog.text

        jobs = client.get(
            "/api/v1/control/jobs",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert jobs.status_code == 200
        assert ADMIN_TOKEN not in jobs.text
        assert seat.capability not in jobs.text

        missing_job = client.get(
            "/api/v1/control/jobs/not-found",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert missing_job.status_code == 404
        assert missing_job.json() == {"error": {"code": "job_not_found"}}
    finally:
        seat.close()


def test_control_writes_require_admin_and_do_not_accept_a_seat_token(
    tmp_path: Path,
    client_factory: Callable[[Path], TestClient],
) -> None:
    database = tmp_path / "control.db"
    seat = InputSessionStore(
        database,
        game="math_quiz",
        player_name="浏览器席位",
        players=("浏览器席位", "对手"),
    )
    client = client_factory(database)
    requests = (
        ("/api/v1/control/jobs", _valid_prepare_payload()),
        ("/api/v1/control/jobs/not-found/start", {}),
        ("/api/v1/control/jobs/not-found/cancel", {}),
    )
    try:
        for index, (path, body) in enumerate(requests):
            common = {
                "Origin": ORIGIN,
                "Sec-Fetch-Site": "same-origin",
                "Idempotency-Key": _key(f"write-auth-{index}"),
            }
            for authorization in (
                None,
                f"Bearer {WRONG_TOKEN}",
                f"Bearer {seat.capability}",
            ):
                headers = dict(common)
                if authorization is not None:
                    headers["Authorization"] = authorization
                response = client.post(path, headers=headers, json=body)
                assert response.status_code == 401
                assert response.json() == {
                    "error": {"code": "control_unauthorized"}
                }
    finally:
        seat.close()


@pytest.mark.parametrize(
    ("headers", "expected_status", "expected_code"),
    [
        (
            {
                "Authorization": f"Bearer {ADMIN_TOKEN}",
                "Idempotency-Key": _key("missing-origin"),
            },
            403,
            "cross_origin_forbidden",
        ),
        (
            {
                "Authorization": f"Bearer {ADMIN_TOKEN}",
                "Origin": "https://evil.example",
                "Sec-Fetch-Site": "cross-site",
                "Idempotency-Key": _key("evil-origin"),
            },
            403,
            "cross_origin_forbidden",
        ),
        (
            {
                "Authorization": f"Bearer {ADMIN_TOKEN}",
                "Origin": ORIGIN,
                "Sec-Fetch-Site": "cross-site",
                "Idempotency-Key": _key("cross-site"),
            },
            403,
            "cross_origin_forbidden",
        ),
    ],
)
def test_control_prepare_rejects_untrusted_browser_context_before_parsing_body(
    tmp_path: Path,
    client_factory: Callable[[Path], TestClient],
    headers: dict[str, str],
    expected_status: int,
    expected_code: str,
) -> None:
    client = client_factory(tmp_path / "control.db")

    response = client.post(
        "/api/v1/control/jobs",
        headers=headers,
        content=b"not-json",
    )

    assert response.status_code == expected_status
    assert response.json() == {"error": {"code": expected_code}}


def test_control_prepare_rejects_bad_host_content_type_and_missing_idempotency_key(
    tmp_path: Path,
    client_factory: Callable[[Path], TestClient],
) -> None:
    client = client_factory(tmp_path / "control.db")

    bad_host = client.post(
        "/api/v1/control/jobs",
        headers={
            **_admin_headers(idempotency_key=_key("bad-host")),
            "Host": "evil.example",
        },
        json={},
    )
    assert bad_host.status_code == 400
    assert bad_host.json() == {"error": {"code": "invalid_host"}}

    wrong_content_type = client.post(
        "/api/v1/control/jobs",
        headers={
            **_admin_headers(idempotency_key=_key("wrong-content-type")),
            "Content-Type": "text/plain",
        },
        content="{}",
    )
    assert wrong_content_type.status_code == 400
    assert wrong_content_type.json() == {"error": {"code": "invalid_request"}}

    missing_idempotency = client.post(
        "/api/v1/control/jobs",
        headers=_admin_headers(),
        json={},
    )
    assert missing_idempotency.status_code == 400
    assert missing_idempotency.json() == {"error": {"code": "invalid_request"}}

    oversized = client.post(
        "/api/v1/control/jobs",
        headers={
            **_admin_headers(idempotency_key=_key("oversized-control-body")),
            "Content-Type": "application/json",
        },
        content=b"{" + (b" " * (256 * 1024)) + b"}",
    )
    assert oversized.status_code == 413
    assert oversized.json() == {"error": {"code": "request_too_large"}}


def test_control_jobs_database_busy_returns_stable_unavailable_error(
    tmp_path: Path,
    client_factory: Callable[[Path], TestClient],
) -> None:
    database = tmp_path / "locked-control.db"
    client = client_factory(database)
    jobs_database = Path(f"{database.resolve()}.jobs.db")
    lock = sqlite3.connect(jobs_database, isolation_level=None)
    lock.execute("BEGIN EXCLUSIVE")
    try:
        response = client.get(
            "/api/v1/control/jobs",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
    finally:
        lock.rollback()
        lock.close()

    assert response.status_code == 503
    assert response.json() == {"error": {"code": "control_unavailable"}}


def test_control_mock_prepare_does_not_load_broken_profiles(
    tmp_path: Path,
    client_factory: Callable[[Path], TestClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_profiles() -> dict:
        raise ValueError("broken profiles must not affect a pure mock job")

    monkeypatch.setattr(control, "load_profiles", broken_profiles)
    response = client_factory(tmp_path / "mock-only.db").post(
        "/api/v1/control/jobs",
        headers=_admin_headers(idempotency_key=_key("mock-with-broken-profiles")),
        json=_valid_prepare_payload(),
    )

    assert response.status_code == 201
    assert _job(response)["status"] == "prepared"


def test_control_prepare_forbids_extra_fields_and_untrusted_route_configuration(
    tmp_path: Path,
    client_factory: Callable[[Path], TestClient],
) -> None:
    database = tmp_path / "control.db"
    client = client_factory(database)
    cases: list[dict[str, object]] = []

    top_level_command = _valid_prepare_payload()
    top_level_command["command"] = "$(touch should-never-exist)"
    cases.append(top_level_command)

    for field in ("base_url", "endpoint", "env", "model", "path"):
        nested = _valid_prepare_payload()
        player = nested["players"][0]
        assert isinstance(player, dict)
        player[field] = f"untrusted-{field}"
        cases.append(nested)

    unknown_game = _valid_prepare_payload()
    unknown_game["game"] = "unknown_game"
    cases.append(unknown_game)

    unknown_profile = _valid_prepare_payload()
    unknown_profile["players"][0] = {
        "kind": "profile",
        "profile_id": "missing-profile",
    }
    cases.append(unknown_profile)

    for index, payload in enumerate(cases):
        response = client.post(
            "/api/v1/control/jobs",
            headers=_admin_headers(idempotency_key=_key(f"invalid-{index}")),
            json=payload,
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] in {
            "invalid_request",
            "profile_unavailable",
        }
        assert "untrusted-" not in response.text
        assert "missing-profile" not in response.text

    listed = client.get(
        "/api/v1/control/jobs",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    assert listed.status_code == 200
    assert listed.json().get("jobs", []) == []


def test_control_prepare_is_idempotent_conflict_safe_and_capacity_bounded(
    tmp_path: Path,
    client_factory: Callable[[Path], TestClient],
) -> None:
    database = tmp_path / "control.db"
    client = client_factory(database)
    payload = _valid_prepare_payload()
    headers = _admin_headers(idempotency_key=_key("prepare-same-job"))

    first = client.post("/api/v1/control/jobs", headers=headers, json=payload)
    assert first.status_code in {200, 201}
    assert _job(first)["status"] == "prepared"

    duplicate = client.post("/api/v1/control/jobs", headers=headers, json=payload)
    assert duplicate.status_code in {200, 201}
    assert duplicate.json() == first.json()

    conflicting_payload = copy.deepcopy(payload)
    conflicting_payload["seed"] = "43"
    conflict = client.post(
        "/api/v1/control/jobs",
        headers=headers,
        json=conflicting_payload,
    )
    assert conflict.status_code == 409
    assert conflict.json() == {"error": {"code": "idempotency_conflict"}}

    capacity = client.post(
        "/api/v1/control/jobs",
        headers=_admin_headers(idempotency_key=_key("prepare-second-job")),
        json=conflicting_payload,
    )
    assert capacity.status_code == 409
    assert capacity.json() == {"error": {"code": "job_capacity"}}


def test_control_cancel_of_a_prepared_job_is_idempotent_and_releases_capacity(
    tmp_path: Path,
    client_factory: Callable[[Path], TestClient],
) -> None:
    database = tmp_path / "control.db"
    client = client_factory(database)
    prepared = client.post(
        "/api/v1/control/jobs",
        headers=_admin_headers(idempotency_key=_key("prepare-cancelled-job")),
        json=_valid_prepare_payload(),
    )
    assert prepared.status_code in {200, 201}
    job_id = _job_id(prepared)
    cancel_path = f"/api/v1/control/jobs/{job_id}/cancel"
    cancel_headers = _admin_headers(idempotency_key=_key("cancel-same-job"))

    cancelled = client.post(cancel_path, headers=cancel_headers, json={})
    assert cancelled.status_code in {200, 202}
    assert _job(cancelled)["status"] == "cancelled"

    duplicate = client.post(cancel_path, headers=cancel_headers, json={})
    assert duplicate.status_code in {200, 202}
    assert duplicate.json() == cancelled.json()

    replacement = client.post(
        "/api/v1/control/jobs",
        headers=_admin_headers(idempotency_key=_key("prepare-replacement-job")),
        json=_valid_prepare_payload(),
    )
    assert replacement.status_code in {200, 201}
    assert _job_id(replacement) != job_id


@pytest.mark.parametrize("action", ["start", "cancel"])
def test_control_job_actions_require_the_write_envelope_and_empty_body(
    tmp_path: Path,
    action: str,
) -> None:
    database = tmp_path / f"{action}.db"
    with _client(database) as client:
        prepared = client.post(
            "/api/v1/control/jobs",
            headers=_admin_headers(idempotency_key=_key(f"prepare-{action}")),
            json=_valid_prepare_payload(human=action == "start"),
        )
        assert prepared.status_code in {200, 201}
        job_id = _job_id(prepared)
        path = f"/api/v1/control/jobs/{job_id}/{action}"

        missing_origin = client.post(
            path,
            headers={
                "Authorization": f"Bearer {ADMIN_TOKEN}",
                "Idempotency-Key": _key(f"{action}-missing-origin"),
            },
            json={},
        )
        assert missing_origin.status_code == 403
        assert missing_origin.json() == {
            "error": {"code": "cross_origin_forbidden"}
        }

        wrong_content_type = client.post(
            path,
            headers={
                **_admin_headers(
                    idempotency_key=_key(f"{action}-wrong-content-type")
                ),
                "Content-Type": "text/plain",
            },
            content="{}",
        )
        assert wrong_content_type.status_code == 400
        assert wrong_content_type.json() == {"error": {"code": "invalid_request"}}

        missing_idempotency = client.post(path, headers=_admin_headers(), json={})
        assert missing_idempotency.status_code == 400
        assert missing_idempotency.json() == {"error": {"code": "invalid_request"}}

        extra_field = client.post(
            path,
            headers=_admin_headers(idempotency_key=_key(f"{action}-extra-field")),
            json={"command": "$(touch never)"},
        )
        assert extra_field.status_code == 400
        assert extra_field.json() == {"error": {"code": "invalid_request"}}

        accepted = client.post(
            path,
            headers=_admin_headers(idempotency_key=_key(f"{action}-accepted")),
            json={},
        )
        assert accepted.status_code in {200, 202}
        if action == "start":
            stopped = client.post(
                f"/api/v1/control/jobs/{job_id}/cancel",
                headers=_admin_headers(idempotency_key=_key("stop-action-test")),
                json={},
            )
            assert stopped.status_code in {200, 202}


def test_control_start_is_idempotent_and_jobs_database_keeps_tokens_out(
    tmp_path: Path,
) -> None:
    database = tmp_path / "control.db"
    with _client(database) as client:
        prepared = client.post(
            "/api/v1/control/jobs",
            headers=_admin_headers(idempotency_key=_key("prepare-human-job")),
            json=_valid_prepare_payload(human=True),
        )
        assert prepared.status_code in {200, 201}
        job_id = _job_id(prepared)
        start_path = f"/api/v1/control/jobs/{job_id}/start"
        start_headers = _admin_headers(idempotency_key=_key("start-human-job"))

        started = client.post(start_path, headers=start_headers, json={})
        assert started.status_code in {200, 202}
        started_job = _job(started)
        assert started_job["status"] in {"starting", "running"}

        duplicate = client.post(start_path, headers=start_headers, json={})
        assert duplicate.status_code in {200, 202}
        duplicate_job = _job(duplicate)
        assert duplicate_job["job_id"] == job_id
        assert duplicate_job["started_at"] == started_job["started_at"]
        assert duplicate_job["spec"] == started_job["spec"]
        assert duplicate_job["preview"] == started_job["preview"]

        serialized = started.text
        assert ADMIN_TOKEN not in serialized
        for forbidden in (
            "api_key",
            "api_key_env",
            "authorization",
            "base_url",
            "client_secret",
            "endpoint",
            "owner_token",
        ):
            assert forbidden not in serialized.casefold()

        capabilities: list[str] = []
        links = started_job.get("participation_links", [])
        assert isinstance(links, list) and len(links) == 1
        for item in links:
            source = item.get("url") if isinstance(item, dict) else item
            assert isinstance(source, str)
            parsed = urlsplit(source)
            values = parse_qs(parsed.fragment).get("capability", [])
            assert len(values) == 1
            capabilities.append(values[0])

        jobs_database = Path(f"{database.resolve()}.jobs.db")
        assert jobs_database.is_file()
        assert stat.S_IMODE(jobs_database.stat().st_mode) == 0o600
        with sqlite3.connect(jobs_database) as connection:
            start_operations = connection.execute(
                "SELECT COUNT(*) FROM control_operations "
                "WHERE job_id = ? AND operation = 'start'",
                (job_id,),
            ).fetchone()[0]
        assert start_operations == 1
        raw_jobs = jobs_database.read_bytes()
        assert ADMIN_TOKEN.encode("ascii") not in raw_jobs
        for capability in capabilities:
            assert capability.encode("ascii") not in raw_jobs

        cancelled = client.post(
            f"/api/v1/control/jobs/{job_id}/cancel",
            headers=_admin_headers(idempotency_key=_key("cancel-running-human-job")),
            json={},
        )
        assert cancelled.status_code in {200, 202}
