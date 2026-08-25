"""Championship CLI control protocol and Live v2 integration coverage."""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from itertools import groupby
from pathlib import Path
from typing import ClassVar

import pytest
from typer.testing import CliRunner

from llmolympic.cli.main import _prepare_championship, app
from llmolympic.core.championship import (
    ChampionshipCheckpoint,
    prepare_championship,
    resume_championship,
)
from llmolympic.core.events import MatchEvent
from llmolympic.core.storage import SQLiteStore
from llmolympic.live import derive_live_database_path
from llmolympic.web.live_reader import LiveSQLiteReader

runner = CliRunner()
_TOKEN = "c" * 43
_JOB_ID = "championship-live-test"
_CONTEXT_KEYS = {
    "round_number",
    "round_count",
    "round_pairing_number",
    "round_pairing_count",
    "pairing_number",
    "pairing_count",
    "leg_number",
}


class _ControlCapture:
    def __init__(self, database: Path | None = None) -> None:
        self.database = database
        self.frames: list[dict[str, object]] = []
        self.completed_after_commit = False

    def write(self, value: str) -> int:
        prefix = f"@@LLMOLYMPIC_CONTROL_V1:{_TOKEN}:"
        assert value.startswith(prefix)
        encoded = value[len(prefix) :].strip()
        payload = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        )
        assert isinstance(payload, dict)
        self.frames.append(payload)
        if payload["type"] == "completed" and self.database is not None:
            with sqlite3.connect(self.database) as connection:
                archive = connection.execute(
                    "SELECT 1 FROM championship_archives WHERE championship_id = ?",
                    (payload["final_id"],),
                ).fetchone()
                checkpoint = connection.execute(
                    "SELECT status FROM championship_checkpoints WHERE championship_id = ?",
                    (payload["final_id"],),
                ).fetchone()
            self.completed_after_commit = (
                archive is not None
                and checkpoint is not None
                and checkpoint[0] == "finalized"
            )
        return len(value)

    def flush(self) -> None:
        pass


class _RecordingLivePublisher:
    instances: ClassVar[list[_RecordingLivePublisher]] = []

    def __init__(self, database: str | Path, mode: str) -> None:
        self.database = Path(database)
        self.mode = mode
        self.failed = False
        self.operations: list[tuple[str, dict[str, object]]] = []
        self.live_id = f"live-{len(self.instances) + 1}"
        self.instances.append(self)

    def start_session(
        self,
        event: MatchEvent,
        context: Mapping[str, object] | None = None,
        **fields: object,
    ) -> str:
        self.operations.append(
            (
                "start",
                {
                    "event": event,
                    "context": dict(context or {}),
                    **fields,
                },
            )
        )
        return self.live_id

    def publish(
        self,
        live_id: str,
        event: MatchEvent,
        context: Mapping[str, object] | None = None,
    ) -> bool:
        assert live_id == self.live_id
        self.operations.append(
            (
                "publish",
                {"event": event, "context": dict(context or {})},
            )
        )
        return True

    def publish_pairing_completed(self, live_id: str, **fields: object) -> bool:
        assert live_id == self.live_id
        self.operations.append(("pairing_completed", dict(fields)))
        return True

    def publish_round_committed(self, live_id: str, **fields: object) -> bool:
        assert live_id == self.live_id
        self.operations.append(("round_committed", dict(fields)))
        return True

    def complete(self, live_id: str, **fields: object) -> bool:
        assert live_id == self.live_id
        self.operations.append(("complete", dict(fields)))
        return True

    def interrupt(self, live_id: str, **fields: object) -> bool:
        assert live_id == self.live_id
        self.operations.append(("interrupt", dict(fields)))
        return True

    def close(self) -> None:
        self.operations.append(("close", {}))


