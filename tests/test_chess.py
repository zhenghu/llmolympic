"""International chess rules, match orchestration, and color-swap tests."""

from __future__ import annotations

import asyncio

import chess as chesslib
import pytest

from llmolympic.core.events import EventType
from llmolympic.core.game import FORFEIT_MOVE, IllegalMoveError
from llmolympic.core.match import play_match
from llmolympic.core.player import LLMPlayer, Player
from llmolympic.core.series import play_two_leg_series
from llmolympic.games.chess import Chess, ChessState
from llmolympic.providers.mock import MockProvider

PLAYERS = ["白方", "黑方"]


def _play(game: Chess, state: ChessState, moves: list[str]) -> None:
    for move in moves:
        [player] = game.current_players(state)
        game.apply_move(state, player, move)


class _SequencePlayer(Player):
    kind = "scripted"

    def __init__(self, name: str, moves: list[str]) -> None:
        super().__init__(name)
        self._moves = iter(moves)

    async def get_move(self, prompt: str) -> str:
        return next(self._moves)


def _mock_player(name: str, strategy: str) -> LLMPlayer:
    return LLMPlayer(name, MockProvider(strategy), strategy)


def test_new_state_assigns_white_first_and_has_machine_readable_prompt() -> None:
    game = Chess()
    state = game.new_state(PLAYERS, seed=19)

    assert isinstance(state, ChessState)
    assert state.players == PLAYERS
    assert state.players is not PLAYERS
    assert state.seed == 19
    assert state.current_fen == chesslib.STARTING_FEN
    assert state.moves_uci == []
    assert game.current_players(state) == ["白方"]

    prompt = game.prompt_for(state, "白方")
    assert "执白方" in prompt
    assert "FEN:" in prompt
    assert "LEGAL_MOVES_UCI:" in prompt
    assert "e2e4" in prompt
    assert game.describe_config()["draw_policy"] == "automatic_claim"


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [({"initial_fen": None}, "initial_fen"), ({"claim_draw": "false"}, "claim_draw")],
)
def test_constructor_rejects_invalid_public_argument_types(kwargs: dict, error: str) -> None:
    with pytest.raises(TypeError, match=error):
        Chess(**kwargs)


@pytest.mark.parametrize("players", [[], ["甲"], ["甲", "乙", "丙"], ["甲", "甲"]])
def test_new_state_requires_exactly_two_unique_players(players: list[str]) -> None:
    with pytest.raises(ValueError):
        Chess().new_state(players, seed=0)


def test_san_and_uci_are_canonicalized_and_turns_alternate() -> None:
    game = Chess()
    state = game.new_state(PLAYERS, seed=0)

    game.apply_move(state, "白方", " e4 ")
    game.apply_move(state, "黑方", "E7E5")

    assert state.moves_san == ["e4", "e5"]
    assert state.moves_uci == ["e2e4", "e7e5"]
    assert game.current_players(state) == ["白方"]
    board = chesslib.Board(state.current_fen)
    assert board.piece_at(chesslib.E4) == chesslib.Piece(chesslib.PAWN, chesslib.WHITE)
    assert board.piece_at(chesslib.E5) == chesslib.Piece(chesslib.PAWN, chesslib.BLACK)
    assert "e5（e7e5）" in game.prompt_for(state, "白方")


@pytest.mark.parametrize(
    "move",
    ["", "e2e5", "Qh5", "e4 e5", "I play e4", "0000", "--", "Z0", "@@@@"],
)
def test_malformed_or_illegal_move_does_not_mutate_state(move: str) -> None:
    game = Chess()
    state = game.new_state(PLAYERS, seed=0)
    before = state.model_dump()

    with pytest.raises(IllegalMoveError):
        game.apply_move(state, "白方", move)

    assert state.model_dump() == before


def test_wrong_player_and_ambiguous_san_do_not_mutate_state() -> None:
    game = Chess()
    state = game.new_state(PLAYERS, seed=0)
    before = state.model_dump()

    with pytest.raises(IllegalMoveError, match="当前应由"):
        game.apply_move(state, "黑方", "e5")
    assert state.model_dump() == before

    ambiguous = Chess(initial_fen="4k3/8/8/8/8/8/8/1N2KN2 w - - 0 1")
    ambiguous_state = ambiguous.new_state(PLAYERS, seed=0)
    before_ambiguous = ambiguous_state.model_dump()
    with pytest.raises(IllegalMoveError):
        ambiguous.apply_move(ambiguous_state, "白方", "Nd2")
    assert ambiguous_state.model_dump() == before_ambiguous

    ambiguous.apply_move(ambiguous_state, "白方", "Nbd2")
    assert ambiguous_state.moves_uci == ["b1d2"]


def test_replay_rejects_a_forged_null_move_history() -> None:
    game = Chess()
    state = game.new_state(PLAYERS, seed=0)
    forged = chesslib.Board()
    forged.push(chesslib.Move.null())
    state.moves_uci = ["0000"]
    state.moves_san = ["--"]
    state.current_fen = forged.fen(en_passant="fen")

    with pytest.raises(ValueError, match="无法重放"):
        game.current_players(state)


