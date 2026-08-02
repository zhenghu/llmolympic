"""配置文件加载：显式路径或项目根目录的 ``config.toml``。

查找顺序：``LLMOLYMPIC_CONFIG`` 环境变量指定的路径 → 项目根目录。不会搜索
当前工作目录，避免在不可信目录运行命令时误加载其中的端点或密钥配置。
取值优先级：环境变量 > config.toml > 默认值。
"""

from __future__ import annotations

import os
import re
import stat
import tomllib
import warnings
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_PROJECT_CONFIG = Path(__file__).resolve().parent.parent / "config.toml"
_PROFILE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_PROFILE_FIELDS = frozenset(
    {"provider", "default_model", "base_url", "api_key_env", "display_name"}
)
_PROFILE_PROVIDERS = frozenset({"openai", "ollama"})


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    """A named, credential-free Provider configuration.

    ``api_key_env`` stores only an environment-variable name. The credential value is
    resolved immediately before Provider construction and is never part of this object.
    """

    profile_id: str
    provider: str
    default_model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    display_name: str | None = None


def _warn_if_config_is_shared(path: Path) -> None:
    """在 POSIX 上提醒用户不要让含密钥的配置对组/其他用户可读。"""

    if os.name != "posix":
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return
    if mode & 0o077:
        warnings.warn(
            f"配置文件 {str(path)!r} 的权限为 {mode:04o}；建议执行 chmod 600",
            RuntimeWarning,
            stacklevel=2,
        )


def _find_config() -> Path | None:
    if env_path := os.environ.get("LLMOLYMPIC_CONFIG"):
        # 显式指定的路径是唯一来源，不存在就当没有配置
        path = Path(env_path)
        return path if path.is_file() else None
    return _PROJECT_CONFIG if _PROJECT_CONFIG.is_file() else None


@lru_cache
def load_config() -> dict:
    path = _find_config()
    if path is None:
        return {}
    _warn_if_config_is_shared(path)
    with path.open("rb") as f:
        return tomllib.load(f)


def get(section: str, key: str, default: str | None = None, env: str | None = None) -> str | None:
    """读取配置项。``env`` 指定时，同名环境变量优先于配置文件。"""
    if env and (value := os.environ.get(env)):
        return value
    return load_config().get(section, {}).get(key, default)


def _optional_profile_string(
    profile_id: str,
    values: dict,
    field: str,
) -> str | None:
    value = values.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Profile {profile_id!r} 的 {field} 必须是非空字符串")
    return value.strip()


def load_profiles() -> dict[str, ProviderProfile]:
    """读取并严格校验 ``[profiles.<id>]``，结果不包含凭据值。"""

    raw_profiles = load_config().get("profiles", {})
    if not isinstance(raw_profiles, dict):
        raise TypeError("[profiles] 必须是 TOML 表")

    profiles: dict[str, ProviderProfile] = {}
    for profile_id, raw_values in raw_profiles.items():
        if not isinstance(profile_id, str) or not _PROFILE_ID_RE.fullmatch(profile_id):
            raise ValueError(
                f"Profile ID {profile_id!r} 无效；请使用 1-64 位字母、数字、点、下划线或连字符"
            )
        if not isinstance(raw_values, dict):
            raise TypeError(f"[profiles.{profile_id}] 必须是 TOML 表")
        unknown = set(raw_values) - _PROFILE_FIELDS
        if unknown:
            fields = ", ".join(sorted(unknown))
            raise ValueError(
                f"Profile {profile_id!r} 包含未知字段: {fields}；请勿在 Profile 中直接存放 API Key"
            )

        provider = _optional_profile_string(profile_id, raw_values, "provider")
        if provider not in _PROFILE_PROVIDERS:
            choices = ", ".join(sorted(_PROFILE_PROVIDERS))
            raise ValueError(f"Profile {profile_id!r} 的 provider 必须是以下之一: {choices}")
        default_model = _optional_profile_string(profile_id, raw_values, "default_model")
        base_url = _optional_profile_string(profile_id, raw_values, "base_url")
        api_key_env = _optional_profile_string(profile_id, raw_values, "api_key_env")
        display_name = _optional_profile_string(profile_id, raw_values, "display_name")
        if api_key_env is not None and not _ENV_NAME_RE.fullmatch(api_key_env):
            raise ValueError(f"Profile {profile_id!r} 的 api_key_env 必须是合法的环境变量名")
        if provider == "openai" and api_key_env is None:
            raise ValueError(
                f"OpenAI 兼容 Profile {profile_id!r} 必须声明 api_key_env，不会隐式复用其他端点的 Key"
            )
        if provider == "ollama" and api_key_env is not None:
            raise ValueError(f"Ollama Profile {profile_id!r} 不应声明 api_key_env")

        profiles[profile_id] = ProviderProfile(
            profile_id=profile_id,
            provider=provider,
            default_model=default_model,
            base_url=base_url,
            api_key_env=api_key_env,
            display_name=display_name,
        )
    return profiles


def get_profile(profile_id: str) -> ProviderProfile:
    """返回命名 Profile；未配置时给出可用 ID 而不暴露凭据。"""

    profiles = load_profiles()
    try:
        return profiles[profile_id]
    except KeyError as exc:
        available = ", ".join(sorted(profiles)) or "无"
        raise ValueError(f"未找到 Provider Profile {profile_id!r}；已配置: {available}") from exc
