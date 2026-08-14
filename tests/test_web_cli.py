from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.text import Text
from typer.testing import CliRunner

from llmolympic.cli import main

runner = CliRunner()


def _plain(output: str) -> str:
    return Text.from_ansi(output).plain


def test_web_rejects_non_loopback_before_loading_optional_runtime(monkeypatch) -> None:
    def should_not_load():
        raise AssertionError("optional runtime must not load for a rejected host")

    monkeypatch.setattr(main, "_load_web_runtime", should_not_load)

    result = runner.invoke(main.app, ["web", "--host", "0.0.0.0"])  # noqa: S104

    assert result.exit_code == 2
    assert "只允许回环地址" in _plain(result.output)


@pytest.mark.parametrize(
    ("host", "display_host"),
    [("127.0.0.1", "127.0.0.1"), ("::1", "[::1]")],
)
def test_web_starts_hardened_local_server(
    monkeypatch,
    tmp_path: Path,
    host: str,
    display_host: str,
) -> None:
    calls: list[tuple[object, dict[str, object]]] = []
    created_for: list[Path] = []
    sentinel_app = object()

    def create_app(path: Path) -> object:
        created_for.append(path)
        return sentinel_app

    def run(app: object, **kwargs: object) -> None:
        calls.append((app, kwargs))

    monkeypatch.setattr(
        main,
        "_load_web_runtime",
        lambda: (SimpleNamespace(run=run), create_app),
    )
    database = tmp_path / "archive.db"

    result = runner.invoke(
        main.app,
        ["web", "--db", str(database), "--host", host, "--port", "8765"],
    )

    assert result.exit_code == 0, result.output
    assert created_for == [database.resolve()]
    assert calls == [
        (
            sentinel_app,
            {
                "host": host,
                "port": 8765,
                "access_log": False,
                "proxy_headers": False,
                "forwarded_allow_ips": "",
                "server_header": False,
                "date_header": False,
                "ws_max_size": 65_536,
                "ws_max_queue": 16,
                "limit_concurrency": 64,
                "timeout_keep_alive": 5,
            },
        )
    ]
    output = _plain(result.output)
    assert f"本机 Web 页面：http://{display_host}:8765/" in output
    assert f"Web API（健康检查）：http://{display_host}:8765/api/v1/health" in output


def test_web_help_does_not_load_optional_runtime(monkeypatch) -> None:
    def should_not_load():
        raise AssertionError("--help must not import optional Web dependencies")

    monkeypatch.setattr(main, "_load_web_runtime", should_not_load)

    result = runner.invoke(main.app, ["web", "--help"])

    assert result.exit_code == 0
    assert "Web 参与页、只读观战" in _plain(result.output)
