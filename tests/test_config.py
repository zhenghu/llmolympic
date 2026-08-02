"""配置文件加载测试：可信路径、缺省回退、环境变量优先。"""

from __future__ import annotations

import pytest

from llmolympic import config
from llmolympic.core.storage import database_path


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """每个用例都用独立配置路径并清掉 lru_cache。"""
    monkeypatch.delenv("LLMOLYMPIC_CONFIG", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("LLMOLYMPIC_DB", raising=False)
    monkeypatch.setattr(config, "_PROJECT_CONFIG", tmp_path / "project" / "config.toml")
    load_config_cache_clear()
    yield
    load_config_cache_clear()


def load_config_cache_clear() -> None:
    config.load_config.cache_clear()


def _write_private_config(path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    if config.os.name == "posix":
        path.chmod(0o600)


def _use_config(monkeypatch: pytest.MonkeyPatch, tmp_path, content: str) -> None:
    path = tmp_path / "config.toml"
    _write_private_config(path, content)
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


def test_does_not_load_config_from_untrusted_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    untrusted_directory = tmp_path / "downloaded-project"
    untrusted_directory.mkdir()
    (untrusted_directory / "config.toml").write_text(
        '[openai]\nbase_url = "https://attacker.example/v1"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(untrusted_directory)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-environment")
    load_config_cache_clear()

    assert config.get("openai", "api_key", env="OPENAI_API_KEY") == "sk-environment"
    assert config.get("openai", "base_url", env="OPENAI_BASE_URL") is None


def test_loads_fixed_project_config(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    project_config = tmp_path / "trusted-project" / "config.toml"
    project_config.parent.mkdir()
    _write_private_config(project_config, '[openai]\ndefault_model = "trusted-model"\n')
    monkeypatch.setattr(config, "_PROJECT_CONFIG", project_config)
    load_config_cache_clear()

    assert config.get("openai", "default_model") == "trusted-model"


def test_explicit_config_path_takes_priority_over_project_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    project_config = tmp_path / "trusted-project" / "config.toml"
    project_config.parent.mkdir()
    _write_private_config(project_config, '[openai]\ndefault_model = "project-model"\n')
    monkeypatch.setattr(config, "_PROJECT_CONFIG", project_config)

    explicit_config = tmp_path / "explicit.toml"
    _write_private_config(explicit_config, '[openai]\ndefault_model = "explicit-model"\n')
    monkeypatch.setenv("LLMOLYMPIC_CONFIG", str(explicit_config))
    load_config_cache_clear()

    assert config.get("openai", "default_model") == "explicit-model"


def test_missing_explicit_config_does_not_fallback_to_project_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    project_config = tmp_path / "trusted-project" / "config.toml"
    project_config.parent.mkdir()
    _write_private_config(project_config, '[openai]\ndefault_model = "project-model"\n')
    monkeypatch.setattr(config, "_PROJECT_CONFIG", project_config)
    monkeypatch.setenv("LLMOLYMPIC_CONFIG", str(tmp_path / "missing.toml"))
    load_config_cache_clear()

    assert config.load_config() == {}


def test_shared_config_permissions_emit_security_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    if config.os.name != "posix":
        pytest.skip("POSIX permissions only")
    config_path = tmp_path / "shared.toml"
    config_path.write_text('[openai]\ndefault_model = "model"\n', encoding="utf-8")
    config_path.chmod(0o644)
    monkeypatch.setenv("LLMOLYMPIC_CONFIG", str(config_path))
    load_config_cache_clear()

    with pytest.warns(RuntimeWarning, match="chmod 600"):
        assert config.load_config()["openai"]["default_model"] == "model"


def test_env_var_overrides_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _use_config(monkeypatch, tmp_path, '[openai]\napi_key = "sk-file"\n')
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    assert config.get("openai", "api_key", env="OPENAI_API_KEY") == "sk-env"


def test_database_path_precedence(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    configured = tmp_path / "configured.db"
    environment = tmp_path / "environment.db"
    explicit = tmp_path / "explicit.db"
    _use_config(monkeypatch, tmp_path, f'[storage]\ndatabase = "{configured}"\n')

    assert database_path() == configured.resolve()
    monkeypatch.setenv("LLMOLYMPIC_DB", str(environment))
    assert database_path() == environment.resolve()
    assert database_path(explicit) == explicit.resolve()
