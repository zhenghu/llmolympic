"""Anonymous LLM judge-panel protocol, quorum, and aggregation tests."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping

import pytest

from llmolympic.core.judge import (
    JUDGE_SYSTEM_PROMPT,
    JudgePanelError,
    JudgingRequest,
    LLMJudgePanel,
    _judge_prompt,
    _parse_response,
)
from llmolympic.core.player import HumanPlayer, LLMPlayer
from llmolympic.providers.base import Provider

_JUDGE_INPUT_RE = re.compile(r"<judge-input>(.*?)</judge-input>", re.DOTALL)


def _judge_input(messages: list[dict]) -> dict:
    assert len(messages) == 2
    assert messages[0] == {"role": "system", "content": JUDGE_SYSTEM_PROMPT}
    prompt = messages[1]["content"]
    assert isinstance(prompt, str)
    match = _JUDGE_INPUT_RE.search(prompt)
    assert match is not None
    payload = json.loads(match.group(1))
    assert isinstance(payload, dict)
    return payload


class _ScriptedJudgeProvider(Provider):
    """Return protocol-correct scores selected by the anonymous submission text."""

    def __init__(
        self,
        name: str,
        scores_by_submission: Mapping[str, Mapping[str, float]],
        *,
        fail_on: frozenset[str] = frozenset(),
    ) -> None:
        self.name = name
        self.scores_by_submission = {
            submission: dict(scores) for submission, scores in scores_by_submission.items()
        }
        self.fail_on = fail_on
        self.calls: list[list[dict]] = []

    def chat(self, messages: list[dict], *, model: str, **params) -> str:
        raise AssertionError("judge tests must use the native async provider path")

    async def achat(self, messages: list[dict], *, model: str, **params) -> str:
        self.calls.append(messages)
        payload = _judge_input(messages)
        submissions = payload["submissions"]
        assert isinstance(submissions, dict)
        assert len(submissions) == 1
        label, submission = next(iter(submissions.items()))
        if submission in self.fail_on:
            raise RuntimeError("sensitive-provider-detail-must-not-escape")
        return json.dumps(
            {
                "scores": {label: self.scores_by_submission[submission]},
                "rationales": {label: f"assessment for {submission}"},
            },
            ensure_ascii=False,
        )


class _CancellationTracker:
    def __init__(self, expected_starts: int) -> None:
        self.expected_starts = expected_starts
        self.started = 0
        self.cancelled = 0
        self.all_started = asyncio.Event()


class _BlockingJudgeProvider(Provider):
    def __init__(self, name: str, tracker: _CancellationTracker) -> None:
        self.name = name
        self.tracker = tracker

    def chat(self, messages: list[dict], *, model: str, **params) -> str:
        raise AssertionError("cancellation test must use the native async provider path")

    async def achat(self, messages: list[dict], *, model: str, **params) -> str:
        self.tracker.started += 1
        if self.tracker.started == self.tracker.expected_starts:
            self.tracker.all_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.tracker.cancelled += 1
            raise
        raise AssertionError("unreachable")


def _judge(
    judge_id: str,
    provider: Provider,
    *,
    model: str | None = None,
) -> LLMPlayer:
    return LLMPlayer(
        name=f"judge-name-{judge_id}",
        provider=provider,
        model=model or f"judge-model-{judge_id}",
        entrant_id=judge_id,
        move_timeout_seconds=None,
    )


def _request(
    *,
    submissions: dict[str, str] | None = None,
    criteria: dict[str, float] | None = None,
) -> JudgingRequest:
    return JudgingRequest(
        task="Write one compact image-rich sentence.",
        criteria=criteria or {"originality": 3.0, "clarity": 1.0},
        submissions=submissions or {"Alpha": "WORK_ALPHA", "Beta": "WORK_BETA"},
        rubric_version="creative-writing-v1",
    )


@pytest.mark.parametrize(
    "response",
    [
        "not-json",
        '{"scores":{"A":{"originality":NaN,"clarity":1}},"rationales":{"A":"reason"}}',
        '{"scores":{"A":{"originality":Infinity,"clarity":1}},"rationales":{"A":"reason"}}',
        '{"scores":{"A":{"originality":true,"clarity":1}},"rationales":{"A":"reason"}}',
        '{"scores":{"A":{"originality":1,"clarity":1}}}',
        '{"scores":{"A":{"originality":1,"clarity":1}},"rationales":{"A":"reason"},"extra":1}',
        '{"scores":{"A":{"originality":1}},"rationales":{"A":"reason"}}',
        '{"scores":{"A":{"originality":1,"clarity":1,"extra":1}},"rationales":{"A":"reason"}}',
        '{"scores":{"A":{"originality":1,"clarity":1}},"rationales":{}}',
    ],
)
def test_judge_response_rejects_invalid_json_non_finite_bool_and_field_drift(
    response: str,
) -> None:
    with pytest.raises(JudgePanelError) as raised:
        _parse_response(response, label="A", criteria=["originality", "clarity"])

    assert raised.value.reason_code == "invalid_judge_response"


def test_judge_response_accepts_only_the_exact_protocol_shape() -> None:
    scores, rationale = _parse_response(
        '{"scores":{"A":{"originality":7,"clarity":8.5}},"rationales":{"A":"bounded reason"}}',
        label="A",
        criteria=["originality", "clarity"],
    )

    assert scores == {"originality": 7.0, "clarity": 8.5}
    assert rationale == "bounded reason"


def test_judge_prompt_uses_the_actual_anonymous_label_in_response_schema() -> None:
    prompt = _judge_prompt(
        task="task",
        criteria={"originality": 1.0, "clarity": 1.0},
        label="B",
        submission="untrusted work",
        rubric_version="v1",
    )
    instructions = prompt.split("<judge-input>", maxsplit=1)[0]

    assert '"scores":{"B":' in instructions
    assert '"rationales":{"B":' in instructions
    assert '"scores":{"A":' not in instructions


def test_one_incomplete_judge_is_excluded_but_majority_quorum_still_aggregates() -> None:
    scores = {
        "WORK_ALPHA": {"originality": 8.0, "clarity": 4.0},
        "WORK_BETA": {"originality": 4.0, "clarity": 8.0},
    }
    providers = [
        _ScriptedJudgeProvider("judge-provider-1", scores),
        _ScriptedJudgeProvider("judge-provider-2", scores),
        _ScriptedJudgeProvider(
            "judge-provider-failing",
            scores,
            fail_on=frozenset({"WORK_BETA"}),
        ),
    ]
    panel = LLMJudgePanel(
        [_judge(f"judge:{index}", provider) for index, provider in enumerate(providers, start=1)]
    )

    verdict = asyncio.run(panel.adjudicate(_request(), seed=17))

    assert verdict.quorum == 2
    assert verdict.successful_judges == 2
    assert verdict.scores == {"Alpha": 0.7, "Beta": 0.5}
    assert [failure.judge.judge_id for failure in verdict.failures] == ["judge:3"]
    assert verdict.failures[0].reason_code == "provider_error"
    assert verdict.failures[0].error_type == "PlayerProviderError"
    assert "sensitive-provider-detail" not in verdict.model_dump_json()


def test_two_failed_judges_cannot_form_quorum() -> None:
    scores = {
        "WORK_ALPHA": {"originality": 8.0, "clarity": 4.0},
        "WORK_BETA": {"originality": 4.0, "clarity": 8.0},
    }
    providers = [
        _ScriptedJudgeProvider("judge-provider-good", scores),
        _ScriptedJudgeProvider(
            "judge-provider-failing-1", scores, fail_on=frozenset({"WORK_ALPHA"})
        ),
        _ScriptedJudgeProvider(
            "judge-provider-failing-2", scores, fail_on=frozenset({"WORK_BETA"})
        ),
    ]
    panel = LLMJudgePanel(
        [_judge(f"judge:{index}", provider) for index, provider in enumerate(providers, start=1)]
    )

    with pytest.raises(JudgePanelError) as raised:
        asyncio.run(panel.adjudicate(_request(), seed=17))

    assert raised.value.reason_code == "judge_quorum_not_met"
    assert "1" in str(raised.value)
    assert "2" in str(raised.value)
    assert len(raised.value.failures) == 2
    assert {failure["reason_code"] for failure in raised.value.failures} == {"provider_error"}
    assert "sensitive-provider-detail" not in repr(raised.value.failures)


def test_each_judge_call_contains_exactly_one_anonymous_submission() -> None:
    contestant_provider_a = _ScriptedJudgeProvider("CONTESTANT_PROVIDER_ALPHA", {})
    contestant_provider_b = _ScriptedJudgeProvider("CONTESTANT_PROVIDER_BETA", {})
    contestants = [
        LLMPlayer(
            "CONTESTANT_NAME_ALPHA",
            contestant_provider_a,
            "CONTESTANT_MODEL_ALPHA",
            entrant_id="contestant:entrant-alpha",
            move_timeout_seconds=None,
        ),
        LLMPlayer(
            "CONTESTANT_NAME_BETA",
            contestant_provider_b,
            "CONTESTANT_MODEL_BETA",
            entrant_id="contestant:entrant-beta",
            move_timeout_seconds=None,
        ),
    ]
    scores = {
        "ONLY_WORK_ALPHA": {"originality": 7.0, "clarity": 6.0},
        "ONLY_WORK_BETA": {"originality": 6.0, "clarity": 7.0},
    }
    judge_providers = [
        _ScriptedJudgeProvider(f"judge-provider-{index}", scores) for index in range(3)
    ]
    panel = LLMJudgePanel(
        [_judge(f"judge:{index}", provider) for index, provider in enumerate(judge_providers)]
    )
    panel.validate_contestants(contestants)

    verdict = asyncio.run(
        panel.adjudicate(
            _request(
                submissions={
                    contestants[0].name: "ONLY_WORK_ALPHA",
                    contestants[1].name: "ONLY_WORK_BETA",
                }
            ),
            seed=991,
        )
    )

    forbidden = {
        contestants[0].name,
        contestants[1].name,
        contestants[0].entrant_id,
        contestants[1].entrant_id,
        contestant_provider_a.name,
        contestant_provider_b.name,
        contestants[0].model,
        contestants[1].model,
    }
    calls = [call for provider in judge_providers for call in provider.calls]
    assert len(calls) == 6
    for messages in calls:
        serialized = json.dumps(messages, ensure_ascii=False)
        assert all(secret not in serialized for secret in forbidden)
        payload = _judge_input(messages)
        submissions = payload["submissions"]
        assert len(submissions) == 1
        assert set(submissions).issubset({"A", "B"})
        one_work = next(iter(submissions.values()))
        assert one_work in {"ONLY_WORK_ALPHA", "ONLY_WORK_BETA"}
        assert ("ONLY_WORK_ALPHA" in serialized) != ("ONLY_WORK_BETA" in serialized)

    assert set(verdict.scores) == {contestants[0].name, contestants[1].name}
    assert all(
        set(item.label_map.values()) == {contestants[0].name, contestants[1].name}
        for item in verdict.verdicts
    )


def test_duplicate_judges_and_contestant_judge_identity_overlap_are_rejected() -> None:
    scores = {
        "WORK_ALPHA": {"originality": 5.0, "clarity": 5.0},
        "WORK_BETA": {"originality": 5.0, "clarity": 5.0},
    }
    first = _judge("judge:duplicate", _ScriptedJudgeProvider("judge-provider-1", scores))
    duplicate = _judge("judge:duplicate", _ScriptedJudgeProvider("judge-provider-2", scores))
    third = _judge("judge:third", _ScriptedJudgeProvider("judge-provider-3", scores))

    with pytest.raises(ValueError, match="评委稳定身份必须唯一"):
        LLMJudgePanel([first, duplicate, third])

    panel = LLMJudgePanel(
        [
            first,
            _judge("judge:second", _ScriptedJudgeProvider("judge-provider-2", scores)),
            third,
        ]
    )
    contestant = LLMPlayer(
        "contestant",
        _ScriptedJudgeProvider("contestant-provider", {}),
        "contestant-model",
        entrant_id="judge:duplicate",
        move_timeout_seconds=None,
    )
    with pytest.raises(ValueError, match="不能同时担任参赛者和评委"):
        panel.validate_contestants([contestant])

    with pytest.raises(TypeError, match="必须是 LLMPlayer"):
        LLMJudgePanel(  # type: ignore[list-item]
            [
                first,
                HumanPlayer("human judge"),
                third,
            ]
        )


def test_weighted_median_and_anonymous_order_are_deterministic() -> None:
    score_sets = [
        {
            "WORK_ALPHA": {"originality": 2.0, "clarity": 10.0},
            "WORK_BETA": {"originality": 1.0, "clarity": 1.0},
        },
        {
            "WORK_ALPHA": {"originality": 8.0, "clarity": 0.0},
            "WORK_BETA": {"originality": 9.0, "clarity": 9.0},
        },
        {
            "WORK_ALPHA": {"originality": 10.0, "clarity": 10.0},
            "WORK_BETA": {"originality": 5.0, "clarity": 5.0},
        },
    ]
    providers = [
        _ScriptedJudgeProvider(f"judge-provider-{index}", scores)
        for index, scores in enumerate(score_sets)
    ]
    panel = LLMJudgePanel(
        [_judge(f"judge:{index}", provider) for index, provider in enumerate(providers)]
    )
    request = _request()

    first = asyncio.run(panel.adjudicate(request, seed=12345))
    second = asyncio.run(panel.adjudicate(request, seed=12345))

    # Alpha judge totals are 0.4, 0.6, 1.0; Beta totals are 0.1, 0.9, 0.5.
    assert first.scores == {"Alpha": 0.6, "Beta": 0.5}
    assert first.model_dump() == second.model_dump()
    assert first.aggregation == "weighted-median-v1"


def test_cancelling_panel_reaps_every_in_flight_judge_call() -> None:
    async def scenario() -> tuple[int, int]:
        tracker = _CancellationTracker(expected_starts=6)
        judges = [
            _judge(
                f"judge:{index}",
                _BlockingJudgeProvider(f"blocking-provider-{index}", tracker),
            )
            for index in range(3)
        ]
        task = asyncio.create_task(LLMJudgePanel(judges).adjudicate(_request(), seed=2))
        await asyncio.wait_for(tracker.all_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return tracker.started, tracker.cancelled

    started, cancelled = asyncio.run(scenario())

    assert started == 6
    assert cancelled == 6
