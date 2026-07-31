"""选手抽象：引擎不区分人类与模型。"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 避免与 providers 包的运行时循环导入
    from llmolympic.providers.base import Provider

SYSTEM_PROMPT = (
    "你是 LLM Olympics 的一名参赛选手。请仔细阅读题面，"
    "只输出最终答案本身，不要解释过程，不要输出多余文字。"
)


class PlayerTimeoutError(Exception):
    """选手未在限时内提交走法。"""


class Player(ABC):
    """选手基类。

    ``get_move`` 是异步方法：人类选手将来可以经 API/WebSocket 远端提交
    走法，引擎无需改动（参见 DESIGN.md §4）。
    """

    kind: str = "abstract"

    def __init__(self, name: str) -> None:
        self.name = name

    @abstractmethod
    async def get_move(self, prompt: str) -> str:
        """看到题面后返回走法文本。超时抛 :class:`PlayerTimeoutError`。"""

    def describe(self) -> dict:
        """写入对局档案的选手描述（类型、模型、采样参数等）。"""
        return {"name": self.name, "kind": self.kind}


class LLMPlayer(Player):
    """经 Provider 调用大模型的选手。"""

    kind = "llm"

    def __init__(self, name: str, provider: Provider, model: str, **sampling_params) -> None:
        super().__init__(name)
        self.provider = provider
        self.model = model
        self.sampling_params = sampling_params

    async def get_move(self, prompt: str) -> str:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        # provider.chat 是同步阻塞调用，放到线程里避免卡住事件循环
        return await asyncio.to_thread(
            self.provider.chat, messages, model=self.model, **self.sampling_params
        )

    def describe(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "provider": self.provider.name,
            "model": self.model,
            "sampling_params": self.sampling_params,
        }


class HumanPlayer(Player):
    """人类选手：CLI 里等待键盘输入；接口已为将来 API 远端提交留好路。"""

    kind = "human"

    def __init__(
        self,
        name: str,
        timeout: float | None = 60.0,
        input_fn: Callable[[str], str] = input,
    ) -> None:
        super().__init__(name)
        self.timeout = timeout
        self._input_fn = input_fn

    async def get_move(self, prompt: str) -> str:
        try:
            coro = asyncio.to_thread(self._input_fn, f"{self.name}，请输入你的答案 > ")
            return await asyncio.wait_for(coro, timeout=self.timeout)
        except TimeoutError as exc:  # 3.11+ 中 wait_for 超时抛内置 TimeoutError
            raise PlayerTimeoutError(f"{self.name} 超时未作答") from exc
