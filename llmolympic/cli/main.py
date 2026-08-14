"""命令行界面：Typer + Rich。只消费 Match 事件做渲染，不含任何引擎逻辑。"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import math
import shlex
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import combinations
from pathlib import Path
from typing import Annotated, Self
from urllib.parse import urlsplit

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from llmolympic import __version__
from llmolympic.cli.terminal import (
    ARCHIVE_DISPLAY_LIMIT,
    NAME_DISPLAY_LIMIT,
    PROMPT_DISPLAY_LIMIT,
    literal_text,
)
from llmolympic.config import (
    get as cfg_get,
)
from llmolympic.config import (
    get_profile,
    load_provider_pricing,
    resolve_budget_settings,
)
from llmolympic.core.archive import MatchArchive
from llmolympic.core.budget_config import ResolvedProviderBudget, resolve_provider_budget
from llmolympic.core.events import EventType, MatchEvent
from llmolympic.core.game import (
    MAX_PLAYER_NAME_CHARS,
    Game,
    describe_game_config,
    validate_player_count,
    validate_players,
)
from llmolympic.core.judge import JudgePanelError, JudgePanelSnapshot, LLMJudgePanel
from llmolympic.core.match import play_match
from llmolympic.core.player import (
    DEFAULT_LLM_TIMEOUT_SECONDS,
    HumanPlayer,
    LLMPlayer,
    Player,
    UsageBudgetProtocol,
    profile_entrant_id,
)
from llmolympic.core.series import SeriesArchive, play_two_leg_series
from llmolympic.core.storage import (
    DEFAULT_TOURNAMENT_RUNNER_LEASE_SECONDS,
    SQLITE_INT_MAX,
    SQLITE_INT_MIN,
    SaveResult,
    SQLiteStore,
    StorageError,
    TournamentAuditError,
    TournamentAuditReport,
    TournamentRunnerLease,
    TournamentRunnerLeaseLostError,
    TournamentSaveResult,
    audit_tournament,
    database_path,
)
from llmolympic.core.tournament import (
    MAX_TOURNAMENT_PLAYERS,
    MIN_TOURNAMENT_PLAYERS,
    TournamentArchive,
    TournamentCheckpoint,
    prepare_round_robin,
    resume_round_robin,
)
from llmolympic.core.usage import (
    NANODOLLARS_PER_USD,
    BudgetExceededError,
    UsageBudget,
    UsageError,
    UsageTotals,
)
from llmolympic.diagnostics import run_diagnostics
from llmolympic.games import create_game, list_games
from llmolympic.human_input import BrowserHumanPlayer, HumanInputError
from llmolympic.live import GameMode, LiveFinalKind, LivePublisher
from llmolympic.providers import create_profile_provider, create_provider
from llmolympic.providers.base import validate_route_id

app = typer.Typer(
    help="LLM Olympics —— 人类与 LLM 的多项目竞技场",
    no_args_is_help=True,
)
console = Console()

MAX_UNCONFIRMED_TOURNAMENT_MATCHES = 30
MAX_UNCONFIRMED_TOURNAMENT_PROVIDER_CALLS = 5_000
TOURNAMENT_MOVE_ATTEMPTS = 3
DEFAULT_TOURNAMENT_GAME = "knowledge_quiz"
DEFAULT_TOURNAMENT_PLAYERS = "mock:random,mock:fixed,mock:illegal"
DEFAULT_TOURNAMENT_SEED = 0
TOURNAMENT_RUNNER_HEARTBEAT_SECONDS = DEFAULT_TOURNAMENT_RUNNER_LEASE_SECONDS / 4
TOURNAMENT_RUNNER_BUSY_RETRY_SECONDS = 1.0
LOCAL_WEB_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


@dataclass(frozen=True, slots=True)
class _RuntimeBudget:
    resolved: ResolvedProviderBudget
    ledger: UsageBudgetProtocol


def _budget_players(
    players: list[Player],
    judge_panel: LLMJudgePanel | None = None,
) -> tuple[LLMPlayer, ...]:
    values = [player for player in players if isinstance(player, LLMPlayer)]
    if judge_panel is not None:
        values.extend(judge_panel.judges)
    return tuple(values)


def _bind_runtime_budget(
    players: list[Player],
    judge_panel: LLMJudgePanel | None,
    resolved: ResolvedProviderBudget,
    ledger: UsageBudgetProtocol,
) -> _RuntimeBudget:
    llm_players = _budget_players(players, judge_panel)
    for player in llm_players:
        player.bind_usage_budget(ledger, resolved.policy)
    if judge_panel is not None:
        judge_panel.validate_contestants(players)
    return _RuntimeBudget(resolved=resolved, ledger=ledger)


def _resolve_budget_definition(
    players: list[Player],
    judge_panel: LLMJudgePanel | None,
    *,
    max_provider_calls: int | None,
    max_input_tokens: int | None,
    max_output_tokens_per_call: int | None,
    max_total_output_tokens: int | None,
    max_estimated_cost_usd: str | None,
) -> ResolvedProviderBudget | None:
    """Resolve trusted inputs before SQLite is opened or a call is authorized."""

    try:
        settings = resolve_budget_settings(
            max_provider_calls=max_provider_calls,
            max_input_tokens=max_input_tokens,
            max_output_tokens_per_call=max_output_tokens_per_call,
            max_total_output_tokens=max_total_output_tokens,
            max_estimated_cost_usd=max_estimated_cost_usd,
        )
        return resolve_provider_budget(
            _budget_players(players, judge_panel),
            settings,
            load_provider_pricing(),
        )
    except (TypeError, ValueError, UsageError) as exc:
        raise typer.BadParameter(str(exc), param_hint="Provider budget") from exc


def _prepare_in_memory_budget(
    players: list[Player],
    judge_panel: LLMJudgePanel | None,
    *,
    max_provider_calls: int | None,
    max_input_tokens: int | None,
    max_output_tokens_per_call: int | None,
    max_total_output_tokens: int | None,
    max_estimated_cost_usd: str | None,
) -> _RuntimeBudget | None:
    """Resolve and bind a play/series budget before SQLite is opened."""

    resolved = _resolve_budget_definition(
        players,
        judge_panel,
        max_provider_calls=max_provider_calls,
        max_input_tokens=max_input_tokens,
        max_output_tokens_per_call=max_output_tokens_per_call,
        max_total_output_tokens=max_total_output_tokens,
        max_estimated_cost_usd=max_estimated_cost_usd,
    )
    if resolved is None:
        return None
    try:
        return _bind_runtime_budget(
            players,
            judge_panel,
            resolved,
            UsageBudget(resolved.limits),
        )
    except (TypeError, ValueError, UsageError) as exc:
        raise typer.BadParameter(str(exc), param_hint="Provider budget") from exc


def _usage_totals(ledger: UsageBudgetProtocol) -> UsageTotals:
    spent = getattr(ledger, "spent", None)
    if not isinstance(spent, UsageTotals):
        raise TypeError("usage ledger did not expose UsageTotals")
    return spent


def _render_usage_summary(runtime: _RuntimeBudget | None) -> None:
    if runtime is None:
        return
    spent = _usage_totals(runtime.ledger)
    line = Text("Provider 预算：", style="cyan")
    line.append(f"{spent.calls} 次模型/算法调用")
    line.append(f" · input {spent.input} tokens · output {spent.output} tokens")
    line.append(" · 已计价约 $")
    dollars = Decimal(spent.estimated_cost) / Decimal(NANODOLLARS_PER_USD)
    line.append(format(dollars, "f"))
    unpriced = sum(route.price is None for route in runtime.resolved.policy.routes)
    if unpriced:
        line.append(f" · {unpriced} 条路由未计价", style="yellow")
    console.print(line)


def _render_usage_error(exc: UsageError) -> None:
    line = Text("Provider 预算中止；本次结果未存档且未更新 ELO：", style="red")
    if isinstance(exc, BudgetExceededError):
        names = {
            "calls": "调用次数",
            "input": "输入 Token",
            "output": "输出 Token",
            "estimated_cost": "预估费用",
        }
        line.append(names.get(exc.dimension, exc.dimension))
        line.append("达到硬上限")
    else:
        line.append(getattr(exc, "reason_code", "usage_error"))
    console.print(line)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"llmolympic {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="显示版本并退出",
        ),
    ] = False,
) -> None:
    """LLM Olympics command-line interface."""


@app.command()
def doctor(
    database: Annotated[
        Path | None,
        typer.Option("--db", help="SQLite 文件；默认读取 LLMOLYMPIC_DB / storage.database"),
    ] = None,
) -> None:
    """离线检查版本、配置、Provider 就绪状态与 SQLite 兼容性。"""

    checks = run_diagnostics(database)
    styles = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}
    for check in checks:
        line = Text(f"{check.status} ", style=f"bold {styles[check.status]}")
        line.append(literal_text(check.message))
        console.print(line)
    if any(check.status == "FAIL" for check in checks):
        raise typer.Exit(code=1)


def _audit_report_payload(report: TournamentAuditReport) -> dict[str, object]:
    finalized = report.state == "finalized"
    leaderboard_status = (
        "not_applicable"
        if not finalized or not report.rated
        else "pass"
        if report.leaderboard_replay_complete
        else "partial"
    )
    return {
        "report_schema_version": 1,
        "result": "pass",
        "tournament_id": report.tournament_id,
        "state": report.state,
        "game": report.game,
        "progress": {
            "completed_pairings": report.completed_pairings,
            "pairing_count": report.pairing_count,
            "completed_matches": report.completed_pairings * 2,
            "match_count": report.pairing_count * 2,
        },
        "technical_losses": report.technical_losses,
        "rated": report.rated,
        "resumable": report.resumable,
        "checks": {
            "database": "pass",
            "checkpoint": "pass" if report.checkpoint_present else "not_applicable",
            "archive": "pass" if finalized else "not_applicable",
            "ratings": "pass" if finalized and report.rated else "not_applicable",
            "leaderboard": leaderboard_status,
        },
    }


@app.command(name="audit-tournament")
def audit_tournament_command(
    tournament_id: Annotated[str, typer.Argument(help="循环赛或 checkpoint ID")],
    database: Annotated[
        Path | None,
        typer.Option("--db", help="SQLite 文件；默认读取 LLMOLYMPIC_DB / storage.database"),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="输出稳定、无 ANSI 的单行 JSON 报告"),
    ] = False,
) -> None:
    """严格只读审计一项循环赛、checkpoint、关系索引与 ELO 账本。"""

    if not tournament_id.strip():
        raise typer.BadParameter("循环赛 ID 不能为空", param_hint="TOURNAMENT_ID")
    try:
        report = audit_tournament(tournament_id, database)
    except TournamentAuditError as exc:
        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "report_schema_version": 1,
                        "result": "fail",
                        "error_code": exc.code,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            messages = {
                "database_missing": "SQLite 数据库不存在",
                "database_active_writer": "SQLite 正在写入，无法取得一致的只读快照",
                "database_migration_required": "SQLite schema 需要先备份并升级",
                "database_unsupported_schema": "SQLite schema 高于当前支持版本",
                "database_invalid": "SQLite 数据库无法读取、结构无效或已损坏",
                "tournament_not_found": "未找到该循环赛或 checkpoint",
                "tournament_inconsistent": "赛事档案、关系索引或 ELO 账本不一致",
            }
            line = Text("FAIL 循环赛 ", style="bold red")
            line.append(literal_text(tournament_id, max_chars=256))
            line.append(" 审计未通过：")
            line.append(messages.get(exc.code, "赛事审计失败"))
            console.print(line)
        raise typer.Exit(code=1) from exc

    payload = _audit_report_payload(report)
    if json_output:
        typer.echo(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return

    line = Text("PASS 循环赛 ", style="bold green")
    line.append(literal_text(report.tournament_id, max_chars=256))
    line.append(" 深度审计通过")
    console.print(line)
    console.print(
        "状态 "
        f"{report.state} · 进度 {report.completed_pairings}/{report.pairing_count} 组 · "
        f"{report.completed_pairings * 2}/{report.pairing_count * 2} 局 · "
        f"技术负 {report.technical_losses}"
    )
    checks = payload["checks"]
    if not isinstance(checks, dict):  # pragma: no cover - internal contract guard
        raise typer.Exit(code=1)
    console.print("检查 " + " · ".join(f"{name}={status}" for name, status in checks.items()))


def _render_warning() -> None:
    """尽力报告 UI 故障；报告本身失败也不能影响比赛或存档。"""

    try:
        console.print(Text("⚠ 终端显示失败；比赛将继续并优先保存档案。", style="yellow"))
    except Exception:  # noqa: BLE001 - UI failures must never abort persistence
        return


def _render_live_warning() -> None:
    """尽力且不泄密地报告直播 sidecar 故障。"""

    try:
        console.print(Text("⚠ 实时观战不可用；比赛将继续并优先保存档案。", style="yellow"))
    except Exception:  # noqa: BLE001 - reporting cannot own match control flow
        return


class _LiveRun:
    """One failure-isolated live publisher for one top-level CLI run."""

    def __init__(self, database: str | Path, mode: GameMode) -> None:
        self._publisher: LivePublisher | None = None
        self._live_id: str | None = None
        self._terminal = False
        self._warned = False
        try:
            self._publisher = LivePublisher(database, mode)
            if self._publisher.failed:
                self._warn_once()
        except Exception:  # noqa: BLE001 - live observation is strictly non-critical
            self._warn_once()

    def _warn_once(self) -> None:
        if self._warned:
            return
        self._warned = True
        _render_live_warning()

    def observe(
        self,
        event: MatchEvent,
        *,
        context: Mapping[str, object] | None = None,
    ) -> None:
        publisher = self._publisher
        if publisher is None or self._terminal:
            return
        try:
            if publisher.failed:
                self._warn_once()
                return
            if self._live_id is None:
                if event.type != EventType.MATCH_STARTED:
                    return
                self._live_id = publisher.start_session(event, context=context)
                success = self._live_id is not None
            else:
                success = publisher.publish(self._live_id, event, context=context)
            if not success or publisher.failed:
                self._warn_once()
        except Exception:  # noqa: BLE001 - publisher failures must not escape callbacks
            self._warn_once()

    def complete(
        self,
        *,
        final_kind: LiveFinalKind,
        final_id: str,
        final_match_ids: Sequence[str],
    ) -> None:
        publisher = self._publisher
        if publisher is None or self._live_id is None or self._terminal:
            return
        try:
            self._terminal = publisher.complete(
                self._live_id,
                final_kind=final_kind,
                final_id=final_id,
                final_match_ids=final_match_ids,
            )
            if not self._terminal or publisher.failed:
                self._warn_once()
        except Exception:  # noqa: BLE001 - archival success cannot depend on live completion
            self._warn_once()

    def interrupt(self, reason_code: str = "producer_failed") -> None:
        publisher = self._publisher
        if publisher is None or self._live_id is None or self._terminal:
            return
        try:
            self._terminal = publisher.interrupt(
                self._live_id,
                reason_code=reason_code,
            )
            if not self._terminal or publisher.failed:
                self._warn_once()
        except Exception:  # noqa: BLE001 - interruption is a best-effort status signal
            self._warn_once()

    def close(self) -> None:
        publisher = self._publisher
        if publisher is None:
            return
        try:
            publisher.close()
            if publisher.failed:
                self._warn_once()
        except Exception:  # noqa: BLE001 - cleanup cannot change match semantics
            self._warn_once()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is not None:
            self.interrupt()
        self.close()


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


def _split_player_specs(spec: str, *, param_hint: str = "--players") -> list[str]:
    tokens = [token.strip() for token in spec.split(",")]
    if any(not token for token in tokens):
        raise typer.BadParameter("选手规格不能为空", param_hint=param_hint)
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
    *,
    param_hint: str = "--players",
) -> list[Player]:
    """把 ``profile:kimi:moonshot-v1,human:小明`` 等规格解析成选手。

    模型名可省略（如 ``openai``），此时回退到 config.toml 里的
    ``[<provider>] default_model``；``mock`` 省略时默认为 random 策略；
    ``human`` 省略名字时默认为"人类"。``profile:<id>[:model]``
    使用 ``[profiles.<id>]``，并生成与显示名分离的稳定 entrant ID。
    """
    players: list[Player] = []
    for token in _split_player_specs(spec, param_hint=param_hint):
        kind, _, ident = token.partition(":")
        if kind == "human":
            players.append(HumanPlayer(name=ident or "人类", timeout=human_timeout))
            continue
        if kind == "profile":
            profile_id, separator, explicit_model = ident.partition(":")
            if not profile_id:
                raise typer.BadParameter(
                    "Profile 选手必须使用 profile:<id>[:model]",
                    param_hint=param_hint,
                )
            if separator and not explicit_model.strip():
                raise typer.BadParameter("Profile 模型名不能为空", param_hint=param_hint)
            profile = get_profile(profile_id)
            model = explicit_model.strip() if separator else profile.default_model
            if not model:
                raise typer.BadParameter(
                    f"Provider Profile {profile_id!r} 未指定模型，且没有 default_model",
                    param_hint=param_hint,
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
            param_hint=param_hint,
        )
    _disambiguate_duplicate_display_names(players)
    names = [player.name for player in players]
    if len(set(names)) != len(names):
        raise typer.BadParameter(f"选手名字必须唯一: {names}", param_hint=param_hint)
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
        judging = ev.data.get("judging")
        if isinstance(judging, dict):
            summary = Text("匿名评审完成：", style="green")
            summary.append(
                f"{judging.get('successful_judges', 0)}/{judging.get('panel_size', 0)} 名有效评委"
            )
            summary.append(f" · quorum={judging.get('quorum', 0)}")
            console.print(summary)
        scores = ev.data["scores"]
        score_precision = 4 if isinstance(judging, dict) else 2
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
                Text(f"{s:.{score_precision}f}"),
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


def _load_web_runtime():
    """Import the optional Web stack without burdening the base CLI install."""

    try:
        import uvicorn

        from llmolympic.web.app import create_app

        importlib.import_module("websockets")
    except ModuleNotFoundError as exc:
        missing = (exc.name or "").split(".", maxsplit=1)[0]
        if missing not in {"fastapi", "starlette", "uvicorn", "websockets"}:
            raise
        console.print(
            Text(
                'Web 功能未安装。请运行 python -m pip install "llmolympic[web]"。',
                style="red",
            )
        )
        raise typer.Exit(code=1) from exc
    return uvicorn, create_app


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
    mode: str = "play",
) -> tuple[Game, list[Player]]:
    """按固定顺序完成项目、人数、超时与 Provider 校验。"""

    try:
        game_options = {} if rounds is None else {"rounds": rounds}
        selected_game = create_game(game_name, mode=mode, **game_options)
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


def _validate_human_input_mode(
    players: Sequence[Player],
    mode: str,
    web_url: str,
) -> tuple[str, str]:
    """Validate the optional browser input backend before opening SQLite."""

    normalized_mode = mode.strip().casefold()
    if normalized_mode not in {"terminal", "web"}:
        raise typer.BadParameter(
            "人类输入方式必须是 terminal 或 web",
            param_hint="--human-input",
        )
    if normalized_mode == "terminal":
        return normalized_mode, web_url

    human_players = [player for player in players if isinstance(player, HumanPlayer)]
    if len(players) < 2 or not human_players:
        raise typer.BadParameter(
            "Web 人类输入仅支持 play 中至少 2 名选手，且至少包含 1 名人类选手",
            param_hint="--human-input",
        )

    candidate = web_url.strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise typer.BadParameter("Web 地址无效", param_hint="--web-url") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname is None
        or parsed.hostname.casefold() not in {"127.0.0.1", "::1", "localhost"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or port is None
        or port < 1
    ):
        raise typer.BadParameter(
            "Web 地址必须是带端口的本机 HTTP 根地址，例如 http://127.0.0.1:8000",
            param_hint="--web-url",
        )
    display_host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return normalized_mode, f"http://{display_host}:{port}"


def _prepare_judge_panel(
    game: Game,
    contestants: list[Player],
    judge_specs: list[str] | None,
    *,
    llm_timeout: float | None,
    no_llm_timeout: bool,
) -> LLMJudgePanel | None:
    """解析独立评委，并在创建 SQLite 文件前完成角色与身份校验。"""

    requires_panel = bool(getattr(game, "requires_judge_panel", False))
    raw_specs = list(judge_specs or [])
    if not requires_panel:
        if raw_specs:
            raise typer.BadParameter(
                f"项目 {game.name!r} 不使用 LLM 评审团",
                param_hint="--judge",
            )
        return None
    if not raw_specs:
        raise typer.BadParameter(
            f"项目 {game.name!r} 需要至少 3 个 --judge",
            param_hint="--judge",
        )

    joined = ",".join(raw_specs)
    tokens = _split_player_specs(joined, param_hint="--judge")
    if any(token.partition(":")[0] == "human" for token in tokens):
        raise typer.BadParameter("评委必须是 LLM/Profile/mock，不能是人类", param_hint="--judge")
    try:
        effective_timeout = _resolve_llm_timeout(llm_timeout, disabled=no_llm_timeout)
        parsed = _parse_players(
            joined,
            60.0,
            effective_timeout,
            param_hint="--judge",
        )
        if any(not isinstance(judge, LLMPlayer) for judge in parsed):
            raise ValueError("评委必须是 LLM/Profile/mock")
        panel = LLMJudgePanel([judge for judge in parsed if isinstance(judge, LLMPlayer)])
        panel.validate_contestants(contestants)
        return panel
    except typer.BadParameter:
        raise
    except (TypeError, ValueError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--judge") from exc


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
        selected_game = create_game(game_name, mode="round_robin", **game_options)
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


def _checkpoint_player_spec(descriptor: dict) -> str:
    """Rebuild one credential-free CLI player spec from a checkpoint descriptor."""

    if descriptor.get("kind") != LLMPlayer.kind:
        raise ValueError("循环赛 checkpoint 只能恢复 LLM/mock/Profile 选手")
    model = descriptor.get("model")
    if not isinstance(model, str) or not model or "," in model:
        raise ValueError("循环赛 checkpoint 包含无法安全恢复的模型名")
    profile_id = descriptor.get("profile_id")
    if profile_id is not None:
        if not isinstance(profile_id, str) or not profile_id or "," in profile_id:
            raise ValueError("循环赛 checkpoint 包含无效的 Profile ID")
        return f"profile:{profile_id}:{model}"
    provider = descriptor.get("provider")
    if (
        not isinstance(provider, str)
        or not provider
        or any(marker in provider for marker in (",", ":"))
    ):
        raise ValueError("循环赛 checkpoint 包含无法安全恢复的 Provider")
    return f"{provider}:{model}"


def _checkpoint_llm_timeout(checkpoint: TournamentCheckpoint) -> float | None:
    """Return the one CLI-wide timeout frozen into all checkpoint entrants."""

    values = [descriptor.get("move_timeout_seconds") for descriptor in checkpoint.players]
    first = values[0]
    if any(value != first for value in values[1:]):
        raise ValueError("循环赛 checkpoint 的选手超时配置不一致")
    if first is None:
        return None
    if isinstance(first, bool) or not isinstance(first, (int, float)):
        raise TypeError("循环赛 checkpoint 的 LLM 超时无效")
    return float(first)


def _checkpoint_judge_spec(descriptor: object) -> str:
    if not hasattr(descriptor, "model") or not hasattr(descriptor, "provider"):
        raise TypeError("循环赛 checkpoint 包含无效评委描述")
    model = descriptor.model
    provider = descriptor.provider
    profile_id = descriptor.profile_id
    if not isinstance(model, str) or not model or "," in model:
        raise ValueError("循环赛 checkpoint 包含无法安全恢复的评委模型")
    if profile_id is not None:
        if not isinstance(profile_id, str) or not profile_id or "," in profile_id:
            raise ValueError("循环赛 checkpoint 包含无效评委 Profile ID")
        return f"profile:{profile_id}:{model}"
    if (
        not isinstance(provider, str)
        or not provider
        or any(marker in provider for marker in (",", ":"))
    ):
        raise ValueError("循环赛 checkpoint 包含无法安全恢复的评委 Provider")
    return f"{provider}:{model}"


def _restore_checkpoint_judge_panel(
    checkpoint: TournamentCheckpoint,
    game: Game,
    contestants: list[Player],
) -> LLMJudgePanel | None:
    snapshot = checkpoint.judge_panel
    requires_panel = bool(getattr(game, "requires_judge_panel", False))
    if snapshot is None:
        if requires_panel:
            raise ValueError("创意循环赛 checkpoint 缺少评审团快照")
        return None
    if not requires_panel:
        raise ValueError("客观循环赛 checkpoint 不能包含评审团快照")
    if not isinstance(snapshot, JudgePanelSnapshot):
        raise TypeError("循环赛 checkpoint 评审团快照无效")

    judges: list[LLMPlayer] = []
    for descriptor in snapshot.panel:
        parsed = _parse_players(
            _checkpoint_judge_spec(descriptor),
            60.0,
            descriptor.timeout_seconds,
        )
        if len(parsed) != 1 or not isinstance(parsed[0], LLMPlayer):
            raise ValueError("循环赛 checkpoint 只能恢复 LLM/Profile/mock 评委")
        judges.append(parsed[0])
    panel = LLMJudgePanel(judges)
    if panel.snapshot() != snapshot:
        raise ValueError("当前评委配置与循环赛 checkpoint 冻结快照不一致")
    panel.validate_contestants(contestants)
    return panel


def _restore_round_robin(checkpoint: TournamentCheckpoint) -> tuple[Game, list[Player]]:
    """Recreate providers from current config while preserving frozen tournament identity."""

    route_fields = tuple("route_id" in descriptor for descriptor in checkpoint.players)
    if any(route_fields) and not all(route_fields):
        raise ValueError("循环赛 checkpoint 的 route_id 快照不完整")
    has_route_snapshot = all(route_fields)
    if has_route_snapshot:
        for descriptor in checkpoint.players:
            validate_route_id(descriptor.get("route_id"))

    player_spec = ",".join(_checkpoint_player_spec(item) for item in checkpoint.players)
    timeout = _checkpoint_llm_timeout(checkpoint)
    raw_rounds = checkpoint.game_config.get("rounds")
    if raw_rounds is None:
        rounds = None
    elif isinstance(raw_rounds, bool) or not isinstance(raw_rounds, int):
        raise ValueError("循环赛 checkpoint 的 rounds 配置无效")
    else:
        rounds = raw_rounds
    game, players = _prepare_round_robin(
        game_name=checkpoint.game,
        player_spec=player_spec,
        rounds=rounds,
        llm_timeout=timeout,
        no_llm_timeout=timeout is None,
    )
    # Profile display names are presentation metadata and may change between
    # processes. The tournament itself must keep the original name snapshot so
    # completed and future series have byte-for-byte identical descriptors.
    for player, descriptor in zip(players, checkpoint.players):
        player.name = descriptor["name"]
        if not has_route_snapshot:
            if not isinstance(player, LLMPlayer):  # pragma: no cover - parser contract guard
                raise TypeError("旧循环赛 checkpoint 只能恢复 LLM/mock/Profile 选手")
            player._use_legacy_route_description()
    return game, players


def _tournament_workload(
    game: Game,
    player_count: int,
    judge_count: int = 0,
) -> tuple[int, int, int | None, int | None]:
    pairing_count = player_count * (player_count - 1) // 2
    match_count = pairing_count * 2
    if bool(getattr(game, "requires_judge_panel", False)):
        turns = match_count * 2
        max_calls = match_count * (
            2 * TOURNAMENT_MOVE_ATTEMPTS + 2 * judge_count
        )
        return pairing_count, match_count, turns, max_calls
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
    judge_count: int = 0,
) -> None:
    _, match_count, turns, max_calls = _tournament_workload(
        game, player_count, judge_count
    )
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


async def _run(
    game: Game,
    players: list[Player],
    seed: int,
    store: SQLiteStore,
    judge_panel: LLMJudgePanel | None = None,
) -> None:
    live = _LiveRun(store.path, "play")
    browser_humans = [
        player for player in players if isinstance(player, BrowserHumanPlayer)
    ]
    guarded_render = _guard_renderer(_render)
    saved = False

    def on_event(event: MatchEvent) -> None:
        for browser_human in browser_humans:
            browser_human.observe_event(event)
        guarded_render(event)
        live.observe(event)

    try:
        archive = await play_match(
            game,
            players,
            seed=seed,
            on_event=on_event,
            judge_panel=judge_panel,
        )
        result = store.save_match(archive, rating_source="engine")
        saved = True
        for browser_human in browser_humans:
            try:
                browser_human.complete(archive.match_id)
            except HumanInputError as exc:
                line = Text("对局已存档，但浏览器参与页终态更新失败：", style="yellow")
                line.append(literal_text(exc.code))
                console.print(line)
        live.complete(
            final_kind="match",
            final_id=archive.match_id,
            final_match_ids=(archive.match_id,),
        )
        _best_effort_render(_render_saved, archive, store, result)
    finally:
        if not saved:
            live.interrupt()
            for browser_human in browser_humans:
                try:
                    browser_human.interrupt()
                except HumanInputError:
                    pass
        for browser_human in browser_humans:
            browser_human.close()
        live.close()


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
    judge_panel: LLMJudgePanel | None = None,
) -> None:
    live = _LiveRun(store.path, "series")
    saved = False
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

    def on_event(leg_number: int, event: MatchEvent) -> None:
        guarded_render_leg(leg_number, event)
        live.observe(event, context={"leg_number": leg_number})

    try:
        series_archive = await play_two_leg_series(
            game,
            players,
            seed=seed,
            on_event=on_event,
            judge_panel=judge_panel,
        )
        result = store.save_series(series_archive, rating_source="engine")
        saved = True
        live.complete(
            final_kind="series",
            final_id=series_archive.series_id,
            final_match_ids=tuple(leg.match_id for leg in series_archive.legs),
        )
        _best_effort_render(_render_series_summary, series_archive)
        _best_effort_render(_render_series_saved, series_archive, store, result)
    finally:
        if not saved:
            live.interrupt()
        live.close()


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

    line = Text("✓ 循环赛已完成；最终档案与 ELO 已原子封存", style="green")
    line.append("  ")
    line.append(literal_text(tournament.tournament_id, max_chars=NAME_DISPLAY_LIMIT))
    line.append(f"\n  {result.pairing_count} 组对阵 · {result.match_count} 场对局")
    line.append("\n  ")
    line.append(literal_text(store.path))
    console.print(line)
    if result.rated:
        _render_tournament_rating_changes(result)


def _tournament_resume_command(checkpoint: TournamentCheckpoint, store: SQLiteStore) -> str:
    return (
        "llmolympic round-robin --resume "
        f"{shlex.quote(checkpoint.tournament_id)} --db {shlex.quote(str(store.path))}"
    )


def _render_checkpoint_ready(
    checkpoint: TournamentCheckpoint,
    store: SQLiteStore,
    resumed: bool,
) -> None:
    completed = len(checkpoint.completed_series)
    total = len(checkpoint.schedule)
    line = Text("循环赛检查点已就绪", style="green")
    if resumed:
        line = Text("循环赛检查点已加载", style="green")
    line.append("\n  赛事 ID ")
    line.append(literal_text(checkpoint.tournament_id, max_chars=NAME_DISPLAY_LIMIT))
    line.append(f"\n  进度 {completed}/{total} 组")
    line.append("\n  恢复 ")
    line.append(
        literal_text(
            _tournament_resume_command(checkpoint, store),
            multiline=False,
        )
    )
    console.print(line)


def _render_checkpoint_saved(checkpoint: TournamentCheckpoint) -> None:
    completed = len(checkpoint.completed_series)
    total = len(checkpoint.schedule)
    line = Text("✓ 检查点已保存", style="green")
    line.append(f"  {completed}/{total} 组  ")
    line.append(literal_text(checkpoint.tournament_id, max_chars=NAME_DISPLAY_LIMIT))
    console.print(line)


def _render_tournament_interrupted(
    checkpoint: TournamentCheckpoint,
    store: SQLiteStore,
) -> None:
    completed = len(checkpoint.completed_series)
    total = len(checkpoint.schedule)
    line = Text("循环赛已中断。", style="yellow")
    line.append(f"已保存 {completed}/{total} 组；当前未完成的一组不会保存。")
    line.append("\n恢复：")
    line.append(literal_text(_tournament_resume_command(checkpoint, store)))
    console.print(line)


def _is_sqlite_busy_or_locked(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int) and code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return True
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


async def _renew_runner_lease_with_retry(
    store: SQLiteStore,
    lease: TournamentRunnerLease,
    stop: asyncio.Event,
) -> TournamentRunnerLease:
    while True:
        try:
            return await asyncio.to_thread(store.renew_tournament_runner, lease)
        except sqlite3.OperationalError as exc:
            if not _is_sqlite_busy_or_locked(exc):
                raise
            remaining = lease.expires_at_epoch - time.time()
            if remaining <= 0:
                raise TournamentRunnerLeaseLostError(
                    "循环赛 runner lease 心跳未能在过期前取得 SQLite 写锁"
                ) from exc
            if stop.is_set():
                return lease
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=min(TOURNAMENT_RUNNER_BUSY_RETRY_SECONDS, remaining),
                )
            except TimeoutError:
                pass
            if stop.is_set():
                return lease


async def _runner_lease_heartbeat(
    store: SQLiteStore,
    lease: TournamentRunnerLease,
    stop: asyncio.Event,
) -> None:
    current = lease
    while not stop.is_set():
        try:
            await asyncio.wait_for(
                stop.wait(),
                timeout=TOURNAMENT_RUNNER_HEARTBEAT_SECONDS,
            )
        except TimeoutError:
            pass
        if stop.is_set():
            return
        current = await _renew_runner_lease_with_retry(store, current, stop)


async def _run_round_robin(
    game: Game,
    players: list[Player],
    checkpoint: TournamentCheckpoint,
    store: SQLiteStore,
    lease: TournamentRunnerLease,
    judge_panel: LLMJudgePanel | None = None,
) -> None:
    pairing_count, match_count, turns, max_calls = _tournament_workload(
        game,
        len(players),
        0 if judge_panel is None else len(judge_panel.judges),
    )
    intro = Text("项目 ")
    intro.append(literal_text(game.name, style="bold", max_chars=NAME_DISPLAY_LIMIT))
    intro.append(" · seed=")
    intro.append(literal_text(checkpoint.seed, max_chars=NAME_DISPLAY_LIMIT))
    intro.append("\n赛事 ID ")
    intro.append(literal_text(checkpoint.tournament_id, max_chars=NAME_DISPLAY_LIMIT))
    intro.append(f" · 已保存 {len(checkpoint.completed_series)}/{pairing_count} 组")
    intro.append(f"\n{len(players)} 名选手 · {pairing_count} 组对阵 · {match_count} 场对局")
    if turns is not None and max_calls is not None:
        intro.append(f"\n预计 {turns} 个回合 · 最多约 {max_calls} 次选手调用")
    intro.append("\n选手：")
    for index, player in enumerate(players):
        if index:
            intro.append(" · ")
        intro.append(literal_text(player.name, max_chars=NAME_DISPLAY_LIMIT))
    title = "循环赛恢复" if checkpoint.completed_series else "循环赛开始"
    _best_effort_render(console.print, Panel(intro, title=Text(title)))

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

    def save_checkpoint(updated: TournamentCheckpoint) -> None:
        store.save_tournament_checkpoint(updated, lease=lease)
        _best_effort_render(_render_checkpoint_saved, updated)

    def renew_before_pairing(_spec: object) -> None:
        store.renew_tournament_runner(lease)

    live = _LiveRun(store.path, "round_robin")
    guarded_render_pairing = _guard_renderer(render_pairing)
    finalized = False

    def on_event(pairing_number: int, leg_number: int, event: MatchEvent) -> None:
        guarded_render_pairing(pairing_number, leg_number, event)
        live.observe(
            event,
            context={
                "pairing_number": pairing_number,
                "pairing_count": pairing_count,
                "leg_number": leg_number,
            },
        )

    try:
        tournament_task = asyncio.create_task(
            resume_round_robin(
                game,
                players,
                checkpoint,
                on_event=on_event,
                on_checkpoint=save_checkpoint,
                on_pairing_start=renew_before_pairing,
                judge_panel=judge_panel,
            )
        )
        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            _runner_lease_heartbeat(store, lease, heartbeat_stop)
        )
        try:
            done, _ = await asyncio.wait(
                {tournament_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                heartbeat_error = heartbeat_task.exception()
                if heartbeat_error is None:
                    raise StorageError("循环赛 runner lease 心跳意外停止")
                raise heartbeat_error
            tournament = tournament_task.result()
        finally:
            heartbeat_stop.set()
            if not tournament_task.done():
                tournament_task.cancel()
            await asyncio.gather(tournament_task, heartbeat_task, return_exceptions=True)

        renewed_lease = store.renew_tournament_runner(lease)
        result = store.finalize_tournament_checkpoint(
            checkpoint.tournament_id,
            lease=renewed_lease,
        )
        finalized = True
        live.complete(
            final_kind="tournament",
            final_id=tournament.tournament_id,
            final_match_ids=tuple(
                leg.match_id
                for pairing in tournament.pairings
                for leg in pairing.series.legs
            ),
        )
        _best_effort_render(_render_tournament_summary, tournament)
        _best_effort_render(_render_tournament_saved, tournament, store, result)
    finally:
        if not finalized:
            live.interrupt()
        live.close()


@app.command()
def play(
    game: str = typer.Option(
        "math_quiz", "--game", "-g", help=f"比赛项目: {', '.join(list_games('play'))}"
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
    human_input: str = typer.Option(
        "terminal",
        "--human-input",
        help="人类输入方式：terminal（当前终端）或 web（本机浏览器）",
    ),
    web_url: str = typer.Option(
        "http://127.0.0.1:8000",
        "--web-url",
        help="Web 人类输入页的本机根地址（仅 --human-input web 使用）",
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
    judge: Annotated[
        list[str] | None,
        typer.Option(
            "--judge",
            help=(
                "创意项目的独立评委，可重复指定 3–9 次；例如 "
                "--judge profile:judge-a --judge profile:judge-b --judge profile:judge-c"
            ),
        ),
    ] = None,
    max_provider_calls: int | None = typer.Option(
        None,
        "--max-provider-calls",
        min=0,
        max=SQLITE_INT_MAX,
        help="本次运行允许的模型/算法调用总数硬上限",
    ),
    max_input_tokens: int | None = typer.Option(
        None,
        "--max-input-tokens",
        min=0,
        max=SQLITE_INT_MAX,
        help="本次运行累计输入 Token 硬上限",
    ),
    max_output_tokens_per_call: int | None = typer.Option(
        None,
        "--max-output-tokens-per-call",
        min=1,
        max=SQLITE_INT_MAX,
        help="每次模型请求的输出 Token 上限（默认 1024）",
    ),
    max_total_output_tokens: int | None = typer.Option(
        None,
        "--max-total-output-tokens",
        min=0,
        max=SQLITE_INT_MAX,
        help="本次运行累计输出 Token 硬上限",
    ),
    max_estimated_cost_usd: str | None = typer.Option(
        None,
        "--max-estimated-cost-usd",
        help="按本地冻结价格计算的美元费用硬上限（不等同 Provider 最终账单）",
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
        mode="play",
    )
    human_input, web_url = _validate_human_input_mode(
        selected_players,
        human_input,
        web_url,
    )
    judge_panel = _prepare_judge_panel(
        selected_game,
        selected_players,
        judge,
        llm_timeout=llm_timeout,
        no_llm_timeout=no_llm_timeout,
    )
    runtime_budget = _prepare_in_memory_budget(
        selected_players,
        judge_panel,
        max_provider_calls=max_provider_calls,
        max_input_tokens=max_input_tokens,
        max_output_tokens_per_call=max_output_tokens_per_call,
        max_total_output_tokens=max_total_output_tokens,
        max_estimated_cost_usd=max_estimated_cost_usd,
    )
    store = _open_store(database)
    browser_humans: list[BrowserHumanPlayer] = []
    try:
        if human_input == "web":
            terminal_humans = [
                player for player in selected_players if isinstance(player, HumanPlayer)
            ]
            for terminal_human in terminal_humans:
                browser_humans.append(
                    BrowserHumanPlayer.create(
                        store.path,
                        selected_game,
                        selected_players,
                        terminal_human,
                    )
                )
            replacements = {
                id(terminal): browser
                for terminal, browser in zip(
                    terminal_humans,
                    browser_humans,
                    strict=True,
                )
            }
            selected_players = [
                replacements.get(id(player), player)
                for player in selected_players
            ]
            participation = Text(
                f"浏览器人类输入已就绪（{len(browser_humans)} 个独立席位）",
                style="green",
            )
            for index, browser_human in enumerate(browser_humans, start=1):
                participation.append(f"\n\n席位 {index} · ")
                participation.append(literal_text(browser_human.name, style="bold"))
                participation.append("\n")
                participation.append(
                    literal_text(
                        browser_human.participation_url(web_url),
                        style="bold underline",
                    ),
                )
            participation.append(
                "\n\n先启动 llmolympic web，再把每条一次性链接分别交给对应的本机浏览器席位。",
                style="dim",
            )
            console.print(Panel(participation, title=Text("Web 参与链接（请勿混用）")))
        if judge_panel is None:
            asyncio.run(_run(selected_game, selected_players, seed, store))
        else:
            asyncio.run(
                _run(
                    selected_game,
                    selected_players,
                    seed,
                    store,
                    judge_panel=judge_panel,
                )
            )
    except UsageError as exc:
        _render_usage_error(exc)
        raise typer.Exit(code=1) from exc
    except JudgePanelError as exc:
        line = Text("评审失败，对局未存档且未更新 ELO：", style="red")
        line.append(literal_text(exc))
        console.print(line)
        for failure in exc.failures:
            judge_descriptor = failure.get("judge")
            if not isinstance(judge_descriptor, dict):
                continue
            detail = Text("  - ", style="yellow")
            detail.append(
                literal_text(
                    f"{judge_descriptor.get('provider', '?')}:{judge_descriptor.get('model', '?')}",
                    max_chars=NAME_DISPLAY_LIMIT,
                )
            )
            detail.append(" · ")
            detail.append(literal_text(failure.get("reason_code")))
            detail.append(" / ")
            detail.append(literal_text(failure.get("error_type")))
            console.print(detail)
        raise typer.Exit(code=1) from exc
    except HumanInputError as exc:
        line = Text("浏览器人类输入不可用；对局未存档且未更新 ELO：", style="red")
        line.append(literal_text(exc.code))
        console.print(line)
        raise typer.Exit(code=1) from exc
    except (OSError, sqlite3.Error, StorageError) as exc:
        line = Text("对局已完成，但 SQLite 存档失败：", style="red")
        line.append(literal_text(exc))
        console.print(line)
        raise typer.Exit(code=1) from exc
    finally:
        for browser_human in browser_humans:
            browser_human.close()
        _best_effort_render(_render_usage_summary, runtime_budget)


@app.command()
def series(
    game: str = typer.Option(
        "gomoku",
        "--game",
        "-g",
        help=f"比赛项目: {', '.join(list_games('series'))}",
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
    judge: Annotated[
        list[str] | None,
        typer.Option(
            "--judge",
            help="创意双局赛的独立评委，可重复指定 3–9 次；两局冻结并复用同一评审团",
        ),
    ] = None,
    max_provider_calls: int | None = typer.Option(
        None,
        "--max-provider-calls",
        min=0,
        max=SQLITE_INT_MAX,
        help="两局合计模型/算法调用总数硬上限",
    ),
    max_input_tokens: int | None = typer.Option(
        None,
        "--max-input-tokens",
        min=0,
        max=SQLITE_INT_MAX,
        help="两局合计输入 Token 硬上限",
    ),
    max_output_tokens_per_call: int | None = typer.Option(
        None,
        "--max-output-tokens-per-call",
        min=1,
        max=SQLITE_INT_MAX,
        help="每次模型请求的输出 Token 上限（默认 1024）",
    ),
    max_total_output_tokens: int | None = typer.Option(
        None,
        "--max-total-output-tokens",
        min=0,
        max=SQLITE_INT_MAX,
        help="两局合计输出 Token 硬上限",
    ),
    max_estimated_cost_usd: str | None = typer.Option(
        None,
        "--max-estimated-cost-usd",
        help="两局按本地冻结价格计算的美元费用硬上限",
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
        mode="series",
    )
    judge_panel = _prepare_judge_panel(
        selected_game,
        selected_players,
        judge,
        llm_timeout=llm_timeout,
        no_llm_timeout=no_llm_timeout,
    )
    runtime_budget = _prepare_in_memory_budget(
        selected_players,
        judge_panel,
        max_provider_calls=max_provider_calls,
        max_input_tokens=max_input_tokens,
        max_output_tokens_per_call=max_output_tokens_per_call,
        max_total_output_tokens=max_total_output_tokens,
        max_estimated_cost_usd=max_estimated_cost_usd,
    )
    store = _open_store(database)
    try:
        asyncio.run(
            _run_series(
                selected_game,
                selected_players,
                seed,
                store,
                judge_panel=judge_panel,
            )
        )
    except UsageError as exc:
        _render_usage_error(exc)
        raise typer.Exit(code=1) from exc
    except JudgePanelError as exc:
        line = Text("评审失败，双局赛未存档且未更新 ELO：", style="red")
        line.append(literal_text(exc))
        console.print(line)
        raise typer.Exit(code=1) from exc
    except (OSError, sqlite3.Error, StorageError) as exc:
        line = Text("双局赛已完成，但 SQLite 原子存档失败：", style="red")
        line.append(literal_text(exc))
        console.print(line)
        raise typer.Exit(code=1) from exc
    finally:
        _best_effort_render(_render_usage_summary, runtime_budget)


@app.command(name="round-robin")
def round_robin(
    game: str | None = typer.Option(
        None,
        "--game",
        "-g",
        help=f"比赛项目: {', '.join(list_games('round_robin'))}（新赛事默认 knowledge_quiz）",
    ),
    players: str | None = typer.Option(
        None,
        "--players",
        "-p",
        help=(
            "3–16 名非人类选手，如 profile:kimi,profile:deepseek,profile:local；新赛事默认三个 mock"
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
    seed: int | None = typer.Option(
        None,
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
    judge: Annotated[
        list[str] | None,
        typer.Option(
            "--judge",
            help="创意循环赛的独立评委，可重复指定 3–9 次；配置会冻结进 checkpoint",
        ),
    ] = None,
    allow_large_tournament: bool = typer.Option(
        False,
        "--allow-large-tournament",
        help=("显式允许超过默认对局/调用阈值的大型循环赛；请先确认 Provider 费用预算"),
    ),
    max_provider_calls: int | None = typer.Option(
        None,
        "--max-provider-calls",
        min=0,
        max=SQLITE_INT_MAX,
        help="整项循环赛模型/算法调用总数硬上限",
    ),
    max_input_tokens: int | None = typer.Option(
        None,
        "--max-input-tokens",
        min=0,
        max=SQLITE_INT_MAX,
        help="整项循环赛累计输入 Token 硬上限",
    ),
    max_output_tokens_per_call: int | None = typer.Option(
        None,
        "--max-output-tokens-per-call",
        min=1,
        max=SQLITE_INT_MAX,
        help="每次模型请求的输出 Token 上限（默认 1024）",
    ),
    max_total_output_tokens: int | None = typer.Option(
        None,
        "--max-total-output-tokens",
        min=0,
        max=SQLITE_INT_MAX,
        help="整项循环赛累计输出 Token 硬上限",
    ),
    max_estimated_cost_usd: str | None = typer.Option(
        None,
        "--max-estimated-cost-usd",
        help="整项循环赛按本地冻结价格计算的美元费用硬上限",
    ),
    resume: str | None = typer.Option(
        None,
        "--resume",
        help="从 SQLite 检查点恢复赛事 ID；比赛配置由检查点冻结",
    ),
    database: Annotated[
        Path | None,
        typer.Option("--db", help="SQLite 文件；默认读取 LLMOLYMPIC_DB / storage.database"),
    ] = None,
) -> None:
    """新建或恢复 3–16 名非人类选手的交换顺序循环赛。"""

    resumed = resume is not None
    resolved_budget: ResolvedProviderBudget | None = None
    runtime_budget: _RuntimeBudget | None = None
    judge_panel: LLMJudgePanel | None = None
    if resumed:
        conflicts = [
            option
            for option, supplied in (
                ("--game", game is not None),
                ("--players", players is not None),
                ("--rounds", rounds is not None),
                ("--seed", seed is not None),
                ("--llm-timeout", llm_timeout is not None),
                ("--no-llm-timeout", no_llm_timeout),
                ("--judge", judge is not None),
                ("--allow-large-tournament", allow_large_tournament),
                ("--max-provider-calls", max_provider_calls is not None),
                ("--max-input-tokens", max_input_tokens is not None),
                (
                    "--max-output-tokens-per-call",
                    max_output_tokens_per_call is not None,
                ),
                ("--max-total-output-tokens", max_total_output_tokens is not None),
                ("--max-estimated-cost-usd", max_estimated_cost_usd is not None),
            )
            if supplied
        ]
        if conflicts:
            raise typer.BadParameter(
                f"使用 --resume 时不能同时指定比赛配置: {', '.join(conflicts)}；"
                "这些配置已由检查点冻结",
                param_hint="--resume",
            )
        if not resume.strip():
            raise typer.BadParameter("恢复赛事 ID 不能为空", param_hint="--resume")
        store = _open_store(database, create=False)
        try:
            completed = store.get_verified_tournament(resume)
            if completed is not None:
                _best_effort_render(_render_tournament_summary, completed)
                line = Text("循环赛 ", style="yellow")
                line.append(literal_text(resume, max_chars=NAME_DISPLAY_LIMIT))
                line.append(" 已完成，无需恢复，也不会重复更新 ELO。")
                console.print(line)
                return
        except (OSError, sqlite3.Error, StorageError, ValueError) as exc:
            line = Text("无法读取循环赛检查点：", style="red")
            line.append(literal_text(exc))
            console.print(line)
            raise typer.Exit(code=1) from exc
    else:
        selected_game, selected_players = _prepare_round_robin(
            game_name=DEFAULT_TOURNAMENT_GAME if game is None else game,
            player_spec=DEFAULT_TOURNAMENT_PLAYERS if players is None else players,
            rounds=rounds,
            llm_timeout=llm_timeout,
            no_llm_timeout=no_llm_timeout,
        )
        judge_panel = _prepare_judge_panel(
            selected_game,
            selected_players,
            judge,
            llm_timeout=llm_timeout,
            no_llm_timeout=no_llm_timeout,
        )
        effective_seed = DEFAULT_TOURNAMENT_SEED if seed is None else seed
        _validate_tournament_workload(
            selected_game,
            len(selected_players),
            allow_large=allow_large_tournament,
            judge_count=0 if judge_panel is None else len(judge_panel.judges),
        )
        resolved_budget = _resolve_budget_definition(
            selected_players,
            judge_panel,
            max_provider_calls=max_provider_calls,
            max_input_tokens=max_input_tokens,
            max_output_tokens_per_call=max_output_tokens_per_call,
            max_total_output_tokens=max_total_output_tokens,
            max_estimated_cost_usd=max_estimated_cost_usd,
        )
        checkpoint = prepare_round_robin(
            selected_game,
            selected_players,
            seed=effective_seed,
            max_attempts=TOURNAMENT_MOVE_ATTEMPTS,
            judge_panel=judge_panel,
        )
        store = _open_store(database)
        try:
            if resolved_budget is None:
                store.save_tournament_checkpoint(checkpoint)
            else:
                budget_id = resolved_budget.budget_id_for(checkpoint.tournament_id)
                store.create_tournament_checkpoint_with_provider_budget(
                    checkpoint,
                    budget_id,
                    resolved_budget.limits,
                    resolved_budget.policy,
                )
        except (OSError, sqlite3.Error, StorageError, UsageError, ValueError) as exc:
            line = Text("无法创建循环赛检查点：", style="red")
            line.append(literal_text(exc))
            console.print(line)
            raise typer.Exit(code=1) from exc

    tournament_id = resume if resume is not None else checkpoint.tournament_id
    try:
        claim = store.claim_tournament_runner(tournament_id)
    except (OSError, sqlite3.Error, StorageError, TypeError, ValueError) as exc:
        line = Text("无法取得循环赛执行权：", style="red")
        line.append(literal_text(exc))
        console.print(line)
        raise typer.Exit(code=1) from exc

    checkpoint = claim.checkpoint
    lease = claim.lease

    try:
        stored_budget = store.load_tournament_provider_budget(checkpoint.tournament_id)
        if stored_budget is not None:
            resolved_budget = ResolvedProviderBudget(
                limits=stored_budget.limits,
                policy=stored_budget.policy,
            )
            runtime_budget = _RuntimeBudget(
                resolved=resolved_budget,
                ledger=store.bind_tournament_usage_budget(
                    checkpoint.tournament_id,
                    lease=lease,
                ),
            )
    except (OSError, sqlite3.Error, StorageError, UsageError, ValueError) as exc:
        line = Text("无法恢复循环赛 Provider 预算：", style="red")
        line.append(literal_text(exc))
        console.print(line)
        try:
            store.release_tournament_runner(lease)
        except (OSError, sqlite3.Error, StorageError, TypeError, ValueError):
            pass
        raise typer.Exit(code=1) from exc

    try:
        _best_effort_render(_render_checkpoint_ready, checkpoint, store, resumed)
        if checkpoint.is_complete:
            result = store.finalize_tournament_checkpoint(
                checkpoint.tournament_id,
                lease=lease,
            )
            tournament = store.get_verified_tournament(checkpoint.tournament_id)
            if tournament is None:  # pragma: no cover - transaction contract guard
                raise StorageError("循环赛 checkpoint 已封存，但正式档案无法读取")
            _best_effort_render(_render_tournament_summary, tournament)
            _best_effort_render(_render_tournament_saved, tournament, store, result)
            return
        if resumed:
            try:
                selected_game, selected_players = _restore_round_robin(checkpoint)
                judge_panel = _restore_checkpoint_judge_panel(
                    checkpoint,
                    selected_game,
                    selected_players,
                )
            except typer.BadParameter:
                raise
            except (TypeError, ValueError) as exc:
                raise typer.BadParameter(
                    f"无法从检查点恢复比赛配置: {exc}",
                    param_hint="--resume",
                ) from exc
        if runtime_budget is not None:
            runtime_budget = _bind_runtime_budget(
                selected_players,
                judge_panel,
                runtime_budget.resolved,
                runtime_budget.ledger,
            )
        asyncio.run(
            _run_round_robin(
                selected_game,
                selected_players,
                checkpoint,
                store,
                lease,
                judge_panel=judge_panel,
            )
        )
    except KeyboardInterrupt as exc:
        try:
            latest = store.get_tournament_checkpoint(checkpoint.tournament_id) or checkpoint
        except (OSError, sqlite3.Error, StorageError, ValueError):
            latest = checkpoint
        _best_effort_render(_render_tournament_interrupted, latest, store)
        raise typer.Exit(code=130) from exc
    except UsageError as exc:
        _render_usage_error(exc)
        try:
            latest = store.get_tournament_checkpoint(checkpoint.tournament_id)
        except (OSError, sqlite3.Error, StorageError, ValueError):
            latest = None
        if latest is not None:
            _best_effort_render(_render_tournament_interrupted, latest, store)
        raise typer.Exit(code=1) from exc
    except JudgePanelError as exc:
        line = Text("评审失败，循环赛保持在最后完整 checkpoint：", style="red")
        line.append(literal_text(exc))
        console.print(line)
        try:
            latest = store.get_tournament_checkpoint(checkpoint.tournament_id)
        except (OSError, sqlite3.Error, StorageError, ValueError):
            latest = None
        if latest is not None:
            _best_effort_render(_render_tournament_interrupted, latest, store)
        raise typer.Exit(code=1) from exc
    except (OSError, sqlite3.Error, StorageError, TypeError, ValueError) as exc:
        line = Text("循环赛未完成：", style="red")
        line.append(literal_text(exc))
        console.print(line)
        try:
            latest = store.get_tournament_checkpoint(checkpoint.tournament_id)
        except (OSError, sqlite3.Error, StorageError, ValueError):
            latest = None
        if latest is not None:
            _best_effort_render(_render_tournament_interrupted, latest, store)
        raise typer.Exit(code=1) from exc
    finally:
        try:
            store.release_tournament_runner(lease)
        except (OSError, sqlite3.Error, StorageError, TypeError, ValueError) as exc:
            line = Text("警告：未能立即释放循环赛执行权；租约过期后可恢复：", style="yellow")
            line.append(literal_text(exc))
            console.print(line)
        _best_effort_render(_render_usage_summary, runtime_budget)


@app.command(name="web")
def serve_web(
    database: Annotated[
        Path | None,
        typer.Option(
            "--db",
            help="主档案只读；浏览器人类提交仅写独立 input sidecar",
        ),
    ] = None,
    host: Annotated[
        str,
        typer.Option("--host", help="监听地址；首版只允许本机回环地址"),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option("--port", min=1, max=65_535, help="监听端口"),
    ] = 8000,
) -> None:
    """启动本机 Web 参与页、只读观战与对局回放。"""

    normalized_host = host.strip().casefold()
    if normalized_host not in LOCAL_WEB_HOSTS:
        allowed = ", ".join(sorted(LOCAL_WEB_HOSTS))
        raise typer.BadParameter(
            f"首版 Web 服务只允许回环地址（{allowed}）；尚未提供远程认证和 TLS",
            param_hint="--host",
        )

    uvicorn, create_app = _load_web_runtime()
    resolved_database = database_path(database)
    web_app = create_app(resolved_database)
    display_host = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    console.print(f"本机 Web 页面：http://{display_host}:{port}/")
    console.print(f"Web API（健康检查）：http://{display_host}:{port}/api/v1/health")
    console.print(
        "可观看公开事件与存档；持参与链接的本机浏览器可提交人类走法。按 Ctrl-C 停止。"
    )
    uvicorn.run(
        web_app,
        host=normalized_host,
        port=port,
        access_log=False,
        proxy_headers=False,
        forwarded_allow_ips="",
        server_header=False,
        date_header=False,
        ws_max_size=65_536,
        ws_max_queue=16,
        limit_concurrency=64,
        timeout_keep_alive=5,
    )


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
