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
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

_PROJECT_CONFIG = Path(__file__).resolve().parent.parent / "config.toml"
_PROFILE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_PROFILE_CREDENTIAL_ENV_RE = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_PROFILE_CREDENTIAL_ENV_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_KEY")
_PROFILE_FIELDS = frozenset(
    {"provider", "default_model", "base_url", "api_key_env", "display_name"}
)
_PROFILE_PROVIDERS = frozenset({"openai", "ollama"})
_BUDGET_FIELDS = frozenset(
    {
        "max_provider_calls",
        "max_input_tokens",
        "max_output_tokens_per_call",
        "max_total_output_tokens",
        "max_estimated_cost_usd",
    }
)
_PRICING_FIELDS = frozenset({"input_usd_per_million_tokens", "output_usd_per_million_tokens"})
_PRICING_SPEC_RE = re.compile(
    r"(?:profile:[A-Za-z0-9][A-Za-z0-9_.-]{0,63}:|(?:openai|ollama|mock):)"
    r"[^\s,\x00-\x1f\x7f]{1,256}\Z",
    re.ASCII,
)
_SQLITE_INT_MAX = 2**63 - 1
_MAX_PRICE_USD_PER_MILLION = Decimal(1000000)
_MAX_DECIMAL_INPUT_LEN = 128
_MAX_DECIMAL_EXPONENT = 18


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


@dataclass(frozen=True, slots=True)
class ProviderBudgetSettings:
    """Credential-free process defaults for Provider resource accounting."""

    max_provider_calls: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens_per_call: int = 1024
    max_total_output_tokens: int | None = None
    max_estimated_cost_usd: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ProviderTokenPrice:
    """User-supplied, frozen USD prices for one exact CLI model spec."""

    input_usd_per_million_tokens: Decimal
    output_usd_per_million_tokens: Decimal


@dataclass(frozen=True, slots=True)
class ConfigSource:
    """The single trusted configuration location selected for this process."""

    path: Path
    explicit: bool


def is_profile_credential_environment_name(value: str) -> bool:
    """Return whether a Profile env name is credential-only and child-safe."""

    return (
        isinstance(value, str)
        and bool(_PROFILE_CREDENTIAL_ENV_RE.fullmatch(value))
        and not value.startswith("LLMOLYMPIC_")
        and value.endswith(_PROFILE_CREDENTIAL_ENV_SUFFIXES)
    )


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


def config_source() -> ConfigSource:
    """Return the selected config location, even when that file is absent.

    Keeping selection separate from loading lets read-only diagnostics distinguish an
    intentionally empty installation from a misspelled ``LLMOLYMPIC_CONFIG`` path.
    """

    if env_path := os.environ.get("LLMOLYMPIC_CONFIG"):
        return ConfigSource(path=Path(env_path), explicit=True)
    return ConfigSource(path=_PROJECT_CONFIG, explicit=False)


def _find_config() -> Path | None:
    source = config_source()
    return source.path if source.path.is_file() else None


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


def _optional_bounded_integer(
    section: str,
    values: dict,
    field: str,
    *,
    default: int | None = None,
    minimum: int = 0,
) -> int | None:
    value = values.get(field, default)
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= _SQLITE_INT_MAX
    ):
        raise ValueError(f"[{section}] {field} 必须是 {minimum} 到 {_SQLITE_INT_MAX} 之间的整数")
    return value


