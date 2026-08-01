"""命令行界面：Typer + Rich。只消费 Match 事件做渲染，不含任何引擎逻辑。"""

from __future__ import annotations

import asyncio
import math
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
from llmolympic.core.player import (
    DEFAULT_LLM_TIMEOUT_SECONDS,
    HumanPlayer,
    LLMPlayer,
    Player,
)
from llmolympic.core.series import SeriesArchive, play_two_leg_series
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


def _resolve_llm_timeout(
    explicit: float | None,
    *,
    disabled: bool = False,
) -> float | None:
    """解析 CLI > 环境变量 > config.toml > 默认值的 LLM 单步限时。"""
    if disabled:
        if explicit is not None:
            raise typer.BadParameter(
                "--llm-timeout 不能与 --no-llm-timeout 同时使用",
                param_hint="--llm-timeout",
            )
        return None
    raw = explicit
    if raw is None:
        raw = cfg_get(
            "match",
            "llm_timeout_seconds",
            str(DEFAULT_LLM_TIMEOUT_SECONDS),
            env="LLMOLYMPIC_LLM_TIMEOUT",
        )
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise typer.BadParameter("LLM 单步超时必须是数字", param_hint="--llm-timeout") from exc
    if not math.isfinite(value) or value <= 0:
        raise typer.BadParameter(
            "LLM 单步超时必须是大于 0 的有限秒数",
            param_hint="--llm-timeout",
        )
    return value


