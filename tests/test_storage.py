"""SQLite archive and persistent ELO tests."""

from __future__ import annotations

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from llmolympic.core.archive import MatchArchive, MoveRecord, legacy_entrant_id
from llmolympic.core.elo import K_FACTOR, expected_score, update_ratings
from llmolympic.core.events import EventType, MatchEvent
from llmolympic.core.series import SeriesArchive, series_from_legs
from llmolympic.core.storage import (
    MatchIdCollisionError,
    MatchSummary,
    RatingChange,
    RatingEntry,
    SeriesIdCollisionError,
    SQLiteStore,
    StorageError,
    UnsupportedSchemaError,
    database_path,
)

STARTED = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
_MATCH_METADATA_UPDATE_SQL = {
    "schema_version": "UPDATE matches SET schema_version = ? WHERE match_id = ?",
    "game": "UPDATE matches SET game = ? WHERE match_id = ?",
    "seed": "UPDATE matches SET seed = ? WHERE match_id = ?",
    "players_json": "UPDATE matches SET players_json = ? WHERE match_id = ?",
    "scores_json": "UPDATE matches SET scores_json = ? WHERE match_id = ?",
    "started_at": "UPDATE matches SET started_at = ? WHERE match_id = ?",
    "finished_at": "UPDATE matches SET finished_at = ? WHERE match_id = ?",
    "archive_source": "UPDATE matches SET archive_source = ? WHERE match_id = ?",
    "rating_source": "UPDATE matches SET rating_source = ? WHERE match_id = ?",
    "rated": "UPDATE matches SET rated = ? WHERE match_id = ?",
    "rating_policy": "UPDATE matches SET rating_policy = ? WHERE match_id = ?",
}
_SERIES_METADATA_UPDATE_SQL = {
    "schema_version": "UPDATE series_archives SET schema_version = ? WHERE series_id = ?",
    "game": "UPDATE series_archives SET game = ? WHERE series_id = ?",
    "seed": "UPDATE series_archives SET seed = ? WHERE series_id = ?",
    "players_json": "UPDATE series_archives SET players_json = ? WHERE series_id = ?",
    "points_json": "UPDATE series_archives SET points_json = ? WHERE series_id = ?",
    "started_at": "UPDATE series_archives SET started_at = ? WHERE series_id = ?",
    "finished_at": "UPDATE series_archives SET finished_at = ? WHERE series_id = ?",
    "archive_source": "UPDATE series_archives SET archive_source = ? WHERE series_id = ?",
    "rating_source": "UPDATE series_archives SET rating_source = ? WHERE series_id = ?",
    "rated": "UPDATE series_archives SET rated = ? WHERE series_id = ?",
    "rating_policy": "UPDATE series_archives SET rating_policy = ? WHERE series_id = ?",
}


def test_legacy_entrant_id_hashes_exact_unicode_bytes_without_normalization() -> None:
    assert legacy_entrant_id("é") != legacy_entrant_id("e\u0301")


def _archive(
    *,
    match_id: str = "match-1",
    game: str = "math_quiz",
    scores: dict[str, float] | None = None,
    entrant_ids: dict[str, str] | None = None,
    models: dict[str, str] | None = None,
) -> MatchArchive:
    scores = scores or {"甲": 0.8, "乙": 0.6}
    entrant_ids = entrant_ids or {name: f"test:{name}" for name in scores}
    models = models or {name: name for name in scores}
    players = [
        {
            "name": name,
            "display_name": name,
            "entrant_id": entrant_ids[name],
            "kind": "mock",
            "model": models[name],
        }
        for name in scores
    ]
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
        schema_version=2,
        source="local_engine",
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


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _legacy_archive_payload(archive: MatchArchive, *, omit_schema_version: bool = False) -> dict:
    payload = archive.model_dump(mode="json")
    payload.pop("source", None)
    if omit_schema_version:
        payload.pop("schema_version", None)
    else:
        payload["schema_version"] = 1
    for descriptor in payload["players"]:
        descriptor.pop("entrant_id", None)
        descriptor.pop("display_name", None)
    event_players = payload["events"][0]["data"]["players"]
    for descriptor in event_players:
        descriptor.pop("entrant_id", None)
        descriptor.pop("display_name", None)
    return payload


def _legacy_series_payload(series: SeriesArchive) -> dict:
    payload = series.model_dump(mode="json")
    payload["schema_version"] = 1
    payload.pop("source", None)
    for descriptor in payload["players"]:
        descriptor.pop("entrant_id", None)
        descriptor.pop("display_name", None)
    payload["legs"] = [_legacy_archive_payload(leg) for leg in series.legs]
    return payload


def _with_descriptor(archive: MatchArchive, position: int, **updates: object) -> MatchArchive:
    changed = archive.model_copy(deep=True)
    changed.players[position].update(updates)
    changed.events[0].data["players"][position].update(updates)
    return changed


