"""Standard two-player chess backed by the :mod:`chess` rules engine.

The first player is White and the second player is Black.  A state keeps the
canonical UCI move history instead of a live ``Board`` object so it remains
serializable and repetition-based draws can still be reconstructed exactly.
"""

from __future__ import annotations

import re
import unicodedata

import chess as chesslib
from pydantic import Field

from llmolympic.core.game import FORFEIT_MOVE, GameState, IllegalMoveError

_UCI_RE = re.compile(r"[a-h][1-8][a-h][1-8][qrbn]?", re.IGNORECASE)
_TERMINATION_NAMES = {
    chesslib.Termination.CHECKMATE: "checkmate",
    chesslib.Termination.STALEMATE: "stalemate",
    chesslib.Termination.INSUFFICIENT_MATERIAL: "insufficient_material",
    chesslib.Termination.SEVENTYFIVE_MOVES: "seventyfive_moves",
    chesslib.Termination.FIVEFOLD_REPETITION: "fivefold_repetition",
    chesslib.Termination.FIFTY_MOVES: "fifty_moves",
    chesslib.Termination.THREEFOLD_REPETITION: "threefold_repetition",
    chesslib.Termination.VARIANT_WIN: "variant_win",
    chesslib.Termination.VARIANT_LOSS: "variant_loss",
    chesslib.Termination.VARIANT_DRAW: "variant_draw",
}


def _board_fen(board: chesslib.Board) -> str:
    """Return a stable FEN that retains the spec-defined en-passant square."""

    return board.fen(en_passant="fen")


def _render_board(board: chesslib.Board) -> str:
    """Render a deterministic board from White's perspective with coordinates."""

    rows: list[str] = []
    for rank in range(7, -1, -1):
        symbols: list[str] = []
        for file in range(8):
            piece = board.piece_at(chesslib.square(file, rank))
            symbols.append(piece.symbol() if piece is not None else ".")
        rows.append(f"{rank + 1}  {' '.join(symbols)}")
    return "\n".join([*rows, "   a b c d e f g h"])


class ChessState(GameState):
    """Serializable mutable state for one chess game."""

    initial_fen: str = chesslib.STARTING_FEN
    current_fen: str = chesslib.STARTING_FEN
    moves_uci: list[str] = Field(default_factory=list)
    moves_san: list[str] = Field(default_factory=list)
    winner: str | None = None
    is_draw: bool = False
    forfeited_by: str | None = None
    termination: str | None = None


