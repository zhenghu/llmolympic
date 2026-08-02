"""SQLite archive and persistent ELO tests."""

from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from llmolympic.core.archive import MatchArchive, MoveRecord
from llmolympic.core.events import EventType, MatchEvent
from llmolympic.core.series import SeriesArchive, series_from_legs
from llmolympic.core.storage import (
    MatchIdCollisionError,
    SeriesIdCollisionError,
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
            data={"game": game, "seed": 42, "game_config": {}, "players": players},
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


def _technical_loss_archive() -> MatchArchive:
    archive = _archive(scores={"甲": 0.0, "乙": 1.0})
    archive.events[1].data.update(
        {
            "reason": "模型服务调用失败，判技术负",
            "reason_code": "provider_error",
            "forfeit": True,
            "technical_loss": True,
            "forfeit_scope": "match",
        }
    )
    archive.events[-1].data.update(
        {
            "termination": "technical_loss",
            "reason_code": "provider_error",
            "reason": "模型服务调用失败，判技术负",
            "forfeited_by": "甲",
            "cause_event_seq": 1,
        }
    )
    return archive


def _series_archive(
    *,
    series_id: str = "series-1",
    first_scores: dict[str, float] | None = None,
    second_scores: dict[str, float] | None = None,
) -> SeriesArchive:
    first = _archive(
        match_id=f"{series_id}-1",
        scores=first_scores or {"甲": 1.0, "乙": 0.0},
    )
    second = _archive(
        match_id=f"{series_id}-2",
        scores=second_scores or {"乙": 1.0, "甲": 0.0},
    ).model_copy(
        update={
            "started_at": STARTED + timedelta(seconds=3),
            "finished_at": STARTED + timedelta(seconds=5),
        }
    )
    return series_from_legs(first, second, series_id=series_id)


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
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM match_players").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 4


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are not portable")
def test_new_database_directory_and_file_use_private_permissions(tmp_path) -> None:
    path = tmp_path / "private-state" / "olympics.db"

    SQLiteStore(path)

    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are not portable")
def test_existing_database_permissions_are_tightened_on_open(tmp_path) -> None:
    path = tmp_path / "existing.db"
    SQLiteStore(path)
    path.chmod(0o644)

    SQLiteStore(path, create=False)

    assert path.stat().st_mode & 0o777 == 0o600


def test_database_path_must_be_a_regular_file(tmp_path) -> None:
    path = tmp_path / "not-a-database.db"
    path.mkdir()

    with pytest.raises(StorageError, match="不是普通文件"):
        SQLiteStore(path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are not portable")
def test_database_open_fails_closed_when_permissions_cannot_be_tightened(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "permission-denied.db"
    SQLiteStore(path)
    original_chmod = Path.chmod

    def deny_database_chmod(candidate: Path, mode: int) -> None:
        if candidate == path:
            raise PermissionError("injected chmod failure")
        original_chmod(candidate, mode)

    monkeypatch.setattr(Path, "chmod", deny_database_chmod)

    with pytest.raises(StorageError, match="权限收紧为 0600"):
        SQLiteStore(path, create=False)


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are not portable")
def test_sqlite_wal_sidecars_inherit_private_permissions(tmp_path) -> None:
    path = tmp_path / "wal.db"
    store = SQLiteStore(path)
    connection = store._connect()
    try:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE permission_probe (id INTEGER)")

        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{path}{suffix}")
            assert sidecar.is_file()
            assert sidecar.stat().st_mode & 0o777 == 0o600
    finally:
        connection.rollback()
        connection.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are not portable")
def test_existing_default_database_directory_is_tightened(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    default_directory = tmp_path / ".llmolympic"
    default_directory.mkdir(mode=0o755)
    default_directory.chmod(0o755)
    path = default_directory / "llmolympic.db"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 2")
        SQLiteStore._create_base_schema(connection)
        SQLiteStore._create_series_schema(connection)
    path.chmod(0o644)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr("llmolympic.core.storage.cfg_get", lambda *args, **kwargs: None)

    SQLiteStore(create=False)

    assert default_directory.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600


def test_series_is_atomic_and_split_result_has_no_elo_order_drift(tmp_path) -> None:
    path = tmp_path / "series.db"
    store = SQLiteStore(path)
    series = _series_archive()

    result = store.save_series(series)

    assert result.inserted is True
    assert result.rated is True
    assert len(result.rating_changes) == 4
    assert store.get_series(series.series_id) == series
    assert {row.match_id for row in store.list_matches()} == {
        series.legs[0].match_id,
        series.legs[1].match_id,
    }
    board = {entry.player: entry for entry in store.leaderboard()}
    assert board["甲"].rating == pytest.approx(1500.0)
    assert board["乙"].rating == pytest.approx(1500.0)
    assert (board["甲"].games_played, board["甲"].wins, board["甲"].losses) == (
        2,
        1,
        1,
    )
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM series_archives").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM series_matches").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 8
        assert connection.execute(
            "SELECT rating_policy FROM series_archives"
        ).fetchone()[0] == "elo_batch_v1"


def test_series_sweep_preserves_two_matches_of_elo_weight(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "sweep.db")
    series = _series_archive(second_scores={"乙": 0.0, "甲": 1.0})

    store.save_series(series)

    board = {entry.player: entry for entry in store.leaderboard(game="math_quiz")}
    assert board["甲"].rating == pytest.approx(1532.0)
    assert board["乙"].rating == pytest.approx(1468.0)
    assert (board["甲"].games_played, board["甲"].wins, board["甲"].losses) == (
        2,
        2,
        0,
    )


def test_series_final_history_rating_exactly_matches_leaderboard_float(tmp_path) -> None:
    path = tmp_path / "series-history-float.db"
    store = SQLiteStore(path)
    store.save_match(_archive(match_id="warmup", scores={"甲": 1.0, "乙": 0.0}))
    series = _series_archive(series_id="non-integer-split")

    store.save_series(series)

    with sqlite3.connect(path) as connection:
        rating = connection.execute(
            """
            SELECT rating FROM ratings
            WHERE rating_scope = 'overall' AND game = '' AND player = '甲'
            """
        ).fetchone()[0]
        history_after = connection.execute(
            """
            SELECT rating_after FROM rating_history
            WHERE match_id = ? AND rating_scope = 'overall' AND player = '甲'
            """,
            (series.legs[1].match_id,),
        ).fetchone()[0]
    assert history_after == rating


def test_series_save_is_idempotent_and_rejects_series_id_collision(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "series-idempotent.db")
    series = _series_archive()

    assert store.save_series(series).inserted is True
    assert store.save_series(series).inserted is False
    assert all(entry.games_played == 2 for entry in store.leaderboard())

    collision = _series_archive(
        series_id=series.series_id,
        second_scores={"乙": 0.0, "甲": 1.0},
    )
    with pytest.raises(SeriesIdCollisionError, match="另一份"):
        store.save_series(collision)


def test_series_rejects_a_match_already_saved_standalone(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "reused-leg.db")
    series = _series_archive()
    store.save_match(series.legs[0])

    with pytest.raises(MatchIdCollisionError, match="不能重复归入"):
        store.save_series(series)

    assert store.get_series(series.series_id) is None
    assert [row.match_id for row in store.list_matches()] == [series.legs[0].match_id]
    assert all(entry.games_played == 1 for entry in store.leaderboard())


def test_series_rating_failure_rolls_back_both_archives_and_all_ratings(tmp_path) -> None:
    class FailingStore(SQLiteStore):
        def _record_series_ratings(self, *args, **kwargs):
            super()._record_series_ratings(*args, **kwargs)
            raise RuntimeError("injected series failure")

    path = tmp_path / "series-rollback.db"
    store = FailingStore(path)
    series = _series_archive()

    with pytest.raises(RuntimeError, match="injected series failure"):
        store.save_series(series)

    assert store.get_series(series.series_id) is None
    assert store.list_matches() == []
    assert store.leaderboard() == []
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 0


def test_v1_database_is_migrated_without_changing_existing_data(tmp_path) -> None:
    path = tmp_path / "migrate-v1.db"
    archive = _archive(match_id="before-migration")
    SQLiteStore(path).save_match(archive)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE series_matches")
        connection.execute("DROP TABLE series_archives")
        connection.execute("PRAGMA user_version = 1")

    migrated = SQLiteStore(path, create=False)

    assert migrated.get_match(archive.match_id) == archive
    assert all(entry.games_played == 1 for entry in migrated.leaderboard())
    assert migrated.save_series(_series_archive()).inserted is True
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_failed_v1_migration_keeps_user_version_unchanged(tmp_path) -> None:
    path = tmp_path / "broken-v1.db"
    SQLiteStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE series_matches")
        connection.execute("DROP TABLE series_archives")
        connection.execute("CREATE TABLE series_archives (series_id TEXT PRIMARY KEY)")
        connection.execute("PRAGMA user_version = 1")

    with pytest.raises(sqlite3.OperationalError):
        SQLiteStore(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(series_archives)")
        }
        assert columns == {"series_id"}


def test_idempotent_series_save_rejects_damaged_leg_mapping(tmp_path) -> None:
    path = tmp_path / "damaged-series.db"
    series = _series_archive()
    store = SQLiteStore(path)
    store.save_series(series)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM series_matches WHERE series_id = ? AND leg_number = 2",
            (series.series_id,),
        )

    with pytest.raises(StorageError, match="映射已损坏"):
        store.save_series(series)

    assert all(entry.games_played == 2 for entry in store.leaderboard())


def test_structured_technical_loss_round_trips_and_updates_elo(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "technical-loss.db")
    archive = _technical_loss_archive()

    result = store.save_match(archive)

    assert result.rated
    loaded = store.get_match(archive.match_id)
    assert loaded is not None
    assert loaded.events[-1].data["termination"] == "technical_loss"
    board = store.leaderboard(game="math_quiz")
    assert [(entry.player, entry.rating) for entry in board] == [
        ("乙", pytest.approx(1516.0)),
        ("甲", pytest.approx(1484.0)),
    ]


def test_save_rejects_technical_loss_scores_that_reward_forfeiting_player(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "invalid-technical-loss.db")
    archive = _technical_loss_archive()
    archive.scores = {"甲": 1.0, "乙": 0.0}
    archive.events[-1].data["scores"] = dict(archive.scores)

    with pytest.raises(StorageError, match="责任方 0 分"):
        store.save_match(archive)
    assert store.list_matches() == []


def test_save_rejects_technical_loss_with_mismatched_cause_event(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "invalid-cause.db")
    archive = _technical_loss_archive()
    archive.events[-1].data["cause_event_seq"] = 0

    with pytest.raises(StorageError, match="原因事件"):
        store.save_match(archive)
    assert store.list_matches() == []


@pytest.mark.parametrize("termination", [None, "completed"])
def test_save_rejects_technical_loss_with_missing_or_disguised_termination(
    tmp_path, termination: str | None
) -> None:
    store = SQLiteStore(tmp_path / "invalid-termination.db")
    archive = _technical_loss_archive()
    if termination is None:
        archive.events[-1].data.pop("termination")
    else:
        archive.events[-1].data["termination"] = termination

    with pytest.raises(StorageError, match="技术负"):
        store.save_match(archive)
    assert store.list_matches() == []


def test_save_rejects_technical_loss_without_match_forfeit_marker(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "invalid-forfeit-marker.db")
    archive = _technical_loss_archive()
    archive.events[1].data.pop("forfeit")

    with pytest.raises(StorageError, match="原因事件"):
        store.save_match(archive)
    assert store.list_matches() == []


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


@pytest.mark.parametrize("limit", [0, 1001, True, 1.5])
def test_match_history_limit_is_bounded(limit: object, tmp_path) -> None:
    store = SQLiteStore(tmp_path / "history-limit.db")

    with pytest.raises(ValueError, match="1 到 1000"):
        store.list_matches(limit=limit)  # type: ignore[arg-type]


@pytest.mark.parametrize("limit", [0, 1001, True, 1.5])
def test_leaderboard_limit_is_bounded(limit: object, tmp_path) -> None:
    store = SQLiteStore(tmp_path / "leaderboard-limit.db")

    with pytest.raises(ValueError, match="1 到 1000"):
        store.leaderboard(limit=limit)  # type: ignore[arg-type]


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


def test_concurrent_duplicate_series_saves_update_batch_elo_once(tmp_path) -> None:
    path = tmp_path / "concurrent-series.db"
    series = _series_archive()

    def save(_: int):
        return SQLiteStore(path).save_series(series)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(save, range(8)))

    assert sum(result.inserted for result in results) == 1
    store = SQLiteStore(path)
    assert len(store.list_matches()) == 2
    assert all(entry.games_played == 2 for entry in store.leaderboard())
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM series_archives").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 8


def test_concurrent_v1_migration_is_serialized_and_idempotent(tmp_path) -> None:
    path = tmp_path / "concurrent-migration.db"
    SQLiteStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE series_matches")
        connection.execute("DROP TABLE series_archives")
        connection.execute("PRAGMA user_version = 1")

    with ThreadPoolExecutor(max_workers=8) as executor:
        stores = list(executor.map(lambda _: SQLiteStore(path), range(8)))

    assert all(store.path == path.resolve() for store in stores)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"series_archives", "series_matches"} <= tables


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
