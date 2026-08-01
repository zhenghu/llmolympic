"""Provider 统一接口：加一个模型 = 加一个适配器。"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod


class ProviderTimeoutError(TimeoutError):
    """Provider 的原生异步请求超过调用方给定的截止时间。"""


class Provider(ABC):
    """把各家模型 API 统一成同步与异步 chat 调用。

    内置 Provider 实现原生 ``achat``，使比赛能真正取消超时请求。同步 ``chat``
    继续保留，兼容脚本和第三方适配器；默认异步实现仅用于未启用硬超时的旧适配器。
    """

    name: str = "abstract"

    @abstractmethod
    def chat(
        self,
        messages: list[dict],
        *,
        model: str,
        request_timeout: float | None = None,
        **params,
    ) -> str:
        """同步发送消息；``request_timeout`` 是网络请求限时。"""

    async def achat(
        self,
        messages: list[dict],
        *,
        model: str,
        request_timeout: float | None = None,
        **params,
    ) -> str:
        """异步发送消息；旧适配器在未启用硬超时时回退到工作线程。"""
        call_params = dict(params)
        if request_timeout is not None:
            call_params["request_timeout"] = request_timeout
        return await asyncio.to_thread(
            self.chat,
            messages,
            model=model,
            **call_params,
        )
