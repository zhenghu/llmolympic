"""Strictly read-only tournament audit and resume reliability tests."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from itertools import combinations
from pathlib import Path

import pytest
from rich.text import Text
from typer.testing import CliRunner

import llmolympic.core.storage as storage_module
from llmolympic import config
from llmolympic.cli.main import app
from llmolympic.core.archive import MatchArchive
from llmolympic.core.events import EventType, MatchEvent
from llmolympic.core.series import series_from_legs
from llmolympic.core.storage import (
    SQLiteStore,
    StorageError,
    TournamentAuditError,
    audit_tournament,
)
from llmolympic.core.tournament import (
    TournamentArchive,
    TournamentCheckpoint,
    round_robin_pair_seed,
    tournament_from_series,
)

STARTED = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
runner = CliRunner()


def _plain(output: str) -> str:
    return Text.from_ansi(output).plain


def _all_output(result) -> str:
    return _plain(result.stdout) + _plain(result.stderr)


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
) -> MatchArchive:
    scores = {
        descriptor["name"]: 1.0 if descriptor["name"] == winner else 0.0 for descriptor in players
    }
    return MatchArchive(
        schema_version=2,
        source="local_engine",
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


def _tournament(tournament_id: str = "audit-tournament") -> TournamentArchive:
    players = tuple(_descriptor(name) for name in ("secret-model-a", "B", "C"))
    series_archives = []
    for pairing_number, (first_index, second_index) in enumerate(
        combinations(range(len(players)), 2), start=1
    ):
        first = players[first_index]
        second = players[second_index]
        seed = round_robin_pair_seed(42, first["entrant_id"], second["entrant_id"])
        started_at = STARTED + timedelta(seconds=(pairing_number - 1) * 4)
        winner = min(first["name"], second["name"])
        first_leg = _match(
            match_id=f"{tournament_id}-pair-{pairing_number}-leg-1",
            seed=seed,
            players=(first, second),
            winner=winner,
            started_at=started_at,
        )
        second_leg = _match(
            match_id=f"{tournament_id}-pair-{pairing_number}-leg-2",
            seed=seed,
            players=(second, first),
            winner=winner,
            started_at=started_at + timedelta(seconds=2),
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


def _checkpoint(
    tournament: TournamentArchive,
    completed_count: int = 0,
) -> TournamentCheckpoint:
    completed_series = tuple(pairing.series for pairing in tournament.pairings[:completed_count])
    created_at = tournament.started_at - timedelta(seconds=1)
    return TournamentCheckpoint(
        tournament_id=tournament.tournament_id,
        game=tournament.game,
        game_config={},
        seed=tournament.seed,
        max_attempts=3,
        players=tournament.players,
        schedule=tuple(
            {
                "pairing_number": pairing.pairing_number,
                "player_indices": pairing.player_indices,
                "seed": pairing.seed,
            }
            for pairing in tournament.pairings
        ),
        completed_series=completed_series,
        created_at=created_at,
        updated_at=(created_at if not completed_series else completed_series[-1].finished_at),
    )


def _checkpoint_with_conflicting_identity(
    tournament: TournamentArchive,
) -> TournamentCheckpoint:
    payload = _checkpoint(tournament).model_dump(mode="python")
    payload["players"][0]["model"] = "forged-model"
    return TournamentCheckpoint.model_validate(payload)


def _assert_audit_error(code: str, tournament_id: str, database: Path) -> None:
    with pytest.raises(TournamentAuditError) as caught:
        audit_tournament(tournament_id, database)
    assert caught.value.code == code


def _insert_unbacked_rating(
    database: Path,
    tournament: TournamentArchive,
    descriptor: dict,
) -> None:
    observed_at = (tournament.started_at - timedelta(days=1)).isoformat()
    identity_json = json.dumps(
        {"kind": descriptor["kind"], "model": descriptor["model"]},
        sort_keys=True,
        separators=(",", ":"),
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO entrants (
                entrant_id, display_name, identity_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                descriptor["entrant_id"],
                descriptor["display_name"],
                identity_json,
                observed_at,
                observed_at,
            ),
        )
        for rating_scope, game in (("overall", ""), ("game", tournament.game)):
            connection.execute(
                """
                INSERT INTO ratings (
                    rating_scope, game, entrant_id, rating, games_played,
                    wins, draws, losses, updated_at
                ) VALUES (?, ?, ?, 1700.0, 0, 0, 0, 0, ?)
                """,
                (rating_scope, game, descriptor["entrant_id"], observed_at),
            )


def test_audit_finalized_tournament_deeply_verifies_archive_and_ratings(tmp_path: Path) -> None:
    database = tmp_path / "finalized.db"
    tournament = _tournament()
    SQLiteStore(database).save_tournament(tournament, rating_source="engine")

    report = audit_tournament(tournament.tournament_id, database)

    assert report.state == "finalized"
    assert report.game == "math_quiz"
    assert report.completed_pairings == report.pairing_count == 3
    assert report.technical_losses == 0
    assert report.rated
    assert not report.resumable
    assert not report.checkpoint_present
    assert report.leaderboard_replay_complete is True


def test_audit_rejects_missing_global_rating_operation(tmp_path: Path) -> None:
    database = tmp_path / "missing-rating-operation.db"
    tournament = _tournament("missing-rating-operation")
    SQLiteStore(database).save_tournament(tournament, rating_source="engine")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM rating_operations WHERE tournament_id = ?",
            (tournament.tournament_id,),
        )

    _assert_audit_error("tournament_inconsistent", tournament.tournament_id, database)


def test_audit_rejects_unbacked_non_default_opening_rating(tmp_path: Path) -> None:
    database = tmp_path / "unbacked-opening-rating.db"
    tournament = _tournament("unbacked-opening-rating")
    SQLiteStore(database)
    for descriptor in tournament.players:
        _insert_unbacked_rating(database, tournament, descriptor)

    SQLiteStore(database).save_tournament(tournament, rating_source="engine")

    _assert_audit_error("tournament_inconsistent", tournament.tournament_id, database)


def test_audit_rejects_unbacked_opening_rating_even_with_other_history(tmp_path: Path) -> None:
    database = tmp_path / "unbacked-opening-rating-partial.db"
    tournament = _tournament("unbacked-opening-rating-partial")
    store = SQLiteStore(database)
    _insert_unbacked_rating(database, tournament, tournament.players[0])
    store.save_tournament(tournament, rating_source="engine")
    store.save_match(
        _match(
            match_id="unbacked-opening-rating-later",
            seed=103,
            players=(_descriptor("secret-model-a"), _descriptor("D")),
            winner="D",
            started_at=tournament.finished_at + timedelta(seconds=10),
        ),
        rating_source="engine",
    )

    _assert_audit_error("tournament_inconsistent", tournament.tournament_id, database)


def test_audit_reports_missing_tournament_in_existing_database(tmp_path: Path) -> None:
    database = tmp_path / "missing-tournament.db"
    SQLiteStore(database)

    _assert_audit_error("tournament_not_found", "missing", database)
    result = runner.invoke(
        app,
        ["audit-tournament", "missing", "--db", str(database), "--json"],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "error_code": "tournament_not_found",
        "report_schema_version": 1,
        "result": "fail",
    }


def test_audit_unrated_tournament_marks_rating_checks_not_applicable(tmp_path: Path) -> None:
    database = tmp_path / "unrated.db"
    tournament = _tournament("unrated")
    SQLiteStore(database).save_tournament(tournament, rating_source="imported")

    result = runner.invoke(
        app,
        ["audit-tournament", tournament.tournament_id, "--db", str(database), "--json"],
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 0, result.output
    assert payload["rated"] is False
    assert payload["checks"]["ratings"] == "not_applicable"
    assert payload["checks"]["leaderboard"] == "not_applicable"


def test_audit_in_progress_checkpoint_reports_resumable_prefix(tmp_path: Path) -> None:
    database = tmp_path / "checkpoint.db"
    tournament = _tournament("in-progress")
    store = SQLiteStore(database)
    store.save_tournament_checkpoint(_checkpoint(tournament))
    lease = store.claim_tournament_runner(tournament.tournament_id).lease
    store.save_tournament_checkpoint(_checkpoint(tournament, 1), lease=lease)
    store.release_tournament_runner(lease)

    report = audit_tournament(tournament.tournament_id, database)

    assert report.state == "in_progress"
    assert report.completed_pairings == 1
    assert report.pairing_count == 3
    assert not report.rated
    assert report.resumable
    assert report.checkpoint_present
    assert report.leaderboard_replay_complete is None


def test_audit_active_runner_reports_checkpoint_not_yet_resumable(tmp_path: Path) -> None:
    database = tmp_path / "active-runner-checkpoint.db"
    tournament = _tournament("active-runner")
    store = SQLiteStore(database)
    store.save_tournament_checkpoint(_checkpoint(tournament))
    lease = store.claim_tournament_runner(tournament.tournament_id).lease

    report = audit_tournament(tournament.tournament_id, database)

    assert report.state == "in_progress"
    assert not report.resumable
    assert report.checkpoint_present
    assert store.release_tournament_runner(lease)


def test_checkpoint_identity_conflict_fails_before_calls_and_on_legacy_resume(
    tmp_path: Path,
) -> None:
    tournament = _tournament("identity-conflict")
    conflicting = _checkpoint_with_conflicting_identity(tournament)
    trusted_match = _match(
        match_id="trusted-identity",
        seed=111,
        players=(_descriptor("secret-model-a"), _descriptor("D")),
        winner="secret-model-a",
        started_at=tournament.started_at - timedelta(days=1),
    )

    create_database = tmp_path / "reject-at-create.db"
    create_store = SQLiteStore(create_database)
    create_store.save_match(trusted_match, rating_source="engine")
    with pytest.raises(StorageError, match="已绑定到另一份身份元数据"):
        create_store.save_tournament_checkpoint(conflicting)
    with sqlite3.connect(create_database) as connection:
        assert connection.execute("SELECT count(*) FROM tournament_checkpoints").fetchone()[0] == 0

    resume_database = tmp_path / "reject-at-resume.db"
    resume_store = SQLiteStore(resume_database)
    resume_store.save_tournament_checkpoint(conflicting)
    resume_store.save_match(trusted_match, rating_source="engine")

    _assert_audit_error("tournament_inconsistent", tournament.tournament_id, resume_database)
    result = runner.invoke(
        app,
        ["round-robin", "--resume", tournament.tournament_id, "--db", str(resume_database)],
    )
    output = _plain(result.output)
    assert result.exit_code == 1
    assert "无法读取循环赛检查点" in output
    assert "开始下一组" not in output


def test_checkpoint_identity_preflight_allows_first_trusted_override_of_import(
    tmp_path: Path,
) -> None:
    database = tmp_path / "imported-identity.db"
    tournament = _tournament("imported-identity")
    conflicting = _checkpoint_with_conflicting_identity(tournament)
    store = SQLiteStore(database)
    store.save_match(
        _match(
            match_id="imported-identity-observation",
            seed=112,
            players=(_descriptor("secret-model-a"), _descriptor("D")),
            winner="secret-model-a",
            started_at=tournament.started_at - timedelta(days=1),
        ),
        rating_source="imported",
    )

    result = store.save_tournament_checkpoint(conflicting)

    assert result.inserted
    assert audit_tournament(tournament.tournament_id, database).state == "in_progress"


def test_audit_finalized_checkpoint_verifies_formal_archive(tmp_path: Path) -> None:
    database = tmp_path / "finalized-checkpoint.db"
    tournament = _tournament("finalized-checkpoint")
    store = SQLiteStore(database)
    store.save_tournament_checkpoint(_checkpoint(tournament))
    lease = store.claim_tournament_runner(tournament.tournament_id).lease
    for count in range(1, 4):
        store.save_tournament_checkpoint(_checkpoint(tournament, count), lease=lease)
    store.finalize_tournament_checkpoint(tournament.tournament_id, lease=lease)

    report = audit_tournament(tournament.tournament_id, database)

    assert report.state == "finalized"
    assert report.checkpoint_present
    assert report.completed_pairings == report.pairing_count == 3
    assert report.rated
    assert report.leaderboard_replay_complete is True


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE tournament_entrants SET points = points + 1 WHERE tournament_id = ?",
        (
            "UPDATE tournament_rating_snapshots SET rating_after = rating_after + 1 "
            "WHERE tournament_id = ?"
        ),
        (
            'UPDATE entrants SET identity_json = \'{"kind":"forged"}\' '
            "WHERE entrant_id IN (SELECT entrant_id FROM tournament_entrants "
            "WHERE tournament_id = ? LIMIT 1)"
        ),
    ],
)
def test_audit_rejects_cross_table_and_elo_corruption(
    tmp_path: Path,
    statement: str,
) -> None:
    database = tmp_path / "corrupt-state.db"
    tournament = _tournament("corrupt-state")
    SQLiteStore(database).save_tournament(tournament, rating_source="engine")
    with sqlite3.connect(database) as connection:
        connection.execute(statement, (tournament.tournament_id,))

    _assert_audit_error("tournament_inconsistent", tournament.tournament_id, database)


def test_audit_rejects_archive_json_borrowed_from_another_tournament(tmp_path: Path) -> None:
    database = tmp_path / "borrowed-archive.db"
    requested = _tournament("requested-tournament")
    borrowed = _tournament("borrowed-tournament")
    store = SQLiteStore(database)
    store.save_tournament(requested, rating_source="imported")
    store.save_tournament(borrowed, rating_source="imported")
    with sqlite3.connect(database) as connection:
        borrowed_json = connection.execute(
            "SELECT tournament_json FROM tournament_archives WHERE tournament_id = ?",
            (borrowed.tournament_id,),
        ).fetchone()[0]
        connection.execute(
            "UPDATE tournament_archives SET tournament_json = ? WHERE tournament_id = ?",
            (borrowed_json, requested.tournament_id),
        )

    _assert_audit_error("tournament_inconsistent", requested.tournament_id, database)
    with pytest.raises(StorageError, match="正式循环赛档案已损坏"):
        store.get_verified_tournament(requested.tournament_id)
    with pytest.raises(StorageError, match="所属循环赛档案已损坏"):
        store.save_match(
            requested.pairings[0].series.legs[0],
            rating_source="imported",
        )


def test_idempotent_match_rejects_parent_json_borrowed_from_another_series(
    tmp_path: Path,
) -> None:
    database = tmp_path / "borrowed-series-archive.db"
    requested = _tournament("requested-series").pairings[0].series
    borrowed = _tournament("borrowed-series").pairings[0].series
    store = SQLiteStore(database)
    store.save_series(requested, rating_source="imported")
    store.save_series(borrowed, rating_source="imported")
    with sqlite3.connect(database) as connection:
        borrowed_json = connection.execute(
            "SELECT series_json FROM series_archives WHERE series_id = ?",
            (borrowed.series_id,),
        ).fetchone()[0]
        connection.execute(
            "UPDATE series_archives SET series_json = ? WHERE series_id = ?",
            (borrowed_json, requested.series_id),
        )

    with pytest.raises(StorageError, match="所属系列赛档案已损坏"):
        store.save_match(requested.legs[0], rating_source="imported")


@pytest.mark.parametrize("column", ["created_at", "updated_at"])
def test_audit_rejects_invalid_global_entrant_timestamps(
    tmp_path: Path,
    column: str,
) -> None:
    database = tmp_path / f"invalid-entrant-{column}.db"
    tournament = _tournament(f"invalid-entrant-{column}")
    SQLiteStore(database).save_tournament(tournament, rating_source="engine")
    with sqlite3.connect(database) as connection:
        connection.execute(
            f"""
            UPDATE entrants SET {column} = 'not-a-timestamp'
            WHERE entrant_id IN (
                SELECT entrant_id FROM tournament_entrants
                WHERE tournament_id = ? LIMIT 1
            )
            """,  # noqa: S608 - column is constrained by the test parameter above
            (tournament.tournament_id,),
        )

    _assert_audit_error("tournament_inconsistent", tournament.tournament_id, database)


def test_audit_preserves_database_bytes_permissions_and_sidecars(tmp_path: Path) -> None:
    database = tmp_path / "read-only.db"
    tournament = _tournament("read-only")
    SQLiteStore(database).save_tournament(tournament, rating_source="engine")
    if os.name == "posix":
        database.chmod(0o640)
    before = database.read_bytes()
    before_stat = database.stat()

    report = audit_tournament(tournament.tournament_id, database)
    after_stat = database.stat()

    assert report.state == "finalized"
    assert database.read_bytes() == before
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert after_stat.st_mode == before_stat.st_mode
    assert not Path(f"{database}-journal").exists()
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()


def test_audit_uses_immutable_query_only_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "connection-mode.db"
    tournament = _tournament("connection-mode")
    SQLiteStore(database).save_tournament(tournament, rating_source="engine")
    real_connect = sqlite3.connect
    observed: dict[str, object] = {"statements": []}

    def recording_connect(database_arg, *args, **kwargs):
        observed["database"] = database_arg
        observed["uri"] = kwargs.get("uri")
        connection = real_connect(database_arg, *args, **kwargs)
        connection.set_trace_callback(observed["statements"].append)
        return connection

    monkeypatch.setattr(storage_module.sqlite3, "connect", recording_connect)

    audit_tournament(tournament.tournament_id, database)

    assert "mode=ro&immutable=1" in str(observed["database"])
    assert observed["uri"] is True
    statements = {str(statement).lower() for statement in observed["statements"]}
    assert "pragma query_only = on" in statements
    assert "pragma trusted_schema = off" in statements


def test_audit_fails_if_main_file_changes_during_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "racing-writer.db"
    tournament = _tournament("racing-writer")
    SQLiteStore(database).save_tournament(tournament, rating_source="engine")
    original = SQLiteStore._load_verified_tournament

    def load_then_touch(self, connection, tournament_id):
        loaded = original(self, connection, tournament_id)
        before = database.stat()
        os.utime(database, ns=(before.st_atime_ns, before.st_mtime_ns + 1))
        return loaded

    monkeypatch.setattr(SQLiteStore, "_load_verified_tournament", load_then_touch)

    _assert_audit_error("database_active_writer", tournament.tournament_id, database)


def test_audit_fails_closed_when_database_has_wal_or_journal(tmp_path: Path) -> None:
    database = tmp_path / "active.db"
    tournament = _tournament("active")
    SQLiteStore(database).save_tournament(tournament, rating_source="engine")

    for suffix in ("-wal", "-journal"):
        sidecar = Path(f"{database}{suffix}")
        sidecar.write_bytes(b"active-writer-sentinel")
        _assert_audit_error("database_active_writer", tournament.tournament_id, database)
        sidecar.unlink()


def test_audit_refuses_old_schema_without_migrating_or_chmodding(tmp_path: Path) -> None:
    database = tmp_path / "legacy-v4.db"
    SQLiteStore(database)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            DROP TABLE tournament_checkpoint_series;
            DROP TABLE tournament_checkpoints;
            PRAGMA user_version = 4;
            """
        )
    if os.name == "posix":
        database.chmod(0o640)
    before = database.read_bytes()
    before_stat = database.stat()

    _assert_audit_error("database_migration_required", "any-tournament", database)

    after_stat = database.stat()
    assert database.read_bytes() == before
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert after_stat.st_mode == before_stat.st_mode
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4