def _create_legacy_database(
    path: Path,
    *,
    version: int,
    archive_payload: dict | None = None,
    series_payload: dict | None = None,
    rated: bool = True,
) -> tuple[dict[str, str], str | None]:
    """Create a real name-keyed v1/v2 database, never a downgraded v3 schema."""

    if version not in (1, 2):
        raise ValueError("legacy test database version must be 1 or 2")
    raw_archives: dict[str, str] = {}
    raw_series: str | None = None
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE matches (
                match_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                game TEXT NOT NULL,
                seed INTEGER NOT NULL,
                players_json TEXT NOT NULL,
                scores_json TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                archive_json TEXT NOT NULL
            );
            CREATE INDEX matches_finished_at_idx ON matches(finished_at DESC);
            CREATE INDEX matches_game_finished_at_idx
                ON matches(game, finished_at DESC);
            CREATE TABLE match_players (
                match_id TEXT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                player TEXT NOT NULL,
                descriptor_json TEXT NOT NULL,
                score REAL NOT NULL,
                PRIMARY KEY (match_id, position)
            );
            CREATE INDEX match_players_player_idx ON match_players(player, match_id);
            CREATE TABLE ratings (
                rating_scope TEXT NOT NULL CHECK (rating_scope IN ('overall', 'game')),
                game TEXT NOT NULL,
                player TEXT NOT NULL,
                rating REAL NOT NULL,
                games_played INTEGER NOT NULL,
                wins INTEGER NOT NULL,
                draws INTEGER NOT NULL,
                losses INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (rating_scope, game, player)
            );
            CREATE INDEX ratings_leaderboard_idx
                ON ratings(rating_scope, game, rating DESC, games_played DESC, player);
            CREATE TABLE rating_history (
                match_id TEXT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
                rating_scope TEXT NOT NULL CHECK (rating_scope IN ('overall', 'game')),
                game TEXT NOT NULL,
                player TEXT NOT NULL,
                opponent TEXT NOT NULL,
                outcome REAL NOT NULL CHECK (outcome IN (0.0, 0.5, 1.0)),
                rating_before REAL NOT NULL,
                rating_after REAL NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (match_id, rating_scope, game, player)
            );
            """
        )
        if version == 2:
            connection.executescript(
                """
                CREATE TABLE series_archives (
                    series_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    game TEXT NOT NULL,
                    seed INTEGER NOT NULL,
                    players_json TEXT NOT NULL,
                    points_json TEXT NOT NULL,
                    rating_policy TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    series_json TEXT NOT NULL
                );
                CREATE INDEX series_archives_finished_at_idx
                    ON series_archives(finished_at DESC);
                CREATE INDEX series_archives_game_finished_at_idx
                    ON series_archives(game, finished_at DESC);
                CREATE TABLE series_matches (
                    series_id TEXT NOT NULL
                        REFERENCES series_archives(series_id) ON DELETE CASCADE,
                    leg_number INTEGER NOT NULL CHECK (leg_number IN (1, 2)),
                    match_id TEXT NOT NULL UNIQUE
                        REFERENCES matches(match_id) ON DELETE RESTRICT,
                    PRIMARY KEY (series_id, leg_number)
                );
                """
            )

        matches: list[dict] = []
        if archive_payload is not None:
            matches.append(archive_payload)
        if series_payload is not None:
            if version != 2:
                raise ValueError("series payload requires legacy database v2")
            matches.extend(series_payload["legs"])

        match_results: dict[str, tuple[dict, str, str, float]] = {}
        for payload in matches:
            raw = json.dumps(payload, ensure_ascii=False, indent=1)
            raw_archives[payload["match_id"]] = raw
            connection.execute(
                """
                INSERT INTO matches (
                    match_id, schema_version, game, seed, players_json, scores_json,
                    started_at, finished_at, archive_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["match_id"],
                    1,
                    payload["game"],
                    payload["seed"],
                    _json_text(payload["players"]),
                    _json_text(payload["scores"]),
                    payload["started_at"],
                    payload["finished_at"],
                    raw,
                ),
            )
            names = [descriptor["name"] for descriptor in payload["players"]]
            for position, (name, descriptor) in enumerate(zip(names, payload["players"])):
                connection.execute(
                    """
                    INSERT INTO match_players (
                        match_id, position, player, descriptor_json, score
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        payload["match_id"],
                        position,
                        name,
                        _json_text(descriptor),
                        payload["scores"][name],
                    ),
                )
            first, second = names
            first_score = payload["scores"][first]
            second_score = payload["scores"][second]
            outcome = (
                1.0 if first_score > second_score else 0.0 if first_score < second_score else 0.5
            )
            match_results[payload["match_id"]] = (payload, first, second, outcome)

        if rated and matches:
            outcomes: dict[str, list[float]] = {}
            current_ratings: dict[tuple[str, str, str], float] = {}

            def record_history(
                payload: dict,
                scope: str,
                game_key: str,
                player: str,
                opponent: str,
                outcome: float,
                before: float,
                after: float,
            ) -> None:
                outcomes.setdefault(player, []).append(outcome)
                current_ratings[(scope, game_key, player)] = after
                connection.execute(
                    """
                    INSERT INTO rating_history (
                        match_id, rating_scope, game, player, opponent, outcome,
                        rating_before, rating_after, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["match_id"],
                        scope,
                        game_key,
                        player,
                        opponent,
                        outcome,
                        before,
                        after,
                        payload["finished_at"],
                    ),
                )

            standalone_matches = [] if archive_payload is None else [archive_payload]
            for payload in standalone_matches:
                _, first, second, outcome = match_results[payload["match_id"]]
                for scope, game_key in (("overall", ""), ("game", payload["game"])):
                    before_first = current_ratings.get((scope, game_key, first), 1500.0)
                    before_second = current_ratings.get((scope, game_key, second), 1500.0)
                    after_first, after_second = update_ratings(before_first, before_second, outcome)
                    record_history(
                        payload,
                        scope,
                        game_key,
                        first,
                        second,
                        outcome,
                        before_first,
                        after_first,
                    )
                    record_history(
                        payload,
                        scope,
                        game_key,
                        second,
                        first,
                        1.0 - outcome,
                        before_second,
                        after_second,
                    )

            if series_payload is not None:
                player_a, player_b = [item["name"] for item in series_payload["players"]]
                for scope, game_key in (("overall", ""), ("game", series_payload["game"])):
                    running_a = current_ratings.get((scope, game_key, player_a), 1500.0)
                    running_b = current_ratings.get((scope, game_key, player_b), 1500.0)
                    frozen_expectation = expected_score(running_a, running_b)
                    for payload in series_payload["legs"]:
                        score_a = payload["scores"][player_a]
                        score_b = payload["scores"][player_b]
                        outcome_a = 1.0 if score_a > score_b else 0.0 if score_a < score_b else 0.5
                        delta_a = K_FACTOR * (outcome_a - frozen_expectation)
                        next_a = running_a + delta_a
                        next_b = running_b - delta_a
                        record_history(
                            payload,
                            scope,
                            game_key,
                            player_a,
                            player_b,
                            outcome_a,
                            running_a,
                            next_a,
                        )
                        record_history(
                            payload,
                            scope,
                            game_key,
                            player_b,
                            player_a,
                            1.0 - outcome_a,
                            running_b,
                            next_b,
                        )
                        running_a, running_b = next_a, next_b

            updated_at = max(payload["finished_at"] for payload in matches)
            game = matches[0]["game"]
            for player, player_outcomes in outcomes.items():
                for scope, game_key in (("overall", ""), ("game", game)):
                    connection.execute(
                        """
                        INSERT INTO ratings (
                            rating_scope, game, player, rating, games_played,
                            wins, draws, losses, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            scope,
                            game_key,
                            player,
                            current_ratings[(scope, game_key, player)],
                            len(player_outcomes) // 2,
                            player_outcomes.count(1.0) // 2,
                            player_outcomes.count(0.5) // 2,
                            player_outcomes.count(0.0) // 2,
                            updated_at,
                        ),
                    )

        if series_payload is not None:
            raw_series = json.dumps(series_payload, ensure_ascii=False, indent=2)
            connection.execute(
                """
                INSERT INTO series_archives (
                    series_id, schema_version, game, seed, players_json, points_json,
                    rating_policy, started_at, finished_at, series_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    series_payload["series_id"],
                    1,
                    series_payload["game"],
                    series_payload["seed"],
                    _json_text(series_payload["players"]),
                    _json_text(series_payload["points"]),
                    "elo_batch_v1",
                    series_payload["started_at"],
                    series_payload["finished_at"],
                    raw_series,
                ),
            )
            for leg_number, leg in enumerate(series_payload["legs"], start=1):
                connection.execute(
                    """
                    INSERT INTO series_matches (series_id, leg_number, match_id)
                    VALUES (?, ?, ?)
                    """,
                    (series_payload["series_id"], leg_number, leg["match_id"]),
                )
        connection.execute(f"PRAGMA user_version = {version}")
    return raw_archives, raw_series


