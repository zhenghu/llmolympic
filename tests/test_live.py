"""Stage 4.3 durable live-broker and read-only reader tests."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from llmolympic.core.events import EventType, MatchEvent
from llmolympic.core.storage import SQLiteStore
from llmolympic.live import (
    LIVE_SCHEMA_VERSION,
    LivePublisher,
    derive_live_database_path,
)
from llmolympic.web.live_reader import LiveReadError, LiveSQLiteReader

STAMP = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)


class _Clock:
    def __init__(self, value: float = 1_800_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _event(
    seq: int,
    type_: EventType,
    data: dict[str, object],
    *,
    player: str | None = None,
) -> MatchEvent:
    return MatchEvent(seq=seq, type=type_, timestamp=STAMP, player=player, data=data)


def _started(*, secrets: bool = False) -> MatchEvent:
    players = [
        {
            "name": "甲",
            "display_name": "甲",
            "entrant_id": "private:entrant-a",
            "kind": "llm",
            "provider": "private-provider",
            "model": "private-model",
            "route_id": "route:v1:private",
            "api_key": "private-key",
        },
        {
            "name": "乙",
            "display_name": "乙",
            "entrant_id": "private:entrant-b",
            "kind": "mock",
            "model": "private-second-model",
        },
    ]
    data: dict[str, object] = {
        "game": "math_quiz",
        "seed": 7,
        "players": players,
        "game_config": {"rounds": 1},
    }
    if secrets:
        data.update(
            {
                "failure_details": {"headers": {"Authorization": "bearer-secret"}},
                "endpoint": "https://private.invalid/v1",
            }
        )
        data["game_config"] = {
            "rounds": 1,
            "endpoint": "https://nested-private.invalid",
            "headers": {"X-Key": "nested-secret"},
            "criteria": {
                "visible": "kept",
                "api_key": "criteria-secret",
                "nested": {"route_id": "criteria-route-secret"},
            },
        }
    return _event(0, EventType.MATCH_STARTED, data)


def _prompt(seq: int = 1) -> MatchEvent:
    return _event(
        seq,
        EventType.TURN_PROMPT,
        {
            "prompt": "1 + 1 = ?",
            "provider": "prompt-provider-secret",
            "nested": {"api_key": "prompt-key-secret"},
        },
        player="甲",
    )


def _finished(seq: int = 2) -> MatchEvent:
    return _event(
        seq,
        EventType.MATCH_FINISHED,
        {
            "scores": {"甲": 1.0, "乙": 0.0},
            "termination": "completed",
            "failure_details": {"message": "finished-secret"},
        },
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _close(publisher: LivePublisher) -> None:
    publisher.close()


def _flush(publisher: LivePublisher) -> None:
    """Wait for commands already accepted by the best-effort worker."""

    publisher._queue.join()


def _error_code(callable_) -> str:
    with pytest.raises(LiveReadError) as caught:
        callable_()
    return caught.value.code


def test_missing_live_sidecar_is_empty_and_never_created(tmp_path: Path) -> None:
    archive = tmp_path / "archive ? #.db"
    live_path = derive_live_database_path(archive)
    reader = LiveSQLiteReader(archive)

    assert live_path == archive.with_name(f"{archive.name}.live.db")
    assert reader.path == live_path.resolve()
    assert reader.list_live() == []
    assert _error_code(lambda: reader.load_live("missing")) == "live_not_found"
    assert not archive.exists()
    assert not live_path.exists()


def test_publisher_reader_round_trip_is_public_contiguous_and_completed(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.db"
    clock = _Clock()
    publisher = LivePublisher(archive, "play", clock=clock, heartbeat_seconds=0.05)
    live_id = publisher.start_session(_started(secrets=True))

    assert live_id is not None
    assert publisher.publish(live_id, _prompt())
    assert publisher.publish(live_id, _finished())

    # A match_finished event is not a durable completion acknowledgement.
    _flush(publisher)
    running = LiveSQLiteReader(archive, clock=clock).load_live(live_id)
    assert running.match.status == "running"
    assert [item.seq for item in running.events] == [0, 1, 2]
    assert [item.context.match_event_seq for item in running.events] == [0, 1, 2]
    assert [item.event.seq for item in running.events] == [0, 1, 2]
    assert running.next_seq == 3
    assert not running.has_more

    assert publisher.complete(
        live_id,
        final_kind="match",
        final_id="final-match",
        final_match_ids=("final-match",),
    )
    _close(publisher)

    detail = LiveSQLiteReader(archive, clock=clock).load_live(live_id, from_seq=1, limit=1)
    assert detail.match.status == "completed"
    assert detail.match.final_kind == "match"
    assert detail.match.final_id == "final-match"
    assert detail.match.final_match_ids == ("final-match",)
    assert [item.seq for item in detail.events] == [1]
    assert detail.next_seq == 2
    assert detail.has_more


def test_sidecar_contains_only_public_projection(tmp_path: Path) -> None:
    archive = tmp_path / "archive.db"
    publisher = LivePublisher(archive, "play", heartbeat_seconds=0.05)
    live_id = publisher.start_session(_started(secrets=True))
    assert live_id is not None
    assert publisher.publish(live_id, _prompt())
    _close(publisher)

    path = derive_live_database_path(archive)
    with sqlite3.connect(path) as connection:
        session = connection.execute(
            "SELECT players_json FROM live_sessions WHERE live_id = ?", (live_id,)
        ).fetchone()
        events = connection.execute(
            "SELECT event_json FROM live_events WHERE live_id = ? ORDER BY seq", (live_id,)
        ).fetchall()
    assert session is not None
    assert json.loads(session[0]) == ["甲", "乙"]
    serialized = "\n".join(row[0] for row in events)
    for secret in (
        "private:entrant-a",
        "private-provider",
        "private-model",
        "private-second-model",
        "route:v1:private",
        "private-key",
        "bearer-secret",
        "private.invalid",
        "nested-secret",
        "criteria-secret",
        "criteria-route-secret",
        "prompt-provider-secret",
        "prompt-key-secret",
    ):
        assert secret not in serialized

    detail = LiveSQLiteReader(archive).load_live(live_id)
    assert detail.match.players == ("甲", "乙")
    started_data = detail.events[0].event.data.model_dump(mode="json")
    assert started_data["game"] == "math_quiz"
    assert started_data["seed"] == 7
    assert started_data["players"] == ["甲", "乙"]
    assert set(started_data["game_config"]) == {"criteria", "rounds"}
    assert started_data["game_config"]["criteria"]["visible"] == "kept"
    assert started_data["game_config"]["rounds"] == 1
    assert detail.events[1].event.data.model_dump(mode="json") == {
        "prompt": "1 + 1 = ?"
    }


def test_broker_sequence_is_global_across_legs_while_match_sequence_restarts(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.db"
    publisher = LivePublisher(archive, "series", heartbeat_seconds=0.05)
    live_id = publisher.start_session(_started(), context={"leg_number": 1})
    assert live_id is not None
    assert publisher.publish(live_id, _finished(seq=1), context={"leg_number": 1})
    assert publisher.publish(live_id, _started(), context={"leg_number": 2})
    assert publisher.publish(live_id, _prompt(), context={"leg_number": 2})
    _close(publisher)

    items = LiveSQLiteReader(archive).load_live(live_id).events
    assert [item.seq for item in items] == [0, 1, 2, 3]
    assert [item.context.leg_number for item in items] == [1, 1, 2, 2]
    assert [item.context.match_event_seq for item in items] == [0, 1, 0, 1]
    assert [item.event.seq for item in items] == [0, 1, 0, 1]


def test_round_robin_keeps_pairing_count_only_in_session_context(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.db"
    publisher = LivePublisher(archive, "round_robin", heartbeat_seconds=0.05)
    first_context = {"pairing_number": 1, "pairing_count": 2, "leg_number": 1}
    live_id = publisher.start_session(_started(), context=first_context)
    assert live_id is not None
    assert publisher.publish(
        live_id,
        _finished(seq=1),
        context=first_context,
    )
    second_context = {"pairing_number": 2, "pairing_count": 2, "leg_number": 2}
    assert publisher.publish(live_id, _started(), context=second_context)
    assert (
        publisher.complete(
            live_id,
            final_kind="tournament",
            final_id="tournament-final",
            final_match_ids=("match-1", "match-2", "match-3", "match-4"),
        )
        is True
    )
    _close(publisher)

    detail = LiveSQLiteReader(archive).load_live(live_id)
    assert detail.match.status == "completed"
    assert detail.match.pairing_number == 2
    assert detail.match.pairing_count == 2
    assert detail.match.leg_number == 2
    assert [item.context.model_dump(exclude_none=True) for item in detail.events] == [
        {"pairing_number": 1, "leg_number": 1, "match_event_seq": 0},
        {"pairing_number": 1, "leg_number": 1, "match_event_seq": 1},
        {"pairing_number": 2, "leg_number": 2, "match_event_seq": 0},
    ]


def test_complete_rejects_duplicate_and_mode_inconsistent_match_ids(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.db"
    publisher = LivePublisher(archive, "series", heartbeat_seconds=0.05)
    live_id = publisher.start_session(_started(), context={"leg_number": 1})
    assert live_id is not None
    assert not publisher.complete(
        live_id,
        final_kind="series",
        final_id="series-final",
        final_match_ids=("match-1",),
    )
    assert not publisher.complete(
        live_id,
        final_kind="series",
        final_id="series-final",
        final_match_ids=("match-1", "match-1"),
    )
    assert publisher.complete(
        live_id,
        final_kind="series",
        final_id="series-final",
        final_match_ids=("match-1", "match-2"),
    )
    _close(publisher)
    assert LiveSQLiteReader(archive).load_live(live_id).match.status == "completed"


def test_start_rejects_reader_invalid_players_and_mode_context(tmp_path: Path) -> None:
    one_player_data = dict(_started().data)
    one_player_data["players"] = list(one_player_data["players"])[:1]
    one_player = _event(0, EventType.MATCH_STARTED, one_player_data)
    cases = (
        ("play", one_player, None),
        ("play", _started(), {"leg_number": 1}),
        ("series", _started(), None),
        (
            "round_robin",
            _started(),
            {"pairing_number": 1, "leg_number": 1},
        ),
    )

    for index, (mode, event, context) in enumerate(cases):
        archive = tmp_path / str(index) / "archive.db"
        publisher = LivePublisher(archive, mode, heartbeat_seconds=0.05)
        assert publisher.start_session(event, context=context) is None
        _close(publisher)
        assert LiveSQLiteReader(archive).list_live() == []


def test_queue_overflow_close_stops_worker_without_false_closed_state(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.db"
    publisher = LivePublisher(
        archive,
        "play",
        queue_size=1,
        heartbeat_seconds=0.05,
    )
    live_id = publisher.start_session(_started())
    assert live_id is not None
    _flush(publisher)

    entered = threading.Event()
    release = threading.Event()
    original_apply = publisher._apply

    def blocking_apply(connection: sqlite3.Connection, item: object) -> None:
        if isinstance(item, tuple) and item[0] == "event" and not entered.is_set():
            entered.set()
            release.wait(timeout=5.0)
        original_apply(connection, item)

    publisher._apply = blocking_apply  # type: ignore[method-assign]
    try:
        assert publisher.publish(live_id, _prompt(seq=1))
        assert entered.wait(timeout=1.0)
        assert publisher.publish(live_id, _finished(seq=2))
        assert not publisher.publish(live_id, _prompt(seq=3))

        publisher.close(timeout=0.01)
        assert publisher._thread is not None
        assert publisher._thread.is_alive()
        assert not publisher._closed.is_set()
    finally:
        release.set()

    assert publisher._closed.wait(timeout=1.0)
    publisher.close(timeout=1.0)
    assert publisher._thread is not None
    assert not publisher._thread.is_alive()


def test_expired_lease_is_projected_as_interrupted_without_mutating_sidecar(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.db"
    clock = _Clock()
    publisher = LivePublisher(archive, "play", clock=clock, heartbeat_seconds=0.05)
    live_id = publisher.start_session(_started())
    assert live_id is not None
    _close(publisher)
    live_path = derive_live_database_path(archive)
    with sqlite3.connect(live_path) as connection:
        connection.execute(
            """
            UPDATE live_sessions
            SET status = 'running', interruption_code = NULL, lease_expires_at = ?
            WHERE live_id = ?
            """,
            (clock.value + 60.0, live_id),
        )
    before = (_digest(live_path), live_path.stat().st_mtime_ns)

    clock.value += 61.0
    reader = LiveSQLiteReader(archive, clock=clock)
    assert reader.list_live()[0].status == "interrupted"
    assert reader.load_live(live_id).match.status == "interrupted"
    assert (_digest(live_path), live_path.stat().st_mtime_ns) == before

    with sqlite3.connect(f"file:{live_path}?mode=ro", uri=True) as connection:
        assert connection.execute(
            "SELECT status FROM live_sessions WHERE live_id = ?", (live_id,)
        ).fetchone()[0] == "running"


def test_new_publisher_reclaims_expired_running_session_at_capacity(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.db"
    clock = _Clock()
    first = LivePublisher(
        archive,
        "play",
        clock=clock,
        heartbeat_seconds=0.05,
        max_sessions=1,
    )
    expired_id = first.start_session(_started())
    assert expired_id is not None
    _close(first)

    live_path = derive_live_database_path(archive)
    with sqlite3.connect(live_path) as connection:
        connection.execute(
            """
            UPDATE live_sessions SET
                status = 'running', interruption_code = NULL, lease_expires_at = ?
            WHERE live_id = ?
            """,
            (clock.value - 1.0, expired_id),
        )

    second = LivePublisher(
        archive,
        "play",
        clock=clock,
        heartbeat_seconds=0.05,
        max_sessions=1,
    )
    replacement_id = second.start_session(_started())
    assert replacement_id is not None
    _flush(second)
    with sqlite3.connect(live_path) as connection:
        rows = connection.execute(
            "SELECT live_id, status FROM live_sessions ORDER BY live_id"
        ).fetchall()
    assert rows == [(replacement_id, "running")]
    _close(second)


def test_live_sidecar_is_private_and_does_not_modify_archive_database(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.db"
    SQLiteStore(archive)
    archive_stat = archive.stat()
    archive_before = (_digest(archive), archive_stat.st_mode, archive_stat.st_mtime_ns)

    publisher = LivePublisher(archive, "play", heartbeat_seconds=0.05)
    assert publisher.start_session(_started()) is not None
    _close(publisher)

    assert (_digest(archive), archive.stat().st_mode, archive.stat().st_mtime_ns) == archive_before
    live_path = derive_live_database_path(archive)
    with sqlite3.connect(f"file:{live_path}?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == LIVE_SCHEMA_VERSION
    if os.name == "posix":
        assert live_path.stat().st_mode & 0o777 == 0o600
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(f"{live_path}{suffix}")
            if sidecar.exists():
                assert sidecar.stat().st_mode & 0o777 == 0o600


def test_unavailable_broker_is_best_effort_and_never_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("occupied")
    archive = blocker / "archive.db"

    publisher = LivePublisher(archive, "play", heartbeat_seconds=0.05)
    live_id = publisher.start_session(_started())
    assert live_id is None
    assert publisher.publish("unavailable", _prompt()) is False
    assert (
        publisher.complete(
            "unavailable",
            final_kind="match",
            final_id="final-match",
            final_match_ids=("final-match",),
        )
        is False
    )
    assert publisher.interrupt("unavailable", reason_code="broker_failed") is False
    publisher.close()


def test_publisher_rejects_negative_match_event_sequence_before_disk(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.db"
    publisher = LivePublisher(archive, "play", heartbeat_seconds=0.05)
    invalid = _started().model_copy(update={"seq": -1})

    assert publisher.start_session(invalid) is None
    assert publisher.failed
    publisher.close()

    assert LiveSQLiteReader(archive).list_live() == []


def test_reader_rejects_event_sequence_gap_and_invalid_parameters(tmp_path: Path) -> None:
    archive = tmp_path / "archive.db"
    publisher = LivePublisher(archive, "play", heartbeat_seconds=0.05)
    live_id = publisher.start_session(_started())
    assert live_id is not None
    assert publisher.publish(live_id, _prompt())
    _close(publisher)
    live_path = derive_live_database_path(archive)

    with sqlite3.connect(live_path) as connection:
        connection.execute(
            "UPDATE live_events SET seq = 2 WHERE live_id = ? AND seq = 1", (live_id,)
        )
    reader = LiveSQLiteReader(archive)
    assert _error_code(lambda: reader.load_live(live_id)) == "live_invalid"
    assert _error_code(lambda: reader.load_live(live_id, from_seq=-1)) == "invalid_from_seq"
    assert _error_code(lambda: reader.load_live(live_id, limit=0)) == "invalid_limit"
    assert _error_code(lambda: reader.list_live(game="x' OR 1=1 --")) == "invalid_game_id"