def test_audit_missing_database_does_not_create_it(tmp_path: Path) -> None:
    database = tmp_path / "missing.db"

    _assert_audit_error("database_missing", "missing", database)

    assert not database.exists()


def test_audit_classifies_future_and_corrupt_databases_without_raw_content(
    tmp_path: Path,
) -> None:
    future = tmp_path / "future.db"
    with sqlite3.connect(future) as connection:
        connection.execute("PRAGMA user_version = 99")
    _assert_audit_error("database_unsupported_schema", "future", future)

    corrupt = tmp_path / "corrupt.db"
    sentinel = "raw-database-secret"
    corrupt.write_bytes(f"not sqlite {sentinel}".encode())
    result = runner.invoke(
        app,
        ["audit-tournament", "corrupt", "--db", str(corrupt), "--json"],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "error_code": "database_invalid",
        "report_schema_version": 1,
        "result": "fail",
    }
    assert sentinel not in _all_output(result)


@pytest.mark.parametrize(
    "content",
    ["[storage\ndatabase = 'broken'", 'storage = "not-a-table"'],
)
def test_audit_json_maps_invalid_config_without_warning_or_path_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
) -> None:
    config_path = tmp_path / "private-config-path.toml"
    config_path.write_text(content, encoding="utf-8")
    config_path.chmod(0o644)
    monkeypatch.setenv("LLMOLYMPIC_CONFIG", str(config_path))
    monkeypatch.delenv("LLMOLYMPIC_DB", raising=False)
    config.load_config.cache_clear()

    result = runner.invoke(app, ["audit-tournament", "configured", "--json"])
    config.load_config.cache_clear()

    assert result.exit_code == 1
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "error_code": "database_invalid",
        "report_schema_version": 1,
        "result": "fail",
    }
    assert str(config_path) not in _all_output(result)
    assert "Traceback" not in _all_output(result)


