"""Provider 统一接口：加一个模型 = 加一个适配器。"""

from __future__ import annotations

import asyncio
import ipaddress
from abc import ABC, abstractmethod
from urllib.parse import urlsplit

DEFAULT_MAX_OUTPUT_TOKENS = 1024


class ProviderConfigurationError(ValueError):
    """Provider 配置缺失或不安全，可直接转换为 CLI 参数错误。"""


def validate_base_url(
    value: str,
    *,
    source: str,
    require_https_for_remote: bool = False,
) -> str:
    """校验 Provider HTTP(S) 端点，禁止把凭据嵌入 URL。"""

    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProviderConfigurationError(f"{source} 必须是完整的 http:// 或 https:// URL")
    if parsed.username is not None or parsed.password is not None:
        raise ProviderConfigurationError(f"{source} 不能在 URL 中嵌入用户名或密码")
    if parsed.query or parsed.fragment:
        raise ProviderConfigurationError(f"{source} 不能包含查询参数或 URL 片段")
    if require_https_for_remote and parsed.scheme == "http":
        hostname = parsed.hostname
        is_loopback = hostname == "localhost"
        if not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                is_loopback = False
        if not is_loopback:
            raise ProviderConfigurationError(
                f"{source} 携带 API Key 时远程端点必须使用 https://；"
                "http:// 仅允许 localhost、127.0.0.0/8 或 ::1"
            )
    return value.rstrip("/")


class ProviderTimeoutError(TimeoutError):
    """Provider 的原生异步请求超过调用方给定的截止时间。"""


class Provider(ABC):
    """把各家模型 API 统一成同步与异步 chat 调用。

    内置 Provider 实现原生 ``achat``，使比赛能真正取消超时请求。同步 ``chat``
    继续保留，兼容脚本和第三方适配器；默认异步实现仅用于未启用硬超时的旧适配器。
    """

    name: str = "abstract"
    # 命名 Profile 实例会设置这个安全标识；永远不在 Provider
    # 对象上暴露 Profile 解析出的 API Key。
    profile_id: str | None = None

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