class _FailingLivePublisher(_RecordingLivePublisher):
    def publish(
        self,
        live_id: str,
        event: MatchEvent,
        context: Mapping[str, object] | None = None,
    ) -> bool:
        del live_id, event, context
        raise RuntimeError("injected live event failure")

    def publish_pairing_completed(self, live_id: str, **fields: object) -> bool:
        del live_id, fields
        raise RuntimeError("injected live pairing failure")

    def publish_round_committed(self, live_id: str, **fields: object) -> bool:
        del live_id, fields
        raise RuntimeError("injected live checkpoint failure")

    def complete(self, live_id: str, **fields: object) -> bool:
        del live_id, fields
        raise RuntimeError("injected live completion failure")

    def interrupt(self, live_id: str, **fields: object) -> bool:
        del live_id, fields
        raise RuntimeError("injected live interruption failure")

    def close(self) -> None:
        raise RuntimeError("injected live close failure")


@pytest.fixture(autouse=True)
def _clear_recorded_publishers() -> None:
    _RecordingLivePublisher.instances.clear()


def _managed_environment() -> dict[str, str]:
    return {
        "LLMOLYMPIC_CONTROL_PROTOCOL_TOKEN": _TOKEN,
        "LLMOLYMPIC_CONTROL_JOB_ID": _JOB_ID,
    }


def _new_championship_args(database: Path) -> list[str]:
    return [
        "championship",
        "--game",
        "math_quiz",
        "--players",
        "mock:random,mock:fixed,mock:illegal,mock:balanced",
        "--rounds",
        "1",
        "--seed",
        "11",
        "--db",
        str(database),
    ]


def _prepared_checkpoint(
    database: Path,
    *,
    championship_id: str,
) -> tuple[SQLiteStore, object, list[object], ChampionshipCheckpoint]:
    game, players = _prepare_championship(
        game_name="math_quiz",
        player_spec="mock:random,mock:fixed,mock:illegal,mock:balanced",
        rounds=1,
        llm_timeout=1,
        no_llm_timeout=False,
    )
    checkpoint = prepare_championship(
        game,
        players,
        seed=19,
        championship_id=championship_id,
    )
    store = SQLiteStore(database)
    store.save_championship_checkpoint(checkpoint)
    return store, game, players, checkpoint


def _advance_checkpoint(
    store: SQLiteStore,
    game: object,
    players: Sequence[object],
    checkpoint: ChampionshipCheckpoint,
    *,
    stop_after_first_round: bool,
) -> ChampionshipCheckpoint:
    class _RoundSaved(Exception):
        pass

    claim = store.claim_championship_runner(checkpoint.championship_id)

    def save(updated: ChampionshipCheckpoint) -> None:
        store.save_championship_checkpoint(updated, lease=claim.lease)
        if stop_after_first_round:
            raise _RoundSaved

    async def advance() -> None:
        try:
            await resume_championship(
                game,  # type: ignore[arg-type]
                players,  # type: ignore[arg-type]
                checkpoint,
                on_checkpoint=save,
            )
        except _RoundSaved:
            pass

    try:
        asyncio.run(advance())
    finally:
        assert store.release_championship_runner(claim.lease)
    updated = store.get_championship_checkpoint(checkpoint.championship_id)
    assert updated is not None
    return updated


def _event_contexts(
    publisher: _RecordingLivePublisher,
) -> list[dict[str, object]]:
    return [
        operation[1]["context"]  # type: ignore[misc]
        for operation in publisher.operations
        if operation[0] in {"start", "publish"}
    ]


