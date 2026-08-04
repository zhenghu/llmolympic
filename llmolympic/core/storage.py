"""SQLite persistence for match archives and ELO ratings.

The complete :class:`~llmolympic.core.archive.MatchArchive` JSON is the
canonical record.  A small amount of metadata is stored alongside it for fast
history and leaderboard queries.  Saving a match and updating both the
per-game and overall ELO tables happens in one transaction.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import warnings
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from llmolympic.config import get as cfg_get
from llmolympic.core.archive import (
    MatchArchive,
    legacy_entrant_id,
    normalize_player_descriptors,
    validate_entrant_id,
)
from llmolympic.core.elo import DEFAULT_RATING, K_FACTOR, expected_score, update_ratings
from llmolympic.core.events import EventType
from llmolympic.core.series import SERIES_SCHEMA_VERSION, SeriesArchive, head_to_head_point
from llmolympic.core.tournament import (
    TOURNAMENT_CHECKPOINT_SCHEMA_VERSION,
    TOURNAMENT_SCHEMA_VERSION,
    TournamentArchive,
    TournamentCheckpoint,
    tournament_from_series,
)

SCHEMA_VERSION = 6
RatingSource = Literal["engine", "imported"]
SQLITE_INT_MIN = -(2**63)
SQLITE_INT_MAX = 2**63 - 1
MAX_QUERY_LIMIT = 1000
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_SQLITE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
_SAFE_PROFILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_RUNNER_LEASE_TOKEN_RE = re.compile(r"[0-9a-f]{64}\Z")
DEFAULT_TOURNAMENT_RUNNER_LEASE_SECONDS = 60
MAX_TOURNAMENT_RUNNER_LEASE_SECONDS = 86_400
_IDENTITY_SAMPLING_KEYS = frozenset(
    {
        "frequency_penalty",
        "max_completion_tokens",
        "max_tokens",
        "n",
        "num_predict",
        "presence_penalty",
        "seed",
        "temperature",
        "top_p",
    }
)
_SENSITIVE_DESCRIPTOR_KEYS = frozenset(
    {
        "accesstoken",
        "apikey",
        "apikeyenv",
        "auth",
        "authorization",
        "authtoken",
        "baseurl",
        "bearertoken",
        "clientsecret",
        "credential",
        "credentials",
        "endpoint",
        "password",
        "passwordenv",
        "refreshtoken",
        "secret",
        "secretkey",
        "serverurl",
        "token",
    }
)
_SENSITIVE_DESCRIPTOR_SUFFIXES = (
    "accesstoken",
    "accesstokenenv",
    "apikey",
    "apikeyenv",
    "authheader",
    "authtoken",
    "authtokenenv",
    "baseurl",
    "bearertoken",
    "bearertokenenv",
    "clientsecret",
    "credential",
    "credentials",
    "endpoint",
    "password",
    "passwordenv",
    "refreshtoken",
    "refreshtokenenv",
    "secret",
    "secretkey",
    "serverurl",
)
_SERIES_ARCHIVE_FIELDS = frozenset(
    {
        "schema_version",
        "source",
        "series_id",
        "game",
        "seed",
        "players",
        "legs",
        "points",
        "started_at",
        "finished_at",
    }
)

_V3_REQUIRED_COLUMNS = {
    "matches": {
        "match_id",
        "schema_version",
        "game",
        "seed",
        "players_json",
        "scores_json",
        "started_at",
        "finished_at",
        "archive_source",
        "rating_source",
        "rated",
        "rating_policy",
        "archive_json",
    },
    "match_players": {
        "match_id",
        "position",
        "player",
        "entrant_id",
        "display_name",
        "descriptor_json",
        "score",
    },
    "ratings": {
        "rating_scope",
        "game",
        "entrant_id",
        "rating",
        "games_played",
        "wins",
        "draws",
        "losses",
        "updated_at",
    },
    "rating_history": {
        "match_id",
        "rating_scope",
        "game",
        "entrant_id",
        "display_name",
        "opponent_entrant_id",
        "opponent_display_name",
        "outcome",
        "rating_before",
        "rating_after",
        "created_at",
    },
    "series_archives": {
        "series_id",
        "schema_version",
        "game",
        "seed",
        "players_json",
        "points_json",
        "rating_policy",
        "started_at",
        "finished_at",
        "archive_source",
        "rating_source",
        "rated",
        "series_json",
    },
    "series_matches": {"series_id", "leg_number", "match_id"},
    "entrants": {
        "entrant_id",
        "display_name",
        "identity_json",
        "created_at",
        "updated_at",
    },
}

_V4_REQUIRED_COLUMNS = {
    **_V3_REQUIRED_COLUMNS,
    "tournament_archives": {
        "tournament_id",
        "schema_version",
        "format",
        "pairing_policy",
        "seed_policy",
        "game",
        "seed",
        "players_json",
        "points_json",
        "pairing_count",
        "rating_policy",
        "k_factor",
        "started_at",
        "finished_at",
        "archive_source",
        "rating_source",
        "rated",
        "tournament_json",
    },
    "tournament_entrants": {
        "tournament_id",
        "position",
        "entrant_id",
        "display_name",
        "descriptor_json",
        "points",
        "series_played",
        "series_wins",
        "series_draws",
        "series_losses",
        "games_played",
        "wins",
        "draws",
        "losses",
        "technical_losses",
    },
    "tournament_pairings": {
        "tournament_id",
        "pairing_number",
        "series_id",
        "entrant_a_id",
        "entrant_b_id",
    },
    "tournament_rating_snapshots": {
        "tournament_id",
        "rating_scope",
        "game",
        "entrant_id",
        "display_name",
        "rating_before",
        "rating_after",
        "games_added",
        "wins_added",
        "draws_added",
        "losses_added",
    },
    "tournament_rating_contributions": {
        "tournament_id",
        "sequence",
        "match_id",
        "rating_scope",
        "game",
        "entrant_id",
        "opponent_entrant_id",
        "frozen_rating",
        "opponent_frozen_rating",
        "expected_score",
        "rating_delta",
    },
}

_V5_REQUIRED_COLUMNS = {
    **_V4_REQUIRED_COLUMNS,
    "tournament_checkpoints": {
        "tournament_id",
        "schema_version",
        "source",
        "format",
        "pairing_policy",
        "seed_policy",
        "game",
        "seed",
        "players_json",
        "game_config_json",
        "schedule_json",
        "max_attempts",
        "pairing_count",
        "created_at",
        "updated_at",
        "status",
        "finalized_at",
        "final_tournament_id",
        "config_json",
    },
    "tournament_checkpoint_series": {
        "tournament_id",
        "pairing_number",
        "series_id",
        "match_1_id",
        "match_2_id",
        "completed_at",
        "series_json",
    },
}

_REQUIRED_COLUMNS = {
    **_V5_REQUIRED_COLUMNS,
    "tournament_runner_leases": {
        "tournament_id",
        "generation",
        "token_digest",
        "acquired_at_epoch",
        "renewed_at_epoch",
        "expires_at_epoch",
    },
}

_LEGACY_REQUIRED_COLUMNS = {
    "matches": {
        "match_id",
        "schema_version",
        "game",
        "seed",
        "players_json",
        "scores_json",
        "started_at",
        "finished_at",
        "archive_json",
    },
    "match_players": {
        "match_id",
        "position",
        "player",
        "descriptor_json",
        "score",
    },
    "ratings": {
        "rating_scope",
        "game",
        "player",
        "rating",
        "games_played",
        "wins",
        "draws",
        "losses",
        "updated_at",
    },
    "rating_history": {
        "match_id",
        "rating_scope",
        "game",
        "player",
        "opponent",
        "outcome",
        "rating_before",
        "rating_after",
        "created_at",
    },
}

_LEGACY_SERIES_REQUIRED_COLUMNS = {
    "series_archives": {
        "series_id",
        "schema_version",
        "game",
        "seed",
        "players_json",
        "points_json",
        "rating_policy",
        "started_at",
        "finished_at",
        "series_json",
    },
    "series_matches": {"series_id", "leg_number", "match_id"},
}


class StorageError(RuntimeError):
    """Base exception for persistence failures."""


class MatchIdCollisionError(StorageError):
    """A match id is already attached to a different archive."""


class SeriesIdCollisionError(StorageError):
    """A series id is already attached to a different archive."""


class TournamentIdCollisionError(StorageError):
    """A tournament id is already attached to a different archive."""


class TournamentCheckpointCollisionError(StorageError):
    """A tournament checkpoint id is attached to different configuration or progress."""


class TournamentRunnerLeaseError(StorageError):
    """Base exception for runner lease coordination failures."""


class TournamentRunnerLeaseBusyError(TournamentRunnerLeaseError):
    """A different runner currently owns an unexpired tournament lease."""


class TournamentRunnerLeaseLostError(TournamentRunnerLeaseError):
    """A runner no longer owns the active fencing generation."""


class UnsupportedSchemaError(StorageError):
    """The database was created by a newer, unsupported schema version."""


class TournamentAuditError(StorageError):
    """A stable, disclosure-safe failure from strict tournament auditing."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RatingChange:
    """One player's ELO movement caused by a persisted match."""

    player: str
    opponent: str
    game: str | None
    outcome: float
    before: float
    after: float
    entrant_id: str = ""
    opponent_entrant_id: str = ""

    @property
    def display_name(self) -> str:
        return self.player

    @property
    def opponent_display_name(self) -> str:
        return self.opponent


@dataclass(frozen=True)
class SaveResult:
    """Result of saving an archive."""

    inserted: bool
    rated: bool
    rating_changes: tuple[RatingChange, ...] = ()


@dataclass(frozen=True)
class TournamentRatingChange:
    """One entrant's aggregate ELO movement across a complete tournament."""

    entrant_id: str
    display_name: str
    game: str | None
    before: float
    after: float
    games_added: int
    wins_added: int
    draws_added: int
    losses_added: int


@dataclass(frozen=True)
class TournamentSaveResult:
    """Result of atomically saving a complete round-robin tournament."""

    inserted: bool
    rated: bool
    pairing_count: int
    match_count: int
    rating_changes: tuple[TournamentRatingChange, ...] = ()


@dataclass(frozen=True)
class TournamentCheckpointSaveResult:
    """Result of creating or appending one resumable tournament checkpoint."""

    inserted: bool
    completed_pairing_count: int
    pairing_count: int


@dataclass(frozen=True)
class TournamentRunnerLease:
    """Opaque capability and fencing generation for one checkpoint runner."""

    tournament_id: str
    generation: int
    token: str = field(repr=False)
    acquired_at_epoch: int
    renewed_at_epoch: int
    expires_at_epoch: int


@dataclass(frozen=True)
class TournamentRunnerClaim:
    """A checkpoint reloaded under the same transaction that acquired its lease."""

    checkpoint: TournamentCheckpoint
    lease: TournamentRunnerLease


@dataclass(frozen=True)
class RatingEntry:
    player: str
    rating: float
    games_played: int
    wins: int
    draws: int
    losses: int
    updated_at: datetime
    entrant_id: str = ""

    @property
    def display_name(self) -> str:
        return self.player


@dataclass(frozen=True)
class MatchSummary:
    match_id: str
    game: str
    seed: int
    players: tuple[str, ...]
    scores: dict[str, float]
    started_at: datetime
    finished_at: datetime
    series_id: str | None = None
    leg_number: int | None = None
    entrant_ids: tuple[str, ...] = ()
    rating_source: RatingSource = "imported"
    rated: bool = False
    tournament_id: str | None = None
    pairing_number: int | None = None
    pairing_count: int | None = None


@dataclass(frozen=True)
class DatabaseInspection:
    """Read-only database compatibility result used by offline diagnostics."""

    path: Path
    exists: bool
    schema_version: int | None = None
    migration_required: bool = False
    private_permissions: bool = True
    limited_by_active_journal: bool = False


@dataclass(frozen=True)
class TournamentAuditReport:
    """Disclosure-safe result of deeply auditing one tournament."""

    tournament_id: str
    state: Literal["in_progress", "finalized"]
    game: str
    completed_pairings: int
    pairing_count: int
    technical_losses: int
    rated: bool
    resumable: bool
    checkpoint_present: bool
    leaderboard_replay_complete: bool | None


@dataclass(frozen=True)
class _EntrantRef:
    entrant_id: str
    display_name: str
    identity_json: str


@dataclass(frozen=True)
class _TournamentContribution:
    sequence: int
    archive: MatchArchive
    rating_scope: str
    game_key: str
    player: _EntrantRef
    opponent: _EntrantRef
    outcome: float
    frozen_rating: float
    opponent_frozen_rating: float
    expected: float
    delta: float
    before: float
    after: float


@dataclass(frozen=True)
class _TournamentAggregate:
    rating_scope: str
    game_key: str
    player: _EntrantRef
    before: float
    after: float
    outcomes: tuple[float, ...]


@dataclass(frozen=True)
class _TournamentRunnerLeaseState:
    generation: int
    token_digest: bytes | None
    acquired_at_epoch: int | None
    renewed_at_epoch: int | None
    expires_at_epoch: int | None


def database_path(path: str | Path | None = None) -> Path:
    """Resolve the database path from an override, config, or the default.

    Precedence is explicit argument, ``LLMOLYMPIC_DB``,
    ``[storage] database``, then ``~/.llmolympic/llmolympic.db``.
    Relative paths are resolved against the current working directory.
    """

    if path is None:
        configured = cfg_get("storage", "database", env="LLMOLYMPIC_DB")
        path = configured or Path.home() / ".llmolympic" / "llmolympic.db"
    return Path(os.path.expandvars(str(path))).expanduser().resolve()


def _validate_runner_lease_seconds(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_TOURNAMENT_RUNNER_LEASE_SECONDS
    ):
        raise ValueError(
            f"runner lease 秒数必须是 1 到 {MAX_TOURNAMENT_RUNNER_LEASE_SECONDS} 之间的整数"
        )
    return value


def _runner_lease_token_digest(token: str) -> bytes:
    if not isinstance(token, str) or not _RUNNER_LEASE_TOKEN_RE.fullmatch(token):
        raise ValueError("runner lease token 无效")
    return hashlib.sha256(bytes.fromhex(token)).digest()


def _validate_runner_lease_handle(
    lease: TournamentRunnerLease,
    tournament_id: str,
) -> bytes:
    if not isinstance(lease, TournamentRunnerLease):
        raise TypeError("必须提供 TournamentRunnerLease")
    if lease.tournament_id != tournament_id:
        raise ValueError("runner lease 不属于该循环赛")
    if (
        isinstance(lease.generation, bool)
        or not isinstance(lease.generation, int)
        or lease.generation < 1
    ):
        raise ValueError("runner lease generation 无效")
    return _runner_lease_token_digest(lease.token)


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


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise StorageError(f"对局档案包含无法序列化的 JSON 数据：{exc}") from exc


def _set_private_mode(path: Path, mode: int, *, required: bool = False) -> None:
    """Tighten POSIX permissions, failing closed for required database artifacts."""

    if os.name != "posix" or not path.exists():
        return
    try:
        path.chmod(mode)
    except OSError as exc:
        message = f"无法把 {path} 的权限收紧为 {mode:04o}"
        if required:
            raise StorageError(message) from exc
        warnings.warn(message, RuntimeWarning, stacklevel=2)


def _validate_query_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_QUERY_LIMIT:
        raise ValueError(f"limit 必须是 1 到 {MAX_QUERY_LIMIT} 之间的整数")


def _normalized_descriptor_key(key: str) -> str:
    return "".join(character for character in key.casefold() if character.isalnum())


def _is_sensitive_descriptor_key(key: str) -> bool:
    normalized = _normalized_descriptor_key(key)
    return normalized in _SENSITIVE_DESCRIPTOR_KEYS or normalized.endswith(
        _SENSITIVE_DESCRIPTOR_SUFFIXES
    )


def _sensitive_descriptor_path(value: object) -> str | None:
    """Return the first sensitive key path without ever formatting its value."""

    pending: list[tuple[str, object]] = [("descriptor", value)]
    while pending:
        path, candidate = pending.pop()
        if isinstance(candidate, dict):
            for key, nested in candidate.items():
                if not isinstance(key, str):
                    pending.append((f"{path}[*]", nested))
                    continue
                nested_path = f"{path}.{key}"
                if _is_sensitive_descriptor_key(key) and nested != "[REDACTED]":
                    return nested_path
                pending.append((nested_path, nested))
        elif isinstance(candidate, (list, tuple)):
            pending.extend((f"{path}[{index}]", nested) for index, nested in enumerate(candidate))
    return None


