from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from llmolympic import __version__
from llmolympic.core.archive import MatchArchive, archive_from_events
from llmolympic.core.events import EventType, MatchEvent
from llmolympic.core.storage import (
    MatchSummary as StorageMatchSummary,
)
from llmolympic.core.storage import (
    RatingEntry,
)
from llmolympic.web.models import (
    GameInfo,
    HealthResponse,
    LeaderboardResponse,
    MatchDetail,
    MatchListResponse,
    MatchSummary,
    PublicEvent,
    WSArchiveEnvelope,
    WSCompleteEnvelope,
    WSEventEnvelope,
)

STAMP = datetime(2026, 1, 2, 3, 4, 5, 6789, tzinfo=timezone(timedelta(hours=2)))


def _event(type_: EventType, data: dict, *, seq: int = 0, player: str | None = None) -> MatchEvent:
    return MatchEvent(seq=seq, type=type_, timestamp=STAMP, player=player, data=data)


def _assert_no_sensitive_keys(value: object) -> None:
    blocked = {
        "entrant_id",
        "route_id",
        "profile",
        "profile_id",
        "provider",
        "model",
        "failure_details",
        "endpoint",
        "api_key",
        "header",
        "headers",
    }
    if isinstance(value, dict):
        assert not blocked.intersection(value)
        for nested in value.values():
            _assert_no_sensitive_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_no_sensitive_keys(nested)


def test_match_started_uses_display_names_and_recursively_sanitizes_config() -> None:
    xss_name = '<img src=x onerror="alert(1)">'
    event = _event(
        EventType.MATCH_STARTED,
        {
            "game": "gomoku",
            "seed": 7,
            "players": [
                {
                    "name": "internal-name",
                    "display_name": xss_name,
                    "entrant_id": "entrant-secret",
                    "route_id": "route-secret",
                    "provider": "provider-secret",
                    "model": "model-secret",
                },
                {"name": "Bob", "api_key": "descriptor-key-secret"},
            ],
            "game_config": {
                "board_size": 15,
                "endpoint": "endpoint-secret",
                "monkey": "kept-not-a-key-field",
                "nested": {
                    "provider_options": {"timeout": 3},
                    "safe": [
                        {"label": "visible", "route_id": "nested-route-secret"},
                        "plain",
                    ],
                    "x_api_key": "nested-key-secret",
                    "headers": {"Authorization": "header-secret"},
                },
            },
            "failure_details": {"message": "outer-failure-secret"},
        },
    )

    public = PublicEvent.from_event(event)
    payload = public.model_dump(mode="json")["data"]

    assert payload == {
        "game": "gomoku",
        "seed": 7,
        "game_config": {
            "board_size": 15,
        },
        "players": [xss_name, "Bob"],
    }
    _assert_no_sensitive_keys(public.model_dump(mode="json"))
    serialized = public.model_dump_json()
    for secret in (
        "entrant-secret",
        "route-secret",
        "provider-secret",
        "model-secret",
        "descriptor-key-secret",
        "endpoint-secret",
        "nested-route-secret",
        "nested-key-secret",
        "header-secret",
        "outer-failure-secret",
    ):
        assert secret not in serialized


def test_turn_prompt_maps_only_prompt_and_preserves_plain_xss_text() -> None:
    prompt = '<script>alert("prompt")</script>'
    public = PublicEvent.from_event(
        _event(
            EventType.TURN_PROMPT,
            {
                "prompt": prompt,
                "route_id": "route-secret",
                "nested": {"model": "model-secret"},
            },
            player="Alice",
        )
    )

    assert public.model_dump(mode="json")["data"] == {"prompt": prompt}
    assert public.model_dump(mode="json")["timestamp"] == "2026-01-02T01:04:05.006789Z"
    assert "route-secret" not in public.model_dump_json()
    assert "model-secret" not in public.model_dump_json()


def test_move_received_maps_only_move() -> None:
    move = '<b data-move="H8">H8</b>'
    public = PublicEvent.from_event(
        _event(
            EventType.MOVE_RECEIVED,
            {"move": move, "provider": "provider-secret", "raw_response": "raw-secret"},
            player="Alice",
        )
    )

    assert public.model_dump(mode="json")["data"] == {"move": move}
    assert "provider-secret" not in public.model_dump_json()
    assert "raw-secret" not in public.model_dump_json()


