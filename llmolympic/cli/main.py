"""命令行界面：Typer + Rich。只消费 Match 事件做渲染，不含任何引擎逻辑。"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from llmolympic.config import get as cfg_get
from llmolympic.core.events import EventType, MatchEvent
from llmolympic.core.match import Match
from llmolympic.core.player import HumanPlayer, LLMPlayer, Player
from llmolympic.games import create_game, list_games
from llmolympic.providers import create_provider

app = typer.Typer(
    help="LLM Olympics —— 人类与 LLM 的多项目竞技场",
    no_args_is_help=True,
)
console = Console()


def _parse_players(spec: str, human_timeout: float) -> list[Player]:
    """把 ``openai:gpt-4o-mini,human:小明`` 这样的规格解析成选手列表。

    模型名可省略（如 ``openai``），此时回退到 config.toml 里的
    ``[<provider>] default_model``；``mock`` 省略时默认为 random 策略；
    ``human`` 省略名字时默认为"人类"。
    """
    players: list[Player] = []
    for token in spec.split(","):
        kind, _, ident = token.strip().partition(":")
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


async def _run(game_name: str, players: list[Player], rounds: int, seed: int) -> None:
    game = create_game(game_name, rounds=rounds)
    match = Match(game, players, seed=seed)
    async for ev in match.run():
        _render(ev)


@app.command()
def play(
    game: str = typer.Option("math_quiz", "--game", "-g", help=f"比赛项目: {', '.join(list_games())}"),
    players: str = typer.Option(
        "mock:random,mock:fixed",
        "--players",
        "-p",
        help="逗号分隔的选手规格，如 openai:gpt-4o-mini,human:小明,ollama:llama3.1",
    ),
    rounds: int = typer.Option(5, "--rounds", "-n", help="每人题数"),
    seed: int = typer.Option(0, "--seed", "-s", help="随机种子（同 seed 同题）"),
    timeout: float = typer.Option(60.0, "--timeout", "-t", help="人类选手每题限时（秒）"),
) -> None:
    """开始一场对局。"""
    asyncio.run(_run(game, _parse_players(players, timeout), rounds, seed))


@app.command(name="games")
def list_available_games() -> None:
    """列出已注册的比赛项目。"""
    for name in list_games():
        console.print(f"- {name}")
