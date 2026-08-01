"""逻辑推理与猜谜插件的生成、公平性和判分测试。"""

from __future__ import annotations

import pytest

from llmolympic.core.game import FORFEIT_MOVE, IllegalMoveError
from llmolympic.games._choice import LETTERS
from llmolympic.games.reasoning_quiz import (
    GENERATOR_VERSION as REASONING_GENERATOR_VERSION,
)
from llmolympic.games.reasoning_quiz import (
    ReasoningQuiz,
    code_solutions,
    ordering_solutions,
)
from llmolympic.games.riddle_quiz import (
    BANK_VERSION,
    RIDDLE_BANK,
    RiddleQuiz,
    validate_riddle_bank,
)

PLAYERS = ["甲", "乙"]


def _answer_all_correct(game, state) -> None:
    while not game.is_over(state):
        for player in game.current_players(state):
            question = state.questions[state.cursor[player]]
            game.apply_move(state, player, question["answer"])


class TestReasoningQuiz:
    def test_seed_and_player_order_do_not_change_questions(self) -> None:
        game = ReasoningQuiz(rounds=8)

        first = game.new_state(PLAYERS, seed=42)
        repeated = game.new_state(list(reversed(PLAYERS)), seed=42)
        different = game.new_state(PLAYERS, seed=43)

        assert first.questions == repeated.questions
        assert first.questions != different.questions
        assert len(
            {
                (
                    question["kind"],
                    tuple(question["solution"])
                    if isinstance(question["solution"], list)
                    else question["solution"],
                )
                for question in first.questions
            }
        ) == len(first.questions)

    def test_generated_questions_have_one_provable_solution(self) -> None:
        for seed in range(30):
            state = ReasoningQuiz(rounds=6).new_state(PLAYERS, seed=seed)
            for question in state.questions:
                assert len(question["options"]) == 4
                assert len(set(question["options"])) == 4
                assert question["answer"] in LETTERS
                correct = question["options"][LETTERS.index(question["answer"])]
                if question["kind"] == "ordering":
                    assert ordering_solutions(question["constraints"]) == [
                        tuple(question["solution"])
                    ]
                    assert correct == " → ".join(question["solution"])
                else:
                    assert code_solutions(question["clues"]) == [question["solution"]]
                    assert all(
                        clue["guess"] != question["solution"]
                        for clue in question["clues"]
                    )
                    assert correct == question["solution"]

    def test_perfect_partial_and_forfeit_scores(self) -> None:
        game = ReasoningQuiz(rounds=3)
        state = game.new_state(["甲"], seed=7)
        first = state.questions[0]
        game.apply_move(state, "甲", first["answer"])
        second = state.questions[1]
        wrong = next(letter for letter in LETTERS if letter != second["answer"])
        game.apply_move(state, "甲", wrong)
        game.apply_move(state, "甲", FORFEIT_MOVE)

        assert game.is_over(state)
        assert game.score(state) == {"甲": pytest.approx(1 / 3)}

    @pytest.mark.parametrize(
        "wrapper",
        [
            lambda letter, option: letter.lower(),
            lambda letter, option: f"({letter})",
            lambda letter, option: f"答案是 {letter}",
            lambda letter, option: f"谜底是 {letter}",
            lambda letter, option: f"我猜 {letter}",
            lambda letter, option: f"{letter}. {option}",
            lambda letter, option: option,
        ],
    )
    def test_unambiguous_choice_wrappers_are_accepted(self, wrapper) -> None:
        game = ReasoningQuiz(rounds=1)
        state = game.new_state(["甲"], seed=3)
        question = state.questions[0]
        option = question["options"][LETTERS.index(question["answer"])]

        game.apply_move(state, "甲", wrapper(question["answer"], option))

        assert game.score(state) == {"甲": 1.0}

    @pytest.mark.parametrize("move", ["", "E", "A 或 B", "答案是", "随便说说"])
    def test_ambiguous_or_invalid_output_does_not_advance(self, move: str) -> None:
        game = ReasoningQuiz(rounds=1)
        state = game.new_state(["甲"], seed=3)

        with pytest.raises(IllegalMoveError):
            game.apply_move(state, "甲", move)

        assert state.cursor == {"甲": 0}
        assert state.answers == {"甲": []}

    def test_letter_and_labeled_option_must_agree(self) -> None:
        game = ReasoningQuiz(rounds=1)
        state = game.new_state(["甲"], seed=3)
        question = state.questions[0]
        correct = question["answer"]
        other_letter = next(letter for letter in LETTERS if letter != correct)
        other_option = question["options"][LETTERS.index(other_letter)]

        with pytest.raises(IllegalMoveError, match="不一致"):
            game.apply_move(state, "甲", f"{correct}. {other_option}")

        assert state.cursor == {"甲": 0}

    def test_round_limit_bounds_generator_cost(self) -> None:
        state = ReasoningQuiz(rounds=50).new_state(["甲"], seed=101)
        keys = {
            (
                question["kind"],
                tuple(question["solution"])
                if isinstance(question["solution"], list)
                else question["solution"],
            )
            for question in state.questions
        }
        assert len(keys) == 50
        with pytest.raises(ValueError, match="最多为 50"):
            ReasoningQuiz(rounds=51)

    def test_new_states_do_not_share_mutable_match_data(self) -> None:
        game = ReasoningQuiz(rounds=1)
        first = game.new_state(PLAYERS, seed=9)
        second = game.new_state(PLAYERS, seed=9)

        game.apply_move(first, "甲", first.questions[0]["answer"])

        assert second.cursor == {"甲": 0, "乙": 0}
        assert second.answers == {"甲": [], "乙": []}
        assert game.describe_config() == {
            "rounds": 1,
            "source": "generated",
            "generator_version": REASONING_GENERATOR_VERSION,
        }


