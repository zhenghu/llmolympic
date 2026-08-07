"""Match 编排器测试：用 MockProvider 跑完整对局。"""

from __future__ import annotations

import asyncio

import pytest

from llmolympic.core.archive import MatchArchive, MoveRecord, legacy_entrant_id
from llmolympic.core.events import EventType, MatchEvent
from llmolympic.core.game import FORFEIT_MOVE, IllegalMoveError
from llmolympic.core.match import MAX_MOVE_CHARS, Match, play_match
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


class _BlindBatchState:
    def __init__(self, players: list[str], seed: int) -> None:
        self.players = list(players)
        self.seed = seed
        self.moves: dict[str, str] = {}


class _BlindBatchGame:
    """题面显式包含已应用答案数，用于验证同轮状态不泄漏。"""

    name = "blind_batch"
    min_players = 2
    max_players = 2

    def __init__(self) -> None:
        self.state: _BlindBatchState | None = None

    def new_state(self, players: list[str], seed: int) -> _BlindBatchState:
        self.state = _BlindBatchState(players, seed)
        return self.state

    def current_players(self, state: _BlindBatchState) -> list[str]:
        # Deliberately return reverse order: Match must still archive in registration order.
        return [name for name in reversed(state.players) if name not in state.moves]

    def prompt_for(self, state: _BlindBatchState, player: str) -> str:
        return f"{player}:seen={len(state.moves)}"

    def apply_move(self, state: _BlindBatchState, player: str, move: str) -> None:
        if move == "illegal":
            raise IllegalMoveError("非法测试答案")
        state.moves[player] = "" if move == FORFEIT_MOVE else move

    def is_over(self, state: _BlindBatchState) -> bool:
        return len(state.moves) == len(state.players)

    def score(self, state: _BlindBatchState) -> dict[str, float]:
        return {name: float(bool(state.moves[name])) for name in state.players}


class _ThreePlayerBlindBatchGame(_BlindBatchGame):
    min_players = 3
    max_players = 3


class _MatchForfeitBlindBatchGame(_BlindBatchGame):
    forfeit_scope = "match"

    def score(self, state: _BlindBatchState) -> dict[str, float]:
        return {name: 0.0 if state.moves.get(name) == "" else 1.0 for name in state.players}


class _NoCurrentPlayerGame(_BlindBatchGame):
    def current_players(self, state: _BlindBatchState) -> list[str]:
        return []


class _ControlledPlayer(Player):
    kind = "controlled"

    def __init__(
        self,
        name: str,
        *,
        started: asyncio.Event,
        release: asyncio.Event,
        finished: asyncio.Event,
        cancelled: asyncio.Event,
        completion_order: list[str],
        move: str,
    ) -> None:
        super().__init__(name)
        self.started = started
        self.release = release
        self.finished = finished
        self.cancelled = cancelled
        self.completion_order = completion_order
        self.move = move
        self.prompts: list[str] = []

    async def get_move(self, prompt: str) -> str:
        self.prompts.append(prompt)
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        self.completion_order.append(self.name)
        self.finished.set()
        return self.move