def test_championship_cli_publishes_full_live_bracket_and_control_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "championship-live.db"
    control = _ControlCapture(database)
    monkeypatch.setattr(sys, "__stdout__", control)
    monkeypatch.setattr("llmolympic.cli.main.LivePublisher", _RecordingLivePublisher)

    result = runner.invoke(app, _new_championship_args(database), env=_managed_environment())

    assert result.exit_code == 0, result.output
    publisher = _RecordingLivePublisher.instances[-1]
    assert publisher.mode == "championship"
    frame_types = [frame["type"] for frame in control.frames]
    assert frame_types[0] == "running"
    assert frame_types[-2:] == ["finalizing", "completed"]
    assert frame_types.index("live_started") > frame_types.index("running")
    running = control.frames[0]
    assert "championship_id" in running
    assert "tournament_id" not in running

    start = next(payload for kind, payload in publisher.operations if kind == "start")
    roster = start["players"]
    assert isinstance(roster, tuple)
    assert len(roster) == 4
    assert len(set(roster)) == 4
    assert start["game"] == "math_quiz"
    assert start["championship_id"] == running["championship_id"]
    assert start["initial_bracket"] == ()

    contexts = _event_contexts(publisher)
    assert contexts
    assert all(set(context) == _CONTEXT_KEYS for context in contexts)
    placements = [
        key
        for key, _group in groupby(
            (
                context["round_number"],
                context["round_count"],
                context["round_pairing_number"],
                context["round_pairing_count"],
                context["pairing_number"],
                context["pairing_count"],
                context["leg_number"],
            )
            for context in contexts
        )
    ]
    assert placements == [
        (1, 2, 1, 2, 1, 3, 1),
        (1, 2, 1, 2, 1, 3, 2),
        (1, 2, 2, 2, 2, 3, 1),
        (1, 2, 2, 2, 2, 3, 2),
        (2, 2, 1, 1, 3, 3, 1),
        (2, 2, 1, 1, 3, 3, 2),
    ]

    pairings = [
        payload for kind, payload in publisher.operations if kind == "pairing_completed"
    ]
    rounds = [
        payload for kind, payload in publisher.operations if kind == "round_committed"
    ]
    assert [payload["context"]["pairing_number"] for payload in pairings] == [1, 2, 3]
    assert [payload["context"]["pairing_number"] for payload in rounds] == [2, 3]
    for payload in pairings:
        assert len(payload["players"]) == 2
        assert payload["winner"] in payload["players"]
        assert len(payload["match_ids"]) == 2

    operation_kinds = [kind for kind, _payload in publisher.operations]
    assert operation_kinds.index("pairing_completed") < operation_kinds.index(
        "round_committed"
    )
    complete = next(payload for kind, payload in publisher.operations if kind == "complete")
    assert complete["final_kind"] == "championship"
    assert complete["final_id"] == running["championship_id"]
    assert "championship_id" not in complete
    assert complete["final_match_ids"] == tuple(
        control.frames[-1]["final_match_ids"]
    )
    assert control.frames[-2] == control.frames[-1] | {"type": "finalizing"}
    assert control.completed_after_commit


def test_championship_resume_starts_a_new_live_session_from_committed_bracket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "championship-resume-live.db"
    store, game, players, checkpoint = _prepared_checkpoint(
        database,
        championship_id="championship-resume-live",
    )
    partial = _advance_checkpoint(
        store,
        game,
        players,
        checkpoint,
        stop_after_first_round=True,
    )
    assert len(partial.completed_series) == 2
    control = _ControlCapture(database)
    monkeypatch.setattr(sys, "__stdout__", control)
    monkeypatch.setattr("llmolympic.cli.main.LivePublisher", _RecordingLivePublisher)

    result = runner.invoke(
        app,
        ["championship", "--resume", checkpoint.championship_id, "--db", str(database)],
        env=_managed_environment(),
    )

    assert result.exit_code == 0, result.output
    publisher = _RecordingLivePublisher.instances[-1]
    start = next(payload for kind, payload in publisher.operations if kind == "start")
    assert publisher.live_id == "live-1"
    assert len(start["players"]) == 4
    bracket = start["initial_bracket"]
    assert isinstance(bracket, tuple)
    assert len(bracket) == 2
    for index, entry in enumerate(bracket, start=1):
        assert set(entry) == {
            "round_number",
            "round_pairing_number",
            "pairing_number",
            "players",
            "winner",
            "series_id",
            "match_ids",
            "status",
        }
        assert entry["pairing_number"] == index
        assert entry["status"] == "committed"
    contexts = _event_contexts(publisher)
    assert contexts
    assert {context["round_number"] for context in contexts} == {2}
    assert {context["pairing_number"] for context in contexts} == {3}
    assert control.completed_after_commit


