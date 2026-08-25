"""Capability-scoped browser input for local human competitors.

The archive database remains owned by :class:`SQLiteStore`, and the live-event
sidecar remains observer-only.  Browser moves travel through a separate,
private SQLite sidecar.  The match process owns sessions and requests; the Web
process may only atomically submit one bounded string for the current request.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self, TypeVar
from urllib.parse import quote

from llmolympic.core.events import EventType, MatchEvent
from llmolympic.core.game import MAX_PLATFORM_PLAYERS
from llmolympic.core.player import (
    DEFAULT_MAX_RESPONSE_CHARS,
    HumanPlayer,
    PlayerTimeoutError,
)

INPUT_SCHEMA_VERSION = 1
INPUT_MAX_MOVE_CHARS = DEFAULT_MAX_RESPONSE_CHARS
INPUT_MAX_PROMPT_CHARS = 65_536
INPUT_STALE_AFTER_SECONDS = 60.0
INPUT_DEFAULT_HEARTBEAT_SECONDS = 10.0
INPUT_DEFAULT_POLL_SECONDS = 0.1
INPUT_RETENTION_SECONDS = 24 * 60 * 60.0
INPUT_MAX_SESSIONS = 64
_SQLITE_CONTENTION_ATTEMPTS = 4
_SQLITE_CONTENTION_RETRY_SECONDS = 0.01

_T = TypeVar("_T")

InputSessionStatus = Literal["active", "completed", "interrupted", "expired"]
InputRequestStatus = Literal[
    "pending",
    "submitted",
    "consumed",
    "accepted",
    "rejected",
    "expired",
    "cancelled",
]

_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SAFE_GAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SUBMISSION_ID_RE = re.compile(r"[a-f0-9]{32}\Z")
_SAFE_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_PRIVATE_FILE_MODE = 0o600
_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")


class HumanInputError(RuntimeError):
    """Stable infrastructure/protocol failure for browser-human input."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _HumanInputTimeout(HumanInputError):
    def __init__(self) -> None:
        super().__init__("request_expired")


@dataclass(frozen=True)
class InputRequestSnapshot:
    request_id: str
    request_seq: int
    match_event_seq: int
    state: InputRequestStatus
    prompt: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class InputSnapshot:
    session_id: str
    seat_id: str
    status: InputSessionStatus
    game: str
    player_name: str
    players: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    lease_expires_at: datetime
    request: InputRequestSnapshot | None
    final_match_id: str | None


@dataclass(frozen=True)
class InputSubmitResult:
    request_id: str
    status: Literal["accepted", "idempotent"]


def derive_human_input_database_path(database: str | Path) -> Path:
    """Return the private control-sidecar path for an archive database."""

    archive = Path(database).expanduser().resolve(strict=False)
    return Path(f"{archive}.input.db")


derive_input_database_path = derive_human_input_database_path
human_input_database_path = derive_human_input_database_path


def _now_datetime(value: float) -> datetime:
    return datetime.fromtimestamp(value, tz=UTC)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _token_digest(token: str) -> bytes:
    try:
        encoded = token.encode("ascii")
    except UnicodeEncodeError as exc:
        raise HumanInputError("capability_invalid") from exc
    return hashlib.sha256(encoded).digest()


def _move_digest(move: str, capability: str) -> bytes:
    try:
        encoded = move.encode("utf-8")
        key = capability.encode("ascii")
    except UnicodeEncodeError as exc:
        raise HumanInputError("invalid_request") from exc
    return hmac.digest(key, encoded, "sha256")


def _is_sqlite_contention(error: BaseException) -> bool:
    """Recognize SQLite BUSY/LOCKED through the stable wrapped error chain."""

    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, sqlite3.Error):
            code = getattr(current, "sqlite_errorcode", None)
            if isinstance(code, int) and (code & 0xFF) in {
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            }:
                return True
            message = str(current).casefold()
            if "database is locked" in message or "database table is locked" in message:
                return True
        current = current.__cause__ or current.__context__
    return False


def _validate_text(value: object, *, maximum: int, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value):
        raise HumanInputError("invalid_request")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise HumanInputError("invalid_request")
    return value


def _validate_names(player_name: str, players: tuple[str, ...]) -> None:
    if (
        not 2 <= len(players) <= MAX_PLATFORM_PLAYERS
        or len(set(players)) != len(players)
        or player_name not in players
    ):
        raise HumanInputError("input_invalid")
    for name in players:
        _validate_text(name, maximum=512, allow_empty=False)
        if any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in name):
            raise HumanInputError("input_invalid")


def _check_regular_file(path: Path, *, missing_code: str) -> None:
    try:
        details = path.lstat()
    except FileNotFoundError as exc:
        raise HumanInputError(missing_code) from exc
    except OSError as exc:
        raise HumanInputError("input_unavailable") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise HumanInputError("input_unavailable")


