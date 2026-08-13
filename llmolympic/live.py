"""Bounded, disclosure-safe live-event sidecar for local spectators.

The match process is the sole writer.  The optional Web process opens the
sidecar read-only, so a spectator can never call providers or mutate archives.
All producer-facing methods are best effort: losing live observation must not
change match, archive, budget, or rating semantics.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import secrets
import sqlite3
import stat
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import Literal, Self

from llmolympic.core.events import EventType, MatchEvent
from llmolympic.web.models import PublicEvent

LIVE_SCHEMA_VERSION = 1
LIVE_MAX_EVENTS = 10_000
LIVE_MAX_BYTES = 16 * 1024 * 1024
LIVE_MAX_EVENT_BYTES = 256 * 1024
LIVE_DEFAULT_PAGE_LIMIT = 256
LIVE_MAX_PAGE_LIMIT = 1_000
LIVE_STALE_AFTER_SECONDS = 60.0
LIVE_RETENTION_SECONDS = 24 * 60 * 60.0
LIVE_MAX_SESSIONS = 256
LIVE_DEFAULT_QUEUE_SIZE = 256
LIVE_DEFAULT_HEARTBEAT_SECONDS = 10.0
LIVE_CLOSE_TIMEOUT_SECONDS = 5.0

GameMode = Literal["play", "series", "round_robin"]
LiveStatus = Literal["running", "completed", "interrupted"]
LiveFinalKind = Literal["match", "series", "tournament"]

_MODES = frozenset({"play", "series", "round_robin"})
_FINAL_KINDS = frozenset({"match", "series", "tournament"})
_MODE_FINAL_KIND = {"play": "match", "series": "series", "round_robin": "tournament"}
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SAFE_GAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SAFE_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_CONTEXT_KEYS = frozenset({"leg_number", "pairing_number", "pairing_count"})
_MAX_METADATA_BYTES = 1024 * 1024
_BIDI_CONTROL_CHARACTERS = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")

def derive_live_database_path(database: str | Path) -> Path:
    """Return the private live sidecar path for an archive database."""

    return Path(f"{Path(database).expanduser().resolve(strict=False)}.live.db")


live_database_path = derive_live_database_path


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _public_event(event: MatchEvent) -> dict[str, object]:
    """Project via the canonical Web allow-list before any bytes reach disk."""

    return PublicEvent.from_event(event).model_dump(mode="json")


def _contexts(
    mode: GameMode,
    event: MatchEvent,
    context: Mapping[str, object] | None,
) -> tuple[dict[str, int], dict[str, int]]:
    if isinstance(event.seq, bool) or not isinstance(event.seq, int) or event.seq < 0:
        raise ValueError("invalid match event sequence")
    supplied = {} if context is None else dict(context)
    if set(supplied) - _CONTEXT_KEYS:
        raise ValueError("invalid live context field")
    for key, value in supplied.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"invalid {key}")

    if mode == "play":
        if supplied:
            raise ValueError("play does not accept series context")
    elif mode == "series":
        if set(supplied) != {"leg_number"} or supplied["leg_number"] not in {1, 2}:
            raise ValueError("series requires leg_number one or two")
    else:
        if set(supplied) != {"leg_number", "pairing_number", "pairing_count"}:
            raise ValueError("round_robin requires complete pairing context")
        if supplied["leg_number"] not in {1, 2}:
            raise ValueError("round_robin leg_number must be one or two")
        if supplied["pairing_number"] > supplied["pairing_count"]:
            raise ValueError("pairing_number exceeds pairing_count")

    event_context = {"match_event_seq": event.seq}
    for key in ("leg_number", "pairing_number"):
        if key in supplied:
            event_context[key] = supplied[key]
    return event_context, supplied


def _session_context(context: Mapping[str, object]) -> str:
    return _canonical_json(
        {
            key: context[key]
            for key in ("leg_number", "pairing_number", "pairing_count")
            if key in context
        }
    )


def _valid_public_players(players: object) -> bool:
    if not isinstance(players, (list, tuple)) or len(players) < 2:
        return False
    return all(
        isinstance(player, str)
        and bool(player)
        and len(player) <= 512
        and not any(
            ord(character) < 32
            or 127 <= ord(character) <= 159
            or 0xD800 <= ord(character) <= 0xDFFF
            or character in _BIDI_CONTROL_CHARACTERS
            for character in player
        )
        for player in players
    )


def _secure_mode(path: Path, mode: int, *, required: bool = False) -> None:
    try:
        if path.exists():
            path.chmod(mode)
            if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) != mode:
                raise PermissionError(f"could not protect {path}")
    except OSError:
        if required:
            raise


def _token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()


class LivePublisher:
    """Best-effort background writer for one top-level CLI live session."""

    def __init__(
        self,
        database: str | Path,
        mode: GameMode,
        *,
        queue_size: int = LIVE_DEFAULT_QUEUE_SIZE,
        heartbeat_seconds: float = LIVE_DEFAULT_HEARTBEAT_SECONDS,
        max_events: int = LIVE_MAX_EVENTS,
        max_bytes: int = LIVE_MAX_BYTES,
        max_event_bytes: int = LIVE_MAX_EVENT_BYTES,
        retention_seconds: float = LIVE_RETENTION_SECONDS,
        max_sessions: int = LIVE_MAX_SESSIONS,
        clock=time.time,
    ) -> None:
        self.path = derive_live_database_path(database)
        self.mode = mode
        self.failed = False
        self._clock = clock
        self._heartbeat_seconds = float(heartbeat_seconds)
        self._max_events = max_events
        self._max_bytes = max_bytes
        self._max_event_bytes = max_event_bytes
        self._retention_seconds = float(retention_seconds)
        self._max_sessions = max_sessions
        self._queue: queue.Queue[object] = queue.Queue(maxsize=queue_size)
        self._owned: dict[str, str] = {}
        self._session_contexts: dict[str, dict[str, int]] = {}
        self._accepting = True
        self._closed = threading.Event()
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None

        try:
            if mode not in _MODES:
                raise ValueError("invalid live mode")
            if not isinstance(queue_size, int) or isinstance(queue_size, bool) or queue_size < 1:
                raise ValueError("queue_size must be positive")
            if not 0 < self._heartbeat_seconds < LIVE_STALE_AFTER_SECONDS:
                raise ValueError("heartbeat_seconds is outside the live lease")
            for value in (max_events, max_bytes, max_event_bytes, max_sessions):
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise ValueError("live limits must be positive integers")
            if max_events > LIVE_MAX_EVENTS:
                raise ValueError("max_events exceeds the reader limit")
            if max_bytes > LIVE_MAX_BYTES:
                raise ValueError("max_bytes exceeds the reader limit")
            if max_event_bytes > LIVE_MAX_EVENT_BYTES:
                raise ValueError("max_event_bytes exceeds the reader limit")
            if max_sessions > LIVE_MAX_SESSIONS:
                raise ValueError("max_sessions exceeds the reader limit")
            if max_event_bytes > max_bytes:
                raise ValueError("max_event_bytes cannot exceed max_bytes")
            if not math.isfinite(self._retention_seconds) or self._retention_seconds < 0:
                raise ValueError("retention_seconds must be finite and non-negative")
            self._thread = threading.Thread(
                target=self._worker,
                name="llmolympic-live-publisher",
                daemon=True,
            )
            self._thread.start()
            self._ready.wait(timeout=LIVE_CLOSE_TIMEOUT_SECONDS)
            if not self._ready.is_set():
                self.failed = True
                self._accepting = False
                self._stop_requested.set()
        except Exception:  # noqa: BLE001 - live observation must never abort a match
            self.failed = True
            self._accepting = False
            self._stop_requested.set()
            self._ready.set()
            if self._thread is None or not self._thread.is_alive():
                self._closed.set()

    def _enqueue(self, item: object) -> bool:
        if not self._accepting or self.failed:
            return False
        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            self.failed = True
            self._accepting = False
            self._stop_requested.set()
            return False

    def start_session(
        self,
        event: MatchEvent,
        context: Mapping[str, object] | None = None,
    ) -> str | None:
        """Start one top-level run and enqueue its first ``match_started`` event."""

        try:
            if event.type != EventType.MATCH_STARTED:
                return None
            projected = _public_event(event)
            event_context, session_context = _contexts(self.mode, event, context)
            data = projected.get("data")
            if not isinstance(data, Mapping):
                return None
            game = data.get("game")
            players = data.get("players")
            if (
                not isinstance(game, str)
                or _SAFE_GAME_RE.fullmatch(game) is None
                or not _valid_public_players(players)
            ):
                return None
            live_id = uuid.uuid4().hex
            token = secrets.token_urlsafe(32)
            if not self._enqueue(
                (
                    "start",
                    live_id,
                    token,
                    game,
                    players,
                    projected,
                    event_context,
                    session_context,
                )
            ):
                return None
            self._owned[live_id] = token
            self._session_contexts[live_id] = session_context
            return live_id
        except Exception:  # noqa: BLE001 - live observation must never abort a match
            self.failed = True
            self._accepting = False
            self._stop_requested.set()
            return None

    def publish(
        self,
        live_id: str,
        event: MatchEvent,
        context: Mapping[str, object] | None = None,
    ) -> bool:
        try:
            token = self._owned.get(live_id)
            if token is None:
                return False
            event_context, session_context = _contexts(self.mode, event, context)
            previous_context = self._session_contexts.get(live_id)
            if previous_context is None:
                return False
            if (
                self.mode == "round_robin"
                and session_context["pairing_count"] != previous_context["pairing_count"]
            ):
                return False
            if not self._enqueue(
                (
                    "event",
                    live_id,
                    token,
                    _public_event(event),
                    event_context,
                    session_context,
                )
            ):
                return False
            self._session_contexts[live_id] = session_context
            return True
        except Exception:  # noqa: BLE001 - live observation must never abort a match
            self.failed = True
            self._accepting = False
            self._stop_requested.set()
            return False

    def complete(
        self,
        live_id: str,
        *,
        final_kind: LiveFinalKind | None = None,
        final_id: str | None = None,
        final_match_ids: Sequence[str] = (),
        final_match_id: str | None = None,
        series_id: str | None = None,
        tournament_id: str | None = None,
        match_ids: Sequence[str] = (),
    ) -> bool:
        """Queue a terminal success; legacy keyword spellings are normalized."""

        try:
            token = self._owned.get(live_id)
            if token is None:
                return False
            legacy = [
                ("match", final_match_id),
                ("series", series_id),
                ("tournament", tournament_id),
            ]
            supplied = [(kind, value) for kind, value in legacy if value is not None]
            if supplied:
                if final_kind is not None or final_id is not None or len(supplied) != 1:
                    return False
                final_kind, final_id = supplied[0]
            if final_kind is None:
                final_kind = _MODE_FINAL_KIND[self.mode]  # type: ignore[assignment]
            if final_kind not in _FINAL_KINDS or not self._safe_id(final_id):
                return False
            if final_kind != _MODE_FINAL_KIND[self.mode]:
                return False
            if final_match_ids and match_ids:
                return False
            source_ids = final_match_ids or match_ids
            if isinstance(source_ids, (str, bytes)):
                return False
            ids = tuple(source_ids)
            if final_kind == "match" and not ids:
                ids = (str(final_id),)
            if (
                not ids
                or len(ids) > LIVE_MAX_EVENTS
                or len(ids) != len(set(ids))
                or any(not self._safe_id(value) for value in ids)
            ):
                return False
            if len(_canonical_json(list(ids)).encode("utf-8")) > _MAX_METADATA_BYTES:
                return False
            if self.mode == "play" and ids != (final_id,):
                return False
            if self.mode == "series" and len(ids) != 2:
                return False
            if self.mode == "round_robin":
                session_context = self._session_contexts.get(live_id)
                if session_context is None:
                    return False
                pairing_count = session_context.get("pairing_count")
                if pairing_count is None or len(ids) != pairing_count * 2:
                    return False
            if not self._enqueue(("complete", live_id, token, final_kind, final_id, ids)):
                return False
            self._owned.pop(live_id, None)
            self._session_contexts.pop(live_id, None)
            return True
        except Exception:  # noqa: BLE001 - live observation must never abort a match
            self.failed = True
            self._accepting = False
            self._stop_requested.set()
            return False

    def interrupt(
        self,
        live_id: str | None = None,
        reason_code: str = "interrupted",
    ) -> bool:
        try:
            if _SAFE_REASON_RE.fullmatch(reason_code) is None:
                return False
            targets = list(self._owned) if live_id is None else [live_id]
            success = bool(targets)
            for target in targets:
                token = self._owned.get(target)
                if token is None or not self._enqueue(
                    ("interrupt", target, token, reason_code)
                ):
                    success = False
                    continue
                self._owned.pop(target, None)
                self._session_contexts.pop(target, None)
            return success
        except Exception:  # noqa: BLE001 - live observation must never abort a match
            self.failed = True
            self._accepting = False
            self._stop_requested.set()
            return False

    def close(self, timeout: float = LIVE_CLOSE_TIMEOUT_SECONDS) -> None:
        """Interrupt owned sessions and wait a bounded time for queued writes."""

        if self._closed.is_set():
            return
        try:
            self.interrupt(reason_code="publisher_closed")
            self._accepting = False
            self._stop_requested.set()
            if self._thread is not None:
                self._thread.join(timeout=max(0.0, float(timeout)))
                if self._thread.is_alive():
                    self.failed = True
        except Exception:  # noqa: BLE001 - live observation must never abort a match
            self.failed = True
            self._accepting = False
            self._stop_requested.set()
        if self._thread is None or not self._thread.is_alive():
            self._closed.set()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        reason = "publisher_closed" if exc_type is None else "producer_failed"
        self.interrupt(reason_code=reason)
        self.close()

    @staticmethod
    def _safe_id(value: object) -> bool:
        return isinstance(value, str) and _SAFE_ID_RE.fullmatch(value) is not None

    def _connect(self) -> sqlite3.Connection:
        parent_created = not self.path.parent.exists()
        self.path.parent.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
        if parent_created:
            _secure_mode(self.path.parent, _PRIVATE_DIRECTORY_MODE, required=True)
        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                _PRIVATE_FILE_MODE,
            )
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        _secure_mode(self.path, _PRIVATE_FILE_MODE, required=True)
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        if connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0] != "delete":
            raise sqlite3.OperationalError("live sidecar requires DELETE journal mode")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 1000")
        self._initialize(connection)
        _secure_mode(self.path, _PRIVATE_FILE_MODE, required=True)
        for suffix in _SIDECAR_SUFFIXES:
            _secure_mode(Path(f"{self.path}{suffix}"), _PRIVATE_FILE_MODE)
        return connection

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in (0, LIVE_SCHEMA_VERSION):
            raise RuntimeError("unsupported live sidecar schema")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS live_sessions (
                    live_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
                    mode TEXT NOT NULL CHECK (mode IN ('play', 'series', 'round_robin')),
                    game TEXT NOT NULL,
                    players_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'interrupted')),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    heartbeat_at REAL NOT NULL,
                    lease_expires_at REAL NOT NULL,
                    current_context_json TEXT NOT NULL,
                    next_seq INTEGER NOT NULL CHECK (next_seq >= 0),
                    event_count INTEGER NOT NULL CHECK (event_count >= 0),
                    event_bytes INTEGER NOT NULL CHECK (event_bytes >= 0),
                    final_kind TEXT CHECK (final_kind IN ('match', 'series', 'tournament')),
                    final_id TEXT,
                    final_match_ids_json TEXT,
                    interruption_code TEXT,
                    owner_token_digest BLOB NOT NULL,
                    CHECK (
                        (status = 'running' AND final_kind IS NULL AND final_id IS NULL
                         AND final_match_ids_json IS NULL AND interruption_code IS NULL)
                        OR
                        (status = 'completed' AND final_kind IS NOT NULL AND final_id IS NOT NULL
                         AND final_match_ids_json IS NOT NULL AND interruption_code IS NULL)
                        OR
                        (status = 'interrupted' AND final_kind IS NULL AND final_id IS NULL
                         AND final_match_ids_json IS NULL AND interruption_code IS NOT NULL)
                    ),
                    CHECK (next_seq = event_count)
                );
                CREATE INDEX IF NOT EXISTS live_sessions_status_updated_idx
                    ON live_sessions(status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS live_sessions_updated_idx
                    ON live_sessions(updated_at DESC);
                CREATE TABLE IF NOT EXISTS live_events (
                    live_id TEXT NOT NULL REFERENCES live_sessions(live_id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL CHECK (seq >= 0),
                    created_at REAL NOT NULL,
                    event_bytes INTEGER NOT NULL CHECK (event_bytes > 0),
                    event_json TEXT NOT NULL,
                    PRIMARY KEY (live_id, seq)
                ) WITHOUT ROWID;
                """
            )
            connection.execute(f"PRAGMA user_version = {LIVE_SCHEMA_VERSION}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _worker(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            self._cleanup(connection)
            self._ready.set()
            next_heartbeat = time.monotonic() + self._heartbeat_seconds
            while True:
                if self._stop_requested.is_set() and self._queue.empty():
                    return
                timeout = max(
                    0.0,
                    min(next_heartbeat - time.monotonic(), 0.1),
                )
                try:
                    item = self._queue.get(timeout=timeout)
                except queue.Empty:
                    if self._stop_requested.is_set() and self._queue.empty():
                        return
                    if time.monotonic() >= next_heartbeat:
                        self._heartbeat(connection)
                        next_heartbeat = time.monotonic() + self._heartbeat_seconds
                    continue
                try:
                    self._apply(connection, item)
                finally:
                    self._queue.task_done()
        except Exception:  # noqa: BLE001 - worker failures disable only live observation
            self.failed = True
            self._accepting = False
            self._stop_requested.set()
            self._ready.set()
        finally:
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
                else:
                    self._queue.task_done()
            if connection is not None:
                connection.close()
            self._closed.set()

    def _cleanup(self, connection: sqlite3.Connection) -> None:
        now = float(self._clock())
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                UPDATE live_sessions SET
                    status = 'interrupted', updated_at = ?, lease_expires_at = ?,
                    final_kind = NULL, final_id = NULL, final_match_ids_json = NULL,
                    interruption_code = 'lease_expired'
                WHERE status = 'running' AND lease_expires_at <= ?
                """,
                (now, now, now),
            )
            connection.execute(
                "DELETE FROM live_sessions WHERE status != 'running' AND updated_at < ?",
                (now - self._retention_seconds,),
            )
            count = int(connection.execute("SELECT count(*) FROM live_sessions").fetchone()[0])
            excess = max(0, count - self._max_sessions + 1)
            if excess:
                connection.execute(
                    """
                    DELETE FROM live_sessions WHERE live_id IN (
                        SELECT live_id FROM live_sessions
                        WHERE status != 'running' ORDER BY updated_at ASC, live_id ASC LIMIT ?
                    )
                    """,
                    (excess,),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _heartbeat(self, connection: sqlite3.Connection) -> None:
        if not self._owned:
            return
        now = float(self._clock())
        for live_id, token in tuple(self._owned.items()):
            connection.execute(
                """
                UPDATE live_sessions SET heartbeat_at = ?, lease_expires_at = ?
                WHERE live_id = ? AND status = 'running' AND owner_token_digest = ?
                """,
                (now, now + LIVE_STALE_AFTER_SECONDS, live_id, _token_digest(token)),
            )

    def _apply(self, connection: sqlite3.Connection, item: object) -> None:
        if not isinstance(item, tuple) or not item:
            raise TypeError("invalid live command")
        command = item[0]
        if command == "start":
            _, live_id, token, game, players, event, event_context, session_context = item
            self._insert_start(
                connection,
                live_id,
                token,
                game,
                players,
                event,
                event_context,
                session_context,
            )
        elif command == "event":
            _, live_id, token, event, event_context, session_context = item
            self._insert_event(
                connection,
                live_id,
                token,
                event,
                event_context,
                session_context,
            )
        elif command == "complete":
            _, live_id, token, final_kind, final_id, ids = item
            self._finish(connection, live_id, token, final_kind, final_id, ids)
        elif command == "interrupt":
            _, live_id, token, reason = item
            self._interrupt(connection, live_id, token, reason)
        else:
            raise TypeError("invalid live command")

    def _insert_start(
        self,
        connection: sqlite3.Connection,
        live_id: str,
        token: str,
        game: str,
        players: object,
        event: Mapping[str, object],
        event_context: Mapping[str, int],
        session_context: Mapping[str, int],
    ) -> None:
        now = float(self._clock())
        connection.execute("BEGIN IMMEDIATE")
        try:
            count = int(connection.execute("SELECT count(*) FROM live_sessions").fetchone()[0])
            if count >= self._max_sessions:
                raise RuntimeError("live session limit reached")
            connection.execute(
                """
                INSERT INTO live_sessions (
                    live_id, schema_version, mode, game, players_json, status,
                    created_at, updated_at, heartbeat_at, lease_expires_at,
                    current_context_json, next_seq, event_count, event_bytes,
                    owner_token_digest
                ) VALUES (?, 1, ?, ?, ?, 'running', ?, ?, ?, ?, ?, 0, 0, 0, ?)
                """,
                (
                    live_id,
                    self.mode,
                    game,
                    _canonical_json(players),
                    now,
                    now,
                    now,
                    now + LIVE_STALE_AFTER_SECONDS,
                    _session_context(session_context),
                    _token_digest(token),
                ),
            )
            self._insert_event_in_transaction(
                connection,
                live_id,
                token,
                event,
                event_context,
                session_context,
                now,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        live_id: str,
        token: str,
        event: Mapping[str, object],
        event_context: Mapping[str, int],
        session_context: Mapping[str, int],
    ) -> None:
        now = float(self._clock())
        connection.execute("BEGIN IMMEDIATE")
        try:
            self._insert_event_in_transaction(
                connection,
                live_id,
                token,
                event,
                event_context,
                session_context,
                now,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _insert_event_in_transaction(
        self,
        connection: sqlite3.Connection,
        live_id: str,
        token: str,
        event: Mapping[str, object],
        event_context: Mapping[str, int],
        session_context: Mapping[str, int],
        now: float,
    ) -> None:
        row = connection.execute(
            """
            SELECT next_seq, event_count, event_bytes FROM live_sessions
            WHERE live_id = ? AND status = 'running' AND owner_token_digest = ?
            """,
            (live_id, _token_digest(token)),
        ).fetchone()
        if row is None:
            raise RuntimeError("live session lease lost")
        seq = int(row["next_seq"])
        payload = {"seq": seq, "context": dict(event_context), "event": dict(event)}
        serialized = _canonical_json(payload)
        event_bytes = len(serialized.encode("utf-8"))
        if event_bytes > self._max_event_bytes:
            raise RuntimeError("live event size limit reached")
        if int(row["event_count"]) >= self._max_events:
            raise RuntimeError("live event count limit reached")
        if int(row["event_bytes"]) + event_bytes > self._max_bytes:
            raise RuntimeError("live byte limit reached")
        connection.execute(
            """
            INSERT INTO live_events(live_id, seq, created_at, event_bytes, event_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (live_id, seq, now, event_bytes, serialized),
        )
        updated = connection.execute(
            """
            UPDATE live_sessions SET
                updated_at = ?, heartbeat_at = ?, lease_expires_at = ?,
                current_context_json = ?, next_seq = next_seq + 1,
                event_count = event_count + 1, event_bytes = event_bytes + ?
            WHERE live_id = ? AND status = 'running' AND owner_token_digest = ?
              AND next_seq = ?
            """,
            (
                now,
                now,
                now + LIVE_STALE_AFTER_SECONDS,
                _session_context(session_context),
                event_bytes,
                live_id,
                _token_digest(token),
                seq,
            ),
        )
        if updated.rowcount != 1:
            raise RuntimeError("live sequence changed concurrently")

    def _finish(
        self,
        connection: sqlite3.Connection,
        live_id: str,
        token: str,
        final_kind: str,
        final_id: str,
        ids: Sequence[str],
    ) -> None:
        now = float(self._clock())
        updated = connection.execute(
            """
            UPDATE live_sessions SET status = 'completed', updated_at = ?, heartbeat_at = ?,
                lease_expires_at = ?, final_kind = ?, final_id = ?, final_match_ids_json = ?
            WHERE live_id = ? AND status = 'running' AND owner_token_digest = ?
            """,
            (
                now,
                now,
                now,
                final_kind,
                final_id,
                _canonical_json(list(ids)),
                live_id,
                _token_digest(token),
            ),
        )
        if updated.rowcount != 1:
            raise RuntimeError("live session lease lost")

    def _interrupt(
        self,
        connection: sqlite3.Connection,
        live_id: str,
        token: str,
        reason: str,
    ) -> None:
        now = float(self._clock())
        updated = connection.execute(
            """
            UPDATE live_sessions SET status = 'interrupted', updated_at = ?, heartbeat_at = ?,
                lease_expires_at = ?, interruption_code = ?
            WHERE live_id = ? AND status = 'running' AND owner_token_digest = ?
            """,
            (now, now, now, reason, live_id, _token_digest(token)),
        )
        if updated.rowcount != 1:
            raise RuntimeError("live session lease lost")


def inspect_live_database(path: str | Path) -> dict[str, object]:
    """Small read-only diagnostic for tests and optional Web integration."""

    resolved = Path(path).expanduser().resolve(strict=False)
    if not resolved.is_file():
        return {"available": False, "schema_version": None}
    uri = f"{resolved.as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, isolation_level=None)) as connection:
            connection.execute("PRAGMA query_only = ON")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            count = int(connection.execute("SELECT count(*) FROM live_sessions").fetchone()[0])
        return {
            "available": version == LIVE_SCHEMA_VERSION,
            "schema_version": version,
            "session_count": count,
        }
    except sqlite3.Error:
        return {"available": False, "schema_version": None}


__all__ = [
    "LIVE_CLOSE_TIMEOUT_SECONDS",
    "LIVE_DEFAULT_HEARTBEAT_SECONDS",
    "LIVE_DEFAULT_PAGE_LIMIT",
    "LIVE_DEFAULT_QUEUE_SIZE",
    "LIVE_MAX_BYTES",
    "LIVE_MAX_EVENTS",
    "LIVE_MAX_EVENT_BYTES",
    "LIVE_MAX_PAGE_LIMIT",
    "LIVE_MAX_SESSIONS",
    "LIVE_RETENTION_SECONDS",
    "LIVE_SCHEMA_VERSION",
    "LIVE_STALE_AFTER_SECONDS",
    "GameMode",
    "LiveFinalKind",
    "LivePublisher",
    "LiveStatus",
    "derive_live_database_path",
    "inspect_live_database",
    "live_database_path",
]