def test_complete_checkpoint_fast_path_emits_terminal_control_after_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "championship-complete-fast-path.db"
    store, game, players, checkpoint = _prepared_checkpoint(
        database,
        championship_id="championship-complete-fast-path",
    )
    complete = _advance_checkpoint(
        store,
        game,
        players,
        checkpoint,
        stop_after_first_round=False,
    )
    assert complete.is_complete
    control = _ControlCapture(database)
    monkeypatch.setattr(sys, "__stdout__", control)
    monkeypatch.setattr("llmolympic.cli.main.LivePublisher", _RecordingLivePublisher)

    result = runner.invoke(
        app,
        ["championship", "--resume", checkpoint.championship_id, "--db", str(database)],
        env=_managed_environment(),
    )

    assert result.exit_code == 0, result.output
    assert [frame["type"] for frame in control.frames] == [
        "running",
        "finalizing",
        "completed",
    ]
    finalizing, completed = control.frames[-2:]
    assert finalizing["final_kind"] == completed["final_kind"] == "championship"
    assert finalizing["final_id"] == completed["final_id"] == checkpoint.championship_id
    assert finalizing["final_match_ids"] == completed["final_match_ids"]
    assert len(completed["final_match_ids"]) == 6
    assert control.completed_after_commit
    publisher = _RecordingLivePublisher.instances[-1]
    assert [kind for kind, _payload in publisher.operations] == ["close"]


def test_live_publisher_failures_do_not_change_championship_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "championship-live-failure.db"
    control = _ControlCapture(database)
    monkeypatch.setattr(sys, "__stdout__", control)
    monkeypatch.setattr("llmolympic.cli.main.LivePublisher", _FailingLivePublisher)

    result = runner.invoke(app, _new_championship_args(database), env=_managed_environment())

    assert result.exit_code == 0, result.output
    assert "实时观战不可用" in result.output
    frame_types = [frame["type"] for frame in control.frames]
    assert "live_started" in frame_types
    assert frame_types[-2:] == ["finalizing", "completed"]
    championship_id = control.frames[-1]["final_id"]
    assert SQLiteStore(database, create=False).get_championship(championship_id) is not None
    assert control.completed_after_commit


def test_championship_cli_real_live_v2_sidecar_is_canonically_completed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "championship-real-live.db"

    result = runner.invoke(app, _new_championship_args(database))

    assert result.exit_code == 0, result.output
    live_path = derive_live_database_path(database)
    with sqlite3.connect(live_path) as connection:
        live_id, status = connection.execute(
            "SELECT live_id, status FROM live_sessions"
        ).fetchone()
    assert status == "completed"
    detail = LiveSQLiteReader(database).load_live(live_id)
    assert detail.match.mode == "championship"
    assert len(detail.match.players) == 4
    assert detail.match.final_kind == "championship"
    assert detail.match.final_id is not None
    assert detail.match.championship_bracket is not None
    archive = SQLiteStore(database, create=False).get_championship(detail.match.final_id)
    assert archive is not None
    assert detail.match.championship_bracket.champion == archive.champion
    assert len(detail.match.championship_bracket.pairings) == 3
    assert {
        pairing.status for pairing in detail.match.championship_bracket.pairings
    } == {"committed"}
    assert len(detail.match.final_match_ids) == 6
    kinds = [event.kind for event in detail.events]
    assert kinds.count("pairing_completed") == 3
    assert kinds.count("round_committed") == 2
