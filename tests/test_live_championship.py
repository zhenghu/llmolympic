"""Live schema-v2 migration and championship lifecycle coverage."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

import llmolympic.live as live_module
from llmolympic.core.events import EventType, MatchEvent
from llmolympic.live import (
    LIVE_SCHEMA_VERSION,
    LivePublisher,
    derive_live_database_path,
    inspect_live_database,
)
from llmolympic.web.live_reader import LiveReadError, LiveSQLiteReader
from llmolympic.web.models import PublicEvent

STAMP = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _started_for(first: str, second: str, *, seq: int = 0) -> MatchEvent:
    players = [
        {
            "name": name,
            "display_name": name,
            "entrant_id": f"private:{index}",
            "kind": "mock",
        }
        for index, name in enumerate((first, second), start=1)
    ]
    return MatchEvent(
        seq=seq,
        type=EventType.MATCH_STARTED,
        timestamp=STAMP,
        data={
            "game": "math_quiz",
            "seed": 7,
            "players": players,
            "game_config": {"rounds": 1},
        },
    )


def _finished_for(first: str, second: str, *, seq: int = 1) -> MatchEvent:
    return MatchEvent(
        seq=seq,
        type=EventType.MATCH_FINISHED,
        timestamp=STAMP,
        data={
            "scores": {first: 1.0, second: 0.0},
            "termination": "completed",
        },
    )


def _context(
    player_count: int,
    round_number: int,
    round_pairing_number: int,
    leg_number: int,
) -> dict[str, int]:
    return {
        "round_number": round_number,
        "round_count": player_count.bit_length() - 1,
        "round_pairing_number": round_pairing_number,
        "round_pairing_count": player_count >> round_number,
        "pairing_number": (
            player_count
            - (player_count >> (round_number - 1))
            + round_pairing_number
        ),
        "pairing_count": player_count - 1,
        "leg_number": leg_number,
    }


def _committed_rounds(
    roster: tuple[str, ...],
    completed_round_count: int,
) -> tuple[list[dict[str, object]], tuple[str, ...]]:
    sources = roster
    entries: list[dict[str, object]] = []
    pairing_number = 0
    for round_number in range(1, completed_round_count + 1):
        winners: list[str] = []
        for round_pairing_number in range(1, len(sources) // 2 + 1):
            first = sources[2 * (round_pairing_number - 1)]
            second = sources[2 * round_pairing_number - 1]
            pairing_number += 1
            winners.append(first)
            entries.append(
                {
                    "round_number": round_number,
                    "round_pairing_number": round_pairing_number,
                    "pairing_number": pairing_number,
                    "players": [first, second],
                    "winner": first,
                    "series_id": f"series-{pairing_number}",
                    "match_ids": [
                        f"match-{2 * pairing_number - 1}",
                        f"match-{2 * pairing_number}",
                    ],
                    "status": "committed",
                }
            )
        sources = tuple(winners)
    return entries, sources


def _flush(publisher: LivePublisher) -> None:
    publisher._queue.join()


def _create_v1_sidecar(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    projected = PublicEvent.from_event(_started_for("甲", "乙")).model_dump(mode="json")
    event_json = _canonical_json(
        {"seq": 0, "context": {"match_event_seq": 0}, "event": projected}
    )
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE live_sessions (
                live_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL CHECK (schema_version = 1),
                mode TEXT NOT NULL CHECK (mode IN ('play', 'series', 'round_robin')),
                game TEXT NOT NULL,
                players_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'interrupted')),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                heartbeat_at REAL NOT NULL,
                lease_expires_at REAL NOT NULL,
                current_context_json TEXT NOT NULL,
                next_seq INTEGER NOT NULL CHECK (next_seq >= 0),
                event_count INTEGER NOT NULL CHECK (event_count >= 0),
                event_bytes INTEGER NOT NULL CHECK (event_bytes >= 0),
                final_kind TEXT CHECK (final_kind IN ('match', 'series', 'tournament')),
                final_id TEXT,
                final_match_ids_json TEXT,
                interruption_code TEXT,
                owner_token_digest BLOB NOT NULL,
                CHECK (
                    (status = 'running' AND final_kind IS NULL AND final_id IS NULL
                     AND final_match_ids_json IS NULL AND interruption_code IS NULL)
                    OR
                    (status = 'completed' AND final_kind IS NOT NULL AND final_id IS NOT NULL
                     AND final_match_ids_json IS NOT NULL AND interruption_code IS NULL)
                    OR
                    (status = 'interrupted' AND final_kind IS NULL AND final_id IS NULL
                     AND final_match_ids_json IS NULL AND interruption_code IS NOT NULL)
                ),
                CHECK (next_seq = event_count)
            );
            CREATE INDEX live_sessions_status_updated_idx
                ON live_sessions(status, updated_at DESC);
            CREATE INDEX live_sessions_updated_idx
                ON live_sessions(updated_at DESC);
            CREATE TABLE live_events (
                live_id TEXT NOT NULL REFERENCES live_sessions(live_id) ON DELETE CASCADE,
                seq INTEGER NOT NULL CHECK (seq >= 0),
                created_at REAL NOT NULL,
                event_bytes INTEGER NOT NULL CHECK (event_bytes > 0),
                event_json TEXT NOT NULL,
                PRIMARY KEY (live_id, seq)
            ) WITHOUT ROWID;
            PRAGMA user_version = 1;
            """
        )
        connection.execute(
            """
            INSERT INTO live_sessions (
                live_id, schema_version, mode, game, players_json, status,
                created_at, updated_at, heartbeat_at, lease_expires_at,
                current_context_json, next_seq, event_count, event_bytes,
                owner_token_digest
            ) VALUES ('legacy-live', 1, 'play', 'math_quiz', '["甲","乙"]',
                      'running', 1, 1, 1, 4000000000, '{}', 1, 1, ?, zeroblob(32))
            """,
            (len(event_json.encode("utf-8")),),
        )
        connection.execute(
            """
            INSERT INTO live_events(live_id, seq, created_at, event_bytes, event_json)
            VALUES ('legacy-live', 0, 1, ?, ?)
            """,
            (len(event_json.encode("utf-8")), event_json),
        )
    return event_json


