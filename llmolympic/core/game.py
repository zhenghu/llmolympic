"""Game 插件的统一接口定义。

所有比赛项目（数学问答、知识问答、棋类、创意写作……）都实现
``Game`` 协议，Match 编排器只依赖这里的抽象。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel

#: 选手超时或连续非法走法后被判"放弃"时，由 Match 代为提交的特殊走法。
FORFEIT_MOVE = "__forfeit__"

# 平台级资源边界。具体项目可以声明更小的 ``max_players``，但不能绕过
# 这一层上限；这样第三方 Game 忘记声明人数时也不会创建无界并发调用。
MAX_PLATFORM_PLAYERS = 16
MAX_PLAYER_NAME_CHARS = 128

_BIDI_CONTROL_CHARS = frozenset(
    {
        "\u061c",
        "\u200e",
        "\u200f",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
    }
)


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

    项目可声明 ``forfeit_scope = "turn"`` 或 ``"match"``；未声明时默认
    只放弃当前回合。棋类通常使用 ``"match"``，问答通常使用 ``"turn"``。
    """

    name: str
    def new_state(self, players: list[str], seed: int) -> GameState:
        """创建初始状态。相同 seed 与相同有序选手输入必须可复现。

        同一个 Game 实例可能在系列赛中多次调用本方法；单局可变数据必须放进
        新返回的 GameState，不能残留在 Game 实例上。交换选手顺序时，除席位
        归属外，题目或随机开局条件必须保持一致。
        """
        ...

    def current_players(self, state: GameState) -> list[str]:
        """当前有待行动的选手列表（"轮到谁"）。

        严格回合制项目（如五子棋）返回单个选手；同时作答的项目
        （如单轮问答）返回所有尚未提交走法的选手。引擎会先为同轮
        所有选手快照题面，并发收齐答案后再按报名顺序调用
        :meth:`apply_move`；因此实现不得返回重复或未知选手。
        """
        ...

    def prompt_for(self, state: GameState, player: str) -> str:
        """给指定选手看的题面/局面文本。"""
        ...

    def apply_move(self, state: GameState, player: str, move: str) -> None:
        """校验并推进状态；非法走法抛 :class:`IllegalMoveError`。

        实现必须接受 :data:`FORFEIT_MOVE`（放弃）。问答项目通常记为该题
        不得分；双人棋类项目可以将其记为立即判负。
        """
        ...

    def is_over(self, state: GameState) -> bool:
        """对局是否结束。"""
        ...

    def score(self, state: GameState) -> dict[str, float]:
        """终局计分：1.0 胜 / 0.5 平 / 0.0 负，或按比例得分。"""
        ...


def describe_game_config(game: Game) -> dict[str, object]:
    """返回会影响公平对比的可序列化项目配置。

    可配置项目应实现 ``describe_config()``；无配置的旧插件回退为空对象。
    """

    describe = getattr(game, "describe_config", None)
    if describe is None:
        return {}
    config = describe()
    if not isinstance(config, dict):
        raise TypeError("game.describe_config() 必须返回字典")
    return dict(config)


def validate_player_count(game: Game, count: int) -> None:
    """校验项目人数；未声明元数据的旧插件默认允许一名或更多选手。"""
    minimum = getattr(game, "min_players", 1)
    maximum = getattr(game, "max_players", None)
    if maximum == minimum and count != minimum:
        raise ValueError(f"项目 {game.name!r} 需要恰好 {minimum} 名选手，实际为 {count} 名")
    if count < minimum:
        raise ValueError(f"项目 {game.name!r} 至少需要 {minimum} 名选手，实际为 {count} 名")
    if maximum is not None and count > maximum:
        raise ValueError(f"项目 {game.name!r} 最多支持 {maximum} 名选手，实际为 {count} 名")
    if count > MAX_PLATFORM_PLAYERS:
        raise ValueError(
            f"平台单场最多支持 {MAX_PLATFORM_PLAYERS} 名选手，实际为 {count} 名"
        )


def validate_players(game: Game, players: list[str]) -> None:
    """统一校验选手名字与项目人数约束。"""
    if any(not isinstance(name, str) or not name.strip() for name in players):
        raise ValueError("选手名字必须是非空字符串")
    if any(len(name) > MAX_PLAYER_NAME_CHARS for name in players):
        raise ValueError(f"选手名字最多允许 {MAX_PLAYER_NAME_CHARS} 个字符")
    if any(
        any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F or char in _BIDI_CONTROL_CHARS for char in name)
        for name in players
    ):
        raise ValueError("选手名字不能包含控制字符或双向文本控制符")
    if len(set(players)) != len(players):
        raise ValueError(f"选手名字必须唯一: {players}")
    validate_player_count(game, len(players))