def test_kingside_castling_moves_both_king_and_rook() -> None:
    game = Chess()
    state = game.new_state(PLAYERS, seed=0)

    _play(game, state, ["e4", "e5", "Nf3", "Nc6", "Bc4", "Nf6", "O-O"])

    board = chesslib.Board(state.current_fen)
    assert state.moves_uci[-1] == "e1g1"
    assert state.moves_san[-1] == "O-O"
    assert board.piece_at(chesslib.G1) == chesslib.Piece(chesslib.KING, chesslib.WHITE)
    assert board.piece_at(chesslib.F1) == chesslib.Piece(chesslib.ROOK, chesslib.WHITE)


def test_castling_through_check_is_rejected_without_mutation() -> None:
    game = Chess(initial_fen="r3k2r/8/b7/8/8/8/8/R3K2R w KQkq - 0 1")
    state = game.new_state(PLAYERS, seed=0)
    before = state.model_dump()

    with pytest.raises(IllegalMoveError):
        game.apply_move(state, "白方", "O-O")

    assert state.model_dump() == before


@pytest.mark.parametrize(
    ("move", "uci", "piece_type"),
    [("a8=Q+", "a7a8q", chesslib.QUEEN), ("a7a8n", "a7a8n", chesslib.KNIGHT)],
)
def test_promotion_supports_san_uci_and_underpromotion(
    move: str, uci: str, piece_type: chesslib.PieceType
) -> None:
    game = Chess(initial_fen="4k3/P7/8/8/8/8/8/4K3 w - - 0 1")
    state = game.new_state(PLAYERS, seed=0)

    game.apply_move(state, "白方", move)

    board = chesslib.Board(state.current_fen)
    assert state.moves_uci == [uci]
    assert board.piece_at(chesslib.A8) == chesslib.Piece(piece_type, chesslib.WHITE)


def test_promotion_without_piece_is_rejected_without_mutation() -> None:
    game = Chess(initial_fen="4k3/P7/8/8/8/8/8/4K3 w - - 0 1")
    state = game.new_state(PLAYERS, seed=0)
    before = state.model_dump()

    with pytest.raises(IllegalMoveError):
        game.apply_move(state, "白方", "a7a8")

    assert state.model_dump() == before


def test_en_passant_capture_and_one_move_expiry() -> None:
    game = Chess()
    state = game.new_state(PLAYERS, seed=0)
    _play(game, state, ["e4", "a6", "e5", "d5", "exd6"])

    board = chesslib.Board(state.current_fen)
    assert state.moves_uci[-1] == "e5d6"
    assert board.piece_at(chesslib.D6) == chesslib.Piece(chesslib.PAWN, chesslib.WHITE)
    assert board.piece_at(chesslib.D5) is None

    expired = game.new_state(PLAYERS, seed=0)
    _play(game, expired, ["e4", "a6", "e5", "d5", "Nf3", "a5"])
    before = expired.model_dump()
    with pytest.raises(IllegalMoveError):
        game.apply_move(expired, "白方", "exd6")
    assert expired.model_dump() == before


def test_check_is_reported_but_does_not_end_the_game() -> None:
    game = Chess()
    state = game.new_state(PLAYERS, seed=0)

    _play(game, state, ["e4", "f6", "Qh5+"])

    assert not game.is_over(state)
    assert game.current_players(state) == ["黑方"]
    assert "当前是否被将军：是" in game.prompt_for(state, "黑方")


def test_checkmate_scores_the_winner_and_rejects_later_moves() -> None:
    game = Chess()
    state = game.new_state(PLAYERS, seed=0)
    _play(game, state, ["f3", "e5", "g4", "Qh4#"])

    assert game.is_over(state)
    assert state.termination == "checkmate"
    assert state.winner == "黑方"
    assert game.current_players(state) == []
    assert game.score(state) == {"白方": 0.0, "黑方": 1.0}

    before = state.model_dump()
    with pytest.raises(IllegalMoveError, match="已经结束"):
        game.apply_move(state, "白方", "e4")
    assert state.model_dump() == before


@pytest.mark.parametrize(
    ("fen", "termination"),
    [
        ("7k/5K2/6Q1/8/8/8/8/8 b - - 0 1", "stalemate"),
        ("4k3/8/8/8/8/8/8/4K3 w - - 0 1", "insufficient_material"),
        ("4kb2/8/8/8/8/8/8/2B1K3 w - - 0 1", "insufficient_material"),
    ],
)
def test_drawn_initial_positions_are_scored_as_draws(fen: str, termination: str) -> None:
    game = Chess(initial_fen=fen)
    state = game.new_state(PLAYERS, seed=0)

    assert state.is_draw
    assert state.termination == termination
    assert game.current_players(state) == []
    assert game.score(state) == {"白方": 0.5, "黑方": 0.5}