def test_v1_reader_is_read_only_and_publisher_migration_preserves_events(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.db"
    path = derive_live_database_path(archive)
    legacy_event = _create_v1_sidecar(path)
    before = path.read_bytes()

    assert inspect_live_database(path) == {
        "available": True,
        "schema_version": 1,
        "session_count": 1,
    }
    assert path.read_bytes() == before
    legacy_detail = LiveSQLiteReader(archive).load_live("legacy-live")
    assert legacy_detail.match.mode == "play"
    assert legacy_detail.events[0].kind == "match_event"
    assert path.read_bytes() == before

    publisher = LivePublisher(archive, "play", heartbeat_seconds=0.05)
    assert not publisher.failed
    new_live_id = publisher.start_session(_started_for("丙", "丁"))
    assert new_live_id is not None
    _flush(publisher)
    publisher.close()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == LIVE_SCHEMA_VERSION
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(live_sessions)")
        }
        assert "championship_bracket_json" in columns
        legacy = connection.execute(
            "SELECT schema_version FROM live_sessions WHERE live_id = 'legacy-live'"
        ).fetchone()
        assert legacy == (1,)
        assert connection.execute(
            "SELECT event_json FROM live_events WHERE live_id = 'legacy-live'"
        ).fetchone() == (legacy_event,)
        new_event = json.loads(
            connection.execute(
                "SELECT event_json FROM live_events WHERE live_id = ? AND seq = 0",
                (new_live_id,),
            ).fetchone()[0]
        )
        assert new_event["kind"] == "match_event"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert LiveSQLiteReader(archive).load_live("legacy-live").events[0].kind == "match_event"


def test_v1_migration_failure_rolls_back_schema_and_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "archive.db"
    path = derive_live_database_path(archive)
    legacy_event = _create_v1_sidecar(path)
    original = live_module._create_live_sessions_v2

    def fail_after_create(connection: sqlite3.Connection, table: str) -> None:
        original(connection, table)
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(live_module, "_create_live_sessions_v2", fail_after_create)
    publisher = LivePublisher(archive, "play", heartbeat_seconds=0.05)
    assert publisher.failed
    publisher.close()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(live_sessions)")
        }
        assert "championship_bracket_json" not in columns
        assert connection.execute("SELECT count(*) FROM live_sessions").fetchone() == (1,)
        assert connection.execute("SELECT event_json FROM live_events").fetchone() == (
            legacy_event,
        )