def _secure_file(path: Path) -> None:
    try:
        _check_regular_file(path, missing_code="input_unavailable")
        path.chmod(_PRIVATE_FILE_MODE)
        if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) != _PRIVATE_FILE_MODE:
            raise OSError("input sidecar permissions are not private")
        for suffix in _SIDECAR_SUFFIXES:
            related = Path(f"{path}{suffix}")
            if related.is_symlink():
                raise OSError("unsafe SQLite sidecar")
            if related.exists():
                if not related.is_file():
                    raise OSError("unsafe SQLite sidecar")
                related.chmod(_PRIVATE_FILE_MODE)
    except HumanInputError:
        raise
    except OSError as exc:
        raise HumanInputError("input_unavailable") from exc


def _create_private_file(path: Path) -> None:
    try:
        if path.exists() or path.is_symlink():
            _check_regular_file(path, missing_code="input_unavailable")
            _secure_file(path)
            return
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, _PRIVATE_FILE_MODE)
        os.close(descriptor)
        _secure_file(path)
    except HumanInputError:
        raise
    except OSError as exc:
        raise HumanInputError("input_unavailable") from exc


def _connect(path: Path, *, create: bool, timeout: float = 1.0) -> sqlite3.Connection:
    if create:
        _create_private_file(path)
    else:
        _check_regular_file(path, missing_code="participation_not_found")
    connection: sqlite3.Connection | None = None
    try:
        uri = f"{path.as_uri()}?mode=rw"
        connection = sqlite3.connect(uri, uri=True, timeout=timeout)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("PRAGMA secure_delete = ON")
        connection.execute("PRAGMA busy_timeout = 1000")
        return connection
    except sqlite3.Error as exc:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        raise HumanInputError("input_unavailable") from exc


def _initialize_schema(connection: sqlite3.Connection) -> None:
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        has_objects = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view','trigger') LIMIT 1"
        ).fetchone()
        if version not in {0, INPUT_SCHEMA_VERSION} or (version == 0 and has_objects):
            raise HumanInputError("input_invalid")
        if version == 0:
            connection.executescript(
                """
                PRAGMA journal_mode = DELETE;
                CREATE TABLE input_sessions (
                    session_id TEXT PRIMARY KEY,
                    seat_id TEXT NOT NULL UNIQUE,
                    owner_digest BLOB NOT NULL,
                    capability_digest BLOB NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active','completed','interrupted')),
                    game TEXT NOT NULL,
                    player_name TEXT NOT NULL,
                    players_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    lease_expires_at REAL NOT NULL,
                    current_request_id TEXT,
                    next_request_seq INTEGER NOT NULL CHECK(next_request_seq >= 0),
                    final_match_id TEXT,
                    terminal_reason_code TEXT
                );
                CREATE TABLE input_requests (
                    request_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES input_sessions(session_id) ON DELETE CASCADE,
                    request_seq INTEGER NOT NULL CHECK(request_seq >= 0),
                    match_event_seq INTEGER NOT NULL CHECK(match_event_seq >= 0),
                    state TEXT NOT NULL CHECK(state IN (
                        'pending','submitted','consumed','accepted','rejected','expired','cancelled'
                    )),
                    prompt TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    submission_id TEXT,
                    move TEXT,
                    move_digest BLOB,
                    submitted_at REAL,
                    resolved_at REAL,
                    reason TEXT,
                    UNIQUE(session_id, request_seq)
                );
                CREATE INDEX input_requests_session_idx
                    ON input_requests(session_id, request_seq DESC);
                PRAGMA user_version = 1;
                """
            )
        _validate_schema(connection)
    except HumanInputError:
        raise
    except sqlite3.Error as exc:
        raise HumanInputError("input_unavailable") from exc


def _validate_schema(connection: sqlite3.Connection) -> None:
    try:
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != INPUT_SCHEMA_VERSION:
            raise HumanInputError("input_invalid")
        expected = {
            "input_sessions": {
                "session_id", "seat_id", "owner_digest", "capability_digest", "status",
                "game", "player_name", "players_json", "created_at", "updated_at",
                "lease_expires_at", "current_request_id", "next_request_seq",
                "final_match_id", "terminal_reason_code",
            },
            "input_requests": {
                "request_id", "session_id", "request_seq", "match_event_seq", "state",
                "prompt", "created_at", "expires_at", "submission_id", "move",
                "move_digest", "submitted_at", "resolved_at", "reason",
            },
        }
        for table, columns in expected.items():
            actual = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            if actual != columns:
                raise HumanInputError("input_invalid")
    except HumanInputError:
        raise
    except sqlite3.Error as exc:
        raise HumanInputError("input_unavailable") from exc


