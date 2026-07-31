"""对局档案：pydantic 模型，可 JSON 序列化。

每个模型的采样参数、每步 move、完整事件流都记录在案（公平性与可复核）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from llmolympic.core.events import EventType, MatchEvent


class MoveRecord(BaseModel):
    player: str
    prompt: str
    move: str
    accepted: bool
    reason: str | None = None


class MatchArchive(BaseModel):
    match_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    game: str
    seed: int
    players: list[dict]  # Player.describe()，含 provider / 模型 / 采样参数
    events: list[MatchEvent]  # 完整事件流
    moves: list[MoveRecord]  # 每步走法（含被拒绝的）
    scores: dict[str, float]
    started_at: datetime
    finished_at: datetime

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)


def archive_from_events(
    *,
    game: str,
    seed: int,
    players: list[dict],
    events: list[MatchEvent],
    started_at: datetime,
    finished_at: datetime,
) -> MatchArchive:
    """从一条完整事件流还原出对局档案。"""
    moves: list[MoveRecord] = []
    prompts: dict[str, str] = {}
    scores: dict[str, float] = {}
    for ev in events:
        if ev.type == EventType.TURN_PROMPT and ev.player:
            prompts[ev.player] = ev.data.get("prompt", "")
        elif ev.type in (EventType.MOVE_RECEIVED, EventType.MOVE_REJECTED) and ev.player:
            moves.append(
                MoveRecord(
                    player=ev.player,
                    prompt=prompts.get(ev.player, ""),
                    move="" if ev.data.get("move") is None else str(ev.data["move"]),
                    accepted=ev.type == EventType.MOVE_RECEIVED,
                    reason=ev.data.get("reason"),
                )
            )
        elif ev.type == EventType.MATCH_FINISHED:
            scores = dict(ev.data.get("scores", {}))
    return MatchArchive(
        game=game,
        seed=seed,
        players=players,
        events=events,
        moves=moves,
        scores=scores,
        started_at=started_at,
        finished_at=finished_at,
    )
