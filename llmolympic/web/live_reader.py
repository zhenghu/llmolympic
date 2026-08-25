"""Strict, read-only access to the best-effort live-event sidecar.

The game process owns the sidecar and may disappear at any point.  The Web
observer consequently treats a missing sidecar as an empty live lobby and
projects an expired writer lease as an interrupted session.  It never creates,
migrates, cleans up, or otherwise writes either SQLite database.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import TypeAdapter, ValidationError

from llmolympic.live import (
    LIVE_DEFAULT_PAGE_LIMIT,
    LIVE_MAX_BYTES,
    LIVE_MAX_EVENT_BYTES,
    LIVE_MAX_EVENTS,
    LIVE_MAX_PAGE_LIMIT,
    LIVE_MAX_SESSIONS,
    LIVE_SCHEMA_VERSION,
    derive_live_database_path,
)
from llmolympic.web.models import (
    LiveChampionshipBracket,
    LiveMatchDetail,
    LiveMatchSummary,
    LiveStreamItem,
)

_BUSY_TIMEOUT_MS = 250
_PROGRESS_CALLBACK_STEPS = 1_000
_MAX_QUERY_VM_STEPS = 1_000_000
_MAX_METADATA_BYTES = 1024 * 1024
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SAFE_GAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_VALID_MODES = frozenset({"play", "series", "round_robin", "championship"})
_VALID_STATUSES = frozenset({"running", "completed", "interrupted"})
_VALID_FINAL_KINDS = frozenset({"match", "series", "tournament", "championship"})
_SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2})
_SAFE_ERROR_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_BIDI_CONTROL_CHARACTERS = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)
_BASE_REQUIRED_COLUMNS = {
    "live_sessions": {
        "live_id",
        "schema_version",
        "mode",
        "game",
        "players_json",
        "current_context_json",
        "status",
        "created_at",
        "updated_at",
        "heartbeat_at",
        "lease_expires_at",
        "next_seq",
        "event_count",
        "event_bytes",
        "final_kind",
        "final_id",
        "final_match_ids_json",
        "interruption_code",
        "owner_token_digest",
    },
    "live_events": {
        "live_id",
        "seq",
        "created_at",
        "event_bytes",
        "event_json",
    },
}
_REQUIRED_COLUMNS_BY_VERSION = {
    1: _BASE_REQUIRED_COLUMNS,
    2: {
        **_BASE_REQUIRED_COLUMNS,
        "live_sessions": _BASE_REQUIRED_COLUMNS["live_sessions"]
        | {"championship_bracket_json"},
    },
}
_LIVE_STREAM_ITEM_ADAPTER = TypeAdapter(LiveStreamItem)


class LiveReadError(RuntimeError):
    """A disclosure-safe live-reader failure with a stable public code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _sqlite_error_code(exc: sqlite3.Error, default: str) -> str:
    numeric = getattr(exc, "sqlite_errorcode", None)
    if isinstance(numeric, int) and numeric & 0xFF in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }:
        return "database_busy"
    return default


def _json_value(raw: object, *, max_bytes: int = _MAX_METADATA_BYTES) -> Any:
    if not isinstance(raw, str) or _utf8_length(raw) > max_bytes:
        raise LiveReadError("live_invalid")
    try:
        return json.loads(raw, parse_constant=lambda _value: _reject_nonfinite())
    except (TypeError, ValueError, RecursionError):
        raise LiveReadError("live_invalid") from None


def _event_json_value(raw: object, *, expected_bytes: int) -> Any:
    if (
        not isinstance(raw, str)
        or expected_bytes <= 0
        or _utf8_length(raw) != expected_bytes
        or expected_bytes > LIVE_MAX_EVENT_BYTES
    ):
        raise LiveReadError("live_invalid")
    try:
        return json.loads(raw, parse_constant=lambda _value: _reject_nonfinite())
    except (TypeError, ValueError, RecursionError):
        raise LiveReadError("live_invalid") from None


def _reject_nonfinite() -> None:
    raise ValueError("non-finite JSON number")


def _utf8_length(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeError:
        raise LiveReadError("live_invalid") from None


def _epoch_datetime(raw: object) -> datetime:
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not math.isfinite(float(raw))
    ):
        raise LiveReadError("live_invalid")
    try:
        value = datetime.fromtimestamp(float(raw), tz=UTC)
    except (OSError, OverflowError, ValueError):
        raise LiveReadError("live_invalid") from None
    return value


