"""Deterministic round-robin tournaments composed of two-leg series."""

from __future__ import annotations

import copy
import hashlib
import math
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import combinations, pairwise
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from llmolympic.core.archive import normalize_player_descriptors, validate_entrant_id
from llmolympic.core.events import EventType, MatchEvent
from llmolympic.core.game import (
    MAX_PLATFORM_PLAYERS,
    Game,
    describe_game_config,
    validate_players,
)
from llmolympic.core.match import MAX_MOVE_ATTEMPTS
from llmolympic.core.player import HumanPlayer, Player
from llmolympic.core.series import SERIES_SCHEMA_VERSION, SeriesArchive, play_two_leg_series

TOURNAMENT_SCHEMA_VERSION = 1
TOURNAMENT_CHECKPOINT_SCHEMA_VERSION = 1
MIN_TOURNAMENT_PLAYERS = 3
MAX_TOURNAMENT_PLAYERS = MAX_PLATFORM_PLAYERS

_SQLITE_INT_MIN = -(2**63)
_SQLITE_INT_MAX = 2**63 - 1
_SEED_DOMAIN = b"llmolympic.round-robin-pair-seed-v1\0"

TournamentSource = Literal["local_engine", "external"]
TournamentEventCallback = Callable[[int, int, MatchEvent], None]


def _validate_signed_seed(seed: object) -> int:
    if (
        isinstance(seed, bool)
        or not isinstance(seed, int)
        or not _SQLITE_INT_MIN <= seed <= _SQLITE_INT_MAX
    ):
        raise ValueError("seed 必须是 SQLite signed 64-bit 整数")
    return seed


def round_robin_pair_seed(base_seed: int, entrant_a: str, entrant_b: str) -> int:
    """Derive one order-independent signed 64-bit seed for an entrant pair."""

    _validate_signed_seed(base_seed)
    entrant_ids = sorted((validate_entrant_id(entrant_a), validate_entrant_id(entrant_b)))
    if entrant_ids[0] == entrant_ids[1]:
        raise ValueError("循环赛配对中的 entrant_id 必须唯一")
    payload = (
        _SEED_DOMAIN
        + str(base_seed).encode("ascii")
        + b"\0"
        + entrant_ids[0].encode("utf-8")
        + b"\0"
        + entrant_ids[1].encode("utf-8")
    )
    unsigned = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
    return unsigned if unsigned <= _SQLITE_INT_MAX else unsigned - 2**64


@dataclass(frozen=True)
class TournamentStanding:
    """One entrant's aggregate result, ordered by :attr:`TournamentArchive.standings`."""

    player: str
    entrant_id: str
    series_played: int
    series_wins: int
    series_draws: int
    series_losses: int
    games_played: int
    wins: int
    draws: int
    losses: int
    technical_losses: int
    points: float


