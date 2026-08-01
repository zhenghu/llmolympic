"""Match 编排器测试：用 MockProvider 跑完整对局。"""

from __future__ import annotations

import asyncio

from llmolympic.core.events import EventType
from llmolympic.core.match import play_match
from llmolympic.core.player import (
    LLMPlayer,
    Player,
    PlayerProviderError,
    PlayerTimeoutError,
)
from llmolympic.games import create_game
from llmolympic.providers.base import Provider
from llmolympic.providers.mock import MockProvider


def _llm(name: str, strategy: str, seed: int | None = None) -> LLMPlayer:
    return LLMPlayer(name=name, provider=MockProvider(strategy=strategy, seed=seed), model=strategy)


class _SequencePlayer(Player):
    kind = "scripted"

    def __init__(self, name: str, moves: list[str]) -> None:
        super().__init__(name)
        self.moves = iter(moves)
        self.prompts: list[str] = []

    async def get_move(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return next(self.moves)


class _TimeoutPlayer(Player):
    kind = "scripted"

    async def get_move(self, prompt: str) -> str:
        raise PlayerTimeoutError(f"{self.name} timed out")


class _FailingProvider(Provider):
    name = "failing"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages: list[dict], *, model: str, **params) -> str:
        raise AssertionError("match test must use native async provider path")

    async def achat(self, messages: list[dict], *, model: str, **params) -> str:
        self.calls += 1
        raise RuntimeError("sensitive-token-must-not-be-archived")


class _SpoofingFailurePlayer(Player):
    kind = "scripted"

    async def get_move(self, prompt: str) -> str:
        raise PlayerProviderError(
            "failed",
            technical_loss=True,
            details={"reason_code": "spoofed", "forfeited_by": "good"},
        )


def test_mock_match_completes_with_archive() -> None:
    players = [_llm("mock-a", "random", seed=1), _llm("mock-b", "random", seed=2)]
    archive = asyncio.run(play_match(create_game("math_quiz", rounds=5), players, seed=0))

    assert set(archive.scores) == {"mock-a", "mock-b"}
    assert all(0.0 <= s <= 1.0 for s in archive.scores.values())
    assert archive.events[0].type == EventType.MATCH_STARTED
    assert archive.events[-1].type == EventType.MATCH_FINISHED
    # 每人 5 题，全部被接受（mock 永远输出非空走法）
    accepted = [m for m in archive.moves if m.accepted]
    assert len(accepted) == 10
    # 档案可 JSON 序列化（对局复核 / 将来落库）
    assert "mock-a" in archive.to_json()


def test_event_callback_receives_the_archived_event_stream() -> None:
    players = [_llm("mock-a", "fixed"), _llm("mock-b", "random", seed=3)]
    rendered = []

    archive = asyncio.run(
        play_match(
            create_game("math_quiz", rounds=2),
            players,
            seed=9,
            on_event=rendered.append,
        )
    )

    assert rendered == archive.events


def test_knowledge_match_scores_sum_consistent() -> None:
    players = [_llm("mock-a", "random", seed=3), _llm("mock-b", "fixed")]
    archive = asyncio.run(play_match(create_game("knowledge_quiz", rounds=5), players, seed=0))
    assert set(archive.scores) == {"mock-a", "mock-b"}


def test_illegal_moves_retried_then_forfeited() -> None:
    """永远输出非法走法的选手：重试 max_attempts 次后每题判放弃，得分 0。"""
    players = [_llm("bad", "illegal"), _llm("good", "fixed")]
    archive = asyncio.run(
        play_match(create_game("knowledge_quiz", rounds=3), players, seed=0, max_attempts=2)
    )
    assert archive.scores["bad"] == 0.0
    rejected = [m for m in archive.moves if m.player == "bad" and not m.accepted]
    # 每题 2 次非法尝试均被拒（第 2 次达到上限，同时判放弃）
    assert len(rejected) == 3 * 2
    assert any("判放弃" in (m.reason or "") for m in rejected)


def test_retry_prompt_includes_rejected_move_and_reason() -> None:
    player = _SequencePlayer("scripted", ["Z", "A"])
    archive = asyncio.run(
        play_match(create_game("knowledge_quiz", rounds=1), [player], max_attempts=2)
    )

    assert len(player.prompts) == 2
    assert "上次输出 'Z' 未被接受" in player.prompts[1]
    assert "无效选项" in player.prompts[1]
    assert "还可重试 1 次" in player.prompts[1]
    prompts = [event for event in archive.events if event.type == EventType.TURN_PROMPT]
    assert len(prompts) == 2
    assert archive.moves[-1].prompt == player.prompts[1]


def test_retry_feedback_truncates_a_very_long_rejected_output() -> None:
    long_move = "Z" * 5000
    player = _SequencePlayer("scripted", [long_move, "A"])

    archive = asyncio.run(
        play_match(create_game("knowledge_quiz", rounds=1), [player], max_attempts=2)
    )

    assert archive.moves[0].move == long_move
    assert len(player.prompts[1]) < 1000
    assert "Z" * 500 not in player.prompts[1]