def test_v1_reader_rejects_an_injected_event_kind(tmp_path: Path) -> None:
    archive = tmp_path / "archive.db"
    path = derive_live_database_path(archive)
    legacy_event = json.loads(_create_v1_sidecar(path))
    legacy_event["kind"] = "round_committed"
    serialized = _canonical_json(legacy_event)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE live_events SET event_json = ?, event_bytes = ?",
            (serialized, len(serialized.encode("utf-8"))),
        )
        connection.execute(
            "UPDATE live_sessions SET event_bytes = ?",
            (len(serialized.encode("utf-8")),),
        )

    with pytest.raises(LiveReadError, match="live_invalid"):
        LiveSQLiteReader(archive).load_live("legacy-live")


@pytest.mark.parametrize("player_count", [4, 8, 16])
def test_championship_resume_materializes_and_completes_canonical_bracket(
    tmp_path: Path,
    player_count: int,
) -> None:
    roster = tuple(f"选手{index}" for index in range(1, player_count + 1))
    round_count = player_count.bit_length() - 1
    initial, finalists = _committed_rounds(roster, round_count - 1)
    context_one = _context(player_count, round_count, 1, 1)
    context_two = _context(player_count, round_count, 1, 2)
    championship_id = f"championship-{player_count}"
    archive = tmp_path / str(player_count) / "archive.db"
    publisher = LivePublisher(archive, "championship", heartbeat_seconds=0.05)
    live_id = publisher.start_session(
        _started_for(*finalists),
        context=context_one,
        championship_id=championship_id,
        game="math_quiz",
        players=roster,
        initial_bracket=initial,
    )

    assert live_id is not None
    assert publisher.publish(
        live_id,
        _finished_for(*finalists),
        context=context_two,
    )
    final_pairing_number = player_count - 1
    final_match_ids = (
        f"match-{2 * final_pairing_number - 1}",
        f"match-{2 * final_pairing_number}",
    )
    assert publisher.publish_pairing_completed(
        live_id,
        context=context_two,
        players=finalists,
        winner=finalists[0],
        series_id=f"series-{final_pairing_number}",
        match_ids=final_match_ids,
    )
    assert publisher.publish_round_committed(live_id, context=context_two)
    assert publisher.complete(
        live_id,
        championship_id=championship_id,
        champion=finalists[0],
    )
    publisher.close()

    path = derive_live_database_path(archive)
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            """
            SELECT mode, players_json, status, final_kind, final_id,
                   final_match_ids_json, championship_bracket_json
            FROM live_sessions WHERE live_id = ?
            """,
            (live_id,),
        ).fetchone()
        events = [
            json.loads(item[0])
            for item in connection.execute(
                "SELECT event_json FROM live_events WHERE live_id = ? ORDER BY seq",
                (live_id,),
            )
        ]
    assert row[:5] == (
        "championship",
        _canonical_json(roster),
        "completed",
        "championship",
        championship_id,
    )
    canonical_ids = [
        match_id
        for pairing in [*initial, {"match_ids": list(final_match_ids)}]
        for match_id in pairing["match_ids"]
    ]
    assert json.loads(row[5]) == canonical_ids
    assert len(canonical_ids) == {4: 6, 8: 14, 16: 30}[player_count]
    bracket = json.loads(row[6])
    assert bracket["championship_id"] == championship_id
    assert bracket["champion"] == finalists[0]
    assert len(bracket["pairings"]) == player_count - 1
    assert {pairing["status"] for pairing in bracket["pairings"]} == {"committed"}
    assert [event["kind"] for event in events[-2:]] == [
        "pairing_completed",
        "round_committed",
    ]

    detail = LiveSQLiteReader(archive).load_live(live_id)
    assert detail.match.mode == "championship"
    assert detail.match.players == roster
    assert detail.match.final_kind == "championship"
    assert detail.match.final_id == championship_id
    assert detail.match.final_match_ids == tuple(canonical_ids)
    assert detail.match.championship_bracket is not None
    assert detail.match.championship_bracket.champion == finalists[0]
    assert [item.kind for item in detail.events[-2:]] == [
        "pairing_completed",
        "round_committed",
    ]


