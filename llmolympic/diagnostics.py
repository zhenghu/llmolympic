"""Offline, credential-safe health checks for the CLI."""

from __future__ import annotations

import math
import os
import sqlite3
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from llmolympic import __version__, config
from llmolympic.core.storage import (
    SCHEMA_VERSION,
    StorageError,
    UnsupportedSchemaError,
    inspect_database,
)
from llmolympic.providers.base import ProviderConfigurationError, validate_base_url

DiagnosticStatus = Literal["PASS", "WARN", "FAIL"]


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One deliberately non-sensitive doctor result."""

    status: DiagnosticStatus
    message: str


def _validate_section(raw: dict, section: str) -> dict:
    value = raw.get(section, {})
    if not isinstance(value, dict):
        raise TypeError(f"[{section}] must be a table")
    return value


def _validate_optional_url(
    value: object,
    *,
    source: str,
    require_https_for_remote: bool,
) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise ProviderConfigurationError(f"{source} must be a non-empty string")
    validate_base_url(
        value.strip(),
        source=source,
        require_https_for_remote=require_https_for_remote,
    )


def _config_diagnostics() -> tuple[list[Diagnostic], bool]:
    checks: list[Diagnostic] = []
    source = config.config_source()
    try:
        exists = source.path.exists()
        is_file = source.path.is_file()
    except OSError:
        return [Diagnostic("FAIL", "配置路径无法读取")], False

    if not exists:
        if source.explicit:
            return [Diagnostic("FAIL", "LLMOLYMPIC_CONFIG 显式指定的配置文件不存在")], False
        checks.append(Diagnostic("WARN", "未找到配置文件（mock 选手仍可离线使用）"))
        return checks, True
    if not is_file:
        return [Diagnostic("FAIL", "配置路径不是普通文件")], False

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            raw = config.load_config()
        if not isinstance(raw, dict):
            raise TypeError("config root must be a table")
        openai = _validate_section(raw, "openai")
        ollama = _validate_section(raw, "ollama")
        storage = _validate_section(raw, "storage")
        match = _validate_section(raw, "match")
        profiles = config.load_profiles()

        database_value = storage.get("database")
        if database_value is not None and (
            not isinstance(database_value, str) or not database_value.strip()
        ):
            raise ValueError("storage.database must be a non-empty string")
        timeout_value = match.get("llm_timeout_seconds")
        if timeout_value is not None:
            timeout = float(timeout_value)
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError("match timeout must be positive")

        legacy_base_url = os.environ.get("OPENAI_BASE_URL", openai.get("base_url"))
        _validate_optional_url(
            legacy_base_url,
            source="OpenAI endpoint",
            require_https_for_remote=True,
        )
        ollama_base_url = os.environ.get("OLLAMA_BASE_URL", ollama.get("base_url"))
        _validate_optional_url(
            ollama_base_url,
            source="Ollama endpoint",
            require_https_for_remote=False,
        )

        legacy_key = os.environ.get("OPENAI_API_KEY") or openai.get("api_key")
        if legacy_key is not None and (not isinstance(legacy_key, str) or not legacy_key.strip()):
            raise ValueError("OpenAI key must be a non-empty string")

        for profile in profiles.values():
            _validate_optional_url(
                profile.base_url,
                source=f"Provider Profile {profile.profile_id!r} endpoint",
                require_https_for_remote=profile.provider == "openai",
            )
    except (OSError, TypeError, ValueError):
        # Never include parser/configuration exception text: it may contain a value from
        # a credential-bearing legacy config.
        return [Diagnostic("FAIL", "配置文件无法解析或包含无效设置")], False

    checks.append(Diagnostic("PASS", "配置文件已解析"))
    if os.name == "posix" and source.path.stat().st_mode & 0o077:
        checks.append(Diagnostic("WARN", "配置文件权限过宽，建议设为 0600"))

    if not profiles:
        checks.append(Diagnostic("WARN", "未配置 Provider Profile（mock 选手仍可用）"))
    for profile in profiles.values():
        if profile.provider == "openai" and not os.environ.get(profile.api_key_env or ""):
            checks.append(
                Diagnostic("WARN", f"Provider Profile {profile.profile_id} 缺少凭据环境变量")
            )
        else:
            checks.append(Diagnostic("PASS", f"Provider Profile {profile.profile_id} 配置有效"))

    if openai or os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_BASE_URL"):
        if legacy_key:
            checks.append(Diagnostic("PASS", "OpenAI 兼容 Provider 凭据已配置"))
        else:
            checks.append(Diagnostic("WARN", "OpenAI 兼容 Provider 未配置凭据"))
    if ollama or os.environ.get("OLLAMA_BASE_URL"):
        checks.append(Diagnostic("PASS", "Ollama Provider 端点配置有效"))
    checks.append(Diagnostic("WARN", "Provider 网络与模型可用性未检查（doctor 始终离线）"))
    return checks, True


def run_diagnostics(database: Path | None = None) -> tuple[Diagnostic, ...]:
    """Run offline checks without constructing providers or mutating SQLite state."""

    checks = [Diagnostic("PASS", f"llmolympic {__version__}")]
    config_checks, config_valid = _config_diagnostics()
    checks.extend(config_checks)

    if database is None and not config_valid:
        checks.append(Diagnostic("FAIL", "配置无效，无法安全解析数据库路径"))
        return tuple(checks)

    try:
        inspection = inspect_database(database)
    except UnsupportedSchemaError:
        checks.append(Diagnostic("FAIL", f"SQLite schema 高于当前支持版本 v{SCHEMA_VERSION}"))
    except (OSError, sqlite3.Error, StorageError, ValueError):
        checks.append(Diagnostic("FAIL", "SQLite 数据库无法读取、结构无效或已损坏"))
    else:
        if not inspection.exists:
            checks.append(Diagnostic("WARN", "SQLite 数据库尚未创建（首场比赛时创建）"))
        elif inspection.limited_by_active_journal:
            checks.append(
                Diagnostic(
                    "WARN",
                    "SQLite 存在活动写入日志；为避免副作用或读取不一致快照，本次未完整检查数据库",
                )
            )
        elif inspection.migration_required:
            checks.append(
                Diagnostic(
                    "WARN",
                    f"SQLite schema v{inspection.schema_version} 可迁移至 v{SCHEMA_VERSION}（doctor 未迁移）",
                )
            )
        else:
            checks.append(Diagnostic("PASS", f"SQLite schema v{inspection.schema_version} 兼容"))
        if inspection.exists and not inspection.private_permissions:
            checks.append(Diagnostic("WARN", "SQLite 数据库权限过宽，建议设为 0600"))
    return tuple(checks)
