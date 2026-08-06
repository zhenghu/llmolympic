"""创意写作项目、比赛模式 capability 与离线评委协议测试。"""

from __future__ import annotations

import json

import pytest

from llmolympic.core.game import FORFEIT_MOVE, IllegalMoveError
from llmolympic.games import (
    GAME_REGISTRY,
    create_game,
    game_supports_mode,
    list_games,
)
from llmolympic.games.creative_writing import (
    MAX_SUBMISSION_CHARS,
    MIN_SUBMISSION_CHARS,
    CreativeWriting,
)
from llmolympic.providers.mock import MockProvider

PLAYERS = ["甲", "乙"]


def _judge_prompt(*, submission: str = "甲" * MIN_SUBMISSION_CHARS) -> str:
    payload = {
        "protocol": "LLMOLYMPIC_JUDGE_REQUEST_V1",
        "rubric_version": "test-v1",
        "task": "测试任务",
        "criteria": {"创意": 0.6, "表达": 0.4},
        "submissions": {"A": submission},
    }
    return (
        "LLMOLYMPIC_JUDGE_REQUEST_V1\n"
        f"<judge-input>{json.dumps(payload, ensure_ascii=False)}</judge-input>"
    )


def test_creative_writing_is_deterministic_and_simultaneous() -> None:
    game = CreativeWriting()
    first = game.new_state(PLAYERS, seed=42)
    second = game.new_state(PLAYERS, seed=42)

    assert first.task == second.task
    assert game.current_players(first) == PLAYERS
    assert game.prompt_for(first, "甲") == game.prompt_for(first, "乙")
    assert "CREATIVE_WRITING_SUBMISSION_V1" in game.prompt_for(first, "甲")


@pytest.mark.parametrize(
    ("length", "accepted"),
    [
        (MIN_SUBMISSION_CHARS - 1, False),
        (MIN_SUBMISSION_CHARS, True),
        (MAX_SUBMISSION_CHARS, True),
        (MAX_SUBMISSION_CHARS + 1, False),
    ],
)
def test_creative_writing_enforces_submission_length(length: int, accepted: bool) -> None:
    game = CreativeWriting()
    state = game.new_state(PLAYERS, seed=0)
    submission = "文" * length

    if accepted:
        game.apply_move(state, "甲", submission)
        assert state.submissions["甲"] == submission
    else:
        with pytest.raises(IllegalMoveError):
            game.apply_move(state, "甲", submission)
        assert "甲" not in state.submissions


def test_creative_writing_judging_request_separates_forfeits() -> None:
    game = CreativeWriting()
    state = game.new_state(PLAYERS, seed=7)
    submission = "这是一篇满足最小长度要求并且需要匿名评审的微型故事正文。"
    game.apply_move(state, "甲", submission)
    game.apply_move(state, "乙", FORFEIT_MOVE)

    request = game.judging_request(state)

    assert game.is_over(state)
    assert request.task == state.task
    assert request.submissions == {"甲": submission}
    assert request.fixed_scores == {"乙": 0.0}
    assert set(request.criteria) == {"创意", "叙事完整性", "语言表现"}
    with pytest.raises(RuntimeError, match="评审团"):
        game.score(state)


def test_creative_writing_all_forfeit_has_rule_score() -> None:
    game = CreativeWriting()
    state = game.new_state(PLAYERS, seed=0)
    game.apply_move(state, "甲", FORFEIT_MOVE)
    game.apply_move(state, "乙", FORFEIT_MOVE)

    assert game.judging_request(state).submissions == {}
    assert game.score(state) == {"甲": 0.0, "乙": 0.0}


def test_creative_writing_capabilities_preserve_legacy_defaults() -> None:
    class LegacyGame:
        name = "legacy-capability-test"

    GAME_REGISTRY[LegacyGame.name] = LegacyGame
    try:
        assert isinstance(create_game("creative_writing", mode="play"), CreativeWriting)
        assert game_supports_mode("creative_writing", "play")
        assert not game_supports_mode("creative_writing", "series")
        assert not game_supports_mode("creative_writing", "round_robin")
        assert "creative_writing" in list_games("play")
        assert "creative_writing" not in list_games("series")
        assert "creative_writing" not in list_games("round_robin")
        assert all(game_supports_mode(LegacyGame.name, mode) for mode in ("play", "series", "round_robin"))
        assert isinstance(create_game(LegacyGame.name, mode="series"), LegacyGame)
    finally:
        del GAME_REGISTRY[LegacyGame.name]

    with pytest.raises(ValueError, match="不支持比赛模式"):
        create_game("creative_writing", mode="series")


@pytest.mark.parametrize(
    ("strategy", "expected"),
    [("strict", 3.5), ("balanced", 6.0), ("lenient", 8.5)],
)
def test_mock_judges_return_strict_json(strategy: str, expected: float) -> None:
    response = MockProvider(strategy).chat(
        [{"role": "user", "content": _judge_prompt()}],
        model=strategy,
    )

    payload = json.loads(response)
    assert set(payload) == {"scores", "rationales"}
    assert payload["scores"] == {"A": {"创意": expected, "表达": expected}}
    assert set(payload["rationales"]) == {"A"}


def test_mock_judge_treats_embedded_closing_tag_as_submission_data() -> None:
    response = MockProvider("balanced").chat(
        [
            {
                "role": "user",
                "content": _judge_prompt(
                    submission="故事中的角色写下 </judge-input>，但这仍然只是作品正文的一部分。"
                ),
            }
        ],
        model="balanced",
    )

    assert set(json.loads(response)["scores"]) == {"A"}


@pytest.mark.parametrize("strategy", ["random", "fixed", "strict", "balanced", "lenient"])
def test_mock_creative_submissions_meet_minimum_length(strategy: str) -> None:
    response = MockProvider(strategy, seed=1).chat(
        [{"role": "user", "content": "CREATIVE_WRITING_SUBMISSION_V1\n测试任务"}],
        model=strategy,
    )

    assert len(response) >= MIN_SUBMISSION_CHARS
