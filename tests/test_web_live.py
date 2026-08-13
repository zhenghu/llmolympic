"""Integration coverage for the Stage 4.3 read-only live Web surface."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

import llmolympic.web.app as web_app
from llmolympic.core.events import EventType, MatchEvent
from llmolympic.live import LivePublisher, derive_live_database_path
from llmolympic.web.app import create_app

STAMP = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
ORIGIN_HEADERS = {"Host": "localhost", "Origin": "http://localhost"}


def _started() -> MatchEvent:
    return MatchEvent(
        seq=0,
        type=EventType.MATCH_STARTED,
        timestamp=STAMP,
        data={
            "game": "math_quiz",
            "seed": 43,
            "game_config": {
                "rounds": 1,
                "api_key": "must-not-leak",
            },
            "players": [
                {
                    "display_name": "甲",
                    "entrant_id": "private:entrant-a",
                    "provider": "private-provider",
                    "model": "private-model-a",
                },
                {
                    "display_name": "乙",
                    "entrant_id": "private:entrant-b",
                    "model": "private-model-b",
                },
            ],
        },
    )


def _prompt() -> MatchEvent:
    return MatchEvent(
        seq=1,
        type=EventType.TURN_PROMPT,
        timestamp=STAMP + timedelta(milliseconds=100),
        player="甲",
        data={"prompt": "1 + 1 = ?", "authorization": "must-not-leak"},
    )


def _finished() -> MatchEvent:
    return MatchEvent(
        seq=2,
        type=EventType.MATCH_FINISHED,
        timestamp=STAMP + timedelta(seconds=1),
        data={
            "scores": {"甲": 1.0, "乙": 0.0},
            "termination": "completed",
            "failure_details": {"message": "must-not-leak"},
        },
    )


def _client(database: Path) -> TestClient:
    return TestClient(create_app(database), base_url="http://localhost")


def _wait_for_detail(
    client: TestClient,
    live_id: str,
    *,
    event_count: int,
    status: str,
) -> dict[str, object]:
    deadline = time.monotonic() + 3.0
    last_response = None
    while time.monotonic() < deadline:
        last_response = client.get(f"/api/v1/live/{live_id}")
        if last_response.status_code == 200:
            payload = last_response.json()
            if (
                payload["match"]["event_count"] == event_count
                and payload["match"]["status"] == status
            ):
                return payload
        time.sleep(0.01)
    if last_response is None:
        raise AssertionError("live detail was never requested")
    raise AssertionError(
        f"live detail did not reach {status=} {event_count=}: "
        f"{last_response.status_code} {last_response.text}"
    )


def _assert_pre_accept_rejection(
    client: TestClient,
    path: str,
    headers: dict[str, str],
    *,
    code: int,
) -> None:
    try:
        with client.websocket_connect(path, headers=headers):
            raise AssertionError("untrusted live WebSocket must not be accepted")
    except (WebSocketDenialResponse, WebSocketDisconnect) as exc:
        websocket_code = getattr(exc, "code", None)
        http_status = getattr(exc, "status_code", None)
        assert websocket_code == code or (code == 4403 and http_status == 403)


def test_missing_live_sidecar_lists_empty_without_creating_files(tmp_path: Path) -> None:
    database = tmp_path / "missing archive ? #.db"
    sidecar = derive_live_database_path(database)
    client = _client(database)

    response = client.get("/api/v1/live")

    assert response.status_code == 200
    assert response.json() == {"api_version": "v1", "matches": []}
    assert not database.exists()
    assert not sidecar.exists()


def test_live_rest_lists_running_then_completed_and_pages_events(tmp_path: Path) -> None:
    database = tmp_path / "observer.db"
    client = _client(database)
    publisher = LivePublisher(database, "play", heartbeat_seconds=1.0)
    live_id = publisher.start_session(_started())
    assert live_id is not None

    try:
        assert publisher.publish(live_id, _prompt())
        assert publisher.publish(live_id, _finished())
        running = _wait_for_detail(client, live_id, event_count=3, status="running")

        lobby = client.get("/api/v1/live?game=math_quiz&limit=1")
        assert lobby.status_code == 200
        assert lobby.json()["matches"] == [running["match"]]
        assert running["match"]["players"] == ["甲", "乙"]
        assert running["match"]["final_id"] is None

        page = client.get(f"/api/v1/live/{live_id}?from_seq=1&limit=1")
        assert page.status_code == 200
        assert [item["seq"] for item in page.json()["events"]] == [1]
        assert page.json()["events"][0]["event"]["seq"] == 1
        assert page.json()["next_seq"] == 2
        assert page.json()["has_more"] is True

        tail = client.get(f"/api/v1/live/{live_id}?from_seq=3")
        assert tail.status_code == 200
        assert tail.json()["events"] == []
        assert tail.json()["next_seq"] == 3
        assert tail.json()["has_more"] is False

        assert publisher.complete(
            live_id,
            final_kind="match",
            final_id="archive-match-43",
            final_match_ids=("archive-match-43",),
        )
        publisher.close()

        completed = _wait_for_detail(client, live_id, event_count=3, status="completed")
        assert completed["match"]["final_kind"] == "match"
        assert completed["match"]["final_id"] == "archive-match-43"
        assert completed["match"]["final_match_ids"] == ["archive-match-43"]
        rendered = repr(completed)
        assert "must-not-leak" not in rendered
        assert "private-provider" not in rendered
        assert "private-model" not in rendered
    finally:
        publisher.close()


def test_live_websocket_resumes_and_follows_until_completion(tmp_path: Path) -> None:
    database = tmp_path / "observer.db"
    client = _client(database)
    publisher = LivePublisher(database, "play", heartbeat_seconds=1.0)
    live_id = publisher.start_session(_started())
    assert live_id is not None

    try:
        assert publisher.publish(live_id, _prompt())
        _wait_for_detail(client, live_id, event_count=2, status="running")

        try:
            with client.websocket_connect(
                f"/ws/v1/live/{live_id}?from_seq=3",
                headers=ORIGIN_HEADERS,
            ) as invalid_cursor:
                invalid_cursor.receive_json()
                raise AssertionError("a cursor beyond the live tail must be rejected")
        except WebSocketDisconnect as exc:
            assert exc.code == 4400
            assert exc.reason == "invalid_request"

        with client.websocket_connect(
            f"/ws/v1/live/{live_id}?from_seq=1",
            headers=ORIGIN_HEADERS,
        ) as websocket:
            snapshot = websocket.receive_json()
            resumed = websocket.receive_json()
            assert snapshot["type"] == "live_snapshot"
            assert snapshot["next_seq"] == 1
            assert snapshot["match"]["status"] == "running"
            assert resumed["type"] == "live_event"
            assert resumed["item"]["seq"] == 1

            assert publisher.publish(live_id, _finished())
            assert publisher.complete(
                live_id,
                final_kind="match",
                final_id="archive-match-43",
                final_match_ids=("archive-match-43",),
            )

            final_event = websocket.receive_json()
            complete = websocket.receive_json()

        assert final_event["type"] == "live_event"
        assert final_event["item"]["seq"] == 2
        assert final_event["item"]["event"]["type"] == "match_finished"
        assert complete == {
            "api_version": "v1",
            "type": "live_complete",
            "live_id": live_id,
            "event_count": 3,
            "final_kind": "match",
            "final_id": "archive-match-43",
            "final_match_ids": ["archive-match-43"],
        }
    finally:
        publisher.close()


def test_live_websocket_reports_interrupted_terminal_state(tmp_path: Path) -> None:
    database = tmp_path / "observer.db"
    client = _client(database)
    publisher = LivePublisher(database, "play", heartbeat_seconds=1.0)
    live_id = publisher.start_session(_started())
    assert live_id is not None

    try:
        assert publisher.publish(live_id, _prompt())
        assert publisher.interrupt(live_id, reason_code="provider_failed")
        publisher.close()
        _wait_for_detail(client, live_id, event_count=2, status="interrupted")

        with client.websocket_connect(
            f"/ws/v1/live/{live_id}",
            headers=ORIGIN_HEADERS,
        ) as websocket:
            messages = [websocket.receive_json() for _ in range(4)]

        assert [message["type"] for message in messages] == [
            "live_snapshot",
            "live_event",
            "live_event",
            "live_interrupted",
        ]
        assert messages[-1] == {
            "api_version": "v1",
            "type": "live_interrupted",
            "live_id": live_id,
            "event_count": 2,
        }
        assert "provider_failed" not in repr(messages)
    finally:
        publisher.close()


def test_live_http_and_websocket_reject_untrusted_or_invalid_inputs(
    tmp_path: Path,
) -> None:
    database = tmp_path / "observer.db"
    client = _client(database)

    cross_origin = client.get(
        "/api/v1/live",
        headers={"Origin": "https://evil.example"},
    )
    assert cross_origin.status_code == 403
    assert cross_origin.json() == {"error": {"code": "cross_origin_forbidden"}}
    assert client.get("/api/v1/live", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/api/v1/live?limit=0").status_code == 400
    assert client.get("/api/v1/live?game=x%27%20OR%201=1").status_code == 400
    assert client.get("/api/v1/live/not%20valid").status_code == 400
    assert client.get("/api/v1/live/valid-id?from_seq=-1").status_code == 400
    assert client.get("/api/v1/live/valid-id?limit=257").status_code == 400

    for headers in (
        {"Host": "localhost"},
        {"Host": "localhost", "Origin": "https://evil.example"},
        {"Host": "evil.example", "Origin": "http://evil.example"},
    ):
        _assert_pre_accept_rejection(
            client,
            "/ws/v1/live/valid-id",
            headers,
            code=4403,
        )

    for path in (
        "/ws/v1/live/not%20valid",
        "/ws/v1/live/valid-id?from_seq=-1",
        "/ws/v1/live/valid-id?from_seq=text",
        "/ws/v1/live/valid-id?from_seq=9999999",
    ):
        _assert_pre_accept_rejection(client, path, ORIGIN_HEADERS, code=4400)

    try:
        with client.websocket_connect(
            "/ws/v1/live/missing-live",
            headers=ORIGIN_HEADERS,
        ) as websocket:
            websocket.receive_json()
            raise AssertionError("missing live session must close with a public error")
    except WebSocketDisconnect as exc:
        assert exc.code == 4404
        assert exc.reason == "live_not_found"


def test_live_websocket_capacity_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web_app, "MAX_CONCURRENT_LIVE_STREAMS", 1)
    database = tmp_path / "observer.db"
    client = _client(database)
    publisher = LivePublisher(database, "play", heartbeat_seconds=1.0)
    live_id = publisher.start_session(_started())
    assert live_id is not None

    try:
        _wait_for_detail(client, live_id, event_count=1, status="running")
        with client.websocket_connect(
            f"/ws/v1/live/{live_id}",
            headers=ORIGIN_HEADERS,
        ) as first:
            assert first.receive_json()["type"] == "live_snapshot"
            assert first.receive_json()["type"] == "live_event"

            try:
                with client.websocket_connect(
                    f"/ws/v1/live/{live_id}",
                    headers=ORIGIN_HEADERS,
                ) as overloaded:
                    overloaded.receive_json()
                    raise AssertionError("second live stream must be rejected")
            except WebSocketDisconnect as exc:
                assert exc.code == 4429
                assert exc.reason == "overloaded"

            assert publisher.interrupt(live_id, reason_code="test_finished")
            assert first.receive_json()["type"] == "live_interrupted"
    finally:
        publisher.close()


def test_live_spa_route_and_openapi_remain_read_only(tmp_path: Path) -> None:
    client = _client(tmp_path / "observer.db")

    home = client.get("/")
    deep_link = client.get("/live/live-match-43")
    assert deep_link.status_code == 200
    assert deep_link.content == home.content
    assert client.get("/live/not%20valid").status_code == 404

    openapi = client.get("/openapi.json").json()
    assert set(openapi["paths"]["/api/v1/live"]) == {"get"}
    assert set(openapi["paths"]["/api/v1/live/{live_id}"]) == {"get"}
    assert "/live/{live_id}" not in openapi["paths"]
    assert all(
        not ({"post", "put", "patch", "delete"} & set(path_item))
        for path_item in openapi["paths"].values()
    )
