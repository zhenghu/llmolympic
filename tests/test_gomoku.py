"""Unit tests for the isolated 15 x 15 Gomoku core."""

from __future__ import annotations

import pytest

from llmolympic.core.game import FORFEIT_MOVE, IllegalMoveError
from llmolympic.games.gomoku import (
    BLACK,
    BOARD_SIZE,
    EMPTY,
    WHITE,
    Gomoku,
)
from llmolympic.providers.mock import MockProvider

PLAYERS = ["黑方", "白方"]


def _play_black_line(black_moves: list[str], white_moves: list[str]):
    game = Gomoku()
    state = game.new_state(PLAYERS, seed=0)
    for index, move in enumerate(black_moves):
        game.apply_move(state, PLAYERS[0], move)
        if game.is_over(state):
            break
        game.apply_move(state, PLAYERS[1], white_moves[index])
    return game, state


def _contains_five_window(board: list[list[str]]) -> bool:
    directions = ((0, 1), (1, 0), (1, 1), (1, -1))
    for row in range(BOARD_SIZE):
        for column in range(BOARD_SIZE):
            for row_step, column_step in directions:
                cells = []
                for offset in range(5):
                    next_row = row + offset * row_step
                    next_column = column + offset * column_step
                    if not (0 <= next_row < BOARD_SIZE and 0 <= next_column < BOARD_SIZE):
                        break
                    cells.append(board[next_row][next_column])
                if len(cells) == 5 and cells[0] != EMPTY and len(set(cells)) == 1:
                    return True
    return False


def test_new_state_is_empty_and_assigns_black_first() -> None:
    game = Gomoku()
    state = game.new_state(PLAYERS, seed=19)

    assert state.players == PLAYERS
    assert state.players is not PLAYERS
    assert state.seed == 19
    assert len(state.board) == BOARD_SIZE
    assert all(len(row) == BOARD_SIZE for row in state.board)
    assert all(cell == EMPTY for row in state.board for cell in row)
    assert game.current_players(state) == ["黑方"]


def test_new_states_do_not_share_board_rows() -> None:
    game = Gomoku()
    first = game.new_state(PLAYERS, seed=0)
    second = game.new_state(PLAYERS, seed=0)

    game.apply_move(first, "黑方", "A1")

    assert first.board[0][0] == BLACK
    assert second.board[0][0] == EMPTY


@pytest.mark.parametrize("players", [[], ["甲"], ["甲", "乙", "丙"], ["甲", "甲"]])
def test_new_state_requires_exactly_two_unique_players(players: list[str]) -> None:
    with pytest.raises(ValueError):
        Gomoku().new_state(players, seed=0)


@pytest.mark.parametrize(
    ("move", "row", "column"),
    [
        ("A1", 0, 0),
        ("  o15  ", 14, 14),
        ("H 8", 7, 7),
        ("H,8", 7, 7),
        ("( h, 8 )", 7, 7),
        ("`H8`", 7, 7),
        ('{"move": "H8"}', 7, 7),
        ("我的落子是 H8。", 7, 7),
        ("Ｈ８", 7, 7),
        ("H8 / H8", 7, 7),
    ],
)
def test_friendly_coordinate_forms_are_accepted(move: str, row: int, column: int) -> None:
    game = Gomoku()
    state = game.new_state(PLAYERS, seed=0)

    game.apply_move(state, "黑方", move)

    assert state.board[row][column] == BLACK
    assert game.current_players(state) == ["白方"]


@pytest.mark.parametrize(
    "move",
    [
        "",
        "P1",
        "A0",
        "A16",
        "H08",
        "H80",
        "1A",
        "CH8",
        "G7 或 H8",
        "A,,1",
    ],
)
def test_malformed_or_explanatory_moves_do_not_mutate_state(move: str) -> None:
    game = Gomoku()
    state = game.new_state(PLAYERS, seed=0)
    before = state.model_dump()

    with pytest.raises(IllegalMoveError):
        game.apply_move(state, "黑方", move)

    assert state.model_dump() == before


