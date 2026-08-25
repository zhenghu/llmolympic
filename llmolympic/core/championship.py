"""Single-elimination knockout championship composed of two-leg series.

A championship is a deterministic bracket of single-elimination rounds.  The
entrant count must be a power of two (4, 8 or 16).  Every pairing plays one
swapped-order two-leg series; the winner advances and the loser is eliminated.
Draws are broken deterministically.  The archive reuses the existing two-leg
``SeriesArchive`` so storage, ELO and audit tooling stay composable.

Design decisions (see DESIGN.md §6):
- ``format = "single_elimination_two_leg"``
- ``pairing_policy = "power_of_two_bracket_v1"``
- ``seed_policy = "round_seed_sha256_v1"``
- ``tiebreak_policy = "deterministic_v1"``
"""

from __future__ import annotations

import copy
import hashlib
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from llmolympic.core.archive import normalize_player_descriptors
from llmolympic.core.events import EventType, MatchEvent
from llmolympic.core.game import (
    MAX_PLATFORM_PLAYERS,
    Game,
    describe_game_config,
    validate_players,
)
from llmolympic.core.judge import JudgePanelSnapshot, LLMJudgePanel
from llmolympic.core.match import MAX_MOVE_ATTEMPTS
from llmolympic.core.player import HumanPlayer, Player
from llmolympic.core.series import (
    SERIES_SCHEMA_VERSION,
    SeriesArchive,
    play_two_leg_series,
)

CHAMPIONSHIP_SCHEMA_VERSION = 1
CHAMPIONSHIP_CHECKPOINT_SCHEMA_VERSION = 1

MIN_CHAMPIONSHIP_PLAYERS = 4
MAX_CHAMPIONSHIP_PLAYERS = MAX_PLATFORM_PLAYERS

_SQLITE_INT_MIN = -(2**63)
_SQLITE_INT_MAX = 2**63 - 1
_SEED_DOMAIN = b"llmolympic.championship-round-seed-v1\0"

ChampionshipSource = Literal["local_engine", "external"]
ChampionshipEventCallback = Callable[[int, int, MatchEvent], None]
ChampionshipCheckpointCallback = Callable[["ChampionshipCheckpoint"], None]


def _validate_signed_seed(seed: object) -> int:
    if (
        isinstance(seed, bool)
        or not isinstance(seed, int)
        or not _SQLITE_INT_MIN <= seed <= _SQLITE_INT_MAX
    ):
        raise ValueError("seed 必须是 SQLite signed 64-bit 整数")
    return seed


def validate_championship_player_count(count: int) -> None:
    """A knockout bracket must be a power of two within the platform bounds."""

    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or not MIN_CHAMPIONSHIP_PLAYERS <= count <= MAX_CHAMPIONSHIP_PLAYERS
        or count & (count - 1) != 0
    ):
        raise ValueError(
            "淘汰制锦标赛需要 4、8 或 16 名选手（2 的幂），"
            f"实际为 {count} 名"
        )


def championship_round_count(player_count: int) -> int:
    validate_championship_player_count(player_count)
    return player_count.bit_length() - 1


def championship_round_seed(base_seed: int, round_number: int) -> int:
    """Derive one round-scoped signed 64-bit seed from the tournament seed."""

    _validate_signed_seed(base_seed)
    if isinstance(round_number, bool) or not isinstance(round_number, int) or round_number < 1:
        raise ValueError("round_number 必须是至少为 1 的整数")
    payload = (
        _SEED_DOMAIN
        + str(base_seed).encode("ascii")
        + b"\0"
        + str(round_number).encode("ascii")
    )
    unsigned = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
    return unsigned if unsigned <= _SQLITE_INT_MAX else unsigned - 2**64


def _round_one_indices(player_count: int) -> tuple[tuple[int, int], ...]:
    """Return the seeded opening-round bracket: adjacent input-order pairs.

    Pairing (0, 1), (2, 3), ... keeps every opening-round ``first_index``
    strictly below ``second_index`` and, because each later round pairs adjacent
    upper/lower winners, that invariant holds for every bracket position.
    """

    return tuple(
        (index, index + 1)
        for index in range(0, player_count, 2)
    )


def _round_seeds(
    base_seed: int, round_number: int, pairing_count: int
) -> tuple[int, ...]:
    return tuple(
        championship_round_seed(base_seed, round_number)
        for _ in range(pairing_count)
    )


