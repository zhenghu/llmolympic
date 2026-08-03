"""命令行界面：Typer + Rich。只消费 Match 事件做渲染，不含任何引擎逻辑。"""

from __future__ import annotations

import asyncio
import hashlib
import math
import sqlite3
from collections import Counter
from collections.abc import Callable
from itertools import combinations
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from llmolympic.cli.terminal import (
    ARCHIVE_DISPLAY_LIMIT,
    NAME_DISPLAY_LIMIT,
    PROMPT_DISPLAY_LIMIT,
    literal_text,
)
from llmolympic.config import get as cfg_get
from llmolympic.config import get_profile
from llmolympic.core.archive import MatchArchive
from llmolympic.core.events import EventType, MatchEvent
from llmolympic.core.game import (
    MAX_PLAYER_NAME_CHARS,
    Game,
    describe_game_config,
    validate_player_count,
    validate_players,
)
from llmolympic.core.match import play_match
from llmolympic.core.player import (
    DEFAULT_LLM_TIMEOUT_SECONDS,
    HumanPlayer,
    LLMPlayer,
    Player,
    profile_entrant_id,
)
from llmolympic.core.series import SeriesArchive, play_two_leg_series
from llmolympic.core.storage import (
    SQLITE_INT_MAX,
    SQLITE_INT_MIN,
    SaveResult,
    SQLiteStore,
    StorageError,
    TournamentSaveResult,
)
from llmolympic.core.tournament import (
    MAX_TOURNAMENT_PLAYERS,
    MIN_TOURNAMENT_PLAYERS,
    TournamentArchive,
    play_round_robin,
)
from llmolympic.games import create_game, list_games
from llmolympic.providers import create_profile_provider, create_provider

app = typer.Typer(
    help="LLM Olympics —— 人类与 LLM 的多项目竞技场",
    no_args_is_help=True,
)
console = Console()

MAX_UNCONFIRMED_TOURNAMENT_MATCHES = 30
MAX_UNCONFIRMED_TOURNAMENT_PROVIDER_CALLS = 5_000
TOURNAMENT_MOVE_ATTEMPTS = 3


def _render_warning() -> None:
    """尽力报告 UI 故障；报告本身失败也不能影响比赛或存档。"""

    try:
        console.print(Text("⚠ 终端显示失败；比赛将继续并优先保存档案。", style="yellow"))
    except Exception:  # noqa: BLE001 - UI failures must never abort persistence
        return


def _best_effort_render(render: Callable[..., None], *args: object) -> None:
    """执行非关键渲染，确保显示异常不会改变比赛与持久化语义。"""

    try:
        render(*args)
    except Exception:  # noqa: BLE001 - rendering is intentionally non-critical
        _render_warning()


def _guard_renderer(render: Callable[..., None]) -> Callable[..., None]:
    """把事件回调变成只失败一次的非关键渲染器。"""

    disabled = False

    def guarded(*args: object) -> None:
        nonlocal disabled
        if disabled:
            return
        try:
            render(*args)
        except Exception:  # noqa: BLE001 - event callbacks cannot own match control flow
            disabled = True
            _render_warning()

    return guarded


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


def _disambiguate_duplicate_display_names(players: list[Player]) -> None:
    """为同名但身份不同的选手生成稳定、可读的本场显示名。"""

    counts = Counter(player.name for player in players)
    if all(count == 1 for count in counts.values()):
        return

    occupied = {name for name, count in counts.items() if count == 1}
    for player in players:
        if counts[player.name] == 1:
            continue
        if isinstance(player, LLMPlayer) and player.profile_id is not None:
            label = f"{player.profile_id}:{player.model}"
        else:
            label = player.entrant_id
        digest = hashlib.sha256(player.entrant_id.encode("utf-8")).hexdigest()[:8]
        if len(label) > 64:
            label = f"{label[:54]}…{digest}"
        suffix = f" [{label}]"
        base = player.name[: MAX_PLAYER_NAME_CHARS - len(suffix)].rstrip()
        canonical = f"{base or '选手'}{suffix}"
        candidate = canonical
        discriminator = 2
        while candidate in occupied:
            counter = f" #{discriminator}"
            prefix = canonical[: MAX_PLAYER_NAME_CHARS - len(counter)].rstrip()
            candidate = f"{prefix or '选手'}{counter}"
            discriminator += 1
        player.name = candidate
        occupied.add(candidate)