class _ControlledTechnicalLossPlayer(Player):
    kind = "controlled_failure"

    def __init__(
        self,
        name: str,
        *,
        started: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        super().__init__(name)
        self.started = started
        self.release = release

    async def get_move(self, prompt: str) -> str:
        self.started.set()
        await self.release.wait()
        raise PlayerProviderError("failed", technical_loss=True)


def test_mock_match_completes_with_archive() -> None:
    players = [_llm("mock-a", "random", seed=1), _llm("mock-b", "random", seed=2)]
    archive = asyncio.run(play_match(create_game("math_quiz", rounds=5), players, seed=0))

    assert players[0].entrant_id != players[1].entrant_id
    assert len({descriptor["entrant_id"] for descriptor in archive.players}) == 2
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


def test_simultaneous_players_are_collected_blindly_and_applied_deterministically() -> None:
    async def scenario() -> tuple[MatchArchive, _ControlledPlayer, _ControlledPlayer, list[str]]:
        game = _BlindBatchGame()
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        first_release = asyncio.Event()
        second_release = asyncio.Event()
        first_finished = asyncio.Event()
        second_finished = asyncio.Event()
        completion_order: list[str] = []
        first = _ControlledPlayer(
            "first",
            started=first_started,
            release=first_release,
            finished=first_finished,
            cancelled=asyncio.Event(),
            completion_order=completion_order,
            move="first-answer",
        )
        second = _ControlledPlayer(
            "second",
            started=second_started,
            release=second_release,
            finished=second_finished,
            cancelled=asyncio.Event(),
            completion_order=completion_order,
            move="second-answer",
        )

        match_task = asyncio.create_task(play_match(game, [first, second]))
        await asyncio.wait_for(
            asyncio.gather(first_started.wait(), second_started.wait()), timeout=2.0
        )

        assert game.state is not None
        assert game.state.moves == {}
        assert first.prompts == ["first:seen=0"]
        assert second.prompts == ["second:seen=0"]

        # The faster second Player finishes first, but no move is applied until
        # every response in the batch has arrived.
        second_release.set()
        await asyncio.wait_for(second_finished.wait(), timeout=2.0)
        assert game.state.moves == {}
        assert not match_task.done()

        first_release.set()
        archive = await asyncio.wait_for(match_task, timeout=2.0)
        return archive, first, second, completion_order

    archive, first, second, completion_order = asyncio.run(scenario())

    assert completion_order == ["second", "first"]
    assert [move.player for move in archive.moves] == ["first", "second"]
    assert [move.move for move in archive.moves] == ["first-answer", "second-answer"]
    turn_events = [
        (event.type, event.player)
        for event in archive.events
        if event.type in (EventType.TURN_PROMPT, EventType.MOVE_RECEIVED)
    ]
    assert turn_events == [
        (EventType.TURN_PROMPT, "first"),
        (EventType.TURN_PROMPT, "second"),
        (EventType.MOVE_RECEIVED, "first"),
        (EventType.MOVE_RECEIVED, "second"),
    ]
    assert first.prompts == ["first:seen=0"]
    assert second.prompts == ["second:seen=0"]


def test_cancelling_a_simultaneous_batch_cancels_every_player_task() -> None:
    async def scenario() -> _BlindBatchGame:
        game = _BlindBatchGame()
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        first_cancelled = asyncio.Event()
        second_cancelled = asyncio.Event()
        never_release = asyncio.Event()
        completion_order: list[str] = []
        first = _ControlledPlayer(
            "first",
            started=first_started,
            release=never_release,
            finished=asyncio.Event(),
            cancelled=first_cancelled,
            completion_order=completion_order,
            move="unused",
        )
        second = _ControlledPlayer(
            "second",
            started=second_started,
            release=never_release,
            finished=asyncio.Event(),
            cancelled=second_cancelled,
            completion_order=completion_order,
            move="unused",
        )

        match_task = asyncio.create_task(play_match(game, [first, second]))
        await asyncio.wait_for(
            asyncio.gather(first_started.wait(), second_started.wait()), timeout=2.0
        )
        match_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await match_task

        assert first_cancelled.is_set()
        assert second_cancelled.is_set()
        assert completion_order == []
        assert game.state is not None
        assert game.state.moves == {}
        return game

    asyncio.run(scenario())


def test_terminal_peer_failure_discards_completed_result_and_cancels_pending() -> None:
    async def scenario() -> tuple[MatchArchive, _ThreePlayerBlindBatchGame, asyncio.Event]:
        game = _ThreePlayerBlindBatchGame()
        pending_started = asyncio.Event()
        pending_cancelled = asyncio.Event()
        never_release = asyncio.Event()
        pending = _ControlledPlayer(
            "pending",
            started=pending_started,
            release=never_release,
            finished=asyncio.Event(),
            cancelled=pending_cancelled,
            completion_order=[],
            move="unused",
        )

        archive = await asyncio.wait_for(
            play_match(
                game,
                [
                    _SequencePlayer("completed", ["A"]),
                    _SpoofingFailurePlayer("failed"),
                    pending,
                ],
            ),
            timeout=2.0,
        )
        assert pending_started.is_set()
        return archive, game, pending_cancelled

    archive, game, pending_cancelled = asyncio.run(scenario())

    assert pending_cancelled.is_set()
    assert game.state is not None
    assert game.state.moves == {"failed": ""}
    assert [move.player for move in archive.moves] == ["failed"]
    assert archive.events[-1].data["forfeited_by"] == "failed"
    assert archive.scores == {"completed": 1.0, "failed": 0.0, "pending": 1.0}


def test_terminal_batch_archive_is_independent_of_peer_completion_timing() -> None:
    async def scenario(peer_finishes_first: bool) -> tuple[MatchArchive, asyncio.Event]:
        peer_started = asyncio.Event()
        peer_release = asyncio.Event()
        peer_finished = asyncio.Event()
        peer_cancelled = asyncio.Event()
        failure_started = asyncio.Event()
        failure_release = asyncio.Event()
        peer = _ControlledPlayer(
            "peer",
            started=peer_started,
            release=peer_release,
            finished=peer_finished,
            cancelled=peer_cancelled,
            completion_order=[],
            move="A",
        )
        failure = _ControlledTechnicalLossPlayer(
            "failed",
            started=failure_started,
            release=failure_release,
        )

        match_task = asyncio.create_task(play_match(_BlindBatchGame(), [peer, failure]))
        await asyncio.wait_for(
            asyncio.gather(peer_started.wait(), failure_started.wait()), timeout=2.0
        )
        if peer_finishes_first:
            peer_release.set()
            await asyncio.wait_for(peer_finished.wait(), timeout=2.0)
            assert not match_task.done()

        failure_release.set()
        archive = await asyncio.wait_for(match_task, timeout=2.0)
        return archive, peer_cancelled

    completed_archive, completed_peer_cancelled = asyncio.run(scenario(True))
    pending_archive, pending_peer_cancelled = asyncio.run(scenario(False))

    def event_signature(archive: MatchArchive) -> list[tuple]:
        return [(event.seq, event.type, event.player, event.data) for event in archive.events]

    assert not completed_peer_cancelled.is_set()
    assert pending_peer_cancelled.is_set()
    assert event_signature(completed_archive) == event_signature(pending_archive)
    assert completed_archive.scores == pending_archive.scores
    assert [move.player for move in completed_archive.moves] == ["failed"]
    assert [move.player for move in pending_archive.moves] == ["failed"]


def test_simultaneous_terminal_failures_use_registration_order_tie_break() -> None:
    async def scenario() -> tuple[MatchArchive, asyncio.Event]:
        pending_cancelled = asyncio.Event()
        pending = _ControlledPlayer(
            "pending",
            started=asyncio.Event(),
            release=asyncio.Event(),
            finished=asyncio.Event(),
            cancelled=pending_cancelled,
            completion_order=[],
            move="unused",
        )
        archive = await asyncio.wait_for(
            play_match(
                _ThreePlayerBlindBatchGame(),
                [
                    _SpoofingFailurePlayer("first-failure"),
                    _SpoofingFailurePlayer("second-failure"),
                    pending,
                ],
            ),
            timeout=2.0,
        )
        return archive, pending_cancelled

    archive, pending_cancelled = asyncio.run(scenario())

    assert pending_cancelled.is_set()
    assert [move.player for move in archive.moves] == ["first-failure"]
    assert archive.events[-1].data["forfeited_by"] == "first-failure"


def test_non_string_response_terminates_batch_and_cancels_pending_peer() -> None:
    async def scenario() -> tuple[MatchArchive, asyncio.Event]:
        non_string = _SequencePlayer("non-string", ["unused"])
        non_string.moves = iter([object()])
        pending_cancelled = asyncio.Event()
        pending = _ControlledPlayer(
            "pending",
            started=asyncio.Event(),
            release=asyncio.Event(),
            finished=asyncio.Event(),
            cancelled=pending_cancelled,
            completion_order=[],
            move="unused",
        )
        archive = await asyncio.wait_for(
            play_match(_BlindBatchGame(), [non_string, pending]),
            timeout=2.0,
        )
        return archive, pending_cancelled

    archive, pending_cancelled = asyncio.run(scenario())

    assert pending_cancelled.is_set()
    rejected = next(event for event in archive.events if event.type == EventType.MOVE_REJECTED)
    assert rejected.player == "non-string"
    assert rejected.data["reason_code"] == "response_limit"
    assert rejected.data["failure_details"] == {"response_type": "object"}


def test_match_scope_player_error_terminates_batch_and_cancels_pending_peer() -> None:
    async def scenario() -> tuple[MatchArchive, asyncio.Event]:
        pending_cancelled = asyncio.Event()
        pending = _ControlledPlayer(
            "pending",
            started=asyncio.Event(),
            release=asyncio.Event(),
            finished=asyncio.Event(),
            cancelled=pending_cancelled,
            completion_order=[],
            move="unused",
        )
        archive = await asyncio.wait_for(
            play_match(
                _MatchForfeitBlindBatchGame(),
                [_TimeoutPlayer("timed-out"), pending],
            ),
            timeout=2.0,
        )
        return archive, pending_cancelled

    archive, pending_cancelled = asyncio.run(scenario())

    assert pending_cancelled.is_set()
    rejected = next(event for event in archive.events if event.type == EventType.MOVE_REJECTED)
    assert rejected.player == "timed-out"
    assert rejected.data["forfeit_scope"] == "match"
    assert archive.events[-1].data["forfeited_by"] == "timed-out"


def test_unfinished_game_without_current_players_fails_closed() -> None:
    with pytest.raises(ValueError, match="未结束时必须返回至少一名选手"):
        asyncio.run(
            play_match(
                _NoCurrentPlayerGame(),
                [_SequencePlayer("first", ["A"]), _SequencePlayer("second", ["A"])],
            )
        )


def test_simultaneous_retry_is_a_new_batch_after_other_initial_results() -> None:
    first = _SequencePlayer("first", ["Z", "A"])
    second = _SequencePlayer("second", ["A"])

    archive = asyncio.run(
        play_match(create_game("knowledge_quiz", rounds=1), [first, second], max_attempts=2)
    )

    turn_events = [
        (event.type, event.player)
        for event in archive.events
        if event.type in (EventType.TURN_PROMPT, EventType.MOVE_RECEIVED, EventType.MOVE_REJECTED)
    ]
    assert turn_events == [
        (EventType.TURN_PROMPT, "first"),
        (EventType.TURN_PROMPT, "second"),
        (EventType.MOVE_REJECTED, "first"),
        (EventType.MOVE_RECEIVED, "second"),
        (EventType.TURN_PROMPT, "first"),
        (EventType.MOVE_RECEIVED, "first"),
    ]
    assert [move.player for move in archive.moves] == ["first", "second", "first"]
    assert "还可重试 1 次" in first.prompts[1]


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


def test_player_cannot_submit_the_engine_reserved_forfeit_marker() -> None:
    player = _SequencePlayer("scripted", [FORFEIT_MOVE, "A"])

    archive = asyncio.run(
        play_match(create_game("knowledge_quiz", rounds=1), [player], max_attempts=2)
    )

    assert len(player.prompts) == 2
    assert archive.moves[0].move == FORFEIT_MOVE
    assert archive.moves[0].accepted is False
    assert "引擎保留" in (archive.moves[0].reason or "")
    assert archive.moves[1].accepted is True


def test_overlong_output_is_not_archived_and_causes_technical_loss() -> None:
    long_move = "Z" * (MAX_MOVE_CHARS + 1)
    player = _SequencePlayer("scripted", [long_move])
    other = _SequencePlayer("other", ["A"])

    archive = asyncio.run(
        play_match(create_game("knowledge_quiz", rounds=1), [player, other], max_attempts=2)
    )

    assert archive.scores == {"scripted": 0.0, "other": 1.0}
    assert long_move not in archive.to_json()
    rejected = next(event for event in archive.events if event.type == EventType.MOVE_REJECTED)
    assert rejected.data["reason_code"] == "response_limit"
    assert rejected.data["failure_details"] == {
        "limit_chars": MAX_MOVE_CHARS,
        "actual_chars": MAX_MOVE_CHARS + 1,
    }


def test_non_string_output_is_not_archived_and_causes_technical_loss() -> None:
    player = _SequencePlayer("scripted", ["unused"])
    player.moves = iter([object()])
    other = _SequencePlayer("other", ["A"])

    archive = asyncio.run(play_match(create_game("knowledge_quiz", rounds=1), [player, other]))

    rejected = next(event for event in archive.events if event.type == EventType.MOVE_REJECTED)
    assert rejected.data["reason_code"] == "response_limit"
    assert rejected.data["failure_details"] == {"response_type": "object"}
    assert archive.scores == {"scripted": 0.0, "other": 1.0}


def test_match_rejects_excessive_retry_budget() -> None:
    with pytest.raises(ValueError, match="max_attempts 最多为 10"):
        Match(create_game("knowledge_quiz"), [_llm("one", "fixed")], max_attempts=11)


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

    archive = asyncio.run(play_match(create_game("math_quiz", rounds=5), [bad, good], seed=7))

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

    archive = asyncio.run(play_match(create_game("math_quiz", rounds=1), [bad, good], seed=7))

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

    archive = asyncio.run(play_match(create_game("knowledge_quiz", rounds=2), [timed_out, other]))

    rejected = [
        event
        for event in archive.events
        if event.type == EventType.MOVE_REJECTED and event.player == "timed-out"
    ]
    assert len(rejected) == 2
    assert all(event.data["forfeit_scope"] == "turn" for event in rejected)
    assert archive.events[-1].data["termination"] == "completed"


def test_duplicate_player_names_rejected() -> None:
    players = [_llm("same", "fixed"), _llm("same", "random")]
    with pytest.raises(ValueError, match="唯一"):
        Match(create_game("math_quiz"), players)


def test_duplicate_entrant_ids_rejected_before_match_starts() -> None:
    players = [
        LLMPlayer(
            "first",
            MockProvider(strategy="fixed"),
            "fixed",
            entrant_id="shared:identity",
        ),
        LLMPlayer(
            "second",
            MockProvider(strategy="random"),
            "random",
            entrant_id="shared:identity",
        ),
    ]

    with pytest.raises(ValueError, match="entrant_id 必须唯一"):
        Match(create_game("math_quiz"), players)


def test_archive_event_and_move_models_reject_unknown_top_level_fields() -> None:
    archive = asyncio.run(
        play_match(
            create_game("knowledge_quiz", rounds=1),
            [_SequencePlayer("甲", ["A"]), _SequencePlayer("乙", ["A"])],
        )
    )

    archive_payload = archive.model_dump(mode="python")
    archive_payload["unexpected"] = True
    with pytest.raises(ValueError, match="unexpected"):
        MatchArchive.model_validate(archive_payload)

    event_payload = archive.events[0].model_dump(mode="python")
    event_payload["unexpected"] = True
    with pytest.raises(ValueError, match="unexpected"):
        MatchEvent.model_validate(event_payload)

    move_payload = archive.moves[0].model_dump(mode="python")
    move_payload["unexpected"] = True
    with pytest.raises(ValueError, match="unexpected"):
        MoveRecord.model_validate(move_payload)


def test_schema_v2_rejects_match_started_players_that_differ_from_archive() -> None:
    archive = asyncio.run(
        play_match(
            create_game("knowledge_quiz", rounds=1),
            [_SequencePlayer("甲", ["A"]), _SequencePlayer("乙", ["A"])],
        )
    )
    payload = archive.model_dump(mode="python")
    payload["events"][0]["data"]["players"][0]["entrant_id"] = "tampered:entrant"

    with pytest.raises(ValueError, match="match_started .*不一致"):
        MatchArchive.model_validate(payload)


def test_missing_schema_version_normalizes_legacy_match_started_players() -> None:
    archive = asyncio.run(
        play_match(
            create_game("knowledge_quiz", rounds=1),
            [_SequencePlayer("甲", ["A"]), _SequencePlayer("乙", ["A"])],
        )
    )
    payload = archive.model_dump(mode="python")
    payload.pop("schema_version")
    payload.pop("source")
    for descriptor in payload["players"]:
        descriptor.pop("entrant_id")
        descriptor.pop("display_name")
    for descriptor in payload["events"][0]["data"]["players"]:
        descriptor.pop("entrant_id")
        descriptor.pop("display_name")

    loaded = MatchArchive.model_validate(payload)

    assert loaded.schema_version == 1
    assert loaded.source == "legacy"
    assert [descriptor["entrant_id"] for descriptor in loaded.players] == [
        legacy_entrant_id("甲"),
        legacy_entrant_id("乙"),
    ]
    assert loaded.events[0].data["players"] == loaded.players


def test_legacy_rejects_match_started_player_identity_mismatch() -> None:
    archive = asyncio.run(
        play_match(
            create_game("knowledge_quiz", rounds=1),
            [_SequencePlayer("甲", ["A"]), _SequencePlayer("乙", ["A"])],
        )
    )
    payload = archive.model_dump(mode="python")
    payload.pop("schema_version")
    payload.pop("source")
    for descriptor in payload["players"]:
        descriptor.pop("entrant_id")
        descriptor.pop("display_name")
    for descriptor in payload["events"][0]["data"]["players"]:
        descriptor.pop("entrant_id")
        descriptor.pop("display_name")
    payload["events"][0]["data"]["players"][0]["name"] = "冒名者"

    with pytest.raises(ValueError, match="legacy match_started .*描述.*不一致"):
        MatchArchive.model_validate(payload)


def test_legacy_rejects_match_started_metadata_mismatch_for_same_names() -> None:
    archive = asyncio.run(
        play_match(
            create_game("knowledge_quiz", rounds=1),
            [_SequencePlayer("甲", ["A"]), _SequencePlayer("乙", ["A"])],
        )
    )
    payload = archive.model_dump(mode="python")
    payload.pop("schema_version")
    payload.pop("source")
    for descriptor in payload["players"]:
        descriptor.pop("entrant_id")
        descriptor.pop("display_name")
    for descriptor in payload["events"][0]["data"]["players"]:
        descriptor.pop("entrant_id")
        descriptor.pop("display_name")
    payload["events"][0]["data"]["players"][0]["kind"] = "tampered-kind"

    with pytest.raises(ValueError, match="legacy match_started .*描述.*不一致"):
        MatchArchive.model_validate(payload)


def test_schema_v1_rejects_an_explicit_nonlegacy_source() -> None:
    archive = asyncio.run(
        play_match(
            create_game("knowledge_quiz", rounds=1),
            [_SequencePlayer("甲", ["A"]), _SequencePlayer("乙", ["A"])],
        )
    )
    payload = archive.model_dump(mode="python")
    payload["schema_version"] = 1
    payload["source"] = "external"
    for descriptor in payload["players"]:
        descriptor.pop("entrant_id")
        descriptor.pop("display_name")
    for descriptor in payload["events"][0]["data"]["players"]:
        descriptor.pop("entrant_id")
        descriptor.pop("display_name")

    with pytest.raises(ValueError, match="schema v1 .*legacy"):
        MatchArchive.model_validate(payload)
