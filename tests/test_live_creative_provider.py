"""Opt-in real-provider smoke test for creative-writing adjudication.

The billable test is intentionally skipped unless ``LLMOLYMPIC_RUN_LIVE=1``.
It keeps contestants local and deterministic so a live run spends provider
calls only on the configured judge panel.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest
from typer.testing import CliRunner

from llmolympic.cli.main import app
from llmolympic.core.events import EventType
from llmolympic.core.judge import (
    MAX_JUDGES,
    MIN_JUDGES,
    JudgePanelError,
    JudgingRequest,
    PanelVerdict,
    score_judge_submission,
)
from llmolympic.core.player import LLMPlayer, PlayerActionError
from llmolympic.core.storage import SQLiteStore
from llmolympic.games.creative_writing import CRITERIA, RUBRIC_VERSION, CreativeWriting
from llmolympic.providers import create_provider
from llmolympic.providers.base import ProviderConfigurationError

runner = CliRunner()

_CANDIDATE_ENV = "LLMOLYMPIC_LIVE_JUDGE_CANDIDATES"
_LEGACY_JUDGE_ENV = "LLMOLYMPIC_LIVE_JUDGES"
_SELECTED_JUDGE_COUNT = 3
_MAX_MODEL_CHARS = 256
_MAX_CANDIDATE_ENV_CHARS = (
    MAX_JUDGES * (_MAX_MODEL_CHARS + len("openai:")) + MAX_JUDGES - 1
)
_OPENAI_JUDGE_SPEC_RE = re.compile(
    r"openai:(?P<model>[A-Za-z0-9][A-Za-z0-9._-]*/"
    r"[A-Za-z0-9][A-Za-z0-9._:+-]*)\Z",
    re.ASCII,
)
_DYNAMIC_ROUTER_MODELS = frozenset({"openrouter/free"})
_DYNAMIC_ROUTER_PREFIXES = ("openrouter/auto",)
_SAFE_REASON_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z", re.ASCII)
_PROBE_SUBMISSIONS = (
    "雨停以后，车站的时钟终于想起了自己的名字，也等回了最后一位旅客。",
    "信封在清晨自行打开，里面只有一句话：今天请替未来保留一盏灯。",
)


class _LiveJudgeConfigurationError(ValueError):
    """Candidate input is invalid before any potentially billable call."""


@dataclass(frozen=True, slots=True)
class _ProbeFailure:
    candidate: str
    reason_code: str
    error_type: str

    def as_dict(self) -> dict[str, str]:
        return {
            "candidate": self.candidate,
            "error_type": self.error_type,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class _LiveJudgeSelection:
    candidates: tuple[str, ...]
    selected: tuple[str, ...]
    probed_count: int
    failures: tuple[_ProbeFailure, ...]

    def safe_summary(self) -> dict[str, object]:
        return {
            "candidate_count": len(self.candidates),
            "failures": [failure.as_dict() for failure in self.failures],
            "probed_count": self.probed_count,
            "selected_judges": list(self.selected),
        }


class _LiveJudgeSelectionError(RuntimeError):
    """Three protocol-compatible judges could not be selected safely."""

    def __init__(self, selection: _LiveJudgeSelection) -> None:
        self.selection = selection
        summary = json.dumps(selection.safe_summary(), ensure_ascii=True, sort_keys=True)
        super().__init__(
            f"only {len(selection.selected)} of {_SELECTED_JUDGE_COUNT} required live "
            f"judges passed the protocol probe: {summary}"
        )


_JudgeProbe = Callable[[str, float], Awaitable[None]]


def _redact_live_secrets(value: str) -> str:
    """Redact credentials and endpoints if a provider accidentally echoes them."""

    redacted = value
    for environment_name in ("OPENAI_API_KEY", "OPENAI_BASE_URL"):
        sensitive_value = os.environ.get(environment_name, "")
        if sensitive_value:
            redacted = redacted.replace(sensitive_value, "[REDACTED]")
    return redacted


def _parse_live_judge_candidates(raw: str, *, source: str) -> list[str]:
    """Parse a bounded, unique list without normalizing unsafe input into validity."""

    if len(raw) > _MAX_CANDIDATE_ENV_CHARS:
        raise _LiveJudgeConfigurationError(f"{source} is too long")
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise _LiveJudgeConfigurationError(f"{source} cannot contain control characters")

    candidates = raw.split(",")
    if any(not item for item in candidates) or not MIN_JUDGES <= len(candidates) <= MAX_JUDGES:
        raise _LiveJudgeConfigurationError(
            f"{source} must contain {MIN_JUDGES}-{MAX_JUDGES} non-empty comma-separated "
            "judge candidates"
        )

    identities: set[str] = set()
    for candidate in candidates:
        match = _OPENAI_JUDGE_SPEC_RE.fullmatch(candidate)
        if match is None or len(match.group("model")) > _MAX_MODEL_CHARS:
            raise _LiveJudgeConfigurationError(
                f"{source} accepts only explicit ASCII openai:<vendor>/<model> specs"
            )
        model_identity = match.group("model").casefold()
        if model_identity in _DYNAMIC_ROUTER_MODELS or model_identity.startswith(
            _DYNAMIC_ROUTER_PREFIXES
        ):
            raise _LiveJudgeConfigurationError(
                f"{source} cannot use dynamic OpenRouter routing aliases"
            )
        if model_identity in identities:
            raise _LiveJudgeConfigurationError(f"{source} candidates must be unique")
        identities.add(model_identity)
    return candidates


def _live_judge_candidates() -> list[str]:
    raw = os.environ.get(_CANDIDATE_ENV)
    source = _CANDIDATE_ENV
    if raw is None:
        raw = os.environ.get(_LEGACY_JUDGE_ENV)
        source = _LEGACY_JUDGE_ENV
    if raw is None:
        pytest.fail(
            f"LLMOLYMPIC_RUN_LIVE=1 requires {_CANDIDATE_ENV} with "
            f"{MIN_JUDGES}-{MAX_JUDGES} comma-separated judge candidates "
            f"(legacy fallback: {_LEGACY_JUDGE_ENV})",
            pytrace=False,
        )
    try:
        return _parse_live_judge_candidates(raw, source=source)
    except _LiveJudgeConfigurationError as exc:
        pytest.fail(str(exc), pytrace=False)


def _creative_probe_request() -> tuple[JudgingRequest, str]:
    game = CreativeWriting()
    players = ["probe-alpha", "probe-beta"]
    state = game.new_state(players, seed=20260806)
    for player, submission in zip(players, _PROBE_SUBMISSIONS, strict=True):
        game.apply_move(state, player, submission)
    request = game.judging_request(state)
    if request.criteria != CRITERIA or request.rubric_version != RUBRIC_VERSION:
        raise AssertionError("creative-writing probe drifted from the production rubric")
    return request, request.submissions[players[1]]


async def _probe_live_judge(candidate: str, timeout: float) -> None:
    _, _, model = candidate.partition(":")
    judge = LLMPlayer(
        name=candidate,
        provider=create_provider("openai", model),
        model=model,
        move_timeout_seconds=timeout,
    )
    request, submission = _creative_probe_request()
    # B is intentional: it rejects models that hard-code the first anonymous label.
    await score_judge_submission(judge, request, "B", submission)


def _safe_probe_failure(candidate: str, error: Exception) -> _ProbeFailure:
    if isinstance(error, JudgePanelError):
        raw_reason_code = error.reason_code
        error_type = "JudgePanelError"
    elif isinstance(error, PlayerActionError):
        raw_reason_code = error.reason_code
        error_type = type(error).__name__
    elif isinstance(error, ProviderConfigurationError):
        raw_reason_code = "provider_configuration_error"
        error_type = "ProviderConfigurationError"
    else:
        raw_reason_code = "judge_probe_failed"
        error_type = "UnexpectedProbeError"
    reason_code = (
        raw_reason_code
        if isinstance(raw_reason_code, str) and _SAFE_REASON_CODE_RE.fullmatch(raw_reason_code)
        else "judge_probe_failed"
    )
    return _ProbeFailure(
        candidate=candidate,
        reason_code=reason_code,
        error_type=error_type,
    )


async def _select_live_judges(
    candidates: list[str],
    timeout: float,
    *,
    probe: _JudgeProbe = _probe_live_judge,
) -> _LiveJudgeSelection:
    selected: list[str] = []
    failures: list[_ProbeFailure] = []
    probed_count = 0
    for candidate in candidates:
        probed_count += 1
        try:
            await probe(candidate, timeout)
        except Exception as exc:  # noqa: BLE001 - provider failures need a safe summary
            failures.append(_safe_probe_failure(candidate, exc))
        else:
            selected.append(candidate)
            if len(selected) == _SELECTED_JUDGE_COUNT:
                break

    selection = _LiveJudgeSelection(
        candidates=tuple(candidates),
        selected=tuple(selected),
        probed_count=probed_count,
        failures=tuple(failures),
    )
    if len(selected) < _SELECTED_JUDGE_COUNT:
        raise _LiveJudgeSelectionError(selection)
    return selection


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


@pytest.mark.parametrize(
    "raw, message",
    [
        ("openai:a/one,openai:b/two", "must contain 3-9"),
        (",".join(f"openai:v/model-{index}" for index in range(10)), "must contain 3-9"),
        ("openai:a/one,openai:A/ONE,openai:c/three", "must be unique"),
        ("mock:a/one,openai:b/two,openai:c/three", "only explicit ASCII"),
        ("openai:a,openai:b/two,openai:c/three", "only explicit ASCII"),
        ("openai:a/mödel,openai:b/two,openai:c/three", "only explicit ASCII"),
        ("openai:a/one, openai:b/two,openai:c/three", "only explicit ASCII"),
        ("openai:a/one\n,openai:b/two,openai:c/three", "control characters"),
        (
            f"openai:a/{'x' * _MAX_MODEL_CHARS},openai:b/two,openai:c/three",
            "only explicit ASCII",
        ),
        ("openai:openrouter/auto,openai:b/two,openai:c/three", "dynamic OpenRouter"),
        ("openai:openrouter/auto-beta,openai:b/two,openai:c/three", "dynamic OpenRouter"),
    ],
)
def test_live_provider_candidate_parser_rejects_unsafe_inputs(
    raw: str,
    message: str,
) -> None:
    with pytest.raises(_LiveJudgeConfigurationError, match=message):
        _parse_live_judge_candidates(raw, source=_CANDIDATE_ENV)


def test_live_provider_candidates_prefer_new_env_and_fall_back_to_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = "openai:old/a,openai:old/b,openai:old/c"
    current = "openai:new/a,openai:new/b,openai:new/c"
    monkeypatch.setenv(_LEGACY_JUDGE_ENV, legacy)
    monkeypatch.setenv(_CANDIDATE_ENV, current)

    assert _live_judge_candidates() == [
        "openai:new/a",
        "openai:new/b",
        "openai:new/c",
    ]

    monkeypatch.delenv(_CANDIDATE_ENV)
    assert _live_judge_candidates() == legacy.split(",")


def test_live_provider_selection_stops_after_first_three_successes() -> None:
    candidates = [f"openai:vendor/model-{index}" for index in range(5)]
    calls: list[tuple[str, float]] = []

    async def successful_probe(candidate: str, timeout: float) -> None:
        calls.append((candidate, timeout))

    selection = asyncio.run(
        _select_live_judges(candidates, 17.0, probe=successful_probe)
    )

    assert selection.selected == tuple(candidates[:3])
    assert selection.probed_count == 3
    assert calls == [(candidate, 17.0) for candidate in candidates[:3]]
    assert selection.safe_summary()["failures"] == []


def test_live_provider_selection_skips_failures_without_leaking_details() -> None:
    candidates = [f"openai:vendor/model-{index}" for index in range(5)]
    sensitive_marker = "sk-secret-must-not-escape"
    calls: list[str] = []

    async def mixed_probe(candidate: str, timeout: float) -> None:
        del timeout
        calls.append(candidate)
        if candidate == candidates[0]:
            raise JudgePanelError(
                f"invalid response included {sensitive_marker}",
                reason_code="invalid_judge_response",
            )
        if candidate == candidates[2]:
            raise RuntimeError(f"provider endpoint and {sensitive_marker}")

    selection = asyncio.run(_select_live_judges(candidates, 17.0, probe=mixed_probe))
    encoded = json.dumps(selection.safe_summary(), sort_keys=True)

    assert selection.selected == (candidates[1], candidates[3], candidates[4])
    assert calls == candidates
    assert [failure.reason_code for failure in selection.failures] == [
        "invalid_judge_response",
        "judge_probe_failed",
    ]
    assert sensitive_marker not in encoded
    assert "provider endpoint" not in encoded


def test_live_provider_selection_fails_safely_when_fewer_than_three_work() -> None:
    candidates = [f"openai:vendor/model-{index}" for index in range(3)]
    sensitive_marker = "sk-never-log-this"

    async def failing_probe(candidate: str, timeout: float) -> None:
        del candidate, timeout
        raise RuntimeError(f"raw provider response {sensitive_marker}")

    with pytest.raises(_LiveJudgeSelectionError) as raised:
        asyncio.run(_select_live_judges(candidates, 17.0, probe=failing_probe))

    assert raised.value.selection.probed_count == len(candidates)
    assert raised.value.selection.selected == ()
    assert sensitive_marker not in str(raised.value)
    assert "raw provider response" not in str(raised.value)


def test_live_provider_probe_uses_production_rubric_and_label_b(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def capture_score(
        judge: LLMPlayer,
        request: JudgingRequest,
        label: str,
        submission: str,
    ) -> tuple[dict[str, float], str]:
        captured.update(
            judge=judge,
            request=request,
            label=label,
            submission=submission,
        )
        return dict.fromkeys(request.criteria, 8.0), "ok"

    class _ProbeProvider:
        name = "openai"

        async def achat(self, messages: list[dict], *, model: str, **params) -> str:
            raise AssertionError("the score helper is replaced in this offline test")

    module = sys.modules[__name__]
    monkeypatch.setattr(module, "create_provider", lambda kind, model: _ProbeProvider())
    monkeypatch.setattr(module, "score_judge_submission", capture_score)

    asyncio.run(_probe_live_judge("openai:vendor/model", 17.0))

    request = captured["request"]
    assert isinstance(request, JudgingRequest)
    assert request.criteria == CRITERIA
    assert request.rubric_version == RUBRIC_VERSION
    assert captured["label"] == "B"
    assert captured["submission"] in _PROBE_SUBMISSIONS


def test_live_provider_config_requires_environment_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(pytest.fail.Exception, match="requires OPENAI_API_KEY"):
        _openai_api_key()


def test_live_provider_diagnostics_redact_key_and_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_marker = "sk-sensitive-marker"
    endpoint_marker = "https://sensitive-provider.example/v1"
    monkeypatch.setenv("OPENAI_API_KEY", key_marker)
    monkeypatch.setenv("OPENAI_BASE_URL", endpoint_marker)

    redacted = _redact_live_secrets(f"key={key_marker} endpoint={endpoint_marker}")

    assert key_marker not in redacted
    assert endpoint_marker not in redacted
    assert redacted.count("[REDACTED]") == 2


@pytest.mark.live_provider
def test_live_creative_provider_panel_round_trip(tmp_path) -> None:
    """Run one billable panel, then verify adjudication, storage, ELO, and secrecy."""

    if os.environ.get("LLMOLYMPIC_RUN_LIVE") != "1":
        pytest.skip("set LLMOLYMPIC_RUN_LIVE=1 to enable real provider calls")

    candidates = _live_judge_candidates()
    api_key = _openai_api_key()
    timeout = _live_timeout()
    try:
        selection = asyncio.run(_select_live_judges(candidates, timeout))
    except _LiveJudgeSelectionError as exc:
        pytest.fail(str(exc), pytrace=False)
    judges = list(selection.selected)
    print(
        "LIVE_PROVIDER_SELECTION "
        + json.dumps(selection.safe_summary(), ensure_ascii=True, sort_keys=True)
    )
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
    base_url = os.environ.get("OPENAI_BASE_URL", "")
    if base_url and base_url in result.output:
        pytest.fail("CLI output exposed OPENAI_BASE_URL; value redacted", pytrace=False)
    if result.exit_code != 0:
        safe_output = _redact_live_secrets(result.output)
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