def test_audit_json_suppresses_shared_config_warning_on_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "configured.db"
    tournament = _tournament("configured")
    SQLiteStore(database).save_tournament(tournament, rating_source="engine")
    config_path = tmp_path / "shared-config.toml"
    config_path.write_text(
        f'[storage]\ndatabase = "{database}"\n',
        encoding="utf-8",
    )
    config_path.chmod(0o644)
    monkeypatch.setenv("LLMOLYMPIC_CONFIG", str(config_path))
    monkeypatch.delenv("LLMOLYMPIC_DB", raising=False)
    config.load_config.cache_clear()

    result = runner.invoke(app, ["audit-tournament", tournament.tournament_id, "--json"])
    config.load_config.cache_clear()

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert json.loads(result.stdout)["result"] == "pass"
    assert str(config_path) not in _all_output(result)


def test_audit_replays_interleaved_match_tournament_and_series_operations(
    tmp_path: Path,
) -> None:
    database = tmp_path / "interleaved-rating-operations.db"
    tournament = _tournament("interleaved-rating-operations")
    shared = _descriptor("secret-model-a")
    outsider = _descriptor("D")
    store = SQLiteStore(database)
    warmup = _match(
        match_id="interleaved-warmup",
        seed=97,
        players=(shared, outsider),
        winner="secret-model-a",
        started_at=tournament.started_at - timedelta(days=1),
    )
    store.save_match(warmup, rating_source="engine")
    store.save_tournament(tournament, rating_source="engine")
    first_leg = _match(
        match_id="interleaved-series-1",
        seed=98,
        players=(shared, outsider),
        winner="D",
        started_at=tournament.finished_at + timedelta(seconds=10),
    )
    second_leg = _match(
        match_id="interleaved-series-2",
        seed=98,
        players=(outsider, shared),
        winner="secret-model-a",
        started_at=tournament.finished_at + timedelta(seconds=12),
    )
    series = series_from_legs(first_leg, second_leg, series_id="interleaved-series")
    store.save_series(series, rating_source="engine")

    report = audit_tournament(tournament.tournament_id, database)

    assert report.leaderboard_replay_complete is True
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            """
            SELECT rating_operation_seq, match_id, series_id, tournament_id
            FROM rating_operations ORDER BY rating_operation_seq
            """
        ).fetchall() == [
            (1, warmup.match_id, None, None),
            (2, None, None, tournament.tournament_id),
            (3, None, series.series_id, None),
        ]


