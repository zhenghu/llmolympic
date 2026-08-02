"""逻辑推理：动态生成且经穷举验证唯一解的客观选择题。"""

from __future__ import annotations

import itertools
import random
from collections.abc import Sequence

from pydantic import Field

from llmolympic.core.game import FORFEIT_MOVE, GameState, IllegalMoveError
from llmolympic.games._choice import make_options, parse_choice, render_options

GENERATOR_VERSION = 1
MAX_ROUNDS = 50
_PEOPLE = ("甲", "乙", "丙", "丁")
_VALID_CODES = tuple(
    "".join(digits)
    for digits in itertools.permutations("0123456789", 3)
    if digits[0] != "0"
)


class ReasoningQuizState(GameState):
    rounds: int
    questions: list[dict]
    cursor: dict[str, int] = Field(default_factory=dict)
    answers: dict[str, list[str]] = Field(default_factory=dict)


def _order_satisfies(order: Sequence[str], constraint: dict) -> bool:
    left = order.index(constraint["left"])
    right = order.index(constraint["right"])
    if constraint["type"] == "before":
        return left < right
    if constraint["type"] == "immediately_before":
        return left + 1 == right
    raise ValueError(f"未知排序约束: {constraint['type']!r}")


def ordering_solutions(constraints: Sequence[dict]) -> list[tuple[str, ...]]:
    """枚举所有满足约束的排序，供生成器自检和档案复核。"""

    return [
        order
        for order in itertools.permutations(_PEOPLE)
        if all(_order_satisfies(order, constraint) for constraint in constraints)
    ]


def _constraint_text(constraint: dict) -> str:
    if constraint["type"] == "before":
        return f"{constraint['left']} 排在 {constraint['right']} 前面"
    return f"{constraint['left']} 紧挨在 {constraint['right']} 前面"


def _generate_ordering(rng: random.Random) -> dict:
    solution = tuple(rng.sample(_PEOPLE, len(_PEOPLE)))
    candidates = [
        {"type": "before", "left": solution[i], "right": solution[j]}
        for i in range(len(solution))
        for j in range(i + 1, len(solution))
    ]
    candidates.extend(
        {
            "type": "immediately_before",
            "left": solution[index],
            "right": solution[index + 1],
        }
        for index in range(len(solution) - 1)
    )
    rng.shuffle(candidates)

    constraints: list[dict] = []
    possible = list(itertools.permutations(_PEOPLE))
    for constraint in candidates:
        narrowed = [order for order in possible if _order_satisfies(order, constraint)]
        if len(narrowed) == len(possible):
            continue
        constraints.append(constraint)
        possible = narrowed
        if len(possible) == 1:
            break
    if possible != [solution]:
        raise RuntimeError("排序题生成失败：约束没有唯一解")

    correct = " → ".join(solution)
    other_orders = [
        " → ".join(order) for order in itertools.permutations(_PEOPLE) if order != solution
    ]
    options, answer = make_options(rng, correct, rng.sample(other_orders, 3))
    clue_text = "；".join(_constraint_text(constraint) for constraint in constraints)
    return {
        "kind": "ordering",
        "text": f"甲、乙、丙、丁四人排成一列。已知：{clue_text}。哪一个是完整正确顺序？",
        "options": options,
        "answer": answer,
        "constraints": constraints,
        "solution": list(solution),
        "source": "generated",
        "generator_version": GENERATOR_VERSION,
    }


def _code_feedback(secret: str, guess: str) -> tuple[int, int]:
    exact = sum(left == right for left, right in zip(secret, guess, strict=True))
    misplaced = len(set(secret) & set(guess)) - exact
    return exact, misplaced


def code_solutions(clues: Sequence[dict]) -> list[str]:
    """枚举符合全部数字密码线索的候选。"""

    return [
        code
        for code in _VALID_CODES
        if all(
            _code_feedback(code, clue["guess"]) == (clue["exact"], clue["misplaced"])
            for clue in clues
        )
    ]


