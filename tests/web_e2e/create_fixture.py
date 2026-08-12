"""Create one deterministic archive using the installed distribution."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

from llmolympic.core.archive import MatchArchive
from llmolympic.core.events import EventType, MatchEvent
from llmolympic.core.storage import SQLiteStore

MATCH_ID = "web-e2e-match"
XSS_SENTINEL = '<img src=x onerror="globalThis.__LLMOLYMPIC_XSS__=true">'


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    if args.database.exists():
        raise SystemExit("refusing to replace an existing E2E database")

    started = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    players = [
        {
            "name": XSS_SENTINEL,
            "display_name": XSS_SENTINEL,
            "entrant_id": "web:e2e-first",
            "kind": "mock",
            "model": "first",
        },
        {
            "name": "安全对手",
            "display_name": "安全对手",
            "entrant_id": "web:e2e-second",
            "kind": "mock",
            "model": "second",
        },
    ]
    scores = {XSS_SENTINEL: 1.0, "安全对手": 0.0}
    events = [
        MatchEvent(
            seq=0,
            type=EventType.MATCH_STARTED,
            timestamp=started,
            data={
                "game": "math_quiz",
                "seed": 42,
                "game_config": {"rounds": 1},
                "players": players,
            },
        ),
        MatchEvent(
            seq=1,
            type=EventType.TURN_PROMPT,
            timestamp=started + timedelta(milliseconds=100),
            player=XSS_SENTINEL,
            data={"prompt": XSS_SENTINEL},
        ),
        MatchEvent(
            seq=2,
            type=EventType.MOVE_RECEIVED,
            timestamp=started + timedelta(milliseconds=200),
            player=XSS_SENTINEL,
            data={"move": "4"},
        ),
        MatchEvent(
            seq=3,
            type=EventType.MATCH_FINISHED,
            timestamp=started + timedelta(seconds=1),
            data={"scores": scores, "termination": "completed"},
        ),
    ]
    archive = MatchArchive(
        schema_version=2,
        source="local_engine",
        match_id=MATCH_ID,
        game="math_quiz",
        seed=42,
        players=players,
        events=events,
        moves=[],
        scores=scores,
        started_at=started,
        finished_at=started + timedelta(seconds=1),
    )
    SQLiteStore(args.database).save_match(archive, rating_source="engine")


if __name__ == "__main__":
    main()