def test_audit_replays_current_leaderboard_after_later_match(tmp_path: Path) -> None:
    database = tmp_path / "historical.db"
    tournament = _tournament("historical")
    store = SQLiteStore(database)
    store.save_tournament(tournament, rating_source="engine")
    store.save_match(
        _match(
            match_id="later-match",
            seed=99,
            players=(_descriptor("secret-model-a"), _descriptor("D")),
            winner="D",
            started_at=tournament.finished_at + timedelta(seconds=10),
        ),
        rating_source="engine",
    )

    report = audit_tournament(tournament.tournament_id, database)

    assert report.state == "finalized"
    assert report.rated
    assert report.leaderboard_replay_complete is True


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("rating", 999_999.0),
        ("updated_at", "2099-01-01T00:00:00+00:00"),
    ],
)
def test_audit_rejects_basic_leaderboard_corruption_during_full_replay(
    tmp_path: Path,
    column: str,
    value: object,
) -> None:
    database = tmp_path / f"replay-corrupt-{column}.db"
    tournament = _tournament(f"replay-corrupt-{column}")
    store = SQLiteStore(database)
    store.save_tournament(tournament, rating_source="engine")
    store.save_match(
        _match(
            match_id=f"replay-corrupt-{column}-later",
            seed=102,
            players=(_descriptor("secret-model-a"), _descriptor("D")),
            winner="D",
            started_at=tournament.finished_at + timedelta(seconds=10),
        ),
        rating_source="engine",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            f"""
            UPDATE ratings SET {column} = ?
            WHERE rating_scope = 'overall' AND game = '' AND entrant_id = ?
            """,  # noqa: S608 - column is constrained by the test parameter above
            (value, "test:secret-model-a"),
        )

    _assert_audit_error("tournament_inconsistent", tournament.tournament_id, database)