def test_schema_and_full_archive_round_trip(tmp_path) -> None:
    path = tmp_path / "state" / "olympics.db"
    archive = _archive()

    store = SQLiteStore(path)
    result = store.save_match(archive, rating_source="engine")

    assert path.is_file()
    assert result.inserted is True
    assert result.rated is True
    assert len(result.rating_changes) == 4  # 两名选手 × 总榜/项目榜
    loaded = SQLiteStore(path).get_match(archive.match_id)
    assert loaded is not None
    assert loaded.model_dump(mode="json") == archive.model_dump(mode="json")
    assert loaded.moves[0].reason == "测试拒绝"

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert connection.execute("SELECT count(*) FROM match_players").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 4


def test_storage_dtos_keep_legacy_positional_construction() -> None:
    change = RatingChange("甲", "乙", "math_quiz", 1.0, 1500.0, 1516.0)
    entry = RatingEntry("甲", 1516.0, 1, 1, 0, 0, STARTED)
    summary = MatchSummary(
        "match-old",
        "math_quiz",
        42,
        ("甲", "乙"),
        {"甲": 1.0, "乙": 0.0},
        STARTED,
        STARTED + timedelta(seconds=1),
    )

    assert change.player == change.display_name == "甲"
    assert change.opponent == change.opponent_display_name == "乙"
    assert change.entrant_id == change.opponent_entrant_id == ""
    assert entry.player == entry.display_name == "甲"
    assert entry.entrant_id == ""
    assert summary.entrant_ids == ()
    assert summary.rating_source == "imported"
    assert summary.rated is False


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
    _create_legacy_database(path, version=2)
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

    result = store.save_series(series, rating_source="engine")

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
        assert (
            connection.execute("SELECT rating_policy FROM series_archives").fetchone()[0]
            == "elo_batch_v1"
        )


def test_series_sweep_preserves_two_matches_of_elo_weight(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "sweep.db")
    series = _series_archive(second_scores={"乙": 0.0, "甲": 1.0})

    store.save_series(series, rating_source="engine")

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
    store.save_match(
        _archive(match_id="warmup", scores={"甲": 1.0, "乙": 0.0}),
        rating_source="engine",
    )
    series = _series_archive(series_id="non-integer-split")

    store.save_series(series, rating_source="engine")

    with sqlite3.connect(path) as connection:
        rating = connection.execute(
            """
            SELECT rating FROM ratings
            WHERE rating_scope = 'overall' AND game = '' AND entrant_id = 'test:甲'
            """
        ).fetchone()[0]
        history_after = connection.execute(
            """
            SELECT rating_after FROM rating_history
            WHERE match_id = ? AND rating_scope = 'overall' AND entrant_id = 'test:甲'
            """,
            (series.legs[1].match_id,),
        ).fetchone()[0]
    assert history_after == rating


def test_series_save_is_idempotent_and_rejects_series_id_collision(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "series-idempotent.db")
    series = _series_archive()

    assert store.save_series(series, rating_source="engine").inserted is True
    assert store.save_series(series, rating_source="engine").inserted is False
    assert all(entry.games_played == 2 for entry in store.leaderboard())

    collision = _series_archive(
        series_id=series.series_id,
        second_scores={"乙": 0.0, "甲": 1.0},
    )
    with pytest.raises(SeriesIdCollisionError, match="另一份"):
        store.save_series(collision, rating_source="engine")


def test_series_rejects_a_match_already_saved_standalone(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "reused-leg.db")
    series = _series_archive()
    store.save_match(series.legs[0], rating_source="engine")

    with pytest.raises(MatchIdCollisionError, match="不能重复归入"):
        store.save_series(series, rating_source="engine")

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
        store.save_series(series, rating_source="engine")

    assert store.get_series(series.series_id) is None
    assert store.list_matches() == []
    assert store.leaderboard() == []
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 0


def test_v1_database_is_migrated_without_changing_existing_data(tmp_path) -> None:
    path = tmp_path / "migrate-v1.db"
    payload = _legacy_archive_payload(_archive(match_id="before-migration"))
    raw_archives, _ = _create_legacy_database(
        path,
        version=1,
        archive_payload=payload,
    )

    migrated = SQLiteStore(path, create=False)

    loaded = migrated.get_match(payload["match_id"])
    assert loaded is not None
    assert loaded.schema_version == 1
    assert loaded.source == "legacy"
    assert [descriptor["entrant_id"] for descriptor in loaded.players] == [
        legacy_entrant_id("甲"),
        legacy_entrant_id("乙"),
    ]
    assert all(entry.games_played == 1 for entry in migrated.leaderboard())
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert (
            connection.execute(
                "SELECT archive_json FROM matches WHERE match_id = ?", (payload["match_id"],)
            ).fetchone()[0]
            == raw_archives[payload["match_id"]]
        )
        assert connection.execute(
            "SELECT rating_source, rated, rating_policy FROM matches"
        ).fetchone() == ("engine", 1, "elo_v1")