class TestRiddleQuiz:
    def test_seed_and_player_order_do_not_change_questions(self) -> None:
        game = RiddleQuiz(rounds=6)

        first = game.new_state(PLAYERS, seed=42)
        repeated = game.new_state(list(reversed(PLAYERS)), seed=42)
        different = game.new_state(PLAYERS, seed=43)

        assert first.questions == repeated.questions
        assert first.questions != different.questions
        assert len({question["target_id"] for question in first.questions}) == 6

    def test_structured_questions_have_unique_correct_option_and_metadata(self) -> None:
        answers_by_id = {item["id"]: item["answer"] for item in RIDDLE_BANK}
        validate_riddle_bank()
        for seed in range(30):
            state = RiddleQuiz(rounds=8).new_state(PLAYERS, seed=seed)
            for question in state.questions:
                assert len(question["options"]) == 4
                assert len(set(question["options"])) == 4
                assert len(question["clues"]) == 3
                assert question["answer"] in LETTERS
                correct = question["options"][LETTERS.index(question["answer"])]
                assert correct == answers_by_id[question["target_id"]]
                assert question["source"] == "generated_from_structured_bank"
                assert question["bank_version"] == BANK_VERSION
                assert question["matching_target_ids"] == [question["target_id"]]
                for clue in question["clues"]:
                    owners = [
                        item["id"]
                        for item in RIDDLE_BANK
                        if clue in item["features"].values()
                    ]
                    assert owners == [question["target_id"]]

    def test_canonical_answer_alias_and_letter_are_scored_equally(self) -> None:
        game = RiddleQuiz(rounds=3)
        state = game.new_state(["规范名", "别名", "字母"], seed=5)
        question = state.questions[0]
        correct_letter = question["answer"]
        canonical = question["options"][LETTERS.index(correct_letter)]
        alias = question["aliases"][correct_letter][0]

        game.apply_move(state, "规范名", canonical)
        game.apply_move(state, "别名", f"谜底是 {alias}")
        game.apply_move(state, "字母", f"选项{correct_letter}")

        assert state.answers == {
            "规范名": [correct_letter],
            "别名": [correct_letter],
            "字母": [correct_letter],
        }

    def test_all_correct_and_all_forfeit_scores(self) -> None:
        game = RiddleQuiz(rounds=5)
        state = game.new_state(PLAYERS, seed=11)
        while not game.is_over(state):
            for player in game.current_players(state):
                if player == "甲":
                    question = state.questions[state.cursor[player]]
                    game.apply_move(state, player, question["answer"])
                else:
                    game.apply_move(state, player, FORFEIT_MOVE)

        assert game.score(state) == {"甲": 1.0, "乙": 0.0}

    def test_round_limit_is_explicit_instead_of_silent_truncation(self) -> None:
        state = RiddleQuiz(rounds=len(RIDDLE_BANK)).new_state(["甲"], seed=0)
        assert len(state.questions) == len(RIDDLE_BANK)
        assert len({question["target_id"] for question in state.questions}) == len(RIDDLE_BANK)
        with pytest.raises(ValueError, match=f"最多为 {len(RIDDLE_BANK)}"):
            RiddleQuiz(rounds=len(RIDDLE_BANK) + 1)

    def test_config_records_bank_and_generator_versions(self) -> None:
        assert RiddleQuiz(rounds=2).describe_config() == {
            "rounds": 2,
            "source": "generated_from_structured_bank",
            "bank_version": BANK_VERSION,
            "generator_version": 1,
        }


@pytest.mark.parametrize("game_class", [ReasoningQuiz, RiddleQuiz])
def test_same_round_prompt_is_identical_for_all_players(game_class) -> None:
    game = game_class(rounds=1)
    state = game.new_state(PLAYERS, seed=17)

    assert game.prompt_for(state, "甲") == game.prompt_for(state, "乙")
    assert "\nA. " in game.prompt_for(state, "甲")


@pytest.mark.parametrize("game_class", [ReasoningQuiz, RiddleQuiz])
def test_answering_after_completion_is_rejected(game_class) -> None:
    game = game_class(rounds=1)
    state = game.new_state(["甲"], seed=0)
    _answer_all_correct(game, state)

    with pytest.raises(IllegalMoveError, match="没有待作答"):
        game.apply_move(state, "甲", "A")
