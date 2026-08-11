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
    PanelVerdict,
    _judge_prompt,
    _parse_response,
    score_judge_submission,
)
from llmolympic.core.player import HumanPlayer, LLMPlayer, UsageReservationProtocol
from llmolympic.core.usage import (
    BudgetExceededError,
    BudgetLimits,
    ProviderBudgetPolicy,
    RouteBudgetPolicy,
    UsageBudget,
    UsageExceedsReservationError,
    UsageTotals,
)
from llmolympic.providers.base import (
    Provider,
    ProviderChatResult,
    ProviderUsage,
    UsageSupport,
)

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


class _RawResponseJudgeProvider(Provider):
    """Return one exact response so public protocol-boundary failures can be tested."""

    def __init__(self, response: str) -> None:
        self.name = "raw-response-judge"
        self.response = response
        self.calls: list[list[dict]] = []

    def chat(self, messages: list[dict], *, model: str, **params) -> str:
        raise AssertionError("judge tests must use the native async provider path")

    async def achat(self, messages: list[dict], *, model: str, **params) -> str:
        self.calls.append(messages)
        return self.response


class _UsageScriptedJudgeProvider(_ScriptedJudgeProvider):
    def __init__(
        self,
        name: str,
        scores_by_submission: Mapping[str, Mapping[str, float]],
        *,
        usage: ProviderUsage | None = None,
    ) -> None:
        super().__init__(name, scores_by_submission)
        self.usage = usage or ProviderUsage(input_tokens=0, output_tokens=0, total_tokens=0)

    def usage_support_for(self, model: str) -> UsageSupport:
        return UsageSupport.REPORTED

    def resolve_output_token_cap(
        self,
        model: str,
        *,
        requested_cap: int | None,
        params: dict[str, object],
    ) -> int | None:
        return requested_cap

    async def achat_with_usage(
        self,
        messages: list[dict],
        *,
        model: str,
        **params,
    ) -> ProviderChatResult:
        return ProviderChatResult(
            text=await self.achat(messages, model=model, **params),
            usage=self.usage,
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


def _bind_judge_budget(
    judges: list[LLMPlayer],
    budget: UsageBudget,
    *,
    max_output_tokens: int = 1024,
) -> ProviderBudgetPolicy:
    policy = ProviderBudgetPolicy(
        max_output_tokens_per_call=max_output_tokens,
        routes=tuple(
            RouteBudgetPolicy(route_id=judge.route_id)
            for judge in sorted(judges, key=lambda judge: judge.route_id)
        ),
    )
    for judge in judges:
        judge.bind_usage_budget(budget, policy)
    return policy


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


def test_score_judge_submission_supports_label_b_with_the_strict_protocol() -> None:
    scores = {"WORK_ALPHA": {"originality": 7.0, "clarity": 8.5}}
    provider = _ScriptedJudgeProvider("judge-provider", scores)
    request = _request(submissions={"Alpha": "WORK_ALPHA"})

    parsed_scores, rationale = asyncio.run(
        score_judge_submission(
            _judge("judge:probe", provider),
            request,
            "B",
            "WORK_ALPHA",
        )
    )

    assert parsed_scores == scores["WORK_ALPHA"]
    assert rationale == "assessment for WORK_ALPHA"
    assert len(provider.calls) == 1
    assert _judge_input(provider.calls[0])["submissions"] == {"B": "WORK_ALPHA"}


def test_score_judge_submission_rejects_non_protocol_wrapped_json() -> None:
    provider = _RawResponseJudgeProvider(
        '```json\n{"scores":{"B":{"originality":7,"clarity":8}},'
        '"rationales":{"B":"reason"}}\n```'
    )

    with pytest.raises(JudgePanelError) as raised:
        asyncio.run(
            score_judge_submission(
                _judge("judge:strict-probe", provider),
                _request(submissions={"Alpha": "WORK_ALPHA"}),
                "B",
                "WORK_ALPHA",
            )
        )

    assert raised.value.reason_code == "invalid_judge_response"
    assert len(provider.calls) == 1


def test_panel_routes_every_submission_through_public_score_boundary(monkeypatch) -> None:
    scores = {
        "WORK_ALPHA": {"originality": 8.0, "clarity": 4.0},
        "WORK_BETA": {"originality": 4.0, "clarity": 8.0},
    }
    judges = [
        _judge(
            f"judge:{index}",
            _ScriptedJudgeProvider(f"judge-provider-{index}", scores),
        )
        for index in range(3)
    ]
    calls: list[tuple[str, str, str]] = []

    async def tracked_score(
        judge: LLMPlayer,
        request: JudgingRequest,
        label: str,
        submission: str,
        *,
        reservation: UsageReservationProtocol | None = None,
    ) -> tuple[dict[str, float], str]:
        calls.append((judge.entrant_id, label, submission))
        return await score_judge_submission(
            judge,
            request,
            label,
            submission,
            reservation=reservation,
        )

    monkeypatch.setattr("llmolympic.core.judge.score_judge_submission", tracked_score)

    verdict = asyncio.run(LLMJudgePanel(judges).adjudicate(_request(), seed=19))

    assert verdict.successful_judges == 3
    assert len(calls) == 6
    for judge in judges:
        judge_calls = [(label, submission) for judge_id, label, submission in calls if judge_id == judge.entrant_id]
        assert {label for label, _ in judge_calls} == {"A", "B"}
        assert {submission for _, submission in judge_calls} == {"WORK_ALPHA", "WORK_BETA"}


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
    assert verdict.schema_version == 3
    assert verdict.request_digest is not None
    assert verdict.successful_judges == 2
    assert verdict.panel is not None
    assert len(verdict.panel) == verdict.panel_size == 3
    assert {judge.judge_id for judge in verdict.panel} == {
        item.judge.judge_id for item in [*verdict.verdicts, *verdict.failures]
    }
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


def test_duplicate_judge_routes_and_contestant_route_overlap_are_rejected() -> None:
    scores = {
        "WORK_ALPHA": {"originality": 5.0, "clarity": 5.0},
        "WORK_BETA": {"originality": 5.0, "clarity": 5.0},
    }
    first = _judge(
        "judge:route-first",
        _ScriptedJudgeProvider("first-provider-name", scores),
        model="shared-route-model",
    )
    same_route = _judge(
        "judge:route-second",
        _ScriptedJudgeProvider("second-provider-name", scores),
        model="shared-route-model",
    )
    third = _judge(
        "judge:route-third",
        _ScriptedJudgeProvider("third-provider-name", scores),
        model="independent-route-model",
    )

    with pytest.raises(ValueError, match="评委路由身份必须唯一"):
        LLMJudgePanel([first, same_route, third])

    panel = LLMJudgePanel(
        [
            first,
            _judge(
                "judge:route-independent-two",
                _ScriptedJudgeProvider("independent-provider-two", scores),
                model="independent-route-model-two",
            ),
            third,
        ]
    )
    contestant = _judge(
        "contestant:different-stable-id",
        _ScriptedJudgeProvider("contestant-provider-name", scores),
        model="shared-route-model",
    )

    with pytest.raises(ValueError, match="同一模型路由不能同时担任"):
        panel.validate_contestants([contestant])

    assert all(not provider.calls for provider in [first.provider, same_route.provider])


def test_panel_verdict_v3_freezes_and_authenticates_the_complete_panel() -> None:
    scores = {
        "WORK_ALPHA": {"originality": 7.0, "clarity": 6.0},
        "WORK_BETA": {"originality": 6.0, "clarity": 7.0},
    }
    panel = LLMJudgePanel(
        [
            _judge(
                f"judge:snapshot-{index}",
                _ScriptedJudgeProvider(f"snapshot-provider-{index}", scores),
            )
            for index in range(3)
        ]
    )

    verdict = asyncio.run(panel.adjudicate(_request(), seed=71))

    assert verdict.schema_version == 3
    assert verdict.request_digest is not None
    assert verdict.panel is not None
    assert len(verdict.panel) == verdict.panel_size == 3
    assert len({judge.judge_id for judge in verdict.panel}) == 3
    assert len({judge.route_id for judge in verdict.panel}) == 3
    assert {judge.judge_id: judge for judge in verdict.panel} == {
        item.judge.judge_id: item.judge for item in verdict.verdicts
    }

    duplicate_route = json.loads(verdict.model_dump_json())
    duplicate_route["panel"][1]["route_id"] = duplicate_route["panel"][0]["route_id"]
    with pytest.raises(ValueError, match="panel 快照包含重复评委路由"):
        PanelVerdict.model_validate(duplicate_route)

    incomplete_panel = json.loads(verdict.model_dump_json())
    incomplete_panel["panel"].pop()
    with pytest.raises(ValueError, match="panel 快照必须覆盖整个评审团"):
        PanelVerdict.model_validate(incomplete_panel)

    mismatched_output = json.loads(verdict.model_dump_json())
    mismatched_output["verdicts"][0]["judge"]["route_id"] = "route:v1:" + "f" * 64
    with pytest.raises(ValueError, match="必须精确匹配 panel 快照"):
        PanelVerdict.model_validate(mismatched_output)


def test_fixed_scores_v3_freezes_panel_without_calling_judges() -> None:
    providers = [
        _ScriptedJudgeProvider(f"fixed-provider-{index}", {}) for index in range(3)
    ]
    panel = LLMJudgePanel(
        [_judge(f"judge:fixed-{index}", provider) for index, provider in enumerate(providers)]
    )
    request = JudgingRequest(
        task="All contestants forfeited.",
        criteria={"originality": 1.0},
        submissions={},
        fixed_scores={"Alpha": 0.0, "Beta": 0.0},
        rubric_version="creative-writing-v1",
    )

    verdict = asyncio.run(panel.adjudicate(request, seed=72))

    assert verdict.schema_version == 3
    assert verdict.request_digest is not None
    assert verdict.aggregation == "fixed-scores-v1"
    assert verdict.panel is not None
    assert len(verdict.panel) == verdict.panel_size == 3
    assert verdict.verdicts == []
    assert verdict.failures == []
    assert all(provider.calls == [] for provider in providers)


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


def test_judge_fanout_reserves_atomically_before_any_provider_call() -> None:
    scores = {
        "WORK_ALPHA": {"originality": 7.0, "clarity": 6.0},
        "WORK_BETA": {"originality": 6.0, "clarity": 7.0},
    }
    providers = [
        _UsageScriptedJudgeProvider(f"usage-provider-{index}", scores)
        for index in range(3)
    ]
    judges = [
        _judge(f"judge:budget-{index}", provider)
        for index, provider in enumerate(providers)
    ]
    budget = UsageBudget(BudgetLimits(calls=5))
    _bind_judge_budget(judges, budget)

    with pytest.raises(BudgetExceededError):
        asyncio.run(LLMJudgePanel(judges).adjudicate(_request(), seed=3))

    assert [len(provider.calls) for provider in providers] == [0, 0, 0]
    assert budget.spent == UsageTotals.zero()
    assert budget.reserved == UsageTotals.zero()


def test_judge_usage_overrun_propagates_as_operator_error_not_panel_failure() -> None:
    scores = {
        "WORK_ALPHA": {"originality": 7.0, "clarity": 6.0},
        "WORK_BETA": {"originality": 6.0, "clarity": 7.0},
    }
    providers = [
        _UsageScriptedJudgeProvider(
            "overrun-provider",
            scores,
            usage=ProviderUsage(
                input_tokens=10_000,
                output_tokens=1,
                total_tokens=10_001,
            ),
        ),
        _UsageScriptedJudgeProvider("usage-provider-1", scores),
        _UsageScriptedJudgeProvider("usage-provider-2", scores),
    ]
    judges = [
        _judge(f"judge:overrun-{index}", provider)
        for index, provider in enumerate(providers)
    ]
    budget = UsageBudget(BudgetLimits())
    _bind_judge_budget(judges, budget, max_output_tokens=8)

    with pytest.raises(UsageExceedsReservationError):
        asyncio.run(LLMJudgePanel(judges).adjudicate(_request(), seed=4))

    assert budget.poisoned
    assert budget.reserved == UsageTotals.zero()
    # The first impossible report poisons the shared ledger before later tasks
    # dispatch; those still-reserved siblings are released by panel cleanup.
    assert sum(len(provider.calls) for provider in providers) == 1


def test_budgeted_panel_cancellation_charges_all_dispatched_calls() -> None:
    async def scenario() -> tuple[_CancellationTracker, UsageBudget]:
        tracker = _CancellationTracker(expected_starts=6)
        judges = [
            _judge(
                f"judge:budget-cancel-{index}",
                _BlockingJudgeProvider(f"blocking-provider-{index}", tracker),
            )
            for index in range(3)
        ]
        budget = UsageBudget(BudgetLimits(calls=6))
        _bind_judge_budget(judges, budget)
        task = asyncio.create_task(LLMJudgePanel(judges).adjudicate(_request(), seed=5))
        await asyncio.wait_for(tracker.all_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return tracker, budget

    tracker, budget = asyncio.run(scenario())

    assert tracker.started == tracker.cancelled == 6
    assert budget.spent.calls == 6
    assert budget.reserved == UsageTotals.zero()


def test_judge_task_cancelled_before_first_execution_releases_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scores = {
        "WORK_ALPHA": {"originality": 7.0, "clarity": 6.0},
        "WORK_BETA": {"originality": 6.0, "clarity": 7.0},
    }
    providers = [
        _UsageScriptedJudgeProvider(f"usage-provider-{index}", scores)
        for index in range(3)
    ]
    judges = [
        _judge(f"judge:prestart-{index}", provider)
        for index, provider in enumerate(providers)
    ]
    budget = UsageBudget(BudgetLimits(calls=6))
    _bind_judge_budget(judges, budget)
    real_create_task = asyncio.create_task
    created = 0

    def cancel_second_before_run(coroutine):
        nonlocal created
        created += 1
        task = real_create_task(coroutine)
        if created == 2:
            task.cancel()
        return task

    monkeypatch.setattr(
        "llmolympic.core.judge.asyncio.create_task",
        cancel_second_before_run,
    )

    verdict = asyncio.run(LLMJudgePanel(judges).adjudicate(_request(), seed=6))

    assert verdict.successful_judges == 2
    assert [len(provider.calls) for provider in providers] == [1, 2, 2]
    assert budget.spent.calls == 5
    assert budget.reserved == UsageTotals.zero()
