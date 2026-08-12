"""Strict, read-only SQLite access for the local spectator web service.

This module deliberately does not construct :class:`SQLiteStore`.  Every
operation opens a fresh ``mode=ro`` connection so that a long-running web
process sees newly committed matches without ever creating, migrating, or
changing permissions on the database.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import ValidationError

from llmolympic.core.archive import MatchArchive, validate_entrant_id
from llmolympic.core.events import EventType
from llmolympic.core.storage import SCHEMA_VERSION, MatchSummary, RatingEntry

MAX_RESULT_LIMIT = 100
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_EVENTS = 10_000

_BUSY_TIMEOUT_MS = 250
_PROGRESS_CALLBACK_STEPS = 1_000
_MAX_QUERY_VM_STEPS = 2_000_000
_MAX_METADATA_BYTES = 1024 * 1024
_MATCH_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_GAME_ID_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_BIDI_CONTROL_CHARACTERS = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)

# Only the current schema is accepted.  Checking the shape used by this reader
# produces a stable failure instead of allowing a crafted table/view to fail in
# a later query.  The table names are constants; values supplied by callers are
# always bound parameters.
_REQUIRED_COLUMNS = {
    "entrants": {"entrant_id", "display_name"},
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
    "series_matches": {"series_id", "leg_number", "match_id"},
    "tournament_pairings": {
        "tournament_id",
        "pairing_number",
        "series_id",
    },
    "tournament_archives": {"tournament_id", "pairing_count"},
}


class WebReadError(RuntimeError):
    """A disclosure-safe reader failure with a stable machine-readable code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class LoadedMatch:
    """One archive and the verified denormalized summary from the same snapshot."""

    archive: MatchArchive
    summary: MatchSummary


def _sqlite_error_code(exc: sqlite3.Error, default: str) -> str:
    numeric = getattr(exc, "sqlite_errorcode", None)
    if isinstance(numeric, int) and numeric & 0xFF in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }:
        return "database_busy"
    return default


def _reject_nonfinite(value: str) -> None:
    del value
    raise ValueError("non-finite JSON number")


def _json_value(raw: object, *, code: str, max_bytes: int = _MAX_METADATA_BYTES) -> Any:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > max_bytes:
        raise WebReadError(code)
    try:
        return json.loads(raw, parse_constant=_reject_nonfinite)
    except (TypeError, ValueError, RecursionError):
        raise WebReadError(code) from None


def _aware_datetime(raw: object, *, code: str) -> datetime:
    if not isinstance(raw, str):
        raise WebReadError(code)
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        raise WebReadError(code) from None
    if value.utcoffset() is None:
        raise WebReadError(code)
    return value


def _utc_equal(left: datetime, right: datetime) -> bool:
    return left.astimezone(UTC) == right.astimezone(UTC)


def _safe_identity(raw: object, *, code: str) -> str:
    try:
        return validate_entrant_id(raw)
    except (TypeError, ValueError):
        raise WebReadError(code) from None


def _safe_display_name(raw: object, *, code: str) -> str:
    if not isinstance(raw, str) or not raw or len(raw) > 512:
        raise WebReadError(code)
    if any(
        ord(character) < 32 or 127 <= ord(character) <= 159 or character in _BIDI_CONTROL_CHARACTERS
        for character in raw
    ):
        raise WebReadError(code)
    return raw