def test_bishop_and_knight_are_not_insufficient_material() -> None:
    game = Chess(initial_fen="4k3/8/8/8/8/8/8/1NB1K3 w - - 0 1")
    state = game.new_state(PLAYERS, seed=0)

    assert not game.is_over(state)
    assert state.termination is None


def test_threefold_claim_and_automatic_fivefold_repetition() -> None:
    cycle = ["Nf3", "Nf6", "Ng1", "Ng8"]

    claim_game = Chess(claim_draw=True)
    claim_state = claim_game.new_state(PLAYERS, seed=0)
    _play(claim_game, claim_state, [*cycle, "Nf3", "Nf6", "Ng1"])
    assert claim_state.is_draw
    assert claim_state.termination == "threefold_repetition"
    assert len(claim_state.moves_uci) == 7

    automatic_game = Chess(claim_draw=False)
    automatic_state = automatic_game.new_state(PLAYERS, seed=0)
    _play(automatic_game, automatic_state, cycle * 4)
    assert automatic_state.is_draw
    assert automatic_state.termination == "fivefold_repetition"
    assert len(automatic_state.moves_uci) == 16


@pytest.mark.parametrize(
    ("fen", "claim_draw", "termination"),
    [
        ("4k3/8/8/8/8/8/8/R3K3 w - - 99 50", True, "fifty_moves"),
        ("4k3/8/8/8/8/8/8/R3K3 w - - 150 76", False, "seventyfive_moves"),
    ],
)
def test_move_count_draw_rules(fen: str, claim_draw: bool, termination: str) -> None:
    game = Chess(initial_fen=fen, claim_draw=claim_draw)
    state = game.new_state(PLAYERS, seed=0)

    assert state.is_draw
    assert state.termination == termination
    assert game.score(state) == {"白方": 0.5, "黑方": 0.5}


def test_forfeit_immediately_awards_the_game_to_the_opponent() -> None:
    game = Chess()
    state = game.new_state(PLAYERS, seed=0)

    game.apply_move(state, "白方", FORFEIT_MOVE)

    assert state.winner == "黑方"
    assert state.forfeited_by == "白方"
    assert state.termination == "forfeit"
    assert state.moves_uci == []
    assert game.score(state) == {"白方": 0.0, "黑方": 1.0}


def test_match_archives_checkmate_with_ordered_events() -> None:
    white = _SequencePlayer("white", ["f3", "g4"])
    black = _SequencePlayer("black", ["e5", "Qh4#"])

    archive = asyncio.run(play_match(Chess(), [white, black], seed=11))

    assert archive.game == "chess"
    assert archive.scores == {"white": 0.0, "black": 1.0}
    assert [record.move for record in archive.moves] == ["f3", "e5", "g4", "Qh4#"]
    assert all(record.accepted for record in archive.moves)
    assert archive.events[0].type == EventType.MATCH_STARTED
    assert archive.events[-1].type == EventType.MATCH_FINISHED
    assert archive.events[-1].data["termination"] == "completed"
    assert [event.seq for event in archive.events] == list(range(len(archive.events)))


def test_repeated_illegal_chess_moves_become_a_technical_loss() -> None:
    bad = _SequencePlayer("bad", ["not-a-move", "still-not-a-move"])
    good = _SequencePlayer("good", [])

    archive = asyncio.run(play_match(Chess(), [bad, good], max_attempts=2))

    assert archive.scores == {"bad": 0.0, "good": 1.0}
    assert len(archive.moves) == 2
    assert all(not record.accepted for record in archive.moves)
    rejected = [event for event in archive.events if event.type == EventType.MOVE_REJECTED]
    assert [event.data["reason_code"] for event in rejected] == [
        "illegal_move",
        "illegal_move_limit",
    ]
    assert rejected[-1].data["forfeit_scope"] == "match"
    assert rejected[-1].data["technical_loss"] is True
    assert archive.events[-1].data["termination"] == "technical_loss"
    assert archive.events[-1].data["forfeited_by"] == "bad"


def test_mock_two_leg_series_completes_and_swaps_white_and_black() -> None:
    player_a = _mock_player("甲", "fixed")
    player_b = _mock_player("乙", "fixed")

    series = asyncio.run(play_two_leg_series(Chess(), [player_a, player_b], seed=23))

    assert [[descriptor["name"] for descriptor in leg.players] for leg in series.legs] == [
        ["甲", "乙"],
        ["乙", "甲"],
    ]
    assert [leg.seed for leg in series.legs] == [23, 23]
    assert all(leg.events[-1].data["termination"] == "completed" for leg in series.legs)
    assert all(leg.moves and all(move.accepted for move in leg.moves) for leg in series.legs)

    first_prompts = [
        next(event for event in leg.events if event.type == EventType.TURN_PROMPT)
        for leg in series.legs
    ]
    assert [(event.player, "执白方" in event.data["prompt"]) for event in first_prompts] == [
        ("甲", True),
        ("乙", True),
    ]
    assert series.points == {"甲": 1.0, "乙": 1.0}