def test_wrong_turn_and_occupied_cell_do_not_mutate_state() -> None:
    game = Gomoku()
    state = game.new_state(PLAYERS, seed=0)

    before = state.model_dump()
    with pytest.raises(IllegalMoveError, match="当前应由"):
        game.apply_move(state, "白方", "A1")
    assert state.model_dump() == before

    game.apply_move(state, "黑方", "A1")
    after_black = state.model_dump()
    with pytest.raises(IllegalMoveError, match="已有棋子"):
        game.apply_move(state, "白方", "a1")
    assert state.model_dump() == after_black


def test_turns_alternate_and_stones_use_x_then_o() -> None:
    game = Gomoku()
    state = game.new_state(PLAYERS, seed=0)

    game.apply_move(state, "黑方", "A1")
    game.apply_move(state, "白方", "B1")

    assert state.board[0][0] == BLACK
    assert state.board[0][1] == WHITE
    assert state.move_count == 2
    assert game.current_players(state) == ["黑方"]


@pytest.mark.parametrize(
    ("black_moves", "white_moves"),
    [
        (["A1", "B1", "C1", "D1", "E1"], ["A15", "B15", "C15", "D15"]),
        (["A1", "A2", "A3", "A4", "A5"], ["O15", "N15", "M15", "L15"]),
        (["A1", "B2", "C3", "D4", "E5"], ["O1", "N1", "M1", "L1"]),
        (["E1", "D2", "C3", "B4", "A5"], ["O15", "N15", "M15", "L15"]),
    ],
)
def test_five_in_each_axis_wins(black_moves: list[str], white_moves: list[str]) -> None:
    game, state = _play_black_line(black_moves, white_moves)

    assert game.is_over(state)
    assert state.winner == "黑方"
    assert game.current_players(state) == []
    assert game.score(state) == {"黑方": 1.0, "白方": 0.0}


def test_four_contiguous_stones_do_not_end_the_game() -> None:
    game, state = _play_black_line(
        ["A1", "B1", "C1", "D1"],
        ["A15", "C15", "E15", "G15"],
    )

    assert not game.is_over(state)
    assert state.winner is None


def test_white_can_win_after_black_fails_to_make_a_line() -> None:
    game = Gomoku()
    state = game.new_state(PLAYERS, seed=0)
    black_moves = ["A15", "C15", "E15", "G15", "I15"]
    white_moves = ["A2", "B2", "C2", "D2", "E2"]

    for black_move, white_move in zip(black_moves, white_moves, strict=True):
        game.apply_move(state, "黑方", black_move)
        assert not game.is_over(state)
        game.apply_move(state, "白方", white_move)

    assert state.winner == "白方"
    assert game.score(state) == {"黑方": 0.0, "白方": 1.0}


def test_overline_wins_when_one_move_connects_six_stones() -> None:
    game, state = _play_black_line(
        ["A1", "B1", "C1", "E1", "F1", "D1"],
        ["A15", "C15", "E15", "G15", "I15"],
    )

    assert game.is_over(state)
    assert state.winner == "黑方"
    assert sum(cell == BLACK for cell in state.board[0]) == 6


def test_forfeit_immediately_awards_the_game_to_opponent() -> None:
    game = Gomoku()
    state = game.new_state(PLAYERS, seed=0)

    game.apply_move(state, "黑方", FORFEIT_MOVE)

    assert game.is_over(state)
    assert state.winner == "白方"
    assert state.forfeited_by == "黑方"
    assert state.move_count == 0
    assert game.score(state) == {"黑方": 0.0, "白方": 1.0}


def test_white_forfeit_awards_the_game_to_black() -> None:
    game = Gomoku()
    state = game.new_state(PLAYERS, seed=0)
    game.apply_move(state, "黑方", "H8")

    game.apply_move(state, "白方", FORFEIT_MOVE)

    assert state.winner == "黑方"
    assert state.forfeited_by == "白方"
    assert game.score(state) == {"黑方": 1.0, "白方": 0.0}


