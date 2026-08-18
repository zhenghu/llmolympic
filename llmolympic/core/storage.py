"""SQLite persistence for match archives and ELO ratings.

The complete :class:`~llmolympic.core.archive.MatchArchive` JSON is the
canonical record.  A small amount of metadata is stored alongside it for fast
history and leaderboard queries.  Saving a match and updating both the
per-game and overall ELO tables happens in one transaction.

"""

from __future__ import annotations

import os
import sqlite3
import warnings
from contextlib import closing
from pathlib import Path

from llmolympic.core._storage_championship import _ChampionshipMixin
from llmolympic.core._storage_entrants import _EntrantsMixin
from llmolympic.core._storage_matches import _MatchesMixin
from llmolympic.core._storage_schema import _SchemaMixin
from llmolympic.core._storage_tournament import _TournamentMixin
from llmolympic.core._storage_types import (
    _PRIVATE_DIRECTORY_MODE,
    _PRIVATE_FILE_MODE,
    _SQLITE_SIDECAR_SUFFIXES,
    DEFAULT_TOURNAMENT_RUNNER_LEASE_SECONDS,
    SCHEMA_VERSION,
    SQLITE_INT_MAX,
    SQLITE_INT_MIN,
    DatabaseInspection,
    MatchIdCollisionError,
    MatchSummary,
    ProviderBudgetCollisionError,
    ProviderBudgetPendingError,
    ProviderCallAttemptCollisionError,
    RatingChange,
    RatingEntry,
    SaveResult,
    SeriesIdCollisionError,
    SQLiteUsageReservation,
    StorageError,
    TournamentAuditError,
    TournamentAuditReport,
    TournamentCheckpointCollisionError,
    TournamentIdCollisionError,
    TournamentRunnerLease,
    TournamentRunnerLeaseBusyError,
    TournamentRunnerLeaseLostError,
    TournamentSaveResult,
    UnsupportedSchemaError,
    _set_private_mode,
    database_path,
)
from llmolympic.core._storage_usage import _ProviderUsageMixin
from llmolympic.core.usage import BudgetPoisonedError


