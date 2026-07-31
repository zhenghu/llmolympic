"""Match 循环产出的结构化事件。

界面层（CLI、将来的 WebSocket 推送）只消费事件做渲染，引擎与界面解耦。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class EventType(str, Enum):
    MATCH_STARTED = "match_started"
    TURN_PROMPT = "turn_prompt"
    MOVE_RECEIVED = "move_received"  # 合法走法已被接受
    MOVE_REJECTED = "move_rejected"  # 非法走法被拒 / 超时 / 被判放弃
    MATCH_FINISHED = "match_finished"


class MatchEvent(BaseModel):
    """一条对局事件。``data`` 载荷随类型不同而变化（prompt、move、scores 等）。"""

    seq: int
    type: EventType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    player: str | None = None
    data: dict = Field(default_factory=dict)