class RoundRobinPairingSpec(BaseModel):
    """One immutable position in the canonical round-robin schedule."""

    model_config = ConfigDict(extra="forbid")

    pairing_number: int = Field(ge=1)
    player_indices: tuple[int, int]
    seed: int

    @field_validator("pairing_number", mode="before")
    @classmethod
    def validate_pairing_number(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("pairing_number 必须是整数")
        return value

    @field_validator("player_indices", mode="before")
    @classmethod
    def validate_player_indices(cls, value: object) -> object:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise TypeError("player_indices 必须包含恰好两个索引")
        if any(isinstance(index, bool) or not isinstance(index, int) for index in value):
            raise TypeError("player_indices 必须包含整数索引")
        first, second = value
        if first < 0 or second < 0 or first >= second:
            raise ValueError("player_indices 必须是严格递增的非负索引")
        return value

    @field_validator("seed", mode="before")
    @classmethod
    def validate_seed(cls, value: object) -> object:
        return _validate_signed_seed(value)


class RoundRobinPairing(RoundRobinPairingSpec):
    """One scheduled entrant pair and its complete swapped-order series."""

    series: SeriesArchive


def _normalized_tournament_players(descriptors: object) -> tuple[dict, ...]:
    normalized = tuple(normalize_player_descriptors(descriptors, legacy=False))
    count = len(normalized)
    if not MIN_TOURNAMENT_PLAYERS <= count <= MAX_TOURNAMENT_PLAYERS:
        raise ValueError(
            f"循环赛需要 {MIN_TOURNAMENT_PLAYERS} 到 {MAX_TOURNAMENT_PLAYERS} 名选手，"
            f"实际为 {count} 名"
        )

    names = [descriptor["name"] for descriptor in normalized]
    entrant_ids = [descriptor["entrant_id"] for descriptor in normalized]
    if len(set(names)) != count:
        raise ValueError(f"循环赛选手名字必须唯一: {names}")
    if len(set(entrant_ids)) != count:
        raise ValueError(f"循环赛选手 entrant_id 必须唯一: {entrant_ids}")
    for descriptor in normalized:
        kind = descriptor.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ValueError("循环赛选手描述必须包含非空 kind")
        if kind == HumanPlayer.kind:
            raise ValueError("循环赛暂不支持 HumanPlayer")
    return normalized


def _series_game_config(series: SeriesArchive) -> dict:
    first_event = series.legs[0].events[0]
    if first_event.type != EventType.MATCH_STARTED:
        raise ValueError("循环赛双局赛必须以 match_started 事件开始")
    config = first_event.data.get("game_config")
    if not isinstance(config, dict):
        raise TypeError("循环赛双局赛必须记录项目配置")
    return config


def _round_robin_schedule(players: Sequence[dict], seed: int) -> tuple[RoundRobinPairingSpec, ...]:
    entrant_ids = tuple(descriptor["entrant_id"] for descriptor in players)
    return tuple(
        RoundRobinPairingSpec(
            pairing_number=pairing_number,
            player_indices=player_indices,
            seed=round_robin_pair_seed(
                seed,
                entrant_ids[player_indices[0]],
                entrant_ids[player_indices[1]],
            ),
        )
        for pairing_number, player_indices in enumerate(
            combinations(range(len(players)), 2), start=1
        )
    )


def _validate_schedule(
    schedule: Sequence[RoundRobinPairingSpec],
    expected: Sequence[RoundRobinPairingSpec],
) -> None:
    if len(schedule) != len(expected):
        raise ValueError("循环赛必须让每对选手恰好进行一个换色双局赛")
    for pairing, expected_pairing in zip(schedule, expected):
        if pairing.pairing_number != expected_pairing.pairing_number:
            raise ValueError("循环赛 pairing_number 必须按执行顺序连续编号")
        if pairing.player_indices != expected_pairing.player_indices:
            raise ValueError("循环赛配对必须严格遵循输入顺序的两两组合")
        if pairing.seed != expected_pairing.seed:
            raise ValueError("循环赛 pairing seed 与稳定身份派生结果不一致")


def _validate_completed_series(
    *,
    players: Sequence[dict],
    schedule: Sequence[RoundRobinPairingSpec],
    completed_series: Sequence[SeriesArchive],
    source: TournamentSource,
    game: str,
    game_config: dict | None,
) -> tuple[dict[str, float], dict | None]:
    """Validate a canonical completed prefix and derive its aggregate points."""

    if len(completed_series) > len(schedule):
        raise ValueError("循环赛已完成双局赛数量不能超过预建赛程")

    points = {descriptor["name"]: 0.0 for descriptor in players}
    series_ids: set[str] = set()
    match_ids: set[str] = set()
    validated_game_config = game_config
    previous_finished_at: datetime | None = None

    for spec, series in zip(schedule, completed_series):
        if series.schema_version != SERIES_SCHEMA_VERSION:
            raise ValueError("循环赛只接受 schema v2 双局赛档案")
        if series.source != source:
            raise ValueError("循环赛与双局赛档案来源必须一致")
        if series.game != game:
            raise ValueError("循环赛与双局赛项目必须一致")
        if series.seed != spec.seed:
            raise ValueError("循环赛 pairing 与双局赛 seed 必须一致")

        first_index, second_index = spec.player_indices
        expected_players = (players[first_index], players[second_index])
        if series.players != expected_players:
            raise ValueError("循环赛双局赛选手必须与配对索引及身份完全一致")

        for leg in series.legs:
            if [event.seq for event in leg.events] != list(range(len(leg.events))):
                raise ValueError("循环赛对局事件 seq 必须从 0 开始且连续")
            started_events = [
                event for event in leg.events if event.type == EventType.MATCH_STARTED
            ]
            finished_events = [
                event for event in leg.events if event.type == EventType.MATCH_FINISHED
            ]
            if len(started_events) != 1 or started_events[0] is not leg.events[0]:
                raise ValueError("循环赛对局必须以唯一的 match_started 开始")
            if len(finished_events) != 1 or finished_events[0] is not leg.events[-1]:
                raise ValueError("循环赛对局必须以唯一的 match_finished 结束")
            event_timestamps = [event.timestamp for event in leg.events]
            if any(timestamp.utcoffset() is None for timestamp in event_timestamps):
                raise ValueError("循环赛对局事件时间必须包含时区")
            if any(current < previous for previous, current in pairwise(event_timestamps)):
                raise ValueError("循环赛对局事件时间不能逆序")
            if event_timestamps[0] < leg.started_at or event_timestamps[-1] > leg.finished_at:
                raise ValueError("循环赛对局事件必须位于对局时间边界内")
            started_data = started_events[0].data
            if (
                started_data.get("game") != leg.game
                or started_data.get("seed") != leg.seed
                or started_data.get("players") != leg.players
            ):
                raise ValueError("循环赛 match_started 的项目、seed 或选手与对局档案不一致")
            finished_scores = finished_events[0].data.get("scores")
            if not isinstance(finished_scores, dict) or any(
                not isinstance(name, str)
                or isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(score)
                for name, score in finished_scores.items()
            ):
                raise ValueError("循环赛 match_finished 必须包含有限数值比分")
            if {name: float(score) for name, score in finished_scores.items()} != leg.scores:
                raise ValueError("循环赛 match_finished 比分与对局档案不一致")

        current_config = _series_game_config(series)
        if validated_game_config is None:
            validated_game_config = current_config
        elif current_config != validated_game_config:
            raise ValueError("循环赛所有双局赛必须使用完全相同的项目配置")

        if series.series_id in series_ids:
            raise ValueError("循环赛中的 series_id 必须全局唯一")
        series_ids.add(series.series_id)
        for leg in series.legs:
            if leg.match_id in match_ids:
                raise ValueError("循环赛中的 match_id 必须全局唯一")
            match_ids.add(leg.match_id)

        if previous_finished_at is not None and series.started_at < previous_finished_at:
            raise ValueError("循环赛双局赛时间不能重叠或逆序")
        previous_finished_at = series.finished_at
        for player, point in series.points.items():
            if player not in points:
                raise ValueError("双局赛 points 包含循环赛之外的选手")
            points[player] += point

    return points, validated_game_config


def _standings_from_series(
    players: Sequence[dict],
    completed_series: Sequence[SeriesArchive],
    points: dict[str, float],
) -> tuple[TournamentStanding, ...]:
    aggregates: dict[str, dict[str, int | float | str]] = {}
    for descriptor in players:
        player = descriptor["name"]
        aggregates[player] = {
            "player": player,
            "entrant_id": descriptor["entrant_id"],
            "series_played": 0,
            "series_wins": 0,
            "series_draws": 0,
            "series_losses": 0,
            "games_played": 0,
            "wins": 0,
            "draws": 0,
            "losses": 0,
            "technical_losses": 0,
            "points": points[player],
        }

    for series in completed_series:
        first_name = series.players[0]["name"]
        second_name = series.players[1]["name"]
        for player, opponent in (
            (first_name, second_name),
            (second_name, first_name),
        ):
            standing = series.standings[player]
            aggregate = aggregates[player]
            aggregate["series_played"] += 1
            aggregate["games_played"] += 2
            aggregate["wins"] += standing.wins
            aggregate["draws"] += standing.draws
            aggregate["losses"] += standing.losses
            aggregate["technical_losses"] += standing.technical_losses
            if series.points[player] > series.points[opponent]:
                aggregate["series_wins"] += 1
            elif series.points[player] < series.points[opponent]:
                aggregate["series_losses"] += 1
            else:
                aggregate["series_draws"] += 1

    standings = tuple(TournamentStanding(**values) for values in aggregates.values())
    return tuple(
        sorted(
            standings,
            key=lambda standing: (
                -standing.points,
                -standing.wins,
                standing.technical_losses,
                standing.entrant_id,
            ),
        )
    )


class TournamentCheckpoint(BaseModel):
    """A validated, resumable prefix of one local round-robin tournament."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = TOURNAMENT_CHECKPOINT_SCHEMA_VERSION
    source: Literal["local_engine"] = "local_engine"
    tournament_id: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1)
    format: Literal["round_robin_two_leg"] = "round_robin_two_leg"
    pairing_policy: Literal["input_order_combinations_v1"] = "input_order_combinations_v1"
    seed_policy: Literal["entrant_pair_sha256_v1"] = "entrant_pair_sha256_v1"
    game: str
    game_config: dict
    seed: int
    max_attempts: int
    players: tuple[dict, ...]
    schedule: tuple[RoundRobinPairingSpec, ...]
    completed_series: tuple[SeriesArchive, ...] = ()
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="before")
    @classmethod
    def normalize_players(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        normalized["players"] = _normalized_tournament_players(normalized.get("players"))
        game_config = normalized.get("game_config")
        if not isinstance(game_config, dict):
            raise TypeError("循环赛 checkpoint game_config 必须是字典")
        normalized["game_config"] = copy.deepcopy(game_config)
        return normalized

    @field_validator("game", mode="before")
    @classmethod
    def validate_game(cls, value: object) -> object:
        if not isinstance(value, str) or not value:
            raise ValueError("循环赛 game 必须是非空字符串")
        return value

    @field_validator("seed", mode="before")
    @classmethod
    def validate_seed(cls, value: object) -> object:
        return _validate_signed_seed(value)

    @field_validator("max_attempts", mode="before")
    @classmethod
    def validate_max_attempts(cls, value: object) -> object:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= MAX_MOVE_ATTEMPTS
        ):
            raise ValueError(f"max_attempts 必须是 1 到 {MAX_MOVE_ATTEMPTS} 之间的整数")
        return value

    @model_validator(mode="after")
    def validate_consistency(self) -> TournamentCheckpoint:
        expected_schedule = _round_robin_schedule(self.players, self.seed)
        _validate_schedule(self.schedule, expected_schedule)
        _validate_completed_series(
            players=self.players,
            schedule=self.schedule,
            completed_series=self.completed_series,
            source=self.source,
            game=self.game,
            game_config=self.game_config,
        )

        timestamps = (self.created_at, self.updated_at)
        if any(timestamp.utcoffset() is None for timestamp in timestamps):
            raise ValueError("循环赛 checkpoint 时间必须包含时区")
        if self.updated_at < self.created_at:
            raise ValueError("循环赛 checkpoint 更新时间不能早于创建时间")
        if self.completed_series:
            if self.created_at > self.completed_series[0].started_at:
                raise ValueError("循环赛 checkpoint 创建时间不能晚于首组开始时间")
            if self.updated_at != self.completed_series[-1].finished_at:
                raise ValueError("循环赛 checkpoint 更新时间必须等于最后完成组的结束时间")
        elif self.updated_at != self.created_at:
            raise ValueError("空循环赛 checkpoint 的创建和更新时间必须一致")
        return self

    @property
    def points(self) -> dict[str, float]:
        points = {descriptor["name"]: 0.0 for descriptor in self.players}
        for series in self.completed_series:
            for player, point in series.points.items():
                points[player] += point
        return points

    @property
    def standings(self) -> tuple[TournamentStanding, ...]:
        return _standings_from_series(self.players, self.completed_series, self.points)

    @property
    def is_complete(self) -> bool:
        return len(self.completed_series) == len(self.schedule)

    @property
    def next_pairing_number(self) -> int | None:
        if self.is_complete:
            return None
        return self.schedule[len(self.completed_series)].pairing_number


TournamentCheckpointCallback = Callable[[TournamentCheckpoint], None]
TournamentPairingStartCallback = Callable[[RoundRobinPairingSpec], None]


class TournamentArchive(BaseModel):
    """A complete deterministic all-play-all tournament archive."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = TOURNAMENT_SCHEMA_VERSION
    source: TournamentSource = "external"
    tournament_id: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1)
    format: Literal["round_robin_two_leg"] = "round_robin_two_leg"
    pairing_policy: Literal["input_order_combinations_v1"] = "input_order_combinations_v1"
    seed_policy: Literal["entrant_pair_sha256_v1"] = "entrant_pair_sha256_v1"
    game: str
    seed: int
    players: tuple[dict, ...]
    pairings: tuple[RoundRobinPairing, ...]
    points: dict[str, float]
    started_at: datetime
    finished_at: datetime

    @model_validator(mode="before")
    @classmethod
    def normalize_players(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        # Do not duplicate every nested event and move before Pydantic parses a
        # potentially large tournament. Player descriptors are copied by the
        # normalization helper below.
        normalized = dict(value)
        normalized["players"] = _normalized_tournament_players(normalized.get("players"))
        return normalized

    @field_validator("game", mode="before")
    @classmethod
    def validate_game(cls, value: object) -> object:
        if not isinstance(value, str) or not value:
            raise ValueError("循环赛 game 必须是非空字符串")
        return value

    @field_validator("seed", mode="before")
    @classmethod
    def validate_seed(cls, value: object) -> object:
        return _validate_signed_seed(value)

    @field_validator("points", mode="before")
    @classmethod
    def validate_raw_points(cls, value: object) -> object:
        if not isinstance(value, dict):
            raise TypeError("循环赛 points 必须是字典")
        for point in value.values():
            if (
                isinstance(point, bool)
                or not isinstance(point, (int, float))
                or not math.isfinite(point)
            ):
                raise ValueError("循环赛总局分必须是有限数值")
        return value

    @model_validator(mode="after")
    def validate_consistency(self) -> TournamentArchive:
        names = tuple(descriptor["name"] for descriptor in self.players)
        expected_schedule = _round_robin_schedule(self.players, self.seed)
        actual_schedule = tuple(
            RoundRobinPairingSpec(
                pairing_number=pairing.pairing_number,
                player_indices=pairing.player_indices,
                seed=pairing.seed,
            )
            for pairing in self.pairings
        )
        _validate_schedule(actual_schedule, expected_schedule)
        expected_points, _ = _validate_completed_series(
            players=self.players,
            schedule=actual_schedule,
            completed_series=tuple(pairing.series for pairing in self.pairings),
            source=self.source,
            game=self.game,
            game_config=None,
        )

        timestamps = (self.started_at, self.finished_at)
        if any(timestamp.utcoffset() is None for timestamp in timestamps):
            raise ValueError("循环赛开始和结束时间必须包含时区")
        if self.finished_at < self.started_at:
            raise ValueError("循环赛结束时间不能早于开始时间")
        if (
            self.started_at != self.pairings[0].series.started_at
            or self.finished_at != self.pairings[-1].series.finished_at
        ):
            raise ValueError("循环赛时间范围必须与首尾双局赛边界一致")

        if set(self.points) != set(names):
            raise ValueError("循环赛选手与 points 必须完全一致")
        maximum_points = 2.0 * (len(self.players) - 1)
        if any(not 0.0 <= point <= maximum_points for point in self.points.values()):
            raise ValueError(f"循环赛总局分必须是 0.0 到 {maximum_points:.1f} 之间的有限数值")
        if self.points != expected_points:
            raise ValueError("循环赛总局分与所有双局赛档案不一致")
        return self

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)

    @property
    def standings(self) -> tuple[TournamentStanding, ...]:
        """Return deterministic aggregate standings without duplicating archive data."""
        return _standings_from_series(
            self.players,
            tuple(pairing.series for pairing in self.pairings),
            self.points,
        )