def test_move_rejected_uses_explicit_public_allow_list() -> None:
    public = PublicEvent.from_event(
        _event(
            EventType.MOVE_REJECTED,
            {
                "move": "Z99",
                "reason": "超出棋盘",
                "reason_code": "illegal_move",
                "forfeit": True,
                "technical_loss": False,
                "forfeit_scope": "turn",
                "attempt": 3,
                "failure_details": {
                    "endpoint": "endpoint-secret",
                    "headers": {"X-Key": "key-secret"},
                },
            },
            player="Alice",
        )
    )

    assert public.model_dump(mode="json")["data"] == {
        "move": "Z99",
        "reason": "超出棋盘",
        "reason_code": "illegal_move",
        "forfeit": True,
        "technical_loss": False,
    }
    assert "endpoint-secret" not in public.model_dump_json()
    assert "key-secret" not in public.model_dump_json()


def test_match_finished_exposes_only_safe_judging_counts() -> None:
    public = PublicEvent.from_event(
        _event(
            EventType.MATCH_FINISHED,
            {
                "scores": {"Alice": 1, "Bob": 0},
                "termination": "technical_loss",
                "reason": "provider response contained provider-secret",
                "reason_code": "timeout",
                "forfeited_by": "Bob",
                "failure_details": {"message": "failure-secret"},
                "judging": {
                    "panel_size": 3,
                    "successful_judges": 2,
                    "quorum": 2,
                    "panel": [
                        {
                            "provider": "judge-provider-secret",
                            "model": "judge-model-secret",
                            "route_id": "judge-route-secret",
                        }
                    ],
                    "failures": [{"failure_details": "judge-failure-secret"}],
                },
            },
        )
    )

    assert public.model_dump(mode="json")["data"] == {
        "scores": {"Alice": 1.0, "Bob": 0.0},
        "termination": "technical_loss",
        "reason_code": "timeout",
        "forfeited_by": "Bob",
        "judging": {"panel_size": 3, "successful_judges": 2, "quorum": 2},
    }
    serialized = public.model_dump_json()
    for secret in (
        "provider-secret",
        "failure-secret",
        "judge-provider-secret",
        "judge-model-secret",
        "judge-route-secret",
        "judge-failure-secret",
    ):
        assert secret not in serialized


def test_public_event_rejects_a_payload_for_the_wrong_event_type() -> None:
    prompt_event = PublicEvent.from_event(
        _event(EventType.TURN_PROMPT, {"prompt": "question"}, player="Alice")
    )

    with pytest.raises(ValidationError, match="wrong public payload"):
        PublicEvent(
            seq=prompt_event.seq,
            type=EventType.MOVE_RECEIVED,
            timestamp=prompt_event.timestamp,
            player=prompt_event.player,
            data=prompt_event.data,
        )


def test_summary_and_leaderboard_drop_stable_identity_fields() -> None:
    summary = StorageMatchSummary(
        match_id="match-1",
        game="gomoku",
        seed=42,
        players=("Alice", "Bob"),
        scores={"Alice": 1.0, "Bob": 0.0},
        started_at=STAMP,
        finished_at=STAMP + timedelta(seconds=2),
        entrant_ids=("entrant-secret-a", "entrant-secret-b"),
        rating_source="engine",
        rated=True,
        tournament_id="tournament-1",
        pairing_number=2,
        pairing_count=3,
    )
    rating = RatingEntry(
        player="Alice",
        rating=1512.5,
        games_played=4,
        wins=3,
        draws=0,
        losses=1,
        updated_at=STAMP,
        entrant_id="leaderboard-entrant-secret",
    )

    public_summary = MatchSummary.from_storage(summary)
    match_list = MatchListResponse.from_storage([summary])
    leaderboard = LeaderboardResponse.from_storage([rating], game="gomoku")
    public_mapping_summary = MatchSummary.from_storage(
        {
            **summary.__dict__,
            "started_at": summary.started_at.isoformat(),
            "finished_at": summary.finished_at.isoformat(),
            "entrant_ids": ["mapping-entrant-secret-a", "mapping-entrant-secret-b"],
            "detail_available": True,
        }
    )
    mapping_leaderboard = LeaderboardResponse.from_storage(
        [
            {
                **rating.__dict__,
                "display_name": rating.player,
                "updated_at": rating.updated_at.isoformat(),
                "entrant_id": "mapping-leaderboard-secret",
            }
        ],
        game="gomoku",
    )

    assert public_summary.players == ("Alice", "Bob")
    assert public_summary.rated is True
    assert public_summary.model_dump(mode="json")["started_at"] == ("2026-01-02T01:04:05.006789Z")
    assert match_list.matches == (public_summary,)
    assert public_mapping_summary == public_summary
    assert leaderboard.entries[0].player == "Alice"
    assert mapping_leaderboard.entries == leaderboard.entries
    assert leaderboard.entries[0].rating == 1512.5
    assert leaderboard.model_dump(mode="json")["entries"][0]["updated_at"] == (
        "2026-01-02T01:04:05.006789Z"
    )
    serialized = (
        public_summary.model_dump_json()
        + public_mapping_summary.model_dump_json()
        + leaderboard.model_dump_json()
        + mapping_leaderboard.model_dump_json()
    )
    assert "entrant-secret" not in serialized
    assert "leaderboard-entrant-secret" not in serialized
    assert "mapping-entrant-secret" not in serialized
    assert "mapping-leaderboard-secret" not in serialized
    assert "rating_source" not in serialized