class Chess:
    """Standard chess with strict single-move SAN/UCI input."""

    name = "chess"
    forfeit_scope = "match"
    min_players = 2
    max_players = 2

    def __init__(
        self,
        *,
        initial_fen: str = chesslib.STARTING_FEN,
        claim_draw: bool = True,
    ) -> None:
        if not isinstance(initial_fen, str):
            raise TypeError("initial_fen 必须是 FEN 字符串")
        if not isinstance(claim_draw, bool):
            raise TypeError("claim_draw 必须是布尔值")
        try:
            board = chesslib.Board(initial_fen)
        except ValueError as exc:
            raise ValueError(f"无效国际象棋初始 FEN: {initial_fen!r}") from exc
        if not board.is_valid():
            raise ValueError(f"国际象棋初始 FEN 不是有效标准局面: {initial_fen!r}")
        self.initial_fen = _board_fen(board)
        self.claim_draw = claim_draw

    def describe_config(self) -> dict[str, object]:
        return {
            "variant": "standard",
            "initial_fen": self.initial_fen,
            "draw_policy": "automatic_claim" if self.claim_draw else "automatic_only",
            "notations": ["san", "uci"],
            "rules_engine": "chess",
            "rules_engine_version": chesslib.__version__,
        }

    def new_state(self, players: list[str], seed: int) -> ChessState:
        if len(players) != 2:
            raise ValueError("国际象棋必须恰好有 2 名选手")
        if len(set(players)) != 2:
            raise ValueError("国际象棋的两名选手名字必须唯一")
        state = ChessState(
            players=list(players),
            seed=seed,
            initial_fen=self.initial_fen,
            current_fen=self.initial_fen,
        )
        self._record_outcome(state, chesslib.Board(self.initial_fen))
        return state

    def current_players(self, state: ChessState) -> list[str]:
        if self.is_over(state):
            return []
        board = self._board_from_state(state)
        index = 0 if board.turn == chesslib.WHITE else 1
        return [state.players[index]]

    def prompt_for(self, state: ChessState, player: str) -> str:
        if player not in state.players:
            raise ValueError(f"未知选手: {player!r}")
        if self.is_over(state):
            raise ValueError("对局已经结束，不能再生成走法提示")

        board = self._board_from_state(state)
        expected_index = 0 if board.turn == chesslib.WHITE else 1
        expected = state.players[expected_index]
        if player != expected:
            raise ValueError(f"当前应由 {expected} 走棋，不是 {player}")

        color = "白方" if expected_index == 0 else "黑方"
        opponent = state.players[1 - expected_index]
        opponent_color = "黑方" if expected_index == 0 else "白方"
        if state.moves_san:
            last_index = 1 - expected_index
            last = (
                f"{state.players[last_index]}（{'白方' if last_index == 0 else '黑方'}）"
                f"走 {state.moves_san[-1]}（{state.moves_uci[-1]}）"
            )
        else:
            last = "无"
        check = "是" if board.is_check() else "否"
        legal_moves = " ".join(sorted(move.uci() for move in board.legal_moves))
        if self.claim_draw:
            draw_policy = "三次重复和五十回合等可申诉和棋由竞技场自动提出。"
        else:
            draw_policy = "本局只执行规则自动终局，不自动提出可申诉和棋。"

        return (
            "国际象棋（chess）· 标准规则\n"
            f"你是 {player}，执{color}；对手 {opponent} 执{opponent_color}。白方先行。\n"
            "棋盘固定按白方视角显示：大写字母是白棋，小写字母是黑棋，`.` 是空格。\n"
            "棋子字母：K/k 王，Q/q 后，R/r 车，B/b 象，N/n 马，P/p 兵。\n"
            "接受一个完整 SAN（如 e4、Nf3、O-O、Qh7#）或 UCI（如 e2e4、e7e8q）。\n"
            f"{draw_policy}\n"
            f"上一手：{last}\n"
            f"当前是否被将军：{check}\n"
            f"FEN: {state.current_fen}\n"
            f"第 {board.fullmove_number} 回合，轮到{color}：\n{_render_board(board)}\n"
            f"LEGAL_MOVES_UCI: {legal_moves}\n"
            "只输出一个走法，不要解释，不要输出多步。"
        )

    def apply_move(self, state: ChessState, player: str, move: str) -> None:
        if self.is_over(state):
            raise IllegalMoveError("对局已经结束")

        board = self._board_from_state(state)
        expected_index = 0 if board.turn == chesslib.WHITE else 1
        expected = state.players[expected_index]
        if player != expected:
            raise IllegalMoveError(f"当前应由 {expected} 走棋，不是 {player}")

        if move == FORFEIT_MOVE:
            state.winner = state.players[1 - expected_index]
            state.forfeited_by = player
            state.termination = "forfeit"
            return

        parsed = self._parse_move(board, move)
        canonical_san = board.san(parsed)
        canonical_uci = parsed.uci()
        board.push(parsed)

        # Compute every derived value before mutating state.  Invalid input can
        # therefore never leave a partially advanced position behind.
        new_fen = _board_fen(board)
        outcome = board.outcome(claim_draw=self.claim_draw)

        state.moves_uci.append(canonical_uci)
        state.moves_san.append(canonical_san)
        state.current_fen = new_fen
        if outcome is not None:
            self._record_outcome(state, board, outcome=outcome)

    def is_over(self, state: ChessState) -> bool:
        return state.winner is not None or state.is_draw

    def score(self, state: ChessState) -> dict[str, float]:
        if not self.is_over(state):
            raise ValueError("对局尚未结束，不能计分")
        if state.winner is None:
            return {player: 0.5 for player in state.players}
        return {
            player: 1.0 if player == state.winner else 0.0
            for player in state.players
        }

    def _board_from_state(self, state: ChessState) -> chesslib.Board:
        if len(state.moves_uci) != len(state.moves_san):
            raise ValueError("国际象棋状态的 SAN 与 UCI 历史长度不一致")
        try:
            board = chesslib.Board(state.initial_fen)
            for uci, san in zip(state.moves_uci, state.moves_san, strict=True):
                parsed = board.parse_uci(uci)
                if parsed not in board.legal_moves:
                    raise ValueError("UCI 走法历史包含当前局面的非法着")
                if board.san(parsed) != san:
                    raise ValueError("SAN 与 UCI 走法历史不一致")
                board.push(parsed)
        except (ValueError, chesslib.IllegalMoveError) as exc:
            raise ValueError("国际象棋状态包含无法重放的走法历史") from exc
        if _board_fen(board) != state.current_fen:
            raise ValueError("国际象棋状态的 FEN 与走法历史不一致")
        return board

    @staticmethod
    def _parse_move(board: chesslib.Board, move: str) -> chesslib.Move:
        if not isinstance(move, str):
            raise IllegalMoveError("国际象棋走法必须是文本")
        candidate = unicodedata.normalize("NFKC", move).strip()
        if not candidate:
            raise IllegalMoveError("国际象棋走法不能为空")
        if candidate == "0000":
            raise IllegalMoveError("不接受 null move 0000")

        candidate = candidate.replace("0-0-0", "O-O-O").replace("0-0", "O-O")
        try:
            if _UCI_RE.fullmatch(candidate):
                parsed = board.parse_uci(candidate.lower())
            else:
                parsed = board.parse_san(candidate)
            # ``parse_san`` intentionally understands null-move spellings such
            # as ``--`` for analysis tools.  Null moves are never legal in a
            # standard game, so all parsers must pass the same legal-set check.
            if parsed not in board.legal_moves:
                raise ValueError("走法不在当前合法着集合中")
            return parsed
        except ValueError as exc:
            raise IllegalMoveError(
                f"非法国际象棋走法: {move!r}；请只输出一个合法 SAN 或 UCI"
            ) from exc

    def _record_outcome(
        self,
        state: ChessState,
        board: chesslib.Board,
        *,
        outcome: chesslib.Outcome | None = None,
    ) -> None:
        if outcome is None:
            outcome = board.outcome(claim_draw=self.claim_draw)
        if outcome is None:
            return

        state.termination = _TERMINATION_NAMES[outcome.termination]
        if outcome.winner is None:
            state.is_draw = True
        elif outcome.winner == chesslib.WHITE:
            state.winner = state.players[0]
        else:
            state.winner = state.players[1]
