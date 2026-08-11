"""Match 编排器：通用回合循环，事件驱动。

主循环对单轮问答与多轮对局一视同仁（单轮 = 每个选手只有一步的特例）：

    state = game.new_state(players, seed)
    while not game.is_over(state):
        names = game.current_players(state)                  # 同轮待行动者
        prompts = {name: game.prompt_for(state, name) for name in names}
        moves = await collect_concurrently(names, prompts)   # 收齐后才推进
        for name in names:                                   # 稳定顺序应用
            game.apply_move(state, name, moves[name])
    scores = game.score(state)                     # 终局判分
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

from llmolympic.core.archive import MatchArchive, archive_from_events
from llmolympic.core.events import EventType, MatchEvent
from llmolympic.core.game import (
    FORFEIT_MOVE,
    Game,
    IllegalMoveError,
    describe_game_config,
    validate_players,
)
from llmolympic.core.judge import LLMJudgePanel
from llmolympic.core.player import (
    DEFAULT_MAX_RESPONSE_CHARS,
    SYSTEM_PROMPT,
    LLMPlayer,
    Player,
    PlayerActionError,
    UsageReservationProtocol,
    release_undispatched_reservations,
    reserve_player_call_batch,
)

_FEEDBACK_PREVIEW_LIMIT = 200
MAX_MOVE_CHARS = DEFAULT_MAX_RESPONSE_CHARS
MAX_MOVE_ATTEMPTS = 10


class PlayerResponseLimitError(PlayerActionError):
    """选手返回的协议数据类型或大小不安全。"""

    reason_code = "response_limit"


def _validate_move_response(move: object) -> str:
    if not isinstance(move, str):
        raise PlayerResponseLimitError(
            "选手返回了非文本走法，判技术负",
            technical_loss=True,
            details={"response_type": type(move).__name__},
        )
    if len(move) > MAX_MOVE_CHARS:
        raise PlayerResponseLimitError(
            f"选手输出超过 {MAX_MOVE_CHARS} 字符上限，判技术负",
            technical_loss=True,
            details={"limit_chars": MAX_MOVE_CHARS, "actual_chars": len(move)},
        )
    return move


def _feedback_preview(text: str) -> str:
    if len(text) <= _FEEDBACK_PREVIEW_LIMIT:
        return text
    return f"{text[: _FEEDBACK_PREVIEW_LIMIT - 3]}..."


def _technical_loss_scores(players: dict[str, Player], forfeited_by: str) -> dict[str, float]:
    """技术负统一记为责任方 0 分，其余选手 1 分。"""
    return {name: 0.0 if name == forfeited_by else 1.0 for name in players}


async def _collect_move_outcomes(
    requests: list[tuple[Player, str]],
    *,
    terminate_on_player_error: bool,
) -> list[tuple[int, object | None, BaseException | None]]:
    """并发收集一个批次，且不把“返回异常对象”误当成“抛出异常”。

    外层任务被取消时，必须取消并回收同批所有子任务。这能向原生异步
    Provider 传播取消；线程支撑的旧同步调用仍受 Python 无法强制停止线程的限制。
    """

    reservations = reserve_player_call_batch(
        (player, prompt, SYSTEM_PROMPT) for player, prompt in requests
    )

    async def get_move(
        player: Player,
        prompt: str,
        reservation: UsageReservationProtocol | None,
    ) -> str:
        if isinstance(player, LLMPlayer):
            return await player.get_move(prompt, reservation=reservation)
        return await player.get_move(prompt)

    # Preserve the original strict turn-based path: a single Player runs in the
    # caller task, with the same cancellation and context-variable behaviour.
    if len(requests) == 1:
        player, prompt = requests[0]
        try:
            return [
                (
                    0,
                    _validate_move_response(
                        await get_move(player, prompt, reservations[0])
                    ),
                    None,
                )
            ]
        except asyncio.CancelledError:
            raise
        except PlayerActionError as exc:
            return [(0, None, exc)]

    tasks: list[asyncio.Task[str]] = []
    try:
        for (player, prompt), reservation in zip(requests, reservations, strict=True):
            tasks.append(asyncio.create_task(get_move(player, prompt, reservation)))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        release_undispatched_reservations(reservations)
        raise

    def task_outcome(task: asyncio.Task) -> tuple[object | None, BaseException | None]:
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return None, None
        if error is not None:
            return None, error
        try:
            return _validate_move_response(task.result()), None
        except PlayerActionError as exc:
            return None, exc

    def is_terminal(error: BaseException | None) -> bool:
        if isinstance(error, asyncio.CancelledError):
            return False
        return error is not None and (
            not isinstance(error, PlayerActionError)
            or error.technical_loss
            or terminate_on_player_error
        )

    try:
        pending_tasks = set(tasks)
        while pending_tasks:
            _, pending_tasks = await asyncio.wait(
                pending_tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Include every task that finished in this event-loop turn, not just
            # the arbitrary subset returned by wait().  Registration order below
            # remains the deterministic tie-break for simultaneous failures.
            completed = {task for task in tasks if task.done()}
            if any(task.cancelled() for task in completed):
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise asyncio.CancelledError

            completed_outcomes = {task: task_outcome(task) for task in completed}
            terminal_tasks = {task for task, (_, error) in completed_outcomes.items() if is_terminal(error)}
            if terminal_tasks:
                still_pending = [task for task in tasks if task not in completed]
                for task in still_pending:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                outcomes_by_index: list[tuple[int, object | None, BaseException | None]] = [
                    (index, *task_outcome(task))
                    for index, task in enumerate(tasks)
                ]
                terminal_outcomes = [
                    outcome
                    for outcome in outcomes_by_index
                    if is_terminal(outcome[2])
                ]
                for index, _, error in terminal_outcomes:
                    if error is not None and not isinstance(error, PlayerActionError):
                        raise error
                # Once this batch terminates the match, successful and ordinary
                # turn-forfeit results must not mutate state. Otherwise a peer
                # completing just before the failure would produce a different
                # archive from the same peer completing just after it. Keep only
                # terminal errors; enumeration preserves registration-order tie-breaks.
                return terminal_outcomes
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        # A peer may make the batch terminal before another newly-created task
        # receives its first event-loop turn.  Such a task never dispatches its
        # Provider call, so its reservation must be released instead of charged.
        release_undispatched_reservations(reservations)

    return [(index, *task_outcome(task)) for index, task in enumerate(tasks)]


def _retry_prompt(original_prompt: str, move: str, reason: str, remaining: int) -> str:
    move_preview = _feedback_preview(repr(move))
    reason_preview = _feedback_preview(reason)
    return (
        f"{original_prompt}\n\n"
        f"上次输出 {move_preview} 未被接受：{reason_preview}\n"
        f"还可重试 {remaining} 次。请修正后重新作答，"
        "只输出符合题面格式的答案。"
    )


class Match:
    """一场对局。

    ``run()`` 是异步生成器：循环的每一步都产出结构化事件，
    界面层（CLI、将来的 WebSocket 推送）只需消费事件渲染。
    """

    def __init__(
        self,
        game: Game,
        players: list[Player],
        seed: int = 0,
        max_attempts: int = 3,
        judge_panel: LLMJudgePanel | None = None,
    ) -> None:
        names = [p.name for p in players]
        validate_players(game, names)
        entrant_ids = [player.entrant_id for player in players]
        if len(set(entrant_ids)) != len(entrant_ids):
            raise ValueError("对局中的 entrant_id 必须唯一")
        if max_attempts < 1:
            raise ValueError("max_attempts 必须至少为 1")
        if max_attempts > MAX_MOVE_ATTEMPTS:
            raise ValueError(f"max_attempts 最多为 {MAX_MOVE_ATTEMPTS}")
        self.game = game
        self.players = {p.name: p for p in players}
        self.seed = seed
        self.max_attempts = max_attempts
        requires_judge_panel = bool(getattr(game, "requires_judge_panel", False))
        if requires_judge_panel and judge_panel is None:
            raise ValueError(f"项目 {game.name!r} 需要 LLM 评审团")
        if not requires_judge_panel and judge_panel is not None:
            raise ValueError(f"项目 {game.name!r} 不接受 LLM 评审团")
        if judge_panel is not None:
            judge_panel.validate_contestants(players)
        self.judge_panel = judge_panel
        self.forfeit_scope = getattr(game, "forfeit_scope", "turn")
        if self.forfeit_scope not in ("turn", "match"):
            raise ValueError("game.forfeit_scope 必须是 'turn' 或 'match'")

    async def run(self) -> AsyncIterator[MatchEvent]:
        seq = 0

        def emit(type_: EventType, player: str | None = None, **data) -> MatchEvent:
            nonlocal seq
            ev = MatchEvent(seq=seq, type=type_, player=player, data=data)
            seq += 1
            return ev

        state = self.game.new_state(list(self.players), self.seed)
        yield emit(
            EventType.MATCH_STARTED,
            game=self.game.name,
            seed=self.seed,
            game_config=describe_game_config(self.game),
            players=[p.describe() for p in self.players.values()],
        )

        termination: dict[str, object] | None = None
        override_scores = False

        while not self.game.is_over(state) and termination is None:
            # Snapshot every prompt before any Player is called. For a simultaneous
            # round this prevents a later prompt from observing an earlier answer.
            reported_names = list(self.game.current_players(state))
            if not reported_names:
                raise ValueError("game.current_players() 在对局未结束时必须返回至少一名选手")
            if len(set(reported_names)) != len(reported_names):
                raise ValueError("game.current_players() 不能返回重复选手")
            unknown_names = [name for name in reported_names if name not in self.players]
            if unknown_names:
                raise ValueError(f"game.current_players() 返回了未知选手: {unknown_names}")
            active_names = set(reported_names)
            current_names = [name for name in self.players if name in active_names]
            original_prompts = {name: self.game.prompt_for(state, name) for name in current_names}
            attempts = {name: 0 for name in current_names}
            pending = current_names
            prompts = dict(original_prompts)

            while pending and termination is None:
                # Prompt and result events use registration order, independent of
                # Provider completion order. Retrying players form the next batch.
                for name in pending:
                    yield emit(EventType.TURN_PROMPT, player=name, prompt=prompts[name])

                outcomes = await _collect_move_outcomes(
                    [(self.players[name], prompts[name]) for name in pending],
                    terminate_on_player_error=self.forfeit_scope == "match",
                )
                outcomes_by_index = {
                    index: (raw_move, action_error) for index, raw_move, action_error in outcomes
                }
                next_pending: list[str] = []
                next_prompts: dict[str, str] = {}

                for index, name in enumerate(pending):
                    # A terminal peer failure cancels and reaps requests that were
                    # still pending.  They never produced an action for this batch.
                    if index not in outcomes_by_index:
                        continue
                    raw_move, action_error = outcomes_by_index[index]
                    try:
                        if action_error is not None:
                            raise action_error
                        move = _validate_move_response(raw_move)
                    except PlayerActionError as exc:
                        self.game.apply_move(state, name, FORFEIT_MOVE)
                        forfeit_scope = "match" if exc.technical_loss else self.forfeit_scope
                        reason = str(exc)
                        if exc.reason_code == "timeout" and not exc.technical_loss:
                            reason = "超时未作答，判放弃"
                        rejected = emit(
                            EventType.MOVE_REJECTED,
                            player=name,
                            move=None,
                            reason=reason,
                            reason_code=exc.reason_code,
                            forfeit=True,
                            forfeit_scope=forfeit_scope,
                            technical_loss=forfeit_scope == "match",
                            failure_details=exc.details,
                        )
                        yield rejected
                        if forfeit_scope == "match":
                            termination = {
                                "termination": "technical_loss",
                                "reason_code": exc.reason_code,
                                "reason": reason,
                                "forfeited_by": name,
                                "cause_event_seq": rejected.seq,
                                "failure_details": exc.details,
                            }
                            override_scores = exc.technical_loss
                            break
                        continue

                    try:
                        if move == FORFEIT_MOVE:
                            raise IllegalMoveError("选手不能直接提交引擎保留的放弃标记")
                        self.game.apply_move(state, name, move)
                        yield emit(EventType.MOVE_RECEIVED, player=name, move=move)
                    except IllegalMoveError as exc:
                        attempts[name] += 1
                        if attempts[name] >= self.max_attempts:
                            self.game.apply_move(state, name, FORFEIT_MOVE)
                            rejection_reason = f"{exc}；已达最大重试次数，判放弃"
                            rejected = emit(
                                EventType.MOVE_REJECTED,
                                player=name,
                                move=move,
                                reason=rejection_reason,
                                reason_code="illegal_move_limit",
                                attempt=attempts[name],
                                max_attempts=self.max_attempts,
                                forfeit=True,
                                forfeit_scope=self.forfeit_scope,
                                technical_loss=self.forfeit_scope == "match",
                            )
                            yield rejected
                            if self.forfeit_scope == "match":
                                termination = {
                                    "termination": "technical_loss",
                                    "reason_code": "illegal_move_limit",
                                    "reason": rejection_reason,
                                    "forfeited_by": name,
                                    "cause_event_seq": rejected.seq,
                                }
                                break
                            continue

                        reason = str(exc)
                        yield emit(
                            EventType.MOVE_REJECTED,
                            player=name,
                            move=move,
                            reason=reason,
                            reason_code="illegal_move",
                            attempt=attempts[name],
                            max_attempts=self.max_attempts,
                            forfeit=False,
                            technical_loss=False,
                        )
                        next_pending.append(name)
                        next_prompts[name] = _retry_prompt(
                            original_prompts[name],
                            move,
                            reason,
                            self.max_attempts - attempts[name],
                        )

                pending = next_pending
                prompts = next_prompts

        judging: dict[str, object] | None = None
        if termination is not None and override_scores:
            scores = _technical_loss_scores(self.players, str(termination["forfeited_by"]))
        elif self.judge_panel is not None:
            judging_request = getattr(self.game, "judging_request", None)
            if not callable(judging_request):
                raise TypeError("需要评审团的项目必须实现 judging_request(state)")
            verdict = await self.judge_panel.adjudicate(judging_request(state), seed=self.seed)
            scores = verdict.scores
            judging = verdict.model_dump(mode="json")
        else:
            scores = self.game.score(state)
        if set(scores) != set(self.players):
            raise ValueError("终局分数必须恰好覆盖全部参赛者")
        finished_data: dict[str, object] = {
            "scores": scores,
            "termination": "completed",
        }
        if termination is not None:
            finished_data.update(termination)
        if judging is not None:
            finished_data["judging"] = judging
        yield emit(EventType.MATCH_FINISHED, **finished_data)


async def play_match(
    game: Game,
    players: list[Player],
    seed: int = 0,
    max_attempts: int = 3,
    on_event: Callable[[MatchEvent], None] | None = None,
    judge_panel: LLMJudgePanel | None = None,
) -> MatchArchive:
    """跑完一整场并返回对局档案。

    ``on_event`` 可用于实时渲染；回调收到的正是最终写入档案的同一批事件，
    因此界面无需为了存档而重跑一场对局。
    """
    match = Match(
        game,
        players,
        seed=seed,
        max_attempts=max_attempts,
        judge_panel=judge_panel,
    )
    started = datetime.now(UTC)
    events: list[MatchEvent] = []
    async for event in match.run():
        events.append(event)
        if on_event is not None:
            on_event(event)
    finished = datetime.now(UTC)
    return archive_from_events(
        game=game.name,
        seed=seed,
        players=[p.describe() for p in players],
        events=events,
        started_at=started,
        finished_at=finished,
    )