class SQLiteStore(
    _SchemaMixin,
    _EntrantsMixin,
    _MatchesMixin,
    _TournamentMixin,
    _ChampionshipMixin,
    _ProviderUsageMixin,
):
    """Persistent match archive and ELO repository backed by SQLite.

    The class is assembled from focused mixins; see the private
    ``_storage_*`` modules for schema, entrants, matches, tournament and
    Provider-budget responsibilities.
    """

    def __init__(self, path: str | Path | None = None, *, create: bool = True) -> None:
        self.path = database_path(path)
        parent_created = False
        if create:
            parent_created = not self.path.parent.exists()
            self.path.parent.mkdir(
                mode=_PRIVATE_DIRECTORY_MODE,
                parents=True,
                exist_ok=True,
            )
            self._create_database_file()
            if not self.path.is_file():
                raise StorageError(f"数据库路径不是普通文件：{self.path}")
        elif not self.path.is_file():
            raise StorageError(f"数据库不存在：{self.path}")
        default_directory = (Path.home() / ".llmolympic").resolve()
        if parent_created or self.path.parent == default_directory:
            _set_private_mode(self.path.parent, _PRIVATE_DIRECTORY_MODE, required=True)
        self._secure_database_artifacts()
        self._initialize(create=create)
        self._secure_database_artifacts()

    def _create_database_file(self) -> None:
        """Pre-create a new database without a world-readable umask window."""

        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                _PRIVATE_FILE_MODE,
            )
        except FileExistsError:
            return
        else:
            os.close(descriptor)

    def _secure_database_artifacts(self) -> None:
        _set_private_mode(self.path, _PRIVATE_FILE_MODE, required=True)
        for suffix in _SQLITE_SIDECAR_SUFFIXES:
            _set_private_mode(Path(f"{self.path}{suffix}"), _PRIVATE_FILE_MODE)

    def _connect(self) -> sqlite3.Connection:
        self._secure_database_artifacts()
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        self._secure_database_artifacts()
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self, *, create: bool) -> None:
        with closing(self._connect()) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise UnsupportedSchemaError(
                    f"数据库版本 {version} 高于当前支持的版本 {SCHEMA_VERSION}"
                )
            if version == 0 and not create:
                raise StorageError(f"数据库尚未初始化：{self.path}")
            if version == SCHEMA_VERSION:
                self._verify_schema(connection)
                self._verify_foreign_keys(connection)
                return

            # Rebuilding the two archive parent tables into one canonical v7 shape
            # requires foreign-key enforcement to be disabled before the transaction
            # starts.  The migration still runs a full foreign_key_check before commit
            # and enforcement is restored in ``finally`` on every path.
            foreign_keys_disabled = 1 <= version < 7
            if foreign_keys_disabled:
                connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            try:
                locked_version = connection.execute("PRAGMA user_version").fetchone()[0]
                if locked_version > SCHEMA_VERSION:
                    raise UnsupportedSchemaError(
                        f"数据库版本 {locked_version} 高于当前支持的版本 {SCHEMA_VERSION}"
                    )
                if locked_version == 0:
                    self._create_base_schema(connection)
                    self._create_series_schema(connection)
                elif locked_version == 1:
                    self._verify_legacy_schema(connection, include_series=False)
                    self._migrate_to_v3(connection, include_series=False)
                    self._create_series_schema(connection)
                elif locked_version == 2:
                    self._verify_legacy_schema(connection, include_series=True)
                    self._migrate_to_v3(connection, include_series=True)
                elif locked_version == 3:
                    self._verify_v3_schema(connection)
                elif locked_version == 4:
                    self._verify_v4_schema(connection)
                elif locked_version == 5:
                    self._verify_v5_schema(connection)
                elif locked_version == 6:
                    self._verify_v6_schema(connection)
                elif locked_version == 7:
                    self._verify_v7_schema(connection)
                elif locked_version == 8:
                    self._verify_v8_schema(connection)
                if locked_version < 4:
                    self._create_tournament_schema(connection)
                if locked_version < 5:
                    self._create_checkpoint_schema(connection)
                if locked_version < 6:
                    self._create_runner_lease_schema(connection)
                if locked_version < 7:
                    if locked_version > 0:
                        self._canonicalize_archive_tables(connection)
                    self._create_rating_operation_schema(connection)
                    self._backfill_rating_operations(connection)
                if locked_version < 8:
                    self._create_provider_usage_schema(connection)
                if locked_version < 9:
                    self._create_championship_schema(connection)
                self._verify_schema(connection)
                self._verify_foreign_keys(connection)
                if 0 < locked_version < 7:
                    # A v1-v6 database has no durable operation sequence.  Rowid
                    # order is the best available backfill source, but it is not
                    # itself an integrity guarantee (rowids can be rewritten).
                    # Refuse to publish v7 unless that inferred order reproduces
                    # the complete materialized leaderboard.
                    self._verify_global_rating_operation_replay(connection)
                if locked_version < SCHEMA_VERSION:
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                if foreign_keys_disabled:
                    connection.execute("PRAGMA foreign_keys = ON")