def _decimal_string(
    value: object,
    *,
    label: str,
    allow_zero: bool,
) -> Decimal:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{label} 必须是不带首尾空白的十进制字符串")
    if not value.isascii():
        raise ValueError(f"{label} 必须是 ASCII 十进制字符串")
    if len(value) > _MAX_DECIMAL_INPUT_LEN:
        raise ValueError(f"{label} 长度过长，可能有拒绝服务风险")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{label} 必须是合法十进制字符串") from exc
    if not parsed.is_finite():
        raise ValueError(f"{label} 必须是有限十进制数")
    digits = parsed.as_tuple().digits
    exponent = parsed.as_tuple().exponent
    if abs(exponent) > _MAX_DECIMAL_EXPONENT or len(digits) > _MAX_DECIMAL_INPUT_LEN:
        raise ValueError(f"{label} 指数或有效数字位数过大，可能有拒绝服务风险")
    lower_bound = Decimal(0) if allow_zero else Decimal("0.000000001")
    if parsed < lower_bound or parsed > _MAX_PRICE_USD_PER_MILLION:
        qualifier = "非负" if allow_zero else "大于 0"
        raise ValueError(
            f"{label} 必须是{qualifier}且不超过 {_MAX_PRICE_USD_PER_MILLION} 的有限十进制数"
        )
    return parsed


def load_budget_settings() -> ProviderBudgetSettings:
    """严格读取无凭据的 ``[budget]`` 默认值。"""

    raw = load_config().get("budget", {})
    if not isinstance(raw, dict):
        raise TypeError("[budget] 必须是 TOML 表")
    unknown = set(raw) - _BUDGET_FIELDS
    if unknown:
        fields = ", ".join(sorted(str(field) for field in unknown))
        raise ValueError(f"[budget] 包含未知字段: {fields}")

    raw_cost = raw.get("max_estimated_cost_usd")
    estimated_cost = (
        None
        if raw_cost is None
        else _decimal_string(
            raw_cost,
            label="[budget] max_estimated_cost_usd",
            allow_zero=True,
        )
    )
    return ProviderBudgetSettings(
        max_provider_calls=_optional_bounded_integer("budget", raw, "max_provider_calls"),
        max_input_tokens=_optional_bounded_integer("budget", raw, "max_input_tokens"),
        max_output_tokens_per_call=_optional_bounded_integer(
            "budget",
            raw,
            "max_output_tokens_per_call",
            default=1024,
            minimum=1,
        )
        or 1024,
        max_total_output_tokens=_optional_bounded_integer("budget", raw, "max_total_output_tokens"),
        max_estimated_cost_usd=estimated_cost,
    )


_BUDGET_ENV_NAMES = {
    "max_provider_calls": "LLMOLYMPIC_MAX_PROVIDER_CALLS",
    "max_input_tokens": "LLMOLYMPIC_MAX_INPUT_TOKENS",
    "max_output_tokens_per_call": "LLMOLYMPIC_MAX_OUTPUT_TOKENS_PER_CALL",
    "max_total_output_tokens": "LLMOLYMPIC_MAX_TOTAL_OUTPUT_TOKENS",
    "max_estimated_cost_usd": "LLMOLYMPIC_MAX_ESTIMATED_COST_USD",
}


def _environment_budget_integer(
    environ: Mapping[str, str],
    field: str,
    *,
    minimum: int,
) -> int | None:
    env_name = _BUDGET_ENV_NAMES[field]
    if env_name not in environ:
        return None
    raw = environ[env_name]
    if not isinstance(raw, str) or not raw or raw != raw.strip() or not raw.isascii():
        raise ValueError(f"环境变量 {env_name} 必须是不带空白的十进制整数")
    try:
        value = int(raw, 10)
    except ValueError as exc:
        raise ValueError(f"环境变量 {env_name} 必须是十进制整数") from exc
    if str(value) != raw or not minimum <= value <= _SQLITE_INT_MAX:
        raise ValueError(
            f"环境变量 {env_name} 必须是 {minimum} 到 {_SQLITE_INT_MAX} 之间的十进制整数"
        )
    return value