class ChampionshipPairingSpec(BaseModel):
    """One immutable position in the deterministic knockout bracket.

    ``first_index`` / ``second_index`` are resolved for the opening round and
    ``None`` for later rounds, whose opponents are determined by the previous
    round's winners.
    """

    model_config = ConfigDict(extra="forbid")

    round_number: int = Field(ge=1)
    pairing_number: int = Field(ge=1)
    first_index: int | None = Field(default=None, ge=0)
    second_index: int | None = Field(default=None, ge=0)
    seed: int

    @field_validator("round_number", "pairing_number", "first_index", "second_index")
    @classmethod
    def validate_ints(cls, value: object) -> object:
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("赛程字段必须是整数")
        return value

    @field_validator("seed", mode="before")
    @classmethod
    def validate_seed(cls, value: object) -> object:
        return _validate_signed_seed(value)

    @model_validator(mode="after")
    def validate_indices(self) -> ChampionshipPairingSpec:
        if (self.first_index is None) != (self.second_index is None):
            raise ValueError("赛程配对的两个席位必须同时已知或同时待定")
        if (
            self.first_index is not None
            and self.second_index is not None
            and self.first_index >= self.second_index
        ):
            raise ValueError("赛程配对的 first_index 必须严格小于 second_index")
        return self


class ChampionshipPairing(ChampionshipPairingSpec):
    """One scheduled entrant pair and its complete swapped-order series."""

    series: SeriesArchive


def _normalized_championship_players(descriptors: object) -> tuple[dict, ...]:
    normalized = tuple(normalize_player_descriptors(descriptors, legacy=False))
    count = len(normalized)
    validate_championship_player_count(count)

    names = [descriptor["name"] for descriptor in normalized]
    entrant_ids = [descriptor["entrant_id"] for descriptor in normalized]
    if len(set(names)) != count:
        raise ValueError(f"锦标赛选手名字必须唯一: {names}")
    if len(set(entrant_ids)) != count:
        raise ValueError(f"锦标赛选手 entrant_id 必须唯一: {entrant_ids}")
    for descriptor in normalized:
        kind = descriptor.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ValueError("锦标赛选手描述必须包含非空 kind")
        if kind == HumanPlayer.kind:
            raise ValueError("锦标赛暂不支持 HumanPlayer")
    return normalized


def _static_schedule(
    players: Sequence[dict],
    seed: int,
) -> tuple[ChampionshipPairingSpec, ...]:
    """Build the static bracket: round-1 indices known, later rounds pending.

    ``pairing_number`` is globally unique across the whole bracket (1..N in
    execution order), matching the storage primary key and the round-robin
    tournament convention.
    """

    count = len(players)
    rounds = championship_round_count(count)
    specs: list[ChampionshipPairingSpec] = []
    pairing_number = 0
    for round_number in range(1, rounds + 1):
        pairing_count = count >> round_number
        seeds = _round_seeds(seed, round_number, pairing_count)
        if round_number == 1:
            indices = _round_one_indices(count)
            for (first, second), round_seed in zip(indices, seeds):
                pairing_number += 1
                specs.append(
                    ChampionshipPairingSpec(
                        round_number=round_number,
                        pairing_number=pairing_number,
                        first_index=first,
                        second_index=second,
                        seed=round_seed,
                    )
                )
        else:
            for round_seed in seeds:
                pairing_number += 1
                specs.append(
                    ChampionshipPairingSpec(
                        round_number=round_number,
                        pairing_number=pairing_number,
                        first_index=None,
                        second_index=None,
                        seed=round_seed,
                    )
                )
    return tuple(specs)


def _series_winner_indices(
    players: Sequence[dict],
    series: SeriesArchive,
    first_index: int,
    second_index: int,
) -> tuple[int, int]:
    """Return ``(winner_index, loser_index)`` using deterministic tie-breaks.

    The two-leg series points decide first.  Ties fall back to fewer technical
    losses, then to the stable ``entrant_id`` ordering, so a championship can
    never deadlock.
    """

    first_name = players[first_index]["name"]
    second_name = players[second_index]["name"]
    first_points = series.points[first_name]
    second_points = series.points[second_name]
    if first_points > second_points:
        return first_index, second_index
    if second_points > first_points:
        return second_index, first_index

    first_tech = series.standings[first_name].technical_losses
    second_tech = series.standings[second_name].technical_losses
    if first_tech != second_tech:
        if first_tech < second_tech:
            return first_index, second_index
        return second_index, first_index

    first_id = players[first_index]["entrant_id"]
    second_id = players[second_index]["entrant_id"]
    if first_id < second_id:
        return first_index, second_index
    return second_index, first_index