def inspect_database(path: str | Path | None = None) -> DatabaseInspection:
    """Inspect SQLite without creating, chmodding, migrating, or writing sidecars.

    An active rollback journal or WAL requires SQLite's locking/sidecar coordination to
    read correctly.  The diagnostic therefore stops before opening the database instead
    of creating a ``-shm`` file or inspecting a stale main-file snapshot.  Without an
    active journal, ``immutable=1`` and ``query_only`` provide a strictly read-only
    compatibility and integrity check.
    """

    resolved = database_path(path)
    if not resolved.exists():
        return DatabaseInspection(path=resolved, exists=False)
    if not resolved.is_file():
        raise StorageError("数据库路径不是普通文件")

    database_stat = resolved.stat()
    private_permissions = os.name != "posix" or database_stat.st_mode & 0o077 == 0
    journal_paths = (Path(f"{resolved}-journal"), Path(f"{resolved}-wal"))
    if any(sidecar.exists() for sidecar in journal_paths):
        return DatabaseInspection(
            path=resolved,
            exists=True,
            private_permissions=private_permissions,
            limited_by_active_journal=True,
        )

    uri = f"{resolved.as_uri()}?mode=ro&immutable=1"
    try:
        with closing(sqlite3.connect(uri, uri=True, isolation_level=None)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            check_rows = connection.execute("PRAGMA quick_check(1)").fetchall()
            if [row[0] for row in check_rows] != ["ok"]:
                raise StorageError("SQLite 完整性检查失败")

            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise UnsupportedSchemaError(
                    f"数据库版本 {version} 高于当前支持的版本 {SCHEMA_VERSION}"
                )
            if version == 0:
                raise StorageError("数据库尚未初始化")
            if version == 1:
                SQLiteStore._verify_legacy_schema(connection, include_series=False)
            elif version == 2:
                SQLiteStore._verify_legacy_schema(connection, include_series=True)
            elif version == 3:
                SQLiteStore._verify_v3_schema(connection)
            elif version == 4:
                SQLiteStore._verify_v4_schema(connection)
            elif version == 5:
                SQLiteStore._verify_v5_schema(connection)
            elif version == 6:
                SQLiteStore._verify_v6_schema(connection)
            elif version == 7:
                SQLiteStore._verify_v7_schema(connection)
            else:
                SQLiteStore._verify_schema(connection)
            SQLiteStore._verify_foreign_keys(connection)
    except (sqlite3.Error, ValueError) as exc:
        raise StorageError("SQLite 数据库无法读取或已损坏") from exc

    # A writer may have opened a rollback journal or WAL while the immutable snapshot was
    # being inspected.  A changed main file is likewise evidence that the snapshot raced
    # a writer.  Do not report that possibly stale snapshot as healthy.
    after_stat = resolved.stat()
    limited_by_active_journal = any(sidecar.exists() for sidecar in journal_paths) or (
        database_stat.st_dev,
        database_stat.st_ino,
        database_stat.st_size,
        database_stat.st_mtime_ns,
    ) != (
        after_stat.st_dev,
        after_stat.st_ino,
        after_stat.st_size,
        after_stat.st_mtime_ns,
    )
    return DatabaseInspection(
        path=resolved,
        exists=True,
        schema_version=version,
        migration_required=version < SCHEMA_VERSION,
        private_permissions=private_permissions,
        limited_by_active_journal=limited_by_active_journal,
    )

def audit_tournament(
    tournament_id: str,
    path: str | Path | None = None,
) -> TournamentAuditReport:
    """Deeply audit one tournament without creating, migrating, or chmodding SQLite.

    The database is opened as an immutable, query-only snapshot.  Active
    rollback journals or WAL files fail closed because a correct snapshot would
    require SQLite sidecar coordination.  Only the current schema is audited;
    callers must explicitly migrate older databases through the normal writer
    path before using this command.
    """

    if not isinstance(tournament_id, str) or not tournament_id.strip():
        raise ValueError("tournament_id must be a non-empty string")

    try:
        # Audit output is a disclosure-safe diagnostics boundary.  Config
        # permission warnings include local paths and would corrupt --json.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            resolved = database_path(path)
    except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise TournamentAuditError("database_invalid") from exc

    try:
        if not resolved.exists():
            raise TournamentAuditError("database_missing")
        if not resolved.is_file():
            raise TournamentAuditError("database_invalid")
        database_stat = resolved.stat()
        journal_paths = (Path(f"{resolved}-journal"), Path(f"{resolved}-wal"))
        active_sidecar = any(sidecar.exists() for sidecar in journal_paths)
    except TournamentAuditError:
        raise
    except (OSError, ValueError) as exc:
        raise TournamentAuditError("database_invalid") from exc
    if active_sidecar:
        raise TournamentAuditError("database_active_writer")

    uri = f"{resolved.as_uri()}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True, isolation_level=None)
    except sqlite3.Error as exc:
        raise TournamentAuditError("database_invalid") from exc

    report: TournamentAuditReport | None = None
    audit_error: TournamentAuditError | None = None
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
        if [row[0] for row in integrity_rows] != ["ok"]:
            raise TournamentAuditError("database_invalid")

        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise TournamentAuditError("database_unsupported_schema")
        if version == 0:
            raise TournamentAuditError("database_invalid")
        if version < SCHEMA_VERSION:
            raise TournamentAuditError("database_migration_required")
        try:
            SQLiteStore._verify_schema(connection)
            SQLiteStore._verify_foreign_keys(connection)
        except StorageError as exc:
            raise TournamentAuditError("database_invalid") from exc

        verifier = object.__new__(SQLiteStore)
        try:
            loaded_checkpoint = verifier._load_tournament_checkpoint(
                connection,
                tournament_id,
            )
            runner_lease = verifier._load_tournament_runner_lease(
                connection,
                tournament_id,
            )
            loaded_tournament = verifier._load_verified_tournament(
                connection,
                tournament_id,
            )
            budget_rows = connection.execute(
                "SELECT budget_id FROM provider_budgets WHERE tournament_id = ?",
                (tournament_id,),
            ).fetchall()
            for budget_row in budget_rows:
                verifier._provider_budget_snapshot_in_transaction(
                    connection,
                    budget_row["budget_id"],
                )
            if loaded_checkpoint is None and loaded_tournament is None:
                raise TournamentAuditError("tournament_not_found")

            if loaded_checkpoint is not None:
                checkpoint, state = loaded_checkpoint
                checkpoint_present = True
                if state == "in_progress":
                    if loaded_tournament is not None:
                        raise StorageError("in-progress checkpoint has a formal archive")
                    for series in checkpoint.completed_series:
                        if connection.execute(
                            "SELECT 1 FROM series_archives WHERE series_id = ?",
                            (series.series_id,),
                        ).fetchone():
                            raise StorageError("in-progress checkpoint series is formally stored")
                        for leg in series.legs:
                            if connection.execute(
                                "SELECT 1 FROM matches WHERE match_id = ?",
                                (leg.match_id,),
                            ).fetchone():
                                raise StorageError(
                                    "in-progress checkpoint match is formally stored"
                                )
                    tournament = None
                    rated = False
                    leaderboard_replay_complete = None
                    completed_series = checkpoint.completed_series
                    game = checkpoint.game
                    pairing_count = len(checkpoint.schedule)
                else:
                    if runner_lease is not None:
                        raise StorageError("finalized checkpoint still has a runner lease")
                    if loaded_tournament is None:
                        raise StorageError("finalized checkpoint has no formal archive")
                    tournament, rated, leaderboard_replay_complete = loaded_tournament
                    completed_series = tuple(pairing.series for pairing in tournament.pairings)
                    game = tournament.game
                    pairing_count = len(tournament.pairings)
            else:
                if runner_lease is not None:
                    raise StorageError("runner lease has no checkpoint")
                if loaded_tournament is None:
                    raise StorageError("formal tournament disappeared during audit")
                tournament, rated, leaderboard_replay_complete = loaded_tournament
                state = "finalized"
                checkpoint_present = False
                completed_series = tuple(pairing.series for pairing in tournament.pairings)
                game = tournament.game
                pairing_count = len(tournament.pairings)

            technical_losses = sum(
                standing.technical_losses
                for series in completed_series
                for standing in series.standings.values()
            )
            runner_available = True
            if (
                state == "in_progress"
                and runner_lease is not None
                and runner_lease.token_digest is not None
            ):
                now = verifier._database_epoch(connection)
                runner_available = runner_lease.expires_at_epoch <= now
            report = TournamentAuditReport(
                tournament_id=tournament_id,
                state=state,
                game=game,
                completed_pairings=len(completed_series),
                pairing_count=pairing_count,
                technical_losses=technical_losses,
                rated=rated,
                resumable=state == "in_progress" and runner_available,
                checkpoint_present=checkpoint_present,
                leaderboard_replay_complete=leaderboard_replay_complete,
            )
        except TournamentAuditError:
            raise
        except (KeyError, TypeError, ValueError, StorageError) as exc:
            raise TournamentAuditError("tournament_inconsistent") from exc
    except TournamentAuditError as exc:
        audit_error = exc
    except (sqlite3.Error, OSError, ValueError) as exc:
        audit_error = TournamentAuditError("database_invalid")
        audit_error.__cause__ = exc
    finally:
        connection.close()

    try:
        after_stat = resolved.stat()
        active_sidecar = any(sidecar.exists() for sidecar in journal_paths)
    except (OSError, ValueError) as exc:
        raise TournamentAuditError("database_active_writer") from exc
    changed = (
        database_stat.st_dev,
        database_stat.st_ino,
        database_stat.st_size,
        database_stat.st_mtime_ns,
    ) != (
        after_stat.st_dev,
        after_stat.st_ino,
        after_stat.st_size,
        after_stat.st_mtime_ns,
    )
    if changed or active_sidecar:
        raise TournamentAuditError("database_active_writer")
    if audit_error is not None:
        raise audit_error
    if report is None:  # pragma: no cover - defensive exhaustiveness guard
        raise TournamentAuditError("database_invalid")
    return report

__all__ = (
    'DEFAULT_TOURNAMENT_RUNNER_LEASE_SECONDS',
    'SCHEMA_VERSION',
    'SQLITE_INT_MAX',
    'SQLITE_INT_MIN',
    'BudgetPoisonedError',
    'DatabaseInspection',
    'MatchIdCollisionError',
    'MatchSummary',
    'ProviderBudgetCollisionError',
    'ProviderBudgetPendingError',
    'ProviderCallAttemptCollisionError',
    'RatingChange',
    'RatingEntry',
    'SQLiteStore',
    'SQLiteUsageReservation',
    'SaveResult',
    'SeriesIdCollisionError',
    'StorageError',
    'TournamentAuditError',
    'TournamentAuditReport',
    'TournamentCheckpointCollisionError',
    'TournamentIdCollisionError',
    'TournamentRunnerLease',
    'TournamentRunnerLeaseBusyError',
    'TournamentRunnerLeaseLostError',
    'TournamentSaveResult',
    'UnsupportedSchemaError',
    'audit_tournament',
    'database_path',
    'inspect_database',
)