def tournament_from_series(
    players: Sequence[dict],
    series: Sequence[SeriesArchive],
    *,
    seed: int,
    tournament_id: str | None = None,
) -> TournamentArchive:
    """Build a tournament archive from series in canonical input-pair order."""

    _validate_signed_seed(seed)
    normalized_players = _normalized_tournament_players(copy.deepcopy(tuple(players)))
    series_archives = tuple(series)
    expected_indices = tuple(combinations(range(len(normalized_players)), 2))
    if len(series_archives) != len(expected_indices):
        raise ValueError("循环赛必须为每对选手提供恰好一个换色双局赛")

    pairings = tuple(
        RoundRobinPairing(
            pairing_number=pairing_number,
            player_indices=player_indices,
            seed=archive.seed,
            series=archive,
        )
        for pairing_number, (player_indices, archive) in enumerate(
            zip(expected_indices, series_archives), start=1
        )
    )
    points = {descriptor["name"]: 0.0 for descriptor in normalized_players}
    for archive in series_archives:
        for player, point in archive.points.items():
            if player not in points:
                raise ValueError("双局赛 points 包含循环赛之外的选手")
            points[player] += point

    values: dict[str, object] = {
        "schema_version": TOURNAMENT_SCHEMA_VERSION,
        "source": series_archives[0].source,
        "game": series_archives[0].game,
        "seed": seed,
        "players": normalized_players,
        "pairings": pairings,
        "points": points,
        "started_at": series_archives[0].started_at,
        "finished_at": series_archives[-1].finished_at,
    }
    if tournament_id is not None:
        values["tournament_id"] = tournament_id
    return TournamentArchive.model_validate(values)