def _resolved_bracket_indices(
    players: Sequence[dict],
    series_archives: Sequence[SeriesArchive],
) -> tuple[tuple[int, int], ...]:
    """Replay the bracket to recover every pairing's entrant indices.

    The opening round uses the seeded order; each later round pairs adjacent
    winners of the previous round in bracket order.
    """

    count = len(players)
    rounds = championship_round_count(count)
    expected_total = count - 1
    if len(series_archives) != expected_total:
        raise ValueError(
            f"锦标赛需要恰好 {expected_total} 个双局赛，实际为 {len(series_archives)}"
        )

    resolved: list[tuple[int, int]] = []
    winners: list[int] = list(range(count))
    cursor = 0
    for round_number in range(1, rounds + 1):
        pairing_count = count >> round_number
        round_winners: list[int] = []
        for position in range(pairing_count):
            first_index = winners[2 * position]
            second_index = winners[2 * position + 1]
            resolved.append((first_index, second_index))
            series = series_archives[cursor]
            cursor += 1
            winner_index, _ = _series_winner_indices(
                players, series, first_index, second_index
            )
            round_winners.append(winner_index)
        winners = round_winners
    if len(winners) != 1:
        raise ValueError("锦标赛未能收敛到唯一的冠军")
    return tuple(resolved)


def _bracket_indices_for_prefix(
    players: Sequence[dict],
    completed_series: Sequence[SeriesArchive],
) -> tuple[tuple[int, int], ...]:
    """Return the resolved entrant indices for a valid completed-series prefix.

    A championship checkpoint stores completed series in the same canonical
    execution order as a full archive.  For any prefix whose length is a
    multiple of a whole round boundary, replaying the bracket recovers each
    stored series' entrant indices without knowing future results.
    """

    count = len(players)
    completed = len(completed_series)
    if completed == 0:
        return ()
    expected_total = count - 1
    if completed > expected_total:
        raise ValueError(
            f"锦标赛最多包含 {expected_total} 个双局赛，实际已保存 {completed} 个"
        )

    resolved: list[tuple[int, int]] = []
    winners: list[int] = list(range(count))
    cursor = 0
    remaining = completed
    for round_number in range(1, championship_round_count(count) + 1):
        pairing_count = count >> round_number
        round_winners: list[int] = []
        for position in range(pairing_count):
            first_index = winners[2 * position]
            second_index = winners[2 * position + 1]
            if remaining <= 0:
                return tuple(resolved)
            resolved.append((first_index, second_index))
            series = completed_series[cursor]
            cursor += 1
            remaining -= 1
            winner_index, _ = _series_winner_indices(
                players, series, first_index, second_index
            )
            round_winners.append(winner_index)
        winners = round_winners
    if remaining != 0:
        raise ValueError("锦标赛 checkpoint 的已保存双局赛数量无效")
    return tuple(resolved)


def _champion_index(
    players: Sequence[dict],
    series_archives: Sequence[SeriesArchive],
) -> int:
    indices = _resolved_bracket_indices(players, series_archives)
    first, second = indices[-1]
    return _series_winner_indices(players, series_archives[-1], first, second)[0]


def _series_game_config(series: SeriesArchive) -> dict:
    if not series.legs:
        raise ValueError("锦标赛双局赛必须包含两局档案")
    if not series.legs[0].events:
        raise ValueError("锦标赛双局赛的对局必须包含事件流")
    first_event = series.legs[0].events[0]
    if first_event.type != EventType.MATCH_STARTED:
        raise ValueError("锦标赛双局赛必须以 match_started 事件开始")
    config = first_event.data.get("game_config")
    if not isinstance(config, dict):
        raise TypeError("锦标赛双局赛必须记录项目配置")
    return config