def _parse_players(
    spec: str,
    human_timeout: float,
    llm_timeout: float | None = None,
) -> list[Player]:
    """把 ``profile:kimi:moonshot-v1,human:小明`` 等规格解析成选手。

    模型名可省略（如 ``openai``），此时回退到 config.toml 里的
    ``[<provider>] default_model``；``mock`` 省略时默认为 random 策略；
    ``human`` 省略名字时默认为"人类"。``profile:<id>[:model]``
    使用 ``[profiles.<id>]``，并生成与显示名分离的稳定 entrant ID。
    """
    players: list[Player] = []
    for token in _split_player_specs(spec):
        kind, _, ident = token.partition(":")
        if kind == "human":
            players.append(HumanPlayer(name=ident or "人类", timeout=human_timeout))
            continue
        if kind == "profile":
            profile_id, separator, explicit_model = ident.partition(":")
            if not profile_id:
                raise typer.BadParameter(
                    "Profile 选手必须使用 profile:<id>[:model]",
                    param_hint="--players",
                )
            if separator and not explicit_model.strip():
                raise typer.BadParameter("Profile 模型名不能为空", param_hint="--players")
            profile = get_profile(profile_id)
            model = explicit_model.strip() if separator else profile.default_model
            if not model:
                raise typer.BadParameter(
                    f"Provider Profile {profile_id!r} 未指定模型，且没有 default_model",
                    param_hint="--players",
                )
            provider = create_profile_provider(profile)
            entrant_id = profile_entrant_id(profile_id, model)
            players.append(
                LLMPlayer(
                    name=profile.display_name or entrant_id,
                    provider=provider,
                    model=model,
                    move_timeout_seconds=llm_timeout,
                )
            )
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
    entrant_ids = [player.entrant_id for player in players]
    if len(set(entrant_ids)) != len(entrant_ids):
        raise typer.BadParameter(
            f"选手稳定身份必须唯一: {entrant_ids}",
            param_hint="--players",
        )
    _disambiguate_duplicate_display_names(players)
    names = [player.name for player in players]
    if len(set(names)) != len(names):
        raise typer.BadParameter(f"选手名字必须唯一: {names}", param_hint="--players")
    return players


def _render(ev: MatchEvent) -> None:
    if ev.type == EventType.MATCH_STARTED:
        body = Text("项目 ")
        body.append(literal_text(ev.data["game"], style="bold", max_chars=NAME_DISPLAY_LIMIT))
        body.append(" · seed=")
        body.append(literal_text(ev.data["seed"], max_chars=NAME_DISPLAY_LIMIT))
        body.append("\n选手：")
        for index, player in enumerate(ev.data["players"]):
            if index:
                body.append(" vs ")
            body.append(literal_text(player["name"], max_chars=NAME_DISPLAY_LIMIT))
        console.print(Panel(body, title=Text("对局开始")))
    elif ev.type == EventType.TURN_PROMPT:
        title = literal_text(ev.player or "", style="cyan", max_chars=NAME_DISPLAY_LIMIT)
        title.append(" 的题面")
        console.print(
            Panel(
                literal_text(
                    ev.data["prompt"],
                    max_chars=PROMPT_DISPLAY_LIMIT,
                    multiline=True,
                ),
                title=title,
            )
        )
    elif ev.type == EventType.MOVE_RECEIVED:
        line = Text("  ")
        line.append("✓", style="green")
        line.append(" ")
        line.append(literal_text(ev.player or "", max_chars=NAME_DISPLAY_LIMIT))
        line.append(": ")
        line.append(literal_text(ev.data["move"]))
        console.print(line)
    elif ev.type == EventType.MOVE_REJECTED:
        line = Text("  ")
        line.append("✗ ", style="yellow")
        line.append(literal_text(ev.player or "", max_chars=NAME_DISPLAY_LIMIT))
        line.append(": ")
        line.append(literal_text(ev.data.get("reason"), style="yellow"))
        console.print(line)
    elif ev.type == EventType.MATCH_FINISHED:
        if ev.data.get("termination") == "technical_loss":
            line = Text("技术负", style="bold red")
            line.append(" ")
            line.append(literal_text(ev.data.get("forfeited_by"), max_chars=NAME_DISPLAY_LIMIT))
            line.append(" · ")
            line.append(literal_text(ev.data.get("reason_code")))
            console.print(line)
        scores = ev.data["scores"]
        table = Table(title="最终比分")
        table.add_column("排名", justify="right")
        table.add_column("选手")
        table.add_column("得分", justify="right")
        for rank, (name, s) in enumerate(
            sorted(scores.items(), key=lambda kv: kv[1], reverse=True), start=1
        ):
            table.add_row(
                Text(str(rank)),
                literal_text(name, max_chars=NAME_DISPLAY_LIMIT),
                Text(f"{s:.2f}"),
            )
        console.print(table)