def test_v2_series_migration_preserves_raw_json_and_backfills_legacy_identity(
    tmp_path,
) -> None:
    path = tmp_path / "migrate-v2.db"
    payload = _legacy_series_payload(_series_archive(series_id="legacy-series"))
    raw_archives, raw_series = _create_legacy_database(
        path,
        version=2,
        series_payload=payload,
    )

    store = SQLiteStore(path, create=False)
    loaded = store.get_series(payload["series_id"])

    assert loaded is not None
    assert loaded.schema_version == 1
    assert loaded.source == "legacy"
    assert [descriptor["entrant_id"] for descriptor in loaded.players] == [
        legacy_entrant_id("甲"),
        legacy_entrant_id("乙"),
    ]
    assert not store.save_series(loaded, rating_source="engine").inserted
    assert not store.save_match(loaded.legs[0], rating_source="engine").inserted
    assert not store.save_match(loaded.legs[1]).inserted
    assert {entry.entrant_id for entry in store.leaderboard()} == {
        legacy_entrant_id("甲"),
        legacy_entrant_id("乙"),
    }
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert (
            connection.execute(
                "SELECT series_json FROM series_archives WHERE series_id = ?",
                (payload["series_id"],),
            ).fetchone()[0]
            == raw_series
        )
        assert (
            dict(connection.execute("SELECT match_id, archive_json FROM matches").fetchall())
            == raw_archives
        )
        assert connection.execute(
            """
            SELECT archive_source, rating_source, rated, rating_policy
            FROM series_archives
            """
        ).fetchone() == ("legacy", "engine", 1, "elo_batch_v1")
        assert set(
            connection.execute(
                """
                SELECT archive_source, rating_source, rated, rating_policy FROM matches
                """
            ).fetchall()
        ) == {("legacy", "engine", 1, "elo_batch_v1")}
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 8


def test_missing_archive_schema_version_uses_legacy_identity_compatibility(tmp_path) -> None:
    path = tmp_path / "missing-schema.db"
    payload = _legacy_archive_payload(_archive(match_id="missing-schema"), omit_schema_version=True)
    _create_legacy_database(path, version=1, archive_payload=payload, rated=False)

    archive = SQLiteStore(path).get_match(payload["match_id"])

    assert archive is not None
    assert archive.schema_version == 1
    assert archive.source == "legacy"
    assert archive.players[0]["entrant_id"] == legacy_entrant_id("甲")


def test_failed_v1_migration_keeps_user_version_and_schema_unchanged(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "broken-v1.db"
    payload = _legacy_archive_payload(_archive(match_id="rollback-migration"))
    raw_archives, _ = _create_legacy_database(
        path,
        version=1,
        archive_payload=payload,
    )
    original_migrate = SQLiteStore._migrate_to_v3

    def fail_after_migration(connection: sqlite3.Connection, *, include_series: bool) -> None:
        original_migrate(connection, include_series=include_series)
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(SQLiteStore, "_migrate_to_v3", staticmethod(fail_after_migration))

    with pytest.raises(RuntimeError, match="injected migration failure"):
        SQLiteStore(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        columns = {row[1] for row in connection.execute("PRAGMA table_info(ratings)")}
        assert "player" in columns
        assert "entrant_id" not in columns
        assert (
            connection.execute(
                "SELECT archive_json FROM matches WHERE match_id = ?", (payload["match_id"],)
            ).fetchone()[0]
            == raw_archives[payload["match_id"]]
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = 'entrants'"
            ).fetchone()[0]
            == 0
        )


def test_idempotent_series_save_rejects_damaged_leg_mapping(tmp_path) -> None:
    path = tmp_path / "damaged-series.db"
    series = _series_archive()
    store = SQLiteStore(path)
    store.save_series(series, rating_source="engine")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM series_matches WHERE series_id = ? AND leg_number = 2",
            (series.series_id,),
        )

    with pytest.raises(StorageError, match="映射已损坏"):
        store.save_series(series, rating_source="engine")

    assert all(entry.games_played == 2 for entry in store.leaderboard())


def test_series_leg_resave_reuses_whole_series_integrity_check(tmp_path) -> None:
    path = tmp_path / "series-leg-resave.db"
    series = _series_archive()
    store = SQLiteStore(path)
    store.save_series(series, rating_source="engine")

    assert not store.save_match(series.legs[0], rating_source="engine").inserted
    assert not store.save_match(series.legs[1]).inserted

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE rating_history
            SET opponent_display_name = '损坏'
            WHERE match_id = ? AND rating_scope = 'overall' AND entrant_id = 'test:甲'
            """,
            (series.legs[1].match_id,),
        )

    with pytest.raises(StorageError, match="ELO 历史已损坏"):
        store.save_match(series.legs[0])


@pytest.mark.parametrize(
    ("column", "damaged_value"),
    [
        ("schema_version", 1),
        ("game", "tampered-game"),
        ("seed", 43),
        ("players_json", '[{"name":"tampered"}]'),
        ("points_json", '{"甲":2.0,"乙":0.0}'),
        ("started_at", "2026-07-31T12:00:01+00:00"),
        ("finished_at", "2026-07-31T12:00:06+00:00"),
        ("archive_source", "external"),
        ("rating_source", "imported"),
        ("rated", 0),
        ("rating_policy", "unrated"),
    ],
)
def test_series_leg_resave_rejects_tampered_series_metadata(
    tmp_path, column: str, damaged_value: object
) -> None:
    path = tmp_path / f"tampered-series-{column}.db"
    series = _series_archive()
    store = SQLiteStore(path)
    store.save_series(series, rating_source="engine")
    with sqlite3.connect(path) as connection:
        connection.execute(
            _SERIES_METADATA_UPDATE_SQL[column],
            (damaged_value, series.series_id),
        )

    with pytest.raises(StorageError):
        store.save_match(series.legs[0])


@pytest.mark.parametrize(
    ("column", "damaged_value"),
    [
        ("schema_version", 1),
        ("game", "tampered-game"),
        ("seed", 43),
        ("players_json", '[{"name":"tampered"}]'),
        ("scores_json", '{"甲":1.0,"乙":0.0}'),
        ("started_at", "2026-07-31T12:00:04+00:00"),
        ("finished_at", "2026-07-31T12:00:06+00:00"),
        ("archive_source", "external"),
        ("rating_source", "imported"),
        ("rated", 0),
        ("rating_policy", "unrated"),
    ],
)
def test_series_leg_resave_rejects_tampered_sibling_match_metadata(
    tmp_path, column: str, damaged_value: object
) -> None:
    path = tmp_path / f"tampered-series-leg-{column}.db"
    series = _series_archive()
    store = SQLiteStore(path)
    store.save_series(series, rating_source="engine")
    sibling = series.legs[1]
    with sqlite3.connect(path) as connection:
        connection.execute(
            _MATCH_METADATA_UPDATE_SQL[column],
            (damaged_value, sibling.match_id),
        )

    with pytest.raises(StorageError):
        store.save_match(series.legs[0])


def test_series_metadata_json_and_timestamps_use_semantic_comparison(tmp_path) -> None:
    path = tmp_path / "semantic-series-metadata.db"
    series = _series_archive()
    payload = series.model_dump(mode="json")
    store = SQLiteStore(path)
    store.save_series(series, rating_source="engine")
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE series_archives
            SET players_json = ?, points_json = ?, started_at = ?, finished_at = ?
            WHERE series_id = ?
            """,
            (
                json.dumps(payload["players"], ensure_ascii=False, indent=2),
                json.dumps(
                    {"乙": payload["points"]["乙"], "甲": payload["points"]["甲"]},
                    ensure_ascii=False,
                    indent=2,
                ),
                "2026-07-31T14:00:00+02:00",
                "2026-07-31T14:00:05+02:00",
                series.series_id,
            ),
        )

    assert not store.save_match(series.legs[0]).inserted


