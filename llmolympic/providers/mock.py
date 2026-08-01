"""Mock Provider：离线演示与测试用，不依赖任何 API key。

策略：
- ``random``：选择题随机选 A-D，五子棋随机选空位，其他题随机输出整数；
- ``fixed``：选择题答 A，五子棋优先中心后选首个空位，其他题答 42；
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
