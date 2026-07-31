"""游戏插件测试：题目生成确定性、判分正确性、非法走法。"""

from __future__ import annotations

import pytest

from llmolympic.core.game import FORFEIT_MOVE, IllegalMoveError
from llmolympic.games import create_game
from llmolympic.games.knowledge_quiz import KnowledgeQuiz
from llmolympic.games.math_quiz import MathQuiz

PLAYERS = ["甲", "乙"]


class TestMathQuiz:
    def test_same_seed_same_questions(self) -> None:
        game = MathQuiz(rounds=5)
        s1 = game.new_state(PLAYERS, seed=42)
        s2 = game.new_state(PLAYERS, seed=42)
        assert s1.questions == s2.questions

    def test_different_seed_different_questions(self) -> None:
        game = MathQuiz(rounds=5)
        s1 = game.new_state(PLAYERS, seed=1)
        s2 = game.new_state(PLAYERS, seed=2)
        assert s1.questions != s2.questions

    def test_perfect_answers_score_one(self) -> None:
        game = MathQuiz(rounds=5)
        state = game.new_state(PLAYERS, seed=7)
        while not game.is_over(state):
            for p in game.current_players(state):
                answer = state.questions[state.cursor[p]]["answer"]
                game.apply_move(state, p, str(answer))
        assert game.score(state) == {"甲": 1.0, "乙": 1.0}

    def test_forfeit_scores_zero(self) -> None:
        game = MathQuiz(rounds=5)
        state = game.new_state(PLAYERS, seed=7)
        while not game.is_over(state):
            for p in game.current_players(state):
                game.apply_move(state, p, FORFEIT_MOVE)
        assert game.score(state) == {"甲": 0.0, "乙": 0.0}

    def test_empty_answer_is_illegal(self) -> None:
        game = MathQuiz(rounds=1)
        state = game.new_state(PLAYERS, seed=0)
        with pytest.raises(IllegalMoveError):
            game.apply_move(state, "甲", "   ")

    def test_number_extraction_with_extra_text(self) -> None:
        """模型多输出了解释文字时，仍能提取数值判分。"""
        game = MathQuiz(rounds=1)
        state = game.new_state(["甲"], seed=3)
        answer = state.questions[0]["answer"]
        game.apply_move(state, "甲", f"答案是 {answer}")
        assert game.score(state)["甲"] == 1.0


class TestKnowledgeQuiz:
    def test_same_seed_same_questions(self) -> None:
        game = KnowledgeQuiz(rounds=5)
        s1 = game.new_state(PLAYERS, seed=42)
        s2 = game.new_state(PLAYERS, seed=42)
        assert s1.questions == s2.questions

    def test_correct_letters_score_one(self) -> None:
        game = KnowledgeQuiz(rounds=5)
        state = game.new_state(PLAYERS, seed=7)
        while not game.is_over(state):
            for p in game.current_players(state):
                game.apply_move(state, p, state.questions[state.cursor[p]]["answer"])
        assert game.score(state) == {"甲": 1.0, "乙": 1.0}

    def test_invalid_option_is_illegal(self) -> None:
        game = KnowledgeQuiz(rounds=1)
        state = game.new_state(PLAYERS, seed=0)
        with pytest.raises(IllegalMoveError):
            game.apply_move(state, "甲", "E")

    def test_lowercase_letter_accepted(self) -> None:
        game = KnowledgeQuiz(rounds=1)
        state = game.new_state(["甲"], seed=0)
        correct = state.questions[0]["answer"]
        game.apply_move(state, "甲", correct.lower())
        assert game.score(state)["甲"] == 1.0


def test_create_game_unknown_name() -> None:
    with pytest.raises(ValueError, match="未知项目"):
        create_game("chess")
