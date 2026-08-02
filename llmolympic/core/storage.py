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
import sqlite3
import warnings
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from llmolympic.config import get as cfg_get
from llmolympic.core.archive import ARCHIVE_SCHEMA_VERSION, MatchArchive
from llmolympic.core.elo import DEFAULT_RATING, K_FACTOR, expected_score, update_ratings
from llmolympic.core.events import EventType
from llmolympic.core.series import SERIES_SCHEMA_VERSION, SeriesArchive, head_to_head_point

SCHEMA_VERSION = 2
SQLITE_INT_MIN = -(2**63)
SQLITE_INT_MAX = 2**63 - 1
MAX_QUERY_LIMIT = 1000
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_SQLITE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")

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
                if locked_version in (0, 1):
                    self._create_series_schema(connection)
                self._verify_schema(connection)
                if locked_version < SCHEMA_VERSION:
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _create_base_schema(connection: sqlite3.Connection) -> None:
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
            CREATE TABLE IF NOT EXISTS ratings (
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
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS ratings_leaderboard_idx
            ON ratings(rating_scope, game, rating DESC, games_played DESC, player)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rating_history (
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
    def _verify_schema(connection: sqlite3.Connection) -> None:
        for table, required in _REQUIRED_COLUMNS.items():
            columns = {
                row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            missing = required - columns
            if missing:
                names = ", ".join(sorted(missing))
                raise StorageError(f"SQLite 数据库结构不完整：{table} 缺少 {names}")

    def save_match(self, archive: MatchArchive) -> SaveResult:
        """Persist one completed match and atomically update its ELO ratings.

        Re-saving an identical ``match_id`` is an idempotent no-op.  Reusing an
        id for different content raises :class:`MatchIdCollisionError`.
        Matches with any player count other than two are archived but unrated.
        """

        player_names = self._validate_archive(archive)
        archive_payload, archive_json = self._serialize_archive(archive)

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT archive_json FROM matches WHERE match_id = ?", (archive.match_id,)
            ).fetchone()
            if existing is not None:
                try:
                    existing_json = _canonical_json(json.loads(existing["archive_json"]))
                except (json.JSONDecodeError, StorageError) as exc:
                    raise StorageError(
                        f"数据库中 match_id {archive.match_id!r} 的档案 JSON 已损坏"
                    ) from exc
                if existing_json != archive_json:
                    raise MatchIdCollisionError(
                        f"match_id {archive.match_id!r} 已对应另一份对局档案"
                    )
                connection.commit()
                return SaveResult(inserted=False, rated=len(player_names) == 2)

            self._insert_match(
                connection,
                archive,
                player_names,
                archive_payload,
                archive_json,
            )

            changes: list[RatingChange] = []
            if len(player_names) == 2:
                score_a = archive.scores[player_names[0]]
                score_b = archive.scores[player_names[1]]
                outcome_a = 1.0 if score_a > score_b else 0.0 if score_a < score_b else 0.5
                changes.extend(
                    self._record_ratings(
                        connection, archive, player_names[0], player_names[1], outcome_a, None
                    )
                )
                changes.extend(
                    self._record_ratings(
                        connection,
                        archive,
                        player_names[0],
                        player_names[1],
                        outcome_a,
                        archive.game,
                    )
                )

            connection.commit()
            return SaveResult(
                inserted=True,
                rated=len(player_names) == 2,
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
        player_names: list[str],
        archive_payload: dict,
        archive_json: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO matches (
                match_id, schema_version, game, seed, players_json, scores_json,
                started_at, finished_at, archive_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                archive_json,
            ),
        )
        for position, (player, descriptor) in enumerate(
            zip(player_names, archive_payload["players"])
        ):
            connection.execute(
                """
                INSERT INTO match_players (
                    match_id, position, player, descriptor_json, score
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    archive.match_id,
                    position,
                    player,
                    _canonical_json(descriptor),
                    archive.scores[player],
                ),
            )

    def save_series(self, series: SeriesArchive) -> SaveResult:
        """原子保存交换顺序的两局档案，并按总局分更新一次 ELO。

        两局都会出现在普通对局历史中。每局都基于系列开始前的同一 ELO
        期望值计算贡献，最后一次写入榜单，因此各胜一局时双方积分不漂移。
        """

        series, player_names = self._validate_series(series)
        series_payload = series.model_dump(mode="json")
        series_json = _canonical_json(series_payload)
        serialized_legs = [self._serialize_archive(leg) for leg in series.legs]

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT series_json, rating_policy FROM series_archives WHERE series_id = ?",
                (series.series_id,),
            ).fetchone()
            if existing is not None:
                try:
                    existing_json = _canonical_json(json.loads(existing["series_json"]))
                except (json.JSONDecodeError, StorageError) as exc:
                    raise StorageError(
                        f"数据库中 series_id {series.series_id!r} 的档案 JSON 已损坏"
                    ) from exc
                if existing_json != series_json:
                    raise SeriesIdCollisionError(
                        f"series_id {series.series_id!r} 已对应另一份系列赛档案"
                    )
                self._verify_existing_series(connection, series, existing["rating_policy"])
                connection.commit()
                return SaveResult(inserted=False, rated=True)

            for leg in series.legs:
                if connection.execute(
                    "SELECT 1 FROM matches WHERE match_id = ?", (leg.match_id,)
                ).fetchone():
                    raise MatchIdCollisionError(
                        f"match_id {leg.match_id!r} 已存档，不能重复归入新的系列赛"
                    )

            connection.execute(
                """
                INSERT INTO series_archives (
                    series_id, schema_version, game, seed, players_json, points_json,
                    rating_policy, started_at, finished_at, series_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    series.series_id,
                    series.schema_version,
                    series.game,
                    series.seed,
                    _canonical_json(series_payload["players"]),
                    _canonical_json(series_payload["points"]),
                    "elo_batch_v1",
                    series.started_at.astimezone(UTC).isoformat(),
                    series.finished_at.astimezone(UTC).isoformat(),
                    series_json,
                ),
            )
            for leg_number, (leg, serialized) in enumerate(
                zip(series.legs, serialized_legs), start=1
            ):
                archive_payload, archive_json = serialized
                leg_names = [descriptor["name"] for descriptor in leg.players]
                self._insert_match(
                    connection,
                    leg,
                    leg_names,
                    archive_payload,
                    archive_json,
                )
                connection.execute(
                    """
                    INSERT INTO series_matches (series_id, leg_number, match_id)
                    VALUES (?, ?, ?)
                    """,
                    (series.series_id, leg_number, leg.match_id),
                )

            outcomes_a = tuple(
                head_to_head_point(leg, player_names[0]) for leg in series.legs
            )
            changes: list[RatingChange] = []
            changes.extend(
                self._record_series_ratings(
                    connection,
                    series,
                    player_names[0],
                    player_names[1],
                    outcomes_a,
                    None,
                )
            )
            changes.extend(
                self._record_series_ratings(
                    connection,
                    series,
                    player_names[0],
                    player_names[1],
                    outcomes_a,
                    series.game,
                )
            )

            connection.commit()
            return SaveResult(inserted=True, rated=True, rating_changes=tuple(changes))
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _verify_existing_series(
        connection: sqlite3.Connection,
        series: SeriesArchive,
        rating_policy: str,
    ) -> None:
        if rating_policy != "elo_batch_v1":
            raise StorageError(
                f"数据库中 series_id {series.series_id!r} 的 ELO 策略已损坏"
            )
        rows = connection.execute(
            """
            SELECT sm.leg_number, sm.match_id, m.archive_json
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
            raise StorageError(
                f"数据库中 series_id {series.series_id!r} 的对局映射已损坏"
            )
        for row, leg in zip(rows, series.legs):
            try:
                stored_json = _canonical_json(json.loads(row["archive_json"]))
            except (json.JSONDecodeError, StorageError) as exc:
                raise StorageError(
                    f"数据库中 match_id {leg.match_id!r} 的档案 JSON 已损坏"
                ) from exc
            expected_json = _canonical_json(leg.model_dump(mode="json"))
            if stored_json != expected_json:
                raise StorageError(
                    f"数据库中 series_id {series.series_id!r} 的对局档案已损坏"
                )
        history_rows = connection.execute(
            """
            SELECT match_id, rating_scope, game, player, opponent, outcome,
                   rating_before, rating_after, created_at
            FROM rating_history
            WHERE match_id IN (?, ?)
            """,
            expected_ids,
        ).fetchall()
        if len(history_rows) != 8:
            raise StorageError(
                f"数据库中 series_id {series.series_id!r} 的 ELO 历史已损坏"
            )
        history = {
            (row["match_id"], row["rating_scope"], row["game"], row["player"]): row
            for row in history_rows
        }
        player_a, player_b = (descriptor["name"] for descriptor in series.players)
        outcomes_a = tuple(head_to_head_point(leg, player_a) for leg in series.legs)
        for rating_scope, game_key in (("overall", ""), ("game", series.game)):
            first_a_key = (expected_ids[0], rating_scope, game_key, player_a)
            first_b_key = (expected_ids[0], rating_scope, game_key, player_b)
            if first_a_key not in history or first_b_key not in history:
                raise StorageError(
                    f"数据库中 series_id {series.series_id!r} 的 ELO 历史已损坏"
                )
            running_a = float(history[first_a_key]["rating_before"])
            running_b = float(history[first_b_key]["rating_before"])
            frozen_expectation = expected_score(running_a, running_b)
            for leg, outcome_a in zip(series.legs, outcomes_a):
                row_a = history.get((leg.match_id, rating_scope, game_key, player_a))
                row_b = history.get((leg.match_id, rating_scope, game_key, player_b))
                if row_a is None or row_b is None:
                    raise StorageError(
                        f"数据库中 series_id {series.series_id!r} 的 ELO 历史已损坏"
                    )
                delta_a = K_FACTOR * (outcome_a - frozen_expectation)
                next_a = running_a + delta_a
                next_b = running_b - delta_a
                expected_created_at = leg.finished_at.astimezone(UTC).isoformat()
                if (
                    row_a["opponent"] != player_b
                    or float(row_a["outcome"]) != outcome_a
                    or float(row_a["rating_before"]) != running_a
                    or float(row_a["rating_after"]) != next_a
                    or row_a["created_at"] != expected_created_at
                    or row_b["opponent"] != player_a
                    or float(row_b["outcome"]) != 1.0 - outcome_a
                    or float(row_b["rating_before"]) != running_b
                    or float(row_b["rating_after"]) != next_b
                    or row_b["created_at"] != expected_created_at
                ):
                    raise StorageError(
                        f"数据库中 series_id {series.series_id!r} 的 ELO 历史已损坏"
                    )
                running_a = next_a
                running_b = next_b

    def _validate_series(
        self, series: SeriesArchive
    ) -> tuple[SeriesArchive, tuple[str, str]]:
        if series.schema_version != SERIES_SCHEMA_VERSION:
            raise StorageError(
                f"不支持系列赛档案版本 {series.schema_version}；"
                f"当前仅支持 {SERIES_SCHEMA_VERSION}"
            )
        try:
            validated = SeriesArchive.model_validate(series.model_dump(mode="python"))
        except (TypeError, ValueError) as exc:
            raise StorageError(f"系列赛档案无效：{exc}") from exc
        names = tuple(descriptor["name"] for descriptor in validated.players)
        for leg in validated.legs:
            self._validate_archive(leg)
        return validated, names

    @staticmethod
    def _validate_archive(archive: MatchArchive) -> list[str]:
        if archive.schema_version != ARCHIVE_SCHEMA_VERSION:
            raise StorageError(
                f"不支持对局档案版本 {archive.schema_version}；"
                f"当前仅支持 {ARCHIVE_SCHEMA_VERSION}"
            )
        if not SQLITE_INT_MIN <= archive.seed <= SQLITE_INT_MAX:
            raise StorageError(
                f"seed 必须在 SQLite 有符号 64 位整数范围内："
                f"{SQLITE_INT_MIN} 到 {SQLITE_INT_MAX}"
            )
        if archive.started_at.utcoffset() is None or archive.finished_at.utcoffset() is None:
            raise StorageError("对局开始和结束时间必须包含时区")
        if archive.finished_at < archive.started_at:
            raise StorageError("对局结束时间不能早于开始时间")
        player_names: list[str] = []
        for descriptor in archive.players:
            name = descriptor.get("name")
            if not isinstance(name, str) or not name:
                raise StorageError("每个选手描述都必须包含非空 name")
            player_names.append(name)
        if len(set(player_names)) != len(player_names):
            raise StorageError("对局档案中的选手名字必须唯一")
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
        if "game_config" in started_data and not isinstance(
            started_data["game_config"], dict
        ):
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
        normalized_finished_scores = {
            name: float(score) for name, score in finished_scores.items()
        }
        if normalized_finished_scores != archive.scores:
            raise StorageError("match_finished 的比分与档案不一致")

        finished_data = finished_events[0].data
        termination = finished_data.get("termination")
        technical_loss_events = [
            event
            for event in archive.events
            if event.type == EventType.MOVE_REJECTED
            and event.data.get("technical_loss") is True
        ]
        technical_control_fields = {
            "reason_code",
            "forfeited_by",
            "cause_event_seq",
            "failure_details",
        }
        has_technical_controls = any(
            field in finished_data for field in technical_control_fields
        )
        if termination is None:
            if technical_loss_events or has_technical_controls:
                raise StorageError("技术负事件必须包含结构化 termination")
            return player_names  # schema v1 历史档案没有结构化终局原因
        if termination not in ("completed", "technical_loss"):
            raise StorageError("match_finished 的 termination 无效")
        if termination == "completed":
            if technical_loss_events or has_technical_controls:
                raise StorageError("正常结束的档案不能包含技术负控制字段")
            return player_names
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
            score != 1.0
            for player, score in archive.scores.items()
            if player != forfeited_by
        ):
            raise StorageError("技术负必须记为责任方 0 分、其他选手 1 分")
        return player_names

    def _record_ratings(
        self,
        connection: sqlite3.Connection,
        archive: MatchArchive,
        player_a: str,
        player_b: str,
        outcome_a: float,
        game: str | None,
    ) -> list[RatingChange]:
        rating_scope = "overall" if game is None else "game"
        game_key = "" if game is None else game
        before_a = self._current_rating(connection, rating_scope, game_key, player_a)
        before_b = self._current_rating(connection, rating_scope, game_key, player_b)
        after_a, after_b = update_ratings(before_a, before_b, outcome_a)
        changes = [
            RatingChange(player_a, player_b, game, outcome_a, before_a, after_a),
            RatingChange(player_b, player_a, game, 1.0 - outcome_a, before_b, after_b),
        ]
        for change in changes:
            self._upsert_rating(
                connection,
                rating_scope=rating_scope,
                game_key=game_key,
                player=change.player,
                rating=change.after,
                outcomes=(change.outcome,),
                updated_at=archive.finished_at,
            )
            connection.execute(
                """
                INSERT INTO rating_history (
                    match_id, rating_scope, game, player, opponent, outcome,
                    rating_before, rating_after, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    archive.match_id,
                    rating_scope,
                    game_key,
                    change.player,
                    change.opponent,
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
        player_a: str,
        player_b: str,
        outcomes_a: tuple[float, ...],
        game: str | None,
    ) -> list[RatingChange]:
        """按系列开始前的同一 ELO 期望值累计各局变化。"""

        if len(outcomes_a) != len(series.legs):
            raise StorageError("系列赛 ELO 局分数量与对局数量不一致")
        rating_scope = "overall" if game is None else "game"
        game_key = "" if game is None else game
        before_a = self._current_rating(connection, rating_scope, game_key, player_a)
        before_b = self._current_rating(connection, rating_scope, game_key, player_b)
        expected_a = expected_score(before_a, before_b)
        deltas_a = tuple(K_FACTOR * (outcome - expected_a) for outcome in outcomes_a)
        outcomes_b = tuple(1.0 - outcome for outcome in outcomes_a)
        running_a = before_a
        running_b = before_b
        history_rows: list[tuple[MatchArchive, str, str, float, float, float]] = []
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
            player=player_a,
            rating=after_a,
            outcomes=outcomes_a,
            updated_at=series.finished_at,
        )
        self._upsert_rating(
            connection,
            rating_scope=rating_scope,
            game_key=game_key,
            player=player_b,
            rating=after_b,
            outcomes=outcomes_b,
            updated_at=series.finished_at,
        )
        for leg, player, opponent, outcome, before, after in history_rows:
            connection.execute(
                """
                INSERT INTO rating_history (
                    match_id, rating_scope, game, player, opponent, outcome,
                    rating_before, rating_after, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    leg.match_id,
                    rating_scope,
                    game_key,
                    player,
                    opponent,
                    outcome,
                    before,
                    after,
                    leg.finished_at.astimezone(UTC).isoformat(),
                ),
            )

        average_a = sum(outcomes_a) / len(outcomes_a)
        return [
            RatingChange(player_a, player_b, game, average_a, before_a, after_a),
            RatingChange(player_b, player_a, game, 1.0 - average_a, before_b, after_b),
        ]

    @staticmethod
    def _upsert_rating(
        connection: sqlite3.Connection,
        *,
        rating_scope: str,
        game_key: str,
        player: str,
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
                rating_scope, game, player, rating, games_played,
                wins, draws, losses, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (rating_scope, game, player) DO UPDATE SET
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
                player,
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
        connection: sqlite3.Connection, rating_scope: str, game: str, player: str
    ) -> float:
        row = connection.execute(
            """
            SELECT rating FROM ratings
            WHERE rating_scope = ? AND game = ? AND player = ?
            """,
            (rating_scope, game, player),
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
                   m.started_at, m.finished_at, sm.series_id, sm.leg_number
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
        return [
            MatchSummary(
                match_id=row["match_id"],
                game=row["game"],
                seed=row["seed"],
                players=tuple(player["name"] for player in json.loads(row["players_json"])),
                scores={name: float(score) for name, score in json.loads(row["scores_json"]).items()},
                started_at=datetime.fromisoformat(row["started_at"]),
                finished_at=datetime.fromisoformat(row["finished_at"]),
                series_id=row["series_id"],
                leg_number=row["leg_number"],
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
                SELECT player, rating, games_played, wins, draws, losses, updated_at
                FROM ratings
                WHERE rating_scope = ? AND game = ?
                ORDER BY rating DESC, games_played DESC, player ASC
                LIMIT ?
                """,
                (rating_scope, game_key, limit),
            ).fetchall()
        return [
            RatingEntry(
                player=row["player"],
                rating=float(row["rating"]),
                games_played=row["games_played"],
                wins=row["wins"],
                draws=row["draws"],
                losses=row["losses"],
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]
