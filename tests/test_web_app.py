from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from llmolympic.core.archive import MatchArchive
from llmolympic.core.events import EventType, MatchEvent
from llmolympic.core.storage import SQLiteStore
from llmolympic.web.app import create_app

STARTED = datetime(2026, 8, 12, 8, 0, tzinfo=UTC)
ORIGIN = "http://localhost"


def _archive(match_id: str = "web-app-match") -> MatchArchive:
    players = [
        {
            "name": "甲<script>",
            "display_name": "甲<script>",
            "entrant_id": "web:app-a",
            "kind": "mock",
            "model": "private-model-a",
            "route_id": "route:v1:" + "a" * 64,
        },
        {
            "name": "乙",
            "display_name": "乙",
            "entrant_id": "web:app-b",
            "kind": "mock",
            "model": "private-model-b",
            "route_id": "route:v1:" + "b" * 64,
        },
    ]
    scores = {"甲<script>": 1.0, "乙": 0.0}
    events = [
        MatchEvent(
            seq=0,
            type=EventType.MATCH_STARTED,
            timestamp=STARTED,
            data={
                "game": "math_quiz",
                "seed": 42,
                "game_config": {"rounds": 1, "api_key": "must-not-leak"},
                "players": players,
            },
        ),
        MatchEvent(
            seq=1,
            type=EventType.TURN_PROMPT,
            timestamp=STARTED + timedelta(milliseconds=100),
            player="甲<script>",
            data={"prompt": "</script><img src=x onerror=alert(1)>"},
        ),
        MatchEvent(
            seq=2,
            type=EventType.MOVE_RECEIVED,
            timestamp=STARTED + timedelta(milliseconds=200),
            player="甲<script>",
            data={"move": "4", "authorization": "must-not-leak"},
        ),
        MatchEvent(
            seq=3,
            type=EventType.MATCH_FINISHED,
            timestamp=STARTED + timedelta(seconds=1),
            data={"scores": scores, "termination": "completed"},
        ),
    ]
    return MatchArchive(
        schema_version=2,
        source="local_engine",
        match_id=match_id,
        game="math_quiz",
        seed=42,
        players=players,
        events=events,
        moves=[],
        scores=scores,
        started_at=STARTED,
        finished_at=STARTED + timedelta(seconds=1),
    )


def _client(tmp_path: Path) -> tuple[TestClient, Path, MatchArchive]:
    database = tmp_path / "observer.db"
    archive = _archive()
    SQLiteStore(database).save_match(archive, rating_source="engine")
    return TestClient(create_app(database), base_url="http://localhost"), database, archive


def test_rest_api_is_read_only_filtered_and_hardened(tmp_path: Path) -> None:
    client, _database, archive = _client(tmp_path)

    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["database_available"] is True
    assert health.headers["cache-control"] == "no-store"
    assert health.headers["x-content-type-options"] == "nosniff"
    assert "access-control-allow-origin" not in health.headers

    games = client.get("/api/v1/games")
    assert games.status_code == 200
    assert {item["name"] for item in games.json()["games"]} >= {
        "gomoku",
        "chess",
        "creative_writing",
    }

    matches = client.get("/api/v1/matches?game=math_quiz&limit=1")
    assert matches.status_code == 200
    assert matches.json()["matches"][0]["match_id"] == archive.match_id
    assert "entrant_id" not in matches.text
    assert "route_id" not in matches.text
    assert "private-model" not in matches.text

    leaderboard = client.get("/api/v1/leaderboard?game=math_quiz")
    assert leaderboard.status_code == 200
    assert len(leaderboard.json()["entries"]) == 2
    assert "entrant_id" not in leaderboard.text

    detail = client.get(f"/api/v1/matches/{archive.match_id}")
    assert detail.status_code == 200
    assert "must-not-leak" not in detail.text
    assert "private-model" not in detail.text
    assert "route:v1" not in detail.text
    assert "</script><img src=x onerror=alert(1)>" in detail.text


