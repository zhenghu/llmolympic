"""比赛项目注册表。

后期新增项目 = 在本包加一个实现 Game 协议的模块并登记到 GAME_REGISTRY，
引擎、CLI、判分、ELO 全部零改动（见 DESIGN.md §3）。
"""

from __future__ import annotations

from llmolympic.core.game import Game
from llmolympic.games.knowledge_quiz import KnowledgeQuiz
from llmolympic.games.math_quiz import MathQuiz

GAME_REGISTRY: dict[str, type] = {
    MathQuiz.name: MathQuiz,
    KnowledgeQuiz.name: KnowledgeQuiz,
}


def list_games() -> list[str]:
    return sorted(GAME_REGISTRY)


def create_game(name: str, **kwargs) -> Game:
    try:
        cls = GAME_REGISTRY[name]
    except KeyError:
        raise ValueError(f"未知项目 {name!r}，可选: {', '.join(list_games())}") from None
    return cls(**kwargs)
