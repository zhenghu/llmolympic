"""Mock Provider：离线演示与测试用，不依赖任何 API key。

策略：
- ``random``：选择题随机选 A-D，棋类随机选择合法走法，其他题随机输出整数；
- ``fixed``：选择题答 A，棋类使用确定性合法走法，其他题答 42；
- ``illegal``：永远输出非法走法 "Z"，用于测试重试/判负逻辑。
"""

from __future__ import annotations

import random
import re

from llmolympic.providers.base import Provider

_CHOICE_RE = re.compile(r"^A[.、)]", re.MULTILINE)
_GOMOKU_ROW_RE = re.compile(
    r"^\s*(1[0-5]|[1-9])\s+((?:[.XO]\s+){14}[.XO])\s*$",
    re.MULTILINE,
)
_CHESS_LEGAL_RE = re.compile(r"^LEGAL_MOVES_UCI:\s*([^\n]*)$", re.MULTILINE)
_UCI_TOKEN_RE = re.compile(r"[a-h][1-8][a-h][1-8][qrbn]?")
_CHESS_FIXED_PREFERENCES = (
    "e2e4",
    "e7e5",
    "g1f3",
    "b8c6",
    "f1c4",
    "g8f6",
    "e1g1",
)


def _gomoku_empty_cells(prompt: str) -> list[str]:
    """从 Gomoku 文本棋盘提取当前所有合法空位。"""
    if "gomoku" not in prompt.lower():
        return []

    cells: list[str] = []
    for match in _GOMOKU_ROW_RE.finditer(prompt):
        row = int(match.group(1))
        symbols = match.group(2).split()
        for column, symbol in enumerate(symbols):
            if symbol == ".":
                cells.append(f"{chr(ord('A') + column)}{row}")
    return cells


def _chess_legal_moves(prompt: str) -> list[str]:
    """Extract the explicit machine-readable legal-move line from a chess prompt."""

    if "国际象棋（chess）" not in prompt:
        return []
    match = _CHESS_LEGAL_RE.search(prompt)
    if match is None:
        return []
    tokens = match.group(1).split()
    if not tokens or any(_UCI_TOKEN_RE.fullmatch(token) is None for token in tokens):
        return []
    return tokens


class MockProvider(Provider):
    name = "mock"

    def __init__(self, strategy: str = "random", seed: int | None = None) -> None:
        if strategy not in ("random", "fixed", "illegal"):
            raise ValueError(f"未知 mock 策略: {strategy!r}")
        self.strategy = strategy
        self._rng = random.Random(seed)

    def chat(
        self,
        messages: list[dict],
        *,
        model: str,
        request_timeout: float | None = None,
        **params,
    ) -> str:
        prompt = messages[-1]["content"]
        if self.strategy == "illegal":
            return "Z"

        chess_moves = _chess_legal_moves(prompt)
        if chess_moves:
            if self.strategy == "fixed":
                return next(
                    (
                        move
                        for move in _CHESS_FIXED_PREFERENCES
                        if move in chess_moves
                    ),
                    chess_moves[0],
                )
            return self._rng.choice(chess_moves)

        gomoku_cells = _gomoku_empty_cells(prompt)
        if gomoku_cells:
            if self.strategy == "fixed":
                return "H8" if "H8" in gomoku_cells else gomoku_cells[0]
            return self._rng.choice(gomoku_cells)

        is_choice = bool(_CHOICE_RE.search(prompt))
        if self.strategy == "fixed":
            return "A" if is_choice else "42"
        return self._rng.choice("ABCD") if is_choice else str(self._rng.randint(0, 99))

    async def achat(
        self,
        messages: list[dict],
        *,
        model: str,
        request_timeout: float | None = None,
        **params,
    ) -> str:
        return self.chat(
            messages,
            model=model,
            request_timeout=request_timeout,
            **params,
        )
