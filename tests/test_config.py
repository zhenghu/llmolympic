"""配置文件加载测试：可信路径、缺省回退、环境变量优先。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from llmolympic import config
from llmolympic.core.storage import database_path


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """每个用例都用独立配置路径并清掉 lru_cache。"""
    monkeypatch.delenv("LLMOLYMPIC_CONFIG", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
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


def test_budget_and_pricing_defaults_are_credential_free() -> None:
    assert config.load_budget_settings() == config.ProviderBudgetSettings()
    assert config.load_provider_pricing() == {}


def test_budget_and_pricing_are_parsed_strictly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _use_config(
        monkeypatch,
        tmp_path,
        """
[budget]
max_provider_calls = 25
max_input_tokens = 50000
max_output_tokens_per_call = 512
max_total_output_tokens = 10000
max_estimated_cost_usd = "4.25"

[pricing."profile:kimi:moonshot-v1"]
input_usd_per_million_tokens = "0.50"
output_usd_per_million_tokens = "2.00"

[pricing."ollama:llama3.1:8b"]
input_usd_per_million_tokens = "0"
output_usd_per_million_tokens = "0"
""",
    )

    assert config.load_budget_settings() == config.ProviderBudgetSettings(
        max_provider_calls=25,
        max_input_tokens=50000,
        max_output_tokens_per_call=512,
        max_total_output_tokens=10000,
        max_estimated_cost_usd=Decimal("4.25"),
    )
    assert config.load_provider_pricing() == {
        "profile:kimi:moonshot-v1": config.ProviderTokenPrice(
            input_usd_per_million_tokens=Decimal("0.50"),
            output_usd_per_million_tokens=Decimal("2.00"),
        ),
        "ollama:llama3.1:8b": config.ProviderTokenPrice(
            input_usd_per_million_tokens=Decimal(0),
            output_usd_per_million_tokens=Decimal(0),
        ),
    }


def test_budget_zero_totals_are_valid_kill_switches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _use_config(
        monkeypatch,
        tmp_path,
        """
[budget]
max_provider_calls = 0
max_input_tokens = 0
max_total_output_tokens = 0
max_estimated_cost_usd = "0"
""",
    )

    assert config.load_budget_settings() == config.ProviderBudgetSettings(
        max_provider_calls=0,
        max_input_tokens=0,
        max_total_output_tokens=0,
        max_estimated_cost_usd=Decimal(0),
    )


def test_budget_resolution_is_fieldwise_cli_then_env_then_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _use_config(
        monkeypatch,
        tmp_path,
        """
