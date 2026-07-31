"""Provider 统一接口：加一个模型 = 加一个适配器。"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Provider(ABC):
    """把各家模型 API 统一成一个同步阻塞的 chat 调用。

    同步接口由调用方（LLMPlayer）放到线程里执行，保持引擎的事件循环不被卡住。
    """

    name: str = "abstract"

    @abstractmethod
    def chat(self, messages: list[dict], *, model: str, **params) -> str:
        """发送对话消息，返回模型的文本回复。``params`` 为采样参数（temperature 等）。"""
