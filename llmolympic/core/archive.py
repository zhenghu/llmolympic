"""对局档案：pydantic 模型，可 JSON 序列化。

每个模型的采样参数、每步 move、完整事件流都记录在案（公平性与可复核）。
"""

from __future__ import annotations

import copy
import hashlib
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from llmolympic.core.events import EventType, MatchEvent

ARCHIVE_SCHEMA_VERSION = 2
ArchiveSource = Literal["local_engine", "external", "legacy"]
_BIDI_CONTROL_CHARACTERS = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)


def legacy_entrant_id(name: str) -> str:
    """Map one exact historical display name into an isolated stable identity."""

    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    return f"legacy:{digest}"


def validate_entrant_id(entrant_id: object) -> str:
    if not isinstance(entrant_id, str) or not 1 <= len(entrant_id) <= 256:
        raise ValueError("entrant_id 必须是 1 到 256 个字符的非空字符串")
    if any(
        ord(character) < 32
        or 127 <= ord(character) <= 159
        or character in _BIDI_CONTROL_CHARACTERS
        for character in entrant_id
    ):
        raise ValueError("entrant_id 不能包含控制字符或双向文本控制符")
    return entrant_id


def _normalized_descriptor(descriptor: object, *, legacy: bool) -> dict:
    if not isinstance(descriptor, dict):
        raise TypeError("选手描述必须是对象")
    normalized = dict(descriptor)
    name = normalized.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("每个选手描述都必须包含非空 name")
    if legacy:
        normalized["entrant_id"] = legacy_entrant_id(name)
        normalized["display_name"] = name
        return normalized
    entrant_id = normalized.get("entrant_id")
    display_name = normalized.get("display_name")
    validate_entrant_id(entrant_id)
    if entrant_id.startswith("legacy:"):
        raise ValueError("schema v2 选手不能声明保留的 legacy entrant_id")
    if not isinstance(display_name, str) or not display_name:
        raise ValueError("schema v2 选手描述必须包含非空 display_name")
    if display_name != name:
        raise ValueError("选手描述的 display_name 必须与兼容字段 name 一致")
    return normalized


def normalize_player_descriptors(
    descriptors: object, *, legacy: bool
) -> list[dict]:
    if not isinstance(descriptors, (list, tuple)):
        raise TypeError("players 必须是选手描述列表")
    return [_normalized_descriptor(descriptor, legacy=legacy) for descriptor in descriptors]


class MoveRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    player: str
    prompt: str
    move: str
    accepted: bool
    reason: str | None = None


class MatchArchive(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1, 2] = ARCHIVE_SCHEMA_VERSION
    source: ArchiveSource = "external"
    match_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    game: str
    seed: int
    players: list[dict]  # Player.describe()，含 provider / 模型 / 采样参数
    events: list[MatchEvent]  # 完整事件流
    moves: list[MoveRecord]  # 每步走法（含被拒绝的）
    scores: dict[str, float]
    started_at: datetime
    finished_at: datetime

    @model_validator(mode="before")
    @classmethod
    def migrate_identity_fields(cls, value: object) -> object:
        """Enrich schema v1 in memory while keeping persisted legacy JSON untouched."""

        if not isinstance(value, dict):
            return value
        migrated = copy.deepcopy(value)
        # Historical callers commonly omitted schema_version entirely. Keep that
        # wire format in the v1/legacy compatibility path; new engine archives
        # always pass schema_version=2 explicitly in archive_from_events().
        raw_version = migrated.get("schema_version")
        legacy = raw_version in (None, 1)
        migrated["schema_version"] = 1 if legacy else raw_version
        migrated["source"] = migrated.get(
            "source", "legacy" if legacy else "external"
        )
        players = normalize_player_descriptors(migrated.get("players"), legacy=legacy)
        migrated["players"] = players

        events = migrated.get("events")
        if legacy and isinstance(events, (list, tuple)):
            normalized_events = list(events)
            for index, event in enumerate(normalized_events):
                if isinstance(event, MatchEvent):
                    event = event.model_dump(mode="python")
                elif isinstance(event, dict):
                    event = copy.deepcopy(event)
                if not isinstance(event, dict) or event.get("type") not in (
                    EventType.MATCH_STARTED,
                    EventType.MATCH_STARTED.value,
                ):
                    continue
                raw_data = event.get("data")
                if raw_data is not None and not isinstance(raw_data, dict):
                    continue
                data = dict(raw_data or {})
                event_players = data.get("players")
                if event_players is not None:
                    normalized_event_players = normalize_player_descriptors(
                        event_players, legacy=True
                    )
                    if normalized_event_players != players:
                        raise ValueError("legacy match_started 的选手描述与档案不一致")
                data["players"] = copy.deepcopy(players)
                event["data"] = data
                normalized_events[index] = event
            migrated["events"] = normalized_events
        return migrated

    @model_validator(mode="after")
    def validate_identity_fields(self) -> MatchArchive:
        legacy = self.schema_version == 1
        if legacy and self.source != "legacy":
            raise ValueError("schema v1 档案来源必须是 legacy")
        if not legacy and self.source == "legacy":
            raise ValueError("schema v2 档案来源不能是 legacy")
        normalized = normalize_player_descriptors(self.players, legacy=legacy)
        entrant_ids = [descriptor["entrant_id"] for descriptor in normalized]
        if len(set(entrant_ids)) != len(entrant_ids):
            raise ValueError("对局中的 entrant_id 必须唯一")
        for event in self.events:
            if event.type != EventType.MATCH_STARTED:
                continue
            try:
                event_players = normalize_player_descriptors(
                    event.data.get("players"), legacy=legacy
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("match_started 必须包含有效的选手描述") from exc
            if event_players != normalized:
                prefix = "legacy " if legacy else ""
                raise ValueError(f"{prefix}match_started 的选手描述与档案不一致")
        return self

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
        schema_version=ARCHIVE_SCHEMA_VERSION,
        source="local_engine",
        game=game,
        seed=seed,
        players=players,
        events=events,
        moves=moves,
        scores=scores,
        started_at=started_at,
        finished_at=finished_at,
    )