def _open_store(path: Path | None, *, create: bool = True) -> SQLiteStore:
    try:
        return SQLiteStore(path, create=create)
    except (OSError, sqlite3.Error, StorageError) as exc:
        line = Text("无法打开 SQLite 数据库：", style="red")
        line.append(literal_text(exc))
        console.print(line)
        raise typer.Exit(code=1) from exc


def _render_saved(archive: MatchArchive, store: SQLiteStore, result: SaveResult) -> None:
    if not result.inserted:
        line = Text("档案 ", style="yellow")
        line.append(literal_text(archive.match_id, max_chars=NAME_DISPLAY_LIMIT))
        line.append(" 已存在，未重复更新 ELO。")
        console.print(line)
        return

    line = Text("✓ 对局已存档", style="green")
    line.append("  ")
    line.append(literal_text(archive.match_id, max_chars=NAME_DISPLAY_LIMIT))
    line.append("\n  ")
    line.append(literal_text(store.path))
    console.print(line)
    if not result.rated:
        console.print(Text("本场不是双人对局，未计入 ELO。", style="yellow"))
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
            literal_text(scope, max_chars=NAME_DISPLAY_LIMIT),
            literal_text(change.player, max_chars=NAME_DISPLAY_LIMIT),
            Text(f"{change.before:.1f}"),
            Text(f"{change.after:.1f}"),
            Text(f"{delta:+.1f}"),
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
    except (TypeError, ValueError) as exc:
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
    except (TypeError, ValueError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--players") from exc
    try:
        validate_players(selected_game, [player.name for player in selected_players])
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--players") from exc
    return selected_game, selected_players


def _prepare_round_robin(
    *,
    game_name: str,
    player_spec: str,
    rounds: int | None,
    llm_timeout: float | None,
    no_llm_timeout: bool,
) -> tuple[Game, list[Player]]:
    """校验循环赛名单，但只把每个二人配对交给 Game 的人数约束。"""

    try:
        game_options = {} if rounds is None else {"rounds": rounds}
        selected_game = create_game(game_name, **game_options)
    except ValueError as exc:
        param_hint = "--rounds" if rounds is not None and game_name in list_games() else "--game"
        raise typer.BadParameter(str(exc), param_hint=param_hint) from exc

    try:
        player_specs = _split_player_specs(player_spec)
        player_count = len(player_specs)
        if not MIN_TOURNAMENT_PLAYERS <= player_count <= MAX_TOURNAMENT_PLAYERS:
            raise typer.BadParameter(
                f"循环赛需要 {MIN_TOURNAMENT_PLAYERS} 到 {MAX_TOURNAMENT_PLAYERS} 名选手，"
                f"实际为 {player_count} 名",
                param_hint="--players",
            )
        if any(spec.partition(":")[0] == "human" for spec in player_specs):
            raise typer.BadParameter(
                "循环赛暂只支持 LLM/mock/Profile，不支持人类选手",
                param_hint="--players",
            )
        effective_llm_timeout = _resolve_llm_timeout(
            llm_timeout,
            disabled=no_llm_timeout,
        )
        selected_players = _parse_players(player_spec, 60.0, effective_llm_timeout)
        names = [player.name for player in selected_players]
        entrant_ids = [player.entrant_id for player in selected_players]
        if len(set(names)) != player_count:
            raise typer.BadParameter(f"选手名字必须唯一: {names}", param_hint="--players")
        if len(set(entrant_ids)) != player_count:
            raise typer.BadParameter(
                f"选手稳定身份必须唯一: {entrant_ids}",
                param_hint="--players",
            )
        for first, second in combinations(selected_players, 2):
            validate_players(selected_game, [first.name, second.name])
    except typer.BadParameter:
        raise
    except (TypeError, ValueError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--players") from exc
    return selected_game, selected_players


def _tournament_workload(
    game: Game,
    player_count: int,
) -> tuple[int, int, int | None, int | None]:
    pairing_count = player_count * (player_count - 1) // 2
    match_count = pairing_count * 2
    rounds = describe_game_config(game).get("rounds")
    if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 1:
        return pairing_count, match_count, None, None
    turns = match_count * 2 * rounds
    return pairing_count, match_count, turns, turns * TOURNAMENT_MOVE_ATTEMPTS


def _validate_tournament_workload(
    game: Game,
    player_count: int,
    *,
    allow_large: bool,
) -> None:
    _, match_count, turns, max_calls = _tournament_workload(game, player_count)
    too_large = match_count > MAX_UNCONFIRMED_TOURNAMENT_MATCHES or (
        max_calls is not None and max_calls > MAX_UNCONFIRMED_TOURNAMENT_PROVIDER_CALLS
    )
    if not too_large or allow_large:
        return
    estimate = f"{match_count} 场对局"
    if turns is not None and max_calls is not None:
        estimate += f"、{turns} 个回合（最多约 {max_calls} 次选手调用）"
    raise typer.BadParameter(
        f"循环赛预计需要 {estimate}，超过默认费用保护阈值；"
        "确认 Provider 预算与中断风险后使用 --allow-large-tournament",
        param_hint="--allow-large-tournament",
    )


async def _run(game: Game, players: list[Player], seed: int, store: SQLiteStore) -> None:
    archive = await play_match(game, players, seed=seed, on_event=_guard_renderer(_render))
    result = store.save_match(archive, rating_source="engine")
    _best_effort_render(_render_saved, archive, store, result)


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
            literal_text(player, max_chars=NAME_DISPLAY_LIMIT),
            Text(f"{standing.wins}-{standing.draws}-{standing.losses}"),
            Text(f"{standing.points:.1f}"),
            Text(str(standing.technical_losses)),
        )
    console.print(table)


def _render_series_saved(
    series_archive: SeriesArchive,
    store: SQLiteStore,
    result: SaveResult,
) -> None:
    if not result.inserted:
        line = Text("系列赛 ", style="yellow")
        line.append(literal_text(series_archive.series_id, max_chars=NAME_DISPLAY_LIMIT))
        line.append(" 已存在，未重复更新 ELO。")
        console.print(line)
        return
    first, second = series_archive.legs
    line = Text("✓ 两局已原子存档", style="green")
    line.append("  ")
    line.append(literal_text(series_archive.series_id, max_chars=NAME_DISPLAY_LIMIT))
    line.append("\n  第 1 局 ")
    line.append(literal_text(first.match_id, max_chars=NAME_DISPLAY_LIMIT))
    line.append("\n  第 2 局 ")
    line.append(literal_text(second.match_id, max_chars=NAME_DISPLAY_LIMIT))
    line.append("\n  ")
    line.append(literal_text(store.path))
    console.print(line)
    _render_rating_changes(result, title="系列赛 ELO 净变化")


async def _run_series(
    game: Game,
    players: list[Player],
    seed: int,
    store: SQLiteStore,
) -> None:
    intro = Text("项目 ")
    intro.append(literal_text(game.name, style="bold", max_chars=NAME_DISPLAY_LIMIT))
    intro.append(" · seed=")
    intro.append(literal_text(seed, max_chars=NAME_DISPLAY_LIMIT))
    intro.append("\n两局使用同一局面条件，第二局交换选手顺序")
    _best_effort_render(console.print, Panel(intro, title=Text("双局赛开始")))

    def render_leg(leg_number: int, event: MatchEvent) -> None:
        if event.type == EventType.MATCH_STARTED:
            first, second = (descriptor["name"] for descriptor in event.data["players"])
            title = Text(f"第 {leg_number}/2 局 · ")
            title.append(literal_text(first, max_chars=NAME_DISPLAY_LIMIT))
            if game.name == "gomoku":
                title.append("（黑） vs ")
            elif game.name == "chess":
                title.append("（白） vs ")
            else:
                title.append("（第一席） vs ")
            title.append(literal_text(second, max_chars=NAME_DISPLAY_LIMIT))
            if game.name == "gomoku":
                title.append("（白）")
            elif game.name == "chess":
                title.append("（黑）")
            else:
                title.append("（第二席）")
            console.rule(title)
        _render(event)

    guarded_render_leg = _guard_renderer(render_leg)
    series_archive = await play_two_leg_series(
        game,
        players,
        seed=seed,
        on_event=guarded_render_leg,
    )
    result = store.save_series(series_archive, rating_source="engine")
    _best_effort_render(_render_series_summary, series_archive)
    _best_effort_render(_render_series_saved, series_archive, store, result)


def _render_tournament_summary(tournament: TournamentArchive) -> None:
    table = Table(title="循环赛结果")
    table.add_column("排名", justify="right")
    table.add_column("选手")
    table.add_column("系列胜-平-负", justify="right")
    table.add_column("对局胜-平-负", justify="right")
    table.add_column("局分", justify="right")
    table.add_column("技术负", justify="right")
    for rank, standing in enumerate(tournament.standings, start=1):
        table.add_row(
            Text(str(rank)),
            literal_text(standing.player, max_chars=NAME_DISPLAY_LIMIT),
            Text(f"{standing.series_wins}-{standing.series_draws}-{standing.series_losses}"),
            Text(f"{standing.wins}-{standing.draws}-{standing.losses}"),
            Text(f"{standing.points:.1f}"),
            Text(str(standing.technical_losses)),
        )
    console.print(table)


def _render_tournament_rating_changes(result: TournamentSaveResult) -> None:
    table = Table(title="循环赛 ELO 净变化")
    table.add_column("榜单")
    table.add_column("选手")
    table.add_column("原分", justify="right")
    table.add_column("新分", justify="right")
    table.add_column("变化", justify="right")
    for change in result.rating_changes:
        scope = "总榜" if change.game is None else change.game
        table.add_row(
            literal_text(scope, max_chars=NAME_DISPLAY_LIMIT),
            literal_text(change.display_name, max_chars=NAME_DISPLAY_LIMIT),
            Text(f"{change.before:.1f}"),
            Text(f"{change.after:.1f}"),
            Text(f"{change.after - change.before:+.1f}"),
        )
    console.print(table)


def _render_tournament_saved(
    tournament: TournamentArchive,
    store: SQLiteStore,
    result: TournamentSaveResult,
) -> None:
    if not result.inserted:
        line = Text("循环赛 ", style="yellow")
        line.append(literal_text(tournament.tournament_id, max_chars=NAME_DISPLAY_LIMIT))
        line.append(" 已存在，未重复更新 ELO。")
        console.print(line)
        return

    line = Text("✓ 循环赛已原子存档", style="green")
    line.append("  ")
    line.append(literal_text(tournament.tournament_id, max_chars=NAME_DISPLAY_LIMIT))
    line.append(f"\n  {result.pairing_count} 组对阵 · {result.match_count} 场对局")
    line.append("\n  ")
    line.append(literal_text(store.path))
    console.print(line)
    if result.rated:
        _render_tournament_rating_changes(result)


async def _run_round_robin(
    game: Game,
    players: list[Player],
    seed: int,
    store: SQLiteStore,
) -> None:
    pairing_count, match_count, turns, max_calls = _tournament_workload(game, len(players))
    intro = Text("项目 ")
    intro.append(literal_text(game.name, style="bold", max_chars=NAME_DISPLAY_LIMIT))
    intro.append(" · seed=")
    intro.append(literal_text(seed, max_chars=NAME_DISPLAY_LIMIT))
    intro.append(f"\n{len(players)} 名选手 · {pairing_count} 组对阵 · {match_count} 场对局")
    if turns is not None and max_calls is not None:
        intro.append(f"\n预计 {turns} 个回合 · 最多约 {max_calls} 次选手调用")
    intro.append("\n选手：")
    for index, player in enumerate(players):
        if index:
            intro.append(" · ")
        intro.append(literal_text(player.name, max_chars=NAME_DISPLAY_LIMIT))
    _best_effort_render(console.print, Panel(intro, title=Text("循环赛开始")))

    def render_pairing(pairing_number: int, leg_number: int, event: MatchEvent) -> None:
        if event.type == EventType.MATCH_STARTED:
            first, second = (descriptor["name"] for descriptor in event.data["players"])
            title = Text(f"第 {pairing_number}/{pairing_count} 组 · 第 {leg_number}/2 局 · ")
            title.append(literal_text(first, max_chars=NAME_DISPLAY_LIMIT))
            if game.name == "gomoku":
                title.append("（黑） vs ")
            elif game.name == "chess":
                title.append("（白） vs ")
            else:
                title.append("（第一席） vs ")
            title.append(literal_text(second, max_chars=NAME_DISPLAY_LIMIT))
            if game.name == "gomoku":
                title.append("（白）")
            elif game.name == "chess":
                title.append("（黑）")
            else:
                title.append("（第二席）")
            console.rule(title)
        _render(event)

    tournament = await play_round_robin(
        game,
        players,
        seed=seed,
        on_event=_guard_renderer(render_pairing),
    )
    result = store.save_tournament(tournament, rating_source="engine")
    _best_effort_render(_render_tournament_summary, tournament)
    _best_effort_render(_render_tournament_saved, tournament, store, result)


@app.command()
def play(
    game: str = typer.Option(
        "math_quiz", "--game", "-g", help=f"比赛项目: {', '.join(list_games())}"
    ),
    players: str = typer.Option(
        "mock:random,mock:fixed",
        "--players",
        "-p",
        help=("逗号分隔的选手，如 profile:kimi:moonshot-v1,human:小明,openai:gpt-4o-mini"),
    ),
    rounds: int | None = typer.Option(
        None,
        "--rounds",
        "-n",
        min=1,
        max=100,
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
            "LLM 每步限时（秒）；默认读取 LLMOLYMPIC_LLM_TIMEOUT / match.llm_timeout_seconds / 120"
        ),
    ),
    no_llm_timeout: bool = typer.Option(
        False,
        "--no-llm-timeout",
        help=("禁用比赛层 LLM 截止时间；Provider 自身网络超时仍可能生效，仅建议用于旧同步适配器"),
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
        line = Text("对局已完成，但 SQLite 存档失败：", style="red")
        line.append(literal_text(exc))
        console.print(line)
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
        help=(
            "恰好两个非人类选手，如 profile:kimi,profile:deepseek "
            "或 openai:gpt-4o-mini,ollama:llama3.1"
        ),
    ),
    rounds: int | None = typer.Option(
        None,
        "--rounds",
        "-n",
        min=1,
        max=100,
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
            "LLM 每步限时（秒）；默认读取 LLMOLYMPIC_LLM_TIMEOUT / match.llm_timeout_seconds / 120"
        ),
    ),
    no_llm_timeout: bool = typer.Option(
        False,
        "--no-llm-timeout",
        help=("禁用比赛层 LLM 截止时间；Provider 自身网络超时仍可能生效，仅建议用于旧同步适配器"),
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
        line = Text("双局赛已完成，但 SQLite 原子存档失败：", style="red")
        line.append(literal_text(exc))
        console.print(line)
        raise typer.Exit(code=1) from exc


@app.command(name="round-robin")
def round_robin(
    game: str = typer.Option(
        "knowledge_quiz",
        "--game",
        "-g",
        help=f"比赛项目: {', '.join(list_games())}",
    ),
    players: str = typer.Option(
        "mock:random,mock:fixed,mock:illegal",
        "--players",
        "-p",
        help=("3–16 名非人类选手，如 profile:kimi,profile:deepseek,profile:local"),
    ),
    rounds: int | None = typer.Option(
        None,
        "--rounds",
        "-n",
        min=1,
        max=100,
        help="题目型项目的每人题数（棋类项目不适用；默认 5）",
    ),
    seed: int = typer.Option(
        0,
        "--seed",
        "-s",
        min=SQLITE_INT_MIN,
        max=SQLITE_INT_MAX,
        help="赛事随机种子；每组对阵会按稳定身份确定性派生自己的 seed",
    ),
    llm_timeout: float | None = typer.Option(
        None,
        "--llm-timeout",
        min=0.001,
        help=(
            "LLM 每步限时（秒）；默认读取 LLMOLYMPIC_LLM_TIMEOUT / match.llm_timeout_seconds / 120"
        ),
    ),
    no_llm_timeout: bool = typer.Option(
        False,
        "--no-llm-timeout",
        help=("禁用比赛层 LLM 截止时间；Provider 自身网络超时仍可能生效，仅建议用于旧同步适配器"),
    ),
    allow_large_tournament: bool = typer.Option(
        False,
        "--allow-large-tournament",
        help=(
            "显式允许超过默认对局/调用阈值的大型循环赛；请先确认 Provider 费用预算与中断不存档风险"
        ),
    ),
    database: Annotated[
        Path | None,
        typer.Option("--db", help="SQLite 文件；默认读取 LLMOLYMPIC_DB / storage.database"),
    ] = None,
) -> None:
    """让 3–16 名非人类选手两两进行交换顺序双局赛。"""

    selected_game, selected_players = _prepare_round_robin(
        game_name=game,
        player_spec=players,
        rounds=rounds,
        llm_timeout=llm_timeout,
        no_llm_timeout=no_llm_timeout,
    )
    _validate_tournament_workload(
        selected_game,
        len(selected_players),
        allow_large=allow_large_tournament,
    )
    store = _open_store(database)
    try:
        asyncio.run(_run_round_robin(selected_game, selected_players, seed, store))
    except (OSError, sqlite3.Error, StorageError) as exc:
        line = Text("循环赛已完成，但 SQLite 原子存档失败：", style="red")
        line.append(literal_text(exc))
        console.print(line)
        raise typer.Exit(code=1) from exc


@app.command(name="games")
def list_available_games() -> None:
    """列出已注册的比赛项目。"""
    for name in list_games():
        line = Text("- ")
        line.append(literal_text(name, max_chars=NAME_DISPLAY_LIMIT))
        console.print(line)


@app.command()
def history(
    game: str | None = typer.Option(None, "--game", "-g", help="只看指定比赛项目"),
    limit: int = typer.Option(20, "--limit", "-n", min=1, max=1000, help="最多显示多少场"),
    database: Annotated[Path | None, typer.Option("--db", help="SQLite 文件")] = None,
) -> None:
    """查看最近的对局档案。"""
    try:
        rows = _open_store(database, create=False).list_matches(limit=limit, game=game)
    except (OSError, sqlite3.Error, StorageError, ValueError) as exc:
        line = Text("无法读取 SQLite 对局历史：", style="red")
        line.append(literal_text(exc))
        console.print(line)
        raise typer.Exit(code=1) from exc
    if not rows:
        console.print("暂无对局档案。")
        return

    console.print(Text("对局历史", style="bold"))
    for row in rows:
        line = literal_text(row.match_id, style="bold cyan", max_chars=NAME_DISPLAY_LIMIT)
        line.append("  ")
        line.append(literal_text(row.game, max_chars=NAME_DISPLAY_LIMIT))
        line.append("  ")
        line.append(Text(row.finished_at.astimezone().strftime("%Y-%m-%d %H:%M")))
        line.append("\n")
        if row.tournament_id is not None:
            line.append("  循环赛 ")
            line.append(literal_text(row.tournament_id, max_chars=NAME_DISPLAY_LIMIT))
            line.append(f" 第 {row.pairing_number}/{row.pairing_count} 组")
            if row.series_id is not None:
                line.append(" · 系列 ")
                line.append(literal_text(row.series_id, max_chars=NAME_DISPLAY_LIMIT))
                line.append(f" 第 {row.leg_number}/2 局")
            line.append("\n")
        elif row.series_id is not None:
            line.append("  系列 ")
            line.append(literal_text(row.series_id, max_chars=NAME_DISPLAY_LIMIT))
            line.append(f" 第 {row.leg_number}/2 局\n")
        line.append("  ")
        for index, name in enumerate(row.players):
            if index:
                line.append(" · ")
            line.append(literal_text(name, max_chars=NAME_DISPLAY_LIMIT))
            line.append(f" {row.scores[name]:.2f}")
        console.print(line)


@app.command()
def leaderboard(
    game: str | None = typer.Option(None, "--game", "-g", help="项目榜；省略则显示总榜"),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=1000, help="最多显示多少名"),
    database: Annotated[Path | None, typer.Option("--db", help="SQLite 文件")] = None,
) -> None:
    """查看持久化 ELO 排行榜。"""
    try:
        entries = _open_store(database, create=False).leaderboard(game=game, limit=limit)
    except (OSError, sqlite3.Error, StorageError, ValueError) as exc:
        line = Text("无法读取 SQLite ELO 榜：", style="red")
        line.append(literal_text(exc))
        console.print(line)
        raise typer.Exit(code=1) from exc
    if not entries:
        console.print("暂无 ELO 记录。")
        return

    title = Text("ELO ")
    title.append(Text("总榜") if game is None else literal_text(game, max_chars=NAME_DISPLAY_LIMIT))
    table = Table(title=title)
    table.add_column("排名", justify="right")
    table.add_column("选手")
    table.add_column("等级分", justify="right")
    table.add_column("场次", justify="right")
    table.add_column("胜-平-负", justify="right")
    for rank, entry in enumerate(entries, start=1):
        table.add_row(
            Text(str(rank)),
            literal_text(entry.player, max_chars=NAME_DISPLAY_LIMIT),
            Text(f"{entry.rating:.1f}"),
            Text(str(entry.games_played)),
            Text(f"{entry.wins}-{entry.draws}-{entry.losses}"),
        )
    console.print(table)


@app.command(name="archive")
def show_archive(
    match_id: str = typer.Argument(..., help="对局、系列赛或循环赛 ID"),
    database: Annotated[Path | None, typer.Option("--db", help="SQLite 文件")] = None,
) -> None:
    """输出一场对局、一个双局赛或一个循环赛的完整 JSON 档案。"""
    try:
        store = _open_store(database, create=False)
        archive = store.get_match(match_id)
        if archive is None:
            archive = store.get_series(match_id)
        if archive is None:
            archive = store.get_tournament(match_id)
    except (OSError, sqlite3.Error, StorageError, ValueError) as exc:
        line = Text("无法读取 SQLite 档案：", style="red")
        line.append(literal_text(exc))
        console.print(line)
        raise typer.Exit(code=1) from exc
    if archive is None:
        line = Text("未找到对局、系列赛或循环赛 ", style="red")
        line.append(literal_text(repr(match_id), max_chars=NAME_DISPLAY_LIMIT))
        line.append("。")
        console.print(line)
        raise typer.Exit(code=1)
    console.print(
        literal_text(
            archive.to_json(),
            max_chars=ARCHIVE_DISPLAY_LIMIT,
            multiline=True,
        )
    )
