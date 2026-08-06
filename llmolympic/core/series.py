"""交换先后手的双局赛编排与档案。"""

from __future__ import annotations

import copy
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from llmolympic.core.archive import ArchiveSource, MatchArchive, normalize_player_descriptors
from llmolympic.core.events import EventType, MatchEvent
from llmolympic.core.game import Game, validate_players
from llmolympic.core.match import play_match
from llmolympic.core.player import Player

SERIES_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class PlayerSeriesStanding:
    player: str
    wins: int
    draws: int
    losses: int
    technical_losses: int
    points: float


def _player_identities(
    descriptors: tuple[dict, dict] | list[dict],
) -> tuple[tuple[str, str], tuple[str, str]]:
    names: list[str] = []
    entrant_ids: list[str] = []
    for descriptor in descriptors:
        if not isinstance(descriptor, dict):
            raise TypeError("系列赛选手描述必须是对象")
        name = descriptor.get("name")
        display_name = descriptor.get("display_name")
        entrant_id = descriptor.get("entrant_id")
        if not isinstance(name, str) or not name:
            raise ValueError("系列赛中的每个选手描述都必须包含非空 name")
        if display_name != name:
            raise ValueError("系列赛选手的 display_name 必须与 name 一致")
        if not isinstance(entrant_id, str) or not entrant_id:
            raise ValueError("系列赛中的每个选手描述都必须包含非空 entrant_id")
        names.append(name)
        entrant_ids.append(entrant_id)
    if len(names) != 2:
        raise ValueError("交换先后手的双局赛需要恰好 2 名选手")
    if names[0] == names[1]:
        raise ValueError("系列赛中的选手名字必须唯一")
    if entrant_ids[0] == entrant_ids[1]:
        raise ValueError("系列赛中的 entrant_id 必须唯一")
    return (names[0], entrant_ids[0]), (names[1], entrant_ids[1])


def _player_names(descriptors: tuple[dict, dict] | list[dict]) -> tuple[str, str]:
    first, second = _player_identities(descriptors)
    return first[0], second[0]


def head_to_head_point(archive: MatchArchive, player: str) -> float:
    """把一局的原始比分换算为指定选手的胜负局分。"""

    names = _player_names(archive.players)
    if player not in names:
        raise ValueError("局分只能从包含指定选手的双人对局中计算")
    if set(archive.scores) != set(names):
        raise ValueError("对局选手与 scores 必须完全一致")
    if any(
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(score)
        or not 0.0 <= score <= 1.0
        for score in archive.scores.values()
    ):
        raise ValueError("对局比分必须是 0.0 到 1.0 之间的有限数值")
    opponent = names[1] if names[0] == player else names[0]
    player_score = archive.scores[player]
    opponent_score = archive.scores[opponent]
    return 1.0 if player_score > opponent_score else 0.0 if player_score < opponent_score else 0.5


