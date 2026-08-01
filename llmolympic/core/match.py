"""Match 编排器：通用回合循环，事件驱动。

主循环对单轮问答与多轮对局一视同仁（单轮 = 每个选手只有一步的特例）：

    state = game.new_state(players, seed)
    while not game.is_over(state):
        for name in game.current_players(state):   # 轮到谁
            prompt = game.prompt_for(state, name)  # 出题
            move   = await player.get_move(prompt) # 收走法（人/模型同接口）
            game.apply_move(state, name, move)     # 校验并推进
    scores = game.score(state)                     # 终局判分
"""

from __future__ import annotations

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
from llmolympic.core.player import DEFAULT_MAX_RESPONSE_CHARS, Player, PlayerActionError

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


class Match:
    """一场对局。

    ``run()`` 是异步生成器：循环的每一步都产出结构化事件，
    界面层（CLI、将来的 WebSocket 推送）只需消费事件渲染。
    """

    def __init__(
        self, game: Game, players: list[Player], seed: int = 0, max_attempts: int = 3
    ) -> None:
        names = [p.name for p in players]
        validate_players(game, names)
        if max_attempts < 1:
            raise ValueError("max_attempts 必须至少为 1")
        if max_attempts > MAX_MOVE_ATTEMPTS:
            raise ValueError(f"max_attempts 最多为 {MAX_MOVE_ATTEMPTS}")
        self.game = game
        self.players = {p.name: p for p in players}
        self.seed = seed
        self.max_attempts = max_attempts
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
            for name in self.game.current_players(state):
                player = self.players[name]
                original_prompt = self.game.prompt_for(state, name)
                prompt = original_prompt
                yield emit(EventType.TURN_PROMPT, player=name, prompt=original_prompt)

                attempts = 0
                while True:
                    try:
                        move = _validate_move_response(await player.get_move(prompt))
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
                    try:
                        self.game.apply_move(state, name, move)
                        yield emit(EventType.MOVE_RECEIVED, player=name, move=move)
                        break
                    except IllegalMoveError as exc:
                        attempts += 1
                        if attempts >= self.max_attempts:
                            self.game.apply_move(state, name, FORFEIT_MOVE)
                            rejection_reason = f"{exc}；已达最大重试次数，判放弃"
                            rejected = emit(
                                EventType.MOVE_REJECTED,
                                player=name,
                                move=move,
                                reason=rejection_reason,
                                reason_code="illegal_move_limit",
                                attempt=attempts,
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
                        reason = str(exc)
                        yield emit(
                            EventType.MOVE_REJECTED,
                            player=name,
                            move=move,
                            reason=reason,
                            reason_code="illegal_move",
                            attempt=attempts,
                            max_attempts=self.max_attempts,
                            forfeit=False,
                            technical_loss=False,
                        )
                        move_preview = _feedback_preview(repr(move))
                        reason_preview = _feedback_preview(reason)
                        remaining = self.max_attempts - attempts
                        prompt = (
                            f"{original_prompt}\n\n"
                            f"上次输出 {move_preview} 未被接受：{reason_preview}\n"
                            f"还可重试 {remaining} 次。请修正后重新作答，"
                            "只输出符合题面格式的答案。"
                        )
                        yield emit(EventType.TURN_PROMPT, player=name, prompt=prompt)

                if termination is not None:
                    break

        if termination is not None and override_scores:
            scores = _technical_loss_scores(self.players, str(termination["forfeited_by"]))
        else:
            scores = self.game.score(state)
        finished_data: dict[str, object] = {
            "scores": scores,
            "termination": "completed",
        }
        if termination is not None:
            finished_data.update(termination)
        yield emit(EventType.MATCH_FINISHED, **finished_data)


async def play_match(
    game: Game,
    players: list[Player],
    seed: int = 0,
    max_attempts: int = 3,
    on_event: Callable[[MatchEvent], None] | None = None,
) -> MatchArchive:
    """跑完一整场并返回对局档案。

    ``on_event`` 可用于实时渲染；回调收到的正是最终写入档案的同一批事件，
    因此界面无需为了存档而重跑一场对局。
    """
    match = Match(game, players, seed=seed, max_attempts=max_attempts)
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