def _validated_round_robin_inputs(
    game: Game,
    players: Sequence[Player],
    seed: int,
    max_attempts: int,
) -> tuple[tuple[Player, ...], tuple[dict, ...], dict]:
    _validate_signed_seed(seed)
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or not 1 <= max_attempts <= MAX_MOVE_ATTEMPTS
    ):
        raise ValueError(f"max_attempts 必须是 1 到 {MAX_MOVE_ATTEMPTS} 之间的整数")

    entrants = tuple(players)
    count = len(entrants)
    if not MIN_TOURNAMENT_PLAYERS <= count <= MAX_TOURNAMENT_PLAYERS:
        raise ValueError(
            f"循环赛需要 {MIN_TOURNAMENT_PLAYERS} 到 {MAX_TOURNAMENT_PLAYERS} 名选手，"
            f"实际为 {count} 名"
        )
    if len({id(player) for player in entrants}) != count:
        raise ValueError("循环赛不能重复使用同一个 Player 对象")
    if any(
        isinstance(player, HumanPlayer) or player.kind == HumanPlayer.kind for player in entrants
    ):
        raise ValueError("循环赛暂不支持 HumanPlayer")

    descriptors = _normalized_tournament_players(
        tuple(copy.deepcopy(player.describe()) for player in entrants)
    )
    names = [player.name for player in entrants]
    entrant_ids = [player.entrant_id for player in entrants]
    if tuple(descriptor["name"] for descriptor in descriptors) != tuple(names):
        raise ValueError("Player 名字与档案描述不一致")
    if tuple(descriptor["entrant_id"] for descriptor in descriptors) != tuple(entrant_ids):
        raise ValueError("Player entrant_id 与档案描述不一致")
    if not isinstance(game.name, str) or not game.name:
        raise ValueError("循环赛 game 必须是非空字符串")
    game_config = describe_game_config(game)

    schedule = tuple(combinations(range(count), 2))
    for first_index, second_index in schedule:
        validate_players(game, [names[first_index], names[second_index]])
    return entrants, descriptors, copy.deepcopy(game_config)