def test_championship_pairing_is_provisional_until_round_commit(tmp_path: Path) -> None:
    roster = ("甲", "乙", "丙", "丁")
    archive = tmp_path / "archive.db"
    publisher = LivePublisher(archive, "championship", heartbeat_seconds=0.05)
    first_one = _context(4, 1, 1, 1)
    first_two = _context(4, 1, 1, 2)
    live_id = publisher.start_session(
        _started_for("甲", "乙"),
        context=first_one,
        championship_id="championship-provisional",
        game="math_quiz",
        players=roster,
    )
    assert live_id is not None
    assert publisher.publish(live_id, _finished_for("甲", "乙"), context=first_two)
    assert publisher.publish_pairing_completed(
        live_id,
        context=first_two,
        players=("甲", "乙"),
        winner="甲",
        series_id="series-1",
        match_ids=("match-1", "match-2"),
    )
    assert not publisher.publish_round_committed(live_id, context=first_two)
    _flush(publisher)

    path = derive_live_database_path(archive)
    with sqlite3.connect(path) as connection:
        bracket = json.loads(
            connection.execute(
                "SELECT championship_bracket_json FROM live_sessions WHERE live_id = ?",
                (live_id,),
            ).fetchone()[0]
        )
    assert bracket["champion"] is None
    assert [pairing["status"] for pairing in bracket["pairings"]] == ["provisional"]
    provisional_detail = LiveSQLiteReader(archive).load_live(live_id)
    assert provisional_detail.match.pairing_number == 1
    assert provisional_detail.match.leg_number == 2
    assert provisional_detail.match.championship_bracket is not None
    assert provisional_detail.match.championship_bracket.pairings[0].status == (
        "provisional"
    )

    second_one = _context(4, 1, 2, 1)
    second_two = _context(4, 1, 2, 2)
    assert publisher.publish(
        live_id,
        _started_for("丙", "丁"),
        context=second_one,
    )
    assert publisher.publish(
        live_id,
        _finished_for("丙", "丁"),
        context=second_two,
    )
    assert publisher.publish_pairing_completed(
        live_id,
        context=second_two,
        players=("丙", "丁"),
        winner="丙",
        series_id="series-2",
        match_ids=("match-3", "match-4"),
    )
    assert publisher.publish_round_committed(live_id, context=second_two)
    _flush(publisher)

    with sqlite3.connect(path) as connection:
        bracket = json.loads(
            connection.execute(
                "SELECT championship_bracket_json FROM live_sessions WHERE live_id = ?",
                (live_id,),
            ).fetchone()[0]
        )
        kinds = [
            json.loads(row[0])["kind"]
            for row in connection.execute(
                "SELECT event_json FROM live_events WHERE live_id = ? ORDER BY seq",
                (live_id,),
            )
        ]
    assert [pairing["status"] for pairing in bracket["pairings"]] == [
        "committed",
        "committed",
    ]
    assert kinds.count("pairing_completed") == 2
    assert kinds[-1] == "round_committed"
    committed_detail = LiveSQLiteReader(archive).load_live(live_id)
    assert committed_detail.match.pairing_number == 2
    assert {
        pairing.status
        for pairing in committed_detail.match.championship_bracket.pairings
    } == {"committed"}
    publisher.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("round_count", 3),
        ("round_pairing_count", 1),
        ("round_pairing_number", 3),
        ("pairing_number", 2),
        ("pairing_count", 4),
        ("leg_number", 3),
    ],
)
def test_championship_context_formula_is_strict(
    tmp_path: Path,
    field: str,
    value: int,
) -> None:
    context = _context(4, 1, 1, 1)
    context[field] = value
    publisher = LivePublisher(
        tmp_path / field / "archive.db",
        "championship",
        heartbeat_seconds=0.05,
    )

    assert (
        publisher.start_session(
            _started_for("甲", "乙"),
            context=context,
            championship_id="championship-invalid",
            game="math_quiz",
            players=("甲", "乙", "丙", "丁"),
        )
        is None
    )
    publisher.close()


def test_championship_resume_rejects_partial_committed_round(tmp_path: Path) -> None:
    roster = ("甲", "乙", "丙", "丁")
    initial, _ = _committed_rounds(roster, 1)
    publisher = LivePublisher(
        tmp_path / "archive.db",
        "championship",
        heartbeat_seconds=0.05,
    )

    assert (
        publisher.start_session(
            _started_for("丙", "丁"),
            context=_context(4, 1, 2, 1),
            championship_id="championship-partial",
            game="math_quiz",
            players=roster,
            initial_bracket=initial[:1],
        )
        is None
    )
    publisher.close()