def _validate_completed_series(
    *,
    players: Sequence[dict],
    series_archives: Sequence[SeriesArchive],
    indices: Sequence[tuple[int, int]],
    source: ChampionshipSource,
    game: str,
    judge_panel: JudgePanelSnapshot | None,
) -> None:
    """Validate the canonical completed series list and its bracket structure."""

    if len(series_archives) != len(indices):
        raise ValueError("锦标赛已完成双局赛数量必须与赛程一致")

    series_ids: set[str] = set()
    match_ids: set[str] = set()
    validated_config: dict | None = None
    previous_finished_at: datetime | None = None

    requires_panel = game == "creative_writing"
    if requires_panel and judge_panel is None:
        raise ValueError("创意锦标赛必须冻结评审团快照")
    if not requires_panel and judge_panel is not None:
        raise ValueError("客观锦标赛不能包含评审团快照")

    for (first_index, second_index), series in zip(indices, series_archives):
        if series.schema_version != SERIES_SCHEMA_VERSION:
            raise ValueError("锦标赛只接受 schema v2 双局赛档案")
        if series.source != source:
            raise ValueError("锦标赛与双局赛档案来源必须一致")
        if series.game != game:
            raise ValueError("锦标赛与双局赛项目必须一致")
        if series.judge_panel != judge_panel:
            raise ValueError("锦标赛双局赛的评审团与赛事冻结快照不一致")

        expected_players = (players[first_index], players[second_index])
        if series.players != expected_players:
            raise ValueError("锦标赛双局赛选手必须与配对索引及身份完全一致")

        current_config = _series_game_config(series)
        if validated_config is None:
            validated_config = current_config
        elif current_config != validated_config:
            raise ValueError("锦标赛所有双局赛必须使用完全相同的项目配置")

        if series.series_id in series_ids:
            raise ValueError("锦标赛中的 series_id 必须全局唯一")
        series_ids.add(series.series_id)
        for leg in series.legs:
            if leg.match_id in match_ids:
                raise ValueError("锦标赛中的 match_id 必须全局唯一")
            match_ids.add(leg.match_id)

        if previous_finished_at is not None and series.started_at < previous_finished_at:
            raise ValueError("锦标赛双局赛时间不能重叠或逆序")
        previous_finished_at = series.finished_at


@dataclass(frozen=True)
class ChampionshipStanding:
    """One entrant's aggregate result, ordered by :attr:`ChampionshipArchive.standings`."""

    player: str
    entrant_id: str
    rank: int
    series_played: int
    series_wins: int
    series_draws: int
    series_losses: int
    games_played: int
    wins: int
    draws: int
    losses: int
    technical_losses: int


