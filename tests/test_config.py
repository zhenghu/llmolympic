"""配置文件加载测试：文件读取、缺省回退、环境变量优先。"""

from __future__ import annotations

import pytest

from llmolympic import config


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """每个用例都用独立配置路径并清掉 lru_cache。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    yield
    load_config_cache_clear()


def load_config_cache_clear() -> None:
    config.load_config.cache_clear()


def _use_config(monkeypatch: pytest.MonkeyPatch, tmp_path, content: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setenv("LLMOLYMPIC_CONFIG", str(path))
    load_config_cache_clear()


def test_no_config_file_returns_default(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("LLMOLYMPIC_CONFIG", str(tmp_path / "不存在.toml"))
    load_config_cache_clear()
    assert config.load_config() == {}
    assert config.get("openai", "api_key") is None
    assert config.get("ollama", "base_url", "http://localhost:11434") == "http://localhost:11434"


def test_loads_values_from_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _use_config(monkeypatch, tmp_path, '[openai]\napi_key = "sk-test"\n')
    assert config.get("openai", "api_key") == "sk-test"
    assert config.get("openai", "base_url") is None


def test_env_var_overrides_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _use_config(monkeypatch, tmp_path, '[openai]\napi_key = "sk-file"\n')
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    assert config.get("openai", "api_key", env="OPENAI_API_KEY") == "sk-env"
