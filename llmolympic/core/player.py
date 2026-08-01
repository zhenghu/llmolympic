"""选手抽象：引擎不区分人类与模型。"""

from __future__ import annotations

import asyncio
import inspect
import math
from abc import ABC, abstractmethod
from collections.abc import Callable

from llmolympic.providers.base import Provider, ProviderTimeoutError

SYSTEM_PROMPT = (
    "你是 LLM Olympics 的一名参赛选手。请仔细阅读题面，"
    "只输出最终答案本身，不要解释过程，不要输出多余文字。"
)

DEFAULT_LLM_TIMEOUT_SECONDS = 120.0


class PlayerActionError(Exception):
    """选手在生成走法时失败，并携带可存档的机器信息。"""

    reason_code = "player_error"

    def __init__(
        self,
        message: str,
        *,
        technical_loss: bool = False,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.technical_loss = technical_loss
        self.details = dict(details or {})


class PlayerTimeoutError(PlayerActionError):
    """选手未在限时内提交走法。"""

    reason_code = "timeout"


class PlayerProviderError(PlayerActionError):
    """模型 Provider 在生成走法时失败。"""

    reason_code = "provider_error"


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

    def __init__(
        self,
        name: str,
        provider: Provider,
        model: str,
        *,
        move_timeout_seconds: float | None = DEFAULT_LLM_TIMEOUT_SECONDS,
        **sampling_params,
    ) -> None:
        super().__init__(name)
        if "request_timeout" in sampling_params:
            raise ValueError(
                "request_timeout 是 Provider 内部保留参数；请使用 move_timeout_seconds"
            )
        if move_timeout_seconds is not None and (
            not math.isfinite(move_timeout_seconds) or move_timeout_seconds <= 0
        ):
            raise ValueError("LLM 单步超时必须是大于 0 的有限秒数")
        if move_timeout_seconds is not None:
            async_implementation = getattr(type(provider), "achat", None)
            if (
                async_implementation is None
                or async_implementation is Provider.achat
                or not inspect.iscoroutinefunction(async_implementation)
            ):
                raise ValueError(
                    f"Provider {provider.name!r} 没有原生异步调用，不能启用可靠的 LLM 超时"
                )
        self.provider = provider
        self.model = model
        self.move_timeout_seconds = move_timeout_seconds
        self.sampling_params = sampling_params

    async def get_move(self, prompt: str) -> str:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        try:
            async_chat = getattr(self.provider, "achat", None)
            if async_chat is None:
                return await asyncio.to_thread(
                    self.provider.chat,
                    messages,
                    model=self.model,
                    **self.sampling_params,
                )
            call = async_chat(
                messages,
                model=self.model,
                request_timeout=self.move_timeout_seconds,
                **self.sampling_params,
            )
            if self.move_timeout_seconds is None:
                return await call
            async with asyncio.timeout(self.move_timeout_seconds):
                return await call
        except ProviderTimeoutError as exc:
            raise self._timeout_error() from exc
        except TimeoutError as exc:
            raise self._timeout_error() from exc
        except Exception as exc:
            raise PlayerProviderError(
                f"{self.provider.name} 模型服务调用失败，判技术负",
                technical_loss=True,
                details={
                    "provider": self.provider.name,
                    "model": self.model,
                    "error_type": type(exc).__name__,
                },
            ) from exc

    def _timeout_error(self) -> PlayerTimeoutError:
        details: dict[str, object] = {
            "provider": self.provider.name,
            "model": self.model,
        }
        if self.move_timeout_seconds is None:
            message = f"{self.provider.name} 模型请求超时，判技术负"
        else:
            message = (
                f"{self.provider.name} 模型未在 {self.move_timeout_seconds:g} 秒内响应，"
                "判技术负"
            )
            details["timeout_seconds"] = self.move_timeout_seconds
        return PlayerTimeoutError(
            message,
            technical_loss=True,
            details=details,
        )

    def describe(self) -> dict:
        description = {
            "name": self.name,
            "kind": self.kind,
            "provider": self.provider.name,
            "model": self.model,
            "sampling_params": self.sampling_params,
        }
        if self.move_timeout_seconds is not None:
            description["move_timeout_seconds"] = self.move_timeout_seconds
        return description


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
        if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("人类行动超时必须是大于 0 的有限秒数")
        self.timeout = timeout
        self._input_fn = input_fn

    async def get_move(self, prompt: str) -> str:
        try:
            coro = asyncio.to_thread(self._input_fn, f"{self.name}，请输入你的答案 > ")
            return await asyncio.wait_for(coro, timeout=self.timeout)
        except TimeoutError as exc:  # 3.11+ 中 wait_for 超时抛内置 TimeoutError
            raise PlayerTimeoutError(
                f"{self.name} 超时未作答",
                details={"timeout_seconds": self.timeout},
            ) from exc