def resolve_budget_settings(
    *,
    max_provider_calls: int | None = None,
    max_input_tokens: int | None = None,
    max_output_tokens_per_call: int | None = None,
    max_total_output_tokens: int | None = None,
    max_estimated_cost_usd: str | Decimal | None = None,
    environ: Mapping[str, str] | None = None,
) -> ProviderBudgetSettings:
    """Resolve each budget field independently as CLI > environment > TOML > default."""

    environment = os.environ if environ is None else environ
    configured = load_budget_settings()

    def integer_value(field: str, cli_value: int | None, *, minimum: int) -> int | None:
        if cli_value is not None:
            return _optional_bounded_integer(
                "CLI",
                {field: cli_value},
                field,
                minimum=minimum,
            )
        environment_value = _environment_budget_integer(
            environment,
            field,
            minimum=minimum,
        )
        return getattr(configured, field) if environment_value is None else environment_value

    if max_estimated_cost_usd is not None:
        raw_cost: object = max_estimated_cost_usd
        cost_label = "--max-estimated-cost-usd"
    elif (cost_env := _BUDGET_ENV_NAMES["max_estimated_cost_usd"]) in environment:
        raw_cost = environment[cost_env]
        cost_label = f"环境变量 {cost_env}"
    else:
        raw_cost = configured.max_estimated_cost_usd
        cost_label = "[budget] max_estimated_cost_usd"
    if raw_cost is None:
        estimated_cost = None
    elif isinstance(raw_cost, Decimal):
        estimated_cost = _decimal_string(
            str(raw_cost),
            label=cost_label,
            allow_zero=True,
        )
    else:
        estimated_cost = _decimal_string(raw_cost, label=cost_label, allow_zero=True)

    return ProviderBudgetSettings(
        max_provider_calls=integer_value(
            "max_provider_calls",
            max_provider_calls,
            minimum=0,
        ),
        max_input_tokens=integer_value(
            "max_input_tokens",
            max_input_tokens,
            minimum=0,
        ),
        max_output_tokens_per_call=integer_value(
            "max_output_tokens_per_call",
            max_output_tokens_per_call,
            minimum=1,
        )
        or 1024,
        max_total_output_tokens=integer_value(
            "max_total_output_tokens",
            max_total_output_tokens,
            minimum=0,
        ),
        max_estimated_cost_usd=estimated_cost,
    )


def load_provider_pricing() -> dict[str, ProviderTokenPrice]:
    """读取按精确 CLI model spec 索引的本地冻结价格。"""

    raw_pricing = load_config().get("pricing", {})
    if not isinstance(raw_pricing, dict):
        raise TypeError("[pricing] 必须是 TOML 表")
    pricing: dict[str, ProviderTokenPrice] = {}
    for spec, raw_values in raw_pricing.items():
        if not isinstance(spec, str) or _PRICING_SPEC_RE.fullmatch(spec) is None:
            raise ValueError(
                "[pricing] 键必须是显式 openai:/ollama:/mock: 或 profile:<id>:<model> spec"
            )
        if not isinstance(raw_values, dict):
            raise TypeError(f"[pricing.{spec!r}] 必须是 TOML 表")
        unknown = set(raw_values) - _PRICING_FIELDS
        if unknown:
            fields = ", ".join(sorted(str(field) for field in unknown))
            raise ValueError(f"[pricing.{spec!r}] 包含未知字段: {fields}")
        missing = _PRICING_FIELDS - set(raw_values)
        if missing:
            fields = ", ".join(sorted(missing))
            raise ValueError(f"[pricing.{spec!r}] 缺少字段: {fields}")
        pricing[spec] = ProviderTokenPrice(
            input_usd_per_million_tokens=_decimal_string(
                raw_values["input_usd_per_million_tokens"],
                label=f"[pricing.{spec!r}] input_usd_per_million_tokens",
                allow_zero=True,
            ),
            output_usd_per_million_tokens=_decimal_string(
                raw_values["output_usd_per_million_tokens"],
                label=f"[pricing.{spec!r}] output_usd_per_million_tokens",
                allow_zero=True,
            ),
        )
    return pricing


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
        if api_key_env is not None and not is_profile_credential_environment_name(
            api_key_env
        ):
            raise ValueError(
                f"Profile {profile_id!r} 的 api_key_env 必须是大写凭据环境变量名，"
                "并以 _API_KEY、_TOKEN、_SECRET 或 _KEY 结尾"
            )
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