def _generate_code_breaker(rng: random.Random) -> dict:
    secret = rng.choice(_VALID_CODES)
    possible = list(_VALID_CODES)
    clues: list[dict] = []
    used_guesses: set[str] = set()

    while len(possible) > 1:
        available = [code for code in _VALID_CODES if code != secret and code not in used_guesses]
        sample_size = min(64, len(available))
        guess_pool = rng.sample(available, sample_size)
        other_candidate = next((code for code in possible if code != secret), None)
        if other_candidate is not None and other_candidate not in guess_pool:
            # 以另一个当前候选作猜测时，它自身会得到 3/0，而 secret 不会；
            # 把它纳入候选池可保证每轮至少排除一个密码，无需泄露 secret 兜底。
            guess_pool.append(other_candidate)

        possible_snapshot = tuple(possible)

        def remaining_count(
            guess: str, current_candidates: tuple[str, ...] = possible_snapshot
        ) -> int:
            expected = _code_feedback(secret, guess)
            return sum(
                _code_feedback(candidate, guess) == expected
                for candidate in current_candidates
            )

        guess = min(guess_pool, key=lambda candidate: (remaining_count(candidate), candidate))
        exact, misplaced = _code_feedback(secret, guess)
        clue = {"guess": guess, "exact": exact, "misplaced": misplaced}
        clues.append(clue)
        used_guesses.add(guess)
        possible = [
            candidate
            for candidate in possible
            if _code_feedback(candidate, guess) == (exact, misplaced)
        ]

    if possible != [secret]:
        raise RuntimeError("密码题生成失败：线索没有唯一解")

    distractors = rng.sample([code for code in _VALID_CODES if code != secret], 3)
    options, answer = make_options(rng, secret, distractors)
    rendered_clues = "\n".join(
        f"- {clue['guess']}：数字和位置都正确 {clue['exact']} 个；"
        f"数字正确但位置错误 {clue['misplaced']} 个"
        for clue in clues
    )
    return {
        "kind": "code_breaker",
        "text": (
            "密码由 3 个互不重复的数字组成，首位不是 0。根据以下线索推出唯一密码：\n"
            f"{rendered_clues}"
        ),
        "options": options,
        "answer": answer,
        "clues": clues,
        "solution": secret,
        "source": "generated",
        "generator_version": GENERATOR_VERSION,
    }


_GENERATORS = (_generate_ordering, _generate_code_breaker)


class ReasoningQuiz:
    """每位选手回答同一组动态逻辑题，按正确率计分。"""

    name = "reasoning_quiz"
    forfeit_scope = "turn"
    min_players = 1
    max_players = None

    def __init__(self, rounds: int = 5) -> None:
        if rounds < 1:
            raise ValueError("rounds 必须至少为 1")
        if rounds > MAX_ROUNDS:
            raise ValueError(f"reasoning_quiz 的 rounds 最多为 {MAX_ROUNDS}")
        self.rounds = rounds

    def describe_config(self) -> dict[str, object]:
        return {
            "rounds": self.rounds,
            "source": "generated",
            "generator_version": GENERATOR_VERSION,
        }

    def new_state(self, players: list[str], seed: int) -> ReasoningQuizState:
        rng = random.Random(seed)  # noqa: S311 - 公平复现用种子，不用于安全令牌
        questions: list[dict] = []
        seen_solutions: set[tuple[str, tuple[str, ...] | str]] = set()
        attempts = 0
        while len(questions) < self.rounds:
            attempts += 1
            if attempts > self.rounds * 100:
                raise RuntimeError("推理题生成失败：无法在限定次数内生成不重复题目")
            question = rng.choice(_GENERATORS)(rng)
            solution = question["solution"]
            solution_key: tuple[str, ...] | str
            if isinstance(solution, list):
                solution_key = tuple(solution)
            else:
                solution_key = solution
            key = (question["kind"], solution_key)
            if key in seen_solutions:
                continue
            seen_solutions.add(key)
            questions.append(question)
        return ReasoningQuizState(
            players=list(players),
            seed=seed,
            rounds=self.rounds,
            questions=questions,
            cursor={player: 0 for player in players},
            answers={player: [] for player in players},
        )

    def current_players(self, state: ReasoningQuizState) -> list[str]:
        return [player for player in state.players if state.cursor[player] < state.rounds]

    def prompt_for(self, state: ReasoningQuizState, player: str) -> str:
        index = state.cursor[player]
        question = state.questions[index]
        return (
            f"逻辑推理（{question['kind']}）第 {index + 1}/{state.rounds} 题：\n"
            f"{question['text']}\n{render_options(question['options'])}\n"
            "（只输出一个选项字母，或完整选项内容）"
        )

    def apply_move(self, state: ReasoningQuizState, player: str, move: str) -> None:
        if player not in self.current_players(state):
            raise IllegalMoveError(f"{player} 当前没有待作答的题")
        if move == FORFEIT_MOVE:
            answer = ""
        else:
            question = state.questions[state.cursor[player]]
            answer = parse_choice(move, question["options"])
        state.answers[player].append(answer)
        state.cursor[player] += 1

    def is_over(self, state: ReasoningQuizState) -> bool:
        return not self.current_players(state)

    def score(self, state: ReasoningQuizState) -> dict[str, float]:
        return {
            player: sum(
                answer == question["answer"]
                for question, answer in zip(
                    state.questions, state.answers[player], strict=True
                )
            )
            / state.rounds
            for player in state.players
        }
