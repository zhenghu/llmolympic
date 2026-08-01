"""数学问答：程序动态生成题目（模板 + 随机参数），数值判分。

题目现场生成、不在任何模型训练集里，保证模型间对比公平（见 DESIGN.md §5）。
"""

from __future__ import annotations

import random
import re

from pydantic import Field

from llmolympic.core.game import FORFEIT_MOVE, GameState, IllegalMoveError

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_TOLERANCE = 1e-6


class MathQuizState(GameState):
    rounds: int
    questions: list[dict]  # {"text": str, "answer": float, "source": "generated"}
    cursor: dict[str, int] = Field(default_factory=dict)  # 每个选手下一题的下标
    answers: dict[str, list[str]] = Field(default_factory=dict)  # 每个选手已提交的原始走法


def _gen_question(rng: random.Random) -> dict:
    kind = rng.choice(["add", "sub", "mul", "div", "linear"])
    if kind == "add":
        a, b = rng.randint(10, 999), rng.randint(10, 999)
        text, answer = f"{a} + {b} = ?", a + b
    elif kind == "sub":
        a, b = rng.randint(100, 999), rng.randint(10, 99)
        text, answer = f"{a} - {b} = ?", a - b
    elif kind == "mul":
        a, b = rng.randint(3, 25), rng.randint(3, 25)
        text, answer = f"{a} × {b} = ?", a * b
    elif kind == "div":
        b, q = rng.randint(2, 15), rng.randint(2, 15)
        text, answer = f"{b * q} ÷ {b} = ?", q
    else:  # linear: a·x + b = c，保证 x 为正整数
        x, a, b = rng.randint(1, 9), rng.randint(2, 9), rng.randint(1, 20)
        text, answer = f"已知 {a}x + {b} = {a * x + b}，求 x", x
    return {"text": text, "answer": float(answer), "source": "generated"}


def _extract_number(text: str) -> float | None:
    m = _NUMBER_RE.search(text)
    return float(m.group()) if m else None


class MathQuiz:
    """单轮问答项目：每位选手依次回答同样的 rounds 道题。"""

    name = "math_quiz"
    forfeit_scope = "turn"
    min_players = 1
    max_players = None

    def __init__(self, rounds: int = 5) -> None:
        if rounds < 1:
            raise ValueError("rounds 必须至少为 1")
        self.rounds = rounds

    def describe_config(self) -> dict[str, object]:
        return {"rounds": self.rounds}

    def new_state(self, players: list[str], seed: int) -> MathQuizState:
        rng = random.Random(seed)  # 同一 seed 生成完全相同的题目（可复现）
        return MathQuizState(
            players=list(players),
            seed=seed,
            rounds=self.rounds,
            questions=[_gen_question(rng) for _ in range(self.rounds)],
            cursor={p: 0 for p in players},
            answers={p: [] for p in players},
        )

    def current_players(self, state: MathQuizState) -> list[str]:
        return [p for p in state.players if state.cursor[p] < state.rounds]

    def prompt_for(self, state: MathQuizState, player: str) -> str:
        idx = state.cursor[player]
        q = state.questions[idx]
        return (
            f"数学问答 第 {idx + 1}/{state.rounds} 题：\n{q['text']}\n"
            "（只输出最终数字答案，不要输出其他内容）"
        )

    def apply_move(self, state: MathQuizState, player: str, move: str) -> None:
        if player not in self.current_players(state):
            raise IllegalMoveError(f"{player} 当前没有待作答的题")
        if move == FORFEIT_MOVE:
            state.answers[player].append("")
        elif not move.strip():
            raise IllegalMoveError("答案不能为空")
        else:
            state.answers[player].append(move)
        state.cursor[player] += 1

    def is_over(self, state: MathQuizState) -> bool:
        return not self.current_players(state)

    def score(self, state: MathQuizState) -> dict[str, float]:
        scores: dict[str, float] = {}
        for player in state.players:
            correct = 0
            for q, raw in zip(state.questions, state.answers[player], strict=True):
                got = _extract_number(raw)
                if got is not None and abs(got - q["answer"]) < _TOLERANCE:
                    correct += 1
            scores[player] = correct / state.rounds
        return scores