def _validate_human_timeout(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        raise typer.BadParameter(
            "人类行动超时必须是大于 0 的有限秒数",
            param_hint="--timeout",
        )
    return value


def _parse_players(
    spec: str,
    human_timeout: float,
    llm_timeout: float | None = None,
) -> list[Player]:
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
        players.append(
            LLMPlayer(
                name=f"{kind}:{ident}",
                provider=provider,
                model=ident,
                move_timeout_seconds=llm_timeout,
            )
        )
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
        if ev.data.get("termination") == "technical_loss":
            console.print(
                f"[bold red]技术负[/] {ev.data.get('forfeited_by')} · "
                f"{ev.data.get('reason_code')}"
            )
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

    _render_rating_changes(result)


def _render_rating_changes(result: SaveResult, *, title: str = "ELO 更新") -> None:
    """显示单场或系列赛最终写入榜单的净变化。"""

    table = Table(title=title)
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


def _prepare_contest(
    *,
    game_name: str,
    player_spec: str,
    rounds: int | None,
    timeout: float,
    llm_timeout: float | None,
    no_llm_timeout: bool,
    require_two: bool = False,
    allow_human: bool = True,
) -> tuple[Game, list[Player]]:
    """按固定顺序完成项目、人数、超时与 Provider 校验。"""

    try:
        game_options = {} if rounds is None else {"rounds": rounds}
        selected_game = create_game(game_name, **game_options)
    except ValueError as exc:
        param_hint = "--rounds" if rounds is not None and game_name in list_games() else "--game"
        raise typer.BadParameter(str(exc), param_hint=param_hint) from exc

    try:
        player_specs = _split_player_specs(player_spec)
        if require_two and len(player_specs) != 2:
            raise typer.BadParameter(
                "交换先后手的双局赛需要恰好 2 名选手",
                param_hint="--players",
            )
        validate_player_count(selected_game, len(player_specs))
    except typer.BadParameter:
        raise
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--players") from exc

    has_human_player = any(spec.partition(":")[0] == "human" for spec in player_specs)
    if has_human_player and not allow_human:
        raise typer.BadParameter(
            "双局赛暂只支持 LLM/mock；终端人类输入超时后无法安全取消，暂未开放",
            param_hint="--players",
        )
    try:
        human_timeout = _validate_human_timeout(timeout) if has_human_player else timeout
        has_llm_player = any(spec.partition(":")[0] != "human" for spec in player_specs)
        effective_llm_timeout = None
        if llm_timeout is not None or no_llm_timeout:
            effective_llm_timeout = _resolve_llm_timeout(
                llm_timeout,
                disabled=no_llm_timeout,
            )
        elif has_llm_player:
            effective_llm_timeout = _resolve_llm_timeout(None)
        selected_players = _parse_players(player_spec, human_timeout, effective_llm_timeout)
    except typer.BadParameter:
        raise
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--players") from exc
    try:
        validate_players(selected_game, [player.name for player in selected_players])
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--players") from exc
    return selected_game, selected_players


async def _run(
    game: Game, players: list[Player], seed: int, store: SQLiteStore
) -> None:
    archive = await play_match(game, players, seed=seed, on_event=_render)
    result = store.save_match(archive)
    _render_saved(archive, store, result)


def _render_series_summary(series_archive: SeriesArchive) -> None:
    table = Table(title="双局赛结果")
    table.add_column("选手")
    table.add_column("胜-平-负", justify="right")
    table.add_column("局分", justify="right")
    table.add_column("技术负", justify="right")
    for descriptor in series_archive.players:
        player = descriptor["name"]
        standing = series_archive.standings[player]
        table.add_row(
            player,
            f"{standing.wins}-{standing.draws}-{standing.losses}",
            f"{standing.points:.1f}",
            str(standing.technical_losses),
        )
    console.print(table)


def _render_series_saved(
    series_archive: SeriesArchive,
    store: SQLiteStore,
    result: SaveResult,
) -> None:
    if not result.inserted:
        console.print(
            f"[yellow]系列赛 {series_archive.series_id} 已存在，未重复更新 ELO。[/]"
        )
        return
    first, second = series_archive.legs
    console.print(
        f"[green]✓ 两局已原子存档[/]  {series_archive.series_id}\n"
        f"  第 1 局 {first.match_id}\n"
        f"  第 2 局 {second.match_id}\n"
        f"  {store.path}"
    )
    _render_rating_changes(result, title="系列赛 ELO 净变化")


async def _run_series(
    game: Game,
    players: list[Player],
    seed: int,
    store: SQLiteStore,
) -> None:
    console.print(
        Panel(
            f"项目 [bold]{game.name}[/] · seed={seed}\n"
            "两局使用同一局面条件，第二局交换选手顺序",
            title="双局赛开始",
        )
    )

    def render_leg(leg_number: int, event: MatchEvent) -> None:
        if event.type == EventType.MATCH_STARTED:
            first, second = (descriptor["name"] for descriptor in event.data["players"])
            if game.name == "gomoku":
                seats = f"{first}（黑） vs {second}（白）"
            elif game.name == "chess":
                seats = f"{first}（白） vs {second}（黑）"
            else:
                seats = f"{first}（第一席） vs {second}（第二席）"
            console.rule(f"第 {leg_number}/2 局 · {seats}")
        _render(event)

    series_archive = await play_two_leg_series(
        game,
        players,
        seed=seed,
        on_event=render_leg,
    )
    _render_series_summary(series_archive)
    result = store.save_series(series_archive)
    _render_series_saved(series_archive, store, result)


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
        help="题目型项目的每人题数（棋类项目不适用；默认 5）",
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
    llm_timeout: float | None = typer.Option(
        None,
        "--llm-timeout",
        min=0.001,
        help=(
            "LLM 每步限时（秒）；默认读取 LLMOLYMPIC_LLM_TIMEOUT / "
            "match.llm_timeout_seconds / 120"
        ),
    ),
    no_llm_timeout: bool = typer.Option(
        False,
        "--no-llm-timeout",
        help=(
            "禁用比赛层 LLM 截止时间；Provider 自身网络超时仍可能生效，"
            "仅建议用于旧同步适配器"
        ),
    ),
    database: Annotated[
        Path | None,
        typer.Option("--db", help="SQLite 文件；默认读取 LLMOLYMPIC_DB / storage.database"),
    ] = None,
) -> None:
    """开始一场对局，结束后自动存档并更新 ELO。"""
    selected_game, selected_players = _prepare_contest(
        game_name=game,
        player_spec=players,
        rounds=rounds,
        timeout=timeout,
        llm_timeout=llm_timeout,
        no_llm_timeout=no_llm_timeout,
    )
    store = _open_store(database)
    try:
        asyncio.run(_run(selected_game, selected_players, seed, store))
    except (OSError, sqlite3.Error, StorageError) as exc:
        console.print(f"[red]对局已完成，但 SQLite 存档失败：{exc}[/]")
        raise typer.Exit(code=1) from exc


