"""SQLite archive and persistent ELO tests."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from llmolympic.core.archive import MatchArchive, MoveRecord
from llmolympic.core.events import EventType, MatchEvent
from llmolympic.core.storage import (
    MatchIdCollisionError,
    SQLiteStore,
    StorageError,
    UnsupportedSchemaError,
    database_path,
)

STARTED = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


def _archive(
    *,
    match_id: str = "match-1",
    game: str = "math_quiz",
    scores: dict[str, float] | None = None,
) -> MatchArchive:
    scores = scores or {"甲": 0.8, "乙": 0.6}
    players = [{"name": name, "kind": "mock", "model": name} for name in scores]
    events = [
        MatchEvent(
            seq=0,
            type=EventType.MATCH_STARTED,
            timestamp=STARTED,
            data={"game": game, "seed": 42, "players": players},
        ),
        MatchEvent(
            seq=1,
            type=EventType.MOVE_REJECTED,
            timestamp=STARTED + timedelta(seconds=1),
            player=next(iter(scores)),
            data={"move": "?", "reason": "测试拒绝"},
        ),
        MatchEvent(
            seq=2,
            type=EventType.MATCH_FINISHED,
            timestamp=STARTED + timedelta(seconds=2),
            data={"scores": scores},
        ),
    ]
    return MatchArchive(
        match_id=match_id,
        game=game,
        seed=42,
        players=players,
        events=events,
        moves=[
            MoveRecord(
                player=next(iter(scores)),
                prompt="一道题",
                move="?",
                accepted=False,
                reason="测试拒绝",
            )
        ],
        scores=scores,
        started_at=STARTED,
        finished_at=STARTED + timedelta(seconds=2),
    )


def test_schema_and_full_archive_round_trip(tmp_path) -> None:
    path = tmp_path / "state" / "olympics.db"
    archive = _archive()

    store = SQLiteStore(path)
    result = store.save_match(archive)

    assert path.is_file()
    assert result.inserted is True
    assert result.rated is True
    assert len(result.rating_changes) == 4  # 两名选手 × 总榜/项目榜
    loaded = SQLiteStore(path).get_match(archive.match_id)
    assert loaded is not None
    assert loaded.model_dump(mode="json") == archive.model_dump(mode="json")
    assert loaded.moves[0].reason == "测试拒绝"

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM match_players").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 4


def test_quiz_scores_are_converted_to_head_to_head_outcome(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "ratings.db")
    result = store.save_match(_archive(scores={"甲": 0.8, "乙": 0.6}))

    game_changes = {change.player: change for change in result.rating_changes if change.game}
    assert game_changes["甲"].outcome == 1.0
    assert game_changes["乙"].outcome == 0.0
    assert game_changes["甲"].after == pytest.approx(1516.0)
    assert game_changes["乙"].after == pytest.approx(1484.0)

    board = store.leaderboard(game="math_quiz")
    assert [(entry.player, entry.rating) for entry in board] == [
        ("甲", pytest.approx(1516.0)),
        ("乙", pytest.approx(1484.0)),
    ]
    assert (board[0].wins, board[0].draws, board[0].losses) == (1, 0, 0)


def test_game_ratings_are_isolated_while_overall_rating_accumulates(tmp_path) -> None:
    path = tmp_path / "persistent.db"
    SQLiteStore(path).save_match(_archive(match_id="math", scores={"甲": 1.0, "乙": 0.0}))

    reopened = SQLiteStore(path)
    reopened.save_match(
        _archive(
            match_id="knowledge",
            game="knowledge_quiz",
            scores={"甲": 0.0, "乙": 1.0},
        )
    )

    math_board = {entry.player: entry for entry in reopened.leaderboard(game="math_quiz")}
    knowledge_board = {
        entry.player: entry for entry in reopened.leaderboard(game="knowledge_quiz")
    }
    overall_board = {entry.player: entry for entry in reopened.leaderboard()}
    assert math_board["甲"].rating == pytest.approx(1516.0)
    assert knowledge_board["乙"].rating == pytest.approx(1516.0)
    assert overall_board["甲"].games_played == 2
    assert overall_board["乙"].games_played == 2
    assert overall_board["甲"].wins == overall_board["甲"].losses == 1


def test_duplicate_save_is_idempotent_and_collision_is_rejected(tmp_path) -> None:
    path = tmp_path / "idempotent.db"
    store = SQLiteStore(path)
    archive = _archive()

    assert store.save_match(archive).inserted is True
    assert store.save_match(archive).inserted is False
    assert store.leaderboard()[0].games_played == 1

    collision = archive.model_copy(deep=True)
    collision.scores = {"甲": 0.0, "乙": 1.0}
    collision.events[-1].data["scores"] = collision.scores
    with pytest.raises(MatchIdCollisionError, match="另一份"):
        store.save_match(collision)

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM matches").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 4


def test_collision_check_preserves_json_boolean_and_number_types(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "typed-json.db")
    first = _archive()
    first.events[0].data["probe"] = 1
    second = first.model_copy(deep=True)
    second.events[0].data["probe"] = True

    store.save_match(first)
    with pytest.raises(MatchIdCollisionError):
        store.save_match(second)


def test_non_two_player_match_is_archived_but_not_rated(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "multiplayer.db")
    archive = _archive(scores={"甲": 1.0, "乙": 0.5, "丙": 0.0})

    result = store.save_match(archive)

    assert result.inserted is True
    assert result.rated is False
    assert result.rating_changes == ()
    assert store.get_match(archive.match_id) is not None
    assert store.leaderboard() == []


def test_invalid_archive_does_not_write_partial_data(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "invalid.db")
    invalid = _archive().model_copy(update={"scores": {"甲": 1.0}})

    with pytest.raises(StorageError, match="完全一致"):
        store.save_match(invalid)

    assert store.get_match(invalid.match_id) is None
    assert store.list_matches() == []


def test_archive_rejects_conflicting_finished_event_scores(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "conflicting-scores.db")
    archive = _archive()
    archive.events[-1].data["scores"] = {"甲": 0.0, "乙": 1.0}

    with pytest.raises(StorageError, match="match_finished"):
        store.save_match(archive)
    assert store.list_matches() == []


def test_failure_during_rating_update_rolls_back_archive(tmp_path) -> None:
    class FailingStore(SQLiteStore):
        def _record_ratings(self, *args, **kwargs):
            super()._record_ratings(*args, **kwargs)
            raise RuntimeError("injected failure")

    store = FailingStore(tmp_path / "rollback.db")
    archive = _archive()

    with pytest.raises(RuntimeError, match="injected"):
        store.save_match(archive)

    assert store.get_match(archive.match_id) is None
    assert store.leaderboard() == []


def test_match_history_filter_and_order(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "history.db")
    first = _archive(match_id="first")
    second = _archive(match_id="second", game="knowledge_quiz").model_copy(
        update={
            "started_at": STARTED + timedelta(minutes=1),
            "finished_at": STARTED + timedelta(minutes=2),
        }
    )
    store.save_match(first)
    store.save_match(second)

    assert [row.match_id for row in store.list_matches()] == ["second", "first"]
    assert [row.match_id for row in store.list_matches(game="math_quiz")] == ["first"]


def test_concurrent_writers_do_not_lose_rating_updates(tmp_path) -> None:
    path = tmp_path / "concurrent.db"
    archives = [_archive(match_id=f"concurrent-{index}") for index in range(8)]

    def save(archive: MatchArchive) -> None:
        SQLiteStore(path).save_match(archive)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(save, archives))

    store = SQLiteStore(path)
    assert len(store.list_matches()) == 8
    assert all(entry.games_played == 8 for entry in store.leaderboard())
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 32


def test_concurrent_duplicate_saves_update_elo_once(tmp_path) -> None:
    path = tmp_path / "concurrent-duplicate.db"
    archive = _archive(match_id="one-match")

    def save(_: int):
        return SQLiteStore(path).save_match(archive)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(save, range(8)))

    assert sum(result.inserted for result in results) == 1
    store = SQLiteStore(path)
    assert len(store.list_matches()) == 1
    assert all(entry.games_played == 1 for entry in store.leaderboard())
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 4


def test_database_path_environment_override(monkeypatch, tmp_path) -> None:
    configured = tmp_path / "configured.db"
    monkeypatch.setenv("LLMOLYMPIC_DB", str(configured))

    assert database_path() == configured.resolve()


def test_newer_database_schema_is_rejected(tmp_path) -> None:
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 99")

    with pytest.raises(UnsupportedSchemaError, match="高于"):
        SQLiteStore(path)


def test_archive_json_version_is_backward_compatible_but_rejects_future_versions() -> None:
    payload = json.loads(_archive().model_dump_json())
    payload.pop("schema_version")
    assert MatchArchive.model_validate(payload).schema_version == 1

    payload["schema_version"] = 99
    with pytest.raises(ValueError, match="schema_version"):
        MatchArchive.model_validate(payload)


def test_save_rejects_future_archive_version_even_after_unvalidated_copy(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "archive-version.db")
    future = _archive().model_copy(update={"schema_version": 99})

    with pytest.raises(StorageError, match="不支持对局档案版本"):
        store.save_match(future)
    assert store.list_matches() == []


def test_save_rejects_seed_outside_sqlite_integer_range(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "seed.db")
    archive = _archive().model_copy(update={"seed": 2**63})
    archive.events[0].data["seed"] = archive.seed

    with pytest.raises(StorageError, match="64 位"):
        store.save_match(archive)
    assert store.list_matches() == []