def _nonnegative_integer(raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise LiveReadError("live_invalid")
    return raw


def _safe_identifier(raw: object, *, nullable: bool = False) -> str | None:
    if raw is None and nullable:
        return None
    if not isinstance(raw, str) or _SAFE_ID_RE.fullmatch(raw) is None:
        raise LiveReadError("live_invalid")
    return raw


def _validate_limit(value: int, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise LiveReadError("invalid_limit")
    return value


def _validate_from_seq(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= LIVE_MAX_EVENTS:
        raise LiveReadError("invalid_from_seq")
    return value


def _players(raw: object) -> tuple[str, ...]:
    value = _json_value(raw)
    if (
        not isinstance(value, list)
        or len(value) < 2
        or any(
            not isinstance(player, str)
            or not player
            or len(player) > 512
            or any(
                ord(character) < 32
                or 127 <= ord(character) <= 159
                or 0xD800 <= ord(character) <= 0xDFFF
                or character in _BIDI_CONTROL_CHARACTERS
                for character in player
            )
            for player in value
        )
    ):
        raise LiveReadError("live_invalid")
    return tuple(value)


def _final_match_ids(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    value = _json_value(raw)
    if (
        not isinstance(value, list)
        or len(value) > LIVE_MAX_EVENTS
        or len(value) != len(set(value))
        or any(not isinstance(item, str) or _SAFE_ID_RE.fullmatch(item) is None for item in value)
    ):
        raise LiveReadError("live_invalid")
    return tuple(value)


def _current_context(raw: object) -> dict[str, int | None]:
    fields = (
        "pairing_number",
        "pairing_count",
        "leg_number",
        "round_number",
        "round_count",
        "round_pairing_number",
        "round_pairing_count",
    )
    if raw is None:
        return dict.fromkeys(fields)
    value = _json_value(raw)
    if not isinstance(value, dict) or set(value) - set(fields):
        raise LiveReadError("live_invalid")
    result: dict[str, int | None] = {}
    for field in fields:
        item = value.get(field)
        if item is not None and (
            isinstance(item, bool) or not isinstance(item, int) or item < 1
        ):
            raise LiveReadError("live_invalid")
        result[field] = item
    if result["leg_number"] is not None and result["leg_number"] not in {1, 2}:
        raise LiveReadError("live_invalid")
    return result


def _championship_bracket(raw: object) -> LiveChampionshipBracket | None:
    if raw is None:
        return None
    value = _json_value(raw)
    try:
        return LiveChampionshipBracket.model_validate(value)
    except (TypeError, ValueError, ValidationError, RecursionError):
        raise LiveReadError("live_invalid") from None


def _summary_from_row(
    row: Mapping[str, object],
    *,
    now: float,
    database_schema_version: int,
) -> LiveMatchSummary:
    live_id = _safe_identifier(row["live_id"])
    mode = row["mode"]
    status = row["status"]
    game = row["game"]
    if mode not in _VALID_MODES or status not in _VALID_STATUSES:
        raise LiveReadError("live_invalid")
    if not isinstance(game, str) or _SAFE_GAME_RE.fullmatch(game) is None:
        raise LiveReadError("live_invalid")
    event_count = _nonnegative_integer(row["event_count"])
    next_seq = _nonnegative_integer(row["next_seq"])
    event_bytes = _nonnegative_integer(row["event_bytes"])
    if (
        event_count != next_seq
        or event_count > LIVE_MAX_EVENTS
        or event_bytes > LIVE_MAX_BYTES
    ):
        raise LiveReadError("live_invalid")
    created_at = _epoch_datetime(row["created_at"])
    updated_at = _epoch_datetime(row["updated_at"])
    heartbeat_at = _epoch_datetime(row["heartbeat_at"])
    lease_expires_at = row["lease_expires_at"]
    if (
        isinstance(lease_expires_at, bool)
        or not isinstance(lease_expires_at, (int, float))
        or not math.isfinite(float(lease_expires_at))
    ):
        raise LiveReadError("live_invalid")
    if not created_at <= updated_at or not created_at <= heartbeat_at:
        raise LiveReadError("live_invalid")
    row_schema_version = row["schema_version"]
    if (
        isinstance(row_schema_version, bool)
        or not isinstance(row_schema_version, int)
        or row_schema_version not in _SUPPORTED_SCHEMA_VERSIONS
        or row_schema_version > database_schema_version
    ):
        raise LiveReadError("live_invalid")
    owner_digest = row["owner_token_digest"]
    if not isinstance(owner_digest, bytes) or len(owner_digest) != 32:
        raise LiveReadError("live_invalid")
    public_status = "interrupted" if status == "running" and lease_expires_at <= now else status
    final_kind = row["final_kind"]
    if final_kind is not None and final_kind not in _VALID_FINAL_KINDS:
        raise LiveReadError("live_invalid")
    final_id = _safe_identifier(row["final_id"], nullable=True)
    final_match_ids = _final_match_ids(row["final_match_ids_json"])
    context = _current_context(row["current_context_json"])
    bracket = (
        _championship_bracket(row["championship_bracket_json"])
        if row_schema_version == 2
        else None
    )
    interruption_code = row["interruption_code"]
    if interruption_code is not None and (
        not isinstance(interruption_code, str)
        or _SAFE_ERROR_CODE_RE.fullmatch(interruption_code) is None
    ):
        raise LiveReadError("live_invalid")
    if status == "completed":
        if (
            final_kind is None
            or final_id is None
            or not final_match_ids
            or interruption_code is not None
        ):
            raise LiveReadError("live_invalid")
    elif status == "interrupted":
        if (
            final_kind is not None
            or final_id is not None
            or final_match_ids
            or row["final_match_ids_json"] is not None
            or interruption_code is None
        ):
            raise LiveReadError("live_invalid")
    elif (
        final_kind is not None
        or final_id is not None
        or final_match_ids
        or row["final_match_ids_json"] is not None
        or interruption_code is not None
    ):
        raise LiveReadError("live_invalid")
    if public_status != "completed":
        # An expired session is a public interruption; no partially written
        # completion metadata may escape with it.
        final_kind = None
        final_id = None
        final_match_ids = ()
    try:
        return LiveMatchSummary(
            live_id=live_id,
            mode=mode,
            status=public_status,
            game=game,
            players=_players(row["players_json"]),
            started_at=created_at,
            updated_at=updated_at,
            event_count=event_count,
            pairing_number=context["pairing_number"],
            pairing_count=context["pairing_count"],
            leg_number=context["leg_number"],
            round_number=context["round_number"],
            round_count=context["round_count"],
            round_pairing_number=context["round_pairing_number"],
            round_pairing_count=context["round_pairing_count"],
            championship_bracket=bracket,
            final_kind=final_kind,
            final_id=final_id,
            final_match_ids=final_match_ids,
        )
    except (TypeError, ValueError, ValidationError):
        raise LiveReadError("live_invalid") from None


class LiveSQLiteReader:
    """Read current live sessions without creating the best-effort sidecar."""

    __slots__ = ("_clock", "_path")

    def __init__(
        self,
        archive_database: str | Path,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        try:
            archive_path = Path(archive_database).expanduser().resolve(strict=False)
            self._path = derive_live_database_path(archive_path)
        except (TypeError, ValueError, OSError):
            raise LiveReadError("live_unavailable") from None
        self._clock = time.time if clock is None else clock

    @property
    def path(self) -> Path:
        return self._path

    def _open(self) -> sqlite3.Connection:
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
            return connection
        except sqlite3.Error as exc:
            try:
                connection.close()
            except (NameError, sqlite3.Error):
                pass
            raise LiveReadError(_sqlite_error_code(exc, "live_unavailable")) from None

    @staticmethod
    def _verify_schema(connection: sqlite3.Connection) -> int:
        try:
            row = connection.execute("PRAGMA user_version").fetchone()
            if (
                row is None
                or row[0] not in _SUPPORTED_SCHEMA_VERSIONS
                or LIVE_SCHEMA_VERSION != 2
            ):
                raise LiveReadError("live_unavailable")
            version = int(row[0])
            for table, expected_columns in _REQUIRED_COLUMNS_BY_VERSION[version].items():
                object_row = connection.execute(
                    "SELECT type FROM sqlite_schema WHERE name = ?", (table,)
                ).fetchone()
                if object_row is None or object_row["type"] != "table":
                    raise LiveReadError("live_unavailable")
                columns = {
                    column["name"]
                    for column in connection.execute("SELECT name FROM pragma_table_info(?)", (table,))
                }
                if not expected_columns.issubset(columns):
                    raise LiveReadError("live_unavailable")
            return version
        except LiveReadError:
            raise
        except sqlite3.Error as exc:
            raise LiveReadError(_sqlite_error_code(exc, "live_unavailable")) from None

    @contextmanager
    def _snapshot(self) -> Iterator[tuple[sqlite3.Connection, int]]:
        connection = self._open()
        try:
            connection.execute("BEGIN")
            version = self._verify_schema(connection)
            yield connection, version
        except LiveReadError:
            raise
        except sqlite3.Error as exc:
            raise LiveReadError(_sqlite_error_code(exc, "live_unavailable")) from None
        finally:
            connection.close()

    def list_live(self, *, game: str | None = None, limit: int = 20) -> list[LiveMatchSummary]:
        limit = _validate_limit(limit, maximum=min(100, LIVE_MAX_SESSIONS))
        if game is not None and (
            not isinstance(game, str) or _SAFE_GAME_RE.fullmatch(game) is None
        ):
            raise LiveReadError("invalid_game_id")
        if not self._path.is_file():
            return []
        sql = "SELECT * FROM live_sessions"
        parameters: list[object] = []
        if game is not None:
            sql += " WHERE game = ?"
            parameters.append(game)
        sql += " ORDER BY updated_at DESC, live_id DESC LIMIT ?"
        parameters.append(limit)
        with self._snapshot() as (connection, schema_version):
            rows = connection.execute(sql, parameters).fetchall()
        now = self._now()
        return [
            _summary_from_row(
                row,
                now=now,
                database_schema_version=schema_version,
            )
            for row in rows
        ]

    def load_live(
        self,
        live_id: str,
        *,
        from_seq: int = 0,
        limit: int = LIVE_DEFAULT_PAGE_LIMIT,
    ) -> LiveMatchDetail:
        live_id = _safe_identifier(live_id)
        from_seq = _validate_from_seq(from_seq)
        limit = _validate_limit(limit, maximum=LIVE_MAX_PAGE_LIMIT)
        if not self._path.is_file():
            raise LiveReadError("live_not_found")
        with self._snapshot() as (connection, schema_version):
            row = connection.execute(
                "SELECT * FROM live_sessions WHERE live_id = ?", (live_id,)
            ).fetchone()
            if row is None:
                raise LiveReadError("live_not_found")
            summary = _summary_from_row(
                row,
                now=self._now(),
                database_schema_version=schema_version,
            )
            row_schema_version = int(row["schema_version"])
            if from_seq > summary.event_count:
                raise LiveReadError("invalid_from_seq")
            event_rows = connection.execute(
                """
                SELECT seq, created_at, event_bytes, event_json
                FROM live_events
                WHERE live_id = ? AND seq >= ?
                ORDER BY seq ASC
                LIMIT ?
                """,
                (live_id, from_seq, limit),
            ).fetchall()
            aggregate = connection.execute(
                """
                SELECT count(*) AS row_count, min(seq) AS min_seq, max(seq) AS max_seq,
                       coalesce(sum(event_bytes), 0) AS total_bytes
                FROM live_events
                WHERE live_id = ?
                """,
                (live_id,),
            ).fetchone()
        expected_min = None if summary.event_count == 0 else 0
        expected_max = None if summary.event_count == 0 else summary.event_count - 1
        if (
            aggregate is None
            or aggregate["row_count"] != summary.event_count
            or aggregate["min_seq"] != expected_min
            or aggregate["max_seq"] != expected_max
            or aggregate["total_bytes"] != row["event_bytes"]
        ):
            raise LiveReadError("live_invalid")
        items: list[LiveStreamItem] = []
        for expected_seq, event_row in enumerate(event_rows, start=from_seq):
            seq = _nonnegative_integer(event_row["seq"])
            created_at = _epoch_datetime(event_row["created_at"])
            event_bytes = _nonnegative_integer(event_row["event_bytes"])
            if (
                seq != expected_seq
                or created_at < summary.started_at
                or not 0 < event_bytes <= LIVE_MAX_EVENT_BYTES
            ):
                raise LiveReadError("live_invalid")
            payload = _event_json_value(event_row["event_json"], expected_bytes=event_bytes)
            if row_schema_version == 1 and isinstance(payload, dict):
                if "kind" in payload:
                    raise LiveReadError("live_invalid")
                payload = {"kind": "match_event", **payload}
            try:
                item = _LIVE_STREAM_ITEM_ADAPTER.validate_python(payload)
            except (TypeError, ValueError, ValidationError, RecursionError):
                raise LiveReadError("live_invalid") from None
            if item.seq != seq:
                raise LiveReadError("live_invalid")
            items.append(item)
        next_seq = from_seq + len(items)
        try:
            return LiveMatchDetail(
                match=summary,
                events=tuple(items),
                next_seq=next_seq,
                has_more=next_seq < summary.event_count,
            )
        except (TypeError, ValueError, ValidationError):
            raise LiveReadError("live_invalid") from None

    def _now(self) -> float:
        try:
            value = self._clock()
        except Exception:  # noqa: BLE001 - the clock is an injected boundary
            raise LiveReadError("live_unavailable") from None
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise LiveReadError("live_unavailable")
        return float(value)


__all__ = ["LiveReadError", "LiveSQLiteReader"]
