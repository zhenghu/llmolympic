"""Freestyle Gomoku on a 15 x 15 board.

The first player uses ``X`` (black) and the second uses ``O`` (white).  A
player wins as soon as they have at least five contiguous stones on any of the
four axes; overlines therefore win as well.
"""

from __future__ import annotations

import re
import unicodedata

from pydantic import Field

from llmolympic.core.game import FORFEIT_MOVE, GameState, IllegalMoveError

BOARD_SIZE = 15
COLUMNS = "ABCDEFGHIJKLMNO"
EMPTY = "."
BLACK = "X"
WHITE = "O"
MARKS = (BLACK, WHITE)

_COORDINATE_RE = re.compile(
    r"(?<![A-Z0-9])([A-O])\s*(?:[,:/-]\s*)?(1[0-5]|[1-9])(?![A-Z0-9])"
)
_WIN_AXES = ((0, 1), (1, 0), (1, 1), (1, -1))


def _empty_board() -> list[list[str]]:
    return [[EMPTY for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)]


def _parse_coordinate(move: str) -> tuple[int, int]:
    """提取唯一的合法坐标，返回从零开始的 ``(行, 列)``。"""

    if not isinstance(move, str):
        raise IllegalMoveError("落子坐标必须是文本，例如 H8")

    candidate = unicodedata.normalize("NFKC", move).upper()
    coordinates = {
        (int(row_text) - 1, ord(column_text) - ord("A"))
        for column_text, row_text in _COORDINATE_RE.findall(candidate)
    }
    if not coordinates:
        raise IllegalMoveError(f"无效落子坐标: {move!r}；请只输出 A1 到 O15")
    if len(coordinates) > 1:
        raise IllegalMoveError(f"输出包含多个不同坐标: {move!r}；请只选择一个落子点")
    return coordinates.pop()


class GomokuState(GameState):
    """Mutable state for one Gomoku game."""

    board: list[list[str]] = Field(default_factory=_empty_board)
    turn: int = 0
    move_count: int = 0
    winner: str | None = None
    is_draw: bool = False
    forfeited_by: str | None = None
    last_move: str | None = None
    last_player: str | None = None


class Gomoku:
    """Two-player freestyle Gomoku game plugin."""

    name = "gomoku"
    forfeit_scope = "match"
    min_players = 2
    max_players = 2

    def new_state(self, players: list[str], seed: int) -> GomokuState:
        if len(players) != 2:
            raise ValueError("五子棋必须恰好有 2 名选手")
        if len(set(players)) != 2:
            raise ValueError("五子棋的两名选手名字必须唯一")
        return GomokuState(players=list(players), seed=seed)

    def current_players(self, state: GomokuState) -> list[str]:
        if self.is_over(state):
            return []
        return [state.players[state.turn]]

    def prompt_for(self, state: GomokuState, player: str) -> str:
        if player not in state.players:
            raise ValueError(f"未知选手: {player!r}")
        if self.is_over(state):
            raise ValueError("对局已经结束，不能再生成落子提示")
        expected = state.players[state.turn]
        if player != expected:
            raise ValueError(f"当前应由 {expected} 落子，不是 {player}")
        player_index = state.players.index(player)
        mark = MARKS[player_index]
        opponent = state.players[1 - player_index]
        opponent_mark = MARKS[1 - player_index]
        header = "    " + " ".join(COLUMNS)
        rows = [f"{row + 1:>2}  " + " ".join(cells) for row, cells in enumerate(state.board)]
        board = "\n".join([header, *rows])
        if state.last_move is None:
            last_move = "无"
        else:
            last_mark = MARKS[state.players.index(state.last_player)]
            last_move = f"{state.last_player}（{last_mark}）下在 {state.last_move}"
        return (
            "五子棋（gomoku）· 15×15 自由规则（无禁手）\n"
            f"你是 {player}，执 {mark}；对手 {opponent} 执 {opponent_mark}。\n"
            "黑棋 X 先行，双方交替落子；横、竖或斜线连续 5 子或以上获胜；"
            "满盘无人获胜则和棋。\n"
            "坐标列 A-O 从左到右（包含 I），行 1-15 从上到下；A1 是左上角，"
            "O15 是右下角，`.` 是空位。\n"
            f"上一手：{last_move}\n"
            f"第 {state.move_count + 1} 手，轮到你：\n{board}\n"
            "只输出一个未占用坐标，格式如 H8。不要解释，不要输出多个坐标。"
        )

    def apply_move(self, state: GomokuState, player: str, move: str) -> None:
        if self.is_over(state):
            raise IllegalMoveError("对局已经结束")

        expected = state.players[state.turn]
        if player != expected:
            raise IllegalMoveError(f"当前应由 {expected} 落子，不是 {player}")

        if move == FORFEIT_MOVE:
            state.winner = state.players[1 - state.turn]
            state.forfeited_by = player
            return

        row, column = _parse_coordinate(move)
        if state.board[row][column] != EMPTY:
            raise IllegalMoveError(f"{COLUMNS[column]}{row + 1} 已有棋子")

        mark = MARKS[state.turn]
        state.board[row][column] = mark
        state.move_count += 1
        state.last_move = f"{COLUMNS[column]}{row + 1}"
        state.last_player = player

        if self._has_five(state.board, row, column, mark):
            state.winner = player
        elif state.move_count == BOARD_SIZE * BOARD_SIZE:
            state.is_draw = True
        else:
            state.turn = 1 - state.turn

    def is_over(self, state: GomokuState) -> bool:
        return state.winner is not None or state.is_draw

    def score(self, state: GomokuState) -> dict[str, float]:
        if not self.is_over(state):
            raise ValueError("对局尚未结束，不能计分")
        if state.winner is None:
            return {player: 0.5 for player in state.players}
        return {player: 1.0 if player == state.winner else 0.0 for player in state.players}

    @staticmethod
    def _has_five(
        board: list[list[str]], row: int, column: int, mark: str
    ) -> bool:
        for row_step, column_step in _WIN_AXES:
            contiguous = 1
            for direction in (-1, 1):
                next_row = row + direction * row_step
                next_column = column + direction * column_step
                while (
                    0 <= next_row < BOARD_SIZE
                    and 0 <= next_column < BOARD_SIZE
                    and board[next_row][next_column] == mark
                ):
                    contiguous += 1
                    next_row += direction * row_step
                    next_column += direction * column_step
            if contiguous >= 5:
                return True
        return False
