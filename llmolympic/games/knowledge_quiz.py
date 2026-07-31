"""知识竞答：内置静态选择题库，选项字母精确匹配判分。

静态题库可能存在"模型训练时见过"的偏差，题目元数据标注 source: static，
报表中可与生成题分开统计（见 DESIGN.md §5）。
"""

from __future__ import annotations

import random

from pydantic import Field

from llmolympic.core.game import FORFEIT_MOVE, GameState, IllegalMoveError

#: 内置题库：question / options / answer（正确选项字母）/ domain。
QUESTION_BANK: list[dict] = [
    {
        "question": "水的化学式是什么？",
        "options": ["CO₂", "H₂O", "O₂", "NaCl"],
        "answer": "B",
        "domain": "化学",
    },
    {
        "question": "光在真空中的传播速度约为？",
        "options": ["3×10⁸ m/s", "3×10⁶ m/s", "340 m/s", "1.5×10¹¹ m/s"],
        "answer": "A",
        "domain": "物理",
    },
    {
        "question": "《红楼梦》的作者是谁？",
        "options": ["罗贯中", "施耐庵", "曹雪芹", "吴承恩"],
        "answer": "C",
        "domain": "文学",
    },
    {
        "question": "地球绕太阳公转一周大约需要多久？",
        "options": ["30 天", "365 天", "24 小时", "10 年"],
        "answer": "B",
        "domain": "天文",
    },
    {
        "question": "下列哪个数是质数？",
        "options": ["21", "27", "29", "33"],
        "answer": "C",
        "domain": "数学",
    },
    {
        "question": "人体最大的器官是？",
        "options": ["肝脏", "皮肤", "心脏", "肺"],
        "answer": "B",
        "domain": "生物",
    },
    {
        "question": "法国的首都是哪座城市？",
        "options": ["伦敦", "柏林", "马德里", "巴黎"],
        "answer": "D",
        "domain": "地理",
    },
    {
        "question": "二进制数 1010 对应的十进制数是？",
        "options": ["8", "10", "12", "14"],
        "answer": "B",
        "domain": "计算机",
    },
    {
        "question": "植物的光合作用主要发生在哪个细胞器中？",
        "options": ["线粒体", "细胞核", "叶绿体", "核糖体"],
        "answer": "C",
        "domain": "生物",
    },
    {
        "question": "下列哪个不是操作系统？",
        "options": ["Linux", "Oracle", "Windows", "macOS"],
        "answer": "B",
        "domain": "计算机",
    },
    {
        "question": "万有引力定律由谁提出？",
        "options": ["爱因斯坦", "伽利略", "牛顿", "霍金"],
        "answer": "C",
        "domain": "物理",
    },
    {
        "question": "一年中有多少个节气？",
        "options": ["12", "24", "36", "48"],
        "answer": "B",
        "domain": "常识",
    },
]

_LETTERS = ("A", "B", "C", "D")


class KnowledgeQuizState(GameState):
    rounds: int
    questions: list[dict]  # 从 QUESTION_BANK 抽出的题目（含 "source": "static"）
    cursor: dict[str, int] = Field(default_factory=dict)
    answers: dict[str, list[str]] = Field(default_factory=dict)


class KnowledgeQuiz:
    """单轮选择题项目：每位选手依次回答同样的 rounds 道题。"""

    name = "knowledge_quiz"

    def __init__(self, rounds: int = 5) -> None:
        if rounds < 1:
            raise ValueError("rounds 必须至少为 1")
        self.rounds = rounds

    def new_state(self, players: list[str], seed: int) -> KnowledgeQuizState:
        rng = random.Random(seed)  # 同一 seed 抽出完全相同的题目（可复现）
        k = min(self.rounds, len(QUESTION_BANK))
        questions = [dict(q, source="static") for q in rng.sample(QUESTION_BANK, k)]
        return KnowledgeQuizState(
            players=list(players),
            seed=seed,
            rounds=k,
            questions=questions,
            cursor={p: 0 for p in players},
            answers={p: [] for p in players},
        )

    def current_players(self, state: KnowledgeQuizState) -> list[str]:
        return [p for p in state.players if state.cursor[p] < state.rounds]

    def prompt_for(self, state: KnowledgeQuizState, player: str) -> str:
        idx = state.cursor[player]
        q = state.questions[idx]
        options = "\n".join(
            f"{letter}. {text}" for letter, text in zip(_LETTERS, q["options"], strict=True)
        )
        return (
            f"知识竞答（{q['domain']}）第 {idx + 1}/{state.rounds} 题：\n"
            f"{q['question']}\n{options}\n（只输出选项字母 A/B/C/D）"
        )

    def apply_move(self, state: KnowledgeQuizState, player: str, move: str) -> None:
        if player not in self.current_players(state):
            raise IllegalMoveError(f"{player} 当前没有待作答的题")
        if move == FORFEIT_MOVE:
            state.answers[player].append("")
        else:
            letter = move.strip().upper()[:1]
            if letter not in _LETTERS:
                raise IllegalMoveError(f"无效选项: {move!r}，请回答 A/B/C/D")
            state.answers[player].append(letter)
        state.cursor[player] += 1

    def is_over(self, state: KnowledgeQuizState) -> bool:
        return not self.current_players(state)

    def score(self, state: KnowledgeQuizState) -> dict[str, float]:
        return {
            player: sum(
                1
                for q, raw in zip(state.questions, state.answers[player], strict=True)
                if raw == q["answer"]
            )
            / state.rounds
            for player in state.players
        }
