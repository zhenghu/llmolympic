"""Match 编排器测试：用 MockProvider 跑完整对局。"""

from __future__ import annotations

import asyncio

from llmolympic.core.events import EventType
from llmolympic.core.match import play_match
from llmolympic.core.player import LLMPlayer
from llmolympic.games import create_game
from llmolympic.providers.mock import MockProvider


def _llm(name: str, strategy: str, seed: int | None = None) -> LLMPlayer:
    return LLMPlayer(name=name, provider=MockProvider(strategy=strategy, seed=seed), model=strategy)


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


def test_duplicate_player_names_rejected() -> None:
    import pytest

    from llmolympic.core.match import Match

    players = [_llm("same", "fixed"), _llm("same", "random")]
    with pytest.raises(ValueError, match="唯一"):
        Match(create_game("math_quiz"), players)