def _constant_time_authorized(row: sqlite3.Row, token: str, column: str) -> bool:
    expected = row[column]
    return isinstance(expected, bytes) and hmac.compare_digest(expected, _token_digest(token))


class InputSessionStore:
    """Producer-owned session and request lifecycle for one browser seat."""

    def __init__(
        self,
        database: str | Path,
        *,
        player_name: str,
        game: str = "math_quiz",
        players: tuple[str, ...] | None = None,
        heartbeat_seconds: float = INPUT_DEFAULT_HEARTBEAT_SECONDS,
        poll_seconds: float = INPUT_DEFAULT_POLL_SECONDS,
        clock=time.time,
    ) -> None:
        self.path = derive_human_input_database_path(database)
        self.player_name = player_name
        self.game = game
        self.players = players or (player_name, "对手")
        self._clock = clock
        self._heartbeat_seconds = float(heartbeat_seconds)
        self._poll_seconds = float(poll_seconds)
        self.session_id = uuid.uuid4().hex
        self.seat_id = uuid.uuid4().hex
        self.capability = secrets.token_urlsafe(32)
        self._owner_token = secrets.token_urlsafe(32)
        self._stop = threading.Event()
        self._closed = False
        self._failure: HumanInputError | None = None
        self._thread: threading.Thread | None = None

        if _SAFE_GAME_RE.fullmatch(game) is None:
            raise HumanInputError("input_invalid")
        _validate_names(player_name, self.players)
        if (
            not math.isfinite(self._heartbeat_seconds)
            or not 0 < self._heartbeat_seconds < INPUT_STALE_AFTER_SECONDS
            or not math.isfinite(self._poll_seconds)
            or self._poll_seconds <= 0
        ):
            raise HumanInputError("input_invalid")

        connection = _connect(self.path, create=True)
        try:
            _initialize_schema(connection)
            self._create_session(connection)
        finally:
            connection.close()
            _secure_file(self.path)
        self._thread = threading.Thread(
            target=self._heartbeat,
            name="llmolympic-human-input-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def _retry_sqlite_contention(self, operation: Callable[[], _T]) -> _T:
        """Retry a complete fenced transaction after bounded SQLite contention."""

        last_error: BaseException | None = None
        for attempt in range(_SQLITE_CONTENTION_ATTEMPTS):
            try:
                return operation()
            except HumanInputError as exc:
                if not _is_sqlite_contention(exc):
                    raise
                last_error = exc
            except sqlite3.Error as exc:
                if not _is_sqlite_contention(exc):
                    raise HumanInputError("input_unavailable") from exc
                last_error = exc

            if attempt + 1 < _SQLITE_CONTENTION_ATTEMPTS:
                delay = _SQLITE_CONTENTION_RETRY_SECONDS * (2**attempt)
                if self._stop.wait(delay):
                    raise HumanInputError("input_interrupted") from last_error

        if isinstance(last_error, HumanInputError):
            raise last_error
        raise HumanInputError("input_unavailable") from last_error

    @property
    def control_fragment(self) -> str:
        return f"#capability={quote(self.capability, safe='')}"

    def participation_url(self, base_url: str) -> str:
        return (
            f"{base_url.rstrip('/')}/participate/{self.session_id}/{self.seat_id}"
            f"{self.control_fragment}"
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _create_session(self, connection: sqlite3.Connection) -> None:
        now = float(self._clock())
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE input_requests SET state='cancelled', move=NULL, resolved_at=? "
                "WHERE state IN ('pending','submitted','consumed') AND session_id IN ("
                "SELECT session_id FROM input_sessions "
                "WHERE status='active' AND lease_expires_at <= ?)",
                (now, now),
            )
            connection.execute(
                "UPDATE input_sessions SET status='interrupted', updated_at=?, "
                "terminal_reason_code='lease_expired', current_request_id=NULL "
                "WHERE status='active' AND lease_expires_at <= ?",
                (now, now),
            )
            connection.execute(
                "DELETE FROM input_sessions WHERE status != 'active' AND updated_at < ?",
                (now - INPUT_RETENTION_SECONDS,),
            )
            count = int(connection.execute("SELECT COUNT(*) FROM input_sessions").fetchone()[0])
            excess = count - INPUT_MAX_SESSIONS + 1
            if excess > 0:
                connection.execute(
                    "DELETE FROM input_sessions WHERE session_id IN ("
                    "SELECT session_id FROM input_sessions WHERE status != 'active' "
                    "ORDER BY updated_at, session_id LIMIT ?)",
                    (excess,),
                )
                count = int(
                    connection.execute("SELECT COUNT(*) FROM input_sessions").fetchone()[0]
                )
            if count >= INPUT_MAX_SESSIONS:
                connection.execute("ROLLBACK")
                raise HumanInputError("input_overloaded")
            connection.execute(
                """
                INSERT INTO input_sessions(
                    session_id, seat_id, owner_digest, capability_digest, status,
                    game, player_name, players_json, created_at, updated_at,
                    lease_expires_at, current_request_id, next_request_seq,
                    final_match_id, terminal_reason_code
                ) VALUES(?,?,?,?, 'active', ?,?,?,?,?,?, NULL,0,NULL,NULL)
                """,
                (
                    self.session_id,
                    self.seat_id,
                    _token_digest(self._owner_token),
                    _token_digest(self.capability),
                    self.game,
                    self.player_name,
                    _canonical_json(self.players),
                    now,
                    now,
                    now + INPUT_STALE_AFTER_SECONDS,
                ),
            )
            connection.commit()
        except HumanInputError:
            raise
        except (OSError, OverflowError, sqlite3.Error, TypeError, ValueError) as exc:
            connection.rollback()
            raise HumanInputError("input_unavailable") from exc

    def _heartbeat_once(self) -> None:
        connection = _connect(self.path, create=False)
        try:
            _validate_schema(connection)
            now = float(self._clock())
            updated = connection.execute(
                "UPDATE input_sessions SET updated_at=?, lease_expires_at=? "
                "WHERE session_id=? AND status='active' AND owner_digest=? "
                "AND lease_expires_at > ?",
                (
                    now,
                    now + INPUT_STALE_AFTER_SECONDS,
                    self.session_id,
                    _token_digest(self._owner_token),
                    now,
                ),
            ).rowcount
            connection.commit()
            if updated != 1:
                raise HumanInputError("input_session_lost")
        except HumanInputError:
            connection.rollback()
            raise
        except (OSError, OverflowError, sqlite3.Error, TypeError, ValueError) as exc:
            connection.rollback()
            raise HumanInputError("input_unavailable") from exc
        finally:
            connection.close()
            _secure_file(self.path)

    def _heartbeat(self) -> None:
        while not self._stop.wait(self._heartbeat_seconds):
            try:
                self._retry_sqlite_contention(self._heartbeat_once)
            except HumanInputError as exc:
                if self._stop.is_set():
                    return
                self._failure = exc
                self._stop.set()
                return

    def _owner_connection(self) -> sqlite3.Connection:
        if self._closed:
            raise HumanInputError("input_interrupted")
        if self._failure is not None:
            raise self._failure
        connection = _connect(self.path, create=False)
        try:
            _validate_schema(connection)
        except HumanInputError:
            connection.close()
            raise
        return connection

    async def resolve(
        self,
        prompt: str,
        *,
        timeout_seconds: float,
        match_event_seq: int = 0,
    ) -> str:
        """Publish one prompt and await exactly one browser submission."""

        if (
            not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or isinstance(match_event_seq, bool)
            or not isinstance(match_event_seq, int)
            or match_event_seq < 0
        ):
            raise HumanInputError("input_invalid")
        prompt = _validate_text(prompt, maximum=INPUT_MAX_PROMPT_CHARS)
        create_task = asyncio.create_task(
            asyncio.to_thread(
                self._retry_sqlite_contention,
                lambda: self._create_request(
                    prompt,
                    timeout_seconds,
                    match_event_seq,
                ),
            )
        )
        cancelled_error: asyncio.CancelledError | None = None
        while True:
            try:
                request_id = await asyncio.shield(create_task)
                break
            except asyncio.CancelledError as exc:
                if create_task.cancelled():
                    raise cancelled_error or exc
                cancelled_error = exc
            except Exception:
                if cancelled_error is not None:
                    raise cancelled_error from None
                raise

        if cancelled_error is not None:
            cancelled_error = await self._finish_cancelled_request(
                request_id,
                cancelled_error,
            )
            raise cancelled_error

        try:
            while True:
                outcome = await asyncio.to_thread(
                    self._retry_sqlite_contention,
                    lambda: self._consume_if_ready(request_id),
                )
                if outcome is not None:
                    return outcome
                await asyncio.sleep(self._poll_seconds)
        except _HumanInputTimeout as exc:
            raise PlayerTimeoutError(
                f"{self.player_name} 超时未作答",
                details={"timeout_seconds": timeout_seconds},
            ) from exc
        except asyncio.CancelledError as exc:
            cancelled_error = await self._finish_cancelled_request(request_id, exc)
            raise cancelled_error

    async def _finish_cancelled_request(
        self,
        request_id: str,
        cancelled_error: asyncio.CancelledError,
    ) -> asyncio.CancelledError:
        """Finish best-effort cleanup before propagating the latest cancellation."""

        cleanup_task = asyncio.create_task(
            asyncio.to_thread(
                self._retry_sqlite_contention,
                lambda: self._cancel_request(request_id),
            )
        )
        while True:
            try:
                await asyncio.shield(cleanup_task)
                return cancelled_error
            except asyncio.CancelledError as exc:
                if cleanup_task.cancelled():
                    return exc
                cancelled_error = exc
            except HumanInputError:
                # Cleanup is best-effort and must not replace cancellation.
                return cancelled_error

    def _create_request(
        self,
        prompt: str,
        timeout_seconds: float,
        match_event_seq: int,
    ) -> str:
        connection = self._owner_connection()
        now = float(self._clock())
        request_id = uuid.uuid4().hex
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM input_sessions WHERE session_id=?",
                (self.session_id,),
            ).fetchone()
            if row is None or not _constant_time_authorized(
                row, self._owner_token, "owner_digest"
            ):
                raise HumanInputError("input_session_lost")
            if row["status"] != "active" or row["lease_expires_at"] <= now:
                raise HumanInputError("input_interrupted")
            current_request_id = row["current_request_id"]
            if current_request_id is not None:
                current = connection.execute(
                    "SELECT state FROM input_requests WHERE request_id=? AND session_id=?",
                    (current_request_id, self.session_id),
                ).fetchone()
                if current is None or current["state"] not in {
                    "accepted",
                    "rejected",
                    "expired",
                    "cancelled",
                }:
                    raise HumanInputError("input_request_active")
            request_seq = row["next_request_seq"]
            if isinstance(request_seq, bool) or not isinstance(request_seq, int) or request_seq < 0:
                raise HumanInputError("input_invalid")
            connection.execute(
                """
                INSERT INTO input_requests(
                    request_id, session_id, request_seq, match_event_seq, state,
                    prompt, created_at, expires_at, submission_id, move,
                    move_digest, submitted_at, resolved_at, reason
                ) VALUES(?,?,?,?, 'pending', ?,?,?, NULL,NULL,NULL,NULL,NULL,NULL)
                """,
                (
                    request_id,
                    self.session_id,
                    request_seq,
                    match_event_seq,
                    prompt,
                    now,
                    now + timeout_seconds,
                ),
            )
            updated = connection.execute(
                "UPDATE input_sessions SET current_request_id=?, next_request_seq=?, "
                "updated_at=?, lease_expires_at=? WHERE session_id=? AND owner_digest=?",
                (
                    request_id,
                    request_seq + 1,
                    now,
                    now + INPUT_STALE_AFTER_SECONDS,
                    self.session_id,
                    _token_digest(self._owner_token),
                ),
            ).rowcount
            if updated != 1:
                raise HumanInputError("input_session_lost")
            connection.commit()
            return request_id
        except HumanInputError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise HumanInputError("input_unavailable") from exc
        finally:
            connection.close()
            _secure_file(self.path)

    def _consume_if_ready(self, request_id: str) -> str | None:
        connection = self._owner_connection()
        now = float(self._clock())
        try:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                "SELECT * FROM input_sessions WHERE session_id=?",
                (self.session_id,),
            ).fetchone()
            request = connection.execute(
                "SELECT * FROM input_requests WHERE request_id=? AND session_id=?",
                (request_id, self.session_id),
            ).fetchone()
            if session is None or request is None or not _constant_time_authorized(
                session, self._owner_token, "owner_digest"
            ):
                raise HumanInputError("input_session_lost")
            if session["status"] != "active":
                raise HumanInputError("input_interrupted")
            if session["lease_expires_at"] <= now:
                raise HumanInputError("input_session_lost")
            state = request["state"]
            if state == "submitted":
                move = request["move"]
                if not isinstance(move, str):
                    raise HumanInputError("input_invalid")
                connection.execute(
                    "UPDATE input_requests SET state='consumed', move=NULL WHERE request_id=?",
                    (request_id,),
                )
                connection.execute(
                    "UPDATE input_sessions SET updated_at=?, lease_expires_at=? "
                    "WHERE session_id=? AND owner_digest=?",
                    (
                        now,
                        now + INPUT_STALE_AFTER_SECONDS,
                        self.session_id,
                        _token_digest(self._owner_token),
                    ),
                )
                connection.commit()
                return move
            if state == "pending":
                if request["expires_at"] <= now:
                    connection.execute(
                        "UPDATE input_requests SET state='expired', resolved_at=? "
                        "WHERE request_id=?",
                        (now, request_id),
                    )
                    connection.execute(
                        "UPDATE input_sessions SET current_request_id=NULL, updated_at=? "
                        "WHERE session_id=? AND current_request_id=?",
                        (now, self.session_id, request_id),
                    )
                    connection.commit()
                    raise _HumanInputTimeout()
                connection.commit()
                return None
            if state == "expired":
                raise _HumanInputTimeout()
            if state == "cancelled":
                raise HumanInputError("input_interrupted")
            if state == "consumed":
                connection.commit()
                return None
            raise HumanInputError("input_invalid")
        except HumanInputError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise HumanInputError("input_unavailable") from exc
        finally:
            connection.close()
            _secure_file(self.path)

    def resolve_request(
        self,
        *,
        accepted: bool,
        reason: str | None = None,
    ) -> None:
        """Record the engine verdict for the most recently consumed move."""

        self._retry_sqlite_contention(
            lambda: self._resolve_request_once(accepted=accepted, reason=reason)
        )

    def _resolve_request_once(
        self,
        *,
        accepted: bool,
        reason: str | None,
    ) -> None:
        connection = self._owner_connection()
        now = float(self._clock())
        try:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                "SELECT * FROM input_sessions WHERE session_id=?",
                (self.session_id,),
            ).fetchone()
            if session is None or not _constant_time_authorized(
                session, self._owner_token, "owner_digest"
            ):
                raise HumanInputError("input_session_lost")
            if session["status"] != "active":
                raise HumanInputError("input_interrupted")
            if session["lease_expires_at"] <= now:
                raise HumanInputError("input_session_lost")
            request_id = session["current_request_id"]
            if not isinstance(request_id, str):
                connection.commit()
                return
            state = "accepted" if accepted else "rejected"
            safe_reason = None
            if reason is not None:
                safe_reason = _validate_text(str(reason), maximum=512)[:512]
            updated = connection.execute(
                "UPDATE input_requests SET state=?, resolved_at=?, reason=? "
                "WHERE request_id=? AND session_id=? AND state='consumed'",
                (state, now, safe_reason, request_id, self.session_id),
            ).rowcount
            if updated == 1:
                connection.execute(
                    "UPDATE input_sessions SET updated_at=?, lease_expires_at=? "
                    "WHERE session_id=? AND owner_digest=?",
                    (
                        now,
                        now + INPUT_STALE_AFTER_SECONDS,
                        self.session_id,
                        _token_digest(self._owner_token),
                    ),
                )
            connection.commit()
        except HumanInputError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise HumanInputError("input_unavailable") from exc
        finally:
            connection.close()
            _secure_file(self.path)

    def _cancel_request(self, request_id: str) -> None:
        connection = self._owner_connection()
        now = float(self._clock())
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE input_requests SET state='cancelled', move=NULL, resolved_at=? "
                "WHERE request_id=? AND session_id=? AND state IN ('pending','submitted','consumed')",
                (now, request_id, self.session_id),
            )
            connection.execute(
                "UPDATE input_sessions SET current_request_id=NULL, updated_at=? "
                "WHERE session_id=? AND current_request_id=? AND owner_digest=?",
                (
                    now,
                    self.session_id,
                    request_id,
                    _token_digest(self._owner_token),
                ),
            )
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise HumanInputError("input_unavailable") from exc
        finally:
            connection.close()
            try:
                _secure_file(self.path)
            except HumanInputError:
                pass

    def _terminal(self, status: Literal["completed", "interrupted"], value: str) -> bool:
        if self._closed:
            return False
        connection = self._owner_connection()
        now = float(self._clock())
        try:
            connection.execute("BEGIN IMMEDIATE")
            if status == "completed":
                if _SAFE_ID_RE.fullmatch(value) is None:
                    raise HumanInputError("input_invalid")
                final_match_id = value
                reason = None
            else:
                if _SAFE_REASON_RE.fullmatch(value) is None:
                    raise HumanInputError("input_invalid")
                final_match_id = None
                reason = value
            connection.execute(
                "UPDATE input_requests SET state='cancelled', move=NULL, resolved_at=? "
                "WHERE session_id=? AND state IN ('pending','submitted','consumed')",
                (now, self.session_id),
            )
            updated = connection.execute(
                "UPDATE input_sessions SET status=?, updated_at=?, current_request_id=NULL, "
                "final_match_id=?, terminal_reason_code=? "
                "WHERE session_id=? AND owner_digest=? AND status='active' "
                "AND lease_expires_at > ?",
                (
                    status,
                    now,
                    final_match_id,
                    reason,
                    self.session_id,
                    _token_digest(self._owner_token),
                    now,
                ),
            ).rowcount
            if updated not in {0, 1}:
                raise HumanInputError("input_invalid")
            connection.commit()
            if updated == 1:
                self._stop.set()
            return updated == 1
        except HumanInputError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise HumanInputError("input_unavailable") from exc
        finally:
            connection.close()
            _secure_file(self.path)

    def complete(self, final_match_id: str) -> bool:
        return self._terminal("completed", final_match_id)

    def interrupt(self, reason_code: str = "producer_failed") -> bool:
        return self._terminal("interrupted", reason_code)

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.interrupt("producer_closed")
        except HumanInputError:
            pass
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._closed = True


