"""比赛项目注册表。

后期新增项目 = 在本包加一个实现 Game 协议的模块并登记到 GAME_REGISTRY；
引擎、判分、存档与 ELO 无需改动（见 DESIGN.md §3）。
"""

from __future__ import annotations

from llmolympic.core.game import Game
from llmolympic.games.chess import Chess
from llmolympic.games.creative_writing import CreativeWriting
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
    CreativeWriting.name: CreativeWriting,
}

_GAME_OPTIONS: dict[str, frozenset[str]] = {
    MathQuiz.name: frozenset({"rounds"}),
    KnowledgeQuiz.name: frozenset({"rounds"}),
    ReasoningQuiz.name: frozenset({"rounds"}),
    RiddleQuiz.name: frozenset({"rounds"}),
    Gomoku.name: frozenset(),
    Chess.name: frozenset(),
    CreativeWriting.name: frozenset(),
}

GAME_MODES = frozenset({"play", "series", "round_robin"})


def _game_modes(game_class: type) -> frozenset[str]:
    """Return declared modes while keeping legacy Game plugins fully compatible."""

    declared = getattr(game_class, "supported_modes", GAME_MODES)
    try:
        modes = frozenset(declared)
    except TypeError as exc:
        raise TypeError("game.supported_modes 必须是比赛模式集合") from exc
    if not modes or not modes <= GAME_MODES:
        invalid = sorted(modes - GAME_MODES)
        detail = f": {', '.join(invalid)}" if invalid else ""
        raise ValueError(f"game.supported_modes 包含无效比赛模式{detail}")
    return modes


def list_games(mode: str | None = None) -> list[str]:
    """List registered games, optionally filtered by a supported competition mode."""

    if mode is None:
        return sorted(GAME_REGISTRY)
    if mode not in GAME_MODES:
        raise ValueError(f"未知比赛模式 {mode!r}，可选: {', '.join(sorted(GAME_MODES))}")
    return sorted(name for name, game_class in GAME_REGISTRY.items() if mode in _game_modes(game_class))


def game_supports_mode(name: str, mode: str) -> bool:
    """Return whether a registered game explicitly or compatibly supports ``mode``."""

    if mode not in GAME_MODES:
        raise ValueError(f"未知比赛模式 {mode!r}，可选: {', '.join(sorted(GAME_MODES))}")
    try:
        game_class = GAME_REGISTRY[name]
    except KeyError:
        raise ValueError(f"未知项目 {name!r}，可选: {', '.join(list_games())}") from None
    return mode in _game_modes(game_class)


def create_game(name: str, *, mode: str | None = None, **kwargs) -> Game:
    try:
        game_class = GAME_REGISTRY[name]
    except KeyError:
        raise ValueError(f"未知项目 {name!r}，可选: {', '.join(list_games())}") from None

    if mode is not None and not game_supports_mode(name, mode):
        raise ValueError(f"项目 {name!r} 不支持比赛模式 {mode!r}")

    supported_options = _GAME_OPTIONS.get(name)
    if supported_options is not None:
        unsupported = sorted(set(kwargs) - supported_options)
        if unsupported:
            raise ValueError(f"项目 {name!r} 不支持参数: {', '.join(unsupported)}")
    return game_class(**kwargs)
