"""HTTP security and lifecycle tests for local browser participation."""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from llmolympic.human_input import (
    HumanInputError,
    InputSessionStore,
    derive_human_input_database_path,
)
from llmolympic.web.app import MAX_INPUT_BODY_BYTES, create_app


def _start_request(
    session: InputSessionStore,
    prompt: str = "<script>window.evil=true</script>\n请输入走法",
) -> tuple[ThreadPoolExecutor, Future[str]]:
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(
        asyncio.run,
        session.resolve(prompt, timeout_seconds=5.0, match_event_seq=7),
    )
    return executor, future


def _headers(session: InputSessionStore, *, origin: bool = False) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {session.capability}"}
    if origin:
        headers.update({"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"})
    return headers


def _get_request(client: TestClient, session: InputSessionStore) -> dict[str, object]:
    path = f"/api/v1/participation/{session.session_id}/{session.seat_id}"
    for _ in range(200):
        response = client.get(path, headers=_headers(session))
        if response.status_code == 200 and response.json()["request"] is not None:
            return response.json()
        time.sleep(0.005)
    raise AssertionError("participation request was not published")


@pytest.fixture
def participation(tmp_path: Path):
    archive = tmp_path / "archive.db"
    session = InputSessionStore(
        archive,
        game="gomoku",
        player_name="浏览器人类",
        players=("浏览器人类", "模型"),
        heartbeat_seconds=0.05,
    )
    client = TestClient(create_app(archive), base_url="http://localhost")
    try:
        yield archive, session, client
    finally:
        session.close()


def test_participation_page_and_capability_scoped_snapshot(participation) -> None:
    _archive, session, client = participation
    executor, pending = _start_request(session)
    try:
        page = client.get(f"/participate/{session.session_id}/{session.seat_id}")
        assert page.status_code == 200
        assert "capability" not in page.text

        path = f"/api/v1/participation/{session.session_id}/{session.seat_id}"
        assert client.get(path).status_code == 404
        wrong = client.get(path, headers={"Authorization": f"Bearer {'x' * 43}"})
        assert wrong.status_code == 404
        assert wrong.json() == {"error": {"code": "participation_not_found"}}

        payload = _get_request(client, session)
        serialized = repr(payload)
        assert session.capability not in serialized
        assert "owner" not in serialized
        assert "submission_id" not in serialized
        request = payload["request"]
        assert isinstance(request, dict)
        assert request["state"] == "pending"
        assert request["prompt"].startswith("<script>")
        assert request["match_event_seq"] == 7
    finally:
        session.interrupt()
        with pytest.raises(HumanInputError):
            pending.result(timeout=2)
        executor.shutdown(wait=True)


def test_submission_requires_exact_origin_json_and_is_idempotent(participation) -> None:
    _archive, session, client = participation
    executor, pending = _start_request(session, "请选择 H8")
    try:
        snapshot = _get_request(client, session)
        request_id = snapshot["request"]["request_id"]
        path = (
            f"/api/v1/participation/{session.session_id}/{session.seat_id}/requests/"
            f"{request_id}/submissions"
        )
        body = {"submission_id": "1" * 32, "move": "H8"}

        assert client.post(path, headers=_headers(session), json=body).status_code == 403
        evil = client.post(
            path,
            headers={**_headers(session), "Origin": "https://evil.example"},
            json=body,
        )
        assert evil.status_code == 403
        form = client.post(
            path,
            headers={**_headers(session, origin=True), "Content-Type": "text/plain"},
            content='{"submission_id":"' + "1" * 32 + '","move":"H8"}',
        )
        assert form.status_code == 400

        accepted = client.post(path, headers=_headers(session, origin=True), json=body)
        assert accepted.status_code == 202
        assert accepted.json()["status"] == "submitted"
        duplicate = client.post(path, headers=_headers(session, origin=True), json=body)
        assert duplicate.status_code == 200
        assert duplicate.json()["status"] == "duplicate"
        assert pending.result(timeout=2) == "H8"

        conflict = client.post(
            path,
            headers=_headers(session, origin=True),
            json={"submission_id": "2" * 32, "move": "A1"},
        )
        assert conflict.status_code == 409
        assert conflict.json() == {"error": {"code": "submission_conflict"}}
    finally:
        executor.shutdown(wait=True)


def test_submission_limits_body_and_move_before_side_effect(participation) -> None:
    _archive, session, client = participation
    executor, pending = _start_request(session)
    try:
        snapshot = _get_request(client, session)
        request_id = snapshot["request"]["request_id"]
        path = (
            f"/api/v1/participation/{session.session_id}/{session.seat_id}/requests/"
            f"{request_id}/submissions"
        )
        oversized = client.post(
            path,
            headers={
                **_headers(session, origin=True),
                "Content-Type": "application/json",
            },
            content=b"{" + b" " * MAX_INPUT_BODY_BYTES + b"}",
        )
        assert oversized.status_code == 413
        assert oversized.json() == {"error": {"code": "request_too_large"}}

        move_limit = client.post(
            path,
            headers=_headers(session, origin=True),
            json={"submission_id": "3" * 32, "move": "x" * 4097},
        )
        assert move_limit.status_code == 400
        current = _get_request(client, session)
        assert current["request"]["state"] == "pending"
    finally:
        session.interrupt()
        with pytest.raises(HumanInputError):
            pending.result(timeout=2)
        executor.shutdown(wait=True)


def test_missing_sidecar_is_not_created_by_participation_get(tmp_path: Path) -> None:
    archive = tmp_path / "missing.db"
    input_path = derive_human_input_database_path(archive)
    client = TestClient(create_app(archive), base_url="http://localhost")

    response = client.get(
        f"/api/v1/participation/{'a' * 32}/{'b' * 32}",
        headers={"Authorization": f"Bearer {'c' * 43}"},
    )

    assert response.status_code == 404
    assert not input_path.exists()
