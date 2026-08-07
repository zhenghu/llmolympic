"""Opt-in real-provider smoke test for creative-writing adjudication.

The billable test is intentionally skipped unless ``LLMOLYMPIC_RUN_LIVE=1``.
It keeps contestants local and deterministic so a live run spends provider
calls only on the configured judge panel.
"""

from __future__ import annotations

import json
import math
import os

import pytest
from typer.testing import CliRunner

from llmolympic.cli.main import app
from llmolympic.core.events import EventType
from llmolympic.core.judge import MAX_JUDGES, MIN_JUDGES, PanelVerdict
from llmolympic.core.storage import SQLiteStore

runner = CliRunner()


def _redact_openai_key(value: str) -> str:
    """Make test diagnostics safe even if a provider accidentally echoes its key."""

    key = os.environ.get("OPENAI_API_KEY", "")
    return value.replace(key, "[REDACTED]") if key else value


def _live_judges() -> list[str]:
    raw = os.environ.get("LLMOLYMPIC_LIVE_JUDGES")
    if raw is None:
        pytest.fail(
            "LLMOLYMPIC_RUN_LIVE=1 requires LLMOLYMPIC_LIVE_JUDGES "
            "with 3-9 comma-separated CLI judge specs",
            pytrace=False,
        )
    judges = [item.strip() for item in raw.split(",")]
    if any(not item for item in judges) or not MIN_JUDGES <= len(judges) <= MAX_JUDGES:
        pytest.fail(
            "LLMOLYMPIC_LIVE_JUDGES must contain 3-9 non-empty comma-separated "
            "CLI judge specs",
            pytrace=False,
        )
    if any(
        kind != "openai" or not model.strip()
        for kind, _, model in (item.partition(":") for item in judges)
    ):
        pytest.fail(
            "LLMOLYMPIC_LIVE_JUDGES accepts only explicit openai:<model> specs; "
            "mock, human, Ollama, and profiles do not satisfy this cloud-provider smoke",
            pytrace=False,
        )
    return judges


def _live_timeout() -> float:
    raw = os.environ.get("LLMOLYMPIC_LIVE_LLM_TIMEOUT", "180")
    try:
        timeout = float(raw)
    except ValueError:
        timeout = math.nan
    if not math.isfinite(timeout) or timeout <= 0:
        pytest.fail(
            "LLMOLYMPIC_LIVE_LLM_TIMEOUT must be a positive finite number",
            pytrace=False,
        )
    return timeout


def _openai_api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key.strip():
        pytest.fail(
            "LLMOLYMPIC_RUN_LIVE=1 requires OPENAI_API_KEY in the environment",
            pytrace=False,
        )
    return key


def test_live_provider_config_rejects_mock_judges(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "LLMOLYMPIC_LIVE_JUDGES",
        "mock:strict,mock:balanced,mock:lenient",
    )

    with pytest.raises(pytest.fail.Exception, match="only explicit openai"):
        _live_judges()


def test_live_provider_config_requires_environment_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(pytest.fail.Exception, match="requires OPENAI_API_KEY"):
        _openai_api_key()


@pytest.mark.live_provider
def test_live_creative_provider_panel_round_trip(tmp_path) -> None:
    """Run one billable panel, then verify adjudication, storage, ELO, and secrecy."""

    if os.environ.get("LLMOLYMPIC_RUN_LIVE") != "1":
        pytest.skip("set LLMOLYMPIC_RUN_LIVE=1 to enable real provider calls")

    judges = _live_judges()
    api_key = _openai_api_key()
    timeout = _live_timeout()
    database = tmp_path / "live-creative-provider.db"
    args = [
        "play",
        "--game",
        "creative_writing",
        "--players",
        "mock:random,mock:fixed",
        "--seed",
        "20260806",
        "--llm-timeout",
        str(timeout),
        "--db",
        str(database),
    ]
    for judge in judges:
        args.extend(("--judge", judge))

    result = runner.invoke(app, args)
    if api_key and api_key in result.output:
        pytest.fail("CLI output exposed OPENAI_API_KEY; value redacted", pytrace=False)
    if result.exit_code != 0:
        safe_output = _redact_openai_key(result.output)
        pytest.fail(
            f"live creative-provider smoke failed with exit code {result.exit_code}:\n"
            f"{safe_output[-4_000:]}",
            pytrace=False,
        )

    store = SQLiteStore(database)
    matches = store.list_matches(game="creative_writing")
    assert len(matches) == 1
    summary = matches[0]
    assert summary.rated is True

    archive = store.get_match(summary.match_id)
    assert archive is not None
    assert archive.game == "creative_writing"
    assert archive.seed == 20260806
    assert archive.scores == summary.scores
    assert len(archive.players) == 2

    finished = [event for event in archive.events if event.type == EventType.MATCH_FINISHED]
    assert len(finished) == 1
    verdict = PanelVerdict.model_validate(finished[0].data["judging"])
    expected_players = {"mock:random", "mock:fixed"}
    assert verdict.panel_size == len(judges)
    assert verdict.quorum == len(judges) // 2 + 1
    # Unit tests cover quorum degradation.  A live-provider smoke is stricter:
    # every configured route must work, otherwise a broken provider could hide
    # behind the panel's normal majority fallback and leave the workflow green.
    assert verdict.successful_judges == verdict.panel_size
    assert len(verdict.verdicts) == verdict.successful_judges
    assert verdict.failures == []
    assert verdict.scores == archive.scores
    assert set(verdict.scores) == expected_players
    assert all(0.0 <= score <= 1.0 for score in verdict.scores.values())

    overall_elo = store.leaderboard()
    creative_elo = store.leaderboard(game="creative_writing")
    for leaderboard in (overall_elo, creative_elo):
        assert len(leaderboard) == 2
        assert {entry.player for entry in leaderboard} == expected_players
        assert all(entry.games_played == 1 for entry in leaderboard)
        assert all(entry.wins + entry.draws + entry.losses == 1 for entry in leaderboard)

    archive_json = archive.to_json()
    if api_key and api_key in archive_json:
        pytest.fail("persisted archive exposed OPENAI_API_KEY; value redacted", pytrace=False)
    base_url = os.environ.get("OPENAI_BASE_URL", "")
    if base_url and base_url in archive_json:
        pytest.fail("persisted archive exposed OPENAI_BASE_URL; value redacted", pytrace=False)

    smoke_summary = {
        "failure_reason_codes": sorted(failure.reason_code for failure in verdict.failures),
        "game": archive.game,
        "match_id": archive.match_id,
        "panel_size": verdict.panel_size,
        "quorum": verdict.quorum,
        "scores": dict(sorted(verdict.scores.items())),
        "successful_judges": verdict.successful_judges,
    }
    print("LIVE_PROVIDER_SMOKE " + json.dumps(smoke_summary, sort_keys=True))
