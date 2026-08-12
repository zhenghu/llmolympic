"""Security and consistency tests for the Stage 4 read-only web reader."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from llmolympic.core.archive import MatchArchive
from llmolympic.core.events import EventType, MatchEvent
from llmolympic.core.storage import SCHEMA_VERSION, SQLiteStore
from llmolympic.web.reader import (
    MAX_ARCHIVE_BYTES,
    MAX_ARCHIVE_EVENTS,
    LoadedMatch,
    WebReadError,
    WebSQLiteReader,
)

STARTED = datetime(2026, 8, 12, 8, 0, tzinfo=UTC)


def _archive(
    match_id: str = "web-match-1",
    *,
    seconds: int = 0,
    source: str = "local_engine",
) -> MatchArchive:
    started = STARTED + timedelta(seconds=seconds)
    players = [
        {
            "name": "甲",
            "display_name": "甲",
            "entrant_id": "web:test-a",
            "kind": "mock",
            "model": "first",
        },
        {
            "name": "乙",
            "display_name": "乙",
            "entrant_id": "web:test-b",
            "kind": "mock",
            "model": "second",
        },
    ]
    scores = {"甲": 1.0, "乙": 0.0}
    events = [
        MatchEvent(
            seq=0,
            type=EventType.MATCH_STARTED,
            timestamp=started,
            data={
                "game": "math_quiz",
                "seed": 7,
                "game_config": {},
                "players": players,
            },
        ),
        MatchEvent(
            seq=1,
            type=EventType.TURN_PROMPT,
            timestamp=started + timedelta(milliseconds=250),
            player="甲",
            data={"prompt": "1 + 1 = ?"},
        ),
        MatchEvent(
            seq=2,
            type=EventType.MOVE_RECEIVED,
            timestamp=started + timedelta(milliseconds=500),
            player="甲",
            data={"move": "2"},
        ),
        MatchEvent(
            seq=3,
            type=EventType.MATCH_FINISHED,
            timestamp=started + timedelta(seconds=1),
            data={"scores": scores, "termination": "completed"},
        ),
    ]
    return MatchArchive(
        schema_version=2,
        source=source,
        match_id=match_id,
        game="math_quiz",
        seed=7,
        players=players,
        events=events,
        moves=[],
        scores=scores,
        started_at=started,
        finished_at=started + timedelta(seconds=1),
    )


def _database(tmp_path: Path) -> tuple[Path, SQLiteStore, MatchArchive]:
    path = tmp_path / "web.db"
    store = SQLiteStore(path)
    archive = _archive()
    store.save_match(archive, rating_source="engine")
    return path, store, archive


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _error_code(call) -> str:
    with pytest.raises(WebReadError) as caught:
        call()
    assert str(caught.value) == caught.value.code
    return caught.value.code


def _archive_payload(path: Path, match_id: str = "web-match-1") -> dict:
    with sqlite3.connect(path) as connection:
        raw = connection.execute(
            "SELECT archive_json FROM matches WHERE match_id = ?", (match_id,)
        ).fetchone()[0]
    return json.loads(raw)


def _replace_archive(path: Path, payload: object, match_id: str = "web-match-1") -> None:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE matches SET archive_json = ? WHERE match_id = ?",
            (raw, match_id),
        )


def test_reader_returns_core_dtos_and_verified_archive(tmp_path: Path) -> None:
    path, _, archive = _database(tmp_path)
    reader = WebSQLiteReader(path)

    assert reader.health() == {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "read_only": True,
        "match_count": 1,
    }
    summaries = reader.list_matches(game="math_quiz", limit=10)
    assert len(summaries) == 1
    assert summaries[0].match_id == archive.match_id
    assert summaries[0].players == ("甲", "乙")
    assert summaries[0].entrant_ids == ("web:test-a", "web:test-b")

    ratings = reader.leaderboard(game="math_quiz")
    assert [entry.entrant_id for entry in ratings] == ["web:test-a", "web:test-b"]
    assert all(entry.games_played == 1 for entry in ratings)

    loaded = reader.load_match(archive.match_id)
    assert isinstance(loaded, LoadedMatch)
    assert loaded.summary == summaries[0]
    assert loaded.archive.model_dump(mode="json") == archive.model_dump(mode="json")
    assert reader.get_match(archive.match_id) == loaded


def test_all_reader_operations_leave_database_bytes_metadata_and_sidecars_unchanged(
    tmp_path: Path,
) -> None:
    path, _, archive = _database(tmp_path)
    reader = WebSQLiteReader(path)
    sidecars = [Path(f"{path}{suffix}") for suffix in ("-journal", "-wal", "-shm")]
    assert not any(sidecar.exists() for sidecar in sidecars)

    before_hash = _digest(path)
    before_stat = path.stat()
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        before_version = connection.execute("PRAGMA user_version").fetchone()[0]

    reader.health()
    reader.list_matches()
    reader.leaderboard()
    reader.load_match(archive.match_id)

    after_stat = path.stat()
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        after_version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert _digest(path) == before_hash
    assert after_stat.st_mode == before_stat.st_mode
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert after_stat.st_size == before_stat.st_size
    assert before_version == after_version == SCHEMA_VERSION
    assert not any(sidecar.exists() for sidecar in sidecars)


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        (lambda path: None, "database_unavailable"),
        (
            lambda path: (
                sqlite3.connect(path).execute("PRAGMA user_version = 7").connection.close()
            ),
            "database_schema_unsupported",
        ),
    ],
)
def test_missing_or_old_database_is_rejected_without_creation(
    tmp_path: Path,
    setup,
    expected: str,
) -> None:
    path = tmp_path / "untrusted.db"
    setup(path)
    reader = WebSQLiteReader(path)

    assert _error_code(reader.health) == expected
    if expected == "database_unavailable":
        assert not path.exists()
    assert not any(Path(f"{path}{suffix}").exists() for suffix in ("-journal", "-wal", "-shm"))


def test_current_version_with_incomplete_schema_is_rejected_stably(tmp_path: Path) -> None:
    path = tmp_path / "incomplete.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE matches (match_id TEXT PRIMARY KEY)")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    error = _error_code(WebSQLiteReader(path).health)
    assert error == "database_schema_invalid"
    assert str(path) not in error


def test_reader_validates_limits_ids_and_never_interpolates_input(tmp_path: Path) -> None:
    path, _, _ = _database(tmp_path)
    reader = WebSQLiteReader(path)

    for limit in (0, 101, -1, True, "1"):
        assert _error_code(lambda limit=limit: reader.list_matches(limit=limit)) == "invalid_limit"
        assert _error_code(lambda limit=limit: reader.leaderboard(limit=limit)) == "invalid_limit"
    assert (
        _error_code(lambda: reader.list_matches(game="math_quiz' OR 1=1 --")) == "invalid_game_id"
    )
    assert _error_code(lambda: reader.load_match("web-match-1' OR 1=1 --")) == "invalid_match_id"
    assert _error_code(lambda: reader.load_match("a" * 129)) == "invalid_match_id"
    assert _error_code(lambda: reader.load_match("missing-match")) == "match_not_found"
    assert len(reader.list_matches(limit=1)) == 1
    assert len(reader.leaderboard(limit=1)) == 1


def test_external_and_legacy_archives_have_summaries_but_no_detail(tmp_path: Path) -> None:
    path, store, _ = _database(tmp_path)
    external = _archive("external-match", seconds=3, source="external")
    store.save_match(external, rating_source="imported")
    reader = WebSQLiteReader(path)

    assert {summary.match_id for summary in reader.list_matches()} == {
        "web-match-1",
        "external-match",
    }
    assert _error_code(lambda: reader.load_match("external-match")) == "match_detail_unsupported"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["events"][1].update(seq=7),
        lambda payload: payload["events"].insert(1, dict(payload["events"][0])),
        lambda payload: payload["events"].append(dict(payload["events"][-1])),
        lambda payload: payload["events"][-1]["data"]["scores"].update({"甲": 0.0}),
        lambda payload: payload["events"][1].update(player="陌生选手"),
        lambda payload: payload["events"][1].update(timestamp="2026-08-13T08:00:00Z"),
        lambda payload: payload["events"][2].update(timestamp="2026-08-12T08:00:00.100000Z"),
        lambda payload: payload.update(finished_at="2026-08-11T08:00:00Z"),
        lambda payload: payload["moves"].append(
            {
                "player": "陌生选手",
                "prompt": "x",
                "move": "y",
                "accepted": True,
                "reason": None,
            }
        ),
    ],
)
def test_deep_archive_semantic_corruption_is_rejected(
    tmp_path: Path,
    mutate,
) -> None:
    path, _, _ = _database(tmp_path)
    payload = _archive_payload(path)
    mutate(payload)
    _replace_archive(path, payload)

    assert _error_code(lambda: WebSQLiteReader(path).load_match("web-match-1")) == "archive_invalid"


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE matches SET game = 'gomoku' WHERE match_id = 'web-match-1'",
        (
            'UPDATE matches SET scores_json = \'{"甲":0.0,"乙":1.0}\' '
            "WHERE match_id = 'web-match-1'"
        ),
        (
            "UPDATE match_players SET display_name = '伪造' "
            "WHERE match_id = 'web-match-1' AND position = 0"
        ),
        (
            'UPDATE match_players SET descriptor_json = \'{"name":"伪造"}\' '
            "WHERE match_id = 'web-match-1' AND position = 0"
        ),
    ],
)
def test_denormalized_match_metadata_and_identity_tampering_is_rejected(
    tmp_path: Path,
    sql: str,
) -> None:
    path, _, _ = _database(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute(sql)

    code = _error_code(lambda: WebSQLiteReader(path).load_match("web-match-1"))
    assert code == "match_index_invalid"


def test_archive_size_and_event_count_are_bounded_before_model_validation(tmp_path: Path) -> None:
    oversized_path, _, _ = _database(tmp_path / "oversized")
    with sqlite3.connect(oversized_path) as connection:
        connection.execute(
            "UPDATE matches SET archive_json = CAST(zeroblob(?) AS TEXT) WHERE match_id = ?",
            (MAX_ARCHIVE_BYTES + 1, "web-match-1"),
        )
    assert (
        _error_code(lambda: WebSQLiteReader(oversized_path).load_match("web-match-1"))
        == "archive_too_large"
    )

    event_path, _, _ = _database(tmp_path / "events")
    payload = _archive_payload(event_path)
    payload["events"] = [payload["events"][1]] * (MAX_ARCHIVE_EVENTS + 1)
    _replace_archive(event_path, payload)
    assert (
        _error_code(lambda: WebSQLiteReader(event_path).load_match("web-match-1"))
        == "archive_event_limit_exceeded"
    )


def test_reader_created_before_a_commit_sees_the_new_match(tmp_path: Path) -> None:
    path, store, _ = _database(tmp_path)
    reader = WebSQLiteReader(path)
    assert reader.health()["match_count"] == 1

    second = _archive("web-match-2", seconds=5)
    store.save_match(second, rating_source="engine")

    assert reader.health()["match_count"] == 2
    assert [summary.match_id for summary in reader.list_matches()] == [
        "web-match-2",
        "web-match-1",
    ]
    assert reader.load_match("web-match-2").archive == second