def test_structured_technical_loss_round_trips_and_updates_elo(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "technical-loss.db")
    archive = _technical_loss_archive()

    result = store.save_match(archive, rating_source="engine")

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
        store.save_match(archive, rating_source="engine")
    assert store.list_matches() == []


def test_save_rejects_technical_loss_with_mismatched_cause_event(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "invalid-cause.db")
    archive = _technical_loss_archive()
    archive.events[-1].data["cause_event_seq"] = 0

    with pytest.raises(StorageError, match="原因事件"):
        store.save_match(archive, rating_source="engine")
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
        store.save_match(archive, rating_source="engine")
    assert store.list_matches() == []


def test_save_rejects_technical_loss_without_match_forfeit_marker(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "invalid-forfeit-marker.db")
    archive = _technical_loss_archive()
    archive.events[1].data.pop("forfeit")

    with pytest.raises(StorageError, match="原因事件"):
        store.save_match(archive, rating_source="engine")
    assert store.list_matches() == []


def test_quiz_scores_are_converted_to_head_to_head_outcome(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "ratings.db")
    result = store.save_match(_archive(scores={"甲": 0.8, "乙": 0.6}), rating_source="engine")

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
    SQLiteStore(path).save_match(
        _archive(match_id="math", scores={"甲": 1.0, "乙": 0.0}),
        rating_source="engine",
    )

    reopened = SQLiteStore(path)
    reopened.save_match(
        _archive(
            match_id="knowledge",
            game="knowledge_quiz",
            scores={"甲": 0.0, "乙": 1.0},
        ),
        rating_source="engine",
    )

    math_board = {entry.player: entry for entry in reopened.leaderboard(game="math_quiz")}
    knowledge_board = {entry.player: entry for entry in reopened.leaderboard(game="knowledge_quiz")}
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

    assert store.save_match(archive, rating_source="engine").inserted is True
    assert store.save_match(archive, rating_source="engine").inserted is False
    assert store.leaderboard()[0].games_played == 1

    collision = archive.model_copy(deep=True)
    collision.scores = {"甲": 0.0, "乙": 1.0}
    collision.events[-1].data["scores"] = collision.scores
    with pytest.raises(MatchIdCollisionError, match="另一份"):
        store.save_match(collision, rating_source="engine")

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM matches").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 4


@pytest.mark.parametrize(
    ("damage_sql", "message"),
    [
        (
            "UPDATE match_players SET player = '错名' WHERE position = 0",
            "选手索引已损坏",
        ),
        (
            """
            UPDATE rating_history SET opponent_display_name = '错名'
            WHERE rating_scope = 'overall' AND entrant_id = 'test:甲'
            """,
            "ELO 历史已损坏",
        ),
    ],
)
def test_idempotent_match_resave_deeply_checks_indexes_and_history(
    tmp_path, damage_sql: str, message: str
) -> None:
    path = tmp_path / "damaged-match-index.db"
    archive = _archive()
    store = SQLiteStore(path)
    store.save_match(archive, rating_source="engine")
    with sqlite3.connect(path) as connection:
        connection.execute(damage_sql)

    with pytest.raises(StorageError, match=message):
        store.save_match(archive)


@pytest.mark.parametrize(
    ("column", "damaged_value"),
    [
        ("schema_version", 1),
        ("game", "tampered-game"),
        ("seed", 43),
        ("players_json", '[{"name":"tampered"}]'),
        ("scores_json", '{"甲":0.0,"乙":1.0}'),
        ("started_at", "2026-07-31T12:00:01+00:00"),
        ("finished_at", "2026-07-31T12:00:03+00:00"),
        ("archive_source", "external"),
        ("rating_source", "imported"),
        ("rated", 0),
        ("rating_policy", "unrated"),
    ],
)
def test_idempotent_match_resave_rejects_tampered_denormalized_metadata(
    tmp_path, column: str, damaged_value: object
) -> None:
    path = tmp_path / f"tampered-match-{column}.db"
    archive = _archive()
    store = SQLiteStore(path)
    store.save_match(archive, rating_source="engine")
    with sqlite3.connect(path) as connection:
        connection.execute(
            _MATCH_METADATA_UPDATE_SQL[column],
            (damaged_value, archive.match_id),
        )

    with pytest.raises(StorageError):
        store.save_match(archive)


