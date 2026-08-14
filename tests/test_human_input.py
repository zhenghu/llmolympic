"""Security and race regressions for the Stage 4.4 browser-human inbox."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import sqlite3
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from llmolympic.core.player import HumanPlayer, PlayerTimeoutError
from llmolympic.core.storage import SQLiteStore
from llmolympic.human_input import (
    INPUT_MAX_MOVE_CHARS,
    INPUT_MAX_PROMPT_CHARS,
    INPUT_MAX_SESSIONS,
    BrowserHumanPlayer,
    HumanInputError,
    InputSessionStore,
    WebSubmissionStore,
    derive_human_input_database_path,
)


class _Clock:
    def __init__(self, value: float = 1_800_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _error_code(callable_) -> str:
    with pytest.raises(HumanInputError) as caught:
        callable_()
    return caught.value.code


def _session(
    archive: Path,
    *,
    clock: _Clock | None = None,
    heartbeat_seconds: float = 0.05,
    poll_seconds: float = 0.01,
    player_name: str = "浏览器人类",
) -> InputSessionStore:
    options = {
        "player_name": player_name,
        "heartbeat_seconds": heartbeat_seconds,
        "poll_seconds": poll_seconds,
    }
    if clock is not None:
        options["clock"] = clock
    return InputSessionStore(archive, **options)


def _web(archive: Path, *, clock: _Clock | None = None) -> WebSubmissionStore:
    options = {} if clock is None else {"clock": clock}
    return WebSubmissionStore(archive, **options)


def _load(web: WebSubmissionStore, session: InputSessionStore) -> object:
    return web.load(
        session.session_id,
        session.seat_id,
        capability=session.capability,
    )


def _submit(
    web: WebSubmissionStore,
    session: InputSessionStore,
    request_id: str,
    move: str,
    *,
    submission_id: str,
    capability: str | None = None,
) -> object:
    return web.submit(
        session.session_id,
        session.seat_id,
        request_id,
        capability=session.capability if capability is None else capability,
        move=move,
        submission_id=submission_id,
    )


async def _wait_for_request(
    web: WebSubmissionStore,
    session: InputSessionStore,
    *,
    previous: str | None = None,
):
    for _ in range(100):
        try:
            snapshot = _load(web, session)
        except HumanInputError as exc:
            if exc.code not in {"input_not_ready", "request_not_found"}:
                raise
        else:
            request = snapshot.request
            if request is not None and request.request_id != previous:
                return request
        await asyncio.sleep(0.005)
    raise AssertionError("browser-human request did not become visible")


def test_input_sidecar_is_private_digest_only_and_leaves_archive_unchanged(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.db"
    SQLiteStore(archive)
    archive_before = (_digest(archive), archive.stat().st_mode, archive.stat().st_mtime_ns)

    session = _session(archive)
    try:
        input_path = derive_human_input_database_path(archive)
        assert input_path.is_file()
        assert session.capability.encode("ascii") not in input_path.read_bytes()
        expected_digest = hashlib.sha256(session.capability.encode("ascii")).digest()
        assert expected_digest in input_path.read_bytes()
        with sqlite3.connect(input_path) as connection:
            capability_digest, owner_digest = connection.execute(
                "SELECT capability_digest, owner_digest FROM input_sessions "
                "WHERE session_id=?",
                (session.session_id,),
            ).fetchone()
        assert capability_digest == expected_digest
        assert isinstance(owner_digest, bytes) and len(owner_digest) == 32
        assert "?" not in session.control_fragment
        assert session.control_fragment.startswith("#")
        if os.name == "posix":
            assert stat.S_IMODE(input_path.stat().st_mode) == 0o600
            for suffix in ("-journal", "-wal", "-shm"):
                sidecar = Path(f"{input_path}{suffix}")
                if sidecar.exists():
                    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
    finally:
        session.close()

    assert (_digest(archive), archive.stat().st_mode, archive.stat().st_mtime_ns) == archive_before


def test_pending_submission_resolves_once_and_load_never_discloses_move(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.db"
    session = _session(archive)
    web = _web(archive)

    async def scenario() -> None:
        pending = asyncio.create_task(session.resolve("请选择落子", timeout_seconds=1.0))
        request = await _wait_for_request(web, session)
        result = _submit(
            web,
            session,
            request.request_id,
            "H8",
            submission_id="1" * 32,
        )
        assert result.status in {"accepted", "idempotent"}
        snapshot = _load(web, session)
        assert "H8" not in repr(snapshot)
        assert not hasattr(snapshot.request, "move")
        assert await pending == "H8"
        with sqlite3.connect(derive_human_input_database_path(archive)) as connection:
            stored_digest = connection.execute(
                "SELECT move_digest FROM input_requests WHERE request_id=?",
                (request.request_id,),
            ).fetchone()[0]
        assert stored_digest == hmac.digest(
            session.capability.encode("ascii"),
            b"H8",
            "sha256",
        )
        assert stored_digest != hashlib.sha256(b"H8").digest()

    try:
        asyncio.run(scenario())
    finally:
        session.close()


def test_atomic_double_submit_and_idempotent_retry_keep_the_first_move(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.db"
    session = _session(archive)
    web = _web(archive)

    async def scenario() -> None:
        pending = asyncio.create_task(session.resolve("move", timeout_seconds=2.0))
        request = await _wait_for_request(web, session)
        barrier = threading.Barrier(2)

        def submit(move: str, submission_id: str) -> tuple[str, str]:
            barrier.wait(timeout=1.0)
            try:
                result = _submit(
                    web,
                    session,
                    request.request_id,
                    move,
                    submission_id=submission_id,
                )
            except HumanInputError as exc:
                return move, exc.code
            return move, result.status

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(
                executor.map(
                    lambda args: submit(*args),
                    (("H8", "2" * 32), ("A1", "3" * 32)),
                )
            )
        accepted = [move for move, status in outcomes if status == "accepted"]
        rejected = [status for _move, status in outcomes if status != "accepted"]
        assert len(accepted) == 1
        assert rejected == ["already_submitted"]
        assert await pending == accepted[0]

        winner_id = "2" * 32 if accepted[0] == "H8" else "3" * 32
        retry = _submit(
            web,
            session,
            request.request_id,
            accepted[0],
            submission_id=winner_id,
        )
        assert retry.status == "idempotent"

    try:
        asyncio.run(scenario())
    finally:
        session.close()


def test_wrong_or_cross_session_capability_cannot_read_or_submit(tmp_path: Path) -> None:
    archive = tmp_path / "archive.db"
    first = _session(archive, player_name="甲")
    second = _session(archive, player_name="乙")
    web = _web(archive)

    async def scenario() -> None:
        pending = asyncio.create_task(first.resolve("move", timeout_seconds=1.0))
        request = await _wait_for_request(web, first)
        wrong = "A" * len(first.capability)
        assert _error_code(
            lambda: web.load(first.session_id, first.seat_id, capability=wrong)
        ) == "input_forbidden"
        assert _error_code(
            lambda: _submit(
                web,
                first,
                request.request_id,
                "A1",
                submission_id="4" * 32,
                capability=second.capability,
            )
        ) == "input_forbidden"
        _submit(
            web,
            first,
            request.request_id,
            "H8",
            submission_id="5" * 32,
        )
        assert await pending == "H8"

    try:
        asyncio.run(scenario())
    finally:
        first.close()
        second.close()


def test_web_process_restart_can_submit_the_same_pending_request(tmp_path: Path) -> None:
    archive = tmp_path / "archive.db"
    session = _session(archive)

    async def scenario() -> None:
        initial_web = _web(archive)
        pending = asyncio.create_task(session.resolve("move", timeout_seconds=1.0))
        request = await _wait_for_request(initial_web, session)

        restarted_web = _web(archive)
        snapshot = _load(restarted_web, session)
        assert snapshot.request.request_id == request.request_id
        _submit(
            restarted_web,
            session,
            request.request_id,
            "H8",
            submission_id="6" * 32,
        )
        assert await pending == "H8"

    try:
        asyncio.run(scenario())
    finally:
        session.close()


def test_timeout_and_late_submission_never_feed_the_next_request(tmp_path: Path) -> None:
    archive = tmp_path / "archive.db"
    session = _session(archive)
    web = _web(archive)

    async def scenario() -> None:
        expired = asyncio.create_task(session.resolve("first", timeout_seconds=0.03))
        first_request = await _wait_for_request(web, session)
        with pytest.raises(PlayerTimeoutError):
            await expired
        assert _error_code(
            lambda: _submit(
                web,
                session,
                first_request.request_id,
                "stale-move",
                submission_id="7" * 32,
            )
        ) == "request_stale"

        current = asyncio.create_task(session.resolve("second", timeout_seconds=1.0))
        second_request = await _wait_for_request(
            web,
            session,
            previous=first_request.request_id,
        )
        assert second_request.request_id != first_request.request_id
        _submit(
            web,
            session,
            second_request.request_id,
            "current-move",
            submission_id="8" * 32,
        )
        assert await current == "current-move"

    try:
        asyncio.run(scenario())
    finally:
        session.close()


def test_interrupt_cancels_pending_resolve_and_rejects_late_submission(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.db"
    session = _session(archive)
    web = _web(archive)

    async def scenario() -> None:
        pending = asyncio.create_task(session.resolve("move", timeout_seconds=1.0))
        request = await _wait_for_request(web, session)
        assert session.interrupt("producer_failed")
        with pytest.raises(HumanInputError) as caught:
            await pending
        assert caught.value.code == "input_interrupted"
        assert _error_code(
            lambda: _submit(
                web,
                session,
                request.request_id,
                "late",
                submission_id="9" * 32,
            )
        ) == "session_interrupted"

    try:
        asyncio.run(scenario())
    finally:
        session.close()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX symlink semantics")
def test_input_sidecar_symlink_is_rejected_without_touching_its_target(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.db"
    SQLiteStore(archive)
    target = tmp_path / "must-not-touch.db"
    target.write_bytes(b"target-sentinel")
    target.chmod(0o644)
    target_before = (target.read_bytes(), target.stat().st_mode, target.stat().st_mtime_ns)
    input_path = derive_human_input_database_path(archive)
    input_path.symlink_to(target)

    with pytest.raises(HumanInputError):
        _session(archive)

    assert input_path.is_symlink()
    assert (target.read_bytes(), target.stat().st_mode, target.stat().st_mtime_ns) == target_before


def test_corrupt_input_sidecar_fails_closed_without_replacing_it(tmp_path: Path) -> None:
    archive = tmp_path / "archive.db"
    SQLiteStore(archive)
    input_path = derive_human_input_database_path(archive)
    input_path.write_bytes(b"not-a-sqlite-database")
    input_path.chmod(0o600)
    before = (input_path.read_bytes(), input_path.stat().st_mtime_ns)

    with pytest.raises(HumanInputError):
        _session(archive)

    assert (input_path.read_bytes(), input_path.stat().st_mtime_ns) == before


def test_missing_input_sidecar_is_not_created_by_the_web_submitter(tmp_path: Path) -> None:
    archive = tmp_path / "archive.db"
    input_path = derive_human_input_database_path(archive)
    web = _web(archive)

    assert _error_code(
        lambda: web.load("a" * 32, "b" * 32, capability="c" * 43)
    ) == "participation_not_found"
    assert _error_code(
        lambda: web.submit(
            "a" * 32,
            "b" * 32,
            "d" * 32,
            capability="c" * 43,
            move="H8",
            submission_id="e" * 32,
        )
    ) == "participation_not_found"
    assert not archive.exists()
    assert not input_path.exists()


def test_engine_verdict_closes_request_and_next_request_has_new_identity(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.db"
    session = _session(archive)
    web = _web(archive)
    input_path = derive_human_input_database_path(archive)

    async def scenario() -> None:
        first_pending = asyncio.create_task(
            session.resolve("first prompt", timeout_seconds=1.0, match_event_seq=7)
        )
        first = await _wait_for_request(web, session)
        assert first.request_seq == 0
        assert first.match_event_seq == 7
        _submit(
            web,
            session,
            first.request_id,
            "illegal",
            submission_id="a" * 32,
        )
        assert await first_pending == "illegal"
        session.resolve_request(accepted=False, reason="illegal_move")
        rejected = _load(web, session).request
        assert rejected is not None
        assert rejected.request_id == first.request_id
        assert rejected.state == "rejected"
        assert not hasattr(rejected, "move")

        second_pending = asyncio.create_task(
            session.resolve("second prompt", timeout_seconds=1.0, match_event_seq=9)
        )
        second = await _wait_for_request(web, session, previous=first.request_id)
        assert second.request_id != first.request_id
        assert second.request_seq == first.request_seq + 1
        assert second.match_event_seq == 9
        assert _error_code(
            lambda: _submit(
                web,
                session,
                first.request_id,
                "replay",
                submission_id="b" * 32,
            )
        ) == "request_stale"
        _submit(
            web,
            session,
            second.request_id,
            "legal",
            submission_id="c" * 32,
        )
        assert await second_pending == "legal"
        session.resolve_request(accepted=True)
        accepted = _load(web, session).request
        assert accepted is not None
        assert accepted.request_id == second.request_id
        assert accepted.state == "accepted"

        with sqlite3.connect(input_path) as connection:
            rows = connection.execute(
                "SELECT request_id, state, reason, move FROM input_requests "
                "WHERE session_id=? ORDER BY request_seq",
                (session.session_id,),
            ).fetchall()
        assert rows == [
            (first.request_id, "rejected", "illegal_move", None),
            (second.request_id, "accepted", None, None),
        ]

    try:
        asyncio.run(scenario())
    finally:
        session.close()


def test_completion_is_terminal_and_exposes_only_final_archive_id(tmp_path: Path) -> None:
    archive = tmp_path / "archive.db"
    session = _session(archive, heartbeat_seconds=59.0)
    web = _web(archive)

    try:
        assert session.complete("match:final-1")
        snapshot = _load(web, session)
        assert snapshot.status == "completed"
        assert snapshot.final_match_id == "match:final-1"
        assert snapshot.request is None
        assert not session.complete("match:final-1")
        assert not session.interrupt("producer_failed")
    finally:
        session.close()


def test_expired_lease_fails_closed_and_is_reclaimed_by_a_new_producer(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.db"
    clock = _Clock()
    session = _session(archive, clock=clock, heartbeat_seconds=59.0)
    web = _web(archive, clock=clock)
    input_path = derive_human_input_database_path(archive)

    async def scenario() -> None:
        pending = asyncio.create_task(session.resolve("move", timeout_seconds=30.0))
        request = await _wait_for_request(web, session)
        clock.value += 61.0
        snapshot = _load(web, session)
        assert snapshot.status == "expired"
        assert snapshot.request is None
        assert _error_code(
            lambda: _submit(
                web,
                session,
                request.request_id,
                "late",
                submission_id="d" * 32,
            )
        ) == "session_interrupted"
        with pytest.raises(HumanInputError) as caught:
            await pending
        assert caught.value.code == "input_session_lost"

    try:
        asyncio.run(scenario())
        replacement = _session(
            archive,
            clock=clock,
            heartbeat_seconds=59.0,
            player_name="新的人类",
        )
        try:
            with sqlite3.connect(input_path) as connection:
                status, reason = connection.execute(
                    "SELECT status, terminal_reason_code FROM input_sessions "
                    "WHERE session_id=?",
                    (session.session_id,),
                ).fetchone()
            assert (status, reason) == ("interrupted", "lease_expired")
        finally:
            replacement.close()
    finally:
        session.close()


def test_expired_lease_cannot_be_revived_by_a_late_heartbeat(tmp_path: Path) -> None:
    archive = tmp_path / "archive.db"
    clock = _Clock()
    session = _session(archive, clock=clock, heartbeat_seconds=0.01)
    web = _web(archive, clock=clock)
    input_path = derive_human_input_database_path(archive)

    try:
        original_expiry = _load(web, session).lease_expires_at
        clock.value += 61.0

        assert session._stop.wait(timeout=1.0)
        assert session._failure is not None
        assert session._failure.code == "input_session_lost"
        snapshot = _load(web, session)
        assert snapshot.status == "expired"
        with sqlite3.connect(input_path) as connection:
            stored_expiry = connection.execute(
                "SELECT lease_expires_at FROM input_sessions WHERE session_id=?",
                (session.session_id,),
            ).fetchone()[0]
        assert stored_expiry == original_expiry.timestamp()
    finally:
        session.close()


def test_expired_lease_cannot_be_revived_by_consume_or_completion(tmp_path: Path) -> None:
    archive = tmp_path / "archive.db"
    clock = _Clock()
    session = _session(archive, clock=clock, heartbeat_seconds=59.0)
    web = _web(archive, clock=clock)
    request_id = session._create_request("move", 120.0, 0)
    _submit(
        web,
        session,
        request_id,
        "submitted-before-expiry",
        submission_id="6" * 32,
    )

    try:
        clock.value += 61.0
        with pytest.raises(HumanInputError) as caught:
            session._consume_if_ready(request_id)
        assert caught.value.code == "input_session_lost"
        assert not session.complete("must-not-complete")
        assert _load(web, session).status == "expired"
    finally:
        session.close()


def test_stale_session_recovery_scrubs_an_unconsumed_submitted_move(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.db"
    clock = _Clock()
    stale = _session(archive, clock=clock, heartbeat_seconds=59.0)
    web = _web(archive, clock=clock)
    input_path = derive_human_input_database_path(archive)
    request_id = stale._create_request("secret prompt", 30.0, 0)
    _submit(
        web,
        stale,
        request_id,
        "secret submitted move",
        submission_id="f" * 32,
    )

    with sqlite3.connect(input_path) as connection:
        assert connection.execute(
            "SELECT state, move FROM input_requests WHERE request_id=?",
            (request_id,),
        ).fetchone() == ("submitted", "secret submitted move")

    clock.value += 61.0
    replacement = _session(
        archive,
        clock=clock,
        heartbeat_seconds=59.0,
        player_name="恢复后的生产者",
    )
    try:
        with sqlite3.connect(input_path) as connection:
            request_state = connection.execute(
                "SELECT state, move, resolved_at FROM input_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            session_state = connection.execute(
                "SELECT status, current_request_id, terminal_reason_code "
                "FROM input_sessions WHERE session_id=?",
                (stale.session_id,),
            ).fetchone()
        assert request_state == ("cancelled", None, clock.value)
        assert session_state == ("interrupted", None, "lease_expired")
    finally:
        replacement.close()
        stale.close()


def test_capacity_eviction_preserves_stale_move_scrubbing(tmp_path: Path) -> None:
    archive = tmp_path / "archive.db"
    clock = _Clock()
    input_path = derive_human_input_database_path(archive)
    oldest_session_id = ""

    for index in range(INPUT_MAX_SESSIONS - 1):
        terminal = _session(archive, clock=clock, heartbeat_seconds=59.0)
        if index == 0:
            oldest_session_id = terminal.session_id
        assert terminal.complete(f"match-{index}")
        terminal.close()
        clock.value += 1.0

    stale = _session(archive, clock=clock, heartbeat_seconds=59.0)
    web = _web(archive, clock=clock)
    request_id = stale._create_request("capacity prompt", 30.0, 0)
    _submit(
        web,
        stale,
        request_id,
        "capacity-secret-move",
        submission_id="e" * 32,
    )
    clock.value += 61.0

    replacement = _session(
        archive,
        clock=clock,
        heartbeat_seconds=59.0,
        player_name="容量恢复者",
    )
    try:
        with sqlite3.connect(input_path) as connection:
            count = connection.execute("SELECT COUNT(*) FROM input_sessions").fetchone()[0]
            oldest = connection.execute(
                "SELECT 1 FROM input_sessions WHERE session_id=?",
                (oldest_session_id,),
            ).fetchone()
            recovered = connection.execute(
                "SELECT state, move FROM input_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
        assert count == INPUT_MAX_SESSIONS
        assert oldest is None
        assert recovered == ("cancelled", None)
        assert b"capacity-secret-move" not in input_path.read_bytes()
    finally:
        replacement.close()
        stale.close()


def test_timeout_submission_race_is_atomic_first_write_wins(tmp_path: Path) -> None:
    outcomes: set[tuple[tuple[str, str], tuple[str, str]]] = set()

    for index in range(8):
        archive = tmp_path / f"race-{index}.db"
        producer_clock = _Clock()
        browser_clock = _Clock()
        session = _session(
            archive,
            clock=producer_clock,
            heartbeat_seconds=59.0,
        )
        web = _web(archive, clock=browser_clock)
        request_id = session._create_request("race", 1.0, index)
        producer_clock.value += 2.0
        browser_clock.value += 0.5
        barrier = threading.Barrier(2)

        def consume(
            race_barrier: threading.Barrier = barrier,
            race_session: InputSessionStore = session,
            race_request_id: str = request_id,
        ) -> tuple[str, str]:
            race_barrier.wait(timeout=1.0)
            try:
                value = race_session._consume_if_ready(race_request_id)
            except HumanInputError as exc:
                return ("error", exc.code)
            assert value is not None
            return ("move", value)

        def submit(
            race_barrier: threading.Barrier = barrier,
            race_web: WebSubmissionStore = web,
            race_session: InputSessionStore = session,
            race_request_id: str = request_id,
            race_index: int = index,
        ) -> tuple[str, str]:
            race_barrier.wait(timeout=1.0)
            try:
                result = _submit(
                    race_web,
                    race_session,
                    race_request_id,
                    "race-move",
                    submission_id=f"{race_index:032x}",
                )
            except HumanInputError as exc:
                return ("error", exc.code)
            return ("submit", result.status)

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                consumed = executor.submit(consume)
                submitted = executor.submit(submit)
                outcome = (consumed.result(), submitted.result())
            outcomes.add(outcome)
            assert outcome in {
                (("move", "race-move"), ("submit", "accepted")),
                (("error", "request_expired"), ("error", "request_stale")),
            }
            if outcome[0][0] == "move":
                session.resolve_request(accepted=True)
        finally:
            session.close()

    assert outcomes


def test_cancelled_resolve_rejects_old_request_and_allows_a_fresh_one(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.db"
    session = _session(archive)
    web = _web(archive)

    async def scenario() -> None:
        cancelled = asyncio.create_task(session.resolve("first", timeout_seconds=1.0))
        first = await _wait_for_request(web, session)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert _error_code(
            lambda: _submit(
                web,
                session,
                first.request_id,
                "stale",
                submission_id="1" * 32,
            )
        ) == "request_stale"

        current = asyncio.create_task(session.resolve("second", timeout_seconds=1.0))
        second = await _wait_for_request(web, session, previous=first.request_id)
        _submit(
            web,
            session,
            second.request_id,
            "fresh",
            submission_id="2" * 32,
        )
        assert await current == "fresh"

    try:
        asyncio.run(scenario())
    finally:
        session.close()


def test_bounded_inputs_fail_before_mutating_the_pending_request(tmp_path: Path) -> None:
    archive = tmp_path / "archive.db"
    session = _session(archive)
    web = _web(archive)

    async def scenario() -> None:
        pending = asyncio.create_task(session.resolve("move", timeout_seconds=1.0))
        request = await _wait_for_request(web, session)
        assert _error_code(
            lambda: _submit(
                web,
                session,
                request.request_id,
                "x" * (INPUT_MAX_MOVE_CHARS + 1),
                submission_id="3" * 32,
            )
        ) == "invalid_request"
        assert _error_code(
            lambda: _submit(
                web,
                session,
                request.request_id,
                "valid",
                submission_id="not-hex",
            )
        ) == "invalid_request"
        assert _load(web, session).request.state == "pending"
        _submit(
            web,
            session,
            request.request_id,
            "valid",
            submission_id="4" * 32,
        )
        assert await pending == "valid"

        with pytest.raises(HumanInputError) as caught:
            await session.resolve(
                "x" * (INPUT_MAX_PROMPT_CHARS + 1),
                timeout_seconds=1.0,
            )
        assert caught.value.code == "invalid_request"

    try:
        asyncio.run(scenario())
    finally:
        session.close()


def test_browser_human_player_uses_fragment_capability_and_async_inbox(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.db"
    human = HumanPlayer("人类", timeout=1.0, entrant_id="human:web-player")
    opponent = SimpleNamespace(name="模型")
    game = SimpleNamespace(name="math_quiz")
    browser = BrowserHumanPlayer.create(archive, game, [human, opponent], human)
    web = _web(archive)
    url = urlsplit(browser.participation_url("http://127.0.0.1:8000"))
    capability = parse_qs(url.fragment)["capability"][0]

    async def scenario() -> None:
        pending = asyncio.create_task(browser.get_move("browser prompt"))
        for _ in range(100):
            snapshot = web.load(
                browser.session_id,
                browser.seat_id,
                capability=capability,
            )
            if snapshot.request is not None:
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("browser player request did not become visible")
        web.submit(
            browser.session_id,
            browser.seat_id,
            snapshot.request.request_id,
            capability=capability,
            move="42",
            submission_id="5" * 32,
        )
        assert await pending == "42"

    try:
        assert url.query == ""
        assert url.fragment.startswith("capability=")
        description = browser.describe()
        assert description["entrant_id"] == human.entrant_id
        assert capability not in repr(description)
        assert browser.session_id not in repr(description)
        asyncio.run(scenario())
        browser.complete("browser-final")
        terminal = web.load(
            browser.session_id,
            browser.seat_id,
            capability=capability,
        )
        assert terminal.status == "completed"
        assert terminal.final_match_id == "browser-final"
    finally:
        browser.close()


def test_runtime_sidecar_corruption_cannot_modify_the_archive(tmp_path: Path) -> None:
    archive = tmp_path / "archive.db"
    SQLiteStore(archive)
    before = (_digest(archive), archive.stat().st_mode, archive.stat().st_mtime_ns)
    session = _session(archive, heartbeat_seconds=59.0)
    web = _web(archive)
    input_path = derive_human_input_database_path(archive)
    input_path.write_bytes(b"corrupted-after-session-start")
    input_path.chmod(0o600)

    async def scenario() -> None:
        with pytest.raises(HumanInputError) as caught:
            await session.resolve("move", timeout_seconds=1.0)
        assert caught.value.code in {"input_invalid", "input_unavailable"}

    try:
        asyncio.run(scenario())
        assert _error_code(
            lambda: web.load(
                session.session_id,
                session.seat_id,
                capability=session.capability,
            )
        ) in {"input_invalid", "input_unavailable"}
    finally:
        session.close()

    assert (_digest(archive), archive.stat().st_mode, archive.stat().st_mtime_ns) == before


def test_schema_valid_row_corruption_is_normalized_without_accepting_a_move(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.db"
    session = _session(archive, heartbeat_seconds=59.0)
    web = _web(archive)
    input_path = derive_human_input_database_path(archive)
    request_id = session._create_request("prompt", 30.0, 0)

    with sqlite3.connect(input_path) as connection:
        connection.execute(
            "UPDATE input_sessions SET lease_expires_at='not-a-time' WHERE session_id=?",
            (session.session_id,),
        )
        connection.commit()

    try:
        assert _error_code(
            lambda: _submit(
                web,
                session,
                request_id,
                "must-not-be-accepted",
                submission_id="0" * 32,
            )
        ) == "input_unavailable"
        with sqlite3.connect(input_path) as connection:
            assert connection.execute(
                "SELECT state, move, submission_id FROM input_requests WHERE request_id=?",
                (request_id,),
            ).fetchone() == ("pending", None, None)
    finally:
        session.close()