class ChampionshipArchive(BaseModel):
    """A complete single-elimination knockout championship archive."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = CHAMPIONSHIP_SCHEMA_VERSION
    source: ChampionshipSource = "external"
    championship_id: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1)
    format: Literal["single_elimination_two_leg"] = "single_elimination_two_leg"
    pairing_policy: Literal["power_of_two_bracket_v1"] = "power_of_two_bracket_v1"
    seed_policy: Literal["round_seed_sha256_v1"] = "round_seed_sha256_v1"
    tiebreak_policy: Literal["deterministic_v1"] = "deterministic_v1"
    game: str
    seed: int
    players: tuple[dict, ...]
    pairings: tuple[ChampionshipPairing, ...]
    champion: str
    started_at: datetime
    finished_at: datetime
    judge_panel: JudgePanelSnapshot | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_players(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        normalized["players"] = _normalized_championship_players(normalized.get("players"))
        return normalized

    @field_validator("game", mode="before")
    @classmethod
    def validate_game(cls, value: object) -> object:
        if not isinstance(value, str) or not value:
            raise ValueError("锦标赛 game 必须是非空字符串")
        return value

    @field_validator("seed", mode="before")
    @classmethod
    def validate_seed(cls, value: object) -> object:
        return _validate_signed_seed(value)

    @model_validator(mode="after")
    def validate_consistency(self) -> ChampionshipArchive:
        players = self.players
        count = len(players)
        validate_championship_player_count(count)
        static_schedule = _static_schedule(players, self.seed)

        if len(self.pairings) != len(static_schedule):
            raise ValueError("锦标赛配对数量与赛程不一致")
        seen: set[tuple[int, int]] = set()
        for pairing, spec in zip(self.pairings, static_schedule):
            key = (pairing.round_number, pairing.pairing_number)
            if key in seen:
                raise ValueError("锦标赛配对轮次与编号必须唯一")
            seen.add(key)
            if (
                pairing.round_number != spec.round_number
                or pairing.pairing_number != spec.pairing_number
                or pairing.seed != spec.seed
            ):
                raise ValueError("锦标赛配对规格与确定性赛程不一致")
            if spec.first_index is not None and (
                pairing.first_index != spec.first_index
                or pairing.second_index != spec.second_index
            ):
                raise ValueError("锦标赛首轮配对与种子顺序不一致")

        series_archives = tuple(pairing.series for pairing in self.pairings)
        resolved_indices = _resolved_bracket_indices(players, series_archives)
        if tuple(
            (pairing.first_index, pairing.second_index) for pairing in self.pairings
        ) != resolved_indices:
            raise ValueError("锦标赛配对选手与淘汰晋级结果不一致")

        _validate_completed_series(
            players=players,
            series_archives=series_archives,
            indices=resolved_indices,
            source=self.source,
            game=self.game,
            judge_panel=self.judge_panel,
        )

        champion_index = _champion_index(players, series_archives)
        if self.champion != players[champion_index]["name"]:
            raise ValueError("锦标赛 champion 与淘汰结果不一致")

        timestamps = (self.started_at, self.finished_at)
        if any(timestamp.utcoffset() is None for timestamp in timestamps):
            raise ValueError("锦标赛开始和结束时间必须包含时区")
        if self.finished_at < self.started_at:
            raise ValueError("锦标赛结束时间不能早于开始时间")
        if (
            self.started_at != series_archives[0].started_at
            or self.finished_at != series_archives[-1].finished_at
        ):
            raise ValueError("锦标赛时间范围必须与首尾双局赛边界一致")
        return self

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)

    @property
    def standings(self) -> tuple[ChampionshipStanding, ...]:
        """Return deterministic aggregate standings (champion first)."""

        players = self.players
        series_archives = tuple(pairing.series for pairing in self.pairings)
        count = len(players)
        rounds = championship_round_count(count)

        aggregates: dict[str, dict[str, int | str]] = {}
        for descriptor in players:
            aggregates[descriptor["name"]] = {
                "player": descriptor["name"],
                "entrant_id": descriptor["entrant_id"],
                "rank": 0,
                "series_played": 0,
                "series_wins": 0,
                "series_draws": 0,
                "series_losses": 0,
                "games_played": 0,
                "wins": 0,
                "draws": 0,
                "losses": 0,
                "technical_losses": 0,
            }

        winners: list[int] = list(range(count))
        cursor = 0
        for round_number in range(1, rounds + 1):
            pairing_count = count >> round_number
            round_winners: list[int] = []
            for position in range(pairing_count):
                first_index = winners[2 * position]
                second_index = winners[2 * position + 1]
                series = series_archives[cursor]
                cursor += 1
                first_name = players[first_index]["name"]
                second_name = players[second_index]["name"]
                winner_index, loser_index = _series_winner_indices(
                    players, series, first_index, second_index
                )
                loser_name = players[loser_index]["name"]
                aggregates[loser_name]["rank"] = (count >> round_number) + 1
                round_winners.append(winner_index)
                if len(series.legs) != 2:
                    raise ValueError("锦标赛双局赛必须恰好包含两局")
                for name, opponent in (
                    (first_name, second_name),
                    (second_name, first_name),
                ):
                    standing = series.standings[name]
                    agg = aggregates[name]
                    agg["series_played"] += 1
                    agg["games_played"] += len(series.legs)
                    agg["wins"] += standing.wins
                    agg["draws"] += standing.draws
                    agg["losses"] += standing.losses
                    agg["technical_losses"] += standing.technical_losses
                    if series.points[name] > series.points[opponent]:
                        agg["series_wins"] += 1
                    elif series.points[name] < series.points[opponent]:
                        agg["series_losses"] += 1
                    else:
                        agg["series_draws"] += 1
            winners = round_winners
        if len(winners) != 1:
            raise ValueError("锦标赛未能收敛到唯一的冠军")
        aggregates[players[winners[0]]["name"]]["rank"] = 1

        standings = tuple(
            ChampionshipStanding(**values) for values in aggregates.values()
        )
        return tuple(
            sorted(
                standings,
                key=lambda standing: (standing.rank, standing.entrant_id),
            )
        )


def championship_from_series(
    players: Sequence[dict],
    series: Sequence[SeriesArchive],
    *,
    seed: int,
    champion: str,
    championship_id: str | None = None,
    judge_panel: JudgePanelSnapshot | None = None,
) -> ChampionshipArchive:
    """Build a championship archive from series in bracket order."""

    _validate_signed_seed(seed)
    normalized_players = _normalized_championship_players(copy.deepcopy(tuple(players)))
    series_archives = tuple(series)

    indices = _resolved_bracket_indices(normalized_players, series_archives)
    static_schedule = _static_schedule(normalized_players, seed)

    pairings = tuple(
        ChampionshipPairing(
            round_number=spec.round_number,
            pairing_number=spec.pairing_number,
            first_index=first,
            second_index=second,
            seed=spec.seed,
            series=archive,
        )
        for spec, (first, second), archive in zip(
            static_schedule, indices, series_archives
        )
    )

    values: dict[str, object] = {
        "schema_version": CHAMPIONSHIP_SCHEMA_VERSION,
        "source": series_archives[0].source,
        "game": series_archives[0].game,
        "seed": seed,
        "players": normalized_players,
        "pairings": pairings,
        "champion": champion,
        "started_at": series_archives[0].started_at,
        "finished_at": series_archives[-1].finished_at,
        "judge_panel": judge_panel,
    }
    if championship_id is not None:
        values["championship_id"] = championship_id
    return ChampionshipArchive.model_validate(values)


class ChampionshipCheckpoint(BaseModel):
    """A validated, resumable prefix of one local knockout championship.

    The completed series are stored in canonical bracket execution order, so the
    next pairing is always ``len(completed_series)`` in the static schedule.
    Only whole round boundaries are persisted: a checkpoint is either empty or
    contains every series of the rounds that have fully resolved.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = CHAMPIONSHIP_CHECKPOINT_SCHEMA_VERSION
    source: Literal["local_engine"] = "local_engine"
    championship_id: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1)
    format: Literal["single_elimination_two_leg"] = "single_elimination_two_leg"
    pairing_policy: Literal["power_of_two_bracket_v1"] = "power_of_two_bracket_v1"
    seed_policy: Literal["round_seed_sha256_v1"] = "round_seed_sha256_v1"
    tiebreak_policy: Literal["deterministic_v1"] = "deterministic_v1"
    game: str
    game_config: dict
    seed: int
    max_attempts: int
    players: tuple[dict, ...]
    schedule: tuple[ChampionshipPairingSpec, ...]
    judge_panel: JudgePanelSnapshot | None = None
    completed_series: tuple[SeriesArchive, ...] = ()
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="before")
    @classmethod
    def normalize_players(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        normalized["players"] = _normalized_championship_players(normalized.get("players"))
        game_config = normalized.get("game_config")
        if not isinstance(game_config, dict):
            raise TypeError("锦标赛 checkpoint game_config 必须是字典")
        normalized["game_config"] = copy.deepcopy(game_config)
        return normalized

    @field_validator("game", mode="before")
    @classmethod
    def validate_game(cls, value: object) -> object:
        if not isinstance(value, str) or not value:
            raise ValueError("锦标赛 game 必须是非空字符串")
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
    def validate_consistency(self) -> ChampionshipCheckpoint:
        count = len(self.players)
        validate_championship_player_count(count)
        static_schedule = _static_schedule(self.players, self.seed)
        if self.schedule != static_schedule:
            raise ValueError("锦标赛 checkpoint 赛程与确定性赛程不一致")

        completed = len(self.completed_series)
        indices = _bracket_indices_for_prefix(self.players, self.completed_series)
        if len(indices) != completed:
            raise ValueError("锦标赛 checkpoint 的已保存双局赛索引无效")

        requires_panel = self.game == "creative_writing"
        if requires_panel and self.judge_panel is None:
            raise ValueError("创意锦标赛 checkpoint 必须冻结评审团快照")
        if not requires_panel and self.judge_panel is not None:
            raise ValueError("客观锦标赛 checkpoint 不能包含评审团快照")

        _validate_completed_series(
            players=self.players,
            series_archives=self.completed_series,
            indices=indices,
            source=self.source,
            game=self.game,
            judge_panel=self.judge_panel,
        )

        timestamps = (self.created_at, self.updated_at)
        if any(timestamp.utcoffset() is None for timestamp in timestamps):
            raise ValueError("锦标赛 checkpoint 时间必须包含时区")
        if self.updated_at < self.created_at:
            raise ValueError("锦标赛 checkpoint 更新时间不能早于创建时间")
        if self.completed_series:
            if self.created_at > self.completed_series[0].started_at:
                raise ValueError("锦标赛 checkpoint 创建时间不能晚于首组开始时间")
            if self.updated_at != self.completed_series[-1].finished_at:
                raise ValueError("锦标赛 checkpoint 更新时间必须等于最后完成组的结束时间")
        elif self.updated_at != self.created_at:
            raise ValueError("空锦标赛 checkpoint 的创建和更新时间必须一致")
        return self

    @property
    def is_complete(self) -> bool:
        return len(self.completed_series) == len(self.players) - 1

    @property
    def completed_rounds(self) -> int:
        """Return the number of fully resolved rounds in this checkpoint."""

        count = len(self.players)
        rounds = championship_round_count(count)
        completed = len(self.completed_series)
        for round_number in range(1, rounds + 1):
            if completed == count - (count >> round_number):
                return round_number
        return 0

    @property
    def next_pairing_number(self) -> int | None:
        if self.is_complete:
            return None
        return len(self.completed_series) + 1


def _validated_championship_inputs(
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
    validate_championship_player_count(count)
    if len({id(player) for player in entrants}) != count:
        raise ValueError("锦标赛不能重复使用同一个 Player 对象")
    if any(
        isinstance(player, HumanPlayer) or player.kind == HumanPlayer.kind for player in entrants
    ):
        raise ValueError("锦标赛暂不支持 HumanPlayer")

    descriptors = _normalized_championship_players(
        tuple(copy.deepcopy(player.describe()) for player in entrants)
    )
    names = [player.name for player in entrants]
    entrant_ids = [player.entrant_id for player in entrants]
    if tuple(descriptor["name"] for descriptor in descriptors) != tuple(names):
        raise ValueError("Player 名字与档案描述不一致")
    if tuple(descriptor["entrant_id"] for descriptor in descriptors) != tuple(entrant_ids):
        raise ValueError("Player entrant_id 与档案描述不一致")
    if not isinstance(game.name, str) or not game.name:
        raise ValueError("锦标赛 game 必须是非空字符串")
    game_config = describe_game_config(game)

    schedule = _static_schedule(descriptors, seed)
    for spec in schedule:
        if spec.first_index is not None:
            validate_players(game, [names[spec.first_index], names[spec.second_index]])
    return entrants, descriptors, copy.deepcopy(game_config)


def prepare_championship(
    game: Game,
    players: Sequence[Player],
    seed: int = 0,
    max_attempts: int = 3,
    *,
    championship_id: str | None = None,
    judge_panel: LLMJudgePanel | None = None,
) -> ChampionshipCheckpoint:
    """Preflight and freeze one championship identity, configuration, and bracket."""

    entrants, descriptors, game_config = _validated_championship_inputs(
        game, players, seed, max_attempts
    )
    requires_panel = bool(getattr(game, "requires_judge_panel", False))
    if requires_panel and judge_panel is None:
        raise ValueError(f"项目 {game.name!r} 需要 LLM 评审团")
    if not requires_panel and judge_panel is not None:
        raise ValueError(f"项目 {game.name!r} 不接受 LLM 评审团")
    if judge_panel is not None:
        judge_panel.validate_contestants(list(entrants))
    created_at = datetime.now(UTC)
    values: dict[str, object] = {
        "schema_version": CHAMPIONSHIP_CHECKPOINT_SCHEMA_VERSION,
        "source": "local_engine",
        "game": game.name,
        "game_config": game_config,
        "seed": seed,
        "max_attempts": max_attempts,
        "players": descriptors,
        "schedule": _static_schedule(descriptors, seed),
        "judge_panel": None if judge_panel is None else judge_panel.snapshot(),
        "completed_series": (),
        "created_at": created_at,
        "updated_at": created_at,
    }
    if championship_id is not None:
        values["championship_id"] = championship_id
    return ChampionshipCheckpoint.model_validate(values)


async def resume_championship(
    game: Game,
    players: Sequence[Player],
    checkpoint: ChampionshipCheckpoint,
    *,
    on_event: ChampionshipEventCallback | None = None,
    on_checkpoint: ChampionshipCheckpointCallback | None = None,
    judge_panel: LLMJudgePanel | None = None,
) -> ChampionshipArchive:
    """Continue only the unfinished rounds of a validated championship checkpoint."""

    entrants, descriptors, game_config = _validated_championship_inputs(
        game,
        players,
        checkpoint.seed,
        checkpoint.max_attempts,
    )
    if game.name != checkpoint.game:
        raise ValueError("恢复锦标赛的 game 与 checkpoint 不一致")
    if descriptors != checkpoint.players:
        raise ValueError("恢复锦标赛的选手描述与 checkpoint 不一致")
    if game_config != checkpoint.game_config:
        raise ValueError("恢复锦标赛的项目配置与 checkpoint 不一致")
    if _static_schedule(descriptors, checkpoint.seed) != checkpoint.schedule:
        raise ValueError("恢复锦标赛的赛程与 checkpoint 不一致")
    requires_panel = bool(getattr(game, "requires_judge_panel", False))
    if requires_panel and judge_panel is None:
        raise ValueError(f"项目 {game.name!r} 需要 LLM 评审团")
    if not requires_panel and judge_panel is not None:
        raise ValueError(f"项目 {game.name!r} 不接受 LLM 评审团")
    if judge_panel is not None:
        judge_panel.validate_contestants(list(entrants))
    runtime_snapshot = None if judge_panel is None else judge_panel.snapshot()
    if runtime_snapshot != checkpoint.judge_panel:
        raise ValueError("恢复锦标赛的评审团与 checkpoint 冻结快照不一致")

    count = len(entrants)
    rounds = championship_round_count(count)
    winners: list[int] = list(range(count))
    series_archives: list[SeriesArchive] = list(checkpoint.completed_series)
    pairing_number = len(series_archives)

    # Replay completed rounds to re-establish the bracket's surviving entrants.
    cursor = 0
    for round_number in range(1, checkpoint.completed_rounds + 1):
        pairing_count = count >> round_number
        round_winners: list[int] = []
        for _position in range(pairing_count):
            first_index = winners[0]
            second_index = winners[1]
            series = series_archives[cursor]
            cursor += 1
            winner_index, _ = _series_winner_indices(
                descriptors, series, first_index, second_index
            )
            round_winners.append(winner_index)
            winners = winners[2:]
        winners = round_winners

    for round_number in range(checkpoint.completed_rounds + 1, rounds + 1):
        pairing_count = count >> round_number
        round_winners: list[int] = []
        round_series: list[SeriesArchive] = []
        for position in range(pairing_count):
            first_index = winners[2 * position]
            second_index = winners[2 * position + 1]
            pairing_number += 1

            event_callback: Callable[[int, MatchEvent], None] | None = None
            if on_event is not None:

                def event_callback(
                    leg_number: int,
                    event: MatchEvent,
                    *,
                    _pairing_number: int = pairing_number,
                ) -> None:
                    on_event(_pairing_number, leg_number, event)

            archive = await play_two_leg_series(
                game,
                [entrants[first_index], entrants[second_index]],
                seed=championship_round_seed(checkpoint.seed, round_number),
                max_attempts=checkpoint.max_attempts,
                on_event=event_callback,
                judge_panel=judge_panel,
            )
            round_series.append(archive)
            winner_index, _ = _series_winner_indices(
                descriptors, archive, first_index, second_index
            )
            round_winners.append(winner_index)
        series_archives.extend(round_series)
        winners = round_winners
        current = championship_checkpoint_with_series(
            checkpoint,
            series_archives,
        )
        checkpoint = current
        if on_checkpoint is not None:
            on_checkpoint(current)

    if len(winners) != 1:
        raise ValueError("锦标赛未能收敛到唯一的冠军")
    champion = descriptors[winners[0]]["name"]
    return championship_from_series(
        descriptors,
        series_archives,
        seed=checkpoint.seed,
        champion=champion,
        championship_id=checkpoint.championship_id,
        judge_panel=checkpoint.judge_panel,
    )


def championship_checkpoint_with_series(
    checkpoint: ChampionshipCheckpoint,
    completed_series: Sequence[SeriesArchive],
) -> ChampionshipCheckpoint:
    """Return a checkpoint extended to a valid new completed-series prefix."""

    if not completed_series:
        raise ValueError("锦标赛 checkpoint 不能追加空的双局赛列表")
    if len(completed_series) <= len(checkpoint.completed_series):
        raise ValueError("锦标赛 checkpoint 只能追加新的双局赛")
    if tuple(completed_series[: len(checkpoint.completed_series)]) != checkpoint.completed_series:
        raise ValueError("锦标赛 checkpoint 只能保留既有 prefix 并追加新双局赛")
    values: dict[str, object] = {
        field: getattr(checkpoint, field) for field in ChampionshipCheckpoint.model_fields
    }
    values["completed_series"] = tuple(completed_series)
    values["updated_at"] = completed_series[-1].finished_at
    return ChampionshipCheckpoint.model_validate(values)


async def play_championship(
    game: Game,
    players: Sequence[Player],
    seed: int = 0,
    max_attempts: int = 3,
    on_event: ChampionshipEventCallback | None = None,
    *,
    championship_id: str | None = None,
    judge_panel: LLMJudgePanel | None = None,
    on_checkpoint: ChampionshipCheckpointCallback | None = None,
) -> ChampionshipArchive:
    """Play one single-elimination knockout bracket of swapped-order series.

    If ``on_checkpoint`` is supplied, it is invoked after each whole round is
    resolved with an up-to-date :class:`ChampionshipCheckpoint`.  The
    ``championship_id`` anchors the checkpoint identity so a consumer can
    persist progress between rounds.
    """

    checkpoint = prepare_championship(
        game,
        players,
        seed=seed,
        max_attempts=max_attempts,
        championship_id=championship_id,
        judge_panel=judge_panel,
    )
    return await resume_championship(
        game,
        players,
        checkpoint,
        on_event=on_event,
        on_checkpoint=on_checkpoint,
        judge_panel=judge_panel,
    )
