"""配置文件加载：显式路径或项目根目录的 ``config.toml``。

查找顺序：``LLMOLYMPIC_CONFIG`` 环境变量指定的路径 → 项目根目录。不会搜索
当前工作目录，避免在不可信目录运行命令时误加载其中的端点或密钥配置。
取值优先级：环境变量 > config.toml > 默认值。
"""

from __future__ import annotations

import os
import stat
import tomllib
import warnings
from functools import lru_cache
from pathlib import Path

_PROJECT_CONFIG = Path(__file__).resolve().parent.parent / "config.toml"


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