def _archive_with_private_descriptors() -> MatchArchive:
    players = [
        {
            "name": '<script>alert("name")</script>',
            "display_name": '<script>alert("name")</script>',
            "entrant_id": "private:alice",
            "kind": "llm",
            "provider": "private-provider-a",
            "model": "private-model-a",
            "profile_id": "private-profile-a",
            "route_id": "private-route-a",
        },
        {
            "name": "Bob",
            "display_name": "Bob",
            "entrant_id": "private:bob",
            "kind": "llm",
            "provider": "private-provider-b",
            "model": "private-model-b",
            "profile_id": "private-profile-b",
            "route_id": "private-route-b",
        },
    ]
    events = [
        MatchEvent(
            seq=0,
            type=EventType.MATCH_STARTED,
            timestamp=STAMP,
            data={"game": "gomoku", "seed": 9, "game_config": {}, "players": players},
        ),
        MatchEvent(
            seq=1,
            type=EventType.TURN_PROMPT,
            timestamp=STAMP + timedelta(seconds=1),
            player=players[0]["name"],
            data={"prompt": "board"},
        ),
        MatchEvent(
            seq=2,
            type=EventType.MOVE_RECEIVED,
            timestamp=STAMP + timedelta(seconds=2),
            player=players[0]["name"],
            data={"move": "H8"},
        ),
        MatchEvent(
            seq=3,
            type=EventType.MATCH_FINISHED,
            timestamp=STAMP + timedelta(seconds=3),
            data={
                "scores": {players[0]["name"]: 1.0, "Bob": 0.0},
                "termination": "completed",
            },
        ),
    ]
    return archive_from_events(
        game="gomoku",
        seed=9,
        players=players,
        events=events,
        started_at=STAMP,
        finished_at=STAMP + timedelta(seconds=3),
    )


def test_match_detail_and_websocket_envelopes_never_reemit_descriptors() -> None:
    archive = _archive_with_private_descriptors()

    detail = MatchDetail.from_archive(archive)
    archive_envelope = WSArchiveEnvelope.from_archive(archive)
    event_envelope = WSEventEnvelope.from_event(archive.match_id, archive.events[0])
    complete_envelope = WSCompleteEnvelope(
        match_id=archive.match_id,
        event_count=len(archive.events),
    )

    assert detail.match.players == ('<script>alert("name")</script>', "Bob")
    assert archive_envelope.type == "archive"
    assert event_envelope.type == "event"
    assert complete_envelope.type == "complete"
    serialized = (
        f"{detail.model_dump_json()}\n{archive_envelope.model_dump_json()}\n"
        f"{event_envelope.model_dump_json()}\n{complete_envelope.model_dump_json()}"
    )
    for secret in (
        "private:alice",
        "private:bob",
        "private-provider-a",
        "private-provider-b",
        "private-model-a",
        "private-model-b",
        "private-profile-a",
        "private-profile-b",
        "private-route-a",
        "private-route-b",
    ):
        assert secret not in serialized


def test_health_and_game_metadata_are_explicitly_versioned() -> None:
    class _Game:
        supported_modes = ("play", "round_robin")
        requires_judge_panel = True

    health = HealthResponse(
        status="ok",
        service_version=__version__,
        database_available=True,
        database_schema_version=8,
    )
    game = GameInfo.from_game("creative_writing", _Game)

    assert health.api_version == "v1"
    assert health.service_version == __version__
    assert game.supported_modes == ("play", "round_robin")
    assert game.requires_judge_panel is True