def test_rest_rejects_cross_origin_bad_host_and_invalid_inputs(tmp_path: Path) -> None:
    client, _database, _archive = _client(tmp_path)

    cross_origin = client.get(
        "/api/v1/games",
        headers={"Origin": "https://evil.example"},
    )
    assert cross_origin.status_code == 403
    assert cross_origin.json() == {"error": {"code": "cross_origin_forbidden"}}

    bad_host = client.get("/api/v1/games", headers={"Host": "evil.example"})
    assert bad_host.status_code == 400
    assert "evil.example" not in bad_host.text

    assert client.get("/api/v1/matches?limit=101").status_code == 400
    assert client.get("/api/v1/matches?game=x%27%20OR%201=1").status_code == 400
    assert client.get("/api/v1/matches/not%20valid").status_code == 400

    openapi = client.get("/openapi.json").json()
    for path_item in openapi["paths"].values():
        for operation in path_item.values():
            assert "422" not in operation.get("responses", {})
        assert not ({"post", "put", "patch", "delete"} & set(path_item))
    assert "HTTPValidationError" not in openapi.get("components", {}).get("schemas", {})


def test_ipv6_loopback_host_and_origin_are_supported(tmp_path: Path) -> None:
    client, _database, archive = _client(tmp_path)
    headers = {"Host": "[::1]:8000", "Origin": "http://[::1]:8000"}

    response = client.get("/api/v1/games", headers=headers)
    assert response.status_code == 200

    with client.websocket_connect(
        f"/ws/v1/matches/{archive.match_id}?from_seq=4",
        headers=headers,
    ) as websocket:
        assert websocket.receive_json()["type"] == "archive"
        assert websocket.receive_json()["type"] == "complete"


def test_health_degrades_without_creating_missing_database(tmp_path: Path) -> None:
    database = tmp_path / "missing.db"
    client = TestClient(create_app(database), base_url="http://localhost")

    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["database_available"] is False
    assert not database.exists()
    history = client.get("/api/v1/matches")
    assert history.status_code == 503
    assert history.json() == {"error": {"code": "database_unavailable"}}


def test_websocket_replays_from_sequence_with_public_envelopes(tmp_path: Path) -> None:
    client, _database, archive = _client(tmp_path)

    with client.websocket_connect(
        f"/ws/v1/matches/{archive.match_id}?from_seq=2",
        headers={"Origin": ORIGIN, "Host": "localhost"},
    ) as websocket:
        messages = [websocket.receive_json() for _ in range(4)]

    assert [message["type"] for message in messages] == [
        "archive",
        "event",
        "event",
        "complete",
    ]
    assert messages[0]["match"]["rated"] is True
    assert [message["event"]["seq"] for message in messages[1:3]] == [2, 3]
    rendered = repr(messages)
    assert "must-not-leak" not in rendered
    assert "route:v1" not in rendered
    assert "private-model" not in rendered


def test_websocket_rejects_missing_or_cross_origin_before_accept(tmp_path: Path) -> None:
    client, _database, archive = _client(tmp_path)

    for headers in (
        {"Host": "localhost"},
        {"Host": "localhost", "Origin": "https://evil.example"},
        {"Host": "localhost", "Origin": "null"},
    ):
        try:
            with client.websocket_connect(
                f"/ws/v1/matches/{archive.match_id}",
                headers=headers,
            ):
                raise AssertionError("cross-origin WebSocket must not be accepted")
        except (WebSocketDenialResponse, WebSocketDisconnect) as exc:
            assert getattr(exc, "code", None) == 4403 or getattr(exc, "status_code", None) == 403


def test_websocket_rejects_invalid_resume_and_missing_match_stably(tmp_path: Path) -> None:
    client, _database, archive = _client(tmp_path)
    headers = {"Host": "localhost", "Origin": ORIGIN}

    for suffix in ("?from_seq=-1", "?from_seq=text", "?from_seq=9999999"):
        try:
            with client.websocket_connect(
                f"/ws/v1/matches/{archive.match_id}{suffix}",
                headers=headers,
            ):
                raise AssertionError("invalid replay offset must not be accepted")
        except WebSocketDisconnect as exc:
            assert exc.code == 4400

    try:
        with client.websocket_connect(
            "/ws/v1/matches/missing-match",
            headers=headers,
        ):
            raise AssertionError("missing replay must not be accepted")
    except WebSocketDisconnect as exc:
        assert exc.code == 4404