def prepare_round_robin(
    game: Game,
    players: Sequence[Player],
    seed: int = 0,
    max_attempts: int = 3,
    *,
    tournament_id: str | None = None,
) -> TournamentCheckpoint:
    """Preflight and freeze one tournament identity, configuration, and schedule."""

    _, descriptors, game_config = _validated_round_robin_inputs(game, players, seed, max_attempts)
    created_at = datetime.now(UTC)
    values: dict[str, object] = {
        "schema_version": TOURNAMENT_CHECKPOINT_SCHEMA_VERSION,
        "source": "local_engine",
        "game": game.name,
        "game_config": game_config,
        "seed": seed,
        "max_attempts": max_attempts,
        "players": descriptors,
        "schedule": _round_robin_schedule(descriptors, seed),
        "completed_series": (),
        "created_at": created_at,
        "updated_at": created_at,
    }
    if tournament_id is not None:
        values["tournament_id"] = tournament_id
    return TournamentCheckpoint.model_validate(values)


def checkpoint_with_series(
    checkpoint: TournamentCheckpoint,
    series: SeriesArchive,
) -> TournamentCheckpoint:
    """Return a checkpoint extended by exactly its next scheduled series."""

    if checkpoint.is_complete:
        raise ValueError("循环赛 checkpoint 已完成，不能追加双局赛")
    values: dict[str, object] = {
        field: getattr(checkpoint, field) for field in TournamentCheckpoint.model_fields
    }
    values["completed_series"] = (*checkpoint.completed_series, series)
    values["updated_at"] = series.finished_at
    return TournamentCheckpoint.model_validate(values)


