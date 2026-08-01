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
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from llmolympic.config import get as cfg_get
from llmolympic.core.archive import ARCHIVE_SCHEMA_VERSION, MatchArchive
from llmolympic.core.elo import DEFAULT_RATING, update_ratings
from llmolympic.core.events import EventType

SCHEMA_VERSION = 1
SQLITE_INT_MIN = -(2**63)
SQLITE_INT_MAX = 2**63 - 1

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
}


class StorageError(RuntimeError):
    """Base exception for persistence failures."""


class MatchIdCollisionError(StorageError):
    """A match id is already attached to a different archive."""


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


class SQLiteStore:
    """Persistent match archive and ELO repository backed by SQLite."""

    def __init__(self, path: str | Path | None = None, *, create: bool = True) -> None:
        self.path = database_path(path)
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        elif not self.path.is_file():
            raise StorageError(f"数据库不存在：{self.path}")
        self._initialize(create=create)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
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
            if version == 0:
                if not create:
                    raise StorageError(f"数据库尚未初始化：{self.path}")
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;

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
                    );

                    CREATE INDEX IF NOT EXISTS matches_finished_at_idx
                        ON matches(finished_at DESC);
                    CREATE INDEX IF NOT EXISTS matches_game_finished_at_idx
                        ON matches(game, finished_at DESC);

                    CREATE TABLE IF NOT EXISTS match_players (
                        match_id TEXT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
                        position INTEGER NOT NULL,
                        player TEXT NOT NULL,
                        descriptor_json TEXT NOT NULL,
                        score REAL NOT NULL,
                        PRIMARY KEY (match_id, position)
                    );

                    CREATE INDEX IF NOT EXISTS match_players_player_idx
                        ON match_players(player, match_id);

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
                    );

                    CREATE INDEX IF NOT EXISTS ratings_leaderboard_idx
                        ON ratings(rating_scope, game, rating DESC, games_played DESC, player);

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
                    );

                    PRAGMA user_version = 1;
                    COMMIT;
                    """
                )
            self._verify_schema(connection)

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
        archive_payload = archive.model_dump(mode="json")
        archive_json = _canonical_json(archive_payload)
        players_json = _canonical_json(archive_payload["players"])
        scores_json = _canonical_json(archive_payload["scores"])
        started_at = archive.started_at.astimezone(UTC).isoformat()
        finished_at = archive.finished_at.astimezone(UTC).isoformat()

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
                    players_json,
                    scores_json,
                    started_at,
                    finished_at,
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
            win = int(change.outcome == 1.0)
            draw = int(change.outcome == 0.5)
            loss = int(change.outcome == 0.0)
            connection.execute(
                """
                INSERT INTO ratings (
                    rating_scope, game, player, rating, games_played,
                    wins, draws, losses, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT (rating_scope, game, player) DO UPDATE SET
                    rating = excluded.rating,
                    games_played = ratings.games_played + 1,
                    wins = ratings.wins + excluded.wins,
                    draws = ratings.draws + excluded.draws,
                    losses = ratings.losses + excluded.losses,
                    updated_at = excluded.updated_at
                """,
                (
                    rating_scope,
                    game_key,
                    change.player,
                    change.after,
                    win,
                    draw,
                    loss,
                    archive.finished_at.astimezone(UTC).isoformat(),
                ),
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

    def list_matches(self, *, limit: int = 20, game: str | None = None) -> list[MatchSummary]:
        """Return recent persisted matches, newest first."""

        if limit < 1:
            raise ValueError("limit 必须至少为 1")
        sql = """
            SELECT match_id, game, seed, players_json, scores_json, started_at, finished_at
            FROM matches
        """
        params: list[object] = []
        if game is not None:
            sql += " WHERE game = ?"
            params.append(game)
        sql += " ORDER BY finished_at DESC, match_id DESC LIMIT ?"
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
            )
            for row in rows
        ]

    def leaderboard(self, *, game: str | None = None, limit: int = 50) -> list[RatingEntry]:
        """Return the overall leaderboard or the leaderboard for one game."""

        if limit < 1:
            raise ValueError("limit 必须至少为 1")
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
