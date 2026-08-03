"""SQLite v4 round-robin persistence and frozen-ELO tests."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from itertools import combinations

import pytest

from llmolympic.core.archive import MatchArchive
from llmolympic.core.events import EventType, MatchEvent
from llmolympic.core.series import series_from_legs
from llmolympic.core.storage import (
    MatchIdCollisionError,
    SQLiteStore,
    StorageError,
    TournamentIdCollisionError,
)
from llmolympic.core.tournament import (
    TournamentArchive,
    round_robin_pair_seed,
    tournament_from_series,
)

STARTED = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def _descriptor(name: str) -> dict:
    return {
        "name": name,
        "display_name": name,
        "entrant_id": f"test:{name}",
        "kind": "mock",
        "model": name,
    }


def _match(
    *,
    match_id: str,
    seed: int,
    players: tuple[dict, dict],
    winner: str,
    started_at: datetime,
    source: str,
) -> MatchArchive:
    scores = {
        descriptor["name"]: 1.0 if descriptor["name"] == winner else 0.0 for descriptor in players
    }
    return MatchArchive(
        schema_version=2,
        source=source,
        match_id=match_id,
        game="math_quiz",
        seed=seed,
        players=list(players),
        events=[
            MatchEvent(
                seq=0,
                type=EventType.MATCH_STARTED,
                timestamp=started_at,
                data={
                    "game": "math_quiz",
                    "seed": seed,
                    "game_config": {},
                    "players": list(players),
                },
            ),
            MatchEvent(
                seq=1,
                type=EventType.MATCH_FINISHED,
                timestamp=started_at + timedelta(seconds=1),
                data={"scores": scores},
            ),
        ],
        moves=[],
        scores=scores,
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=1),
    )


def _tournament(
    *,
    tournament_id: str = "tournament-1",
    names: tuple[str, ...] = ("A", "B", "C"),
    source: str = "local_engine",
    reverse_winners: bool = False,
) -> TournamentArchive:
    players = tuple(_descriptor(name) for name in names)
    series_archives = []
    for pairing_number, (first_index, second_index) in enumerate(
        combinations(range(len(players)), 2), start=1
    ):
        first = players[first_index]
        second = players[second_index]
        ordered_names = sorted((first["name"], second["name"]))
        winner = ordered_names[-1] if reverse_winners else ordered_names[0]
        seed = round_robin_pair_seed(
            42,
            first["entrant_id"],
            second["entrant_id"],
        )
        pairing_started = STARTED + timedelta(seconds=(pairing_number - 1) * 4)
        first_leg = _match(
            match_id=f"{tournament_id}-pair-{pairing_number}-leg-1",
            seed=seed,
            players=(first, second),
            winner=winner,
            started_at=pairing_started,
            source=source,
        )
        second_leg = _match(
            match_id=f"{tournament_id}-pair-{pairing_number}-leg-2",
            seed=seed,
            players=(second, first),
            winner=winner,
            started_at=pairing_started + timedelta(seconds=2),
            source=source,
        )
        series_archives.append(
            series_from_legs(
                first_leg,
                second_leg,
                series_id=f"{tournament_id}-series-{pairing_number}",
            )
        )
    return tournament_from_series(
        players,
        series_archives,
        seed=42,
        tournament_id=tournament_id,
    )


def _seed_unequal_ratings(store: SQLiteStore) -> None:
    for index, opponent in enumerate(("B", "C"), start=1):
        store.save_match(
            _match(
                match_id=f"warmup-{index}",
                seed=index,
                players=(_descriptor("A"), _descriptor(opponent)),
                winner="A",
                started_at=STARTED - timedelta(minutes=3 - index),
                source="local_engine",
            ),
            rating_source="engine",
        )


def test_tournament_save_round_trip_and_frozen_rating_ledger(tmp_path) -> None:
    path = tmp_path / "tournament.db"
    tournament = _tournament()
    store = SQLiteStore(path)

    result = store.save_tournament(tournament, rating_source="engine")

    assert result.inserted and result.rated
    assert result.pairing_count == 3
    assert result.match_count == 6
    assert len(result.rating_changes) == 6
    loaded = store.get_tournament(tournament.tournament_id)
    assert loaded is not None
    assert loaded.model_dump(mode="json") == tournament.model_dump(mode="json")
    summaries = store.list_matches(limit=10)
    assert len(summaries) == 6
    assert {summary.tournament_id for summary in summaries} == {tournament.tournament_id}
    assert {summary.pairing_number for summary in summaries} == {1, 2, 3}
    assert {summary.pairing_count for summary in summaries} == {3}
    assert all(entry.games_played == 4 for entry in store.leaderboard())

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("SELECT count(*) FROM tournament_archives").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM tournament_pairings").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM series_archives").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM matches").fetchone()[0] == 6
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 24
        assert (
            connection.execute("SELECT count(*) FROM tournament_rating_snapshots").fetchone()[0]
            == 6
        )
        assert (
            connection.execute("SELECT count(*) FROM tournament_rating_contributions").fetchone()[0]
            == 24
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_tournament_and_owned_children_are_idempotent_and_deeply_verified(tmp_path) -> None:
    path = tmp_path / "idempotent-tournament.db"
    tournament = _tournament()
    store = SQLiteStore(path)
    store.save_tournament(tournament, rating_source="engine")

    assert not store.save_tournament(tournament, rating_source="engine").inserted
    assert not store.save_series(
        tournament.pairings[0].series,
        rating_source="engine",
    ).inserted
    assert not store.save_match(
        tournament.pairings[0].series.legs[0],
        rating_source="engine",
    ).inserted

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE tournament_rating_contributions
            SET rating_delta = rating_delta + 1
            WHERE tournament_id = ? AND sequence = 6
            """,
            (tournament.tournament_id,),
        )

    with pytest.raises(StorageError, match="ELO 历史已损坏"):
        store.save_match(tournament.pairings[0].series.legs[0])


