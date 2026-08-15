"""Security regressions for publishing the local Web admin capability."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from llmolympic.cli import main as cli_main


@pytest.mark.parametrize("terminal_columns", [20, 80])
def test_web_non_tty_requires_a_private_control_token_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_columns: int,
) -> None:
    calls: list[str] = []

    class FakeUvicorn:
        @staticmethod
        def run(app, **kwargs) -> None:
            del app, kwargs
            calls.append("run")

    def create_app(database, *, control_token: str):
        del database, control_token
        calls.append("create")
        return object()

    monkeypatch.setattr(
        cli_main,
        "_load_web_runtime",
        lambda: (FakeUvicorn, create_app),
    )

    result = CliRunner().invoke(
        cli_main.app,
        ["web", "--db", str(tmp_path / "archive.db")],
        env={"COLUMNS": str(terminal_columns)},
    )
    semantic_result = CliRunner().invoke(
        cli_main.app,
        ["web", "--db", str(tmp_path / "archive.db")],
        env={"COLUMNS": str(terminal_columns)},
        standalone_mode=False,
    )

    assert result.exit_code == 2
    error = semantic_result.exception
    assert isinstance(error, typer.BadParameter)
    assert error.param_hint == "--control-token-file"
    assert error.message == (
        "非交互输出不会打印管理凭证；请使用 "
        "--control-token-file 指定权限 0600 的文件"
    )
    assert "#admin=" not in result.output
    assert calls == []


@pytest.mark.parametrize("server_fails", [False, True])
def test_web_control_token_file_is_private_never_printed_and_always_cleaned_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_fails: bool,
) -> None:
    token_path = tmp_path / "private" / "admin.token"
    observed: dict[str, object] = {}

    def create_app(database, *, control_token: str):
        observed["database"] = database
        observed["token"] = control_token
        return object()

    class FakeUvicorn:
        @staticmethod
        def run(app, **kwargs) -> None:
            observed["app"] = app
            observed["kwargs"] = kwargs
            observed["exists_during_run"] = token_path.is_file()
            observed["mode_during_run"] = stat.S_IMODE(token_path.stat().st_mode)
            observed["file_value_during_run"] = token_path.read_text().strip()
            if server_fails:
                raise RuntimeError("deliberate server failure")

    monkeypatch.setattr(
        cli_main,
        "_load_web_runtime",
        lambda: (FakeUvicorn, create_app),
    )

    result = CliRunner().invoke(
        cli_main.app,
        [
            "web",
            "--db",
            str(tmp_path / "archive.db"),
            "--control-token-file",
            str(token_path),
        ],
    )

    assert result.exit_code == (1 if server_fails else 0)
    assert observed["exists_during_run"] is True
    assert observed["mode_during_run"] == 0o600
    assert observed["file_value_during_run"] == observed["token"]
    assert isinstance(observed["token"], str) and len(observed["token"]) == 43
    assert observed["token"] not in result.output
    assert "#admin=" not in result.output
    assert not token_path.exists()


def test_web_control_token_file_rejects_a_symlink_without_touching_its_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "must-not-change.txt"
    target.write_text("preserve me", encoding="utf-8")
    token_path = tmp_path / "admin.token"
    token_path.symlink_to(target)
    called = False

    class FakeUvicorn:
        @staticmethod
        def run(app, **kwargs) -> None:
            del app, kwargs
            raise AssertionError("server must not start with an unsafe token path")

    def create_app(database, *, control_token: str):
        nonlocal called
        del database, control_token
        called = True
        return object()

    monkeypatch.setattr(
        cli_main,
        "_load_web_runtime",
        lambda: (FakeUvicorn, create_app),
    )

    result = CliRunner().invoke(
        cli_main.app,
        ["web", "--control-token-file", str(token_path)],
    )

    assert result.exit_code != 0
    assert called is False
    assert target.read_text(encoding="utf-8") == "preserve me"
    assert token_path.is_symlink()
