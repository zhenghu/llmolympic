"""配置文件加载：项目根目录的 ``config.toml``（标准库 tomllib 解析，零新依赖）。

查找顺序：``LLMOLYMPIC_CONFIG`` 环境变量指定的路径 → 当前目录 → 项目根目录。
取值优先级：环境变量 > config.toml > 默认值。
"""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path


def _find_config() -> Path | None:
    if env_path := os.environ.get("LLMOLYMPIC_CONFIG"):
        # 显式指定的路径是唯一来源，不存在就当没有配置
        path = Path(env_path)
        return path if path.is_file() else None
    for path in (Path.cwd() / "config.toml", Path(__file__).resolve().parent.parent / "config.toml"):
        if path.is_file():
            return path
    return None


@lru_cache
def load_config() -> dict:
    path = _find_config()
    if path is None:
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def get(section: str, key: str, default: str | None = None, env: str | None = None) -> str | None:
    """读取配置项。``env`` 指定时，同名环境变量优先于配置文件。"""
    if env and (value := os.environ.get(env)):
        return value
    return load_config().get(section, {}).get(key, default)