[budget]
max_provider_calls = 10
max_input_tokens = 20
max_output_tokens_per_call = 30
max_total_output_tokens = 40
max_estimated_cost_usd = "5.00"
""",
    )
    environment = {
        "LLMOLYMPIC_MAX_PROVIDER_CALLS": "11",
        "LLMOLYMPIC_MAX_OUTPUT_TOKENS_PER_CALL": "31",
        "LLMOLYMPIC_MAX_ESTIMATED_COST_USD": "6.00",
    }

    resolved = config.resolve_budget_settings(
        max_provider_calls=12,
        max_total_output_tokens=42,
        environ=environment,
    )

    assert resolved == config.ProviderBudgetSettings(
        max_provider_calls=12,
        max_input_tokens=20,
        max_output_tokens_per_call=31,
        max_total_output_tokens=42,
        max_estimated_cost_usd=Decimal("6.00"),
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LLMOLYMPIC_MAX_PROVIDER_CALLS", ""),
        ("LLMOLYMPIC_MAX_INPUT_TOKENS", " 1"),
        ("LLMOLYMPIC_MAX_OUTPUT_TOKENS_PER_CALL", "0"),
        ("LLMOLYMPIC_MAX_TOTAL_OUTPUT_TOKENS", "01"),
        ("LLMOLYMPIC_MAX_ESTIMATED_COST_USD", "NaN"),
    ],
)
def test_budget_environment_values_are_strict(
    name: str,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _use_config(monkeypatch, tmp_path, "")

    with pytest.raises(ValueError):
        config.resolve_budget_settings(environ={name: value})


@pytest.mark.parametrize(
    ("content", "error"),
    [
        ("[budget]\nunknown = 1\n", "未知字段"),
        ("[budget]\nmax_provider_calls = true\n", "必须是"),
        ("[budget]\nmax_output_tokens_per_call = 0\n", "必须是"),
        ("[budget]\nmax_estimated_cost_usd = 1.5\n", "十进制字符串"),
        ('[budget]\nmax_estimated_cost_usd = "NaN"\n', "有限"),
        (
            (
                '[pricing."bad spec"]\ninput_usd_per_million_tokens = "1"\n'
                'output_usd_per_million_tokens = "1"\n'
            ),
            "键必须是显式",
        ),
        (
            '[pricing."openai:model"]\ninput_usd_per_million_tokens = "1"\n',
            "缺少字段",
        ),
        (
            (
                '[pricing."openai:model"]\ninput_usd_per_million_tokens = "-1"\n'
                'output_usd_per_million_tokens = "1"\n'
            ),
            "非负",
        ),
        (
            (
                '[pricing."openai:model"]\ninput_usd_per_million_tokens = "1"\n'
                'output_usd_per_million_tokens = "1"\napi_key = "secret"\n'
            ),
            "未知字段",
        ),
    ],
)
def test_invalid_budget_or_pricing_is_rejected(
    content: str,
    error: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _use_config(monkeypatch, tmp_path, content)

    with pytest.raises((TypeError, ValueError), match=error):
        config.load_budget_settings()
        config.load_provider_pricing()


def test_named_provider_profiles_are_strict_and_credential_free(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _use_config(
        monkeypatch,
        tmp_path,
        """
[profiles.kimi]
provider = "openai"
default_model = "moonshot-v1"
base_url = "https://kimi.example/v1"
api_key_env = "KIMI_API_KEY"
display_name = "Kimi"

[profiles.local]
provider = "ollama"
default_model = "llama3.1:8b"
""",
    )
    monkeypatch.setenv("KIMI_API_KEY", "profile-secret-must-not-be-loaded")

    profiles = config.load_profiles()

    assert set(profiles) == {"kimi", "local"}
    assert profiles["kimi"] == config.ProviderProfile(
        profile_id="kimi",
        provider="openai",
        default_model="moonshot-v1",
        base_url="https://kimi.example/v1",
        api_key_env="KIMI_API_KEY",
        display_name="Kimi",
    )
    assert "profile-secret" not in repr(profiles)
    assert config.get_profile("local").default_model == "llama3.1:8b"


@pytest.mark.parametrize(
    ("profile_toml", "error"),
    [
        ('[profiles."bad:id"]\nprovider = "ollama"\n', "Profile ID"),
        ('[profiles.bad]\nprovider = "unknown"\n', "provider"),
        ('[profiles.bad]\nprovider = "openai"\n', "api_key_env"),
        (
            '[profiles.bad]\nprovider = "openai"\napi_key_env = "NOT-VALID!"\n',
            "环境变量名",
        ),
        (
            '[profiles.bad]\nprovider = "ollama"\napi_key_env = "UNUSED_KEY"\n',
            "不应声明",
        ),
        (
            '[profiles.bad]\nprovider = "openai"\napi_key_env = "KEY"\napi_key = "secret"\n',
            "未知字段",
        ),
    ],
)
def test_invalid_provider_profile_is_rejected(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    profile_toml: str,
    error: str,
) -> None:
    _use_config(monkeypatch, tmp_path, profile_toml)

    with pytest.raises(ValueError, match=error):
        config.load_profiles()


def test_unknown_profile_error_lists_safe_available_ids(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_config(
        monkeypatch,
        tmp_path,
        '[profiles.local]\nprovider = "ollama"\ndefault_model = "model"\n',
    )

    with pytest.raises(ValueError, match="local") as raised:
        config.get_profile("missing")

    assert "model" not in str(raised.value)