def _finite_score(raw: object, *, code: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise WebReadError(code)
    value = float(raw)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise WebReadError(code)
    return value


def _players_and_scores(
    players_raw: object,
    scores_raw: object,
    *,
    code: str,
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    if not isinstance(players_raw, list) or not players_raw:
        raise WebReadError(code)
    if not isinstance(scores_raw, dict):
        raise WebReadError(code)

    players: list[dict[str, Any]] = []
    names: list[str] = []
    entrant_ids: list[str] = []
    for descriptor in players_raw:
        if not isinstance(descriptor, dict):
            raise WebReadError(code)
        name = _safe_display_name(descriptor.get("name"), code=code)
        display_name = _safe_display_name(descriptor.get("display_name"), code=code)
        entrant_id = _safe_identity(descriptor.get("entrant_id"), code=code)
        if display_name != name:
            raise WebReadError(code)
        names.append(name)
        entrant_ids.append(entrant_id)
        players.append(descriptor)
    if len(set(names)) != len(names) or len(set(entrant_ids)) != len(entrant_ids):
        raise WebReadError(code)

    if set(scores_raw) != set(names):
        raise WebReadError(code)
    scores = {name: _finite_score(scores_raw[name], code=code) for name in names}
    return players, scores


def _validate_limit(limit: object) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULT_LIMIT:
        raise WebReadError("invalid_limit")
    return limit


def _validate_game(game: object | None) -> str | None:
    if game is None:
        return None
    if not isinstance(game, str) or _GAME_ID_RE.fullmatch(game) is None:
        raise WebReadError("invalid_game_id")
    return game


def _validate_match_id(match_id: object) -> str:
    if not isinstance(match_id, str) or _MATCH_ID_RE.fullmatch(match_id) is None:
        raise WebReadError("invalid_match_id")
    return match_id


def _validated_optional_related_id(raw: object, *, code: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or _MATCH_ID_RE.fullmatch(raw) is None:
        raise WebReadError(code)
    return raw


class WebSQLiteReader:
    """Open a fresh, current-schema read-only snapshot for every operation."""

    __slots__ = ("_path",)

    def __init__(self, path: str | Path) -> None:
        try:
            self._path = Path(path).expanduser().resolve(strict=False)
        except (TypeError, ValueError, OSError):
            raise WebReadError("database_unavailable") from None

    def _open(self) -> sqlite3.Connection:
        # Percent-encoding prevents ``?`` and ``#`` in a filesystem name from
        # becoming URI options.  ``mode=ro`` is enforced by SQLite itself.
        uri = f"file:{quote(str(self._path), safe='/')}?mode=ro"
        try:
            connection = sqlite3.connect(
                uri,
                uri=True,
                timeout=_BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA query_only = ON")
            connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            progress_calls = 0

            def stop_expensive_query() -> int:
                nonlocal progress_calls
                progress_calls += 1
                return int(progress_calls * _PROGRESS_CALLBACK_STEPS > _MAX_QUERY_VM_STEPS)

            connection.set_progress_handler(stop_expensive_query, _PROGRESS_CALLBACK_STEPS)
            if connection.execute("PRAGMA trusted_schema").fetchone()[0] != 0:
                raise sqlite3.OperationalError("trusted_schema was not disabled")
            if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
                raise sqlite3.OperationalError("query_only was not enabled")
            return connection
        except sqlite3.Error as exc:
            try:
                connection.close()
            except (NameError, sqlite3.Error):
                pass
            raise WebReadError(_sqlite_error_code(exc, "database_unavailable")) from None

    @staticmethod
    def _verify_schema(connection: sqlite3.Connection) -> None:
        try:
            version_row = connection.execute("PRAGMA user_version").fetchone()
            version = None if version_row is None else version_row[0]
        except sqlite3.Error as exc:
            raise WebReadError(_sqlite_error_code(exc, "database_schema_invalid")) from None
        if version != SCHEMA_VERSION:
            raise WebReadError("database_schema_unsupported")

        try:
            for table, expected_columns in _REQUIRED_COLUMNS.items():
                object_row = connection.execute(
                    "SELECT type FROM sqlite_schema WHERE name = ?",
                    (table,),
                ).fetchone()
                if object_row is None or object_row["type"] != "table":
                    raise WebReadError("database_schema_invalid")
                columns = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM pragma_table_info(?)",
                        (table,),
                    )
                }
                if not expected_columns.issubset(columns):
                    raise WebReadError("database_schema_invalid")
        except WebReadError:
            raise
        except sqlite3.Error as exc:
            raise WebReadError(_sqlite_error_code(exc, "database_schema_invalid")) from None

    @contextmanager
    def _snapshot(self) -> Iterator[sqlite3.Connection]:
        connection = self._open()
        try:
            connection.execute("BEGIN")
            self._verify_schema(connection)
            yield connection
        except WebReadError:
            raise
        except sqlite3.Error as exc:
            raise WebReadError(_sqlite_error_code(exc, "database_read_failed")) from None
        finally:
            connection.close()

    def health(self) -> dict[str, object]:
        """Verify the database can be safely read without disclosing its path."""

        with self._snapshot() as connection:
            try:
                match_count = connection.execute("SELECT count(*) FROM matches").fetchone()[0]
            except sqlite3.Error as exc:
                raise WebReadError(_sqlite_error_code(exc, "database_read_failed")) from None
        if isinstance(match_count, bool) or not isinstance(match_count, int) or match_count < 0:
            raise WebReadError("database_read_failed")
        return {
            "status": "ok",
            "schema_version": SCHEMA_VERSION,
            "read_only": True,
            "match_count": match_count,
        }

    def list_matches(
        self,
        *,
        limit: int = 20,
        game: str | None = None,
    ) -> list[MatchSummary]:
        """Return verified recent-match summaries, newest first."""

        limit = _validate_limit(limit)
        game = _validate_game(game)
        sql = """
            SELECT m.match_id, m.schema_version, m.game, m.seed,
                   m.players_json, m.scores_json, m.started_at, m.finished_at,
                   m.archive_source, m.rating_source, m.rated, m.rating_policy,
                   length(CAST(m.players_json AS BLOB)) AS players_bytes,
                   length(CAST(m.scores_json AS BLOB)) AS scores_bytes,
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

        with self._snapshot() as connection:
            try:
                rows = connection.execute(sql, params).fetchall()
                summaries = [self._summary(connection, row) for row in rows]
            except WebReadError:
                raise
            except sqlite3.Error as exc:
                raise WebReadError(_sqlite_error_code(exc, "database_read_failed")) from None
        return summaries

    def _summary(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> MatchSummary:
        code = "match_index_invalid"
        match_id = row["match_id"]
        try:
            _validate_match_id(match_id)
        except WebReadError:
            raise WebReadError(code) from None
        game = row["game"]
        try:
            _validate_game(game)
        except WebReadError:
            raise WebReadError(code) from None
        if (
            row["schema_version"] not in (1, 2)
            or isinstance(row["seed"], bool)
            or not isinstance(row["seed"], int)
            or row["archive_source"] not in ("local_engine", "external", "legacy")
            or row["rating_source"] not in ("engine", "imported")
            or row["rated"] not in (0, 1)
            or not isinstance(row["rating_policy"], str)
            or row["rating_policy"] != ("elo_v1" if row["rated"] else "unrated")
            or not isinstance(row["players_bytes"], int)
            or not 0 <= row["players_bytes"] <= _MAX_METADATA_BYTES
            or not isinstance(row["scores_bytes"], int)
            or not 0 <= row["scores_bytes"] <= _MAX_METADATA_BYTES
        ):
            raise WebReadError(code)
        if (
            (row["schema_version"] == 1 and row["archive_source"] != "legacy")
            or (row["schema_version"] == 2 and row["archive_source"] == "legacy")
            or (row["rated"] and row["rating_source"] != "engine")
        ):
            raise WebReadError(code)

        players_raw = _json_value(row["players_json"], code=code)
        scores_raw = _json_value(row["scores_json"], code=code)
        players, scores = _players_and_scores(players_raw, scores_raw, code=code)
        started = _aware_datetime(row["started_at"], code=code)
        finished = _aware_datetime(row["finished_at"], code=code)
        if finished.astimezone(UTC) < started.astimezone(UTC):
            raise WebReadError(code)
        self._verify_match_players(connection, match_id, players, scores, code=code)

        series_id = _validated_optional_related_id(row["series_id"], code=code)
        tournament_id = _validated_optional_related_id(row["tournament_id"], code=code)
        leg_number = row["leg_number"]
        pairing_number = row["pairing_number"]
        pairing_count = row["pairing_count"]
        if (series_id is None) != (leg_number is None):
            raise WebReadError(code)
        if leg_number is not None and (
            isinstance(leg_number, bool) or not isinstance(leg_number, int) or leg_number < 1
        ):
            raise WebReadError(code)
        tournament_values = (tournament_id, pairing_number, pairing_count)
        if any(value is None for value in tournament_values) != all(
            value is None for value in tournament_values
        ):
            raise WebReadError(code)
        if tournament_id is not None:
            if series_id is None or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 1
                for value in (pairing_number, pairing_count)
            ):
                raise WebReadError(code)
            if pairing_number > pairing_count:
                raise WebReadError(code)

        return MatchSummary(
            match_id=match_id,
            game=game,
            seed=row["seed"],
            players=tuple(descriptor["display_name"] for descriptor in players),
            entrant_ids=tuple(descriptor["entrant_id"] for descriptor in players),
            scores=scores,
            started_at=started,
            finished_at=finished,
            series_id=series_id,
            leg_number=leg_number,
            rating_source=row["rating_source"],
            rated=bool(row["rated"]),
            tournament_id=tournament_id,
            pairing_number=pairing_number,
            pairing_count=pairing_count,
        )

    @staticmethod
    def _verify_match_players(
        connection: sqlite3.Connection,
        match_id: str,
        players: list[dict[str, Any]],
        scores: dict[str, float],
        *,
        code: str,
    ) -> None:
        try:
            rows = connection.execute(
                """
                SELECT position, player, entrant_id, display_name,
                       descriptor_json, score
                FROM match_players
                WHERE match_id = ?
                ORDER BY position
                """,
                (match_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise WebReadError(_sqlite_error_code(exc, "database_read_failed")) from None
        if len(rows) != len(players):
            raise WebReadError(code)
        for position, (row, descriptor) in enumerate(zip(rows, players)):
            stored_descriptor = _json_value(row["descriptor_json"], code=code)
            name = descriptor["display_name"]
            if (
                row["position"] != position
                or row["player"] != name
                or row["display_name"] != name
                or row["entrant_id"] != descriptor["entrant_id"]
                or stored_descriptor != descriptor
                or _finite_score(row["score"], code=code) != scores[name]
            ):
                raise WebReadError(code)

    def leaderboard(
        self,
        *,
        limit: int = 50,
        game: str | None = None,
    ) -> list[RatingEntry]:
        """Return a verified overall or per-game leaderboard."""

        limit = _validate_limit(limit)
        game = _validate_game(game)
        scope = "overall" if game is None else "game"
        game_key = "" if game is None else game
        with self._snapshot() as connection:
            try:
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
                    (scope, game_key, limit),
                ).fetchall()
            except sqlite3.Error as exc:
                raise WebReadError(_sqlite_error_code(exc, "database_read_failed")) from None

        result: list[RatingEntry] = []
        for row in rows:
            code = "leaderboard_invalid"
            entrant_id = _safe_identity(row["entrant_id"], code=code)
            display_name = _safe_display_name(row["display_name"], code=code)
            try:
                rating = float(row["rating"])
            except (TypeError, ValueError):
                raise WebReadError(code) from None
            counts = [row[key] for key in ("games_played", "wins", "draws", "losses")]
            if (
                not math.isfinite(rating)
                or any(
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                    for value in counts
                )
                or sum(counts[1:]) != counts[0]
            ):
                raise WebReadError(code)
            updated = _aware_datetime(row["updated_at"], code=code)
            result.append(
                RatingEntry(
                    entrant_id=entrant_id,
                    player=display_name,
                    rating=rating,
                    games_played=counts[0],
                    wins=counts[1],
                    draws=counts[2],
                    losses=counts[3],
                    updated_at=updated,
                )
            )
        return result

    def load_match(self, match_id: str) -> LoadedMatch:
        """Load and deeply verify one replayable local-engine schema-v2 archive."""

        match_id = _validate_match_id(match_id)
        with self._snapshot() as connection:
            try:
                row = connection.execute(
                    """
                    SELECT m.match_id, m.schema_version, m.game, m.seed,
                           m.players_json, m.scores_json,
                           m.started_at, m.finished_at, m.archive_source,
                           m.rating_source, m.rated, m.rating_policy,
                           length(CAST(m.players_json AS BLOB)) AS players_bytes,
                           length(CAST(m.scores_json AS BLOB)) AS scores_bytes,
                           typeof(m.archive_json) AS archive_type,
                           length(CAST(m.archive_json AS BLOB)) AS archive_bytes,
                           sm.series_id, sm.leg_number, tp.tournament_id,
                           tp.pairing_number, ta.pairing_count
                    FROM matches AS m
                    LEFT JOIN series_matches AS sm ON sm.match_id = m.match_id
                    LEFT JOIN tournament_pairings AS tp ON tp.series_id = sm.series_id
                    LEFT JOIN tournament_archives AS ta
                           ON ta.tournament_id = tp.tournament_id
                    WHERE m.match_id = ?
                    """,
                    (match_id,),
                ).fetchone()
            except sqlite3.Error as exc:
                raise WebReadError(_sqlite_error_code(exc, "database_read_failed")) from None
            if row is None:
                raise WebReadError("match_not_found")
            if row["schema_version"] != 2 or row["archive_source"] != "local_engine":
                raise WebReadError("match_detail_unsupported")
            summary = self._summary(connection, row)
            if (
                row["archive_type"] != "text"
                or not isinstance(row["archive_bytes"], int)
                or row["archive_bytes"] < 0
            ):
                raise WebReadError("archive_invalid")
            if row["archive_bytes"] > MAX_ARCHIVE_BYTES:
                raise WebReadError("archive_too_large")
            try:
                archive_row = connection.execute(
                    "SELECT archive_json FROM matches WHERE match_id = ?",
                    (match_id,),
                ).fetchone()
            except sqlite3.Error as exc:
                raise WebReadError(_sqlite_error_code(exc, "database_read_failed")) from None
            if archive_row is None:
                raise WebReadError("match_not_found")
            payload = _json_value(
                archive_row["archive_json"],
                code="archive_invalid",
                max_bytes=MAX_ARCHIVE_BYTES,
            )
            if not isinstance(payload, dict):
                raise WebReadError("archive_invalid")
            if payload.get("schema_version") != 2 or payload.get("source") != "local_engine":
                raise WebReadError("match_detail_unsupported")
            events = payload.get("events")
            if not isinstance(events, list):
                raise WebReadError("archive_invalid")
            if len(events) > MAX_ARCHIVE_EVENTS:
                raise WebReadError("archive_event_limit_exceeded")
            try:
                archive = MatchArchive.model_validate(payload)
            except (ValidationError, TypeError, ValueError, RecursionError):
                raise WebReadError("archive_invalid") from None
            self._validate_archive_semantics(archive)
            self._verify_archive_row(row, archive)
            normalized = archive.model_dump(mode="json")
            players, scores = _players_and_scores(
                normalized["players"],
                normalized["scores"],
                code="archive_invalid",
            )
            self._verify_match_players(
                connection,
                archive.match_id,
                players,
                scores,
                code="match_index_invalid",
            )
        return LoadedMatch(archive=archive, summary=summary)

    def get_match(self, match_id: str) -> LoadedMatch:
        """Compatibility alias for callers used to the storage API."""

        return self.load_match(match_id)

    @staticmethod
    def _validate_archive_semantics(archive: MatchArchive) -> None:
        code = "archive_invalid"
        if archive.schema_version != 2 or archive.source != "local_engine":
            raise WebReadError("match_detail_unsupported")
        if len(archive.events) > MAX_ARCHIVE_EVENTS or not archive.events:
            raise WebReadError(code)

        players, scores = _players_and_scores(archive.players, archive.scores, code=code)
        names = {descriptor["display_name"] for descriptor in players}
        if [event.seq for event in archive.events] != list(range(len(archive.events))):
            raise WebReadError(code)
        started_events = [
            event for event in archive.events if event.type == EventType.MATCH_STARTED
        ]
        finished_events = [
            event for event in archive.events if event.type == EventType.MATCH_FINISHED
        ]
        if (
            len(started_events) != 1
            or len(finished_events) != 1
            or archive.events[0] is not started_events[0]
            or archive.events[-1] is not finished_events[0]
        ):
            raise WebReadError(code)

        started_at = archive.started_at
        finished_at = archive.finished_at
        if started_at.utcoffset() is None or finished_at.utcoffset() is None:
            raise WebReadError(code)
        started_utc = started_at.astimezone(UTC)
        finished_utc = finished_at.astimezone(UTC)
        if finished_utc < started_utc:
            raise WebReadError(code)

        previous_timestamp: datetime | None = None
        for event in archive.events:
            if event.timestamp.utcoffset() is None:
                raise WebReadError(code)
            timestamp = event.timestamp.astimezone(UTC)
            if timestamp < started_utc or timestamp > finished_utc:
                raise WebReadError(code)
            if previous_timestamp is not None and timestamp < previous_timestamp:
                raise WebReadError(code)
            previous_timestamp = timestamp
            if event.player is not None and event.player not in names:
                raise WebReadError(code)
            if (
                event.type
                in {
                    EventType.TURN_PROMPT,
                    EventType.MOVE_RECEIVED,
                    EventType.MOVE_REJECTED,
                }
                and event.player not in names
            ):
                raise WebReadError(code)
            if event.type in {EventType.MATCH_STARTED, EventType.MATCH_FINISHED} and event.player:
                raise WebReadError(code)

        started_data = started_events[0].data
        if (
            started_data.get("game") != archive.game
            or started_data.get("seed") != archive.seed
            or started_data.get("players") != archive.players
        ):
            raise WebReadError(code)
        raw_finished_scores = finished_events[0].data.get("scores")
        _, finished_scores = _players_and_scores(
            archive.players,
            raw_finished_scores,
            code=code,
        )
        if finished_scores != scores:
            raise WebReadError(code)
        forfeited_by = finished_events[0].data.get("forfeited_by")
        if forfeited_by is not None and forfeited_by not in names:
            raise WebReadError(code)
        if any(move.player not in names for move in archive.moves):
            raise WebReadError(code)

    @staticmethod
    def _verify_archive_row(row: sqlite3.Row, archive: MatchArchive) -> None:
        code = "match_index_invalid"
        payload = archive.model_dump(mode="json")
        players_raw = _json_value(row["players_json"], code=code)
        scores_raw = _json_value(row["scores_json"], code=code)
        _, stored_scores = _players_and_scores(players_raw, scores_raw, code=code)
        started = _aware_datetime(row["started_at"], code=code)
        finished = _aware_datetime(row["finished_at"], code=code)
        if (
            row["match_id"] != archive.match_id
            or row["schema_version"] != archive.schema_version
            or row["game"] != archive.game
            or row["seed"] != archive.seed
            or players_raw != payload["players"]
            or stored_scores != archive.scores
            or not _utc_equal(started, archive.started_at)
            or not _utc_equal(finished, archive.finished_at)
            or row["archive_source"] != archive.source
            or row["rating_source"] not in ("engine", "imported")
            or row["rated"] not in (0, 1)
            or row["rating_policy"] != ("elo_v1" if row["rated"] else "unrated")
        ):
            raise WebReadError(code)


# Short aliases keep integration code readable without weakening the boundary.
ReadError = WebReadError
SQLiteReader = WebSQLiteReader