def test_audit_rejects_epsilon_tamper_in_later_rating_history(tmp_path: Path) -> None:
    database = tmp_path / "epsilon-rating-tamper.db"
    tournament = _tournament("epsilon-rating-tamper")
    store = SQLiteStore(database)
    store.save_tournament(tournament, rating_source="engine")
    later = _match(
        match_id="epsilon-rating-tamper-later",
        seed=103,
        players=(_descriptor("secret-model-a"), _descriptor("D")),
        winner="D",
        started_at=tournament.finished_at + timedelta(seconds=10),
    )
    store.save_match(later, rating_source="engine")
    with sqlite3.connect(database) as connection:
        # This is below the old replay tolerance.  Updating both history and the
        # materialized leaderboard used to let the modified value propagate and
        # still produce a false PASS.
        connection.execute(
            "UPDATE rating_history SET rating_after = rating_after + 5e-10 WHERE match_id = ?",
            (later.match_id,),
        )
        connection.execute(
            """
            UPDATE ratings SET rating = rating + 5e-10
            WHERE entrant_id IN (?, ?)
            """,
            ("test:secret-model-a", "test:D"),
        )

    _assert_audit_error("tournament_inconsistent", tournament.tournament_id, database)


def test_audit_rejects_orphaned_other_tournament_snapshot(tmp_path: Path) -> None:
    database = tmp_path / "orphaned-other-snapshot.db"
    tournament = _tournament("orphaned-other-snapshot-target")
    decoy = _tournament("orphaned-other-snapshot-decoy")
    store = SQLiteStore(database)
    store.save_tournament(tournament, rating_source="engine")
    store.save_tournament(decoy, rating_source="imported")
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO tournament_rating_snapshots (
                tournament_id, rating_scope, game, entrant_id, display_name,
                rating_before, rating_after, games_added, wins_added,
                draws_added, losses_added
            ) VALUES (?, 'overall', '', ?, ?, 1500.0, 1500.0, 0, 0, 0, 0)
            """,
            (
                decoy.tournament_id,
                "test:secret-model-a",
                "secret-model-a",
            ),
        )

    _assert_audit_error("tournament_inconsistent", tournament.tournament_id, database)


def test_audit_replays_commit_order_when_later_operation_has_older_event_time(
    tmp_path: Path,
) -> None:
    database = tmp_path / "historical-commit-order.db"
    tournament = _tournament("historical-commit-order")
    store = SQLiteStore(database)
    store.save_tournament(tournament, rating_source="engine")
    store.save_match(
        _match(
            match_id="later-commit-older-event",
            seed=100,
            players=(_descriptor("secret-model-a"), _descriptor("D")),
            winner="D",
            started_at=tournament.started_at - timedelta(days=1),
        ),
        rating_source="engine",
    )

    report = audit_tournament(tournament.tournament_id, database)

    assert report.state == "finalized"
    assert report.leaderboard_replay_complete is True


def test_audit_replays_other_rating_operation_saved_before_tournament(
    tmp_path: Path,
) -> None:
    database = tmp_path / "prior-rating-operation.db"
    tournament = _tournament("prior-rating-operation")
    store = SQLiteStore(database)
    store.save_match(
        _match(
            match_id="prior-warmup",
            seed=101,
            players=(_descriptor("secret-model-a"), _descriptor("D")),
            winner="secret-model-a",
            started_at=tournament.started_at - timedelta(days=1),
        ),
        rating_source="engine",
    )
    store.save_tournament(tournament, rating_source="engine")

    report = audit_tournament(tournament.tournament_id, database)

    assert report.state == "finalized"
    assert report.leaderboard_replay_complete is True


def test_audit_rejects_finalization_before_checkpoint_completion(tmp_path: Path) -> None:
    database = tmp_path / "bad-finalized-at.db"
    tournament = _tournament("bad-finalized-at")
    store = SQLiteStore(database)
    store.save_tournament_checkpoint(_checkpoint(tournament))
    lease = store.claim_tournament_runner(tournament.tournament_id).lease
    for count in range(1, 4):
        store.save_tournament_checkpoint(_checkpoint(tournament, count), lease=lease)
    store.finalize_tournament_checkpoint(tournament.tournament_id, lease=lease)
    bad_timestamp = (tournament.finished_at - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE tournament_checkpoints SET finalized_at = ? WHERE tournament_id = ?",
            (bad_timestamp, tournament.tournament_id),
        )

    _assert_audit_error("tournament_inconsistent", tournament.tournament_id, database)
    result = runner.invoke(
        app,
        ["round-robin", "--resume", tournament.tournament_id, "--db", str(database)],
    )
    output = _plain(result.output)
    assert result.exit_code == 1
    assert "无法读取循环赛检查点" in output
    assert "已完成，无需恢复" not in output


def test_finalize_never_writes_time_before_checkpoint_when_wall_clock_moves_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "clock-rollback.db"
    tournament = _tournament("clock-rollback")
    store = SQLiteStore(database)
    store.save_tournament_checkpoint(_checkpoint(tournament))
    lease = store.claim_tournament_runner(tournament.tournament_id).lease
    for count in range(1, 4):
        store.save_tournament_checkpoint(_checkpoint(tournament, count), lease=lease)

    class ClockBehind(datetime):
        @classmethod
        def now(cls, tz=None):
            value = STARTED - timedelta(days=1)
            return value if tz is not None else value.replace(tzinfo=None)

    monkeypatch.setattr(storage_module, "datetime", ClockBehind)

    store.finalize_tournament_checkpoint(tournament.tournament_id, lease=lease)

    with sqlite3.connect(database) as connection:
        finalized_at, updated_at = connection.execute(
            "SELECT finalized_at, updated_at FROM tournament_checkpoints WHERE tournament_id = ?",
            (tournament.tournament_id,),
        ).fetchone()
    assert datetime.fromisoformat(finalized_at) >= datetime.fromisoformat(updated_at)
    assert audit_tournament(tournament.tournament_id, database).state == "finalized"


def test_audit_cli_emits_stable_json_without_entrant_details(tmp_path: Path) -> None:
    database = tmp_path / "cli-success.db"
    tournament = _tournament("cli-success")
    SQLiteStore(database).save_tournament(tournament, rating_source="engine")

    result = runner.invoke(
        app,
        ["audit-tournament", tournament.tournament_id, "--db", str(database), "--json"],
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert payload == {
        "checks": {
            "archive": "pass",
            "checkpoint": "not_applicable",
            "database": "pass",
            "leaderboard": "pass",
            "ratings": "pass",
        },
        "game": "math_quiz",
        "progress": {
            "completed_matches": 6,
            "completed_pairings": 3,
            "match_count": 6,
            "pairing_count": 3,
        },
        "rated": True,
        "report_schema_version": 1,
        "result": "pass",
        "resumable": False,
        "state": "finalized",
        "technical_losses": 0,
        "tournament_id": tournament.tournament_id,
    }
    assert "\x1b" not in result.stdout
    assert "secret-model-a" not in _all_output(result)


def test_audit_cli_never_constructs_providers_or_accesses_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "offline.db"
    tournament = _tournament("offline")
    SQLiteStore(database).save_tournament(tournament, rating_source="engine")

    def unexpected_call(*args, **kwargs):
        raise AssertionError("tournament audit must remain offline")

    monkeypatch.setattr(
        "llmolympic.providers.openai_provider.OpenAIProvider.__init__",
        unexpected_call,
    )
    monkeypatch.setattr("httpx.Client.request", unexpected_call)
    monkeypatch.setattr("httpx.AsyncClient.request", unexpected_call)

    result = runner.invoke(
        app,
        ["audit-tournament", tournament.tournament_id, "--db", str(database), "--json"],
    )

    assert result.exit_code == 0, result.output


def test_audit_cli_failure_is_redacted_and_machine_readable(tmp_path: Path) -> None:
    database = tmp_path / "cli-failure.db"
    sentinel = "secret-model-a"
    tournament = _tournament("cli-failure")
    SQLiteStore(database).save_tournament(tournament, rating_source="engine")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE tournament_entrants SET points = points + 1 WHERE tournament_id = ?",
            (tournament.tournament_id,),
        )

    result = runner.invoke(
        app,
        ["audit-tournament", tournament.tournament_id, "--db", str(database), "--json"],
    )

    assert result.exit_code == 1
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "error_code": "tournament_inconsistent",
        "report_schema_version": 1,
        "result": "fail",
    }
    assert sentinel not in _all_output(result)


def test_audit_cli_rejects_blank_id_before_opening_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_call(*args, **kwargs):
        raise AssertionError("blank tournament id must fail before database access")

    monkeypatch.setattr("llmolympic.cli.main.audit_tournament", unexpected_call)

    result = runner.invoke(app, ["audit-tournament", "   ", "--json"])

    assert result.exit_code == 2
    assert "循环赛 ID 不能为空" in _plain(result.output)


def test_resume_deeply_verifies_completed_tournament_before_declaring_success(
    tmp_path: Path,
) -> None:
    database = tmp_path / "resume-corrupt.db"
    tournament = _tournament("resume-corrupt")
    SQLiteStore(database).save_tournament(tournament, rating_source="engine")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE tournament_entrants SET points = points + 1 WHERE tournament_id = ?",
            (tournament.tournament_id,),
        )

    result = runner.invoke(
        app,
        ["round-robin", "--resume", tournament.tournament_id, "--db", str(database)],
    )
    output = _plain(result.output)

    assert result.exit_code == 1
    assert "无法读取循环赛检查点" in output
    assert "已完成，无需恢复" not in output


def test_resume_rejects_corrupt_completed_tournament_rating_snapshot(
    tmp_path: Path,
) -> None:
    database = tmp_path / "resume-corrupt-rating.db"
    tournament = _tournament("resume-corrupt-rating")
    SQLiteStore(database).save_tournament(tournament, rating_source="engine")
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE tournament_rating_snapshots
            SET rating_after = rating_after + 1
            WHERE tournament_id = ? AND rating_scope = 'overall'
            """,
            (tournament.tournament_id,),
        )

    result = runner.invoke(
        app,
        ["round-robin", "--resume", tournament.tournament_id, "--db", str(database)],
    )
    output = _plain(result.output)

    assert result.exit_code == 1
    assert "无法读取循环赛检查点" in output
    assert "已完成，无需恢复" not in output


def test_runtime_verified_tournament_uses_one_explicit_read_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "verified-snapshot.db"
    tournament = _tournament("verified-snapshot")
    store = SQLiteStore(database)
    store.save_tournament(tournament, rating_source="engine")
    original = SQLiteStore._load_verified_tournament
    transaction_states: list[bool] = []

    def observe_transaction(self, connection, tournament_id):
        transaction_states.append(connection.in_transaction)
        return original(self, connection, tournament_id)

    monkeypatch.setattr(SQLiteStore, "_load_verified_tournament", observe_transaction)

    loaded = store.get_verified_tournament(tournament.tournament_id)

    assert loaded is not None
    assert transaction_states == [True]


def test_root_help_registers_tournament_audit_command() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "audit-tournament" in _plain(result.output)
