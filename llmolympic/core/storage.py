"""SQLite persistence for match archives and ELO ratings.

The complete :class:`~llmolympic.core.archive.MatchArchive` JSON is the
canonical record.  A small amount of metadata is stored alongside it for fast
history and leaderboard queries.  Saving a match and updating both the
per-game and overall ELO tables happens in one transaction.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import warnings
from contextlib import closing
from dataclasses import dataclass
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

SCHEMA_VERSION = 3
RatingSource = Literal["engine", "imported"]
SQLITE_INT_MIN = -(2**63)
SQLITE_INT_MAX = 2**63 - 1
MAX_QUERY_LIMIT = 1000
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_SQLITE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
_SAFE_PROFILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
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

_REQUIRED_COLUMNS = {
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


class UnsupportedSchemaError(StorageError):
    """The database was created by a newer, unsupported schema version."""


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


@dataclass(frozen=True)
class _EntrantRef:
    entrant_id: str
    display_name: str
    identity_json: str


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
            pending.extend(
                (f"{path}[{index}]", nested)
                for index, nested in enumerate(candidate)
            )
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
                self._verify_schema(connection)
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
    def _verify_schema(connection: sqlite3.Connection) -> None:
        for table, required in _REQUIRED_COLUMNS.items():
            columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            missing = required - columns
            if missing:
                names = ", ".join(sorted(missing))
                raise StorageError(f"SQLite 数据库结构不完整：{table} 缺少 {names}")

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
            raise StorageError(
                f"entrant_id {entrant.entrant_id!r} 的观察时间已损坏"
            ) from exc
        if existing_observed_at.utcoffset() is None:
            raise StorageError(f"entrant_id {entrant.entrant_id!r} 的观察时间已损坏")
        is_newer_observation = (
            observed_at.astimezone(UTC) > existing_observed_at.astimezone(UTC)
        )
        has_trusted_observation = connection.execute(
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
            (entrant.entrant_id,),
        ).fetchone()[0]
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
            raise StorageError(
                f"数据库中 match_id {archive.match_id!r} 的反规范化元数据已损坏"
            )

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
            raise StorageError(
                f"数据库中 series_id {series.series_id!r} 的反规范化元数据已损坏"
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
                raise StorageError(
                    f"数据库中 match_id {archive.match_id!r} 的未计分状态已损坏"
                )
            return
        if len(entrants) != 2 or len(rows) != 4:
            raise StorageError(f"数据库中 match_id {archive.match_id!r} 的 ELO 历史已损坏")
        history = {
            (row["rating_scope"], row["game"], row["entrant_id"]): row for row in rows
        }
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
                raise StorageError(
                    f"数据库中 match_id {archive.match_id!r} 的 ELO 历史已损坏"
                )
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
                raise StorageError(
                    f"数据库中 match_id {archive.match_id!r} 的 ELO 历史已损坏"
                )

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

    def _verify_existing_series_leg(
        self,
        connection: sqlite3.Connection,
        archive: MatchArchive,
        *,
        requested_rating_source: RatingSource,
    ) -> SaveResult | None:
        row = connection.execute(
            """
            SELECT sa.series_json, sa.archive_source, sa.rating_source,
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
        stored_rated = bool(row["rated"])
        expected_policy = "elo_batch_v1" if stored_rated else "unrated"
        expected_rated = row["rating_source"] == "engine" and row[
            "archive_source"
        ] in ("local_engine", "legacy")
        if row["rating_policy"] != expected_policy or stored_rated != expected_rated:
            raise StorageError(
                f"数据库中 match_id {archive.match_id!r} 所属系列赛计分状态已损坏"
            )
        if row["archive_source"] != archive.source or (
            requested_rating_source == "engine" and row["rating_source"] != "engine"
        ):
            raise MatchIdCollisionError(
                f"match_id {archive.match_id!r} 已以不同来源或计分策略存档，"
                "不能通过幂等重存升级"
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
            raise StorageError(
                f"数据库中 series_id {series.series_id!r} 的来源或计分状态已损坏"
            )
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
            raise StorageError(
                f"数据库中 series_id {series.series_id!r} 的反规范化元数据已损坏"
            )
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
                running_a = self._finite_database_float(
                    history[first_a_key]["rating_before"]
                )
                running_b = self._finite_database_float(
                    history[first_b_key]["rating_before"]
                )
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
                updated_at = excluded.updated_at
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

    def list_matches(self, *, limit: int = 20, game: str | None = None) -> list[MatchSummary]:
        """Return recent persisted matches, newest first."""

        _validate_query_limit(limit)
        sql = """
            SELECT m.match_id, m.game, m.seed, m.players_json, m.scores_json,
                   m.started_at, m.finished_at, m.rating_source, m.rated,
                   sm.series_id, sm.leg_number
            FROM matches AS m
            LEFT JOIN series_matches AS sm ON sm.match_id = m.match_id
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