class SQLiteStore:
    """Persistent match archive and ELO repository backed by SQLite."""

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
                if locked_version < 4:
                    self._create_tournament_schema(connection)
                if locked_version < 5:
                    self._create_checkpoint_schema(connection)
                if locked_version < 6:
                    self._create_runner_lease_schema(connection)
                self._verify_schema(connection)
                self._verify_foreign_keys(connection)
                if locked_version < SCHEMA_VERSION:
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _verify_legacy_schema(connection: sqlite3.Connection, *, include_series: bool) -> None:
        required_tables = dict(_LEGACY_REQUIRED_COLUMNS)
        if include_series:
            required_tables.update(_LEGACY_SERIES_REQUIRED_COLUMNS)
        for table, required in required_tables.items():
            columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            missing = required - columns
            if missing:
                names = ", ".join(sorted(missing))
                raise StorageError(f"SQLite 数据库结构不完整：{table} 缺少 {names}")

    @staticmethod
    def _create_base_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS entrants (
                entrant_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                identity_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS matches (
                match_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                game TEXT NOT NULL,
                seed INTEGER NOT NULL,
                players_json TEXT NOT NULL,
                scores_json TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                archive_source TEXT NOT NULL
                    CHECK (archive_source IN ('local_engine', 'external', 'legacy')),
                rating_source TEXT NOT NULL
                    CHECK (rating_source IN ('engine', 'imported')),
                rated INTEGER NOT NULL CHECK (rated IN (0, 1)),
                rating_policy TEXT NOT NULL,
                archive_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS matches_finished_at_idx ON matches(finished_at DESC)"
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS matches_game_finished_at_idx
            ON matches(game, finished_at DESC)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS match_players (
                match_id TEXT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                player TEXT NOT NULL,
                entrant_id TEXT NOT NULL REFERENCES entrants(entrant_id) ON DELETE RESTRICT,
                display_name TEXT NOT NULL,
                descriptor_json TEXT NOT NULL,
                score REAL NOT NULL,
                PRIMARY KEY (match_id, position)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS match_players_player_idx
            ON match_players(player, match_id)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS match_players_entrant_idx
            ON match_players(entrant_id, match_id)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ratings (
                rating_scope TEXT NOT NULL CHECK (rating_scope IN ('overall', 'game')),
                game TEXT NOT NULL,
                entrant_id TEXT NOT NULL REFERENCES entrants(entrant_id) ON DELETE RESTRICT,
                rating REAL NOT NULL,
                games_played INTEGER NOT NULL,
                wins INTEGER NOT NULL,
                draws INTEGER NOT NULL,
                losses INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (rating_scope, game, entrant_id)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS ratings_leaderboard_idx
            ON ratings(rating_scope, game, rating DESC, games_played DESC, entrant_id)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rating_history (
                match_id TEXT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
                rating_scope TEXT NOT NULL CHECK (rating_scope IN ('overall', 'game')),
                game TEXT NOT NULL,
                entrant_id TEXT NOT NULL REFERENCES entrants(entrant_id) ON DELETE RESTRICT,
                display_name TEXT NOT NULL,
                opponent_entrant_id TEXT NOT NULL
                    REFERENCES entrants(entrant_id) ON DELETE RESTRICT,
                opponent_display_name TEXT NOT NULL,
                outcome REAL NOT NULL CHECK (outcome IN (0.0, 0.5, 1.0)),
                rating_before REAL NOT NULL,
                rating_after REAL NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (match_id, rating_scope, game, entrant_id)
            )
            """
        )

    @staticmethod
    def _create_series_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS series_archives (
                series_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                game TEXT NOT NULL,
                seed INTEGER NOT NULL,
                players_json TEXT NOT NULL,
                points_json TEXT NOT NULL,
                rating_policy TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                archive_source TEXT NOT NULL
                    CHECK (archive_source IN ('local_engine', 'external', 'legacy')),
                rating_source TEXT NOT NULL
                    CHECK (rating_source IN ('engine', 'imported')),
                rated INTEGER NOT NULL CHECK (rated IN (0, 1)),
                series_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS series_archives_finished_at_idx
            ON series_archives(finished_at DESC)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS series_archives_game_finished_at_idx
            ON series_archives(game, finished_at DESC)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS series_matches (
                series_id TEXT NOT NULL
                    REFERENCES series_archives(series_id) ON DELETE CASCADE,
                leg_number INTEGER NOT NULL CHECK (leg_number IN (1, 2)),
                match_id TEXT NOT NULL UNIQUE
                    REFERENCES matches(match_id) ON DELETE RESTRICT,
                PRIMARY KEY (series_id, leg_number)
            )
            """
        )

    @staticmethod
    def _create_tournament_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tournament_archives (
                tournament_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                format TEXT NOT NULL CHECK (format = 'round_robin_two_leg'),
                pairing_policy TEXT NOT NULL,
                seed_policy TEXT NOT NULL,
                game TEXT NOT NULL,
                seed INTEGER NOT NULL,
                players_json TEXT NOT NULL,
                points_json TEXT NOT NULL,
                pairing_count INTEGER NOT NULL CHECK (pairing_count >= 1),
                rating_policy TEXT NOT NULL
                    CHECK (rating_policy IN ('unrated', 'elo_tournament_batch_v1')),
                k_factor REAL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                archive_source TEXT NOT NULL
                    CHECK (archive_source IN ('local_engine', 'external')),
                rating_source TEXT NOT NULL
                    CHECK (rating_source IN ('engine', 'imported')),
                rated INTEGER NOT NULL CHECK (rated IN (0, 1)),
                tournament_json TEXT NOT NULL,
                CHECK (
                    (rated = 0 AND rating_policy = 'unrated' AND k_factor IS NULL)
                    OR
                    (rated = 1 AND rating_policy = 'elo_tournament_batch_v1'
                     AND k_factor IS NOT NULL AND k_factor > 0)
                )
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS tournament_archives_finished_at_idx
            ON tournament_archives(finished_at DESC)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS tournament_archives_game_finished_at_idx
            ON tournament_archives(game, finished_at DESC)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tournament_entrants (
                tournament_id TEXT NOT NULL
                    REFERENCES tournament_archives(tournament_id) ON DELETE RESTRICT,
                position INTEGER NOT NULL CHECK (position >= 0),
                entrant_id TEXT NOT NULL REFERENCES entrants(entrant_id) ON DELETE RESTRICT,
                display_name TEXT NOT NULL,
                descriptor_json TEXT NOT NULL,
                points REAL NOT NULL,
                series_played INTEGER NOT NULL CHECK (series_played >= 0),
                series_wins INTEGER NOT NULL CHECK (series_wins >= 0),
                series_draws INTEGER NOT NULL CHECK (series_draws >= 0),
                series_losses INTEGER NOT NULL CHECK (series_losses >= 0),
                games_played INTEGER NOT NULL CHECK (games_played >= 0),
                wins INTEGER NOT NULL CHECK (wins >= 0),
                draws INTEGER NOT NULL CHECK (draws >= 0),
                losses INTEGER NOT NULL CHECK (losses >= 0),
                technical_losses INTEGER NOT NULL CHECK (technical_losses >= 0),
                PRIMARY KEY (tournament_id, position),
                UNIQUE (tournament_id, entrant_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tournament_pairings (
                tournament_id TEXT NOT NULL
                    REFERENCES tournament_archives(tournament_id) ON DELETE RESTRICT,
                pairing_number INTEGER NOT NULL CHECK (pairing_number >= 1),
                series_id TEXT NOT NULL UNIQUE
                    REFERENCES series_archives(series_id) ON DELETE RESTRICT,
                entrant_a_id TEXT NOT NULL,
                entrant_b_id TEXT NOT NULL,
                PRIMARY KEY (tournament_id, pairing_number),
                UNIQUE (tournament_id, entrant_a_id, entrant_b_id),
                CHECK (entrant_a_id <> entrant_b_id),
                FOREIGN KEY (tournament_id, entrant_a_id)
                    REFERENCES tournament_entrants(tournament_id, entrant_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (tournament_id, entrant_b_id)
                    REFERENCES tournament_entrants(tournament_id, entrant_id)
                    ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tournament_rating_snapshots (
                tournament_id TEXT NOT NULL
                    REFERENCES tournament_archives(tournament_id) ON DELETE RESTRICT,
                rating_scope TEXT NOT NULL CHECK (rating_scope IN ('overall', 'game')),
                game TEXT NOT NULL,
                entrant_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                rating_before REAL NOT NULL,
                rating_after REAL NOT NULL,
                games_added INTEGER NOT NULL CHECK (games_added >= 0),
                wins_added INTEGER NOT NULL CHECK (wins_added >= 0),
                draws_added INTEGER NOT NULL CHECK (draws_added >= 0),
                losses_added INTEGER NOT NULL CHECK (losses_added >= 0),
                PRIMARY KEY (tournament_id, rating_scope, game, entrant_id),
                FOREIGN KEY (tournament_id, entrant_id)
                    REFERENCES tournament_entrants(tournament_id, entrant_id)
                    ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tournament_rating_contributions (
                tournament_id TEXT NOT NULL
                    REFERENCES tournament_archives(tournament_id) ON DELETE RESTRICT,
                sequence INTEGER NOT NULL CHECK (sequence >= 1),
                match_id TEXT NOT NULL,
                rating_scope TEXT NOT NULL CHECK (rating_scope IN ('overall', 'game')),
                game TEXT NOT NULL,
                entrant_id TEXT NOT NULL,
                opponent_entrant_id TEXT NOT NULL,
                frozen_rating REAL NOT NULL,
                opponent_frozen_rating REAL NOT NULL,
                expected_score REAL NOT NULL,
                rating_delta REAL NOT NULL,
                PRIMARY KEY (
                    tournament_id, match_id, rating_scope, game, entrant_id
                ),
                UNIQUE (
                    tournament_id, rating_scope, game, entrant_id, sequence
                ),
                FOREIGN KEY (match_id, rating_scope, game, entrant_id)
                    REFERENCES rating_history(match_id, rating_scope, game, entrant_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (tournament_id, entrant_id)
                    REFERENCES tournament_entrants(tournament_id, entrant_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (tournament_id, opponent_entrant_id)
                    REFERENCES tournament_entrants(tournament_id, entrant_id)
                    ON DELETE RESTRICT
            )
            """
        )

    @staticmethod
    def _create_checkpoint_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tournament_checkpoints (
                tournament_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL CHECK (schema_version = 1),
                source TEXT NOT NULL CHECK (source = 'local_engine'),
                format TEXT NOT NULL CHECK (format = 'round_robin_two_leg'),
                pairing_policy TEXT NOT NULL
                    CHECK (pairing_policy = 'input_order_combinations_v1'),
                seed_policy TEXT NOT NULL
                    CHECK (seed_policy = 'entrant_pair_sha256_v1'),
                game TEXT NOT NULL,
                seed INTEGER NOT NULL,
                players_json TEXT NOT NULL,
                game_config_json TEXT NOT NULL,
                schedule_json TEXT NOT NULL,
                max_attempts INTEGER NOT NULL CHECK (max_attempts >= 1),
                pairing_count INTEGER NOT NULL CHECK (pairing_count >= 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('in_progress', 'finalized')),
                finalized_at TEXT,
                final_tournament_id TEXT UNIQUE
                    REFERENCES tournament_archives(tournament_id) ON DELETE RESTRICT,
                config_json TEXT NOT NULL,
                CHECK (
                    (status = 'in_progress'
                     AND finalized_at IS NULL
                     AND final_tournament_id IS NULL)
                    OR
                    (status = 'finalized'
                     AND finalized_at IS NOT NULL
                     AND final_tournament_id = tournament_id)
                )
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS tournament_checkpoints_updated_at_idx
            ON tournament_checkpoints(status, updated_at DESC)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tournament_checkpoint_series (
                tournament_id TEXT NOT NULL
                    REFERENCES tournament_checkpoints(tournament_id) ON DELETE RESTRICT,
                pairing_number INTEGER NOT NULL CHECK (pairing_number >= 1),
                series_id TEXT NOT NULL UNIQUE,
                match_1_id TEXT NOT NULL UNIQUE,
                match_2_id TEXT NOT NULL UNIQUE,
                completed_at TEXT NOT NULL,
                series_json TEXT NOT NULL,
                PRIMARY KEY (tournament_id, pairing_number),
                CHECK (match_1_id <> match_2_id)
            )
            """
        )

    @staticmethod
    def _create_runner_lease_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tournament_runner_leases (
                tournament_id TEXT PRIMARY KEY
                    REFERENCES tournament_checkpoints(tournament_id) ON DELETE RESTRICT,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                token_digest BLOB UNIQUE,
                acquired_at_epoch INTEGER,
                renewed_at_epoch INTEGER,
                expires_at_epoch INTEGER,
                CHECK (
                    (token_digest IS NULL
                     AND acquired_at_epoch IS NULL
                     AND renewed_at_epoch IS NULL
                     AND expires_at_epoch IS NULL)
                    OR
                    (typeof(token_digest) = 'blob'
                     AND length(token_digest) = 32
                     AND acquired_at_epoch IS NOT NULL
                     AND renewed_at_epoch IS NOT NULL
                     AND expires_at_epoch IS NOT NULL
                     AND acquired_at_epoch <= renewed_at_epoch
                     AND renewed_at_epoch < expires_at_epoch)
                )
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS tournament_runner_leases_expires_at_idx
            ON tournament_runner_leases(expires_at_epoch)
            WHERE token_digest IS NOT NULL
            """
        )

    @staticmethod
    def _migrate_to_v3(connection: sqlite3.Connection, *, include_series: bool) -> None:
        """Move name-keyed ratings to isolated legacy entrant identities atomically."""

        connection.execute(
            """
            CREATE TABLE entrants (
                entrant_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                identity_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute("DROP INDEX IF EXISTS match_players_player_idx")
        connection.execute("ALTER TABLE match_players RENAME TO match_players_v2")
        connection.execute("DROP INDEX IF EXISTS ratings_leaderboard_idx")
        connection.execute("ALTER TABLE ratings RENAME TO ratings_v2")
        connection.execute("ALTER TABLE rating_history RENAME TO rating_history_v2")

        legacy_names = {
            row[0]
            for row in connection.execute(
                """
                SELECT player FROM match_players_v2
                UNION SELECT player FROM ratings_v2
                UNION SELECT player FROM rating_history_v2
                UNION SELECT opponent FROM rating_history_v2
                """
            )
        }
        legacy_timestamp = datetime(1970, 1, 1, tzinfo=UTC).isoformat()
        legacy_identity = _canonical_json({"kind": "legacy"})
        connection.executemany(
            """
            INSERT INTO entrants (
                entrant_id, display_name, identity_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    legacy_entrant_id(name),
                    name,
                    legacy_identity,
                    legacy_timestamp,
                    legacy_timestamp,
                )
                for name in sorted(legacy_names)
            ],
        )

        connection.execute(
            """
            CREATE TABLE match_players (
                match_id TEXT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                player TEXT NOT NULL,
                entrant_id TEXT NOT NULL REFERENCES entrants(entrant_id) ON DELETE RESTRICT,
                display_name TEXT NOT NULL,
                descriptor_json TEXT NOT NULL,
                score REAL NOT NULL,
                PRIMARY KEY (match_id, position)
            )
            """
        )
        for row in connection.execute(
            """
            SELECT match_id, position, player, descriptor_json, score
            FROM match_players_v2
            """
        ).fetchall():
            connection.execute(
                """
                INSERT INTO match_players (
                    match_id, position, player, entrant_id, display_name,
                    descriptor_json, score
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["match_id"],
                    row["position"],
                    row["player"],
                    legacy_entrant_id(row["player"]),
                    row["player"],
                    row["descriptor_json"],
                    row["score"],
                ),
            )
        connection.execute("DROP TABLE match_players_v2")
        connection.execute(
            "CREATE INDEX match_players_player_idx ON match_players(player, match_id)"
        )
        connection.execute(
            "CREATE INDEX match_players_entrant_idx ON match_players(entrant_id, match_id)"
        )

        connection.execute(
            """
            CREATE TABLE ratings (
                rating_scope TEXT NOT NULL CHECK (rating_scope IN ('overall', 'game')),
                game TEXT NOT NULL,
                entrant_id TEXT NOT NULL REFERENCES entrants(entrant_id) ON DELETE RESTRICT,
                rating REAL NOT NULL,
                games_played INTEGER NOT NULL,
                wins INTEGER NOT NULL,
                draws INTEGER NOT NULL,
                losses INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (rating_scope, game, entrant_id)
            )
            """
        )
        for row in connection.execute("SELECT * FROM ratings_v2").fetchall():
            connection.execute(
                """
                INSERT INTO ratings (
                    rating_scope, game, entrant_id, rating, games_played,
                    wins, draws, losses, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["rating_scope"],
                    row["game"],
                    legacy_entrant_id(row["player"]),
                    row["rating"],
                    row["games_played"],
                    row["wins"],
                    row["draws"],
                    row["losses"],
                    row["updated_at"],
                ),
            )
        connection.execute("DROP TABLE ratings_v2")
        connection.execute(
            """
            CREATE INDEX ratings_leaderboard_idx
            ON ratings(rating_scope, game, rating DESC, games_played DESC, entrant_id)
            """
        )

        connection.execute(
            """
            CREATE TABLE rating_history (
                match_id TEXT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
                rating_scope TEXT NOT NULL CHECK (rating_scope IN ('overall', 'game')),
                game TEXT NOT NULL,
                entrant_id TEXT NOT NULL REFERENCES entrants(entrant_id) ON DELETE RESTRICT,
                display_name TEXT NOT NULL,
                opponent_entrant_id TEXT NOT NULL
                    REFERENCES entrants(entrant_id) ON DELETE RESTRICT,
                opponent_display_name TEXT NOT NULL,
                outcome REAL NOT NULL CHECK (outcome IN (0.0, 0.5, 1.0)),
                rating_before REAL NOT NULL,
                rating_after REAL NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (match_id, rating_scope, game, entrant_id)
            )
            """
        )
        for row in connection.execute("SELECT * FROM rating_history_v2").fetchall():
            connection.execute(
                """
                INSERT INTO rating_history (
                    match_id, rating_scope, game, entrant_id, display_name,
                    opponent_entrant_id, opponent_display_name, outcome,
                    rating_before, rating_after, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["match_id"],
                    row["rating_scope"],
                    row["game"],
                    legacy_entrant_id(row["player"]),
                    row["player"],
                    legacy_entrant_id(row["opponent"]),
                    row["opponent"],
                    row["outcome"],
                    row["rating_before"],
                    row["rating_after"],
                    row["created_at"],
                ),
            )

        connection.execute(
            "ALTER TABLE matches ADD COLUMN archive_source TEXT NOT NULL DEFAULT 'legacy'"
        )
        connection.execute(
            "ALTER TABLE matches ADD COLUMN rating_source TEXT NOT NULL DEFAULT 'imported'"
        )
        connection.execute("ALTER TABLE matches ADD COLUMN rated INTEGER NOT NULL DEFAULT 0")
        connection.execute(
            "ALTER TABLE matches ADD COLUMN rating_policy TEXT NOT NULL DEFAULT 'unrated'"
        )
        connection.execute(
            """
            UPDATE matches
            SET rated = EXISTS (
                    SELECT 1 FROM rating_history_v2 AS rh WHERE rh.match_id = matches.match_id
                ),
                rating_source = CASE
                    WHEN EXISTS (
                        SELECT 1 FROM rating_history_v2 AS rh
                        WHERE rh.match_id = matches.match_id
                    ) THEN 'engine'
                    ELSE 'imported'
                END,
                rating_policy = CASE
                    WHEN EXISTS (
                        SELECT 1 FROM rating_history_v2 AS rh
                        WHERE rh.match_id = matches.match_id
                    ) THEN 'elo_v1'
                    ELSE 'unrated'
                END
            """
        )

        if include_series:
            connection.execute(
                """
                ALTER TABLE series_archives
                ADD COLUMN archive_source TEXT NOT NULL DEFAULT 'legacy'
                """
            )
            connection.execute(
                """
                ALTER TABLE series_archives
                ADD COLUMN rating_source TEXT NOT NULL DEFAULT 'imported'
                """
            )
            connection.execute(
                "ALTER TABLE series_archives ADD COLUMN rated INTEGER NOT NULL DEFAULT 0"
            )
            connection.execute(
                """
                UPDATE series_archives
                SET rated = EXISTS (
                    SELECT 1
                    FROM series_matches AS sm
                    JOIN rating_history_v2 AS rh ON rh.match_id = sm.match_id
                    WHERE sm.series_id = series_archives.series_id
                ),
                    rating_source = CASE
                        WHEN EXISTS (
                            SELECT 1
                            FROM series_matches AS sm
                            JOIN rating_history_v2 AS rh ON rh.match_id = sm.match_id
                            WHERE sm.series_id = series_archives.series_id
                        ) THEN 'engine'
                        ELSE 'imported'
                    END
                """
            )
            connection.execute(
                """
                UPDATE matches
                SET rating_source = 'engine', rated = 1, rating_policy = 'elo_batch_v1'
                WHERE match_id IN (
                    SELECT sm.match_id
                    FROM series_matches AS sm
                    JOIN series_archives AS sa ON sa.series_id = sm.series_id
                    WHERE sa.rated = 1
                )
                """
            )
        connection.execute("DROP TABLE rating_history_v2")

    @staticmethod
    def _verify_required_columns(
        connection: sqlite3.Connection,
        required_columns: dict[str, set[str]],
    ) -> None:
        for table, required in required_columns.items():
            columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            missing = required - columns
            if missing:
                names = ", ".join(sorted(missing))
                raise StorageError(f"SQLite 数据库结构不完整：{table} 缺少 {names}")

    @staticmethod
    def _verify_v3_schema(connection: sqlite3.Connection) -> None:
        SQLiteStore._verify_required_columns(connection, _V3_REQUIRED_COLUMNS)

    @staticmethod
    def _verify_v4_schema(connection: sqlite3.Connection) -> None:
        SQLiteStore._verify_required_columns(connection, _V4_REQUIRED_COLUMNS)

    @staticmethod
    def _verify_v5_schema(connection: sqlite3.Connection) -> None:
        SQLiteStore._verify_required_columns(connection, _V5_REQUIRED_COLUMNS)

    @staticmethod
    def _verify_runner_lease_schema(connection: sqlite3.Connection) -> None:
        table_info = connection.execute("PRAGMA table_info(tournament_runner_leases)").fetchall()
        primary_key = [row["name"] for row in table_info if row["pk"]]
        if primary_key != ["tournament_id"]:
            raise StorageError("SQLite 数据库结构不完整：tournament_runner_leases 主键无效")

        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(tournament_runner_leases)"
        ).fetchall()
        if not any(
            row["table"] == "tournament_checkpoints"
            and row["from"] == "tournament_id"
            and row["to"] == "tournament_id"
            and row["on_delete"].upper() == "RESTRICT"
            for row in foreign_keys
        ):
            raise StorageError("SQLite 数据库结构不完整：tournament_runner_leases 外键无效")

        has_unique_token = False
        for index in connection.execute("PRAGMA index_list(tournament_runner_leases)").fetchall():
            if not index["unique"] or index["partial"]:
                continue
            columns = [
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                    (index["name"],),
                ).fetchall()
            ]
            if columns == ["token_digest"]:
                has_unique_token = True
                break
        if not has_unique_token:
            raise StorageError(
                "SQLite 数据库结构不完整：tournament_runner_leases 缺少 token 唯一约束"
            )

    @staticmethod
    def _verify_schema(connection: sqlite3.Connection) -> None:
        SQLiteStore._verify_required_columns(connection, _REQUIRED_COLUMNS)
        SQLiteStore._verify_runner_lease_schema(connection)

    @staticmethod
    def _verify_foreign_keys(connection: sqlite3.Connection) -> None:
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise StorageError("SQLite 数据库外键完整性检查失败")

    @staticmethod
    def _validate_rating_source(rating_source: object) -> RatingSource:
        if rating_source not in ("engine", "imported"):
            raise ValueError("rating_source 必须是 'engine' 或 'imported'")
        return rating_source

    @staticmethod
    def _entrant_ref(descriptor: dict, *, legacy: bool) -> _EntrantRef:
        try:
            entrant_id = validate_entrant_id(descriptor.get("entrant_id"))
        except ValueError as exc:
            raise StorageError(str(exc)) from exc
        display_name = descriptor.get("display_name")
        if not isinstance(display_name, str) or not display_name:
            raise StorageError("选手描述必须包含非空 display_name")
        if descriptor.get("name") != display_name:
            raise StorageError("选手描述的 display_name 必须与 name 一致")
        if legacy:
            expected_id = legacy_entrant_id(display_name)
            if entrant_id != expected_id:
                raise StorageError("legacy entrant_id 与历史选手名称不一致")
            identity = {"kind": "legacy"}
        else:
            if entrant_id.startswith("legacy:"):
                raise StorageError("新档案不能声明保留的 legacy entrant_id")
            sensitive_path = _sensitive_descriptor_path(descriptor)
            if sensitive_path is not None:
                raise StorageError(f"选手描述不能包含凭据或连接端点字段：{sensitive_path}")
            kind = descriptor.get("kind")
            if not isinstance(kind, str) or not kind:
                raise StorageError("选手描述必须包含非空 kind")
            identity = {"kind": kind}
            for key in ("profile_id", "provider", "model"):
                if key not in descriptor:
                    continue
                value = descriptor[key]
                if not isinstance(value, str) or not value:
                    raise StorageError(f"选手描述的 {key} 必须是非空字符串")
                if key == "profile_id" and _SAFE_PROFILE_ID.fullmatch(value) is None:
                    raise StorageError("选手描述的 profile_id 格式无效")
                identity[key] = value
            if "sampling_params" in descriptor:
                sampling_params = descriptor["sampling_params"]
                if not isinstance(sampling_params, dict):
                    raise StorageError("选手描述的 sampling_params 必须是对象")
                safe_sampling_params: dict[str, object] = {}
                for key, value in sampling_params.items():
                    if not isinstance(key, str):
                        raise StorageError("选手描述的 sampling_params 键必须是字符串")
                    if key not in _IDENTITY_SAMPLING_KEYS:
                        if value != "[REDACTED]":
                            raise StorageError(f"选手描述的 sampling_params.{key} 必须已脱敏")
                        continue
                    if value == "[REDACTED]":
                        continue
                    if value is not None and not isinstance(value, (bool, int, float)):
                        raise StorageError(f"选手描述的 sampling_params.{key} 必须是标量")
                    if isinstance(value, float) and not math.isfinite(value):
                        raise StorageError(f"选手描述的 sampling_params.{key} 必须是有限数值")
                    safe_sampling_params[key] = value
                identity["sampling_params"] = safe_sampling_params
        return _EntrantRef(
            entrant_id=entrant_id,
            display_name=display_name,
            identity_json=_canonical_json(identity),
        )

    @staticmethod
    def _has_trusted_entrant_observation(
        connection: sqlite3.Connection,
        entrant_id: str,
    ) -> bool:
        return bool(
            connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM match_players AS mp
                    JOIN matches AS m ON m.match_id = mp.match_id
                    WHERE mp.entrant_id = ?
                      AND m.schema_version = 2
                      AND m.archive_source = 'local_engine'
                      AND m.rating_source = 'engine'
                )
                """,
                (entrant_id,),
            ).fetchone()[0]
        )

    def _verify_checkpoint_entrant_bindings(
        self,
        connection: sqlite3.Connection,
        checkpoint: TournamentCheckpoint,
    ) -> None:
        """Reject checkpoints that cannot become trusted tournament entrants."""

        for descriptor in checkpoint.players:
            entrant = self._entrant_ref(descriptor, legacy=False)
            existing = connection.execute(
                """
                SELECT identity_json, updated_at
                FROM entrants WHERE entrant_id = ?
                """,
                (entrant.entrant_id,),
            ).fetchone()
            if existing is None:
                continue
            try:
                observed_at = datetime.fromisoformat(existing["updated_at"])
            except (TypeError, ValueError) as exc:
                raise StorageError(f"entrant_id {entrant.entrant_id!r} 的观察时间已损坏") from exc
            if observed_at.utcoffset() is None:
                raise StorageError(f"entrant_id {entrant.entrant_id!r} 的观察时间已损坏")
            if (
                self._has_trusted_entrant_observation(connection, entrant.entrant_id)
                and existing["identity_json"] != entrant.identity_json
            ):
                raise StorageError(f"entrant_id {entrant.entrant_id!r} 已绑定到另一份身份元数据")

    @staticmethod
    def _upsert_entrant(
        connection: sqlite3.Connection,
        entrant: _EntrantRef,
        *,
        observed_at: datetime,
        trusted_engine: bool,
    ) -> None:
        timestamp = observed_at.astimezone(UTC).isoformat()
        existing = connection.execute(
            """
            SELECT display_name, identity_json, updated_at
            FROM entrants WHERE entrant_id = ?
            """,
            (entrant.entrant_id,),
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO entrants (
                    entrant_id, display_name, identity_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    entrant.entrant_id,
                    entrant.display_name,
                    entrant.identity_json,
                    timestamp,
                    timestamp,
                ),
            )
            return
        try:
            existing_observed_at = datetime.fromisoformat(existing["updated_at"])
        except (TypeError, ValueError) as exc:
            raise StorageError(f"entrant_id {entrant.entrant_id!r} 的观察时间已损坏") from exc
        if existing_observed_at.utcoffset() is None:
            raise StorageError(f"entrant_id {entrant.entrant_id!r} 的观察时间已损坏")
        is_newer_observation = observed_at.astimezone(UTC) > existing_observed_at.astimezone(UTC)
        has_trusted_observation = SQLiteStore._has_trusted_entrant_observation(
            connection,
            entrant.entrant_id,
        )
        if trusted_engine and not has_trusted_observation:
            # The first trusted engine observation establishes both identity and
            # presentation. An imported archive may carry a forged future
            # timestamp, so it must not be able to pin either one.
            connection.execute(
                """
                UPDATE entrants
                SET display_name = ?, identity_json = ?, updated_at = ?
                WHERE entrant_id = ?
                """,
                (
                    entrant.display_name,
                    entrant.identity_json,
                    timestamp,
                    entrant.entrant_id,
                ),
            )
            return
        if existing["identity_json"] != entrant.identity_json:
            raise StorageError(f"entrant_id {entrant.entrant_id!r} 已绑定到另一份身份元数据")
        if (
            trusted_engine
            and is_newer_observation
            and existing["display_name"] != entrant.display_name
        ):
            connection.execute(
                """
                UPDATE entrants SET display_name = ?, updated_at = ?
                WHERE entrant_id = ?
                """,
                (entrant.display_name, timestamp, entrant.entrant_id),
            )

    @staticmethod
    def _semantic_match_json(raw_json: str) -> str:
        try:
            archive = MatchArchive.model_validate_json(raw_json)
        except (TypeError, ValueError) as exc:
            raise StorageError("数据库中的对局档案 JSON 已损坏") from exc
        return _canonical_json(archive.model_dump(mode="json"))

    @staticmethod
    def _semantic_series_json(raw_json: str) -> str:
        try:
            series = SeriesArchive.model_validate_json(raw_json)
        except (TypeError, ValueError) as exc:
            raise StorageError("数据库中的系列赛档案 JSON 已损坏") from exc
        return _canonical_json(series.model_dump(mode="json"))

    @staticmethod
    def _semantic_tournament_json(raw_json: str) -> str:
        try:
            tournament = TournamentArchive.model_validate_json(raw_json)
        except (TypeError, ValueError) as exc:
            raise StorageError("数据库中的循环赛档案 JSON 已损坏") from exc
        return _canonical_json(tournament.model_dump(mode="json"))

    @staticmethod
    def _checkpoint_config_payload(checkpoint: TournamentCheckpoint) -> dict:
        payload = checkpoint.model_dump(mode="json")
        payload.pop("completed_series")
        payload.pop("updated_at")
        return payload

    @staticmethod
    def _semantic_checkpoint_config_json(raw_json: str) -> str:
        try:
            payload = json.loads(raw_json)
        except (TypeError, ValueError) as exc:
            raise StorageError("数据库中的循环赛 checkpoint 配置 JSON 已损坏") from exc
        if (
            not isinstance(payload, dict)
            or "completed_series" in payload
            or "updated_at" in payload
        ):
            raise StorageError("数据库中的循环赛 checkpoint 配置 JSON 已损坏")
        return _canonical_json(payload)

    @staticmethod
    def _semantic_descriptor_json(raw_json: str, *, legacy: bool) -> str:
        try:
            descriptor = json.loads(raw_json)
            normalized = normalize_player_descriptors([descriptor], legacy=legacy)[0]
        except (TypeError, ValueError) as exc:
            raise StorageError("数据库中的选手描述 JSON 已损坏") from exc
        return _canonical_json(normalized)

    @staticmethod
    def _semantic_json_column(raw_json: object) -> str:
        if not isinstance(raw_json, str):
            raise StorageError("数据库中的 JSON 元数据已损坏")
        try:
            value = json.loads(raw_json)
        except (TypeError, ValueError) as exc:
            raise StorageError("数据库中的 JSON 元数据已损坏") from exc
        return _canonical_json(value)

    @staticmethod
    def _semantic_players_json(raw_json: object, *, legacy: bool) -> str:
        if not isinstance(raw_json, str):
            raise StorageError("数据库中的选手 JSON 元数据已损坏")
        try:
            players = normalize_player_descriptors(json.loads(raw_json), legacy=legacy)
        except (TypeError, ValueError) as exc:
            raise StorageError("数据库中的选手 JSON 元数据已损坏") from exc
        return _canonical_json(players)

    @staticmethod
    def _timestamp_matches(raw_timestamp: object, expected: datetime) -> bool:
        if not isinstance(raw_timestamp, str):
            return False
        try:
            observed = datetime.fromisoformat(raw_timestamp)
        except ValueError:
            return False
        return observed.utcoffset() is not None and observed.astimezone(UTC) == expected.astimezone(
            UTC
        )

    @staticmethod
    def _finite_database_float(value: object) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite SQLite number")
        return number

    def _verify_match_metadata(
        self,
        row: sqlite3.Row,
        archive: MatchArchive,
        *,
        rating_source: str,
        rated: bool,
        rating_policy: str,
    ) -> None:
        payload = archive.model_dump(mode="json")
        try:
            stored_players = self._semantic_players_json(
                row["players_json"], legacy=archive.schema_version == 1
            )
            stored_scores = self._semantic_json_column(row["scores_json"])
        except StorageError as exc:
            raise StorageError(
                f"数据库中 match_id {archive.match_id!r} 的反规范化元数据已损坏"
            ) from exc
        if (
            row["match_id"] != archive.match_id
            or row["schema_version"] != archive.schema_version
            or row["game"] != archive.game
            or row["seed"] != archive.seed
            or stored_players != _canonical_json(payload["players"])
            or stored_scores != _canonical_json(payload["scores"])
            or not self._timestamp_matches(row["started_at"], archive.started_at)
            or not self._timestamp_matches(row["finished_at"], archive.finished_at)
            or row["archive_source"] != archive.source
            or row["rating_source"] != rating_source
            or row["rated"] != int(rated)
            or row["rating_policy"] != rating_policy
        ):
            raise StorageError(f"数据库中 match_id {archive.match_id!r} 的反规范化元数据已损坏")

    def _verify_series_metadata(
        self,
        row: sqlite3.Row,
        series: SeriesArchive,
        *,
        rating_source: str,
        rated: bool,
        rating_policy: str,
    ) -> None:
        payload = series.model_dump(mode="json")
        try:
            stored_players = self._semantic_players_json(
                row["players_json"], legacy=series.schema_version == 1
            )
            stored_points = self._semantic_json_column(row["points_json"])
        except StorageError as exc:
            raise StorageError(
                f"数据库中 series_id {series.series_id!r} 的反规范化元数据已损坏"
            ) from exc
        if (
            row["series_id"] != series.series_id
            or row["schema_version"] != series.schema_version
            or row["game"] != series.game
            or row["seed"] != series.seed
            or stored_players != _canonical_json(payload["players"])
            or stored_points != _canonical_json(payload["points"])
            or not self._timestamp_matches(row["started_at"], series.started_at)
            or not self._timestamp_matches(row["finished_at"], series.finished_at)
            or row["archive_source"] != series.source
            or row["rating_source"] != rating_source
            or row["rated"] != int(rated)
            or row["rating_policy"] != rating_policy
        ):
            raise StorageError(f"数据库中 series_id {series.series_id!r} 的反规范化元数据已损坏")

    def _verify_tournament_metadata(
        self,
        row: sqlite3.Row,
        tournament: TournamentArchive,
        *,
        rating_source: str,
        rated: bool,
        rating_policy: str,
    ) -> None:
        payload = tournament.model_dump(mode="json")
        try:
            stored_players = self._semantic_players_json(row["players_json"], legacy=False)
            stored_points = self._semantic_json_column(row["points_json"])
            stored_k_factor = (
                None if row["k_factor"] is None else self._finite_database_float(row["k_factor"])
            )
        except (StorageError, TypeError, ValueError) as exc:
            raise StorageError(
                f"数据库中 tournament_id {tournament.tournament_id!r} 的反规范化元数据已损坏"
            ) from exc
        expected_k_factor = K_FACTOR if rated else None
        if (
            row["tournament_id"] != tournament.tournament_id
            or row["schema_version"] != tournament.schema_version
            or row["format"] != tournament.format
            or row["pairing_policy"] != tournament.pairing_policy
            or row["seed_policy"] != tournament.seed_policy
            or row["game"] != tournament.game
            or row["seed"] != tournament.seed
            or stored_players != _canonical_json(payload["players"])
            or stored_points != _canonical_json(payload["points"])
            or row["pairing_count"] != len(tournament.pairings)
            or row["rating_policy"] != rating_policy
            or stored_k_factor != expected_k_factor
            or not self._timestamp_matches(row["started_at"], tournament.started_at)
            or not self._timestamp_matches(row["finished_at"], tournament.finished_at)
            or row["archive_source"] != tournament.source
            or row["rating_source"] != rating_source
            or row["rated"] != int(rated)
        ):
            raise StorageError(
                f"数据库中 tournament_id {tournament.tournament_id!r} 的反规范化元数据已损坏"
            )

    def _verify_match_players(
        self,
        connection: sqlite3.Connection,
        archive: MatchArchive,
        entrants: list[_EntrantRef],
    ) -> None:
        rows = connection.execute(
            """
            SELECT position, player, entrant_id, display_name, descriptor_json, score
            FROM match_players
            WHERE match_id = ?
            ORDER BY position
            """,
            (archive.match_id,),
        ).fetchall()
        payload = archive.model_dump(mode="json")
        if len(rows) != len(entrants) or [row["position"] for row in rows] != list(
            range(len(entrants))
        ):
            raise StorageError(f"数据库中 match_id {archive.match_id!r} 的选手索引已损坏")
        legacy = archive.schema_version == 1
        for row, entrant, descriptor in zip(rows, entrants, payload["players"]):
            try:
                stored_descriptor = self._semantic_descriptor_json(
                    row["descriptor_json"], legacy=legacy
                )
                stored_score = self._finite_database_float(row["score"])
            except (StorageError, TypeError, ValueError) as exc:
                raise StorageError(
                    f"数据库中 match_id {archive.match_id!r} 的选手索引已损坏"
                ) from exc
            if (
                row["player"] != entrant.display_name
                or row["entrant_id"] != entrant.entrant_id
                or row["display_name"] != entrant.display_name
                or stored_descriptor != _canonical_json(descriptor)
                or stored_score != archive.scores[entrant.display_name]
            ):
                raise StorageError(f"数据库中 match_id {archive.match_id!r} 的选手索引已损坏")

    def _verify_match_history(
        self,
        connection: sqlite3.Connection,
        archive: MatchArchive,
        entrants: list[_EntrantRef],
        *,
        rated: bool,
    ) -> None:
        rows = connection.execute(
            """
            SELECT rating_scope, game, entrant_id, display_name,
                   opponent_entrant_id, opponent_display_name, outcome,
                   rating_before, rating_after, created_at
            FROM rating_history
            WHERE match_id = ?
            """,
            (archive.match_id,),
        ).fetchall()
        if not rated:
            if rows:
                raise StorageError(f"数据库中 match_id {archive.match_id!r} 的未计分状态已损坏")
            return
        if len(entrants) != 2 or len(rows) != 4:
            raise StorageError(f"数据库中 match_id {archive.match_id!r} 的 ELO 历史已损坏")
        history = {(row["rating_scope"], row["game"], row["entrant_id"]): row for row in rows}
        if len(history) != len(rows):
            raise StorageError(f"数据库中 match_id {archive.match_id!r} 的 ELO 历史已损坏")
        player_a, player_b = entrants
        score_a = archive.scores[player_a.display_name]
        score_b = archive.scores[player_b.display_name]
        outcome_a = 1.0 if score_a > score_b else 0.0 if score_a < score_b else 0.5
        for rating_scope, game_key in (("overall", ""), ("game", archive.game)):
            row_a = history.get((rating_scope, game_key, player_a.entrant_id))
            row_b = history.get((rating_scope, game_key, player_b.entrant_id))
            if row_a is None or row_b is None:
                raise StorageError(f"数据库中 match_id {archive.match_id!r} 的 ELO 历史已损坏")
            try:
                before_a = self._finite_database_float(row_a["rating_before"])
                before_b = self._finite_database_float(row_b["rating_before"])
                stored_outcome_a = self._finite_database_float(row_a["outcome"])
                stored_outcome_b = self._finite_database_float(row_b["outcome"])
                stored_after_a = self._finite_database_float(row_a["rating_after"])
                stored_after_b = self._finite_database_float(row_b["rating_after"])
            except (TypeError, ValueError) as exc:
                raise StorageError(
                    f"数据库中 match_id {archive.match_id!r} 的 ELO 历史已损坏"
                ) from exc
            after_a, after_b = update_ratings(before_a, before_b, outcome_a)
            if (
                row_a["display_name"] != player_a.display_name
                or row_a["opponent_entrant_id"] != player_b.entrant_id
                or row_a["opponent_display_name"] != player_b.display_name
                or stored_outcome_a != outcome_a
                or stored_after_a != after_a
                or not self._timestamp_matches(row_a["created_at"], archive.finished_at)
                or row_b["display_name"] != player_b.display_name
                or row_b["opponent_entrant_id"] != player_a.entrant_id
                or row_b["opponent_display_name"] != player_a.display_name
                or stored_outcome_b != 1.0 - outcome_a
                or stored_after_b != after_b
                or not self._timestamp_matches(row_b["created_at"], archive.finished_at)
            ):
                raise StorageError(f"数据库中 match_id {archive.match_id!r} 的 ELO 历史已损坏")

    def _verify_existing_match(
        self,
        connection: sqlite3.Connection,
        metadata_row: sqlite3.Row,
        archive: MatchArchive,
        entrants: list[_EntrantRef],
        *,
        rating_source: str,
        rated: bool,
        rating_policy: str,
    ) -> None:
        self._verify_match_metadata(
            metadata_row,
            archive,
            rating_source=rating_source,
            rated=rated,
            rating_policy=rating_policy,
        )
        self._verify_match_players(connection, archive, entrants)
        self._verify_match_history(connection, archive, entrants, rated=rated)

    def _verify_existing_tournament_child(
        self,
        connection: sqlite3.Connection,
        *,
        requested_rating_source: RatingSource,
        series_id: str | None = None,
        match_id: str | None = None,
    ) -> TournamentSaveResult | None:
        if (series_id is None) == (match_id is None):
            raise ValueError("series_id 与 match_id 必须且只能提供一个")
        if series_id is not None:
            row = connection.execute(
                """
                SELECT ta.tournament_id AS stored_tournament_id,
                       ta.tournament_json, ta.archive_source, ta.rating_source,
                       ta.rated, ta.rating_policy, ta.pairing_count
                FROM tournament_pairings AS tp
                JOIN tournament_archives AS ta
                  ON ta.tournament_id = tp.tournament_id
                WHERE tp.series_id = ?
                """,
                (series_id,),
            ).fetchone()
            collision_error = SeriesIdCollisionError
            identifier = series_id
            identifier_name = "series_id"
        else:
            row = connection.execute(
                """
                SELECT ta.tournament_id AS stored_tournament_id,
                       ta.tournament_json, ta.archive_source, ta.rating_source,
                       ta.rated, ta.rating_policy, ta.pairing_count
                FROM series_matches AS sm
                JOIN tournament_pairings AS tp ON tp.series_id = sm.series_id
                JOIN tournament_archives AS ta
                  ON ta.tournament_id = tp.tournament_id
                WHERE sm.match_id = ?
                """,
                (match_id,),
            ).fetchone()
            collision_error = MatchIdCollisionError
            identifier = match_id
            identifier_name = "match_id"
        if row is None:
            return None
        try:
            tournament = TournamentArchive.model_validate_json(row["tournament_json"])
            tournament, _ = self._validate_tournament(tournament)
        except (TypeError, ValueError, StorageError) as exc:
            raise StorageError(
                f"数据库中 {identifier_name} {identifier!r} 所属循环赛档案已损坏"
            ) from exc
        if tournament.tournament_id != row["stored_tournament_id"]:
            raise StorageError(f"数据库中 {identifier_name} {identifier!r} 所属循环赛档案已损坏")
        stored_rated = bool(row["rated"])
        expected_rated = (
            row["rating_source"] == "engine" and row["archive_source"] == "local_engine"
        )
        expected_policy = "elo_tournament_batch_v1" if stored_rated else "unrated"
        if stored_rated != expected_rated or row["rating_policy"] != expected_policy:
            raise StorageError(
                f"数据库中 {identifier_name} {identifier!r} 所属循环赛计分状态已损坏"
            )
        if requested_rating_source == "engine" and row["rating_source"] != "engine":
            raise collision_error(
                f"{identifier_name} {identifier!r} 已作为未计分循环赛子记录存档，"
                "不能通过幂等重存升级"
            )
        self._verify_existing_tournament(
            connection,
            tournament,
            rating_source=row["rating_source"],
            rated=stored_rated,
            rating_policy=row["rating_policy"],
        )
        return TournamentSaveResult(
            inserted=False,
            rated=stored_rated,
            pairing_count=row["pairing_count"],
            match_count=row["pairing_count"] * 2,
        )

    def _verify_existing_series_leg(
        self,
        connection: sqlite3.Connection,
        archive: MatchArchive,
        *,
        requested_rating_source: RatingSource,
    ) -> SaveResult | None:
        tournament_result = self._verify_existing_tournament_child(
            connection,
            requested_rating_source=requested_rating_source,
            match_id=archive.match_id,
        )
        if tournament_result is not None:
            return SaveResult(inserted=False, rated=tournament_result.rated)
        row = connection.execute(
            """
            SELECT sa.series_id AS stored_series_id,
                   sa.series_json, sa.archive_source, sa.rating_source,
                   sa.rated, sa.rating_policy
            FROM series_matches AS sm
            JOIN series_archives AS sa ON sa.series_id = sm.series_id
            WHERE sm.match_id = ?
            """,
            (archive.match_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            series = SeriesArchive.model_validate_json(row["series_json"])
            series, _ = self._validate_series(series)
        except (TypeError, ValueError, StorageError) as exc:
            raise StorageError(
                f"数据库中 match_id {archive.match_id!r} 所属系列赛档案已损坏"
            ) from exc
        if series.series_id != row["stored_series_id"]:
            raise StorageError(f"数据库中 match_id {archive.match_id!r} 所属系列赛档案已损坏")
        stored_rated = bool(row["rated"])
        expected_policy = "elo_batch_v1" if stored_rated else "unrated"
        expected_rated = row["rating_source"] == "engine" and row["archive_source"] in (
            "local_engine",
            "legacy",
        )
        if row["rating_policy"] != expected_policy or stored_rated != expected_rated:
            raise StorageError(f"数据库中 match_id {archive.match_id!r} 所属系列赛计分状态已损坏")
        if row["archive_source"] != archive.source or (
            requested_rating_source == "engine" and row["rating_source"] != "engine"
        ):
            raise MatchIdCollisionError(
                f"match_id {archive.match_id!r} 已以不同来源或计分策略存档，不能通过幂等重存升级"
            )
        self._verify_existing_series(
            connection,
            series,
            row["rating_policy"],
            rated=stored_rated,
            archive_source=row["archive_source"],
            rating_source=row["rating_source"],
        )
        return SaveResult(inserted=False, rated=stored_rated)

    def save_match(
        self,
        archive: MatchArchive,
        *,
        rating_source: RatingSource = "imported",
    ) -> SaveResult:
        """Persist one completed match, rating only trusted local-engine archives.

        Re-saving an identical ``match_id`` is an idempotent no-op.  Reusing an
        id for different content raises :class:`MatchIdCollisionError`.
        ``rating_source="imported"`` is the safe default and never changes ELO.
        Matches with any player count other than two are also archived but unrated.
        """

        rating_source = self._validate_rating_source(rating_source)
        entrants = self._validate_archive(archive)
        trusted_engine = (
            rating_source == "engine"
            and archive.schema_version == 2
            and archive.source == "local_engine"
        )
        rated = trusted_engine and len(entrants) == 2
        rating_policy = "elo_v1" if rated else "unrated"
        archive_payload, archive_json = self._serialize_archive(archive)
        semantic_archive_json = _canonical_json(archive.model_dump(mode="json"))

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            checkpoint_owner = connection.execute(
                """
                SELECT tcs.tournament_id
                FROM tournament_checkpoint_series AS tcs
                JOIN tournament_checkpoints AS tc
                  ON tc.tournament_id = tcs.tournament_id
                WHERE tc.status = 'in_progress'
                  AND (tcs.match_1_id = ? OR tcs.match_2_id = ?)
                """,
                (archive.match_id, archive.match_id),
            ).fetchone()
            if checkpoint_owner is not None:
                raise MatchIdCollisionError(
                    f"match_id {archive.match_id!r} 已由进行中的循环赛 checkpoint 保留"
                )
            existing = connection.execute(
                """
                SELECT match_id, schema_version, game, seed, players_json, scores_json,
                       started_at, finished_at, archive_json, archive_source,
                       rating_source, rated, rating_policy
                FROM matches WHERE match_id = ?
                """,
                (archive.match_id,),
            ).fetchone()
            if existing is not None:
                try:
                    existing_json = self._semantic_match_json(existing["archive_json"])
                except StorageError as exc:
                    raise StorageError(
                        f"数据库中 match_id {archive.match_id!r} 的档案 JSON 已损坏"
                    ) from exc
                if existing_json != semantic_archive_json:
                    raise MatchIdCollisionError(
                        f"match_id {archive.match_id!r} 已对应另一份对局档案"
                    )
                series_result = self._verify_existing_series_leg(
                    connection,
                    archive,
                    requested_rating_source=rating_source,
                )
                if series_result is not None:
                    connection.commit()
                    return series_result
                stored_rated = bool(existing["rated"])
                expected_stored_policy = "elo_v1" if stored_rated else "unrated"
                expected_stored_rated = existing["rating_source"] == "engine" and (
                    (archive.schema_version == 1 and archive.source == "legacy")
                    or (
                        archive.schema_version == 2
                        and archive.source == "local_engine"
                        and len(entrants) == 2
                    )
                )
                if (
                    existing["rating_policy"] != expected_stored_policy
                    or stored_rated != expected_stored_rated
                    or (stored_rated and existing["rating_source"] != "engine")
                    or (
                        stored_rated
                        and existing["archive_source"] not in ("local_engine", "legacy")
                    )
                    or (stored_rated and len(entrants) != 2)
                ):
                    raise StorageError(
                        f"数据库中 match_id {archive.match_id!r} 的计分来源或策略已损坏"
                    )
                read_only_downgrade = (
                    existing["rating_source"] == "engine" and rating_source == "imported"
                )
                exact_policy_match = (
                    existing["rating_source"] == rating_source
                    and stored_rated == rated
                    and existing["rating_policy"] == rating_policy
                )
                historical_engine_repeat = (
                    archive.source == "legacy"
                    and existing["rating_source"] == "engine"
                    and rating_source == "engine"
                )
                if existing["archive_source"] != archive.source or not (
                    read_only_downgrade or exact_policy_match or historical_engine_repeat
                ):
                    raise MatchIdCollisionError(
                        f"match_id {archive.match_id!r} 已以不同来源或计分策略存档，"
                        "不能通过幂等重存升级"
                    )
                self._verify_existing_match(
                    connection,
                    existing,
                    archive,
                    entrants,
                    rating_source=existing["rating_source"],
                    rated=stored_rated,
                    rating_policy=existing["rating_policy"],
                )
                connection.commit()
                return SaveResult(inserted=False, rated=stored_rated)

            for entrant in entrants:
                self._upsert_entrant(
                    connection,
                    entrant,
                    observed_at=archive.started_at,
                    trusted_engine=trusted_engine,
                )

            self._insert_match(
                connection,
                archive,
                entrants,
                archive_payload,
                archive_json,
                rating_source=rating_source,
                rated=rated,
                rating_policy=rating_policy,
            )

            changes: list[RatingChange] = []
            if rated:
                score_a = archive.scores[entrants[0].display_name]
                score_b = archive.scores[entrants[1].display_name]
                outcome_a = 1.0 if score_a > score_b else 0.0 if score_a < score_b else 0.5
                changes.extend(
                    self._record_ratings(
                        connection, archive, entrants[0], entrants[1], outcome_a, None
                    )
                )
                changes.extend(
                    self._record_ratings(
                        connection,
                        archive,
                        entrants[0],
                        entrants[1],
                        outcome_a,
                        archive.game,
                    )
                )

            connection.commit()
            return SaveResult(
                inserted=True,
                rated=rated,
                rating_changes=tuple(changes),
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _serialize_archive(archive: MatchArchive) -> tuple[dict, str]:
        archive_payload = archive.model_dump(mode="json")
        return archive_payload, _canonical_json(archive_payload)

    @staticmethod
    def _insert_match(
        connection: sqlite3.Connection,
        archive: MatchArchive,
        entrants: list[_EntrantRef],
        archive_payload: dict,
        archive_json: str,
        *,
        rating_source: RatingSource,
        rated: bool,
        rating_policy: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO matches (
                match_id, schema_version, game, seed, players_json, scores_json,
                started_at, finished_at, archive_source, rating_source, rated,
                rating_policy, archive_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                archive.match_id,
                archive.schema_version,
                archive.game,
                archive.seed,
                _canonical_json(archive_payload["players"]),
                _canonical_json(archive_payload["scores"]),
                archive.started_at.astimezone(UTC).isoformat(),
                archive.finished_at.astimezone(UTC).isoformat(),
                archive.source,
                rating_source,
                int(rated),
                rating_policy,
                archive_json,
            ),
        )
        for position, (entrant, descriptor) in enumerate(zip(entrants, archive_payload["players"])):
            connection.execute(
                """
                INSERT INTO match_players (
                    match_id, position, player, entrant_id, display_name,
                    descriptor_json, score
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    archive.match_id,
                    position,
                    entrant.display_name,
                    entrant.entrant_id,
                    entrant.display_name,
                    _canonical_json(descriptor),
                    archive.scores[entrant.display_name],
                ),
            )

    def _insert_series_structure(
        self,
        connection: sqlite3.Connection,
        series: SeriesArchive,
        *,
        rating_source: RatingSource,
        rated: bool,
        rating_policy: str,
    ) -> None:
        series_payload = series.model_dump(mode="json")
        connection.execute(
            """
            INSERT INTO series_archives (
                series_id, schema_version, game, seed, players_json, points_json,
                rating_policy, started_at, finished_at, series_json,
                archive_source, rating_source, rated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                series.series_id,
                series.schema_version,
                series.game,
                series.seed,
                _canonical_json(series_payload["players"]),
                _canonical_json(series_payload["points"]),
                rating_policy,
                series.started_at.astimezone(UTC).isoformat(),
                series.finished_at.astimezone(UTC).isoformat(),
                _canonical_json(series_payload),
                series.source,
                rating_source,
                int(rated),
            ),
        )
        for leg_number, leg in enumerate(series.legs, start=1):
            archive_payload, archive_json = self._serialize_archive(leg)
            leg_entrants = self._validate_archive(leg)
            self._insert_match(
                connection,
                leg,
                leg_entrants,
                archive_payload,
                archive_json,
                rating_source=rating_source,
                rated=rated,
                rating_policy=rating_policy,
            )
            connection.execute(
                """
                INSERT INTO series_matches (series_id, leg_number, match_id)
                VALUES (?, ?, ?)
                """,
                (series.series_id, leg_number, leg.match_id),
            )

    def _verify_tournament_series_structure(
        self,
        connection: sqlite3.Connection,
        series: SeriesArchive,
        *,
        rating_source: str,
        rated: bool,
        rating_policy: str,
    ) -> None:
        series_row = connection.execute(
            """
            SELECT series_id, schema_version, game, seed, players_json, points_json,
                   started_at, finished_at, archive_source, rating_source, rated,
                   rating_policy, series_json
            FROM series_archives
            WHERE series_id = ?
            """,
            (series.series_id,),
        ).fetchone()
        if series_row is None:
            raise StorageError(f"循环赛中的 series_id {series.series_id!r} 已丢失")
        try:
            stored_series_json = self._semantic_series_json(series_row["series_json"])
        except StorageError as exc:
            raise StorageError(
                f"数据库中 series_id {series.series_id!r} 的档案 JSON 已损坏"
            ) from exc
        if stored_series_json != _canonical_json(series.model_dump(mode="json")):
            raise StorageError(f"数据库中 series_id {series.series_id!r} 的循环赛档案已损坏")
        self._verify_series_metadata(
            series_row,
            series,
            rating_source=rating_source,
            rated=rated,
            rating_policy=rating_policy,
        )
        rows = connection.execute(
            """
            SELECT sm.leg_number, sm.match_id, m.schema_version, m.game, m.seed,
                   m.players_json, m.scores_json, m.started_at, m.finished_at,
                   m.archive_json, m.archive_source, m.rating_source, m.rated,
                   m.rating_policy
            FROM series_matches AS sm
            JOIN matches AS m ON m.match_id = sm.match_id
            WHERE sm.series_id = ?
            ORDER BY sm.leg_number
            """,
            (series.series_id,),
        ).fetchall()
        if [row["leg_number"] for row in rows] != [1, 2] or [row["match_id"] for row in rows] != [
            leg.match_id for leg in series.legs
        ]:
            raise StorageError(f"数据库中 series_id {series.series_id!r} 的循环赛对局映射已损坏")
        for row, leg in zip(rows, series.legs):
            self._verify_match_metadata(
                row,
                leg,
                rating_source=rating_source,
                rated=rated,
                rating_policy=rating_policy,
            )
            try:
                stored_match_json = self._semantic_match_json(row["archive_json"])
            except StorageError as exc:
                raise StorageError(
                    f"数据库中 match_id {leg.match_id!r} 的档案 JSON 已损坏"
                ) from exc
            if stored_match_json != _canonical_json(leg.model_dump(mode="json")):
                raise StorageError(f"数据库中 match_id {leg.match_id!r} 的循环赛档案已损坏")
            self._verify_match_players(
                connection,
                leg,
                self._validate_archive(leg),
            )

    def _verify_tournament_ratings(
        self,
        connection: sqlite3.Connection,
        tournament: TournamentArchive,
        entrants: tuple[_EntrantRef, ...],
        *,
        rated: bool,
    ) -> bool:
        history_rows = connection.execute(
            """
            SELECT rh.match_id, rh.rating_scope, rh.game, rh.entrant_id,
                   rh.display_name, rh.opponent_entrant_id,
                   rh.opponent_display_name, rh.outcome, rh.rating_before,
                   rh.rating_after, rh.created_at
            FROM rating_history AS rh
            JOIN series_matches AS sm ON sm.match_id = rh.match_id
            JOIN tournament_pairings AS tp ON tp.series_id = sm.series_id
            WHERE tp.tournament_id = ?
            """,
            (tournament.tournament_id,),
        ).fetchall()
        snapshot_rows = connection.execute(
            """
            SELECT rating_scope, game, entrant_id, display_name, rating_before,
                   rating_after, games_added, wins_added, draws_added,
                   losses_added
            FROM tournament_rating_snapshots
            WHERE tournament_id = ?
            """,
            (tournament.tournament_id,),
        ).fetchall()
        contribution_rows = connection.execute(
            """
            SELECT sequence, match_id, rating_scope, game, entrant_id,
                   opponent_entrant_id, frozen_rating, opponent_frozen_rating,
                   expected_score, rating_delta
            FROM tournament_rating_contributions
            WHERE tournament_id = ?
            """,
            (tournament.tournament_id,),
        ).fetchall()
        if not rated:
            if history_rows or snapshot_rows or contribution_rows:
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的未计分状态已损坏"
                )
            return True

        snapshot_map = {
            (row["rating_scope"], row["game"], row["entrant_id"]): row for row in snapshot_rows
        }
        expected_snapshot_keys = {
            (rating_scope, game_key, entrant.entrant_id)
            for rating_scope, game_key in (("overall", ""), ("game", tournament.game))
            for entrant in entrants
        }
        if len(snapshot_map) != len(snapshot_rows) or set(snapshot_map) != expected_snapshot_keys:
            raise StorageError(
                f"数据库中 tournament_id {tournament.tournament_id!r} 的 ELO 快照已损坏"
            )
        try:
            frozen_ratings = {
                key: self._finite_database_float(row["rating_before"])
                for key, row in snapshot_map.items()
            }
        except (TypeError, ValueError) as exc:
            raise StorageError(
                f"数据库中 tournament_id {tournament.tournament_id!r} 的 ELO 快照已损坏"
            ) from exc

        expected_contributions, expected_aggregates = self._tournament_rating_ledger(
            tournament,
            entrants,
            frozen_ratings,
        )
        history = {
            (row["match_id"], row["rating_scope"], row["game"], row["entrant_id"]): row
            for row in history_rows
        }
        contributions = {
            (row["match_id"], row["rating_scope"], row["game"], row["entrant_id"]): row
            for row in contribution_rows
        }
        expected_keys = {
            (
                contribution.archive.match_id,
                contribution.rating_scope,
                contribution.game_key,
                contribution.player.entrant_id,
            )
            for contribution in expected_contributions
        }
        if (
            len(history) != len(history_rows)
            or len(contributions) != len(contribution_rows)
            or set(history) != expected_keys
            or set(contributions) != expected_keys
        ):
            raise StorageError(
                f"数据库中 tournament_id {tournament.tournament_id!r} 的 ELO 历史已损坏"
            )
        for expected in expected_contributions:
            key = (
                expected.archive.match_id,
                expected.rating_scope,
                expected.game_key,
                expected.player.entrant_id,
            )
            history_row = history[key]
            contribution_row = contributions[key]
            try:
                stored_outcome = self._finite_database_float(history_row["outcome"])
                stored_before = self._finite_database_float(history_row["rating_before"])
                stored_after = self._finite_database_float(history_row["rating_after"])
                stored_frozen = self._finite_database_float(contribution_row["frozen_rating"])
                stored_opponent_frozen = self._finite_database_float(
                    contribution_row["opponent_frozen_rating"]
                )
                stored_expected = self._finite_database_float(contribution_row["expected_score"])
                stored_delta = self._finite_database_float(contribution_row["rating_delta"])
            except (TypeError, ValueError) as exc:
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的 ELO 历史已损坏"
                ) from exc
            if (
                history_row["display_name"] != expected.player.display_name
                or history_row["opponent_entrant_id"] != expected.opponent.entrant_id
                or history_row["opponent_display_name"] != expected.opponent.display_name
                or stored_outcome != expected.outcome
                or stored_before != expected.before
                or stored_after != expected.after
                or not self._timestamp_matches(
                    history_row["created_at"], expected.archive.finished_at
                )
                or contribution_row["sequence"] != expected.sequence
                or contribution_row["opponent_entrant_id"] != expected.opponent.entrant_id
                or stored_frozen != expected.frozen_rating
                or stored_opponent_frozen != expected.opponent_frozen_rating
                or stored_expected != expected.expected
                or stored_delta != expected.delta
            ):
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的 ELO 历史已损坏"
                )

        for expected in expected_aggregates:
            key = (
                expected.rating_scope,
                expected.game_key,
                expected.player.entrant_id,
            )
            row = snapshot_map[key]
            wins = sum(outcome == 1.0 for outcome in expected.outcomes)
            draws = sum(outcome == 0.5 for outcome in expected.outcomes)
            losses = sum(outcome == 0.0 for outcome in expected.outcomes)
            try:
                stored_after = self._finite_database_float(row["rating_after"])
            except (TypeError, ValueError) as exc:
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的 ELO 快照已损坏"
                ) from exc
            if (
                row["display_name"] != expected.player.display_name
                or stored_after != expected.after
                or row["games_added"] != len(expected.outcomes)
                or row["wins_added"] != wins
                or row["draws_added"] != draws
                or row["losses_added"] != losses
            ):
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的 ELO 快照已损坏"
                )

        return self._verify_current_tournament_ratings_if_latest(
            connection,
            tournament,
            expected_aggregates,
        )

    def _verify_current_tournament_ratings_if_latest(
        self,
        connection: sqlite3.Connection,
        tournament: TournamentArchive,
        expected_aggregates: list[_TournamentAggregate],
    ) -> bool:
        """Verify the materialized leaderboard when this is its only known operation.

        Schema v6 has event timestamps but no global rating-operation sequence.
        Another history/snapshot row may therefore have been committed before or
        after this tournament regardless of its event time.  Counts can always be
        checked; the current numeric rating is only replay-complete when no other
        operation for this entrant and scope exists.
        """

        tournament_finished_at = tournament.finished_at.astimezone(UTC)
        replay_complete = True
        for expected in expected_aggregates:
            row = connection.execute(
                """
                SELECT rating, games_played, wins, draws, losses, updated_at
                FROM ratings
                WHERE rating_scope = ? AND game = ? AND entrant_id = ?
                """,
                (
                    expected.rating_scope,
                    expected.game_key,
                    expected.player.entrant_id,
                ),
            ).fetchone()
            if row is None:
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的当前 ELO 排行榜已损坏"
                )
            try:
                current_rating = self._finite_database_float(row["rating"])
            except (TypeError, ValueError) as exc:
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的当前 ELO 排行榜已损坏"
                ) from exc
            leaderboard_history_rows = connection.execute(
                """
                SELECT outcome, rating_after, created_at
                FROM rating_history
                WHERE rating_scope = ? AND game = ? AND entrant_id = ?
                """,
                (
                    expected.rating_scope,
                    expected.game_key,
                    expected.player.entrant_id,
                ),
            ).fetchall()
            try:
                history_outcomes = tuple(
                    self._finite_database_float(history_row["outcome"])
                    for history_row in leaderboard_history_rows
                )
                history_after_values = tuple(
                    self._finite_database_float(history_row["rating_after"])
                    for history_row in leaderboard_history_rows
                )
                history_created_at = tuple(
                    datetime.fromisoformat(history_row["created_at"])
                    for history_row in leaderboard_history_rows
                )
            except (TypeError, ValueError) as exc:
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的当前 ELO 排行榜已损坏"
                ) from exc
            if (
                not leaderboard_history_rows
                or any(outcome not in (0.0, 0.5, 1.0) for outcome in history_outcomes)
                or any(timestamp.utcoffset() is None for timestamp in history_created_at)
                or row["games_played"] != len(history_outcomes)
                or row["wins"] != history_outcomes.count(1.0)
                or row["draws"] != history_outcomes.count(0.5)
                or row["losses"] != history_outcomes.count(0.0)
                or current_rating not in history_after_values
            ):
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的当前 ELO 排行榜已损坏"
                )
            raw_updated_at = row["updated_at"]
            if not isinstance(raw_updated_at, str):
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的当前 ELO 排行榜已损坏"
                )
            try:
                updated_at = datetime.fromisoformat(raw_updated_at)
            except ValueError as exc:
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的当前 ELO 排行榜已损坏"
                ) from exc
            operation_time_rows = connection.execute(
                """
                SELECT m.finished_at
                FROM rating_history AS rh
                JOIN matches AS m ON m.match_id = rh.match_id
                LEFT JOIN series_matches AS sm ON sm.match_id = rh.match_id
                WHERE rh.rating_scope = ? AND rh.game = ? AND rh.entrant_id = ?
                  AND sm.match_id IS NULL
                UNION
                SELECT sa.finished_at
                FROM rating_history AS rh
                JOIN series_matches AS sm ON sm.match_id = rh.match_id
                JOIN series_archives AS sa ON sa.series_id = sm.series_id
                LEFT JOIN tournament_pairings AS tp ON tp.series_id = sm.series_id
                WHERE rh.rating_scope = ? AND rh.game = ? AND rh.entrant_id = ?
                  AND tp.series_id IS NULL
                UNION
                SELECT ta.finished_at
                FROM tournament_rating_snapshots AS trs
                JOIN tournament_archives AS ta
                  ON ta.tournament_id = trs.tournament_id
                WHERE trs.rating_scope = ? AND trs.game = ? AND trs.entrant_id = ?
                """,
                (
                    expected.rating_scope,
                    expected.game_key,
                    expected.player.entrant_id,
                    expected.rating_scope,
                    expected.game_key,
                    expected.player.entrant_id,
                    expected.rating_scope,
                    expected.game_key,
                    expected.player.entrant_id,
                ),
            ).fetchall()
            try:
                operation_finished_at = tuple(
                    datetime.fromisoformat(operation_row["finished_at"])
                    for operation_row in operation_time_rows
                )
            except (TypeError, ValueError) as exc:
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的当前 ELO 排行榜已损坏"
                ) from exc
            if (
                updated_at.utcoffset() is None
                or not operation_finished_at
                or any(timestamp.utcoffset() is None for timestamp in operation_finished_at)
                or updated_at.astimezone(UTC)
                != max(timestamp.astimezone(UTC) for timestamp in operation_finished_at)
            ):
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的当前 ELO 排行榜已损坏"
                )
            other_tournament = connection.execute(
                """
                SELECT 1
                FROM tournament_rating_snapshots AS trs
                WHERE trs.rating_scope = ?
                  AND trs.game = ?
                  AND trs.entrant_id = ?
                  AND trs.tournament_id <> ?
                LIMIT 1
                """,
                (
                    expected.rating_scope,
                    expected.game_key,
                    expected.player.entrant_id,
                    tournament.tournament_id,
                ),
            ).fetchone()
            outside_history_rows = connection.execute(
                """
                SELECT rh.rating_after
                FROM rating_history AS rh
                WHERE rh.rating_scope = ?
                  AND rh.game = ?
                  AND rh.entrant_id = ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM series_matches AS sm
                      JOIN tournament_pairings AS tp
                        ON tp.series_id = sm.series_id
                      WHERE sm.match_id = rh.match_id
                        AND tp.tournament_id = ?
                  )
                """,
                (
                    expected.rating_scope,
                    expected.game_key,
                    expected.player.entrant_id,
                    tournament.tournament_id,
                ),
            ).fetchall()
            try:
                outside_after_values = {
                    self._finite_database_float(history_row["rating_after"])
                    for history_row in outside_history_rows
                }
            except (TypeError, ValueError) as exc:
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的当前 ELO 排行榜已损坏"
                ) from exc
            if other_tournament is not None and not outside_history_rows:
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的当前 ELO 排行榜已损坏"
                )
            if expected.before not in {DEFAULT_RATING, *outside_after_values}:
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的当前 ELO 排行榜已损坏"
                )
            if outside_history_rows:
                replay_complete = False
                continue

            if (
                updated_at.astimezone(UTC) != tournament_finished_at
                or current_rating != expected.after
            ):
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的当前 ELO 排行榜已损坏"
                )
        return replay_complete

    def _verify_existing_tournament(
        self,
        connection: sqlite3.Connection,
        tournament: TournamentArchive,
        *,
        rating_source: str,
        rated: bool,
        rating_policy: str,
    ) -> bool:
        expected_rated = rating_source == "engine" and tournament.source == "local_engine"
        expected_policy = "elo_tournament_batch_v1" if rated else "unrated"
        if rated != expected_rated or rating_policy != expected_policy:
            raise StorageError(
                f"数据库中 tournament_id {tournament.tournament_id!r} 的来源或计分状态已损坏"
            )
        tournament_row = connection.execute(
            """
            SELECT tournament_id, schema_version, format, pairing_policy,
                   seed_policy, game, seed, players_json, points_json,
                   pairing_count, rating_policy, k_factor, started_at,
                   finished_at, archive_source, rating_source, rated,
                   tournament_json
            FROM tournament_archives
            WHERE tournament_id = ?
            """,
            (tournament.tournament_id,),
        ).fetchone()
        if tournament_row is None:
            raise StorageError(f"数据库中 tournament_id {tournament.tournament_id!r} 已丢失")
        try:
            stored_json = self._semantic_tournament_json(tournament_row["tournament_json"])
        except StorageError as exc:
            raise StorageError(
                f"数据库中 tournament_id {tournament.tournament_id!r} 的档案 JSON 已损坏"
            ) from exc
        if stored_json != _canonical_json(tournament.model_dump(mode="json")):
            raise TournamentIdCollisionError(
                f"tournament_id {tournament.tournament_id!r} 已对应另一份循环赛档案"
            )
        self._verify_tournament_metadata(
            tournament_row,
            tournament,
            rating_source=rating_source,
            rated=rated,
            rating_policy=rating_policy,
        )

        entrants = tuple(
            self._entrant_ref(descriptor, legacy=False) for descriptor in tournament.players
        )
        standings = {standing.entrant_id: standing for standing in tournament.standings}
        entrant_rows = connection.execute(
            """
            SELECT position, entrant_id, display_name, descriptor_json, points,
                   series_played, series_wins, series_draws, series_losses,
                   games_played, wins, draws, losses, technical_losses
            FROM tournament_entrants
            WHERE tournament_id = ?
            ORDER BY position
            """,
            (tournament.tournament_id,),
        ).fetchall()
        if len(entrant_rows) != len(entrants) or [row["position"] for row in entrant_rows] != list(
            range(len(entrants))
        ):
            raise StorageError(
                f"数据库中 tournament_id {tournament.tournament_id!r} 的参赛者索引已损坏"
            )
        payload = tournament.model_dump(mode="json")
        for row, entrant, descriptor in zip(
            entrant_rows,
            entrants,
            payload["players"],
        ):
            standing = standings[entrant.entrant_id]
            try:
                stored_descriptor = self._semantic_descriptor_json(
                    row["descriptor_json"], legacy=False
                )
                stored_points = self._finite_database_float(row["points"])
            except (StorageError, TypeError, ValueError) as exc:
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的参赛者索引已损坏"
                ) from exc
            if (
                row["entrant_id"] != entrant.entrant_id
                or row["display_name"] != entrant.display_name
                or stored_descriptor != _canonical_json(descriptor)
                or stored_points != standing.points
                or row["series_played"] != standing.series_played
                or row["series_wins"] != standing.series_wins
                or row["series_draws"] != standing.series_draws
                or row["series_losses"] != standing.series_losses
                or row["games_played"] != standing.games_played
                or row["wins"] != standing.wins
                or row["draws"] != standing.draws
                or row["losses"] != standing.losses
                or row["technical_losses"] != standing.technical_losses
            ):
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的参赛者索引已损坏"
                )

        if rated:
            for entrant in entrants:
                identity_row = connection.execute(
                    """
                    SELECT display_name, identity_json, created_at, updated_at
                    FROM entrants WHERE entrant_id = ?
                    """,
                    (entrant.entrant_id,),
                ).fetchone()
                if identity_row is None:
                    raise StorageError(
                        f"数据库中 tournament_id {tournament.tournament_id!r} 的全局选手身份已损坏"
                    )
                try:
                    created_at = datetime.fromisoformat(identity_row["created_at"])
                    updated_at = datetime.fromisoformat(identity_row["updated_at"])
                except (TypeError, ValueError) as exc:
                    raise StorageError(
                        f"数据库中 tournament_id {tournament.tournament_id!r} 的全局选手身份已损坏"
                    ) from exc
                if (
                    not isinstance(identity_row["display_name"], str)
                    or not identity_row["display_name"]
                    or identity_row["identity_json"] != entrant.identity_json
                    or created_at.utcoffset() is None
                    or updated_at.utcoffset() is None
                ):
                    raise StorageError(
                        f"数据库中 tournament_id {tournament.tournament_id!r} 的全局选手身份已损坏"
                    )

        pairing_rows = connection.execute(
            """
            SELECT pairing_number, series_id, entrant_a_id, entrant_b_id
            FROM tournament_pairings
            WHERE tournament_id = ?
            ORDER BY pairing_number
            """,
            (tournament.tournament_id,),
        ).fetchall()
        if len(pairing_rows) != len(tournament.pairings):
            raise StorageError(
                f"数据库中 tournament_id {tournament.tournament_id!r} 的配对索引已损坏"
            )
        for row, pairing in zip(pairing_rows, tournament.pairings):
            player_a = entrants[pairing.player_indices[0]]
            player_b = entrants[pairing.player_indices[1]]
            if (
                row["pairing_number"] != pairing.pairing_number
                or row["series_id"] != pairing.series.series_id
                or row["entrant_a_id"] != player_a.entrant_id
                or row["entrant_b_id"] != player_b.entrant_id
            ):
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的配对索引已损坏"
                )
            self._verify_tournament_series_structure(
                connection,
                pairing.series,
                rating_source=rating_source,
                rated=rated,
                rating_policy=rating_policy,
            )

        return self._verify_tournament_ratings(
            connection,
            tournament,
            entrants,
            rated=rated,
        )

    def _load_verified_tournament(
        self,
        connection: sqlite3.Connection,
        tournament_id: str,
    ) -> tuple[TournamentArchive, bool, bool | None] | None:
        """Load one formal tournament and deeply verify all relational state."""

        row = connection.execute(
            """
            SELECT tournament_json, rating_source, rated, rating_policy
            FROM tournament_archives
            WHERE tournament_id = ?
            """,
            (tournament_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            tournament = TournamentArchive.model_validate_json(row["tournament_json"])
            tournament, _ = self._validate_tournament(tournament)
        except (TypeError, ValueError, StorageError) as exc:
            raise StorageError(
                f"数据库中 tournament_id {tournament_id!r} 的正式循环赛档案已损坏"
            ) from exc
        if tournament.tournament_id != tournament_id:
            raise StorageError(f"数据库中 tournament_id {tournament_id!r} 的正式循环赛档案已损坏")
        rated = bool(row["rated"])
        replay_complete = self._verify_existing_tournament(
            connection,
            tournament,
            rating_source=row["rating_source"],
            rated=rated,
            rating_policy=row["rating_policy"],
        )
        return tournament, rated, replay_complete if rated else None

    @staticmethod
    def _database_epoch(connection: sqlite3.Connection) -> int:
        value = connection.execute(
            "SELECT CAST(strftime('%s', 'now') AS INTEGER) AS epoch"
        ).fetchone()["epoch"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise StorageError("SQLite 无法提供 runner lease 时钟")
        return value

    @staticmethod
    def _load_tournament_runner_lease(
        connection: sqlite3.Connection,
        tournament_id: str,
    ) -> _TournamentRunnerLeaseState | None:
        row = connection.execute(
            """
            SELECT generation, token_digest, acquired_at_epoch,
                   renewed_at_epoch, expires_at_epoch
            FROM tournament_runner_leases
            WHERE tournament_id = ?
            """,
            (tournament_id,),
        ).fetchone()
        if row is None:
            return None

        generation = row["generation"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise StorageError("循环赛 runner lease 已损坏")

        raw_digest = row["token_digest"]
        timestamps = (
            row["acquired_at_epoch"],
            row["renewed_at_epoch"],
            row["expires_at_epoch"],
        )
        if raw_digest is None:
            if any(value is not None for value in timestamps):
                raise StorageError("循环赛 runner lease 已损坏")
            digest = None
        else:
            if not isinstance(raw_digest, (bytes, bytearray, memoryview)):
                raise StorageError("循环赛 runner lease 已损坏")
            digest = bytes(raw_digest)
            if len(digest) != 32 or any(
                isinstance(value, bool) or not isinstance(value, int) for value in timestamps
            ):
                raise StorageError("循环赛 runner lease 已损坏")
            acquired_at, renewed_at, expires_at = timestamps
            if not acquired_at <= renewed_at < expires_at:
                raise StorageError("循环赛 runner lease 已损坏")

        return _TournamentRunnerLeaseState(
            generation=generation,
            token_digest=digest,
            acquired_at_epoch=timestamps[0],
            renewed_at_epoch=timestamps[1],
            expires_at_epoch=timestamps[2],
        )

    def _require_active_tournament_runner(
        self,
        connection: sqlite3.Connection,
        tournament_id: str,
        lease: TournamentRunnerLease | None,
        *,
        renew_seconds: int | None = None,
    ) -> TournamentRunnerLease:
        if lease is None:
            raise TournamentRunnerLeaseLostError("循环赛 checkpoint 写入需要有效的 runner lease")
        digest = _validate_runner_lease_handle(lease, tournament_id)
        state = self._load_tournament_runner_lease(connection, tournament_id)
        now = self._database_epoch(connection)
        if (
            state is None
            or state.token_digest is None
            or state.generation != lease.generation
            or state.token_digest != digest
            or state.expires_at_epoch is None
            or state.expires_at_epoch <= now
        ):
            raise TournamentRunnerLeaseLostError(
                "循环赛 runner lease 已过期、释放或被其他执行者接管"
            )

        if renew_seconds is None:
            return TournamentRunnerLease(
                tournament_id=tournament_id,
                generation=state.generation,
                token=lease.token,
                acquired_at_epoch=state.acquired_at_epoch,
                renewed_at_epoch=state.renewed_at_epoch,
                expires_at_epoch=state.expires_at_epoch,
            )

        duration = _validate_runner_lease_seconds(renew_seconds)
        renewed_at = max(now, state.renewed_at_epoch)
        expires_at = max(now + duration, renewed_at + 1)
        updated = connection.execute(
            """
            UPDATE tournament_runner_leases
            SET renewed_at_epoch = ?, expires_at_epoch = ?
            WHERE tournament_id = ? AND generation = ? AND token_digest = ?
            """,
            (
                renewed_at,
                expires_at,
                tournament_id,
                lease.generation,
                digest,
            ),
        )
        if updated.rowcount != 1:
            raise TournamentRunnerLeaseLostError("循环赛 runner lease 在续租时发生并发变化")
        return TournamentRunnerLease(
            tournament_id=tournament_id,
            generation=state.generation,
            token=lease.token,
            acquired_at_epoch=state.acquired_at_epoch,
            renewed_at_epoch=renewed_at,
            expires_at_epoch=expires_at,
        )

    def claim_tournament_runner(
        self,
        tournament_id: str,
        *,
        lease_seconds: int = DEFAULT_TOURNAMENT_RUNNER_LEASE_SECONDS,
    ) -> TournamentRunnerClaim:
        """Atomically reload and claim one in-progress tournament checkpoint."""

        if not isinstance(tournament_id, str) or not tournament_id.strip():
            raise ValueError("tournament_id 必须是非空字符串")
        duration = _validate_runner_lease_seconds(lease_seconds)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            loaded = self._load_tournament_checkpoint(connection, tournament_id)
            if loaded is None:
                raise StorageError(f"循环赛 checkpoint {tournament_id!r} 不存在")
            checkpoint, status = loaded
            if status != "in_progress":
                raise StorageError(f"循环赛 checkpoint {tournament_id!r} 已封存")

            state = self._load_tournament_runner_lease(connection, tournament_id)
            now = self._database_epoch(connection)
            if (
                state is not None
                and state.token_digest is not None
                and state.expires_at_epoch is not None
                and state.expires_at_epoch > now
            ):
                raise TournamentRunnerLeaseBusyError(
                    "循环赛 checkpoint 正由另一个执行者运行；请稍后重试"
                )

            generation = 1 if state is None else state.generation + 1
            token = secrets.token_hex(32)
            digest = _runner_lease_token_digest(token)
            expires_at = now + duration
            if state is None:
                connection.execute(
                    """
                    INSERT INTO tournament_runner_leases (
                        tournament_id, generation, token_digest,
                        acquired_at_epoch, renewed_at_epoch, expires_at_epoch
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (tournament_id, generation, digest, now, now, expires_at),
                )
            else:
                updated = connection.execute(
                    """
                    UPDATE tournament_runner_leases
                    SET generation = ?, token_digest = ?, acquired_at_epoch = ?,
                        renewed_at_epoch = ?, expires_at_epoch = ?
                    WHERE tournament_id = ? AND generation = ?
                    """,
                    (
                        generation,
                        digest,
                        now,
                        now,
                        expires_at,
                        tournament_id,
                        state.generation,
                    ),
                )
                if updated.rowcount != 1:
                    raise TournamentRunnerLeaseBusyError("循环赛 runner lease 在领取时发生并发变化")
            connection.commit()
            return TournamentRunnerClaim(
                checkpoint=checkpoint,
                lease=TournamentRunnerLease(
                    tournament_id=tournament_id,
                    generation=generation,
                    token=token,
                    acquired_at_epoch=now,
                    renewed_at_epoch=now,
                    expires_at_epoch=expires_at,
                ),
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def renew_tournament_runner(
        self,
        lease: TournamentRunnerLease,
        *,
        lease_seconds: int = DEFAULT_TOURNAMENT_RUNNER_LEASE_SECONDS,
    ) -> TournamentRunnerLease:
        """Extend one active lease without reviving an expired generation."""

        if not isinstance(lease, TournamentRunnerLease):
            raise TypeError("必须提供 TournamentRunnerLease")
        duration = _validate_runner_lease_seconds(lease_seconds)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            status_row = connection.execute(
                "SELECT status FROM tournament_checkpoints WHERE tournament_id = ?",
                (lease.tournament_id,),
            ).fetchone()
            if status_row is None or status_row["status"] != "in_progress":
                raise TournamentRunnerLeaseLostError(
                    "循环赛 runner lease 对应的 checkpoint 已不存在或已封存"
                )
            renewed = self._require_active_tournament_runner(
                connection,
                lease.tournament_id,
                lease,
                renew_seconds=duration,
            )
            connection.commit()
            return renewed
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def release_tournament_runner(self, lease: TournamentRunnerLease) -> bool:
        """Release only the matching fencing generation; stale releases are no-ops."""

        if not isinstance(lease, TournamentRunnerLease):
            raise TypeError("必须提供 TournamentRunnerLease")
        digest = _validate_runner_lease_handle(lease, lease.tournament_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE tournament_runner_leases
                SET token_digest = NULL, acquired_at_epoch = NULL,
                    renewed_at_epoch = NULL, expires_at_epoch = NULL
                WHERE tournament_id = ? AND generation = ? AND token_digest = ?
                """,
                (lease.tournament_id, lease.generation, digest),
            )
            connection.commit()
            return updated.rowcount == 1
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def expire_tournament_runner_leases(self) -> int:
        """Clear expired owners while preserving monotonic fencing generations."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = self._database_epoch(connection)
            updated = connection.execute(
                """
                UPDATE tournament_runner_leases
                SET token_digest = NULL, acquired_at_epoch = NULL,
                    renewed_at_epoch = NULL, expires_at_epoch = NULL
                WHERE token_digest IS NOT NULL AND expires_at_epoch <= ?
                """,
                (now,),
            )
            connection.commit()
            return updated.rowcount
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _load_tournament_checkpoint(
        self,
        connection: sqlite3.Connection,
        tournament_id: str,
    ) -> tuple[TournamentCheckpoint, str] | None:
        row = connection.execute(
            """
            SELECT tournament_id, schema_version, source, format,
                   pairing_policy, seed_policy, game, seed, players_json,
                   game_config_json, schedule_json, max_attempts,
                   pairing_count, created_at, updated_at, status,
                   finalized_at, final_tournament_id, config_json
            FROM tournament_checkpoints
            WHERE tournament_id = ?
            """,
            (tournament_id,),
        ).fetchone()
        if row is None:
            return None

        series_rows = connection.execute(
            """
            SELECT pairing_number, series_id, match_1_id, match_2_id,
                   completed_at, series_json
            FROM tournament_checkpoint_series
            WHERE tournament_id = ?
            ORDER BY pairing_number
            """,
            (tournament_id,),
        ).fetchall()
        try:
            semantic_config = self._semantic_checkpoint_config_json(row["config_json"])
            config_payload = json.loads(row["config_json"])
            completed_series: list[SeriesArchive] = []
            for expected_pairing_number, series_row in enumerate(series_rows, start=1):
                if series_row["pairing_number"] != expected_pairing_number:
                    raise StorageError("已完成组编号不是连续前缀")
                series = SeriesArchive.model_validate_json(series_row["series_json"])
                series, _ = self._validate_series(series)
                if (
                    self._semantic_series_json(series_row["series_json"])
                    != _canonical_json(series.model_dump(mode="json"))
                    or series_row["series_id"] != series.series_id
                    or series_row["match_1_id"] != series.legs[0].match_id
                    or series_row["match_2_id"] != series.legs[1].match_id
                    or not self._timestamp_matches(series_row["completed_at"], series.finished_at)
                ):
                    raise StorageError("已完成双局赛索引与档案不一致")
                completed_series.append(series)

            config_payload["completed_series"] = [
                series.model_dump(mode="json") for series in completed_series
            ]
            config_payload["updated_at"] = row["updated_at"]
            checkpoint = TournamentCheckpoint.model_validate(config_payload)
            checkpoint, _ = self._validate_checkpoint(checkpoint)

            payload = checkpoint.model_dump(mode="json")
            stored_players = self._semantic_players_json(row["players_json"], legacy=False)
            stored_game_config = self._semantic_json_column(row["game_config_json"])
            stored_schedule = self._semantic_json_column(row["schedule_json"])
            expected_config = _canonical_json(self._checkpoint_config_payload(checkpoint))
        except (KeyError, TypeError, ValueError, StorageError) as exc:
            raise StorageError(
                f"数据库中 tournament_id {tournament_id!r} 的 checkpoint 已损坏"
            ) from exc

        if (
            semantic_config != expected_config
            or row["tournament_id"] != checkpoint.tournament_id
            or row["schema_version"] != checkpoint.schema_version
            or row["source"] != checkpoint.source
            or row["format"] != checkpoint.format
            or row["pairing_policy"] != checkpoint.pairing_policy
            or row["seed_policy"] != checkpoint.seed_policy
            or row["game"] != checkpoint.game
            or row["seed"] != checkpoint.seed
            or stored_players != _canonical_json(payload["players"])
            or stored_game_config != _canonical_json(payload["game_config"])
            or stored_schedule != _canonical_json(payload["schedule"])
            or row["max_attempts"] != checkpoint.max_attempts
            or row["pairing_count"] != len(checkpoint.schedule)
            or not self._timestamp_matches(row["created_at"], checkpoint.created_at)
            or not self._timestamp_matches(row["updated_at"], checkpoint.updated_at)
            or len(completed_series) > row["pairing_count"]
        ):
            raise StorageError(
                f"数据库中 tournament_id {tournament_id!r} 的 checkpoint 元数据已损坏"
            )

        status = row["status"]
        if status == "in_progress":
            if row["finalized_at"] is not None or row["final_tournament_id"] is not None:
                raise StorageError(
                    f"数据库中 tournament_id {tournament_id!r} 的 checkpoint 状态已损坏"
                )
        elif status == "finalized":
            if (
                row["final_tournament_id"] != tournament_id
                or not checkpoint.is_complete
                or not isinstance(row["finalized_at"], str)
            ):
                raise StorageError(
                    f"数据库中 tournament_id {tournament_id!r} 的 checkpoint 状态已损坏"
                )
            try:
                finalized_at = datetime.fromisoformat(row["finalized_at"])
            except ValueError as exc:
                raise StorageError(
                    f"数据库中 tournament_id {tournament_id!r} 的 checkpoint 状态已损坏"
                ) from exc
            if finalized_at.utcoffset() is None or finalized_at.astimezone(
                UTC
            ) < checkpoint.updated_at.astimezone(UTC):
                raise StorageError(
                    f"数据库中 tournament_id {tournament_id!r} 的 checkpoint 状态已损坏"
                )
            final_row = connection.execute(
                """
                SELECT tournament_json FROM tournament_archives
                WHERE tournament_id = ?
                """,
                (tournament_id,),
            ).fetchone()
            if final_row is None:
                raise StorageError(
                    f"数据库中 tournament_id {tournament_id!r} 的正式循环赛档案已丢失"
                )
            expected_tournament = tournament_from_series(
                checkpoint.players,
                checkpoint.completed_series,
                seed=checkpoint.seed,
                tournament_id=checkpoint.tournament_id,
            )
            try:
                stored_tournament = self._semantic_tournament_json(final_row["tournament_json"])
            except StorageError as exc:
                raise StorageError(
                    f"数据库中 tournament_id {tournament_id!r} 的正式循环赛档案已损坏"
                ) from exc
            if stored_tournament != _canonical_json(expected_tournament.model_dump(mode="json")):
                raise StorageError(
                    f"数据库中 tournament_id {tournament_id!r} 的正式循环赛档案已损坏"
                )
        else:
            raise StorageError(f"数据库中 tournament_id {tournament_id!r} 的 checkpoint 状态已损坏")
        self._verify_checkpoint_entrant_bindings(connection, checkpoint)
        return checkpoint, status

    def save_tournament_checkpoint(
        self,
        checkpoint: TournamentCheckpoint,
        *,
        lease: TournamentRunnerLease | None = None,
        lease_seconds: int = DEFAULT_TOURNAMENT_RUNNER_LEASE_SECONDS,
    ) -> TournamentCheckpointSaveResult:
        """Create an empty checkpoint or append one series under an active lease."""

        checkpoint, _ = self._validate_checkpoint(checkpoint)
        payload = checkpoint.model_dump(mode="json")
        config_json = _canonical_json(self._checkpoint_config_payload(checkpoint))
        pairing_count = len(checkpoint.schedule)
        completed_count = len(checkpoint.completed_series)

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_checkpoint_entrant_bindings(connection, checkpoint)
            loaded = self._load_tournament_checkpoint(
                connection,
                checkpoint.tournament_id,
            )
            if loaded is None:
                if completed_count:
                    raise StorageError("新循环赛 checkpoint 必须在第一组开始前以空进度创建")
                if lease is not None:
                    raise ValueError("新循环赛 checkpoint 必须先创建，再领取 runner lease")
                if connection.execute(
                    "SELECT 1 FROM tournament_archives WHERE tournament_id = ?",
                    (checkpoint.tournament_id,),
                ).fetchone():
                    raise TournamentCheckpointCollisionError(
                        f"tournament_id {checkpoint.tournament_id!r} 已有正式循环赛档案"
                    )
                connection.execute(
                    """
                    INSERT INTO tournament_checkpoints (
                        tournament_id, schema_version, source, format,
                        pairing_policy, seed_policy, game, seed, players_json,
                        game_config_json, schedule_json, max_attempts,
                        pairing_count, created_at, updated_at, status,
                        finalized_at, final_tournament_id, config_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                    """,
                    (
                        checkpoint.tournament_id,
                        checkpoint.schema_version,
                        checkpoint.source,
                        checkpoint.format,
                        checkpoint.pairing_policy,
                        checkpoint.seed_policy,
                        checkpoint.game,
                        checkpoint.seed,
                        _canonical_json(payload["players"]),
                        _canonical_json(payload["game_config"]),
                        _canonical_json(payload["schedule"]),
                        checkpoint.max_attempts,
                        pairing_count,
                        checkpoint.created_at.astimezone(UTC).isoformat(),
                        checkpoint.updated_at.astimezone(UTC).isoformat(),
                        "in_progress",
                        config_json,
                    ),
                )
                connection.commit()
                return TournamentCheckpointSaveResult(
                    inserted=True,
                    completed_pairing_count=0,
                    pairing_count=pairing_count,
                )

            stored, status = loaded
            if status == "in_progress":
                self._require_active_tournament_runner(
                    connection,
                    checkpoint.tournament_id,
                    lease,
                    renew_seconds=lease_seconds,
                )
            stored_config_json = _canonical_json(self._checkpoint_config_payload(stored))
            if stored_config_json != config_json:
                raise TournamentCheckpointCollisionError(
                    f"tournament_id {checkpoint.tournament_id!r} 已对应另一份 checkpoint 配置"
                )
            stored_series_json = tuple(
                _canonical_json(series.model_dump(mode="json"))
                for series in stored.completed_series
            )
            incoming_series_json = tuple(
                _canonical_json(series.model_dump(mode="json"))
                for series in checkpoint.completed_series
            )
            stored_count = len(stored.completed_series)
            if incoming_series_json == stored_series_json:
                connection.commit()
                return TournamentCheckpointSaveResult(
                    inserted=False,
                    completed_pairing_count=stored_count,
                    pairing_count=pairing_count,
                )
            if status != "in_progress":
                raise TournamentCheckpointCollisionError(
                    f"tournament_id {checkpoint.tournament_id!r} 的 checkpoint 已封存"
                )
            if (
                completed_count != stored_count + 1
                or incoming_series_json[:stored_count] != stored_series_json
            ):
                raise TournamentCheckpointCollisionError(
                    "循环赛 checkpoint 只能按赛程连续追加恰好一组双局赛"
                )

            series = checkpoint.completed_series[-1]
            if (
                connection.execute(
                    "SELECT 1 FROM series_archives WHERE series_id = ?",
                    (series.series_id,),
                ).fetchone()
                or connection.execute(
                    "SELECT 1 FROM tournament_checkpoint_series WHERE series_id = ?",
                    (series.series_id,),
                ).fetchone()
            ):
                raise SeriesIdCollisionError(f"series_id {series.series_id!r} 已存档")
            match_ids = (series.legs[0].match_id, series.legs[1].match_id)
            if (
                connection.execute(
                    "SELECT 1 FROM matches WHERE match_id IN (?, ?)",
                    match_ids,
                ).fetchone()
                or connection.execute(
                    """
                SELECT 1 FROM tournament_checkpoint_series
                WHERE match_1_id IN (?, ?)
                   OR match_2_id IN (?, ?)
                """,
                    (*match_ids, *match_ids),
                ).fetchone()
            ):
                raise MatchIdCollisionError("循环赛 checkpoint 的 match_id 已存档")

            connection.execute(
                """
                INSERT INTO tournament_checkpoint_series (
                    tournament_id, pairing_number, series_id, match_1_id,
                    match_2_id, completed_at, series_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint.tournament_id,
                    completed_count,
                    series.series_id,
                    match_ids[0],
                    match_ids[1],
                    series.finished_at.astimezone(UTC).isoformat(),
                    incoming_series_json[-1],
                ),
            )
            updated = connection.execute(
                """
                UPDATE tournament_checkpoints
                SET updated_at = ?
                WHERE tournament_id = ? AND status = 'in_progress'
                """,
                (
                    checkpoint.updated_at.astimezone(UTC).isoformat(),
                    checkpoint.tournament_id,
                ),
            )
            if updated.rowcount != 1:
                raise StorageError("循环赛 checkpoint 状态发生并发变化")
            connection.commit()
            return TournamentCheckpointSaveResult(
                inserted=True,
                completed_pairing_count=completed_count,
                pairing_count=pairing_count,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_tournament_checkpoint(
        self,
        tournament_id: str,
    ) -> TournamentCheckpoint | None:
        """Load and deeply validate one resumable tournament checkpoint."""

        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            try:
                loaded = self._load_tournament_checkpoint(connection, tournament_id)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return None if loaded is None else loaded[0]

    def finalize_tournament_checkpoint(
        self,
        tournament_id: str,
        *,
        lease: TournamentRunnerLease | None = None,
    ) -> TournamentSaveResult:
        """Atomically promote a complete checkpoint and apply tournament ELO once."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            loaded = self._load_tournament_checkpoint(connection, tournament_id)
            if loaded is None:
                raise StorageError(f"循环赛 checkpoint {tournament_id!r} 不存在")
            checkpoint, status = loaded
            if not checkpoint.is_complete:
                raise StorageError("循环赛 checkpoint 尚未完成，不能封存")
            if status == "in_progress":
                self._require_active_tournament_runner(
                    connection,
                    tournament_id,
                    lease,
                    renew_seconds=DEFAULT_TOURNAMENT_RUNNER_LEASE_SECONDS,
                )
            tournament = tournament_from_series(
                checkpoint.players,
                checkpoint.completed_series,
                seed=checkpoint.seed,
                tournament_id=checkpoint.tournament_id,
            )
            if (
                status == "in_progress"
                and connection.execute(
                    "SELECT 1 FROM tournament_archives WHERE tournament_id = ?",
                    (tournament_id,),
                ).fetchone()
            ):
                raise StorageError("进行中的 checkpoint 已存在同 ID 正式循环赛档案")

            result = self._save_tournament_in_transaction(
                connection,
                tournament,
                rating_source="engine",
                checkpoint_owner_id=tournament_id,
            )
            if status == "in_progress":
                if not result.inserted:
                    raise StorageError("进行中的 checkpoint 未能创建正式循环赛档案")
                finalized_at = max(
                    datetime.now(UTC),
                    checkpoint.updated_at.astimezone(UTC),
                )
                updated = connection.execute(
                    """
                    UPDATE tournament_checkpoints
                    SET status = 'finalized', finalized_at = ?,
                        final_tournament_id = ?
                    WHERE tournament_id = ? AND status = 'in_progress'
                    """,
                    (
                        finalized_at.isoformat(),
                        tournament_id,
                        tournament_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise StorageError("循环赛 checkpoint 状态发生并发变化")
                digest = _validate_runner_lease_handle(lease, tournament_id)
                deleted = connection.execute(
                    """
                    DELETE FROM tournament_runner_leases
                    WHERE tournament_id = ? AND generation = ? AND token_digest = ?
                    """,
                    (tournament_id, lease.generation, digest),
                )
                if deleted.rowcount != 1:
                    raise TournamentRunnerLeaseLostError("循环赛 runner lease 在封存时发生并发变化")
            elif result.inserted:
                raise StorageError("已封存 checkpoint 的正式循环赛档案状态已损坏")
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def save_tournament(
        self,
        tournament: TournamentArchive,
        *,
        rating_source: RatingSource = "imported",
    ) -> TournamentSaveResult:
        """Atomically persist and batch-rate one complete round-robin tournament."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = self._save_tournament_in_transaction(
                connection,
                tournament,
                rating_source=rating_source,
            )
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _save_tournament_in_transaction(
        self,
        connection: sqlite3.Connection,
        tournament: TournamentArchive,
        *,
        rating_source: RatingSource,
        checkpoint_owner_id: str | None = None,
    ) -> TournamentSaveResult:
        """Persist a complete tournament using the caller's active write transaction."""

        if not connection.in_transaction:
            raise StorageError("保存循环赛需要调用方先开启 SQLite 写事务")
        rating_source = self._validate_rating_source(rating_source)
        tournament, entrants = self._validate_tournament(tournament)
        trusted_engine = (
            rating_source == "engine"
            and tournament.schema_version == TOURNAMENT_SCHEMA_VERSION
            and tournament.source == "local_engine"
        )
        rated = trusted_engine
        rating_policy = "elo_tournament_batch_v1" if rated else "unrated"
        tournament_payload = tournament.model_dump(mode="json")
        tournament_json = _canonical_json(tournament_payload)
        pairing_count = len(tournament.pairings)
        match_count = pairing_count * 2

        def persist() -> TournamentSaveResult:
            checkpoint_row = connection.execute(
                """
                SELECT status FROM tournament_checkpoints
                WHERE tournament_id = ?
                """,
                (tournament.tournament_id,),
            ).fetchone()
            if (
                checkpoint_row is not None
                and checkpoint_row["status"] == "in_progress"
                and checkpoint_owner_id != tournament.tournament_id
            ):
                raise TournamentIdCollisionError(
                    f"tournament_id {tournament.tournament_id!r} 已由进行中的循环赛 checkpoint 保留"
                )
            existing = connection.execute(
                """
                SELECT tournament_json, archive_source, rating_source, rated,
                       rating_policy
                FROM tournament_archives
                WHERE tournament_id = ?
                """,
                (tournament.tournament_id,),
            ).fetchone()
            if existing is not None:
                try:
                    existing_json = self._semantic_tournament_json(existing["tournament_json"])
                except StorageError as exc:
                    raise StorageError(
                        f"数据库中 tournament_id {tournament.tournament_id!r} 的档案 JSON 已损坏"
                    ) from exc
                if existing_json != tournament_json:
                    raise TournamentIdCollisionError(
                        f"tournament_id {tournament.tournament_id!r} 已对应另一份循环赛档案"
                    )
                stored_rated = bool(existing["rated"])
                expected_stored_rated = (
                    existing["rating_source"] == "engine"
                    and existing["archive_source"] == "local_engine"
                )
                expected_stored_policy = "elo_tournament_batch_v1" if stored_rated else "unrated"
                if (
                    stored_rated != expected_stored_rated
                    or existing["rating_policy"] != expected_stored_policy
                ):
                    raise StorageError(
                        f"数据库中 tournament_id {tournament.tournament_id!r} "
                        "的计分来源或策略已损坏"
                    )
                read_only_downgrade = (
                    existing["rating_source"] == "engine" and rating_source == "imported"
                )
                exact_policy_match = (
                    existing["rating_source"] == rating_source
                    and stored_rated == rated
                    and existing["rating_policy"] == rating_policy
                )
                if existing["archive_source"] != tournament.source or not (
                    read_only_downgrade or exact_policy_match
                ):
                    raise TournamentIdCollisionError(
                        f"tournament_id {tournament.tournament_id!r} "
                        "已以不同来源或计分策略存档，不能通过幂等重存升级"
                    )
                self._verify_existing_tournament(
                    connection,
                    tournament,
                    rating_source=existing["rating_source"],
                    rated=stored_rated,
                    rating_policy=existing["rating_policy"],
                )
                return TournamentSaveResult(
                    inserted=False,
                    rated=stored_rated,
                    pairing_count=pairing_count,
                    match_count=match_count,
                )

            for pairing in tournament.pairings:
                series = pairing.series
                checkpoint_series_owner = connection.execute(
                    """
                    SELECT tcs.tournament_id
                    FROM tournament_checkpoint_series AS tcs
                    JOIN tournament_checkpoints AS tc
                      ON tc.tournament_id = tcs.tournament_id
                    WHERE tc.status = 'in_progress' AND tcs.series_id = ?
                    """,
                    (series.series_id,),
                ).fetchone()
                if (
                    checkpoint_series_owner is not None
                    and checkpoint_series_owner["tournament_id"] != checkpoint_owner_id
                ):
                    raise SeriesIdCollisionError(
                        f"series_id {series.series_id!r} 已由进行中的循环赛 checkpoint 保留"
                    )
                if connection.execute(
                    "SELECT 1 FROM series_archives WHERE series_id = ?",
                    (series.series_id,),
                ).fetchone():
                    raise SeriesIdCollisionError(
                        f"series_id {series.series_id!r} 已存档，不能重复归入循环赛"
                    )
                for leg in series.legs:
                    checkpoint_match_owner = connection.execute(
                        """
                        SELECT tcs.tournament_id
                        FROM tournament_checkpoint_series AS tcs
                        JOIN tournament_checkpoints AS tc
                          ON tc.tournament_id = tcs.tournament_id
                        WHERE tc.status = 'in_progress'
                          AND (tcs.match_1_id = ? OR tcs.match_2_id = ?)
                        """,
                        (leg.match_id, leg.match_id),
                    ).fetchone()
                    if (
                        checkpoint_match_owner is not None
                        and checkpoint_match_owner["tournament_id"] != checkpoint_owner_id
                    ):
                        raise MatchIdCollisionError(
                            f"match_id {leg.match_id!r} 已由进行中的循环赛 checkpoint 保留"
                        )
                    if connection.execute(
                        "SELECT 1 FROM matches WHERE match_id = ?",
                        (leg.match_id,),
                    ).fetchone():
                        raise MatchIdCollisionError(
                            f"match_id {leg.match_id!r} 已存档，不能重复归入循环赛"
                        )

            for entrant in entrants:
                self._upsert_entrant(
                    connection,
                    entrant,
                    observed_at=tournament.started_at,
                    trusted_engine=trusted_engine,
                )

            connection.execute(
                """
                INSERT INTO tournament_archives (
                    tournament_id, schema_version, format, pairing_policy,
                    seed_policy, game, seed, players_json, points_json,
                    pairing_count, rating_policy, k_factor, started_at,
                    finished_at, archive_source, rating_source, rated,
                    tournament_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tournament.tournament_id,
                    tournament.schema_version,
                    tournament.format,
                    tournament.pairing_policy,
                    tournament.seed_policy,
                    tournament.game,
                    tournament.seed,
                    _canonical_json(tournament_payload["players"]),
                    _canonical_json(tournament_payload["points"]),
                    pairing_count,
                    rating_policy,
                    K_FACTOR if rated else None,
                    tournament.started_at.astimezone(UTC).isoformat(),
                    tournament.finished_at.astimezone(UTC).isoformat(),
                    tournament.source,
                    rating_source,
                    int(rated),
                    tournament_json,
                ),
            )
            standings = {standing.entrant_id: standing for standing in tournament.standings}
            for position, (entrant, descriptor) in enumerate(
                zip(entrants, tournament_payload["players"])
            ):
                standing = standings[entrant.entrant_id]
                connection.execute(
                    """
                    INSERT INTO tournament_entrants (
                        tournament_id, position, entrant_id, display_name,
                        descriptor_json, points, series_played, series_wins,
                        series_draws, series_losses, games_played, wins, draws,
                        losses, technical_losses
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tournament.tournament_id,
                        position,
                        entrant.entrant_id,
                        entrant.display_name,
                        _canonical_json(descriptor),
                        standing.points,
                        standing.series_played,
                        standing.series_wins,
                        standing.series_draws,
                        standing.series_losses,
                        standing.games_played,
                        standing.wins,
                        standing.draws,
                        standing.losses,
                        standing.technical_losses,
                    ),
                )

            for pairing in tournament.pairings:
                series = pairing.series
                self._insert_series_structure(
                    connection,
                    series,
                    rating_source=rating_source,
                    rated=rated,
                    rating_policy=rating_policy,
                )
                player_a = entrants[pairing.player_indices[0]]
                player_b = entrants[pairing.player_indices[1]]
                connection.execute(
                    """
                    INSERT INTO tournament_pairings (
                        tournament_id, pairing_number, series_id,
                        entrant_a_id, entrant_b_id
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        tournament.tournament_id,
                        pairing.pairing_number,
                        series.series_id,
                        player_a.entrant_id,
                        player_b.entrant_id,
                    ),
                )

            rating_changes: list[TournamentRatingChange] = []
            if rated:
                rating_changes = self._record_tournament_ratings(
                    connection,
                    tournament,
                    entrants,
                )

            return TournamentSaveResult(
                inserted=True,
                rated=rated,
                pairing_count=pairing_count,
                match_count=match_count,
                rating_changes=tuple(rating_changes),
            )

        return persist()

    def save_series(
        self,
        series: SeriesArchive,
        *,
        rating_source: RatingSource = "imported",
    ) -> SaveResult:
        """原子保存交换顺序的两局档案，并按总局分更新一次 ELO。

        默认 ``rating_source="imported"`` 只存档；只有可信本地引擎来源且显式
        指定 ``rating_source="engine"`` 时计分。
        两局都会出现在普通对局历史中。每局都基于系列开始前的同一 ELO
        期望值计算贡献，最后一次写入榜单，因此各胜一局时双方积分不漂移。
        """

        rating_source = self._validate_rating_source(rating_source)
        series, entrants = self._validate_series(series)
        trusted_engine = (
            rating_source == "engine"
            and series.schema_version == 2
            and series.source == "local_engine"
        )
        rated = trusted_engine
        rating_policy = "elo_batch_v1" if rated else "unrated"
        series_payload = series.model_dump(mode="json")
        series_json = _canonical_json(series_payload)
        semantic_series_json = series_json
        serialized_legs = [self._serialize_archive(leg) for leg in series.legs]

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            checkpoint_owner = connection.execute(
                """
                SELECT tcs.tournament_id
                FROM tournament_checkpoint_series AS tcs
                JOIN tournament_checkpoints AS tc
                  ON tc.tournament_id = tcs.tournament_id
                WHERE tc.status = 'in_progress' AND tcs.series_id = ?
                """,
                (series.series_id,),
            ).fetchone()
            if checkpoint_owner is not None:
                raise SeriesIdCollisionError(
                    f"series_id {series.series_id!r} 已由进行中的循环赛 checkpoint 保留"
                )
            for leg in series.legs:
                checkpoint_match_owner = connection.execute(
                    """
                    SELECT tcs.tournament_id
                    FROM tournament_checkpoint_series AS tcs
                    JOIN tournament_checkpoints AS tc
                      ON tc.tournament_id = tcs.tournament_id
                    WHERE tc.status = 'in_progress'
                      AND (tcs.match_1_id = ? OR tcs.match_2_id = ?)
                    """,
                    (leg.match_id, leg.match_id),
                ).fetchone()
                if checkpoint_match_owner is not None:
                    raise MatchIdCollisionError(
                        f"match_id {leg.match_id!r} 已由进行中的循环赛 checkpoint 保留"
                    )
            existing = connection.execute(
                """
                SELECT series_json, archive_source, rating_source, rated, rating_policy
                FROM series_archives WHERE series_id = ?
                """,
                (series.series_id,),
            ).fetchone()
            if existing is not None:
                try:
                    existing_json = self._semantic_series_json(existing["series_json"])
                except StorageError as exc:
                    raise StorageError(
                        f"数据库中 series_id {series.series_id!r} 的档案 JSON 已损坏"
                    ) from exc
                if existing_json != semantic_series_json:
                    raise SeriesIdCollisionError(
                        f"series_id {series.series_id!r} 已对应另一份系列赛档案"
                    )
                tournament_result = self._verify_existing_tournament_child(
                    connection,
                    requested_rating_source=rating_source,
                    series_id=series.series_id,
                )
                if tournament_result is not None:
                    connection.commit()
                    return SaveResult(
                        inserted=False,
                        rated=tournament_result.rated,
                    )
                stored_rated = bool(existing["rated"])
                expected_stored_rated = existing["rating_source"] == "engine" and existing[
                    "archive_source"
                ] in ("local_engine", "legacy")
                expected_stored_policy = "elo_batch_v1" if stored_rated else "unrated"
                if (
                    stored_rated != expected_stored_rated
                    or existing["rating_policy"] != expected_stored_policy
                ):
                    raise StorageError(
                        f"数据库中 series_id {series.series_id!r} 的计分来源或策略已损坏"
                    )
                read_only_downgrade = (
                    existing["rating_source"] == "engine" and rating_source == "imported"
                )
                exact_policy_match = (
                    existing["rating_source"] == rating_source
                    and stored_rated == rated
                    and existing["rating_policy"] == rating_policy
                )
                historical_engine_repeat = (
                    series.source == "legacy"
                    and existing["rating_source"] == "engine"
                    and rating_source == "engine"
                )
                if existing["archive_source"] != series.source or not (
                    read_only_downgrade or exact_policy_match or historical_engine_repeat
                ):
                    raise SeriesIdCollisionError(
                        f"series_id {series.series_id!r} 已以不同来源或计分策略存档，"
                        "不能通过幂等重存升级"
                    )
                self._verify_existing_series(
                    connection,
                    series,
                    existing["rating_policy"],
                    rated=stored_rated,
                    archive_source=existing["archive_source"],
                    rating_source=existing["rating_source"],
                )
                connection.commit()
                return SaveResult(inserted=False, rated=stored_rated)

            for leg in series.legs:
                if connection.execute(
                    "SELECT 1 FROM matches WHERE match_id = ?", (leg.match_id,)
                ).fetchone():
                    raise MatchIdCollisionError(
                        f"match_id {leg.match_id!r} 已存档，不能重复归入新的系列赛"
                    )

            for entrant in entrants:
                self._upsert_entrant(
                    connection,
                    entrant,
                    observed_at=series.started_at,
                    trusted_engine=trusted_engine,
                )

            connection.execute(
                """
                INSERT INTO series_archives (
                    series_id, schema_version, game, seed, players_json, points_json,
                    rating_policy, started_at, finished_at, series_json
                    , archive_source, rating_source, rated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    series.series_id,
                    series.schema_version,
                    series.game,
                    series.seed,
                    _canonical_json(series_payload["players"]),
                    _canonical_json(series_payload["points"]),
                    rating_policy,
                    series.started_at.astimezone(UTC).isoformat(),
                    series.finished_at.astimezone(UTC).isoformat(),
                    series_json,
                    series.source,
                    rating_source,
                    int(rated),
                ),
            )
            for leg_number, (leg, serialized) in enumerate(
                zip(series.legs, serialized_legs), start=1
            ):
                archive_payload, archive_json = serialized
                leg_entrants = self._validate_archive(leg)
                self._insert_match(
                    connection,
                    leg,
                    leg_entrants,
                    archive_payload,
                    archive_json,
                    rating_source=rating_source,
                    rated=rated,
                    rating_policy=rating_policy,
                )
                connection.execute(
                    """
                    INSERT INTO series_matches (series_id, leg_number, match_id)
                    VALUES (?, ?, ?)
                    """,
                    (series.series_id, leg_number, leg.match_id),
                )

            outcomes_a = tuple(
                head_to_head_point(leg, entrants[0].display_name) for leg in series.legs
            )
            changes: list[RatingChange] = []
            if rated:
                changes.extend(
                    self._record_series_ratings(
                        connection,
                        series,
                        entrants[0],
                        entrants[1],
                        outcomes_a,
                        None,
                    )
                )
                changes.extend(
                    self._record_series_ratings(
                        connection,
                        series,
                        entrants[0],
                        entrants[1],
                        outcomes_a,
                        series.game,
                    )
                )

            connection.commit()
            return SaveResult(inserted=True, rated=rated, rating_changes=tuple(changes))
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _verify_existing_series(
        self,
        connection: sqlite3.Connection,
        series: SeriesArchive,
        rating_policy: str,
        *,
        rated: bool,
        archive_source: str,
        rating_source: str,
    ) -> None:
        expected_policy = "elo_batch_v1" if rated else "unrated"
        expected_rated = rating_source == "engine" and archive_source in (
            "local_engine",
            "legacy",
        )
        if (
            archive_source != series.source
            or rated != expected_rated
            or rating_policy != expected_policy
        ):
            raise StorageError(f"数据库中 series_id {series.series_id!r} 的来源或计分状态已损坏")
        series_row = connection.execute(
            """
            SELECT series_id, schema_version, game, seed, players_json, points_json,
                   started_at, finished_at, archive_source, rating_source, rated,
                   rating_policy
            FROM series_archives
            WHERE series_id = ?
            """,
            (series.series_id,),
        ).fetchone()
        if series_row is None:
            raise StorageError(f"数据库中 series_id {series.series_id!r} 的反规范化元数据已损坏")
        self._verify_series_metadata(
            series_row,
            series,
            rating_source=rating_source,
            rated=rated,
            rating_policy=rating_policy,
        )
        rows = connection.execute(
            """
            SELECT sm.leg_number, sm.match_id, m.schema_version, m.game, m.seed,
                   m.players_json, m.scores_json, m.started_at, m.finished_at,
                   m.archive_json, m.archive_source, m.rating_source, m.rated,
                   m.rating_policy
            FROM series_matches AS sm
            JOIN matches AS m ON m.match_id = sm.match_id
            WHERE sm.series_id = ?
            ORDER BY sm.leg_number
            """,
            (series.series_id,),
        ).fetchall()
        expected_ids = [leg.match_id for leg in series.legs]
        if [row["leg_number"] for row in rows] != [1, 2] or [
            row["match_id"] for row in rows
        ] != expected_ids:
            raise StorageError(f"数据库中 series_id {series.series_id!r} 的对局映射已损坏")
        for row, leg in zip(rows, series.legs):
            self._verify_match_metadata(
                row,
                leg,
                rating_source=rating_source,
                rated=rated,
                rating_policy=rating_policy,
            )
            if (
                row["archive_source"] != archive_source
                or row["rating_source"] != rating_source
                or bool(row["rated"]) != rated
                or row["rating_policy"] != rating_policy
            ):
                raise StorageError(
                    f"数据库中 series_id {series.series_id!r} 的对局来源或计分状态已损坏"
                )
            try:
                stored_json = self._semantic_match_json(row["archive_json"])
            except StorageError as exc:
                raise StorageError(
                    f"数据库中 match_id {leg.match_id!r} 的档案 JSON 已损坏"
                ) from exc
            expected_json = _canonical_json(leg.model_dump(mode="json"))
            if stored_json != expected_json:
                raise StorageError(f"数据库中 series_id {series.series_id!r} 的对局档案已损坏")
            leg_entrants = self._validate_archive(leg)
            self._verify_match_players(connection, leg, leg_entrants)
        history_rows = connection.execute(
            """
            SELECT match_id, rating_scope, game, entrant_id, display_name,
                   opponent_entrant_id, opponent_display_name, outcome,
                   rating_before, rating_after, created_at
            FROM rating_history
            WHERE match_id IN (?, ?)
            """,
            expected_ids,
        ).fetchall()
        if not rated:
            if history_rows:
                raise StorageError(f"数据库中 series_id {series.series_id!r} 的未计分状态已损坏")
            return
        if len(history_rows) != 8:
            raise StorageError(f"数据库中 series_id {series.series_id!r} 的 ELO 历史已损坏")
        history = {
            (row["match_id"], row["rating_scope"], row["game"], row["entrant_id"]): row
            for row in history_rows
        }
        if len(history) != len(history_rows):
            raise StorageError(f"数据库中 series_id {series.series_id!r} 的 ELO 历史已损坏")
        player_a = self._entrant_ref(series.players[0], legacy=series.schema_version == 1)
        player_b = self._entrant_ref(series.players[1], legacy=series.schema_version == 1)
        outcomes_a = tuple(head_to_head_point(leg, player_a.display_name) for leg in series.legs)
        for rating_scope, game_key in (("overall", ""), ("game", series.game)):
            first_a_key = (expected_ids[0], rating_scope, game_key, player_a.entrant_id)
            first_b_key = (expected_ids[0], rating_scope, game_key, player_b.entrant_id)
            if first_a_key not in history or first_b_key not in history:
                raise StorageError(f"数据库中 series_id {series.series_id!r} 的 ELO 历史已损坏")
            try:
                running_a = self._finite_database_float(history[first_a_key]["rating_before"])
                running_b = self._finite_database_float(history[first_b_key]["rating_before"])
            except (TypeError, ValueError) as exc:
                raise StorageError(
                    f"数据库中 series_id {series.series_id!r} 的 ELO 历史已损坏"
                ) from exc
            frozen_expectation = expected_score(running_a, running_b)
            for leg, outcome_a in zip(series.legs, outcomes_a):
                row_a = history.get((leg.match_id, rating_scope, game_key, player_a.entrant_id))
                row_b = history.get((leg.match_id, rating_scope, game_key, player_b.entrant_id))
                if row_a is None or row_b is None:
                    raise StorageError(f"数据库中 series_id {series.series_id!r} 的 ELO 历史已损坏")
                delta_a = K_FACTOR * (outcome_a - frozen_expectation)
                next_a = running_a + delta_a
                next_b = running_b - delta_a
                try:
                    stored_outcome_a = self._finite_database_float(row_a["outcome"])
                    stored_before_a = self._finite_database_float(row_a["rating_before"])
                    stored_after_a = self._finite_database_float(row_a["rating_after"])
                    stored_outcome_b = self._finite_database_float(row_b["outcome"])
                    stored_before_b = self._finite_database_float(row_b["rating_before"])
                    stored_after_b = self._finite_database_float(row_b["rating_after"])
                except (TypeError, ValueError) as exc:
                    raise StorageError(
                        f"数据库中 series_id {series.series_id!r} 的 ELO 历史已损坏"
                    ) from exc
                if (
                    row_a["opponent_entrant_id"] != player_b.entrant_id
                    or row_a["display_name"] != player_a.display_name
                    or row_a["opponent_display_name"] != player_b.display_name
                    or stored_outcome_a != outcome_a
                    or stored_before_a != running_a
                    or stored_after_a != next_a
                    or not self._timestamp_matches(row_a["created_at"], leg.finished_at)
                    or row_b["opponent_entrant_id"] != player_a.entrant_id
                    or row_b["display_name"] != player_b.display_name
                    or row_b["opponent_display_name"] != player_a.display_name
                    or stored_outcome_b != 1.0 - outcome_a
                    or stored_before_b != running_b
                    or stored_after_b != next_b
                    or not self._timestamp_matches(row_b["created_at"], leg.finished_at)
                ):
                    raise StorageError(f"数据库中 series_id {series.series_id!r} 的 ELO 历史已损坏")
                running_a = next_a
                running_b = next_b

    def _validate_checkpoint(
        self, checkpoint: TournamentCheckpoint
    ) -> tuple[TournamentCheckpoint, tuple[_EntrantRef, ...]]:
        if checkpoint.schema_version != TOURNAMENT_CHECKPOINT_SCHEMA_VERSION:
            raise StorageError(
                f"不支持循环赛 checkpoint 版本 {checkpoint.schema_version}；"
                f"当前支持 {TOURNAMENT_CHECKPOINT_SCHEMA_VERSION}"
            )
        try:
            validated = TournamentCheckpoint.model_validate(checkpoint.model_dump(mode="python"))
        except (TypeError, ValueError) as exc:
            raise StorageError(f"循环赛 checkpoint 无效：{exc}") from exc
        entrants = tuple(
            self._entrant_ref(descriptor, legacy=False) for descriptor in validated.players
        )
        if len({entrant.entrant_id for entrant in entrants}) != len(entrants):
            raise StorageError("循环赛 checkpoint 中的 entrant_id 必须唯一")
        for series in validated.completed_series:
            self._validate_series(series)
        return validated, entrants

    def _validate_tournament(
        self, tournament: TournamentArchive
    ) -> tuple[TournamentArchive, tuple[_EntrantRef, ...]]:
        if tournament.schema_version != TOURNAMENT_SCHEMA_VERSION:
            raise StorageError(
                f"不支持循环赛档案版本 {tournament.schema_version}；"
                f"当前支持 {TOURNAMENT_SCHEMA_VERSION}"
            )
        try:
            validated = TournamentArchive.model_validate(tournament.model_dump(mode="python"))
        except (TypeError, ValueError) as exc:
            raise StorageError(f"循环赛档案无效：{exc}") from exc
        entrants = tuple(
            self._entrant_ref(descriptor, legacy=False) for descriptor in validated.players
        )
        if len({entrant.entrant_id for entrant in entrants}) != len(entrants):
            raise StorageError("循环赛档案中的 entrant_id 必须唯一")
        for pairing in validated.pairings:
            self._validate_series(pairing.series)
        return validated, entrants

    def _validate_series(
        self, series: SeriesArchive
    ) -> tuple[SeriesArchive, tuple[_EntrantRef, _EntrantRef]]:
        if series.schema_version not in (1, SERIES_SCHEMA_VERSION):
            raise StorageError(
                f"不支持系列赛档案版本 {series.schema_version}；"
                f"当前支持 1 和 {SERIES_SCHEMA_VERSION}"
            )
        try:
            validated = SeriesArchive.model_validate(series.model_dump(mode="python"))
        except (TypeError, ValueError) as exc:
            raise StorageError(f"系列赛档案无效：{exc}") from exc
        entrants = tuple(
            self._entrant_ref(
                descriptor,
                legacy=validated.schema_version == 1,
            )
            for descriptor in validated.players
        )
        for leg in validated.legs:
            self._validate_archive(leg)
        if len(entrants) != 2:
            raise StorageError("系列赛必须包含恰好两个 entrant_id")
        return validated, (entrants[0], entrants[1])

    def _validate_archive(self, archive: MatchArchive) -> list[_EntrantRef]:
        if archive.schema_version not in (1, 2):
            raise StorageError(f"不支持对局档案版本 {archive.schema_version}；当前支持 1 和 2")
        if archive.schema_version == 1 and archive.source != "legacy":
            raise StorageError("schema v1 档案来源必须是 legacy")
        if archive.schema_version == 2 and archive.source not in (
            "local_engine",
            "external",
        ):
            raise StorageError("schema v2 档案来源必须是 local_engine 或 external")
        try:
            MatchArchive.model_validate(archive.model_dump(mode="python"))
        except (TypeError, ValueError) as exc:
            raise StorageError(f"对局档案无效：{exc}") from exc
        if not SQLITE_INT_MIN <= archive.seed <= SQLITE_INT_MAX:
            raise StorageError(
                f"seed 必须在 SQLite 有符号 64 位整数范围内：{SQLITE_INT_MIN} 到 {SQLITE_INT_MAX}"
            )
        if archive.started_at.utcoffset() is None or archive.finished_at.utcoffset() is None:
            raise StorageError("对局开始和结束时间必须包含时区")
        if archive.finished_at < archive.started_at:
            raise StorageError("对局结束时间不能早于开始时间")
        entrants = [
            self._entrant_ref(descriptor, legacy=archive.schema_version == 1)
            for descriptor in archive.players
        ]
        player_names = [entrant.display_name for entrant in entrants]
        if len(set(player_names)) != len(player_names):
            raise StorageError("对局档案中的选手名字必须唯一")
        if len({entrant.entrant_id for entrant in entrants}) != len(entrants):
            raise StorageError("对局档案中的 entrant_id 必须唯一")
        if set(player_names) != set(archive.scores):
            raise StorageError("对局档案中的选手与 scores 必须完全一致")
        for player, score in archive.scores.items():
            if not math.isfinite(score) or not 0.0 <= score <= 1.0:
                raise StorageError(f"{player} 的比分必须是 0.0 到 1.0 之间的有限数值")
        if not archive.events:
            raise StorageError("对局档案必须包含事件流")
        if [event.seq for event in archive.events] != list(range(len(archive.events))):
            raise StorageError("对局事件 seq 必须从 0 开始且连续递增")
        started_events = [
            event for event in archive.events if event.type == EventType.MATCH_STARTED
        ]
        finished_events = [
            event for event in archive.events if event.type == EventType.MATCH_FINISHED
        ]
        if len(started_events) != 1 or archive.events[0] is not started_events[0]:
            raise StorageError("对局事件流必须以唯一的 match_started 开始")
        if len(finished_events) != 1 or archive.events[-1] is not finished_events[0]:
            raise StorageError("对局事件流必须以唯一的 match_finished 结束")
        started_data = started_events[0].data
        if started_data.get("game") != archive.game or started_data.get("seed") != archive.seed:
            raise StorageError("match_started 的项目或 seed 与档案不一致")
        if "game_config" in started_data and not isinstance(started_data["game_config"], dict):
            raise StorageError("match_started 的 game_config 必须是对象")
        if started_data.get("players") != archive.players:
            raise StorageError("match_started 的选手描述与档案不一致")
        finished_scores = finished_events[0].data.get("scores")
        if not isinstance(finished_scores, dict):
            raise StorageError("match_finished 的比分与档案不一致")
        if any(
            not isinstance(name, str)
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
            for name, score in finished_scores.items()
        ):
            raise StorageError("match_finished 的比分必须是选手名到数值的映射")
        normalized_finished_scores = {name: float(score) for name, score in finished_scores.items()}
        if normalized_finished_scores != archive.scores:
            raise StorageError("match_finished 的比分与档案不一致")

        finished_data = finished_events[0].data
        termination = finished_data.get("termination")
        technical_loss_events = [
            event
            for event in archive.events
            if event.type == EventType.MOVE_REJECTED and event.data.get("technical_loss") is True
        ]
        technical_control_fields = {
            "reason_code",
            "forfeited_by",
            "cause_event_seq",
            "failure_details",
        }
        has_technical_controls = any(field in finished_data for field in technical_control_fields)
        if termination is None:
            if technical_loss_events or has_technical_controls:
                raise StorageError("技术负事件必须包含结构化 termination")
            return entrants  # schema v1 历史档案没有结构化终局原因
        if termination not in ("completed", "technical_loss"):
            raise StorageError("match_finished 的 termination 无效")
        if termination == "completed":
            if technical_loss_events or has_technical_controls:
                raise StorageError("正常结束的档案不能包含技术负控制字段")
            return entrants
        failure_details = finished_data.get("failure_details")
        if failure_details is not None and not isinstance(failure_details, dict):
            raise StorageError("match_finished 的 failure_details 必须是对象")
        forfeited_by = finished_data.get("forfeited_by")
        reason_code = finished_data.get("reason_code")
        reason = finished_data.get("reason")
        cause_event_seq = finished_data.get("cause_event_seq")
        if forfeited_by not in player_names:
            raise StorageError("技术负的 forfeited_by 必须是参赛选手")
        if not isinstance(reason_code, str) or not reason_code:
            raise StorageError("技术负必须包含非空 reason_code")
        if not isinstance(reason, str) or not reason:
            raise StorageError("技术负必须包含非空 reason")
        if (
            isinstance(cause_event_seq, bool)
            or not isinstance(cause_event_seq, int)
            or not 0 <= cause_event_seq < len(archive.events)
        ):
            raise StorageError("技术负必须包含有效 cause_event_seq")
        cause_event = archive.events[cause_event_seq]
        if (
            cause_event.type != EventType.MOVE_REJECTED
            or cause_event.player != forfeited_by
            or cause_event.data.get("reason_code") != reason_code
            or cause_event.data.get("reason") != reason
            or cause_event.data.get("forfeit") is not True
            or cause_event.data.get("forfeit_scope") != "match"
            or cause_event.data.get("technical_loss") is not True
            or cause_event.data.get("failure_details") != failure_details
            or len(technical_loss_events) != 1
            or technical_loss_events[0] is not cause_event
        ):
            raise StorageError("技术负的原因事件与 match_finished 不一致")
        if archive.scores[forfeited_by] != 0.0 or any(
            score != 1.0 for player, score in archive.scores.items() if player != forfeited_by
        ):
            raise StorageError("技术负必须记为责任方 0 分、其他选手 1 分")
        return entrants

    @staticmethod
    def _tournament_rating_ledger(
        tournament: TournamentArchive,
        entrants: tuple[_EntrantRef, ...],
        frozen_ratings: dict[tuple[str, str, str], float],
    ) -> tuple[list[_TournamentContribution], list[_TournamentAggregate]]:
        contributions: list[_TournamentContribution] = []
        aggregates: list[_TournamentAggregate] = []
        for rating_scope, game_key in (("overall", ""), ("game", tournament.game)):
            outcomes: dict[str, list[float]] = {entrant.entrant_id: [] for entrant in entrants}
            deltas: dict[str, list[float]] = {entrant.entrant_id: [] for entrant in entrants}
            for pairing in tournament.pairings:
                player_a = entrants[pairing.player_indices[0]]
                player_b = entrants[pairing.player_indices[1]]
                frozen_a = frozen_ratings[(rating_scope, game_key, player_a.entrant_id)]
                frozen_b = frozen_ratings[(rating_scope, game_key, player_b.entrant_id)]
                expected_a = expected_score(frozen_a, frozen_b)
                for leg_number, leg in enumerate(pairing.series.legs, start=1):
                    sequence = (pairing.pairing_number - 1) * 2 + leg_number
                    outcome_a = head_to_head_point(leg, player_a.display_name)
                    delta_a = K_FACTOR * (outcome_a - expected_a)
                    before_a = frozen_a + math.fsum(deltas[player_a.entrant_id])
                    before_b = frozen_b + math.fsum(deltas[player_b.entrant_id])
                    deltas[player_a.entrant_id].append(delta_a)
                    deltas[player_b.entrant_id].append(-delta_a)
                    after_a = frozen_a + math.fsum(deltas[player_a.entrant_id])
                    after_b = frozen_b + math.fsum(deltas[player_b.entrant_id])
                    outcomes[player_a.entrant_id].append(outcome_a)
                    outcomes[player_b.entrant_id].append(1.0 - outcome_a)
                    contributions.extend(
                        (
                            _TournamentContribution(
                                sequence=sequence,
                                archive=leg,
                                rating_scope=rating_scope,
                                game_key=game_key,
                                player=player_a,
                                opponent=player_b,
                                outcome=outcome_a,
                                frozen_rating=frozen_a,
                                opponent_frozen_rating=frozen_b,
                                expected=expected_a,
                                delta=delta_a,
                                before=before_a,
                                after=after_a,
                            ),
                            _TournamentContribution(
                                sequence=sequence,
                                archive=leg,
                                rating_scope=rating_scope,
                                game_key=game_key,
                                player=player_b,
                                opponent=player_a,
                                outcome=1.0 - outcome_a,
                                frozen_rating=frozen_b,
                                opponent_frozen_rating=frozen_a,
                                expected=1.0 - expected_a,
                                delta=-delta_a,
                                before=before_b,
                                after=after_b,
                            ),
                        )
                    )
            for entrant in entrants:
                before = frozen_ratings[(rating_scope, game_key, entrant.entrant_id)]
                aggregates.append(
                    _TournamentAggregate(
                        rating_scope=rating_scope,
                        game_key=game_key,
                        player=entrant,
                        before=before,
                        after=before + math.fsum(deltas[entrant.entrant_id]),
                        outcomes=tuple(outcomes[entrant.entrant_id]),
                    )
                )
        return contributions, aggregates

    def _record_tournament_ratings(
        self,
        connection: sqlite3.Connection,
        tournament: TournamentArchive,
        entrants: tuple[_EntrantRef, ...],
    ) -> list[TournamentRatingChange]:
        frozen_ratings = {
            (rating_scope, game_key, entrant.entrant_id): self._current_rating(
                connection,
                rating_scope,
                game_key,
                entrant.entrant_id,
            )
            for rating_scope, game_key in (("overall", ""), ("game", tournament.game))
            for entrant in entrants
        }
        contributions, aggregates = self._tournament_rating_ledger(
            tournament,
            entrants,
            frozen_ratings,
        )
        for contribution in contributions:
            connection.execute(
                """
                INSERT INTO rating_history (
                    match_id, rating_scope, game, entrant_id, display_name,
                    opponent_entrant_id, opponent_display_name, outcome,
                    rating_before, rating_after, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contribution.archive.match_id,
                    contribution.rating_scope,
                    contribution.game_key,
                    contribution.player.entrant_id,
                    contribution.player.display_name,
                    contribution.opponent.entrant_id,
                    contribution.opponent.display_name,
                    contribution.outcome,
                    contribution.before,
                    contribution.after,
                    contribution.archive.finished_at.astimezone(UTC).isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO tournament_rating_contributions (
                    tournament_id, sequence, match_id, rating_scope, game,
                    entrant_id, opponent_entrant_id, frozen_rating,
                    opponent_frozen_rating, expected_score, rating_delta
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tournament.tournament_id,
                    contribution.sequence,
                    contribution.archive.match_id,
                    contribution.rating_scope,
                    contribution.game_key,
                    contribution.player.entrant_id,
                    contribution.opponent.entrant_id,
                    contribution.frozen_rating,
                    contribution.opponent_frozen_rating,
                    contribution.expected,
                    contribution.delta,
                ),
            )

        result: list[TournamentRatingChange] = []
        for aggregate in aggregates:
            self._upsert_rating(
                connection,
                rating_scope=aggregate.rating_scope,
                game_key=aggregate.game_key,
                entrant_id=aggregate.player.entrant_id,
                rating=aggregate.after,
                outcomes=aggregate.outcomes,
                updated_at=tournament.finished_at,
            )
            wins = sum(outcome == 1.0 for outcome in aggregate.outcomes)
            draws = sum(outcome == 0.5 for outcome in aggregate.outcomes)
            losses = sum(outcome == 0.0 for outcome in aggregate.outcomes)
            connection.execute(
                """
                INSERT INTO tournament_rating_snapshots (
                    tournament_id, rating_scope, game, entrant_id, display_name,
                    rating_before, rating_after, games_added, wins_added,
                    draws_added, losses_added
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tournament.tournament_id,
                    aggregate.rating_scope,
                    aggregate.game_key,
                    aggregate.player.entrant_id,
                    aggregate.player.display_name,
                    aggregate.before,
                    aggregate.after,
                    len(aggregate.outcomes),
                    wins,
                    draws,
                    losses,
                ),
            )
            result.append(
                TournamentRatingChange(
                    entrant_id=aggregate.player.entrant_id,
                    display_name=aggregate.player.display_name,
                    game=None if aggregate.rating_scope == "overall" else aggregate.game_key,
                    before=aggregate.before,
                    after=aggregate.after,
                    games_added=len(aggregate.outcomes),
                    wins_added=wins,
                    draws_added=draws,
                    losses_added=losses,
                )
            )
        return result

    def _record_ratings(
        self,
        connection: sqlite3.Connection,
        archive: MatchArchive,
        player_a: _EntrantRef,
        player_b: _EntrantRef,
        outcome_a: float,
        game: str | None,
    ) -> list[RatingChange]:
        rating_scope = "overall" if game is None else "game"
        game_key = "" if game is None else game
        before_a = self._current_rating(connection, rating_scope, game_key, player_a.entrant_id)
        before_b = self._current_rating(connection, rating_scope, game_key, player_b.entrant_id)
        after_a, after_b = update_ratings(before_a, before_b, outcome_a)
        changes = [
            RatingChange(
                player=player_a.display_name,
                opponent=player_b.display_name,
                game=game,
                outcome=outcome_a,
                before=before_a,
                after=after_a,
                entrant_id=player_a.entrant_id,
                opponent_entrant_id=player_b.entrant_id,
            ),
            RatingChange(
                player=player_b.display_name,
                opponent=player_a.display_name,
                game=game,
                outcome=1.0 - outcome_a,
                before=before_b,
                after=after_b,
                entrant_id=player_b.entrant_id,
                opponent_entrant_id=player_a.entrant_id,
            ),
        ]
        for change in changes:
            self._upsert_rating(
                connection,
                rating_scope=rating_scope,
                game_key=game_key,
                entrant_id=change.entrant_id,
                rating=change.after,
                outcomes=(change.outcome,),
                updated_at=archive.finished_at,
            )
            connection.execute(
                """
                INSERT INTO rating_history (
                    match_id, rating_scope, game, entrant_id, display_name,
                    opponent_entrant_id, opponent_display_name, outcome,
                    rating_before, rating_after, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    archive.match_id,
                    rating_scope,
                    game_key,
                    change.entrant_id,
                    change.display_name,
                    change.opponent_entrant_id,
                    change.opponent_display_name,
                    change.outcome,
                    change.before,
                    change.after,
                    archive.finished_at.astimezone(UTC).isoformat(),
                ),
            )
        return changes

    def _record_series_ratings(
        self,
        connection: sqlite3.Connection,
        series: SeriesArchive,
        player_a: _EntrantRef,
        player_b: _EntrantRef,
        outcomes_a: tuple[float, ...],
        game: str | None,
    ) -> list[RatingChange]:
        """按系列开始前的同一 ELO 期望值累计各局变化。"""

        if len(outcomes_a) != len(series.legs):
            raise StorageError("系列赛 ELO 局分数量与对局数量不一致")
        rating_scope = "overall" if game is None else "game"
        game_key = "" if game is None else game
        before_a = self._current_rating(connection, rating_scope, game_key, player_a.entrant_id)
        before_b = self._current_rating(connection, rating_scope, game_key, player_b.entrant_id)
        expected_a = expected_score(before_a, before_b)
        deltas_a = tuple(K_FACTOR * (outcome - expected_a) for outcome in outcomes_a)
        outcomes_b = tuple(1.0 - outcome for outcome in outcomes_a)
        running_a = before_a
        running_b = before_b
        history_rows: list[tuple[MatchArchive, _EntrantRef, _EntrantRef, float, float, float]] = []
        for leg, outcome_a, delta_a in zip(series.legs, outcomes_a, deltas_a):
            next_a = running_a + delta_a
            next_b = running_b - delta_a
            history_rows.extend(
                (
                    (leg, player_a, player_b, outcome_a, running_a, next_a),
                    (leg, player_b, player_a, 1.0 - outcome_a, running_b, next_b),
                )
            )
            running_a = next_a
            running_b = next_b
        after_a = running_a
        after_b = running_b

        self._upsert_rating(
            connection,
            rating_scope=rating_scope,
            game_key=game_key,
            entrant_id=player_a.entrant_id,
            rating=after_a,
            outcomes=outcomes_a,
            updated_at=series.finished_at,
        )
        self._upsert_rating(
            connection,
            rating_scope=rating_scope,
            game_key=game_key,
            entrant_id=player_b.entrant_id,
            rating=after_b,
            outcomes=outcomes_b,
            updated_at=series.finished_at,
        )
        for leg, player, opponent, outcome, before, after in history_rows:
            connection.execute(
                """
                INSERT INTO rating_history (
                    match_id, rating_scope, game, entrant_id, display_name,
                    opponent_entrant_id, opponent_display_name, outcome,
                    rating_before, rating_after, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    leg.match_id,
                    rating_scope,
                    game_key,
                    player.entrant_id,
                    player.display_name,
                    opponent.entrant_id,
                    opponent.display_name,
                    outcome,
                    before,
                    after,
                    leg.finished_at.astimezone(UTC).isoformat(),
                ),
            )

        average_a = sum(outcomes_a) / len(outcomes_a)
        return [
            RatingChange(
                player=player_a.display_name,
                opponent=player_b.display_name,
                game=game,
                outcome=average_a,
                before=before_a,
                after=after_a,
                entrant_id=player_a.entrant_id,
                opponent_entrant_id=player_b.entrant_id,
            ),
            RatingChange(
                player=player_b.display_name,
                opponent=player_a.display_name,
                game=game,
                outcome=1.0 - average_a,
                before=before_b,
                after=after_b,
                entrant_id=player_b.entrant_id,
                opponent_entrant_id=player_a.entrant_id,
            ),
        ]

    @staticmethod
    def _upsert_rating(
        connection: sqlite3.Connection,
        *,
        rating_scope: str,
        game_key: str,
        entrant_id: str,
        rating: float,
        outcomes: tuple[float, ...],
        updated_at: datetime,
    ) -> None:
        wins = sum(outcome == 1.0 for outcome in outcomes)
        draws = sum(outcome == 0.5 for outcome in outcomes)
        losses = sum(outcome == 0.0 for outcome in outcomes)
        connection.execute(
            """
            INSERT INTO ratings (
                rating_scope, game, entrant_id, rating, games_played,
                wins, draws, losses, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (rating_scope, game, entrant_id) DO UPDATE SET
                rating = excluded.rating,
                games_played = ratings.games_played + excluded.games_played,
                wins = ratings.wins + excluded.wins,
                draws = ratings.draws + excluded.draws,
                losses = ratings.losses + excluded.losses,
                updated_at = MAX(ratings.updated_at, excluded.updated_at)
            """,
            (
                rating_scope,
                game_key,
                entrant_id,
                rating,
                len(outcomes),
                wins,
                draws,
                losses,
                updated_at.astimezone(UTC).isoformat(),
            ),
        )

    @staticmethod
    def _current_rating(
        connection: sqlite3.Connection, rating_scope: str, game: str, entrant_id: str
    ) -> float:
        row = connection.execute(
            """
            SELECT rating FROM ratings
            WHERE rating_scope = ? AND game = ? AND entrant_id = ?
            """,
            (rating_scope, game, entrant_id),
        ).fetchone()
        return DEFAULT_RATING if row is None else float(row["rating"])

    def get_match(self, match_id: str) -> MatchArchive | None:
        """Load the complete archive for ``match_id``."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT archive_json FROM matches WHERE match_id = ?", (match_id,)
            ).fetchone()
        return None if row is None else MatchArchive.model_validate_json(row["archive_json"])

    def get_series(self, series_id: str) -> SeriesArchive | None:
        """Load the complete two-leg archive for ``series_id``."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT series_json FROM series_archives WHERE series_id = ?",
                (series_id,),
            ).fetchone()
        return None if row is None else SeriesArchive.model_validate_json(row["series_json"])

    def get_tournament(self, tournament_id: str) -> TournamentArchive | None:
        """Load the complete round-robin archive for ``tournament_id``."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT tournament_json FROM tournament_archives
                WHERE tournament_id = ?
                """,
                (tournament_id,),
            ).fetchone()
        return (
            None if row is None else TournamentArchive.model_validate_json(row["tournament_json"])
        )

    def get_verified_tournament(self, tournament_id: str) -> TournamentArchive | None:
        """Load one formal tournament and checkpoint from one consistent snapshot."""

        connection = self._connect()
        try:
            connection.execute("BEGIN")
            loaded_checkpoint = self._load_tournament_checkpoint(connection, tournament_id)
            loaded = self._load_verified_tournament(connection, tournament_id)
            if loaded is not None and (
                loaded_checkpoint is not None and loaded_checkpoint[1] != "finalized"
            ):
                raise StorageError("进行中的 checkpoint 已存在同 ID 正式循环赛档案")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return None if loaded is None else loaded[0]

    def list_matches(self, *, limit: int = 20, game: str | None = None) -> list[MatchSummary]:
        """Return recent persisted matches, newest first."""

        _validate_query_limit(limit)
        sql = """
            SELECT m.match_id, m.game, m.seed, m.players_json, m.scores_json,
                   m.started_at, m.finished_at, m.rating_source, m.rated,
                   sm.series_id, sm.leg_number, tp.tournament_id,
                   tp.pairing_number, ta.pairing_count
            FROM matches AS m
            LEFT JOIN series_matches AS sm ON sm.match_id = m.match_id
            LEFT JOIN tournament_pairings AS tp ON tp.series_id = sm.series_id
            LEFT JOIN tournament_archives AS ta
                   ON ta.tournament_id = tp.tournament_id
        """
        params: list[object] = []
        if game is not None:
            sql += " WHERE m.game = ?"
            params.append(game)
        sql += " ORDER BY m.finished_at DESC, m.match_id DESC LIMIT ?"
        params.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, params).fetchall()
            entrant_ids_by_match: dict[str, list[str]] = {row["match_id"]: [] for row in rows}
            if rows:
                placeholders = ",".join("?" for _ in rows)
                identity_rows = connection.execute(
                    f"""
                    SELECT match_id, entrant_id
                    FROM match_players
                    WHERE match_id IN ({placeholders})
                    ORDER BY match_id, position
                    """,  # noqa: S608 - placeholders are generated, values stay parameterized
                    [row["match_id"] for row in rows],
                ).fetchall()
                for identity_row in identity_rows:
                    entrant_ids_by_match[identity_row["match_id"]].append(
                        identity_row["entrant_id"]
                    )
        return [
            MatchSummary(
                match_id=row["match_id"],
                game=row["game"],
                seed=row["seed"],
                players=tuple(player["name"] for player in json.loads(row["players_json"])),
                entrant_ids=tuple(entrant_ids_by_match[row["match_id"]]),
                scores={
                    name: float(score) for name, score in json.loads(row["scores_json"]).items()
                },
                started_at=datetime.fromisoformat(row["started_at"]),
                finished_at=datetime.fromisoformat(row["finished_at"]),
                series_id=row["series_id"],
                leg_number=row["leg_number"],
                rating_source=row["rating_source"],
                rated=bool(row["rated"]),
                tournament_id=row["tournament_id"],
                pairing_number=row["pairing_number"],
                pairing_count=row["pairing_count"],
            )
            for row in rows
        ]

    def leaderboard(self, *, game: str | None = None, limit: int = 50) -> list[RatingEntry]:
        """Return the overall leaderboard or the leaderboard for one game."""

        _validate_query_limit(limit)
        rating_scope = "overall" if game is None else "game"
        game_key = "" if game is None else game
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT r.entrant_id, e.display_name, r.rating, r.games_played,
                       r.wins, r.draws, r.losses, r.updated_at
                FROM ratings AS r
                JOIN entrants AS e ON e.entrant_id = r.entrant_id
                WHERE r.rating_scope = ? AND r.game = ?
                ORDER BY r.rating DESC, r.games_played DESC,
                         e.display_name ASC, r.entrant_id ASC
                LIMIT ?
                """,
                (rating_scope, game_key, limit),
            ).fetchall()
        return [
            RatingEntry(
                player=row["display_name"],
                entrant_id=row["entrant_id"],
                rating=float(row["rating"]),
                games_played=row["games_played"],
                wins=row["wins"],
                draws=row["draws"],
                losses=row["losses"],
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]