class WebSubmissionStore:
    """Narrow Web-side writer: load a capability seat and submit one move."""

    def __init__(self, database: str | Path, *, clock=time.time) -> None:
        self.path = derive_human_input_database_path(database)
        self._clock = clock

    def _connection(self) -> sqlite3.Connection:
        for attempt in range(_SQLITE_CONTENTION_ATTEMPTS):
            connection: sqlite3.Connection | None = None
            try:
                connection = _connect(self.path, create=False)
                _validate_schema(connection)
                return connection
            except HumanInputError as exc:
                if connection is not None:
                    try:
                        connection.close()
                    except sqlite3.Error:
                        pass
                if not _is_sqlite_contention(exc) or attempt + 1 == _SQLITE_CONTENTION_ATTEMPTS:
                    raise
                time.sleep(_SQLITE_CONTENTION_RETRY_SECONDS * (2**attempt))

        raise AssertionError("unreachable")

    def load(self, session_id: str, seat_id: str, *, capability: str) -> InputSnapshot:
        if _SAFE_ID_RE.fullmatch(session_id) is None or _SAFE_ID_RE.fullmatch(seat_id) is None:
            raise HumanInputError("participation_not_found")
        connection = self._connection()
        now = float(self._clock())
        try:
            session = connection.execute(
                "SELECT * FROM input_sessions WHERE session_id=? AND seat_id=?",
                (session_id, seat_id),
            ).fetchone()
            if session is None or not _constant_time_authorized(
                session, capability, "capability_digest"
            ):
                raise HumanInputError("input_forbidden")
            status: InputSessionStatus = session["status"]
            request = None
            if status == "active" and session["lease_expires_at"] <= now:
                status = "expired"
            elif status == "active" and isinstance(session["current_request_id"], str):
                row = connection.execute(
                    "SELECT * FROM input_requests WHERE request_id=? AND session_id=?",
                    (session["current_request_id"], session_id),
                ).fetchone()
                if row is None:
                    raise HumanInputError("input_invalid")
                state: InputRequestStatus = row["state"]
                if state == "pending" and row["expires_at"] <= now:
                    state = "expired"
                request = InputRequestSnapshot(
                    request_id=row["request_id"],
                    request_seq=row["request_seq"],
                    match_event_seq=row["match_event_seq"],
                    state=state,
                    prompt=row["prompt"],
                    created_at=_now_datetime(row["created_at"]),
                    expires_at=_now_datetime(row["expires_at"]),
                )
            try:
                players_raw = json.loads(session["players_json"])
            except (json.JSONDecodeError, TypeError) as exc:
                raise HumanInputError("input_invalid") from exc
            if not isinstance(players_raw, list) or not all(
                isinstance(player, str) for player in players_raw
            ):
                raise HumanInputError("input_invalid")
            players = tuple(players_raw)
            _validate_names(session["player_name"], players)
            return InputSnapshot(
                session_id=session["session_id"],
                seat_id=session["seat_id"],
                status=status,
                game=session["game"],
                player_name=session["player_name"],
                players=players,
                created_at=_now_datetime(session["created_at"]),
                updated_at=_now_datetime(session["updated_at"]),
                lease_expires_at=_now_datetime(session["lease_expires_at"]),
                request=request,
                final_match_id=session["final_match_id"],
            )
        except HumanInputError:
            raise
        except (OverflowError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise HumanInputError("input_unavailable") from exc
        finally:
            connection.close()

    def submit(
        self,
        session_id: str,
        seat_id: str,
        request_id: str,
        *,
        capability: str,
        move: str,
        submission_id: str,
    ) -> InputSubmitResult:
        if any(
            _SAFE_ID_RE.fullmatch(value) is None
            for value in (session_id, seat_id, request_id)
        ) or _SUBMISSION_ID_RE.fullmatch(submission_id) is None:
            raise HumanInputError("invalid_request")
        move = _validate_text(move, maximum=INPUT_MAX_MOVE_CHARS)
        connection = self._connection()
        now = float(self._clock())
        try:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                "SELECT * FROM input_sessions WHERE session_id=? AND seat_id=?",
                (session_id, seat_id),
            ).fetchone()
            if session is None or not _constant_time_authorized(
                session, capability, "capability_digest"
            ):
                raise HumanInputError("input_forbidden")
            digest = _move_digest(move, capability)
            if session["status"] != "active" or session["lease_expires_at"] <= now:
                raise HumanInputError("session_interrupted")
            if session["current_request_id"] != request_id:
                raise HumanInputError("request_stale")
            request = connection.execute(
                "SELECT * FROM input_requests WHERE request_id=? AND session_id=?",
                (request_id, session_id),
            ).fetchone()
            if request is None:
                raise HumanInputError("request_not_found")
            if (
                request["submission_id"] == submission_id
                and isinstance(request["move_digest"], bytes)
                and hmac.compare_digest(request["move_digest"], digest)
                and request["state"] in {"submitted", "consumed", "accepted", "rejected"}
            ):
                connection.commit()
                return InputSubmitResult(request_id=request_id, status="idempotent")
            if request["state"] != "pending":
                if request["state"] == "expired":
                    raise HumanInputError("request_expired")
                raise HumanInputError("already_submitted")
            if request["expires_at"] <= now:
                connection.execute(
                    "UPDATE input_requests SET state='expired', resolved_at=? WHERE request_id=?",
                    (now, request_id),
                )
                connection.execute(
                    "UPDATE input_sessions SET current_request_id=NULL, updated_at=? "
                    "WHERE session_id=? AND current_request_id=?",
                    (now, session_id, request_id),
                )
                connection.commit()
                raise HumanInputError("request_expired")
            connection.execute(
                "UPDATE input_requests SET state='submitted', submission_id=?, move=?, "
                "move_digest=?, submitted_at=? WHERE request_id=? AND state='pending'",
                (submission_id, move, digest, now, request_id),
            )
            connection.execute(
                "UPDATE input_sessions SET updated_at=? WHERE session_id=?",
                (now, session_id),
            )
            connection.commit()
            _secure_file(self.path)
            return InputSubmitResult(request_id=request_id, status="accepted")
        except HumanInputError:
            connection.rollback()
            raise
        except (OSError, OverflowError, sqlite3.Error, TypeError, ValueError) as exc:
            connection.rollback()
            raise HumanInputError("input_unavailable") from exc
        finally:
            connection.close()
            try:
                _secure_file(self.path)
            except HumanInputError:
                pass