class SeriesArchive(BaseModel):
    """两名选手交换顺序各赛一局的完整档案。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1, 2] = SERIES_SCHEMA_VERSION
    source: ArchiveSource = "external"
    series_id: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1)
    game: str
    seed: int
    players: tuple[dict, dict]
    legs: tuple[MatchArchive, MatchArchive]
    points: dict[str, float]
    started_at: datetime
    finished_at: datetime

    @model_validator(mode="before")
    @classmethod
    def migrate_identity_fields(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        migrated = copy.deepcopy(value)
        # Missing schema_version is an established v1 wire format. New series
        # constructors below always provide schema_version=2 explicitly.
        raw_version = migrated.get("schema_version")
        legacy = raw_version in (None, 1)
        migrated["schema_version"] = 1 if legacy else raw_version
        migrated["source"] = migrated.get(
            "source", "legacy" if legacy else "external"
        )
        migrated["players"] = normalize_player_descriptors(
            migrated.get("players"), legacy=legacy
        )
        return migrated

    @model_validator(mode="after")
    def validate_consistency(self) -> SeriesArchive:
        player_a, player_b = _player_names(self.players)
        first, second = self.legs

        if self.schema_version == 1 and self.source != "legacy":
            raise ValueError("schema v1 系列赛来源必须是 legacy")
        if self.schema_version == 2 and self.source == "legacy":
            raise ValueError("schema v2 系列赛来源不能是 legacy")
        if first.source != self.source or second.source != self.source:
            raise ValueError("系列赛来源必须与两局档案一致")
        if first.schema_version != self.schema_version or second.schema_version != self.schema_version:
            raise ValueError("系列赛 schema_version 必须与两局档案一致")

        if first.match_id == second.match_id:
            raise ValueError("双局赛的两局必须使用不同 match_id")
        if first.game != self.game or second.game != self.game:
            raise ValueError("双局赛的项目必须与两局档案一致")
        if first.seed != self.seed or second.seed != self.seed:
            raise ValueError("双局赛的两局必须使用相同 seed")
        game_configs: list[dict] = []
        for leg in self.legs:
            if not leg.events or leg.events[0].type != EventType.MATCH_STARTED:
                raise ValueError("双局赛的每局必须以 match_started 事件开始")
            game_config = leg.events[0].data.get("game_config")
            if not isinstance(game_config, dict):
                raise TypeError("双局赛的每局必须记录项目配置")
            game_configs.append(game_config)
        if game_configs[0] != game_configs[1]:
            raise ValueError("双局赛的两局必须使用完全相同的项目配置")
        if tuple(first.players) != self.players:
            raise ValueError("第一局选手顺序必须与系列赛选手顺序一致")
        if tuple(second.players) != tuple(reversed(self.players)):
            raise ValueError("第二局必须完整交换两名选手的顺序")
        timestamps = (
            first.started_at,
            first.finished_at,
            second.started_at,
            second.finished_at,
            self.started_at,
            self.finished_at,
        )
        if any(timestamp.utcoffset() is None for timestamp in timestamps):
            raise ValueError("系列赛及两局的开始和结束时间必须包含时区")
        if first.finished_at < first.started_at or second.finished_at < second.started_at:
            raise ValueError("对局结束时间不能早于开始时间")
        if first.finished_at > second.started_at:
            raise ValueError("第二局不能早于第一局结束")
        if self.started_at != first.started_at or self.finished_at != second.finished_at:
            raise ValueError("系列赛时间范围必须覆盖两局且与两局边界一致")

        expected_points = {
            player_a: head_to_head_point(first, player_a)
            + head_to_head_point(second, player_a),
            player_b: head_to_head_point(first, player_b)
            + head_to_head_point(second, player_b),
        }
        if set(self.points) != {player_a, player_b}:
            raise ValueError("系列赛选手与 points 必须完全一致")
        if any(not math.isfinite(point) or not 0.0 <= point <= 2.0 for point in self.points.values()):
            raise ValueError("系列赛局分必须是 0.0 到 2.0 之间的有限数值")
        if self.points != expected_points:
            raise ValueError("系列赛局分与两局档案不一致")
        for leg in self.legs:
            if not leg.events:
                continue
            finished_data = leg.events[-1].data
            if finished_data.get("termination") == "technical_loss":
                forfeited_by = finished_data.get("forfeited_by")
                if leg.events[-1].type != EventType.MATCH_FINISHED:
                    raise ValueError("技术负必须记录在终局事件中")
                if not isinstance(forfeited_by, str) or forfeited_by not in {
                    player_a,
                    player_b,
                }:
                    raise ValueError("技术负责任方必须是系列赛参赛选手")
                opponent = player_b if forfeited_by == player_a else player_a
                if leg.scores[forfeited_by] != 0.0 or leg.scores[opponent] != 1.0:
                    raise ValueError("技术负必须记为责任方 0 分、对手 1 分")
        return self

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)

    @property
    def standings(self) -> dict[str, PlayerSeriesStanding]:
        """按选手名返回胜平负、技术负与局分汇总。"""

        names = _player_names(self.players)
        result: dict[str, PlayerSeriesStanding] = {}
        for player in names:
            outcomes = [head_to_head_point(leg, player) for leg in self.legs]
            technical_losses = sum(
                bool(leg.events)
                and leg.events[-1].data.get("termination") == "technical_loss"
                and leg.events[-1].data.get("forfeited_by") == player
                for leg in self.legs
            )
            result[player] = PlayerSeriesStanding(
                player=player,
                wins=outcomes.count(1.0),
                draws=outcomes.count(0.5),
                losses=outcomes.count(0.0),
                technical_losses=technical_losses,
                points=sum(outcomes),
            )
        return result


def series_from_legs(
    first: MatchArchive,
    second: MatchArchive,
    *,
    series_id: str | None = None,
) -> SeriesArchive:
    """从顺序相反的两局档案生成系列赛档案。"""

    player_a, player_b = _player_names(first.players)
    if first.schema_version != second.schema_version or first.source != second.source:
        raise ValueError("双局赛的两局必须使用相同档案版本与来源")
    values: dict[str, object] = {
        "schema_version": first.schema_version,
        "source": first.source,
        "game": first.game,
        "seed": first.seed,
        "players": tuple(first.players),
        "legs": (first, second),
        "points": {
            player_a: head_to_head_point(first, player_a)
            + head_to_head_point(second, player_a),
            player_b: head_to_head_point(first, player_b)
            + head_to_head_point(second, player_b),
        },
        "started_at": first.started_at,
        "finished_at": second.finished_at,
    }
    if series_id is not None:
        values["series_id"] = series_id
    return SeriesArchive.model_validate(values)


async def play_two_leg_series(
    game: Game,
    players: list[Player],
    seed: int = 0,
    max_attempts: int = 3,
    on_event: Callable[[int, MatchEvent], None] | None = None,
) -> SeriesArchive:
    """使用同一 seed 跑两局，第二局交换选手顺序。

    ``on_event`` 的第一个参数是从 1 开始的局号，便于界面实时区分两局。
    同一批 ``Player`` 实例会跨局复用且不重置；seed 只复现 Game 条件，不保证
    外部模型输出相同。POSIX 内置 stdin 的 ``HumanPlayer`` 会在超时或取消后移除
    事件循环 reader；自定义输入及不支持 reader 的平台会回退到不可强制终止的
    工作线程，库调用者应只在输入可取消时把人类选手用于双局赛。共享终端也不提供
    多名人类之间的盲答隔离。
    """

    entrants = tuple(players)
    if len(entrants) != 2:
        raise ValueError("交换先后手的双局赛需要恰好 2 名选手")
    entrant_names = (entrants[0].name, entrants[1].name)
    if any(not isinstance(name, str) or not name for name in entrant_names):
        raise ValueError("双局赛选手名字必须是非空字符串")
    validate_players(game, list(entrant_names))
    if entrants[0].entrant_id == entrants[1].entrant_id:
        raise ValueError("双局赛选手的 entrant_id 必须唯一")

    legs: list[MatchArchive] = []
    orders = ([entrants[0], entrants[1]], [entrants[1], entrants[0]])
    for leg_number, ordered_players in enumerate(orders, start=1):
        event_callback: Callable[[MatchEvent], None] | None = None
        if on_event is not None:

            def event_callback(event: MatchEvent, *, _leg: int = leg_number) -> None:
                on_event(_leg, event)

        archive = await play_match(
            game,
            ordered_players,
            seed=seed,
            max_attempts=max_attempts,
            on_event=event_callback,
        )
        expected_names = entrant_names if leg_number == 1 else tuple(reversed(entrant_names))
        archived_names = tuple(descriptor.get("name") for descriptor in archive.players)
        if archived_names != expected_names:
            raise ValueError(f"第 {leg_number} 局档案中的选手身份或顺序发生变化")
        expected_ids = (
            (entrants[0].entrant_id, entrants[1].entrant_id)
            if leg_number == 1
            else (entrants[1].entrant_id, entrants[0].entrant_id)
        )
        archived_ids = tuple(descriptor.get("entrant_id") for descriptor in archive.players)
        if archived_ids != expected_ids:
            raise ValueError(f"第 {leg_number} 局档案中的 entrant_id 或顺序发生变化")
        legs.append(archive)

    return series_from_legs(legs[0], legs[1])