async def resume_round_robin(
    game: Game,
    players: Sequence[Player],
    checkpoint: TournamentCheckpoint,
    *,
    on_event: TournamentEventCallback | None = None,
    on_checkpoint: TournamentCheckpointCallback | None = None,
    on_pairing_start: TournamentPairingStartCallback | None = None,
) -> TournamentArchive:
    """Continue only the unfinished suffix of a validated tournament checkpoint."""

    entrants, descriptors, game_config = _validated_round_robin_inputs(
        game,
        players,
        checkpoint.seed,
        checkpoint.max_attempts,
    )
    if game.name != checkpoint.game:
        raise ValueError("恢复循环赛的 game 与 checkpoint 不一致")
    if descriptors != checkpoint.players:
        raise ValueError("恢复循环赛的选手描述与 checkpoint 不一致")
    if game_config != checkpoint.game_config:
        raise ValueError("恢复循环赛的项目配置与 checkpoint 不一致")
    if _round_robin_schedule(descriptors, checkpoint.seed) != checkpoint.schedule:
        raise ValueError("恢复循环赛的赛程与 checkpoint 不一致")

    current = checkpoint
    for spec in current.schedule[len(current.completed_series) :]:
        if on_pairing_start is not None:
            on_pairing_start(spec)
        first_index, second_index = spec.player_indices
        event_callback: Callable[[int, MatchEvent], None] | None = None
        if on_event is not None:

            def event_callback(
                leg_number: int,
                event: MatchEvent,
                *,
                _pairing_number: int = spec.pairing_number,
            ) -> None:
                on_event(_pairing_number, leg_number, event)

        archive = await play_two_leg_series(
            game,
            [entrants[first_index], entrants[second_index]],
            seed=spec.seed,
            max_attempts=current.max_attempts,
            on_event=event_callback,
        )
        current = checkpoint_with_series(current, archive)
        if on_checkpoint is not None:
            on_checkpoint(current)

    return tournament_from_series(
        current.players,
        current.completed_series,
        seed=current.seed,
        tournament_id=current.tournament_id,
    )


async def play_round_robin(
    game: Game,
    players: Sequence[Player],
    seed: int = 0,
    max_attempts: int = 3,
    on_event: TournamentEventCallback | None = None,
    *,
    tournament_id: str | None = None,
    on_checkpoint: TournamentCheckpointCallback | None = None,
    on_pairing_start: TournamentPairingStartCallback | None = None,
) -> TournamentArchive:
    """Play one swapped-order two-leg series for every entrant pair."""

    checkpoint = prepare_round_robin(
        game,
        players,
        seed=seed,
        max_attempts=max_attempts,
        tournament_id=tournament_id,
    )
    if on_checkpoint is not None:
        on_checkpoint(checkpoint)
    return await resume_round_robin(
        game,
        players,
        checkpoint,
        on_event=on_event,
        on_checkpoint=on_checkpoint,
        on_pairing_start=on_pairing_start,
    )
