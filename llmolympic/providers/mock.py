"""Mock Provider：离线演示与测试用，不依赖任何 API key。

策略：
- ``random``：选择题随机选 A-D，其他题随机输出整数；
- ``fixed``：固定应答（选择题答 A，其他答 42）；
- ``illegal``：永远输出非法走法 "Z"，用于测试重试/判负逻辑。
"""

from __future__ import annotations

import random
import re

from llmolympic.providers.base import Provider

_CHOICE_RE = re.compile(r"^A[.、)]", re.MULTILINE)


class MockProvider(Provider):
    name = "mock"

    def __init__(self, strategy: str = "random", seed: int | None = None) -> None:
        if strategy not in ("random", "fixed", "illegal"):
            raise ValueError(f"未知 mock 策略: {strategy!r}")
        self.strategy = strategy
        self._rng = random.Random(seed)

    def chat(self, messages: list[dict], *, model: str, **params) -> str:
        prompt = messages[-1]["content"]
        is_choice = bool(_CHOICE_RE.search(prompt))
        if self.strategy == "illegal":
            return "Z"
        if self.strategy == "fixed":
            return "A" if is_choice else "42"
        return self._rng.choice("ABCD") if is_choice else str(self._rng.randint(0, 99))
