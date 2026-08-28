"""Isolated child-process runner for local Web control jobs.

The runner deliberately treats child output as untrusted.  It drains both
streams without logging them and accepts only bounded protocol frames carrying
the unpredictable token generated for that one process.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import ctypes
import json
import os
import re
import secrets
import signal
import sqlite3
import stat
import sys
import threading
import time
from collections.abc import Mapping
from ctypes import wintypes
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal
from urllib.parse import urlsplit

from pydantic import ValidationError

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows uses msvcrt below.
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX uses fcntl above.
    msvcrt = None  # type: ignore[assignment]

from llmolympic.config import (
    ProviderProfile,
    is_profile_credential_environment_name,
    load_profiles,
)
from llmolympic.control import (
    MAX_PROFILE_API_KEY_BYTES,
    ControlError,
    ControlFinalKind,
    ControlJob,
    ControlParticipationLink,
    JobStore,
    build_job_argv,
    profile_configuration_digest,
    validate_job_spec,
)
from llmolympic.core.storage import SCHEMA_VERSION

_PROTOCOL_PREFIX: Final[bytes] = b"@@LLMOLYMPIC_CONTROL_V1:"
_PROTOCOL_TOKEN_ENV: Final[str] = "LLMOLYMPIC_CONTROL_PROTOCOL_TOKEN"  # noqa: S105
_PROTOCOL_JOB_ENV: Final[str] = "LLMOLYMPIC_CONTROL_JOB_ID"
_PROTOCOL_PARENT_WATCHDOG_ENV: Final[str] = "LLMOLYMPIC_CONTROL_PARENT_WATCHDOG"
_PROTOCOL_PROFILE_SNAPSHOT_ENV: Final[str] = "LLMOLYMPIC_CONTROL_PROFILE_SNAPSHOT"
_MAX_PROTOCOL_JSON_BYTES: Final[int] = 16_384
_MAX_PROTOCOL_FRAME_BYTES: Final[int] = 24_000
_MAX_STDOUT_LINE_BYTES: Final[int] = 64 * 1024
_READ_CHUNK_BYTES: Final[int] = 8 * 1024
_STARTUP_READY_SECONDS: Final[float] = 5.0
_CANCEL_GRACE_SECONDS: Final[float] = 5.0
_TERMINATE_GRACE_SECONDS: Final[float] = 2.0
_KILL_GRACE_SECONDS: Final[float] = 1.0
_ARCHIVE_RECONCILE_ATTEMPTS: Final[int] = 3
_ARCHIVE_RECONCILE_RETRY_SECONDS: Final[float] = 0.05
_ARCHIVE_SQLITE_TIMEOUT_SECONDS: Final[float] = 0.25
_PRIOR_CHILD_EXIT_ATTEMPTS: Final[int] = 10
_PRIOR_CHILD_EXIT_RETRY_SECONDS: Final[float] = 0.05
_ACTIVE_STATUSES = frozenset(
    {"starting", "running", "finalizing", "cancel_requested"}
)
_TERMINAL_STATUSES = frozenset({"cancelled", "completed", "failed", "interrupted"})
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_IDEMPOTENCY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_BASE64URL_RE = re.compile(rb"[A-Za-z0-9_-]+\Z")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_SAFE_ENVIRONMENT_NAMES = frozenset(
    {
        "CURL_CA_BUNDLE",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LLMOLYMPIC_CONFIG",
        "NO_PROXY",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    }
)

_ArchiveCheck = Literal["found", "absent", "unavailable"]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate protocol field")
        value[key] = item
    return value


def _validate_web_base_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 512:
        raise ControlError("invalid_request")
    try:
        parsed = urlsplit(value)
        parsed_port = parsed.port
        port = 80 if parsed_port is None else parsed_port
    except ValueError as exc:
        raise ControlError("invalid_request") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port < 1
    ):
        raise ControlError("invalid_request")
    display_host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"http://{display_host}:{port}"


def _safe_identifier(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE_ID_RE.fullmatch(value) else None


def _safe_final_fields(
    payload: Mapping[str, object],
    *,
    required: bool,
) -> tuple[ControlFinalKind | None, str | None, tuple[str, ...] | None] | None:
    raw_kind = payload.get("final_kind")
    raw_id = payload.get("final_id")
    raw_match_ids = payload.get("final_match_ids")
    if not required and raw_kind is None and raw_id is None and raw_match_ids is None:
        return None, None, None
    if raw_kind not in {"match", "series", "tournament", "championship"}:
        return None
    final_id = _safe_identifier(raw_id)
    if final_id is None or not isinstance(raw_match_ids, list) or len(raw_match_ids) > 4096:
        return None
    match_ids: list[str] = []
    for item in raw_match_ids:
        match_id = _safe_identifier(item)
        if match_id is None:
            return None
        match_ids.append(match_id)
    if len(set(match_ids)) != len(match_ids):
        return None
    return raw_kind, final_id, tuple(match_ids)


def _validated_final_update(
    current: ControlJob,
    fields: tuple[ControlFinalKind, str, tuple[str, ...]],
    *,
    status: str,
) -> tuple[ControlFinalKind, str, tuple[str, ...]] | None:
    """Reject a child result before it can poison the persisted job row."""

    final_kind, final_id, final_match_ids = fields
    if current.final_kind is not None and (
        current.final_kind != final_kind
        or current.final_id != final_id
        or current.final_match_ids != final_match_ids
    ):
        return None
    payload = current.model_dump(mode="json")
    payload.update(
        {
            "status": status,
            "final_kind": final_kind,
            "final_id": final_id,
            "final_match_ids": final_match_ids,
        }
    )
    try:
        ControlJob.model_validate(payload)
    except ValidationError:
        return None
    return final_kind, final_id, final_match_ids


def _formal_archive_status(job: ControlJob, archive_database: Path) -> _ArchiveCheck:
    """Classify a finalizing candidate through a read-only SQLite snapshot.

    ``unavailable`` deliberately remains distinct from a confirmed absence. A
    short-lived SQLite writer, an I/O error, or a schema that cannot currently
    be verified must not turn an already committed formal archive into a
    terminal control-plane failure.
    """

    if job.final_kind is None or job.final_id is None or not job.final_match_ids:
        return "absent"
    connection: sqlite3.Connection | None = None
    try:
        resolved_database = archive_database.resolve(strict=False)
        try:
            database_info = resolved_database.stat()
        except FileNotFoundError:
            return "absent"
        except OSError:
            return "unavailable"
        if not stat.S_ISREG(database_info.st_mode):
            return "unavailable"
        uri = f"{resolved_database.as_uri()}?mode=ro"
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=_ARCHIVE_SQLITE_TIMEOUT_SECONDS,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        connection.execute(
            f"PRAGMA busy_timeout={int(_ARCHIVE_SQLITE_TIMEOUT_SECONDS * 1000)}"
        )
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            return "unavailable"
        if job.final_kind == "match":
            exists = connection.execute(
                "SELECT 1 FROM matches WHERE match_id = ?",
                (job.final_id,),
            ).fetchone()
            return (
                "found"
                if exists is not None and job.final_match_ids == (job.final_id,)
                else "absent"
            )
        if job.final_kind == "series":
            exists = connection.execute(
                "SELECT 1 FROM series_archives WHERE series_id = ?",
                (job.final_id,),
            ).fetchone()
            rows = connection.execute(
                "SELECT sm.match_id FROM series_matches AS sm "
                "JOIN matches AS m ON m.match_id = sm.match_id "
                "WHERE sm.series_id = ? ORDER BY sm.leg_number",
                (job.final_id,),
            ).fetchall()
        elif job.final_kind == "tournament":
            exists = connection.execute(
                "SELECT 1 FROM tournament_archives WHERE tournament_id = ?",
                (job.final_id,),
            ).fetchone()
            rows = connection.execute(
                "SELECT sm.match_id FROM tournament_pairings AS tp "
                "JOIN series_matches AS sm ON sm.series_id = tp.series_id "
                "JOIN matches AS m ON m.match_id = sm.match_id "
                "WHERE tp.tournament_id = ? "
                "ORDER BY tp.pairing_number, sm.leg_number",
                (job.final_id,),
            ).fetchall()
        else:
            exists = connection.execute(
                "SELECT 1 FROM championship_archives WHERE championship_id = ?",
                (job.final_id,),
            ).fetchone()
            rows = connection.execute(
                "SELECT sm.match_id FROM championship_pairings AS cp "
                "JOIN series_matches AS sm ON sm.series_id = cp.series_id "
                "JOIN matches AS m ON m.match_id = sm.match_id "
                "WHERE cp.championship_id = ? "
                "ORDER BY cp.pairing_number, sm.leg_number",
                (job.final_id,),
            ).fetchall()
        return (
            "found"
            if exists is not None
            and tuple(row["match_id"] for row in rows) == job.final_match_ids
            else "absent"
        )
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return "unavailable"
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass


def _reconcile_archive_status(job: ControlJob, archive_database: Path) -> _ArchiveCheck:
    """Retry only transiently unavailable read-only reconciliation."""

    status: _ArchiveCheck = "unavailable"
    for attempt in range(_ARCHIVE_RECONCILE_ATTEMPTS):
        status = _formal_archive_status(job, archive_database)
        if status != "unavailable" or attempt + 1 == _ARCHIVE_RECONCILE_ATTEMPTS:
            return status
        time.sleep(_ARCHIVE_RECONCILE_RETRY_SECONDS)
    return status  # pragma: no cover - the bounded loop always returns


async def _reconcile_archive_status_async(
    job: ControlJob,
    archive_database: Path,
) -> _ArchiveCheck:
    """Async counterpart that keeps SQLite waits off the event loop."""

    status: _ArchiveCheck = "unavailable"
    for attempt in range(_ARCHIVE_RECONCILE_ATTEMPTS):
        status = await asyncio.to_thread(_formal_archive_status, job, archive_database)
        if status != "unavailable" or attempt + 1 == _ARCHIVE_RECONCILE_ATTEMPTS:
            return status
        await asyncio.sleep(_ARCHIVE_RECONCILE_RETRY_SECONDS)
    return status  # pragma: no cover - the bounded loop always returns


def _windows_pid_is_alive(pid: int) -> bool:
    """Query a Windows process without using ``os.kill``.

    Python implements non-console ``os.kill`` signals on Windows with
    ``TerminateProcess``.  A read-only process handle therefore matters here:
    a PID loaded from an earlier controller is never an authority to signal.
    """

    process_query_limited_information = 0x1000
    still_active = 259
    error_invalid_parameter = 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    get_exit_code.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = open_process(process_query_limited_information, False, pid)
    if not handle:
        # Access denied is conservatively treated as alive.  Only Windows'
        # documented invalid-PID result proves that this PID no longer exists.
        return ctypes.get_last_error() != error_invalid_parameter
    try:
        exit_code = wintypes.DWORD()
        if not get_exit_code(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        close_handle(handle)


def _pid_is_alive(pid: int) -> bool:
    """Return a conservative, non-terminating liveness result for ``pid``."""

    if os.name == "nt":  # pragma: no cover - exercised by Windows CI.
        try:
            return _windows_pid_is_alive(pid)
        except (OSError, OverflowError, TypeError, ValueError):
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, OverflowError, ValueError):
        return True
    return True


def _prior_child_has_exited(store: JobStore, job_id: str) -> bool:
    """Bound the startup wait for the prior controller's pipe watchdog.

    A live or unreadable PID remains capacity-blocking.  PID reuse is therefore
    safe: this code never signals a stored PID and never assumes an observed
    live process is still the original child.
    """

    try:
        child_pid = store.child_pid(job_id)
    except (ControlError, OSError, TypeError, ValueError):
        return False
    if child_pid is None:
        return True
    for attempt in range(_PRIOR_CHILD_EXIT_ATTEMPTS):
        if not _pid_is_alive(child_pid):
            return True
        if attempt + 1 < _PRIOR_CHILD_EXIT_ATTEMPTS:
            time.sleep(_PRIOR_CHILD_EXIT_RETRY_SECONDS)
    return False


def _decode_protocol_frame(line: bytes, token: str) -> dict[str, object] | None:
    if len(line) > _MAX_PROTOCOL_FRAME_BYTES or not line.startswith(_PROTOCOL_PREFIX):
        return None
    suffix = line.removeprefix(_PROTOCOL_PREFIX)
    supplied_token, separator, encoded = suffix.partition(b":")
    if not separator or not secrets.compare_digest(supplied_token, token.encode("ascii")):
        return None
    if not encoded or _BASE64URL_RE.fullmatch(encoded) is None:
        return None
    padding = b"=" * (-len(encoded) % 4)
    try:
        raw = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(raw) > _MAX_PROTOCOL_JSON_BYTES:
        return None
    try:
        payload = json.loads(raw, object_pairs_hook=_json_without_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _event_name(payload: Mapping[str, object]) -> str | None:
    event = payload.get("type")
    alias = payload.get("event")
    if event is None:
        event = alias
    elif alias is not None and alias != event:
        return None
    return (
        event
        if event
        in {"running", "live_started", "participation", "finalizing", "completed"}
        else None
    )


@dataclass(slots=True)
class _RunningProcess:
    process: asyncio.subprocess.Process
    token: str
    ready_event: asyncio.Event
    expected_human_links: int
    web_base_url: str
    stdout_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    monitor_task: asyncio.Task[None] | None = None
    termination_task: asyncio.Task[None] | None = None
    cancel_requested: bool = False
    running_received: bool = False
    startup_waiting: bool = True


@dataclass(slots=True, repr=False)
class _RuntimeProfileCredential:
    configuration_digest: str
    environment_name: str
    api_key: str


class ControlJobManager:
    """Run at most one prepared job in a fixed-argument child process."""

    def __init__(
        self,
        store: JobStore,
        *,
        python_executable: str | Path = sys.executable,
        working_directory: str | Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.store = store
        # Preserve a virtualenv launcher symlink: resolving it would bypass the
        # environment's site-packages when the interpreter is exec'd directly.
        self._python_executable = str(Path(python_executable).expanduser().absolute())
        self._working_directory = (
            Path(working_directory).expanduser().resolve()
            if working_directory is not None
            else Path(__file__).resolve().parent.parent
        )
        self._source_environment = dict(os.environ if environment is None else environment)
        self._runtime_profile_credentials: dict[str, _RuntimeProfileCredential] = {}
        self._credential_lock = threading.RLock()
        self._running: dict[str, _RunningProcess] = {}
        self._participation_links: dict[str, dict[str, ControlParticipationLink]] = {}
        self._lock = asyncio.Lock()
        self._closing = False
        self._lease_descriptor: int | None = self._acquire_manager_lease()
        try:
            self._reconcile_interrupted_jobs()
        except Exception:
            self._release_manager_lease()
            raise

    def _acquire_manager_lease(self) -> int:
        if fcntl is None and msvcrt is None:
            raise ControlError("control_unavailable")
        lock_path = self.store.path.with_name(f"{self.store.path.name}.lock")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(lock_path, flags, 0o600)
            os.set_inheritable(descriptor, False)
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or (hasattr(os, "getuid") and info.st_uid != os.getuid())
            ):
                raise ControlError("control_unavailable")
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                if info.st_size == 0:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except (ControlError, OSError) as exc:
            if "descriptor" in locals():
                os.close(descriptor)
            raise ControlError("control_unavailable") from exc
        return descriptor

    def _release_manager_lease(self) -> None:
        descriptor = getattr(self, "_lease_descriptor", None)
        if descriptor is None:
            return
        self._lease_descriptor = None
        try:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            elif msvcrt is not None:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(descriptor)

    def __del__(self) -> None:
        self._release_manager_lease()

    def _reconcile_interrupted_jobs(self) -> None:
        """Fail closed after a controller restart without signaling a stale PID."""

        for job in self.store.list(limit=100):
            recoverable_terminal = (
                job.status == "interrupted"
                and job.final_kind is not None
                and job.failure_code is None
            )
            if job.status not in _ACTIVE_STATUSES and not recoverable_terminal:
                continue
            if not recoverable_terminal and not _prior_child_has_exited(
                self.store,
                job.job_id,
            ):
                # The inherited stdin watchdog should normally make this wait
                # very short.  If the PID is unreadable, remains alive, or was
                # reused, preserve the active row and its capacity reservation.
                continue
            archive_status = (
                _reconcile_archive_status(job, self.store.archive_database)
                if job.final_kind is not None
                else "absent"
            )
            if archive_status == "found":
                self.store.transition(
                    job.job_id,
                    expected=(job.status,),
                    status="completed",
                    finished_at=_utc_now(),
                )
                continue
            if recoverable_terminal:
                continue
            self.store.transition(
                job.job_id,
                expected=(job.status,),
                status="interrupted",
                finished_at=_utc_now(),
                failure_code=(
                    None if archive_status == "unavailable" else "controller_restarted"
                ),
            )

    def public_job(self, job: ControlJob | str) -> ControlJob:
        """Attach ephemeral human-seat links without ever writing them to SQLite."""

        if isinstance(job, str):
            job = self.store.get(job)
        links = tuple(self._participation_links.get(job.job_id, {}).values())
        return job.model_copy(update={"participation_links": links})

    @staticmethod
    def _credential_profile(profile_id: str) -> ProviderProfile:
        try:
            profile = load_profiles().get(profile_id)
        except (OSError, TypeError, ValueError) as exc:
            raise ControlError("profile_unavailable") from exc
        if (
            profile is None
            or profile.provider != "openai"
            or profile.api_key_env is None
            or not is_profile_credential_environment_name(profile.api_key_env)
            or not profile.default_model
        ):
            raise ControlError("profile_unavailable")
        return profile

    def _credential_for_profile(self, profile: ProviderProfile) -> str | None:
        environment_name = profile.api_key_env
        if environment_name is None or not is_profile_credential_environment_name(
            environment_name
        ):
            return None
        with self._credential_lock:
            runtime_credential = self._runtime_profile_credentials.get(profile.profile_id)
            if runtime_credential is not None:
                try:
                    # Normal config loading is process-cached, but keep this guard for
                    # injected/reloadable loaders.  A managed child independently
                    # validates the same digest from its fresh process configuration.
                    configuration_matches = (
                        secrets.compare_digest(
                            runtime_credential.configuration_digest,
                            profile_configuration_digest(profile),
                        )
                    )
                except (TypeError, ValueError):
                    configuration_matches = False
                if (
                    configuration_matches
                    and runtime_credential.environment_name == environment_name
                ):
                    return runtime_credential.api_key
                # Never reuse a browser-supplied Key after any trusted Profile
                # property changes, even when the environment name is unchanged.
                self._runtime_profile_credentials.pop(profile.profile_id, None)
        return self._source_environment.get(environment_name)

    def profile_credential_ready(self, profile: ProviderProfile) -> bool:
        """Return readiness without disclosing the credential or its source."""

        if profile.provider == "ollama":
            return True
        return bool(self._credential_for_profile(profile))

    async def set_profile_credential(self, profile_id: str, api_key: str) -> None:
        """Install one volatile browser-supplied credential for future workers."""

        if (
            not api_key
            or len(api_key.encode("utf-8")) > MAX_PROFILE_API_KEY_BYTES
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in api_key)
        ):
            raise ControlError("invalid_request")
        async with self._lock:
            if self._closing:
                raise ControlError("control_unavailable")
            profile = self._credential_profile(profile_id)
            environment_name = profile.api_key_env
            if environment_name is None:  # Defensive: _credential_profile already rejects this.
                raise ControlError("profile_unavailable")
            try:
                configuration_digest = profile_configuration_digest(profile)
            except (TypeError, ValueError) as exc:
                raise ControlError("profile_unavailable") from exc
            with self._credential_lock:
                self._runtime_profile_credentials[profile.profile_id] = (
                    _RuntimeProfileCredential(
                        configuration_digest=configuration_digest,
                        environment_name=environment_name,
                        api_key=api_key,
                    )
                )

    async def clear_profile_credential(self, profile_id: str) -> None:
        """Forget one volatile credential; an inherited environment value may remain."""

        async with self._lock:
            if self._closing:
                raise ControlError("control_unavailable")
            with self._credential_lock:
                removed = self._runtime_profile_credentials.pop(profile_id, None)
            if removed is not None:
                return
            self._credential_profile(profile_id)

    def _child_environment(self, job: ControlJob, token: str) -> dict[str, str]:
        try:
            current_preview = validate_job_spec(
                job.spec,
                archive_database=self.store.archive_database,
                credential_ready=self.profile_credential_ready,
                require_current_pricing=True,
            )
        except ControlError as exc:
            raise ControlError("worker_start_failed") from exc
        if current_preview != job.preview:
            # Checkpoint/config/catalog state must still match the exact
            # disclosure-safe preview the user confirmed.
            raise ControlError("worker_start_failed")
        environment = {
            key: value
            for key, value in self._source_environment.items()
            if key in _SAFE_ENVIRONMENT_NAMES
        }
        profile_ids = {
            item.profile_id
            for item in (*job.spec.players, *job.spec.judges)
            if item.kind == "profile" and item.profile_id is not None
        }
        if (
            job.spec.resume_tournament_id is not None
            or job.spec.resume_championship_id is not None
        ):
            profile_ids = {
                item.profile_id for item in job.preview.prepared_profiles
            }
        try:
            profiles = load_profiles() if profile_ids else {}
        except (OSError, TypeError, ValueError) as exc:
            raise ControlError("worker_start_failed") from exc
        prepared_profiles = {
            item.profile_id: item.configuration_digest
            for item in job.preview.prepared_profiles
        }
        if set(prepared_profiles) != profile_ids:
            raise ControlError("worker_start_failed")
        routed_credentials: dict[str, str] = {}
        for profile_id in profile_ids:
            profile = profiles.get(profile_id)
            if (
                profile is None
                or profile_configuration_digest(profile) != prepared_profiles[profile_id]
            ):
                raise ControlError("worker_start_failed")
            if profile.api_key_env is not None:
                credential = self._credential_for_profile(profile)
                if not credential:
                    raise ControlError("worker_start_failed")
                existing = routed_credentials.get(profile.api_key_env)
                if existing is not None and not secrets.compare_digest(existing, credential):
                    raise ControlError("worker_start_failed")
                routed_credentials[profile.api_key_env] = credential
        environment.update(routed_credentials)
        environment.update(
            {
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1",
                _PROTOCOL_JOB_ENV: job.job_id,
                _PROTOCOL_PARENT_WATCHDOG_ENV: "1",
                _PROTOCOL_PROFILE_SNAPSHOT_ENV: json.dumps(
                    prepared_profiles,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                _PROTOCOL_TOKEN_ENV: token,
            }
        )
        return environment

    def _has_active_job(self) -> bool:
        if any(item.process.returncode is None for item in self._running.values()):
            return True
        return any(job.status in _ACTIVE_STATUSES for job in self.store.list(limit=100))

    async def start(
        self,
        job_id: str,
        *,
        idempotency_key: str,
        web_base_url: str,
    ) -> ControlJob:
        """Start a prepared job after its separate confirmation request."""

        web_base_url = _validate_web_base_url(web_base_url)
        if _IDEMPOTENCY_RE.fullmatch(idempotency_key) is None:
            raise ControlError("invalid_request")
        self.store.expire_stale_jobs()
        self.store.get(job_id)
        async with self._lock:
            if self._closing:
                raise ControlError("control_unavailable")
            job = self.store.get(job_id)
            if job.status != "prepared":
                self.store.claim_operation(job_id, "start", idempotency_key)
                return self.public_job(job)
            if self._has_active_job():
                raise ControlError("job_capacity")
            token = secrets.token_urlsafe(32)
            argv = build_job_argv(
                job.spec,
                archive_database=self.store.archive_database,
                web_base_url=web_base_url,
                python_executable=self._python_executable,
            )
            try:
                environment = self._child_environment(job, token)
            except ControlError:
                raise
            except (OSError, TypeError, ValueError) as exc:
                raise ControlError("worker_start_failed") from exc
            self.store.claim_operation(job_id, "start", idempotency_key)
            started_at = _utc_now()
            starting_job = self.store.transition(
                job_id,
                expected=("prepared",),
                status="starting",
                started_at=started_at,
                tournament_id=job.spec.resume_tournament_id,
                championship_id=job.spec.resume_championship_id,
            )
            if starting_job.started_at != started_at:
                raise ControlError("job_conflict")
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=str(self._working_directory),
                    env=environment,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=_MAX_STDOUT_LINE_BYTES,
                    start_new_session=os.name == "posix",
                )
            except asyncio.CancelledError:
                self.store.transition(
                    job_id,
                    expected=("starting",),
                    status="interrupted",
                    finished_at=_utc_now(),
                    failure_code="worker_start_interrupted",
                )
                raise
            except (ControlError, OSError, TypeError, ValueError) as exc:
                self.store.transition(
                    job_id,
                    expected=("starting",),
                    status="failed",
                    finished_at=_utc_now(),
                    failure_code="worker_start_failed",
                )
                raise ControlError("worker_start_failed") from exc

            running = _RunningProcess(
                process=process,
                token=token,
                ready_event=asyncio.Event(),
                expected_human_links=job.preview.human_count,
                web_base_url=web_base_url,
            )
            self._running[job_id] = running
            stdout_task = asyncio.create_task(
                self._read_stdout(job_id, process.stdout, token),
                name=f"control-stdout-{job_id}",
            )
            stderr_task = asyncio.create_task(
                self._drain_stream(process.stderr),
                name=f"control-stderr-{job_id}",
            )
            running.stdout_task = stdout_task
            running.stderr_task = stderr_task
            try:
                running_job = self.store.transition(
                    job_id,
                    expected=("starting",),
                    status="running",
                    child_pid=process.pid,
                )
            except Exception:
                stdout_task.cancel()
                stderr_task.cancel()
                self._terminate_process_group(process)
                try:
                    await asyncio.wait_for(
                        asyncio.shield(process.wait()),
                        timeout=_TERMINATE_GRACE_SECONDS,
                    )
                except TimeoutError:
                    self._kill_process_group(process)
                    await process.wait()
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                self._running.pop(job_id, None)
                raise

            running.monitor_task = asyncio.create_task(
                self._watch_process(job_id, running),
                name=f"control-monitor-{job_id}",
            )
            del running_job

        try:
            await asyncio.wait_for(
                running.ready_event.wait(),
                timeout=_STARTUP_READY_SECONDS,
            )
        except asyncio.CancelledError:
            await self._cancel_startup_timeout(job_id, running)
            async with self._lock:
                running.startup_waiting = False
                if self.store.get(job_id).status in _TERMINAL_STATUSES:
                    self._participation_links.pop(job_id, None)
            raise
        except TimeoutError as exc:
            if await self._cancel_startup_timeout(job_id, running):
                raise ControlError("worker_start_timeout") from exc
        async with self._lock:
            result = self.public_job(self.store.get(job_id))
            running.startup_waiting = False
            if result.status in _TERMINAL_STATUSES:
                self._participation_links.pop(job_id, None)
            return result

    async def _cancel_startup_timeout(
        self,
        job_id: str,
        running: _RunningProcess,
    ) -> bool:
        async with self._lock:
            current = self.store.get(job_id)
            if running.ready_event.is_set() or current.status in _TERMINAL_STATUSES:
                return False
            if self._running.get(job_id) is not running:
                return False
            running.startup_waiting = False
            if current.status in {"starting", "running", "finalizing"}:
                self.store.transition(
                    job_id,
                    expected=(current.status,),
                    status="cancel_requested",
                )
            running.cancel_requested = True
            self._interrupt_process(running.process)
            if running.termination_task is None:
                running.termination_task = asyncio.create_task(
                    self._terminate_after_grace(running.process),
                    name=f"control-terminate-{job_id}",
                )
            return True

    async def cancel(
        self,
        job_id: str,
        *,
        idempotency_key: str,
    ) -> ControlJob:
        """Request cancellation once; repeated requests are harmless."""

        self.store.get(job_id)
        async with self._lock:
            claimed = self.store.claim_operation(job_id, "cancel", idempotency_key)
            if not claimed:
                return self.public_job(self.store.get(job_id))
            job = self.store.get(job_id)
            if job.status in _TERMINAL_STATUSES:
                return self.public_job(job)
            if job.status == "prepared":
                job = self.store.transition(
                    job_id,
                    expected=("prepared",),
                    status="cancelled",
                    finished_at=_utc_now(),
                )
                return self.public_job(job)
            if job.status not in _ACTIVE_STATUSES:
                raise ControlError("job_not_stoppable")
            if job.status != "cancel_requested":
                job = self.store.transition(
                    job_id,
                    expected=("starting", "running", "finalizing"),
                    status="cancel_requested",
                )
            running = self._running.get(job_id)
            if running is None or running.process.returncode is not None:
                job = self.store.transition(
                    job_id,
                    expected=("cancel_requested",),
                    status="interrupted",
                    finished_at=_utc_now(),
                    failure_code="worker_missing",
                )
                return self.public_job(job)
            running.cancel_requested = True
            self._interrupt_process(running.process)
            if running.termination_task is None:
                running.termination_task = asyncio.create_task(
                    self._terminate_after_grace(running.process),
                    name=f"control-terminate-{job_id}",
                )
            return self.public_job(job)

    async def shutdown(self) -> None:
        """Stop owned children and prevent new work during application shutdown."""

        try:
            await self._shutdown_owned_processes()
        finally:
            self._participation_links.clear()
            with self._credential_lock:
                self._runtime_profile_credentials.clear()
            self._release_manager_lease()

    async def _shutdown_owned_processes(self) -> None:

        async with self._lock:
            self._closing = True
            running_items = tuple(self._running.items())
            for job_id, running in running_items:
                running.cancel_requested = True
                job = self.store.get(job_id)
                if job.status in {"starting", "running", "finalizing"}:
                    self.store.transition(
                        job_id,
                        expected=(job.status,),
                        status="cancel_requested",
                    )
                self._interrupt_process(running.process)

        monitor_tasks = tuple(
            running.monitor_task
            for _, running in running_items
            if running.monitor_task is not None
        )
        if monitor_tasks:
            _done, pending = await asyncio.wait(
                monitor_tasks,
                timeout=_CANCEL_GRACE_SECONDS,
            )
            if pending:
                for _, running in running_items:
                    if running.process.returncode is None:
                        self._terminate_process_group(running.process)
                _done, pending = await asyncio.wait(
                    pending,
                    timeout=_TERMINATE_GRACE_SECONDS,
                )
                if pending:
                    for _, running in running_items:
                        self._kill_process_group(running.process)
                    _done, pending = await asyncio.wait(
                        pending,
                        timeout=_KILL_GRACE_SECONDS,
                    )
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

    @staticmethod
    def _interrupt_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGINT)
            else:
                process.terminate()
        except (ProcessLookupError, PermissionError):
            return

    @staticmethod
    def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            elif process.returncode is None:
                process.terminate()
        except (ProcessLookupError, PermissionError):
            return

    @staticmethod
    def _kill_process_group(process: asyncio.subprocess.Process) -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            elif process.returncode is None:
                process.kill()
        except (ProcessLookupError, PermissionError):
            return

    @staticmethod
    async def _terminate_after_grace(process: asyncio.subprocess.Process) -> None:
        try:
            await asyncio.wait_for(
                asyncio.shield(process.wait()),
                timeout=_CANCEL_GRACE_SECONDS,
            )
        except TimeoutError:
            ControlJobManager._terminate_process_group(process)
            try:
                await asyncio.wait_for(
                    asyncio.shield(process.wait()),
                    timeout=_TERMINATE_GRACE_SECONDS,
                )
            except TimeoutError:
                ControlJobManager._kill_process_group(process)
        finally:
            ControlJobManager._kill_process_group(process)

    @staticmethod
    async def _drain_stream(stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        while await stream.read(_READ_CHUNK_BYTES):
            pass

    async def _read_stdout(
        self,
        job_id: str,
        stream: asyncio.StreamReader | None,
        token: str,
    ) -> None:
        if stream is None:
            return
        buffer = bytearray()
        discarding = False
        while chunk := await stream.read(_READ_CHUNK_BYTES):
            position = 0
            while position < len(chunk):
                newline = chunk.find(b"\n", position)
                if newline < 0:
                    if not discarding:
                        buffer.extend(chunk[position:])
                        if len(buffer) > _MAX_STDOUT_LINE_BYTES:
                            buffer.clear()
                            discarding = True
                    break
                if not discarding:
                    buffer.extend(chunk[position:newline])
                    await self._handle_line(job_id, bytes(buffer).rstrip(b"\r"), token)
                    buffer.clear()
                else:
                    discarding = False
                position = newline + 1
        if buffer and not discarding:
            await self._handle_line(job_id, bytes(buffer).rstrip(b"\r"), token)

    async def _handle_line(self, job_id: str, line: bytes, token: str) -> None:
        payload = _decode_protocol_frame(line, token)
        if payload is None:
            return
        event = _event_name(payload)
        if event is None:
            return
        payload_job_id = payload.get("job_id")
        if payload_job_id != job_id:
            return
        allowed = {
            "running": {
                "type",
                "event",
                "job_id",
                "tournament_id",
                "championship_id",
            },
            "live_started": {"type", "event", "job_id", "live_id"},
            "participation": {"type", "event", "job_id", "player_name", "url"},
            "finalizing": {
                "type",
                "event",
                "job_id",
                "final_kind",
                "final_id",
                "final_match_ids",
            },
            "completed": {
                "type",
                "event",
                "job_id",
                "final_kind",
                "final_id",
                "final_match_ids",
            },
        }[event]
        if set(payload) - allowed:
            return

        async with self._lock:
            try:
                current = self.store.get(job_id)
                if current.status in _TERMINAL_STATUSES:
                    return
                running = self._running.get(job_id)
                if event == "running":
                    tournament_id = payload.get("tournament_id")
                    championship_id = payload.get("championship_id")
                    if tournament_id is not None and _safe_identifier(tournament_id) is None:
                        return
                    if (
                        championship_id is not None
                        and _safe_identifier(championship_id) is None
                    ):
                        return
                    if current.status not in {"starting", "running"}:
                        return
                    if current.spec.mode == "round_robin":
                        valid_scope = tournament_id is not None and championship_id is None
                    elif current.spec.mode == "championship":
                        valid_scope = championship_id is not None and tournament_id is None
                    else:
                        valid_scope = tournament_id is None and championship_id is None
                    if not valid_scope:
                        return
                    if (
                        current.tournament_id is not None
                        and current.tournament_id != tournament_id
                    ):
                        return
                    if (
                        current.championship_id is not None
                        and current.championship_id != championship_id
                    ):
                        return
                    self.store.transition(
                        job_id,
                        expected=(current.status,),
                        status="running",
                        tournament_id=tournament_id,
                        championship_id=championship_id,
                    )
                    if running is not None:
                        running.running_received = True
                        self._set_ready_if_complete(job_id, running)
                    return
                if event == "live_started":
                    live_id = _safe_identifier(payload.get("live_id"))
                    if live_id is None or current.status not in {"starting", "running"}:
                        return
                    if current.live_id is not None and current.live_id != live_id:
                        return
                    self.store.transition(
                        job_id,
                        expected=(current.status,),
                        status="running",
                        live_id=live_id,
                    )
                    return
                if event == "participation":
                    if current.status not in {"starting", "running"} or running is None:
                        return
                    try:
                        link = ControlParticipationLink(
                            player_name=payload.get("player_name"),
                            url=payload.get("url"),
                        )
                    except ValidationError:
                        return
                    try:
                        parsed_link = urlsplit(link.url)
                        link_port = parsed_link.port or 80
                    except ValueError:
                        return
                    link_host = (
                        f"[{parsed_link.hostname}]"
                        if parsed_link.hostname is not None and ":" in parsed_link.hostname
                        else parsed_link.hostname
                    )
                    if f"http://{link_host}:{link_port}" != running.web_base_url:
                        return
                    human_names = {
                        player.name
                        for player in current.spec.players
                        if player.kind == "human"
                    }
                    if link.player_name not in human_names:
                        return
                    self._participation_links.setdefault(job_id, {})[
                        link.player_name
                    ] = link
                    self._set_ready_if_complete(job_id, running)
                    return
                if event == "finalizing":
                    if current.status not in {"starting", "running", "finalizing"}:
                        return
                    final_fields = _safe_final_fields(payload, required=True)
                    if final_fields is None:
                        return
                    validated = _validated_final_update(
                        current,
                        final_fields,
                        status="finalizing",
                    )
                    if validated is None:
                        return
                    final_kind, final_id, final_match_ids = validated
                    self.store.transition(
                        job_id,
                        expected=(current.status,),
                        status="finalizing",
                        final_kind=final_kind,
                        final_id=final_id,
                        final_match_ids=final_match_ids,
                    )
                    if running is not None:
                        self._set_ready_if_complete(job_id, running)
                    return
                final_fields = _safe_final_fields(payload, required=True)
                if final_fields is None:
                    return
                validated = _validated_final_update(
                    current,
                    final_fields,
                    status="completed",
                )
                if validated is None:
                    return
                final_kind, final_id, final_match_ids = validated
                self.store.transition(
                    job_id,
                    expected=("starting", "running", "finalizing"),
                    status="completed",
                    finished_at=_utc_now(),
                    final_kind=final_kind,
                    final_id=final_id,
                    final_match_ids=final_match_ids,
                )
                if running is not None:
                    running.ready_event.set()
            except (ControlError, OSError, TypeError, ValueError):
                return

    def _set_ready_if_complete(self, job_id: str, running: _RunningProcess) -> None:
        if not running.running_received:
            return
        link_count = len(self._participation_links.get(job_id, {}))
        if link_count >= running.expected_human_links:
            running.ready_event.set()

    async def _watch_process(self, job_id: str, running: _RunningProcess) -> None:
        return_code: int | None = None
        try:
            return_code = await running.process.wait()
            stream_tasks = tuple(
                task
                for task in (running.stdout_task, running.stderr_task)
                if task is not None
            )
            await asyncio.gather(
                *stream_tasks,
                return_exceptions=True,
            )
            async with self._lock:
                current = self.store.get(job_id)
                if current.status in _TERMINAL_STATUSES:
                    return
                archive_status = (
                    await _reconcile_archive_status_async(
                        current,
                        self.store.archive_database,
                    )
                    if current.final_kind is not None
                    else "absent"
                )
                if archive_status == "found":
                    status = "completed"
                    failure_code = None
                elif archive_status == "unavailable":
                    status = "interrupted"
                    failure_code = None
                elif running.cancel_requested or current.status == "cancel_requested":
                    status = "cancelled"
                    failure_code = None
                elif return_code == 0:
                    status = "failed"
                    failure_code = "worker_protocol_incomplete"
                elif return_code is not None and return_code < 0:
                    status = "interrupted"
                    failure_code = "worker_interrupted"
                else:
                    status = "failed"
                    failure_code = "worker_failed"
                self.store.transition(
                    job_id,
                    expected=("starting", "running", "finalizing", "cancel_requested"),
                    status=status,
                    finished_at=_utc_now(),
                    failure_code=failure_code,
                )
        except asyncio.CancelledError:
            stream_tasks = tuple(
                task
                for task in (running.stdout_task, running.stderr_task)
                if task is not None
            )
            for task in stream_tasks:
                task.cancel()
            await asyncio.gather(
                *stream_tasks,
                return_exceptions=True,
            )
            try:
                current = self.store.get(job_id)
                if current.status not in _TERMINAL_STATUSES:
                    self.store.transition(
                        job_id,
                        expected=("starting", "running", "finalizing", "cancel_requested"),
                        status="interrupted",
                        finished_at=_utc_now(),
                        failure_code="worker_shutdown_timeout",
                    )
            except (ControlError, OSError, TypeError, ValueError):
                pass
            raise
        except (ControlError, OSError, TypeError, ValueError):
            return
        finally:
            running.ready_event.set()
            self._kill_process_group(running.process)
            if running.termination_task is not None:
                running.termination_task.cancel()
                await asyncio.gather(running.termination_task, return_exceptions=True)
            async with self._lock:
                if self._running.get(job_id) is running:
                    self._running.pop(job_id, None)
                if not running.startup_waiting:
                    self._participation_links.pop(job_id, None)


__all__ = ["ControlJobManager"]
