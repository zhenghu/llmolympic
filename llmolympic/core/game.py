"""Game 插件的统一接口定义。

所有比赛项目（数学问答、知识问答、将来的象棋、创意写作……）都实现
``Game`` 协议，Match 编排器只依赖这里的抽象。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel

#: 选手超时或连续非法走法后被判"放弃"时，由 Match 代为提交的特殊走法。
FORFEIT_MOVE = "__forfeit__"


class IllegalMoveError(Exception):
    """``Game.apply_move`` 收到非法走法时抛出。"""


class GameState(BaseModel):
    """对局状态基类，各 Game 插件可继承扩展自己的字段。"""

    players: list[str]
    seed: int


@runtime_checkable
class Game(Protocol):
    """比赛项目插件接口。

    核心洞察：单轮问答只是"每个选手恰好只有一步"的多轮对局特例，
    因此接口按通用回合制设计，Match 循环对单轮与多轮项目一视同仁。
    """

    name: str

    def new_state(self, players: list[str], seed: int) -> GameState:
        """创建初始状态。同一 seed 必须生成完全相同的局面（可复现）。"""
        ...

    def current_players(self, state: GameState) -> list[str]:
        """当前有待行动的选手列表（"轮到谁"）。

        严格回合制项目（如象棋）返回单个选手；同时作答的项目
        （如单轮问答）返回所有尚未提交走法的选手。
        """
        ...

    def prompt_for(self, state: GameState, player: str) -> str:
        """给指定选手看的题面/局面文本。"""
        ...

    def apply_move(self, state: GameState, player: str, move: str) -> None:
        """校验并推进状态；非法走法抛 :class:`IllegalMoveError`。

        实现必须接受 :data:`FORFEIT_MOVE`（放弃），记为不得分的一步。
        """
        ...

    def is_over(self, state: GameState) -> bool:
        """对局是否结束。"""
        ...

    def score(self, state: GameState) -> dict[str, float]:
        """终局计分：1.0 胜 / 0.5 平 / 0.0 负，或按比例得分。"""
        ...
