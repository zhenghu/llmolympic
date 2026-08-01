"""比赛项目注册表。

后期新增项目 = 在本包加一个实现 Game 协议的模块并登记到 GAME_REGISTRY；
引擎、判分、存档与 ELO 无需改动（见 DESIGN.md §3）。
"""

from __future__ import annotations

from llmolympic.core.game import Game
from llmolympic.games.chess import Chess
from llmolympic.games.gomoku import Gomoku
from llmolympic.games.knowledge_quiz import KnowledgeQuiz
from llmolympic.games.math_quiz import MathQuiz
from llmolympic.games.reasoning_quiz import ReasoningQuiz
from llmolympic.games.riddle_quiz import RiddleQuiz

GAME_REGISTRY: dict[str, type] = {
    MathQuiz.name: MathQuiz,
    KnowledgeQuiz.name: KnowledgeQuiz,
    ReasoningQuiz.name: ReasoningQuiz,
    RiddleQuiz.name: RiddleQuiz,
    Gomoku.name: Gomoku,
    Chess.name: Chess,
}

_GAME_OPTIONS: dict[str, frozenset[str]] = {
    MathQuiz.name: frozenset({"rounds"}),
    KnowledgeQuiz.name: frozenset({"rounds"}),
    ReasoningQuiz.name: frozenset({"rounds"}),
    RiddleQuiz.name: frozenset({"rounds"}),
    Gomoku.name: frozenset(),
    Chess.name: frozenset(),
}


def list_games() -> list[str]:
    return sorted(GAME_REGISTRY)


def create_game(name: str, **kwargs) -> Game:
    try:
        game_class = GAME_REGISTRY[name]
    except KeyError:
        raise ValueError(f"未知项目 {name!r}，可选: {', '.join(list_games())}") from None

    supported_options = _GAME_OPTIONS.get(name)
    if supported_options is not None:
        unsupported = sorted(set(kwargs) - supported_options)
        if unsupported:
            raise ValueError(f"项目 {name!r} 不支持参数: {', '.join(unsupported)}")
    return game_class(**kwargs)