@app.command()
def series(
    game: str = typer.Option(
        "gomoku",
        "--game",
        "-g",
        help=f"比赛项目: {', '.join(list_games())}",
    ),
    players: str = typer.Option(
        "mock:random,mock:fixed",
        "--players",
        "-p",
        help="恰好两个非人类选手，如 openai:gpt-4o-mini,ollama:llama3.1",
    ),
    rounds: int | None = typer.Option(
        None,
        "--rounds",
        "-n",
        min=1,
        help="题目型项目的每人题数（棋类项目不适用；默认 5）",
    ),
    seed: int = typer.Option(
        0,
        "--seed",
        "-s",
        min=SQLITE_INT_MIN,
        max=SQLITE_INT_MAX,
        help="两局共用的随机种子（用于复现题目或初始局面）",
    ),
    llm_timeout: float | None = typer.Option(
        None,
        "--llm-timeout",
        min=0.001,
        help=(
            "LLM 每步限时（秒）；默认读取 LLMOLYMPIC_LLM_TIMEOUT / "
            "match.llm_timeout_seconds / 120"
        ),
    ),
    no_llm_timeout: bool = typer.Option(
        False,
        "--no-llm-timeout",
        help=(
            "禁用比赛层 LLM 截止时间；Provider 自身网络超时仍可能生效，"
            "仅建议用于旧同步适配器"
        ),
    ),
    database: Annotated[
        Path | None,
        typer.Option("--db", help="SQLite 文件；默认读取 LLMOLYMPIC_DB / storage.database"),
    ] = None,
) -> None:
    """两名选手交换顺序各赛一局，并以公平批次更新 ELO。"""

    selected_game, selected_players = _prepare_contest(
        game_name=game,
        player_spec=players,
        rounds=rounds,
        timeout=60.0,
        llm_timeout=llm_timeout,
        no_llm_timeout=no_llm_timeout,
        require_two=True,
        allow_human=False,
    )
    store = _open_store(database)
    try:
        asyncio.run(_run_series(selected_game, selected_players, seed, store))
    except (OSError, sqlite3.Error, StorageError) as exc:
        console.print(f"[red]双局赛已完成，但 SQLite 原子存档失败：{exc}[/]")
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
        series_text = ""
        if row.series_id is not None:
            series_text = f"  系列 {row.series_id} 第 {row.leg_number}/2 局\n"
        console.print(
            f"[bold cyan]{row.match_id}[/]  {row.game}  "
            f"{row.finished_at.astimezone().strftime('%Y-%m-%d %H:%M')}\n"
            f"{series_text}"
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
    match_id: str = typer.Argument(..., help="对局或系列赛 ID"),
    database: Annotated[Path | None, typer.Option("--db", help="SQLite 文件")] = None,
) -> None:
    """输出一场对局或一个双局赛的完整 JSON 档案。"""
    try:
        store = _open_store(database, create=False)
        archive = store.get_match(match_id)
        if archive is None:
            archive = store.get_series(match_id)
    except (OSError, sqlite3.Error, StorageError, ValueError) as exc:
        console.print(f"[red]无法读取 SQLite 对局档案：{exc}[/]")
        raise typer.Exit(code=1) from exc
    if archive is None:
        console.print(f"[red]未找到对局或系列赛 {match_id!r}。[/]")
        raise typer.Exit(code=1)
    console.print_json(archive.to_json())
