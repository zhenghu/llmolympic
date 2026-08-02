"""CLI terminal hardening regression tests."""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console
from typer.testing import CliRunner

from llmolympic.cli import main as cli_main
from llmolympic.cli.terminal import literal_text, sanitize_terminal_text
from llmolympic.core.events import EventType, MatchEvent
from llmolympic.core.storage import SQLiteStore

runner = CliRunner()


def test_literal_rendering_preserves_markup_as_text_and_filters_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = StringIO()
    monkeypatch.setattr(
        cli_main,
        "console",
        Console(file=stream, force_terminal=False, color_system=None, width=200),
    )
    payload = "42[/]\x1b[2J\x9b31m\u202espoof"

    cli_main._render(
        MatchEvent(
            seq=1,
            type=EventType.MOVE_RECEIVED,
            player="model[bold]name[/]",
            data={"move": payload},
        )
    )

    rendered = stream.getvalue()
    assert "model[bold]name[/]" in rendered
    assert "42[/]" in rendered
    assert "\x1b" not in rendered
    assert "\x9b" not in rendered
    assert "\u202e" not in rendered
    assert "�[2J�31m�spoof" in rendered


def test_terminal_sanitizer_normalizes_multiline_text_and_caps_display_length() -> None:
    assert sanitize_terminal_text("a\r\nb\tc", multiline=True) == "a\nb�c"
    assert sanitize_terminal_text("\x00\x1f\x7f\x80\u2066x\u2069") == "�����x�"

    rendered = literal_text("x" * 100, max_chars=12)
    assert len(rendered.plain) == 12
    assert rendered.plain.endswith("…（已截断）")


def test_single_match_is_saved_when_event_renderer_fails(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "render-failure.db"

    def broken_renderer(event: MatchEvent) -> None:
        raise RuntimeError("injected terminal failure")

    monkeypatch.setattr(cli_main, "_render", broken_renderer)
    result = runner.invoke(
        cli_main.app,
        [
            "play",
            "--players",
            "mock:fixed,mock:random",
            "--rounds",
            "1",
            "--db",
            str(path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "终端显示失败" in result.output
    assert len(SQLiteStore(path).list_matches()) == 1


def test_series_is_saved_before_summary_rendering(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "series-render-failure.db"

    def broken_summary(*args: object) -> None:
        raise RuntimeError("injected terminal failure")

    monkeypatch.setattr(cli_main, "_render_series_summary", broken_summary)
    result = runner.invoke(
        cli_main.app,
        [
            "series",
            "--game",
            "gomoku",
            "--players",
            "mock:fixed,mock:illegal",
            "--db",
            str(path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "终端显示失败" in result.output
    store = SQLiteStore(path)
    rows = store.list_matches(game="gomoku")
    assert len(rows) == 2
    assert rows[0].series_id is not None
    assert store.get_series(rows[0].series_id) is not None


@pytest.mark.parametrize(
    ("command", "option"),
    [
        ("play", "--rounds"),
        ("series", "--rounds"),
        ("history", "--limit"),
        ("leaderboard", "--limit"),
    ],
)
def test_cli_rejects_resource_limit_above_safe_maximum(tmp_path, command: str, option: str) -> None:
    path = tmp_path / f"{command}-over-limit.db"
    maximum = "100" if option == "--rounds" else "1000"

    result = runner.invoke(
        cli_main.app,
        [command, option, str(int(maximum) + 1), "--db", str(path)],
    )

    assert result.exit_code == 2
    assert not path.exists()