def test_full_board_without_five_is_a_draw() -> None:
    game = Gomoku()
    state = game.new_state(PLAYERS, seed=0)
    final_board = [
        [BLACK if (row + 2 * column) % 6 in {0, 1, 2} else WHITE for column in range(15)]
        for row in range(15)
    ]
    # 调整为黑 113 子、白 112 子；整个终局的四个方向都不存在五连。
    final_board[0][0] = WHITE
    final_board[0][1] = WHITE
    assert not _contains_five_window(final_board)

    moves = {
        mark: [
            f"{chr(ord('A') + column)}{row + 1}"
            for row in range(BOARD_SIZE)
            for column in range(BOARD_SIZE)
            if final_board[row][column] == mark
        ]
        for mark in (BLACK, WHITE)
    }
    assert len(moves[BLACK]) == 113
    assert len(moves[WHITE]) == 112
    for index, white_move in enumerate(moves[WHITE]):
        game.apply_move(state, "黑方", moves[BLACK][index])
        assert not game.is_over(state)
        game.apply_move(state, "白方", white_move)
        assert not game.is_over(state)
    game.apply_move(state, "黑方", moves[BLACK][-1])

    assert game.is_over(state)
    assert state.winner is None
    assert state.is_draw
    assert state.board == final_board
    assert state.move_count == BOARD_SIZE * BOARD_SIZE
    assert game.score(state) == {"黑方": 0.5, "白方": 0.5}


def test_score_before_terminal_state_raises() -> None:
    game = Gomoku()
    state = game.new_state(PLAYERS, seed=0)

    with pytest.raises(ValueError, match="尚未结束"):
        game.score(state)


def test_move_after_terminal_state_is_rejected_without_mutation() -> None:
    game = Gomoku()
    state = game.new_state(PLAYERS, seed=0)
    game.apply_move(state, "黑方", FORFEIT_MOVE)
    before = state.model_dump()

    with pytest.raises(IllegalMoveError, match="已经结束"):
        game.apply_move(state, "白方", "A1")

    assert state.model_dump() == before


def test_prompt_contains_the_entire_board_and_requests_only_coordinate() -> None:
    game = Gomoku()
    state = game.new_state(PLAYERS, seed=0)
    game.apply_move(state, "黑方", "H8")

    prompt = game.prompt_for(state, "白方")

    lines = prompt.splitlines()
    assert lines[0].startswith("五子棋（gomoku）")
    assert "A B C D E F G H I J K L M N O" in prompt
    header = next(line for line in lines if "A B C" in line)
    board_rows = [line for line in lines if line[:2].strip().isdigit()]
    assert len(board_rows) == BOARD_SIZE
    assert all(line.startswith(f"{row:>2}  ") for row, line in enumerate(board_rows, start=1))
    assert all(len(line.split()) == BOARD_SIZE + 1 for line in board_rows)
    assert all(set(line.split()[1:]) <= {EMPTY, BLACK, WHITE} for line in board_rows)
    row_eight = next(line for line in lines if line.startswith(" 8  "))
    assert header.index("A") == row_eight.index(".")
    assert row_eight.split()[8] == BLACK  # row label occupies token 0
    assert "你是 白方，执 O" in prompt
    assert "上一手：黑方（X）下在 H8" in prompt
    assert "连续 5 子或以上获胜" in prompt
    assert "满盘无人获胜则和棋" in prompt
    assert "只输出一个未占用坐标" in prompt
    assert "不要解释" in prompt


def test_prompt_rejects_wrong_player_and_terminal_state() -> None:
    game = Gomoku()
    state = game.new_state(PLAYERS, seed=0)

    with pytest.raises(ValueError, match="当前应由"):
        game.prompt_for(state, "白方")

    game.apply_move(state, "黑方", FORFEIT_MOVE)
    with pytest.raises(ValueError, match="已经结束"):
        game.prompt_for(state, "白方")


def test_mock_strategies_choose_legal_empty_cells_from_prompt() -> None:
    game = Gomoku()
    state = game.new_state(PLAYERS, seed=0)
    fixed = MockProvider(strategy="fixed")
    random = MockProvider(strategy="random", seed=3)

    black_prompt = game.prompt_for(state, "黑方")
    black_move = fixed.chat([{"role": "user", "content": black_prompt}], model="fixed")
    assert black_move == "H8"
    game.apply_move(state, "黑方", black_move)

    white_prompt = game.prompt_for(state, "白方")
    white_move = random.chat([{"role": "user", "content": white_prompt}], model="random")
    assert white_move != "H8"
    game.apply_move(state, "白方", white_move)
    assert state.move_count == 2
