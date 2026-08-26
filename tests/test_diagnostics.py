"""Version and strictly offline doctor command tests."""

from __future__ import annotations

import importlib.metadata
import json
import os
import sqlite3
import tomllib
from pathlib import Path

import pytest
from rich.text import Text
from typer.testing import CliRunner

from llmolympic import __version__, config
from llmolympic.cli.main import app
from llmolympic.core.storage import SCHEMA_VERSION, SQLiteStore

runner = CliRunner()


def _plain(output: str) -> str:
    return Text.from_ansi(output).plain


def _all_output(result) -> str:
    return _plain(result.stdout) + _plain(result.stderr)


def _write_private_config(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    if os.name == "posix":
        path.chmod(0o600)


@pytest.fixture(autouse=True)
def _isolate_diagnostics(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("LLMOLYMPIC_CONFIG", raising=False)
    monkeypatch.setattr(config, "_PROJECT_CONFIG", tmp_path / "missing-config.toml")
    for name in (
        "LLMOLYMPIC_DB",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OLLAMA_BASE_URL",
        "DOCTOR_PROFILE_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    config.load_config.cache_clear()
    yield
    config.load_config.cache_clear()


def test_version_has_one_literal_source_and_cli_matches_installed_metadata() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    metadata = importlib.metadata.metadata("llmolympic")

    assert project["build-system"]["requires"] == ["hatchling>=1.27,<1.33"]
    assert project["project"]["dynamic"] == ["version"]
    assert "version" not in project["project"]
    assert project["tool"]["hatch"]["version"]["path"] == "llmolympic/__init__.py"
    assert metadata["Version"] == __version__ == "0.11.0"
    assert project["project"]["license"] == "MIT"
    assert project["project"]["license-files"] == ["LICENSE", "THIRD_PARTY_NOTICES.md"]
    assert metadata["License-Expression"] == "MIT"
    assert set(metadata.get_all("License-File", [])) == {"LICENSE", "THIRD_PARTY_NOTICES.md"}
    assert (
        Path("LICENSE")
        .read_text(encoding="utf-8")
        .startswith("MIT License\n\nCopyright (c) 2026 zhenghu\n")
    )
    third_party = Path("THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "python-chess" in third_party
    assert "GPL-3.0-or-later" in third_party

    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert _plain(result.output) == f"llmolympic {__version__}\n"


def test_release_documents_match_package_version() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    assert f"v{__version__} 通过 GitHub Release" in readme
    assert (
        f"releases/download/v{__version__}/"
        f"llmolympic-{__version__}-py3-none-any.whl" in readme
    )
    assert (
        f'"llmolympic[web] @ https://github.com/zhenghu/llmolympic/releases/'
        f'download/v{__version__}/llmolympic-{__version__}-py3-none-any.whl"' in readme
    )
    assert "阶段 4.2 自 v0.5.0 起随正式 wheel/sdist 发布" in readme
    assert "阶段 4.3/4.4 自 v0.6.0 起" in readme
    assert "阶段 4.5a/4.5b 自 v0.7.0 起" in readme
    assert f"`llmolympic-{__version__}-py3-none-any.whl`" in readme
    assert f"`llmolympic-{__version__}.tar.gz`" in readme

    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{__version__}] - 2026-08-26" in changelog
    assert changelog.index("## [Unreleased]") < changelog.index(f"## [{__version__}]")

    supported_series = ".".join(__version__.split(".")[:2])
    security = Path("SECURITY.md").read_text(encoding="utf-8")
    assert f"| `{supported_series}.x` | 支持 |" in security
    series_start_version = f"{supported_series}.0"
    assert f"| `<{series_start_version}`、历史提交和其他功能分支 | 不支持 |" in security


def test_react_bundle_build_and_ci_contract() -> None:
    package = json.loads(Path("package.json").read_text(encoding="utf-8"))
    assert package["scripts"]["build:web"] == "node scripts/build_web.mjs"
    assert package["scripts"]["verify:web-vendor"] == "node scripts/verify_web_vendor.mjs"
    assert package["devDependencies"]["react"] == "19.2.8"
    assert package["devDependencies"]["react-dom"] == "19.2.8"
    assert "esbuild" in package["devDependencies"]

    for workflow_name in ("ci.yml", "release.yml"):
        workflow = Path(".github/workflows", workflow_name).read_text(encoding="utf-8")
        build = workflow.index("npm run build:web")
        verify = workflow.index("npm run verify:web-vendor")
        distribution = workflow.index("python -m build")
        assert build < verify < distribution

    codeql = Path(".github/workflows/codeql.yml").read_text(encoding="utf-8")
    assert "- llmolympic/web/static/assets/app.js" in codeql
    assert "- web_src/app.js" not in codeql
    assert "react.production.min.js" not in codeql
    assert "react-dom.production.min.js" not in codeql


def test_version_option_does_not_load_bad_config_or_create_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "broken.toml"
    database = tmp_path / "must-not-exist.db"
    sentinel = "version-side-effect-sentinel"
    config_path.write_text(f'broken = "{sentinel}"\n[', encoding="utf-8")
    monkeypatch.setenv("LLMOLYMPIC_CONFIG", str(config_path))
    monkeypatch.setenv("LLMOLYMPIC_DB", str(database))
    config.load_config.cache_clear()

    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert _plain(result.stdout) == f"llmolympic {__version__}\n"
    assert sentinel not in _all_output(result)
    assert not database.exists()


def test_root_no_args_and_help_behavior_remain_compatible() -> None:
    no_args = runner.invoke(app, [])
    help_result = runner.invoke(app, ["--help"])

    assert no_args.exit_code == 2
    assert "Usage:" in _plain(no_args.output)
    assert help_result.exit_code == 0
    assert "--version" in _plain(help_result.output)
    assert "doctor" in _plain(help_result.output)


def test_doctor_missing_config_and_database_warns_without_creating_files(tmp_path: Path) -> None:
    database = tmp_path / "missing.db"

    result = runner.invoke(app, ["doctor", "--db", str(database)])
    output = _plain(result.output)

    assert result.exit_code == 0
    assert f"PASS llmolympic {__version__}" in output
    assert "WARN 未找到配置文件" in output
    assert "WARN SQLite 数据库尚未创建" in output
    assert not database.exists()


def test_doctor_missing_explicit_config_fails_without_creating_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "missing.db"
    monkeypatch.setenv("LLMOLYMPIC_CONFIG", str(tmp_path / "misspelled-config.toml"))
    config.load_config.cache_clear()

    result = runner.invoke(app, ["doctor", "--db", str(database)])
    output = _plain(result.output)

    assert result.exit_code == 1
    assert "FAIL LLMOLYMPIC_CONFIG 显式指定的配置文件不存在" in output
    assert not database.exists()


def test_doctor_never_constructs_providers_or_prints_credentials_and_urls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    database = tmp_path / "missing.db"
    legacy_sentinel = "legacy-doctor-secret"
    environment_sentinel = "environment-doctor-secret"
    profile_sentinel = "profile-doctor-secret"
    endpoint = "https://private-tenant-doctor.example/v1"
    _write_private_config(
        config_path,
        f"""
[openai]
api_key = "{legacy_sentinel}"
base_url = "{endpoint}"

[profiles.remote]
provider = "openai"
default_model = "model"
base_url = "{endpoint}"
api_key_env = "DOCTOR_PROFILE_KEY"
""",
    )
    monkeypatch.setenv("LLMOLYMPIC_CONFIG", str(config_path))
    monkeypatch.setenv("OPENAI_API_KEY", environment_sentinel)
    monkeypatch.setenv("DOCTOR_PROFILE_KEY", profile_sentinel)
    config.load_config.cache_clear()

    def unexpected_call(*args, **kwargs):
        raise AssertionError("doctor must not construct providers or access the network")

    monkeypatch.setattr(
        "llmolympic.providers.openai_provider.OpenAIProvider.__init__",
        unexpected_call,
    )
    monkeypatch.setattr("httpx.Client.request", unexpected_call)
    monkeypatch.setattr("httpx.AsyncClient.request", unexpected_call)

    result = runner.invoke(app, ["doctor", "--db", str(database)])
    output = _plain(result.output)
    captured = _all_output(result)

    assert result.exit_code == 0
    assert "PASS Provider Profile remote 配置有效" in output
    assert "PASS OpenAI 兼容 Provider 凭据已配置" in output
    assert "doctor 始终离线" in output
    for sensitive in (legacy_sentinel, environment_sentinel, profile_sentinel, endpoint):
        assert sensitive not in captured
    assert not database.exists()


def test_doctor_redacts_invalid_config_and_unsafe_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "unsafe.toml"
    database = tmp_path / "missing.db"
    url_sentinel = "url-userinfo-query-secret"
    _write_private_config(
        config_path,
        f"""
[profiles.remote]
provider = "openai"
default_model = "model"
base_url = "https://user:{url_sentinel}@example.com/v1?token={url_sentinel}"
api_key_env = "DOCTOR_PROFILE_KEY"
""",
    )
    monkeypatch.setenv("LLMOLYMPIC_CONFIG", str(config_path))
    monkeypatch.setenv("DOCTOR_PROFILE_KEY", "profile-key-secret")
    config.load_config.cache_clear()

    result = runner.invoke(app, ["doctor", "--db", str(database)])
    output = _plain(result.output)
    captured = _all_output(result)

    assert result.exit_code == 1
    assert "FAIL 配置文件无法解析或包含无效设置" in output
    assert url_sentinel not in captured
    assert "profile-key-secret" not in captured
    assert not database.exists()


def test_doctor_reports_corrupt_and_future_databases_without_raw_content(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.db"
    raw_sentinel = "database-content-secret"
    corrupt.write_bytes(f"not sqlite {raw_sentinel}".encode())

    corrupt_result = runner.invoke(app, ["doctor", "--db", str(corrupt)])
    corrupt_output = _plain(corrupt_result.output)
    corrupt_captured = _all_output(corrupt_result)

    assert corrupt_result.exit_code == 1
    assert "FAIL SQLite 数据库无法读取" in corrupt_output
    assert raw_sentinel not in corrupt_captured

    future = tmp_path / "future.db"
    with sqlite3.connect(future) as connection:
        connection.execute("PRAGMA user_version = 99")

    future_result = runner.invoke(app, ["doctor", "--db", str(future)])
    future_output = _plain(future_result.output)

    assert future_result.exit_code == 1
    assert f"FAIL SQLite schema 高于当前支持版本 v{SCHEMA_VERSION}" in future_output


def test_doctor_inspects_current_database_without_modifying_it(tmp_path: Path) -> None:
    database = tmp_path / "current.db"
    SQLiteStore(database)
    before = database.read_bytes()
    before_stat = database.stat()

    result = runner.invoke(app, ["doctor", "--db", str(database)])
    output = _plain(result.output)
    after_stat = database.stat()

    assert result.exit_code == 0
    assert f"PASS SQLite schema v{SCHEMA_VERSION} 兼容" in output
    assert database.read_bytes() == before
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert after_stat.st_mode == before_stat.st_mode
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()


def test_doctor_does_not_migrate_or_chmod_v4_database(tmp_path: Path) -> None:
    database = tmp_path / "legacy-v4.db"
    SQLiteStore(database)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            DROP TABLE tournament_checkpoint_series;
            DROP TABLE tournament_checkpoints;
            PRAGMA user_version = 4;
            """
        )
    if os.name == "posix":
        database.chmod(0o640)
    before = database.read_bytes()
    before_stat = database.stat()

    result = runner.invoke(app, ["doctor", "--db", str(database)])
    output = _plain(result.output)
    after_stat = database.stat()

    assert result.exit_code == 0
    assert f"WARN SQLite schema v4 可迁移至 v{SCHEMA_VERSION}（doctor 未迁移）" in output
    assert database.read_bytes() == before
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert after_stat.st_mode == before_stat.st_mode
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert "tournament_checkpoints" not in tables
    assert "tournament_checkpoint_series" not in tables


@pytest.mark.parametrize("suffix", ["-journal", "-wal"])
def test_doctor_avoids_opening_database_when_active_journal_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    database = tmp_path / "active-writer.db"
    SQLiteStore(database)
    sidecar = Path(f"{database}{suffix}")
    sidecar.write_bytes(b"active-journal-sentinel")

    def unexpected_connect(*args, **kwargs):
        raise AssertionError("journal-limited doctor must not open SQLite")

    monkeypatch.setattr("llmolympic.core.storage.sqlite3.connect", unexpected_connect)
    result = runner.invoke(app, ["doctor", "--db", str(database)])
    output = _plain(result.output)

    assert result.exit_code == 0
    assert "WARN SQLite 存在活动写入日志" in output
    assert sidecar.read_bytes() == b"active-journal-sentinel"