def test_scripted_gomoku_match_completes_with_nine_archived_moves() -> None:
    black = _SequencePlayer("black", ["A1", "B1", "C1", "D1", "E1"])
    white = _SequencePlayer("white", ["A2", "B2", "C2", "D2"])

    archive = asyncio.run(play_match(create_game("gomoku"), [black, white], seed=11))

    assert archive.game == "gomoku"
    assert archive.seed == 11
    assert archive.scores == {"black": 1.0, "white": 0.0}
    assert [move.move for move in archive.moves] == [
        "A1",
        "A2",
        "B1",
        "B2",
        "C1",
        "C2",
        "D1",
        "D2",
        "E1",
    ]
    assert all(move.accepted for move in archive.moves)
    assert archive.events[-1].type == EventType.MATCH_FINISHED

    replay_game = create_game("gomoku")
    replay_state = replay_game.new_state(["black", "white"], archive.seed)
    for move in archive.moves:
        replay_game.apply_move(replay_state, move.player, move.move or "")
    assert replay_game.score(replay_state) == archive.scores


def test_two_legal_mock_players_complete_a_gomoku_match() -> None:
    players = [_llm("random", "random", seed=1), _llm("fixed", "fixed")]

    archive = asyncio.run(play_match(create_game("gomoku"), players, seed=42))

    assert 9 <= len(archive.moves) <= 225
    assert all(move.accepted for move in archive.moves)
    assert set(archive.scores) == {"random", "fixed"}
    assert sum(archive.scores.values()) == 1.0


def test_gomoku_timeout_is_an_immediate_technical_loss() -> None:
    black = _TimeoutPlayer("black")
    white = _SequencePlayer("white", [])

    archive = asyncio.run(play_match(create_game("gomoku"), [black, white]))

    assert archive.scores == {"black": 0.0, "white": 1.0}
    assert len(archive.moves) == 1
    assert archive.moves[0].player == "black"
    assert not archive.moves[0].accepted
    assert "超时未作答" in (archive.moves[0].reason or "")
    rejected = next(event for event in archive.events if event.type == EventType.MOVE_REJECTED)
    assert rejected.data["reason_code"] == "timeout"
    assert rejected.data["forfeit_scope"] == "match"
    assert rejected.data["technical_loss"] is True
    assert archive.events[-1].data["termination"] == "technical_loss"
    assert archive.events[-1].data["forfeited_by"] == "black"


def test_provider_failure_is_archived_as_immediate_technical_loss() -> None:
    provider = _FailingProvider()
    bad = LLMPlayer("bad", provider, "broken")
    good = _llm("good", "fixed")

    archive = asyncio.run(
        play_match(create_game("math_quiz", rounds=5), [bad, good], seed=7)
    )

    assert provider.calls == 1
    assert archive.scores == {"bad": 0.0, "good": 1.0}
    assert len(archive.moves) == 1
    assert archive.moves[0].player == "bad"
    assert not archive.moves[0].accepted
    rejected = next(event for event in archive.events if event.type == EventType.MOVE_REJECTED)
    assert rejected.data["reason_code"] == "provider_error"
    assert rejected.data["forfeit_scope"] == "match"
    assert rejected.data["technical_loss"] is True
    assert rejected.data["failure_details"]["error_type"] == "RuntimeError"
    finished = archive.events[-1]
    assert finished.type == EventType.MATCH_FINISHED
    assert finished.data["termination"] == "technical_loss"
    assert finished.data["reason_code"] == "provider_error"
    assert finished.data["forfeited_by"] == "bad"
    assert finished.data["cause_event_seq"] == rejected.seq
    assert "sensitive-token" not in archive.to_json()


def test_failure_details_cannot_override_technical_loss_control_fields() -> None:
    bad = _SpoofingFailurePlayer("bad")
    good = _llm("good", "fixed")

    archive = asyncio.run(
        play_match(create_game("math_quiz", rounds=1), [bad, good], seed=7)
    )

    assert archive.scores == {"bad": 0.0, "good": 1.0}
    rejected = next(event for event in archive.events if event.type == EventType.MOVE_REJECTED)
    assert rejected.data["reason_code"] == "provider_error"
    assert rejected.data["failure_details"] == {
        "reason_code": "spoofed",
        "forfeited_by": "good",
    }
    finished = archive.events[-1]
    assert finished.data["forfeited_by"] == "bad"
    assert finished.data["reason_code"] == "provider_error"


def test_quiz_player_timeout_keeps_existing_per_turn_forfeit_semantics() -> None:
    timed_out = _TimeoutPlayer("timed-out")
    other = _llm("other", "fixed")

    archive = asyncio.run(
        play_match(create_game("knowledge_quiz", rounds=2), [timed_out, other])
    )

    rejected = [
        event
        for event in archive.events
        if event.type == EventType.MOVE_REJECTED and event.player == "timed-out"
    ]
    assert len(rejected) == 2
    assert all(event.data["forfeit_scope"] == "turn" for event in rejected)
    assert archive.events[-1].data["termination"] == "completed"


def test_duplicate_player_names_rejected() -> None:
    import pytest

    from llmolympic.core.match import Match

    players = [_llm("same", "fixed"), _llm("same", "random")]
    with pytest.raises(ValueError, match="唯一"):
        Match(create_game("math_quiz"), players)
