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
from llmolympic.core.game import FORFEIT_MOVE, Game, IllegalMoveError
from llmolympic.core.player import Player, PlayerTimeoutError


class Match:
    """一场对局。

    ``run()`` 是异步生成器：循环的每一步都产出结构化事件，
    界面层（CLI、将来的 WebSocket 推送）只需消费事件渲染。
    """

    def __init__(
        self, game: Game, players: list[Player], seed: int = 0, max_attempts: int = 3
    ) -> None:
        names = [p.name for p in players]
        if len(set(names)) != len(names):
            raise ValueError(f"选手名字必须唯一: {names}")
        self.game = game
        self.players = {p.name: p for p in players}
        self.seed = seed
        self.max_attempts = max_attempts

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
            players=[p.describe() for p in self.players.values()],
        )

        while not self.game.is_over(state):
            for name in self.game.current_players(state):
                player = self.players[name]
                prompt = self.game.prompt_for(state, name)
                yield emit(EventType.TURN_PROMPT, player=name, prompt=prompt)

                attempts = 0
                while True:
                    try:
                        move = await player.get_move(prompt)
                    except PlayerTimeoutError:
                        self.game.apply_move(state, name, FORFEIT_MOVE)
                        yield emit(
                            EventType.MOVE_REJECTED, player=name, move=None,
                            reason="超时未作答，判放弃",
                        )
                        break
                    try:
                        self.game.apply_move(state, name, move)
                        yield emit(EventType.MOVE_RECEIVED, player=name, move=move)
                        break
                    except IllegalMoveError as exc:
                        attempts += 1
                        if attempts >= self.max_attempts:
                            self.game.apply_move(state, name, FORFEIT_MOVE)
                            yield emit(
                                EventType.MOVE_REJECTED, player=name, move=move,
                                reason=f"{exc}；已达最大重试次数，判放弃",
                            )
                            break
                        yield emit(
                            EventType.MOVE_REJECTED, player=name, move=move, reason=str(exc)
                        )

        yield emit(EventType.MATCH_FINISHED, scores=self.game.score(state))


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