def test_tournament_resave_verifies_latest_materialized_rating(tmp_path) -> None:
    path = tmp_path / "tampered-tournament-rating.db"
    tournament = _tournament()
    store = SQLiteStore(path)
    store.save_tournament(tournament, rating_source="engine")

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE ratings
            SET rating = rating + 5
            WHERE rating_scope = 'overall' AND game = '' AND entrant_id = 'test:A'
            """
        )

    with pytest.raises(StorageError, match="ELO 排行榜已损坏"):
        store.save_tournament(tournament, rating_source="engine")


def test_tournament_resave_rejects_malformed_materialized_timestamp(tmp_path) -> None:
    path = tmp_path / "malformed-tournament-rating-time.db"
    tournament = _tournament()
    store = SQLiteStore(path)
    store.save_tournament(tournament, rating_source="engine")

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE ratings
            SET updated_at = 'not-a-timestamp'
            WHERE rating_scope = 'overall' AND game = '' AND entrant_id = 'test:A'
            """
        )

    with pytest.raises(StorageError, match="ELO 排行榜已损坏"):
        store.save_tournament(tournament, rating_source="engine")


def test_historical_tournament_resave_rejects_tampered_materialized_counts(
    tmp_path,
) -> None:
    path = tmp_path / "tampered-tournament-rating-count.db"
    tournament = _tournament()
    store = SQLiteStore(path)
    store.save_tournament(tournament, rating_source="engine")
    store.save_match(
        _match(
            match_id="later-before-count-tamper",
            seed=100,
            players=(_descriptor("A"), _descriptor("B")),
            winner="B",
            started_at=tournament.finished_at + timedelta(minutes=1),
            source="local_engine",
        ),
        rating_source="engine",
    )

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE ratings
            SET games_played = games_played + 1
            WHERE rating_scope = 'overall' AND game = '' AND entrant_id = 'test:A'
            """
        )

    with pytest.raises(StorageError, match="ELO 排行榜已损坏"):
        store.save_tournament(tournament, rating_source="engine")


def test_historical_tournament_resave_allows_later_materialized_rating(tmp_path) -> None:
    path = tmp_path / "historical-tournament-rating.db"
    tournament = _tournament()
    store = SQLiteStore(path)
    store.save_tournament(tournament, rating_source="engine")
    later_match = _match(
        match_id="later-standalone",
        seed=99,
        players=(_descriptor("A"), _descriptor("B")),
        winner="B",
        started_at=tournament.finished_at - timedelta(seconds=1),
        source="local_engine",
    )

    store.save_match(later_match, rating_source="engine")

    result = store.save_tournament(tournament, rating_source="engine")
    assert not result.inserted
    assert result.rated


def test_historical_tournament_resave_allows_same_finished_at_later_tournament(
    tmp_path,
) -> None:
    path = tmp_path / "same-finished-at-tournaments.db"
    first = _tournament(tournament_id="same-time-first")
    later = _tournament(
        tournament_id="same-time-later",
        reverse_winners=True,
    )
    entrant_last_match = max(
        leg.finished_at
        for pairing in later.pairings
        for leg in pairing.series.legs
        if any(player["entrant_id"] == "test:A" for player in leg.players)
    )
    assert later.finished_at == first.finished_at
    assert entrant_last_match < later.finished_at
    store = SQLiteStore(path)
    store.save_tournament(first, rating_source="engine")
    first_rating = {entry.entrant_id: entry.rating for entry in store.leaderboard()}["test:A"]

    store.save_tournament(later, rating_source="engine")
    later_rating = {entry.entrant_id: entry.rating for entry in store.leaderboard()}["test:A"]
    assert later_rating != first_rating

    result = store.save_tournament(first, rating_source="engine")
    assert not result.inserted
    assert result.rated


def test_tournament_id_collision_and_preexisting_child_are_rejected(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "tournament-collision.db")
    tournament = _tournament()
    store.save_tournament(tournament, rating_source="engine")
    changed = _tournament(
        tournament_id=tournament.tournament_id,
        reverse_winners=True,
    )

    with pytest.raises(TournamentIdCollisionError):
        store.save_tournament(changed, rating_source="engine")

    other = _tournament(tournament_id="other-tournament")
    fresh_store = SQLiteStore(tmp_path / "child-collision.db")
    fresh_store.save_match(other.pairings[0].series.legs[0], rating_source="engine")
    with pytest.raises(MatchIdCollisionError, match="不能重复归入循环赛"):
        fresh_store.save_tournament(other, rating_source="engine")
    assert fresh_store.get_tournament(other.tournament_id) is None


def test_imported_tournament_is_unrated_and_cannot_be_upgraded(tmp_path) -> None:
    path = tmp_path / "imported-tournament.db"
    tournament = _tournament()
    store = SQLiteStore(path)

    result = store.save_tournament(tournament)

    assert result.inserted and not result.rated
    assert store.leaderboard() == []
    assert not store.save_series(tournament.pairings[0].series).rated
    with pytest.raises(TournamentIdCollisionError, match="不能通过幂等重存升级"):
        store.save_tournament(tournament, rating_source="engine")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 0
        assert (
            connection.execute("SELECT count(*) FROM tournament_rating_contributions").fetchone()[0]
            == 0
        )


def test_tournament_failure_rolls_back_every_child_and_rating(tmp_path) -> None:
    class FailingStore(SQLiteStore):
        def _record_tournament_ratings(self, *args, **kwargs):
            super()._record_tournament_ratings(*args, **kwargs)
            raise RuntimeError("injected tournament failure")

    path = tmp_path / "tournament-rollback.db"
    store = FailingStore(path)

    with pytest.raises(RuntimeError, match="injected tournament failure"):
        store.save_tournament(_tournament(), rating_source="engine")

    assert store.get_tournament("tournament-1") is None
    assert store.list_matches() == []
    assert store.leaderboard() == []
    with sqlite3.connect(path) as connection:
        for count_query in (
            "SELECT count(*) FROM tournament_archives",
            "SELECT count(*) FROM tournament_entrants",
            "SELECT count(*) FROM tournament_pairings",
            "SELECT count(*) FROM series_archives",
            "SELECT count(*) FROM matches",
            "SELECT count(*) FROM rating_history",
            "SELECT count(*) FROM tournament_rating_snapshots",
            "SELECT count(*) FROM tournament_rating_contributions",
        ):
            assert connection.execute(count_query).fetchone()[0] == 0


def test_external_tournament_cannot_be_rated(tmp_path) -> None:
    tournament = _tournament(source="external")
    store = SQLiteStore(tmp_path / "external-tournament.db")

    result = store.save_tournament(tournament, rating_source="engine")

    assert result.inserted and not result.rated
    assert store.leaderboard() == []


def test_frozen_tournament_elo_is_independent_of_pairing_execution_order(tmp_path) -> None:
    first_store = SQLiteStore(tmp_path / "first-order.db")
    second_store = SQLiteStore(tmp_path / "second-order.db")
    _seed_unequal_ratings(first_store)
    _seed_unequal_ratings(second_store)
    assert len({entry.rating for entry in first_store.leaderboard()}) > 1
    first_store.save_tournament(
        _tournament(tournament_id="first-order"),
        rating_source="engine",
    )
    second_store.save_tournament(
        _tournament(
            tournament_id="second-order",
            names=("C", "B", "A"),
        ),
        rating_source="engine",
    )

    first_ratings = {entry.entrant_id: entry.rating for entry in first_store.leaderboard()}
    second_ratings = {entry.entrant_id: entry.rating for entry in second_store.leaderboard()}
    assert first_ratings == pytest.approx(second_ratings)


def test_concurrent_duplicate_tournament_saves_rate_exactly_once(tmp_path) -> None:
    path = tmp_path / "concurrent-tournament.db"
    tournament = _tournament()

    def save(_: int):
        return SQLiteStore(path).save_tournament(
            tournament,
            rating_source="engine",
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(save, range(4)))

    assert sum(result.inserted for result in results) == 1
    store = SQLiteStore(path)
    assert all(entry.games_played == 4 for entry in store.leaderboard())
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM rating_history").fetchone()[0] == 24


def test_v3_database_migrates_to_v4_without_rewriting_existing_data(tmp_path) -> None:
    path = tmp_path / "migrate-v3.db"
    store = SQLiteStore(path)
    archive = _match(
        match_id="before-v4",
        seed=42,
        players=(_descriptor("A"), _descriptor("B")),
        winner="A",
        started_at=STARTED,
        source="local_engine",
    )
    store.save_match(archive, rating_source="engine")
    with sqlite3.connect(path) as connection:
        raw_archive = connection.execute(
            "SELECT archive_json FROM matches WHERE match_id = ?",
            (archive.match_id,),
        ).fetchone()[0]
        ratings_before = connection.execute(
            "SELECT * FROM ratings ORDER BY rating_scope, game, entrant_id"
        ).fetchall()
        history_before = connection.execute(
            """
            SELECT * FROM rating_history
            ORDER BY match_id, rating_scope, game, entrant_id
            """
        ).fetchall()
        connection.executescript(
            """
            DROP TABLE tournament_rating_contributions;
            DROP TABLE tournament_rating_snapshots;
            DROP TABLE tournament_pairings;
            DROP TABLE tournament_entrants;
            DROP TABLE tournament_archives;
            PRAGMA user_version = 3;
            """
        )

    migrated = SQLiteStore(path, create=False)

    assert migrated.get_match(archive.match_id) is not None
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert (
            connection.execute(
                "SELECT archive_json FROM matches WHERE match_id = ?",
                (archive.match_id,),
            ).fetchone()[0]
            == raw_archive
        )
        assert (
            connection.execute(
                "SELECT * FROM ratings ORDER BY rating_scope, game, entrant_id"
            ).fetchall()
            == ratings_before
        )
        assert (
            connection.execute(
                """
            SELECT * FROM rating_history
            ORDER BY match_id, rating_scope, game, entrant_id
            """
            ).fetchall()
            == history_before
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_failed_v3_to_v4_migration_rolls_back_schema_and_version(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "failed-v3-migration.db"
    SQLiteStore(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            DROP TABLE tournament_rating_contributions;
            DROP TABLE tournament_rating_snapshots;
            DROP TABLE tournament_pairings;
            DROP TABLE tournament_entrants;
            DROP TABLE tournament_archives;
            PRAGMA user_version = 3;
            """
        )
    original_create = SQLiteStore._create_tournament_schema

    def fail_after_create(connection: sqlite3.Connection) -> None:
        original_create(connection)
        raise RuntimeError("injected v4 migration failure")

    monkeypatch.setattr(
        SQLiteStore,
        "_create_tournament_schema",
        staticmethod(fail_after_create),
    )

    with pytest.raises(RuntimeError, match="injected v4 migration failure"):
        SQLiteStore(path, create=False)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert (
            connection.execute(
                """
                SELECT count(*) FROM sqlite_master
                WHERE type = 'table' AND name = 'tournament_archives'
                """
            ).fetchone()[0]
            == 0
        )


def test_opening_v4_database_rejects_broken_foreign_keys(tmp_path) -> None:
    path = tmp_path / "broken-v4-foreign-key.db"
    tournament = _tournament()
    SQLiteStore(path).save_tournament(tournament, rating_source="engine")
    contribution = tournament.pairings[0].series.legs[0]

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            DELETE FROM rating_history
            WHERE match_id = ? AND rating_scope = 'overall'
              AND game = '' AND entrant_id = 'test:A'
            """,
            (contribution.match_id,),
        )

    with pytest.raises(StorageError, match="外键完整性检查失败"):
        SQLiteStore(path, create=False)