class BrowserHumanPlayer(HumanPlayer):
    """A normal HumanPlayer whose asynchronous input comes from the sidecar."""

    def __init__(self, human: HumanPlayer, session: InputSessionStore) -> None:
        super().__init__(human.name, timeout=human.timeout, entrant_id=human.entrant_id)
        self._session = session
        self._prompt_event_seq = 0

    @classmethod
    def create(
        cls,
        database: str | Path,
        game: object,
        players: list[object],
        human: HumanPlayer,
    ) -> BrowserHumanPlayer:
        game_name = getattr(game, "name", None)
        names = tuple(getattr(player, "name", None) for player in players)
        if (
            not isinstance(game_name, str)
            or not 2 <= len(names) <= MAX_PLATFORM_PLAYERS
            or not all(isinstance(name, str) for name in names)
            or not any(player is human for player in players)
        ):
            raise HumanInputError("input_invalid")
        session = InputSessionStore(
            database,
            game=game_name,
            player_name=human.name,
            players=names,
        )
        return cls(human, session)

    @property
    def session_id(self) -> str:
        return self._session.session_id

    @property
    def seat_id(self) -> str:
        return self._session.seat_id

    def participation_url(self, base_url: str) -> str:
        return self._session.participation_url(base_url)

    async def get_move(self, prompt: str) -> str:
        if self.timeout is None:
            raise HumanInputError("input_invalid")
        return await self._session.resolve(
            prompt,
            timeout_seconds=self.timeout,
            match_event_seq=self._prompt_event_seq,
        )

    def observe_event(self, event: MatchEvent) -> None:
        if event.player != self.name:
            return
        if event.type == EventType.TURN_PROMPT:
            self._prompt_event_seq = event.seq
        elif event.type == EventType.MOVE_RECEIVED:
            self._session.resolve_request(accepted=True)
        elif event.type == EventType.MOVE_REJECTED:
            reason = event.data.get("reason")
            self._session.resolve_request(
                accepted=False,
                reason=reason if isinstance(reason, str) else None,
            )

    def complete(self, final_match_id: str) -> None:
        self._session.complete(final_match_id)

    def interrupt(self, reason_code: str = "producer_failed") -> None:
        self._session.interrupt(reason_code)

    def close(self) -> None:
        self._session.close()


__all__ = [
    "INPUT_MAX_MOVE_CHARS",
    "INPUT_MAX_PROMPT_CHARS",
    "INPUT_SCHEMA_VERSION",
    "BrowserHumanPlayer",
    "HumanInputError",
    "InputRequestSnapshot",
    "InputSessionStatus",
    "InputSessionStore",
    "InputSnapshot",
    "InputSubmitResult",
    "WebSubmissionStore",
    "derive_human_input_database_path",
    "derive_input_database_path",
    "human_input_database_path",
]
