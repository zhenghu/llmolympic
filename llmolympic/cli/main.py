"""命令行界面：Typer + Rich。只消费 Match 事件做渲染，不含任何引擎逻辑。"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from llmolympic.config import get as cfg_get
from llmolympic.core.archive import MatchArchive
from llmolympic.core.events import EventType, MatchEvent
from llmolympic.core.game import Game, validate_player_count, validate_players
from llmolympic.core.match import play_match
from llmolympic.core.player import HumanPlayer, LLMPlayer, Player
from llmolympic.core.storage import (
    SQLITE_INT_MAX,
    SQLITE_INT_MIN,
    SaveResult,
    SQLiteStore,
    StorageError,
)
from llmolympic.games import create_game, list_games
from llmolympic.providers import create_provider

app = typer.Typer(
    help="LLM Olympics —— 人类与 LLM 的多项目竞技场",
    no_args_is_help=True,
)
console = Console()


def _split_player_specs(spec: str) -> list[str]:
    tokens = [token.strip() for token in spec.split(",")]
    if any(not token for token in tokens):
        raise typer.BadParameter("选手规格不能为空", param_hint="--players")
    return tokens


def _parse_players(spec: str, human_timeout: float) -> list[Player]:
    """把 ``openai:gpt-4o-mini,human:小明`` 这样的规格解析成选手列表。

    模型名可省略（如 ``openai``），此时回退到 config.toml 里的
    ``[<provider>] default_model``；``mock`` 省略时默认为 random 策略；
    ``human`` 省略名字时默认为"人类"。
    """
    players: list[Player] = []
    for token in _split_player_specs(spec):
        kind, _, ident = token.partition(":")
        if kind == "human":
            players.append(HumanPlayer(name=ident or "人类", timeout=human_timeout))
            continue
        if kind == "mock":
            ident = ident or "random"
        elif not ident:
            ident = cfg_get(kind, "default_model") or ""
            if not ident:
                raise typer.BadParameter(
                    f"选手 {kind!r} 未指定模型名，且 config.toml 的 [{kind}] 段里也没有 default_model"
                )
        provider = create_provider(kind, ident)
        players.append(LLMPlayer(name=f"{kind}:{ident}", provider=provider, model=ident))
    names = [player.name for player in players]
    if len(set(names)) != len(names):
        raise typer.BadParameter(f"选手名字必须唯一: {names}", param_hint="--players")
    return players


def _render(ev: MatchEvent) -> None:
    if ev.type == EventType.MATCH_STARTED:
        names = " vs ".join(p["name"] for p in ev.data["players"])
        console.print(
            Panel(
                f"项目 [bold]{ev.data['game']}[/] · seed={ev.data['seed']}\n选手：{names}",
                title="对局开始",
            )
        )
    elif ev.type == EventType.TURN_PROMPT:
        console.print(Panel(ev.data["prompt"], title=f"[cyan]{ev.player}[/] 的题面"))
    elif ev.type == EventType.MOVE_RECEIVED:
        console.print(f"  [green]✓[/] {ev.player}: {ev.data['move']}")
    elif ev.type == EventType.MOVE_REJECTED:
        console.print(f"  [yellow]✗ {ev.player}: {ev.data.get('reason')}[/]")
    elif ev.type == EventType.MATCH_FINISHED:
        scores = ev.data["scores"]
        table = Table(title="最终比分")
        table.add_column("排名", justify="right")
        table.add_column("选手")
        table.add_column("得分", justify="right")
        for rank, (name, s) in enumerate(
            sorted(scores.items(), key=lambda kv: kv[1], reverse=True), start=1
        ):
            table.add_row(str(rank), name, f"{s:.2f}")
        console.print(table)


def _open_store(path: Path | None, *, create: bool = True) -> SQLiteStore:
    try:
        return SQLiteStore(path, create=create)
    except (OSError, sqlite3.Error, StorageError) as exc:
        console.print(f"[red]无法打开 SQLite 数据库：{exc}[/]")
        raise typer.Exit(code=1) from exc


def _render_saved(archive: MatchArchive, store: SQLiteStore, result: SaveResult) -> None:
    if not result.inserted:
        console.print(f"[yellow]档案 {archive.match_id} 已存在，未重复更新 ELO。[/]")
        return

    console.print(f"[green]✓ 对局已存档[/]  {archive.match_id}\n  {store.path}")
    if not result.rated:
        console.print("[yellow]本场不是双人对局，未计入 ELO。[/]")
        return

    table = Table(title="ELO 更新")
    table.add_column("榜单")
    table.add_column("选手")
    table.add_column("原分", justify="right")
    table.add_column("新分", justify="right")
    table.add_column("变化", justify="right")
    for change in result.rating_changes:
        scope = "总榜" if change.game is None else change.game
        delta = change.after - change.before
        table.add_row(
            scope,
            change.player,
            f"{change.before:.1f}",
            f"{change.after:.1f}",
            f"{delta:+.1f}",
        )
    console.print(table)


async def _run(
    game: Game, players: list[Player], seed: int, store: SQLiteStore
) -> None:
    archive = await play_match(game, players, seed=seed, on_event=_render)
    result = store.save_match(archive)
    _render_saved(archive, store, result)


@app.command()
def play(
    game: str = typer.Option("math_quiz", "--game", "-g", help=f"比赛项目: {', '.join(list_games())}"),
    players: str = typer.Option(
        "mock:random,mock:fixed",
        "--players",
        "-p",
        help="逗号分隔的选手规格，如 openai:gpt-4o-mini,human:小明,ollama:llama3.1",
    ),
    rounds: int | None = typer.Option(
        None,
        "--rounds",
        "-n",
        min=1,
        help="问答项目的每人题数（五子棋不适用；默认 5）",
    ),
    seed: int = typer.Option(
        0,
        "--seed",
        "-s",
        min=SQLITE_INT_MIN,
        max=SQLITE_INT_MAX,
        help="随机种子（用于复现题目或初始局面）",
    ),
    timeout: float = typer.Option(
        60.0, "--timeout", "-t", min=0.001, help="人类选手每次行动限时（秒）"
    ),
    database: Annotated[
        Path | None,
        typer.Option("--db", help="SQLite 文件；默认读取 LLMOLYMPIC_DB / storage.database"),
    ] = None,
) -> None:
    """开始一场对局，结束后自动存档并更新 ELO。"""
    try:
        game_options = {} if rounds is None else {"rounds": rounds}
        selected_game = create_game(game, **game_options)
    except ValueError as exc:
        param_hint = "--rounds" if rounds is not None and game in list_games() else "--game"
        raise typer.BadParameter(str(exc), param_hint=param_hint) from exc
    try:
        validate_player_count(selected_game, len(_split_player_specs(players)))
    except typer.BadParameter:
        raise
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--players") from exc
    try:
        selected_players = _parse_players(players, timeout)
    except typer.BadParameter:
        raise
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--players") from exc
    try:
        validate_players(selected_game, [player.name for player in selected_players])
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--players") from exc
    store = _open_store(database)
    try:
        asyncio.run(_run(selected_game, selected_players, seed, store))
    except (OSError, sqlite3.Error, StorageError) as exc:
        console.print(f"[red]对局已完成，但 SQLite 存档失败：{exc}[/]")
        raise typer.Exit(code=1) from exc


@app.command(name="games")
def list_available_games() -> None:
    """列出已注册的比赛项目。"""
    for name in list_games():
        console.print(f"- {name}")


@app.command()
def history(
    game: str | None = typer.Option(None, "--game", "-g", help="只看指定比赛项目"),
    limit: int = typer.Option(20, "--limit", "-n", min=1, help="最多显示多少场"),
    database: Annotated[Path | None, typer.Option("--db", help="SQLite 文件")] = None,
) -> None:
    """查看最近的对局档案。"""
    try:
        rows = _open_store(database, create=False).list_matches(limit=limit, game=game)
    except (OSError, sqlite3.Error, StorageError, ValueError) as exc:
        console.print(f"[red]无法读取 SQLite 对局历史：{exc}[/]")
        raise typer.Exit(code=1) from exc
    if not rows:
        console.print("暂无对局档案。")
        return

    console.print("[bold]对局历史[/]")
    for row in rows:
        score_text = " · ".join(f"{name} {row.scores[name]:.2f}" for name in row.players)
        console.print(
            f"[bold cyan]{row.match_id}[/]  {row.game}  "
            f"{row.finished_at.astimezone().strftime('%Y-%m-%d %H:%M')}\n"
            f"  {score_text}"
        )


@app.command()
def leaderboard(
    game: str | None = typer.Option(None, "--game", "-g", help="项目榜；省略则显示总榜"),
    limit: int = typer.Option(50, "--limit", "-n", min=1, help="最多显示多少名"),
    database: Annotated[Path | None, typer.Option("--db", help="SQLite 文件")] = None,
) -> None:
    """查看持久化 ELO 排行榜。"""
    try:
        entries = _open_store(database, create=False).leaderboard(game=game, limit=limit)
    except (OSError, sqlite3.Error, StorageError, ValueError) as exc:
        console.print(f"[red]无法读取 SQLite ELO 榜：{exc}[/]")
        raise typer.Exit(code=1) from exc
    if not entries:
        console.print("暂无 ELO 记录。")
        return

    table = Table(title=f"ELO {'总榜' if game is None else game}")
    table.add_column("排名", justify="right")
    table.add_column("选手")
    table.add_column("等级分", justify="right")
    table.add_column("场次", justify="right")
    table.add_column("胜-平-负", justify="right")
    for rank, entry in enumerate(entries, start=1):
        table.add_row(
            str(rank),
            entry.player,
            f"{entry.rating:.1f}",
            str(entry.games_played),
            f"{entry.wins}-{entry.draws}-{entry.losses}",
        )
    console.print(table)


@app.command(name="archive")
def show_archive(
    match_id: str = typer.Argument(..., help="对局 ID"),
    database: Annotated[Path | None, typer.Option("--db", help="SQLite 文件")] = None,
) -> None:
    """输出一场对局的完整 JSON 档案。"""
    try:
        archive = _open_store(database, create=False).get_match(match_id)
    except (OSError, sqlite3.Error, StorageError, ValueError) as exc:
        console.print(f"[red]无法读取 SQLite 对局档案：{exc}[/]")
        raise typer.Exit(code=1) from exc
    if archive is None:
        console.print(f"[red]未找到对局 {match_id!r}。[/]")
        raise typer.Exit(code=1)
    console.print_json(archive.to_json())