def test_match_metadata_json_and_timestamps_use_semantic_comparison(tmp_path) -> None:
    path = tmp_path / "semantic-match-metadata.db"
    archive = _archive()
    payload = archive.model_dump(mode="json")
    store = SQLiteStore(path)
    store.save_match(archive, rating_source="engine")
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE matches
            SET players_json = ?, scores_json = ?, started_at = ?, finished_at = ?
            WHERE match_id = ?
            """,
            (
                json.dumps(payload["players"], ensure_ascii=False, indent=2),
                json.dumps({"乙": 0.6, "甲": 0.8}, ensure_ascii=False, indent=2),
                "2026-07-31T14:00:00+02:00",
                "2026-07-31T14:00:02+02:00",
                archive.match_id,
            ),
        )

    assert not store.save_match(archive).inserted


def test_match_history_verification_is_independent_of_name_order(tmp_path) -> None:
    archive = _archive()
    archive.scores = {"乙": 0.6, "甲": 0.8}
    archive.events[-1].data["scores"] = dict(archive.scores)
    store = SQLiteStore(tmp_path / "name-order.db")

    store.save_match(archive, rating_source="engine")

    assert not store.save_match(archive).inserted
    board = {entry.entrant_id: entry for entry in store.leaderboard()}
    assert board["test:甲"].rating == pytest.approx(1516.0)


def test_imported_match_is_unrated_and_cannot_be_upgraded_by_resave(tmp_path) -> None:
    path = tmp_path / "imported.db"
    store = SQLiteStore(path)
    archive = _archive()

    first = store.save_match(archive)
    repeated = store.save_match(archive)

    assert first.inserted and not first.rated
    assert not repeated.inserted and not repeated.rated
    assert store.leaderboard() == []
    with pytest.raises(MatchIdCollisionError, match="不能通过幂等重存升级"):
        store.save_match(archive, rating_source="engine")
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT archive_source, rating_source, rated, rating_policy FROM matches"
        ).fetchone() == ("local_engine", "imported", 0, "unrated")
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 0


def test_engine_match_default_resave_is_readonly_idempotent(tmp_path) -> None:
    path = tmp_path / "engine-default-resave.db"
    store = SQLiteStore(path)
    archive = _archive()

    assert store.save_match(archive, rating_source="engine").rated
    repeated = store.save_match(archive)

    assert not repeated.inserted and repeated.rated
    assert all(entry.games_played == 1 for entry in store.leaderboard())
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 4


@pytest.mark.parametrize(
    "update_sql",
    [
        "UPDATE matches SET rating_policy = 'unrated'",
        "UPDATE matches SET rating_source = 'imported'",
    ],
)
def test_idempotent_match_resave_rejects_corrupt_rating_metadata(tmp_path, update_sql: str) -> None:
    path = tmp_path / "corrupt-match-rating.db"
    store = SQLiteStore(path)
    archive = _archive()
    store.save_match(archive, rating_source="engine")
    with sqlite3.connect(path) as connection:
        connection.execute(update_sql)

    with pytest.raises(StorageError, match="计分来源或策略已损坏"):
        store.save_match(archive)


def test_engine_series_default_resave_is_readonly_idempotent(tmp_path) -> None:
    path = tmp_path / "engine-series-default-resave.db"
    store = SQLiteStore(path)
    series = _series_archive()

    assert store.save_series(series, rating_source="engine").rated
    repeated = store.save_series(series)

    assert not repeated.inserted and repeated.rated
    assert all(entry.games_played == 2 for entry in store.leaderboard())
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 8


def test_imported_series_is_unrated_and_cannot_be_upgraded(tmp_path) -> None:
    path = tmp_path / "imported-series.db"
    store = SQLiteStore(path)
    series = _series_archive()

    first = store.save_series(series)
    repeated = store.save_series(series)

    assert first.inserted and not first.rated
    assert not repeated.inserted and not repeated.rated
    leg_repeat = store.save_match(series.legs[0])
    assert not leg_repeat.inserted and not leg_repeat.rated
    assert store.leaderboard() == []
    with pytest.raises(SeriesIdCollisionError, match="不能通过幂等重存升级"):
        store.save_series(series, rating_source="engine")
    with pytest.raises(MatchIdCollisionError, match="不能通过幂等重存升级"):
        store.save_match(series.legs[0], rating_source="engine")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 0
        assert set(
            connection.execute("SELECT rating_source, rated, rating_policy FROM matches").fetchall()
        ) == {("imported", 0, "unrated")}


def test_external_archive_cannot_be_rated_even_if_called_engine(tmp_path) -> None:
    path = tmp_path / "external-source.db"
    store = SQLiteStore(path)
    archive = _archive().model_copy(update={"source": "external"})

    result = store.save_match(archive, rating_source="engine")

    assert result.inserted and not result.rated
    assert store.leaderboard() == []
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT archive_source, rating_source, rated, rating_policy FROM matches"
        ).fetchone() == ("external", "engine", 0, "unrated")


def test_external_series_cannot_be_rated_even_if_called_engine(tmp_path) -> None:
    path = tmp_path / "external-series.db"
    store = SQLiteStore(path)
    series = _series_archive()
    external = series.model_copy(
        update={
            "source": "external",
            "legs": tuple(leg.model_copy(update={"source": "external"}) for leg in series.legs),
        }
    )

    result = store.save_series(external, rating_source="engine")

    assert result.inserted and not result.rated
    assert store.leaderboard() == []
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT archive_source, rating_source, rated, rating_policy FROM series_archives"
        ).fetchone() == ("external", "engine", 0, "unrated")
        assert set(
            connection.execute(
                """
                SELECT archive_source, rating_source, rated, rating_policy FROM matches
                """
            ).fetchall()
        ) == {("external", "engine", 0, "unrated")}


def test_model_copy_cannot_bypass_schema_source_pairing(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "invalid-source.db")
    v2_claiming_legacy = _archive(match_id="v2-legacy").model_copy(update={"source": "legacy"})
    legacy = MatchArchive.model_validate(_legacy_archive_payload(_archive(match_id="v1-external")))
    v1_claiming_external = legacy.model_copy(update={"source": "external"})

    with pytest.raises(StorageError, match="schema v2 档案来源"):
        store.save_match(v2_claiming_legacy, rating_source="engine")
    with pytest.raises(StorageError, match="schema v1 档案来源"):
        store.save_match(v1_claiming_external, rating_source="engine")
    assert store.list_matches() == []


def test_engine_identity_rename_keeps_one_rating_and_history_snapshots(tmp_path) -> None:
    path = tmp_path / "rename.db"
    store = SQLiteStore(path)
    first = _archive(
        match_id="before-rename",
        scores={"Alpha": 1.0, "Beta": 0.0},
        entrant_ids={"Alpha": "profile:a", "Beta": "profile:b"},
        models={"Alpha": "model-a", "Beta": "model-b"},
    )
    renamed = _archive(
        match_id="after-rename",
        scores={"Renamed": 1.0, "Beta": 0.0},
        entrant_ids={"Renamed": "profile:a", "Beta": "profile:b"},
        models={"Renamed": "model-a", "Beta": "model-b"},
    ).model_copy(
        update={
            "started_at": STARTED + timedelta(minutes=1),
            "finished_at": STARTED + timedelta(minutes=1, seconds=2),
        }
    )

    store.save_match(first, rating_source="engine")
    store.save_match(renamed, rating_source="engine")

    board = {entry.entrant_id: entry for entry in store.leaderboard()}
    assert board["profile:a"].display_name == "Renamed"
    assert board["profile:a"].games_played == 2
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            """
            SELECT display_name FROM rating_history
            WHERE entrant_id = 'profile:a' AND rating_scope = 'overall'
            ORDER BY match_id
            """
        ).fetchall() == [("Renamed",), ("Alpha",)]


def test_same_display_name_with_distinct_ids_remains_distinct(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "same-display.db")
    first = _archive(
        match_id="same-display-1",
        scores={"Same": 1.0, "Opponent": 0.0},
        entrant_ids={"Same": "profile:same-1", "Opponent": "profile:opponent"},
        models={"Same": "same-model", "Opponent": "opponent-model"},
    )
    second = _archive(
        match_id="same-display-2",
        scores={"Same": 0.0, "Opponent": 1.0},
        entrant_ids={"Same": "profile:same-2", "Opponent": "profile:opponent"},
        models={"Same": "same-model", "Opponent": "opponent-model"},
    )

    store.save_match(first, rating_source="engine")
    store.save_match(second, rating_source="engine")

    same_entries = [entry for entry in store.leaderboard() if entry.display_name == "Same"]
    assert {entry.entrant_id for entry in same_entries} == {
        "profile:same-1",
        "profile:same-2",
    }
    assert all(entry.games_played == 1 for entry in same_entries)


def test_identity_metadata_change_for_same_entrant_id_is_rejected(tmp_path) -> None:
    path = tmp_path / "identity-conflict.db"
    store = SQLiteStore(path)
    first = _with_descriptor(
        _archive(match_id="identity-1"),
        0,
        sampling_params={"temperature": 0.2, "seed": 1},
    )
    changed = _with_descriptor(
        _archive(match_id="identity-2"),
        0,
        sampling_params={"temperature": 0.3, "seed": 1},
    )

    store.save_match(first, rating_source="engine")
    with pytest.raises(StorageError, match="另一份身份元数据"):
        store.save_match(changed, rating_source="engine")

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM matches").fetchone()[0] == 1
        identity = json.loads(
            connection.execute(
                "SELECT identity_json FROM entrants WHERE entrant_id = 'test:甲'"
            ).fetchone()[0]
        )
    assert identity["sampling_params"] == {"seed": 1, "temperature": 0.2}


def test_first_trusted_observation_can_take_over_imported_identity(tmp_path) -> None:
    path = tmp_path / "identity-takeover.db"
    store = SQLiteStore(path)
    imported = _archive(
        match_id="imported-claim",
        scores={"Imported": 1.0, "Other": 0.0},
        entrant_ids={"Imported": "profile:a", "Other": "profile:other"},
        models={"Imported": "spoofed-model", "Other": "other-model"},
    )
    trusted = _archive(
        match_id="trusted-claim",
        scores={"Trusted": 1.0, "Other": 0.0},
        entrant_ids={"Trusted": "profile:a", "Other": "profile:other"},
        models={"Trusted": "real-model", "Other": "other-model"},
    ).model_copy(
        update={
            "started_at": STARTED + timedelta(minutes=1),
            "finished_at": STARTED + timedelta(minutes=1, seconds=2),
        }
    )

    store.save_match(imported)
    store.save_match(trusted, rating_source="engine")

    with sqlite3.connect(path) as connection:
        display_name, identity_json = connection.execute(
            """
            SELECT display_name, identity_json FROM entrants
            WHERE entrant_id = 'profile:a'
            """
        ).fetchone()
    assert display_name == "Trusted"
    assert json.loads(identity_json)["model"] == "real-model"


def test_first_trusted_identity_takeover_ignores_imported_future_timestamp(tmp_path) -> None:
    path = tmp_path / "identity-takeover-old-name.db"
    store = SQLiteStore(path)
    imported = _archive(
        match_id="newer-imported-name",
        scores={"Newer Imported": 1.0, "Other": 0.0},
        entrant_ids={"Newer Imported": "profile:a", "Other": "profile:other"},
        models={"Newer Imported": "spoofed-model", "Other": "other-model"},
    ).model_copy(
        update={
            "started_at": STARTED + timedelta(minutes=2),
            "finished_at": STARTED + timedelta(minutes=2, seconds=2),
        }
    )
    trusted = _archive(
        match_id="older-trusted-name",
        scores={"Older Trusted": 1.0, "Other": 0.0},
        entrant_ids={"Older Trusted": "profile:a", "Other": "profile:other"},
        models={"Older Trusted": "real-model", "Other": "other-model"},
    ).model_copy(
        update={
            "started_at": STARTED + timedelta(minutes=1),
            "finished_at": STARTED + timedelta(minutes=1, seconds=2),
        }
    )

    store.save_match(imported)
    store.save_match(trusted, rating_source="engine")

    with sqlite3.connect(path) as connection:
        display_name, identity_json = connection.execute(
            """
            SELECT display_name, identity_json FROM entrants
            WHERE entrant_id = 'profile:a'
            """
        ).fetchone()
    assert display_name == "Older Trusted"
    assert json.loads(identity_json)["model"] == "real-model"


def test_trusted_display_name_only_moves_forward_in_observed_time(tmp_path) -> None:
    path = tmp_path / "identity-name-time.db"
    store = SQLiteStore(path)
    newer = _archive(
        match_id="newer-name",
        scores={"Newer": 1.0, "Other": 0.0},
        entrant_ids={"Newer": "profile:a", "Other": "profile:other"},
        models={"Newer": "model-a", "Other": "other-model"},
    ).model_copy(
        update={
            "started_at": STARTED + timedelta(minutes=2),
            "finished_at": STARTED + timedelta(minutes=2, seconds=2),
        }
    )
    older = _archive(
        match_id="older-name",
        scores={"Older": 1.0, "Other": 0.0},
        entrant_ids={"Older": "profile:a", "Other": "profile:other"},
        models={"Older": "model-a", "Other": "other-model"},
    ).model_copy(
        update={
            "started_at": STARTED + timedelta(minutes=1),
            "finished_at": STARTED + timedelta(minutes=1, seconds=2),
        }
    )

    store.save_match(newer, rating_source="engine")
    store.save_match(older, rating_source="engine")

    board = {entry.entrant_id: entry for entry in store.leaderboard()}
    assert board["profile:a"].display_name == "Newer"


def test_duplicate_entrant_id_and_sensitive_descriptor_are_rejected(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "invalid-identity.db")
    duplicate = _with_descriptor(_archive(match_id="duplicate-id"), 1, entrant_id="test:甲")
    exposed = _with_descriptor(
        _archive(match_id="exposed-secret"),
        0,
        api_key="must-never-be-archived",
    )

    with pytest.raises(StorageError, match="entrant_id"):
        store.save_match(duplicate, rating_source="engine")
    with pytest.raises(StorageError, match="凭据或连接端点"):
        store.save_match(exposed, rating_source="engine")
    assert store.list_matches() == []


def test_recursive_sensitive_keys_accept_only_exact_redaction(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "redacted-descriptor.db")
    redacted = _with_descriptor(
        _archive(match_id="redacted"),
        0,
        sampling_params={
            "temperature": 0.2,
            "max_tokens": 128,
            "max_completion_tokens": 256,
            "Authorization": "[REDACTED]",
        },
        metadata={"connection": {"Auth_Token": "[REDACTED]"}},
    )
    exposed = _with_descriptor(
        _archive(match_id="nested-secret").model_copy(update={"source": "external"}),
        0,
        metadata={"connection": {"BaSe-UrL": "https://private.invalid/v1"}},
    )
    exposed_auth = _with_descriptor(
        _archive(match_id="nested-auth").model_copy(update={"source": "external"}),
        0,
        metadata={"items": [{"AuTh": "private-auth-value"}]},
    )

    assert store.save_match(redacted, rating_source="engine").rated
    with pytest.raises(StorageError, match="凭据或连接端点") as error:
        store.save_match(exposed)
    assert "https://private.invalid/v1" not in str(error.value)
    with pytest.raises(StorageError, match="凭据或连接端点") as auth_error:
        store.save_match(exposed_auth)
    assert "private-auth-value" not in str(auth_error.value)


@pytest.mark.parametrize(
    "descriptor_update",
    [
        {"headers": {"X-API-Key": "header-secret"}},
        {"openai_api_key": "provider-secret"},
        {"metadata": {"connection": {"auth_header": "auth-secret"}}},
        {"metadata": [{"private_endpoint": "https://private.invalid/v1"}]},
        {"headers": {"X-API-Key": "[redacted]"}},
    ],
    ids=[
        "nested-x-api-key",
        "provider-api-key",
        "nested-auth-header",
        "provider-endpoint",
        "redaction-is-case-sensitive",
    ],
)
def test_sensitive_descriptor_key_variants_are_rejected_without_value_leaks(
    tmp_path, descriptor_update: dict[str, object]
) -> None:
    archive = _with_descriptor(
        _archive(match_id="sensitive-key-variant").model_copy(update={"source": "external"}),
        0,
        **descriptor_update,
    )

    with pytest.raises(StorageError, match="凭据或连接端点") as error:
        SQLiteStore(tmp_path / "sensitive-key-variant.db").save_match(archive)

    message = str(error.value)
    assert "header-secret" not in message
    assert "provider-secret" not in message
    assert "auth-secret" not in message
    assert "https://private.invalid/v1" not in message


def test_collision_check_preserves_json_boolean_and_number_types(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "typed-json.db")
    first = _archive()
    first.events[0].data["probe"] = 1
    second = first.model_copy(deep=True)
    second.events[0].data["probe"] = True

    store.save_match(first, rating_source="engine")
    with pytest.raises(MatchIdCollisionError):
        store.save_match(second, rating_source="engine")


def test_non_two_player_match_is_archived_but_not_rated(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "multiplayer.db")
    archive = _archive(scores={"甲": 1.0, "乙": 0.5, "丙": 0.0})

    result = store.save_match(archive, rating_source="engine")

    assert result.inserted is True
    assert result.rated is False
    assert result.rating_changes == ()
    assert store.get_match(archive.match_id) is not None
    assert store.leaderboard() == []


def test_invalid_archive_does_not_write_partial_data(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "invalid.db")
    invalid = _archive().model_copy(update={"scores": {"甲": 1.0}})

    with pytest.raises(StorageError, match="完全一致"):
        store.save_match(invalid, rating_source="engine")

    assert store.get_match(invalid.match_id) is None
    assert store.list_matches() == []


def test_archive_rejects_conflicting_finished_event_scores(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "conflicting-scores.db")
    archive = _archive()
    archive.events[-1].data["scores"] = {"甲": 0.0, "乙": 1.0}

    with pytest.raises(StorageError, match="match_finished"):
        store.save_match(archive, rating_source="engine")
    assert store.list_matches() == []


def test_failure_during_rating_update_rolls_back_archive(tmp_path) -> None:
    class FailingStore(SQLiteStore):
        def _record_ratings(self, *args, **kwargs):
            super()._record_ratings(*args, **kwargs)
            raise RuntimeError("injected failure")

    store = FailingStore(tmp_path / "rollback.db")
    archive = _archive()

    with pytest.raises(RuntimeError, match="injected"):
        store.save_match(archive, rating_source="engine")

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
    store.save_match(first, rating_source="engine")
    store.save_match(second, rating_source="engine")

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
        SQLiteStore(path).save_match(archive, rating_source="engine")

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
        return SQLiteStore(path).save_match(archive, rating_source="engine")

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
        return SQLiteStore(path).save_series(series, rating_source="engine")

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
    _create_legacy_database(path, version=1)

    with ThreadPoolExecutor(max_workers=8) as executor:
        stores = list(executor.map(lambda _: SQLiteStore(path), range(8)))

    assert all(store.path == path.resolve() for store in stores)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
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
    payload.pop("source")
    assert MatchArchive.model_validate(payload).schema_version == 1

    payload["schema_version"] = 99
    with pytest.raises(ValueError, match="schema_version"):
        MatchArchive.model_validate(payload)


def test_save_rejects_future_archive_version_even_after_unvalidated_copy(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "archive-version.db")
    future = _archive().model_copy(update={"schema_version": 99})

    with pytest.raises(StorageError, match="不支持对局档案版本"):
        store.save_match(future, rating_source="engine")
    assert store.list_matches() == []


def test_save_rejects_seed_outside_sqlite_integer_range(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "seed.db")
    archive = _archive().model_copy(update={"seed": 2**63})
    archive.events[0].data["seed"] = archive.seed

    with pytest.raises(StorageError, match="64 位"):
        store.save_match(archive, rating_source="engine")
    assert store.list_matches() == []
