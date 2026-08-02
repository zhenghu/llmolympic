"""Provider 工厂。"""

from __future__ import annotations

import os

from llmolympic.config import ProviderProfile
from llmolympic.providers.base import Provider
from llmolympic.providers.base import ProviderConfigurationError as ProviderConfigurationError
from llmolympic.providers.base import ProviderTimeoutError as ProviderTimeoutError
from llmolympic.providers.mock import MockProvider
from llmolympic.providers.ollama_provider import OllamaProvider
from llmolympic.providers.openai_provider import OpenAIProvider


def create_provider(kind: str, model: str = "") -> Provider:
    """按 CLI 选手规格里的类型名创建 provider。

    ``mock`` 的 ``model`` 段被解释为策略名（random/fixed/illegal）。
    """
    if kind == "mock":
        return MockProvider(strategy=model or "random")
    if kind == "openai":
        return OpenAIProvider()
    if kind == "ollama":
        return OllamaProvider()
    raise ValueError(f"未知 provider {kind!r}，可选: mock, openai, ollama, human")


def create_profile_provider(profile: ProviderProfile) -> Provider:
    """从命名 Profile 创建隔离的 Provider 实例。

    Profile 只保存环境变量名；Key 在这个边界临时解析，既不回写
    Profile，也不作为 Provider 的公开属性。
    """

    if profile.provider == "openai":
        if profile.api_key_env is None:  # load_profiles 已校验，防御手工构造
            raise ProviderConfigurationError(
                f"OpenAI 兼容 Profile {profile.profile_id!r} 未声明 api_key_env"
            )
        api_key = os.environ.get(profile.api_key_env)
        if not api_key:
            raise ProviderConfigurationError(
                f"Provider Profile {profile.profile_id!r} 需要环境变量 "
                f"{profile.api_key_env}，但当前未设置"
            )
        return OpenAIProvider(
            api_key=api_key,
            base_url=profile.base_url,
            profile_id=profile.profile_id,
            use_legacy_config=False,
        )
    if profile.provider == "ollama":
        return OllamaProvider(
            base_url=profile.base_url,
            profile_id=profile.profile_id,
            use_legacy_config=False,
        )
    raise ProviderConfigurationError(
        f"Provider Profile {profile.profile_id!r} 使用了不支持的 provider {profile.provider!r}"
    )
