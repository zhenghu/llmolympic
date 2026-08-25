"""Disclosure-safe data-transfer objects for the local observer API.

The persisted archive is intentionally richer than the public Web surface.  In
particular, player descriptors and judge failures contain routing metadata that
must never be copied into an HTTP or WebSocket response.  This module therefore
builds every response from an explicit allow-list instead of recursively
serializing storage models.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from llmolympic.core.archive import MatchArchive
from llmolympic.core.events import EventType, MatchEvent
from llmolympic.core.game import MAX_PLATFORM_PLAYERS
from llmolympic.core.storage import MatchSummary as StorageMatchSummary
from llmolympic.core.storage import RatingEntry

API_VERSION = "v1"
_MAX_PUBLIC_NESTING = 16
_DROP = object()
_SAFE_PUBLIC_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_BIDI_CONTROL_CHARACTERS = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)
_PUBLIC_GAME_CONFIG_KEYS = frozenset(
    {
        "bank_version",
        "board_size",
        "criteria",
        "draw_policy",
        "generator_version",
        "initial_fen",
        "max_submission_chars",
        "min_submission_chars",
        "notations",
        "rounds",
        "rubric_version",
        "rules",
        "rules_engine",
        "rules_engine_version",
        "source",
        "task_bank_version",
        "variant",
        "win_length",
    }
)


class _PublicModel(BaseModel):
    """Common fail-closed configuration for Web-facing models."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def _valid_public_player_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 512
        and not any(
            ord(character) < 32
            or 127 <= ord(character) <= 159
            or 0xD800 <= ord(character) <= 0xDFFF
            or character in _BIDI_CONTROL_CHARACTERS
            for character in value
        )
    )


def _utc_json(value: datetime) -> str:
    """Render every timestamp in one stable UTC representation."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


_SENSITIVE_EXACT_KEYS = frozenset(
    {
        "accesstoken",
        "apikey",
        "auth",
        "authorization",
        "authtoken",
        "baseurl",
        "bearertoken",
        "clientsecret",
        "credential",
        "credentials",
        "endpoint",
        "entrantid",
        "failuredetails",
        "header",
        "headers",
        "key",
        "model",
        "password",
        "profile",
        "provider",
        "refreshtoken",
        "routeid",
        "secret",
        "secretkey",
        "serverurl",
        "token",
    }
)
_SENSITIVE_IDENTITY_AFFIXES = (
    "endpoint",
    "entrantid",
    "failuredetails",
    "header",
    "headers",
    "model",
    "profile",
    "provider",
    "routeid",
)
_SENSITIVE_SECRET_SUFFIXES = (
    "accesstoken",
    "apikey",
    "authheader",
    "authtoken",
    "bearertoken",
    "clientsecret",
    "password",
    "refreshtoken",
    "secretkey",
)


def _normalized_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.casefold())


def _is_sensitive_key(key: str) -> bool:
    normalized = _normalized_key(key)
    if normalized in _SENSITIVE_EXACT_KEYS:
        return True
    if normalized.endswith(_SENSITIVE_SECRET_SUFFIXES):
        return True
    return any(
        normalized.startswith(affix) or normalized.endswith(affix)
        for affix in _SENSITIVE_IDENTITY_AFFIXES
    )


def _sanitize_public_value(value: object, *, depth: int = 0) -> object:
    """Copy JSON-like data while removing sensitive keys at every depth."""

    if depth > _MAX_PUBLIC_NESTING:
        return _DROP
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else _DROP
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or _is_sensitive_key(key):
                continue
            copied = _sanitize_public_value(item, depth=depth + 1)
            if copied is not _DROP:
                result[key] = copied
        return result if result or not value else _DROP
    if isinstance(value, (list, tuple)):
        result_list: list[object] = []
        for item in value:
            copied = _sanitize_public_value(item, depth=depth + 1)
            if copied is not _DROP:
                result_list.append(copied)
        return result_list if result_list or not value else _DROP
    return _DROP


def _display_names(descriptors: object) -> tuple[str, ...]:
    """Extract names without ever stringifying a full player descriptor."""

    if not isinstance(descriptors, (list, tuple)):
        return ()
    names: list[str] = []
    for descriptor in descriptors:
        if isinstance(descriptor, str):
            names.append(descriptor)
            continue
        if not isinstance(descriptor, Mapping):
            continue
        display_name = descriptor.get("display_name")
        if not isinstance(display_name, str):
            display_name = descriptor.get("name")
        if isinstance(display_name, str):
            names.append(display_name)
    return tuple(names)


def _public_scores(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise TypeError("scores must be an object")
    result: dict[str, float] = {}
    for player, score in value.items():
        if (
            not isinstance(player, str)
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
        ):
            raise TypeError("scores must map player names to numbers")
        numeric = float(score)
        if not math.isfinite(numeric):
            raise ValueError("scores must be finite")
        result[player] = numeric
    return result


def _storage_field(source: object, field: str, default: object = _DROP) -> object:
    """Read one named field without copying the remainder of a storage object."""

    if isinstance(source, Mapping):
        if field in source:
            return source[field]
    elif hasattr(source, field):
        return getattr(source, field)
    if default is not _DROP:
        return default
    raise TypeError(f"storage value is missing {field}")


def _public_player_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise TypeError("players must be a list of display names")
    return tuple(value)


class HealthResponse(_PublicModel):
    api_version: Literal["v1"] = API_VERSION
    status: Literal["ok", "degraded"]
    service_version: str
    database_available: bool
    database_schema_version: int | None = None


class ErrorDetail(_PublicModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")


class ErrorResponse(_PublicModel):
    error: ErrorDetail


GameMode: TypeAlias = Literal["play", "series", "round_robin", "championship"]
LiveMatchStatus: TypeAlias = Literal["running", "completed", "interrupted"]
LiveFinalKind: TypeAlias = Literal["match", "series", "tournament", "championship"]
ParticipationStatus: TypeAlias = Literal["active", "completed", "interrupted", "expired"]
ParticipationRequestState: TypeAlias = Literal[
    "pending",
    "submitted",
    "consumed",
    "accepted",
    "rejected",
    "expired",
    "cancelled",
]
_MODE_FINAL_KIND: dict[GameMode, LiveFinalKind] = {
    "play": "match",
    "series": "series",
    "round_robin": "tournament",
    "championship": "championship",
}


class GameInfo(_PublicModel):
    name: str
    supported_modes: tuple[GameMode, ...]
    requires_judge_panel: bool = False

    @classmethod
    def from_game(cls, name: str, game_class: type) -> GameInfo:
        modes = set(
            getattr(game_class, "supported_modes", ("play", "series", "round_robin"))
        )
        # A championship composes the ordinary two-player play capability into
        # a bracket; it is intentionally not a new core Game mode.
        if "play" in modes:
            modes.add("championship")
        return cls(
            name=name,
            supported_modes=tuple(sorted(modes)),
            requires_judge_panel=bool(getattr(game_class, "requires_judge_panel", False)),
        )


class GameListResponse(_PublicModel):
    api_version: Literal["v1"] = API_VERSION
    games: tuple[GameInfo, ...]


class MatchSummary(_PublicModel):
    match_id: str
    game: str
    seed: int
    players: tuple[str, ...]
    scores: dict[str, float]
    started_at: datetime
    finished_at: datetime
    rated: bool = False
    series_id: str | None = None
    leg_number: int | None = None
    tournament_id: str | None = None
    pairing_number: int | None = None
    pairing_count: int | None = None

    @field_serializer("started_at", "finished_at")
    def _serialize_times(self, value: datetime) -> str:
        return _utc_json(value)

    @classmethod
    def from_storage(
        cls,
        summary: StorageMatchSummary | Mapping[str, object],
    ) -> MatchSummary:
        return cls(
            match_id=_storage_field(summary, "match_id"),
            game=_storage_field(summary, "game"),
            seed=_storage_field(summary, "seed"),
            players=_public_player_list(_storage_field(summary, "players")),
            scores=_public_scores(_storage_field(summary, "scores")),
            started_at=_storage_field(summary, "started_at"),
            finished_at=_storage_field(summary, "finished_at"),
            rated=_storage_field(summary, "rated", False),
            series_id=_storage_field(summary, "series_id", None),
            leg_number=_storage_field(summary, "leg_number", None),
            tournament_id=_storage_field(summary, "tournament_id", None),
            pairing_number=_storage_field(summary, "pairing_number", None),
            pairing_count=_storage_field(summary, "pairing_count", None),
        )

    @classmethod
    def from_archive(cls, archive: MatchArchive | Mapping[str, object]) -> MatchSummary:
        if not isinstance(archive, MatchArchive):
            archive = MatchArchive.model_validate(archive)
        return cls(
            match_id=archive.match_id,
            game=archive.game,
            seed=archive.seed,
            players=_display_names(archive.players),
            scores=_public_scores(archive.scores),
            started_at=archive.started_at,
            finished_at=archive.finished_at,
        )


class MatchListResponse(_PublicModel):
    api_version: Literal["v1"] = API_VERSION
    matches: tuple[MatchSummary, ...]

    @classmethod
    def from_storage(
        cls,
        summaries: Iterable[StorageMatchSummary | Mapping[str, object]],
    ) -> MatchListResponse:
        return cls(matches=tuple(MatchSummary.from_storage(item) for item in summaries))


class LeaderboardEntry(_PublicModel):
    player: str
    rating: float
    games_played: int
    wins: int
    draws: int
    losses: int
    updated_at: datetime

    @field_serializer("updated_at")
    def _serialize_time(self, value: datetime) -> str:
        return _utc_json(value)

    @classmethod
    def from_storage(
        cls,
        entry: RatingEntry | Mapping[str, object],
    ) -> LeaderboardEntry:
        try:
            player = _storage_field(entry, "display_name")
        except TypeError:
            player = _storage_field(entry, "player")
        return cls(
            player=player,
            rating=_storage_field(entry, "rating"),
            games_played=_storage_field(entry, "games_played"),
            wins=_storage_field(entry, "wins"),
            draws=_storage_field(entry, "draws"),
            losses=_storage_field(entry, "losses"),
            updated_at=_storage_field(entry, "updated_at"),
        )


class LeaderboardResponse(_PublicModel):
    api_version: Literal["v1"] = API_VERSION
    game: str | None = None
    entries: tuple[LeaderboardEntry, ...]

    @classmethod
    def from_storage(
        cls,
        entries: Iterable[RatingEntry | Mapping[str, object]],
        *,
        game: str | None = None,
    ) -> LeaderboardResponse:
        return cls(
            game=game,
            entries=tuple(LeaderboardEntry.from_storage(entry) for entry in entries),
        )


class MatchStartedData(_PublicModel):
    game: str
    seed: int
    game_config: dict[str, object]
    players: tuple[str, ...]

    @field_validator("game_config", mode="before")
    @classmethod
    def _sanitize_game_config(cls, value: object) -> dict[str, object]:
        if not isinstance(value, Mapping):
            return {}
        public: dict[str, object] = {}
        for key in _PUBLIC_GAME_CONFIG_KEYS:
            if key not in value:
                continue
            copied = _sanitize_public_value(value[key])
            if copied is not _DROP:
                public[key] = copied
        return public


class TurnPromptData(_PublicModel):
    prompt: str


class MoveReceivedData(_PublicModel):
    move: str


class MoveRejectedData(_PublicModel):
    move: str | None = None
    reason: str | None = None
    reason_code: str | None = None
    forfeit: bool = False
    technical_loss: bool = False


class JudgingSummary(_PublicModel):
    panel_size: int
    successful_judges: int
    quorum: int


class MatchFinishedData(_PublicModel):
    scores: dict[str, float]
    termination: str = "completed"
    reason_code: str | None = None
    forfeited_by: str | None = None
    judging: JudgingSummary | None = None


PublicEventData: TypeAlias = (
    MatchStartedData | TurnPromptData | MoveReceivedData | MoveRejectedData | MatchFinishedData
)


_EVENT_DATA_TYPES: dict[EventType, type[_PublicModel]] = {
    EventType.MATCH_STARTED: MatchStartedData,
    EventType.TURN_PROMPT: TurnPromptData,
    EventType.MOVE_RECEIVED: MoveReceivedData,
    EventType.MOVE_REJECTED: MoveRejectedData,
    EventType.MATCH_FINISHED: MatchFinishedData,
}


class PublicEvent(_PublicModel):
    seq: int
    type: EventType
    timestamp: datetime
    player: str | None = None
    data: PublicEventData

    @field_serializer("timestamp")
    def _serialize_timestamp(self, value: datetime) -> str:
        return _utc_json(value)

    @model_validator(mode="after")
    def _match_event_and_payload(self) -> PublicEvent:
        expected = _EVENT_DATA_TYPES[self.type]
        if not isinstance(self.data, expected):
            # Pydantic model validators use ValueError to produce ValidationError.
            raise ValueError(  # noqa: TRY004
                f"{self.type.value} has the wrong public payload"
            )
        return self

    @classmethod
    def from_event(cls, event: MatchEvent) -> PublicEvent:
        raw = event.data
        if event.type == EventType.MATCH_STARTED:
            data: PublicEventData = MatchStartedData(
                game=raw.get("game"),
                seed=raw.get("seed"),
                game_config=raw.get("game_config", {}),
                players=_display_names(raw.get("players")),
            )
        elif event.type == EventType.TURN_PROMPT:
            data = TurnPromptData(prompt=raw.get("prompt"))
        elif event.type == EventType.MOVE_RECEIVED:
            data = MoveReceivedData(move=raw.get("move"))
        elif event.type == EventType.MOVE_REJECTED:
            data = MoveRejectedData(
                move=raw.get("move"),
                reason=raw.get("reason"),
                reason_code=raw.get("reason_code"),
                forfeit=raw.get("forfeit", False),
                technical_loss=raw.get("technical_loss", False),
            )
        else:
            judging = raw.get("judging")
            public_judging = None
            if isinstance(judging, Mapping):
                public_judging = JudgingSummary(
                    panel_size=judging.get("panel_size"),
                    successful_judges=judging.get("successful_judges"),
                    quorum=judging.get("quorum"),
                )
            data = MatchFinishedData(
                scores=_public_scores(raw.get("scores")),
                termination=raw.get("termination") or "completed",
                reason_code=raw.get("reason_code"),
                forfeited_by=raw.get("forfeited_by"),
                judging=public_judging,
            )
        return cls(
            seq=event.seq,
            type=event.type,
            timestamp=event.timestamp,
            player=event.player,
            data=data,
        )


def public_event(event: MatchEvent) -> PublicEvent:
    """Functional spelling used by streaming code."""

    return PublicEvent.from_event(event)


_CHAMPIONSHIP_PLAYER_COUNTS = frozenset({4, 8, 16})


def _validate_championship_placement(
    *,
    round_number: int,
    round_count: int,
    round_pairing_number: int,
    round_pairing_count: int,
    pairing_number: int,
    pairing_count: int,
    leg_number: int,
) -> None:
    player_count = pairing_count + 1
    if player_count not in _CHAMPIONSHIP_PLAYER_COUNTS:
        raise ValueError("championship pairing_count must describe a 4/8/16-player bracket")
    if round_count != player_count.bit_length() - 1:
        raise ValueError("championship round_count is inconsistent with player_count")
    if not 1 <= round_number <= round_count:
        raise ValueError("championship round_number is outside the bracket")
    expected_round_pairings = player_count >> round_number
    if round_pairing_count != expected_round_pairings:
        raise ValueError("championship round_pairing_count is inconsistent")
    if not 1 <= round_pairing_number <= round_pairing_count:
        raise ValueError("championship round_pairing_number is outside the round")
    expected_pairing_number = (
        player_count
        - (player_count >> (round_number - 1))
        + round_pairing_number
    )
    if pairing_number != expected_pairing_number:
        raise ValueError("championship pairing_number is not canonical")
    if leg_number not in {1, 2}:
        raise ValueError("live series leg number must be one or two")


class LiveChampionshipContext(_PublicModel):
    """Canonical placement within a single-elimination two-leg bracket."""

    round_number: int = Field(ge=1)
    round_count: int = Field(ge=1)
    round_pairing_number: int = Field(ge=1)
    round_pairing_count: int = Field(ge=1)
    pairing_number: int = Field(ge=1)
    pairing_count: int = Field(ge=1)
    leg_number: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_placement(self) -> LiveChampionshipContext:
        _validate_championship_placement(**self.model_dump())
        return self


class LiveEventContext(_PublicModel):
    """Placement of one match event within a top-level live run."""

    pairing_number: int | None = Field(default=None, ge=1)
    pairing_count: int | None = Field(
        default=None,
        ge=1,
        exclude_if=lambda value: value is None,
    )
    leg_number: int | None = Field(default=None, ge=1)
    round_number: int | None = Field(
        default=None,
        ge=1,
        exclude_if=lambda value: value is None,
    )
    round_count: int | None = Field(
        default=None,
        ge=1,
        exclude_if=lambda value: value is None,
    )
    round_pairing_number: int | None = Field(
        default=None,
        ge=1,
        exclude_if=lambda value: value is None,
    )
    round_pairing_count: int | None = Field(
        default=None,
        ge=1,
        exclude_if=lambda value: value is None,
    )
    match_event_seq: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_placement(self) -> LiveEventContext:
        championship_fields = (
            self.round_number,
            self.round_count,
            self.round_pairing_number,
            self.round_pairing_count,
        )
        if any(value is not None for value in championship_fields):
            values = {
                "round_number": self.round_number,
                "round_count": self.round_count,
                "round_pairing_number": self.round_pairing_number,
                "round_pairing_count": self.round_pairing_count,
                "pairing_number": self.pairing_number,
                "pairing_count": self.pairing_count,
                "leg_number": self.leg_number,
            }
            if any(value is None for value in values.values()):
                raise ValueError("championship event context must be complete")
            _validate_championship_placement(**values)  # type: ignore[arg-type]
            return self
        if self.pairing_count is not None:
            raise ValueError("pairing_count is reserved for complete championship context")
        if self.leg_number is not None and self.leg_number not in {1, 2}:
            raise ValueError("live series leg number must be one or two")
        if self.pairing_number is not None and self.leg_number is None:
            raise ValueError("tournament event context requires a leg number")
        return self


class LiveEventItem(_PublicModel):
    """One event ordered by the broker-wide sequence of its live session."""

    kind: Literal["match_event"] = "match_event"
    seq: int = Field(ge=0)
    context: LiveEventContext
    event: PublicEvent

    @model_validator(mode="after")
    def _context_matches_event(self) -> LiveEventItem:
        if self.context.match_event_seq != self.event.seq:
            raise ValueError("live event context and public event sequence differ")
        return self


class LiveChampionshipPairing(_PublicModel):
    """Disclosure-safe result for one two-leg bracket pairing."""

    round_number: int = Field(ge=1)
    round_pairing_number: int = Field(ge=1)
    pairing_number: int = Field(ge=1)
    players: tuple[str, str]
    winner: str
    series_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    match_ids: tuple[str, str]
    status: Literal["provisional", "committed"]

    @field_validator("players")
    @classmethod
    def _validate_pairing_players(cls, value: tuple[str, str]) -> tuple[str, str]:
        if value[0] == value[1] or any(
            not _valid_public_player_name(player) for player in value
        ):
            raise ValueError("championship pairing requires two distinct public players")
        return value

    @field_validator("match_ids")
    @classmethod
    def _validate_pairing_match_ids(cls, value: tuple[str, str]) -> tuple[str, str]:
        if value[0] == value[1] or any(
            _SAFE_PUBLIC_ID_RE.fullmatch(match_id) is None for match_id in value
        ):
            raise ValueError("championship pairing requires two distinct match ids")
        return value

    @model_validator(mode="after")
    def _validate_winner(self) -> LiveChampionshipPairing:
        if self.winner not in self.players:
            raise ValueError("championship pairing winner must be one of its players")
        return self


class LiveChampionshipBracket(_PublicModel):
    """Materialized, reconnect-safe public championship bracket."""

    championship_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    player_count: int
    round_count: int = Field(ge=1)
    pairing_count: int = Field(ge=1)
    champion: str | None = None
    pairings: tuple[LiveChampionshipPairing, ...] = ()

    @model_validator(mode="after")
    def _validate_bracket(self) -> LiveChampionshipBracket:
        if self.player_count not in _CHAMPIONSHIP_PLAYER_COUNTS:
            raise ValueError("championship bracket requires 4, 8, or 16 players")
        if self.round_count != self.player_count.bit_length() - 1:
            raise ValueError("championship bracket round_count is inconsistent")
        if self.pairing_count != self.player_count - 1:
            raise ValueError("championship bracket pairing_count is inconsistent")
        if len(self.pairings) > self.pairing_count:
            raise ValueError("championship bracket has too many pairings")
        if tuple(pairing.pairing_number for pairing in self.pairings) != tuple(
            range(1, len(self.pairings) + 1)
        ):
            raise ValueError("championship bracket pairings must be a canonical prefix")

        committed_count = 0
        provisional_seen = False
        series_ids: set[str] = set()
        match_ids: set[str] = set()
        for pairing in self.pairings:
            round_pairing_count = self.player_count >> pairing.round_number
            _validate_championship_placement(
                round_number=pairing.round_number,
                round_count=self.round_count,
                round_pairing_number=pairing.round_pairing_number,
                round_pairing_count=round_pairing_count,
                pairing_number=pairing.pairing_number,
                pairing_count=self.pairing_count,
                leg_number=2,
            )
            if pairing.status == "committed":
                if provisional_seen:
                    raise ValueError("committed championship pairings must precede provisional ones")
                committed_count += 1
            else:
                provisional_seen = True
            if pairing.series_id in series_ids or any(
                match_id in match_ids for match_id in pairing.match_ids
            ):
                raise ValueError("championship bracket archive ids must be unique")
            series_ids.add(pairing.series_id)
            match_ids.update(pairing.match_ids)

        valid_committed_boundaries = {
            0,
            *(
                self.player_count - (self.player_count >> round_number)
                for round_number in range(1, self.round_count + 1)
            ),
        }
        if committed_count not in valid_committed_boundaries:
            raise ValueError("committed championship pairings must end at a whole-round boundary")
        provisional = self.pairings[committed_count:]
        if provisional:
            completed_rounds = next(
                round_number
                for round_number in range(self.round_count + 1)
                if committed_count
                == self.player_count - (self.player_count >> round_number)
            )
            next_round = completed_rounds + 1
            if next_round > self.round_count or any(
                pairing.round_number != next_round for pairing in provisional
            ):
                raise ValueError("provisional championship pairings must belong to the next round")
        if self.champion is not None and (
            committed_count != self.pairing_count
            or not self.pairings
            or self.champion != self.pairings[-1].winner
        ):
            raise ValueError("champion requires a fully committed bracket")
        return self


class LivePairingCompletedItem(_PublicModel):
    """Provisional pairing result, pending the whole-round checkpoint commit."""

    kind: Literal["pairing_completed"] = "pairing_completed"
    seq: int = Field(ge=0)
    context: LiveChampionshipContext
    pairing: LiveChampionshipPairing

    @model_validator(mode="after")
    def _validate_pairing_context(self) -> LivePairingCompletedItem:
        if self.context.leg_number != 2 or self.pairing.status != "provisional":
            raise ValueError("pairing_completed must describe a provisional second-leg result")
        if (
            self.pairing.round_number != self.context.round_number
            or self.pairing.round_pairing_number != self.context.round_pairing_number
            or self.pairing.pairing_number != self.context.pairing_number
        ):
            raise ValueError("pairing_completed context does not match its pairing")
        return self


class LiveRoundCommittedItem(_PublicModel):
    """Acknowledgement emitted only after a whole-round checkpoint commits."""

    kind: Literal["round_committed"] = "round_committed"
    seq: int = Field(ge=0)
    context: LiveChampionshipContext
    pairing_numbers: tuple[int, ...]

    @model_validator(mode="after")
    def _validate_round(self) -> LiveRoundCommittedItem:
        first = self.context.pairing_number - self.context.round_pairing_number + 1
        expected = tuple(range(first, first + self.context.round_pairing_count))
        if self.context.leg_number != 2 or self.pairing_numbers != expected:
            raise ValueError("round_committed must acknowledge the complete canonical round")
        return self


LiveStreamItem: TypeAlias = Annotated[
    LiveEventItem | LivePairingCompletedItem | LiveRoundCommittedItem,
    Field(discriminator="kind"),
]


class LiveMatchSummary(_PublicModel):
    """Disclosure-safe state for one top-level live run."""

    live_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    mode: GameMode
    status: LiveMatchStatus
    game: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    players: tuple[str, ...]
    started_at: datetime
    updated_at: datetime
    event_count: int = Field(ge=0, le=10_000)
    pairing_number: int | None = Field(default=None, ge=1)
    pairing_count: int | None = Field(default=None, ge=1)
    leg_number: int | None = Field(default=None, ge=1)
    round_number: int | None = Field(
        default=None,
        ge=1,
        exclude_if=lambda value: value is None,
    )
    round_count: int | None = Field(
        default=None,
        ge=1,
        exclude_if=lambda value: value is None,
    )
    round_pairing_number: int | None = Field(
        default=None,
        ge=1,
        exclude_if=lambda value: value is None,
    )
    round_pairing_count: int | None = Field(
        default=None,
        ge=1,
        exclude_if=lambda value: value is None,
    )
    championship_bracket: LiveChampionshipBracket | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    final_kind: LiveFinalKind | None = None
    final_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    final_match_ids: tuple[str, ...] = ()

    @field_serializer("started_at", "updated_at")
    def _serialize_times(self, value: datetime) -> str:
        return _utc_json(value)

    @field_validator("players")
    @classmethod
    def _validate_players(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) < 2 or len(value) > 16 or len(value) != len(set(value)) or any(
            not _valid_public_player_name(player)
            for player in value
        ):
            raise ValueError("live players must contain at least two display names")
        return value

    @field_validator("final_match_ids")
    @classmethod
    def _validate_final_match_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 10_000 or len(value) != len(set(value)):
            raise ValueError("final match ids must be bounded and unique")
        if any(_SAFE_PUBLIC_ID_RE.fullmatch(match_id) is None for match_id in value):
            raise ValueError("final match id is invalid")
        return value

    @model_validator(mode="after")
    def _validate_live_state(self) -> LiveMatchSummary:
        if self.updated_at < self.started_at:
            raise ValueError("live updated_at precedes started_at")
        if self.pairing_number is not None and self.pairing_count is None:
            raise ValueError("pairing_number requires pairing_count")
        if (
            self.pairing_number is not None
            and self.pairing_count is not None
            and self.pairing_number > self.pairing_count
        ):
            raise ValueError("pairing_number exceeds pairing_count")
        if self.mode == "play" and (
            self.pairing_number is not None
            or self.pairing_count is not None
            or self.leg_number is not None
            or self.round_number is not None
            or self.round_count is not None
            or self.round_pairing_number is not None
            or self.round_pairing_count is not None
        ):
            raise ValueError("single-match live runs cannot have series context")
        if self.mode == "series" and (
            self.pairing_number is not None
            or self.pairing_count is not None
            or self.round_number is not None
            or self.round_count is not None
            or self.round_pairing_number is not None
            or self.round_pairing_count is not None
        ):
            raise ValueError("series live runs cannot have tournament context")
        if self.mode in {"series", "round_robin", "championship"} and (
            self.leg_number is not None and self.leg_number not in {1, 2}
        ):
            raise ValueError("live series leg number must be one or two")
        if self.mode == "round_robin" and self.pairing_count is None:
            raise ValueError("tournament live runs require pairing_count")
        championship_fields = {
            "round_number": self.round_number,
            "round_count": self.round_count,
            "round_pairing_number": self.round_pairing_number,
            "round_pairing_count": self.round_pairing_count,
            "pairing_number": self.pairing_number,
            "pairing_count": self.pairing_count,
            "leg_number": self.leg_number,
        }
        if self.mode == "round_robin" and any(
            championship_fields[field] is not None
            for field in (
                "round_number",
                "round_count",
                "round_pairing_number",
                "round_pairing_count",
            )
        ):
            raise ValueError("round-robin live runs cannot have championship context")
        if self.mode == "championship":
            if any(value is None for value in championship_fields.values()):
                raise ValueError("championship live runs require complete bracket context")
            _validate_championship_placement(**championship_fields)  # type: ignore[arg-type]
            if len(self.players) not in _CHAMPIONSHIP_PLAYER_COUNTS:
                raise ValueError("championship live runs require 4, 8, or 16 players")
            if self.championship_bracket is None:
                raise ValueError("championship live runs require a materialized bracket")
            bracket = self.championship_bracket
            if bracket.player_count != len(self.players):
                raise ValueError("championship roster and bracket size differ")
            pairing_winners: dict[tuple[int, int], str] = {}
            for pairing in bracket.pairings:
                if pairing.round_number == 1:
                    offset = (pairing.round_pairing_number - 1) * 2
                    expected_players = self.players[offset : offset + 2]
                else:
                    previous_round = pairing.round_number - 1
                    previous_offset = (pairing.round_pairing_number - 1) * 2 + 1
                    expected_players = (
                        pairing_winners.get((previous_round, previous_offset)),
                        pairing_winners.get((previous_round, previous_offset + 1)),
                    )
                if tuple(expected_players) != pairing.players:
                    raise ValueError("championship pairing players do not follow bracket winners")
                pairing_winners[(pairing.round_number, pairing.round_pairing_number)] = (
                    pairing.winner
                )
            materialized_count = len(bracket.pairings)
            if self.pairing_number not in {
                materialized_count,
                materialized_count + 1,
            }:
                raise ValueError("championship current pairing does not follow its bracket")
            if self.pairing_number == materialized_count:
                if materialized_count == 0 or self.leg_number != 2:
                    raise ValueError("materialized championship pairing requires leg two")
                latest = bracket.pairings[-1]
                if (
                    self.round_number != latest.round_number
                    or self.round_pairing_number != latest.round_pairing_number
                ):
                    raise ValueError("championship current context differs from latest pairing")
            elif bracket.pairings and bracket.pairings[-1].status == "provisional":
                latest = bracket.pairings[-1]
                if (
                    self.round_number != latest.round_number
                    or self.round_pairing_number != latest.round_pairing_number + 1
                ):
                    raise ValueError("championship cannot advance before round commit")
            if self.status != "completed" and bracket.champion is not None:
                raise ValueError("unfinished championship live runs cannot expose a champion")
        elif self.championship_bracket is not None:
            raise ValueError("only championship live runs may expose a bracket")
        if self.status == "completed":
            if (
                self.final_kind != _MODE_FINAL_KIND[self.mode]
                or self.final_id is None
                or not self.final_match_ids
            ):
                raise ValueError("completed live runs require matching final archive references")
            if self.mode == "play" and self.final_match_ids != (self.final_id,):
                raise ValueError("completed match must reference its one final match archive")
            if self.mode == "series" and len(self.final_match_ids) != 2:
                raise ValueError("completed series must reference exactly two match archives")
            if (
                self.mode == "round_robin"
                and self.pairing_count is not None
                and len(self.final_match_ids) != self.pairing_count * 2
            ):
                raise ValueError("completed tournament match archive count is inconsistent")
            if self.mode == "championship":
                bracket = self.championship_bracket
                if (
                    bracket is None
                    or self.round_number != self.round_count
                    or self.round_pairing_number != 1
                    or self.round_pairing_count != 1
                    or self.pairing_number != self.pairing_count
                    or self.leg_number != 2
                    or self.final_id != bracket.championship_id
                    or bracket.champion is None
                    or len(bracket.pairings) != bracket.pairing_count
                    or any(pairing.status != "committed" for pairing in bracket.pairings)
                    or self.final_match_ids
                    != tuple(
                        match_id
                        for pairing in bracket.pairings
                        for match_id in pairing.match_ids
                    )
                ):
                    raise ValueError("completed championship archive references are inconsistent")
        elif self.final_kind is not None or self.final_id is not None or self.final_match_ids:
            raise ValueError("unfinished live runs cannot expose final archive references")
        return self


class LiveMatchListResponse(_PublicModel):
    api_version: Literal["v1"] = API_VERSION
    matches: tuple[LiveMatchSummary, ...]


class LiveMatchDetail(_PublicModel):
    api_version: Literal["v1"] = API_VERSION
    match: LiveMatchSummary
    events: tuple[LiveStreamItem, ...]
    next_seq: int = Field(ge=0)
    has_more: bool

    @model_validator(mode="after")
    def _validate_page(self) -> LiveMatchDetail:
        if self.events:
            first = self.events[0].seq
            if tuple(item.seq for item in self.events) != tuple(
                range(first, first + len(self.events))
            ):
                raise ValueError("live event page must be contiguous")
            if self.next_seq != first + len(self.events):
                raise ValueError("next_seq does not follow live event page")
        if self.next_seq > self.match.event_count:
            raise ValueError("next_seq exceeds live event count")
        if self.has_more != (self.next_seq < self.match.event_count):
            raise ValueError("has_more does not match live event count")
        if self.match.mode != "championship":
            if any(not isinstance(item, LiveEventItem) for item in self.events):
                raise ValueError("non-championship live runs accept match events only")
            return self

        bracket = self.match.championship_bracket
        if bracket is None:  # pragma: no cover - LiveMatchSummary already enforces this
            raise ValueError("championship live detail requires a bracket")
        pairings = {
            pairing.pairing_number: pairing for pairing in bracket.pairings
        }
        for item in self.events:
            context = item.context
            if (
                context.round_count != bracket.round_count
                or context.pairing_count != bracket.pairing_count
                or context.pairing_number > len(bracket.pairings) + 1
            ):
                raise ValueError("championship live item does not match its bracket")
            if isinstance(item, LiveEventItem):
                if any(
                    value is None
                    for value in (
                        context.round_number,
                        context.round_pairing_number,
                        context.round_pairing_count,
                        context.leg_number,
                    )
                ):
                    raise ValueError("championship match events require complete context")
                materialized = pairings.get(context.pairing_number)
                if materialized is not None:
                    expected_players = materialized.players
                elif context.round_number == 1:
                    offset = 2 * (context.round_pairing_number - 1)  # type: ignore[operator]
                    expected_players = self.match.players[offset : offset + 2]
                else:
                    previous_round = context.round_number - 1  # type: ignore[operator]
                    previous_offset = 2 * (context.round_pairing_number - 1) + 1  # type: ignore[operator]
                    first_source = next(
                        (
                            pairing.winner
                            for pairing in bracket.pairings
                            if pairing.round_number == previous_round
                            and pairing.round_pairing_number == previous_offset
                        ),
                        None,
                    )
                    second_source = next(
                        (
                            pairing.winner
                            for pairing in bracket.pairings
                            if pairing.round_number == previous_round
                            and pairing.round_pairing_number == previous_offset + 1
                        ),
                        None,
                    )
                    expected_players = (first_source, second_source)
                if len(expected_players) != 2 or any(
                    player is None for player in expected_players
                ):
                    raise ValueError("championship event pairing cannot be resolved")
                base_players = tuple(expected_players)
                event = item.event
                if event.player is not None and event.player not in base_players:
                    raise ValueError("championship event player is outside its pairing")
                if isinstance(event.data, MatchStartedData):
                    ordered_players = (
                        base_players
                        if context.leg_number == 1
                        else tuple(reversed(base_players))
                    )
                    if event.data.game != self.match.game or event.data.players != ordered_players:
                        raise ValueError("match_started does not match championship pairing")
                elif isinstance(event.data, MatchFinishedData) and (
                    set(event.data.scores) != set(base_players)
                    or (
                        event.data.forfeited_by is not None
                        and event.data.forfeited_by not in base_players
                    )
                ):
                    raise ValueError("match_finished does not match championship pairing")
                continue
            if isinstance(item, LivePairingCompletedItem):
                materialized = pairings.get(item.context.pairing_number)
                if (
                    materialized is None
                    or materialized.model_dump(exclude={"status"})
                    != item.pairing.model_dump(exclude={"status"})
                ):
                    raise ValueError("pairing event does not match materialized bracket")
                continue
            if any(
                pairings.get(pairing_number) is None
                or pairings[pairing_number].status != "committed"
                for pairing_number in item.pairing_numbers
            ):
                raise ValueError("round event does not match committed bracket")
        return self


class ParticipationRequest(_PublicModel):
    """Current human-input prompt, with no submitted content or capability data."""

    request_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    request_seq: int = Field(ge=0)
    match_event_seq: int = Field(ge=0)
    state: ParticipationRequestState
    prompt: str = Field(max_length=65_536)
    created_at: datetime
    expires_at: datetime

    @field_serializer("created_at", "expires_at")
    def _serialize_times(self, value: datetime) -> str:
        return _utc_json(value)

    @model_validator(mode="after")
    def _validate_window(self) -> ParticipationRequest:
        if self.expires_at <= self.created_at:
            raise ValueError("participation request must expire after it is created")
        return self

    @classmethod
    def from_input_snapshot(cls, source: object) -> ParticipationRequest:
        """Copy the broker request through an explicit disclosure allow-list."""

        return cls(
            request_id=_storage_field(source, "request_id"),
            request_seq=_storage_field(source, "request_seq"),
            match_event_seq=_storage_field(source, "match_event_seq"),
            state=_storage_field(source, "state"),
            prompt=_storage_field(source, "prompt"),
            created_at=_storage_field(source, "created_at"),
            expires_at=_storage_field(source, "expires_at"),
        )


class ParticipationSnapshotResponse(_PublicModel):
    """Capability-scoped input state safe to return from the participation GET."""

    api_version: Literal["v1"] = API_VERSION
    session_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    seat_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    status: ParticipationStatus
    game: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    player_name: str = Field(min_length=1, max_length=512)
    players: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    lease_expires_at: datetime
    request: ParticipationRequest | None = None
    final_match_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )

    @field_serializer("created_at", "updated_at", "lease_expires_at")
    def _serialize_times(self, value: datetime) -> str:
        return _utc_json(value)

    @field_validator("player_name")
    @classmethod
    def _validate_player_name(cls, value: str) -> str:
        if any(
            ord(character) < 32
            or 127 <= ord(character) <= 159
            or 0xD800 <= ord(character) <= 0xDFFF
            or character in _BIDI_CONTROL_CHARACTERS
            for character in value
        ):
            raise ValueError("participation player name contains unsafe controls")
        return value

    @field_validator("players")
    @classmethod
    def _validate_players(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if (
            not 2 <= len(value) <= MAX_PLATFORM_PLAYERS
            or len(set(value)) != len(value)
            or any(
                not player
                or len(player) > 512
                or any(
                    ord(character) < 32
                    or 127 <= ord(character) <= 159
                    or 0xD800 <= ord(character) <= 0xDFFF
                    or character in _BIDI_CONTROL_CHARACTERS
                    for character in player
                )
                for player in value
            )
        ):
            raise ValueError(
                "participation players must contain 2 to "
                f"{MAX_PLATFORM_PLAYERS} safe display names"
            )
        return value

    @model_validator(mode="after")
    def _validate_snapshot(self) -> ParticipationSnapshotResponse:
        if self.player_name not in self.players:
            raise ValueError("participation player is not in the match")
        if self.updated_at < self.created_at:
            raise ValueError("participation updated_at precedes created_at")
        if self.lease_expires_at <= self.created_at:
            raise ValueError("participation lease must expire after creation")
        if self.status != "active" and self.request is not None:
            raise ValueError("terminal participation snapshots cannot expose a request")
        if self.status == "completed":
            if self.final_match_id is None:
                raise ValueError("completed participation requires a final match archive")
        elif self.final_match_id is not None:
            raise ValueError("unfinished participation cannot expose a final match archive")
        return self

    @classmethod
    def from_input_snapshot(cls, source: object) -> ParticipationSnapshotResponse:
        """Copy only the public, capability-scoped broker snapshot fields."""

        raw_request = _storage_field(source, "request", None)
        return cls(
            session_id=_storage_field(source, "session_id"),
            seat_id=_storage_field(source, "seat_id"),
            status=_storage_field(source, "status"),
            game=_storage_field(source, "game"),
            player_name=_storage_field(source, "player_name"),
            players=_public_player_list(_storage_field(source, "players")),
            created_at=_storage_field(source, "created_at"),
            updated_at=_storage_field(source, "updated_at"),
            lease_expires_at=_storage_field(source, "lease_expires_at"),
            request=(
                ParticipationRequest.from_input_snapshot(raw_request)
                if raw_request is not None
                else None
            ),
            final_match_id=_storage_field(source, "final_match_id", None),
        )


class ParticipationSubmissionRequest(_PublicModel):
    """Idempotent browser submission bound to the request in the URL path."""

    submission_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    move: str = Field(max_length=4_096)


class ParticipationSubmissionResponse(_PublicModel):
    api_version: Literal["v1"] = API_VERSION
    request_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    status: Literal["submitted", "duplicate"]


class WSLiveSnapshotEnvelope(_PublicModel):
    api_version: Literal["v1"] = API_VERSION
    type: Literal["live_snapshot"] = "live_snapshot"
    match: LiveMatchSummary
    next_seq: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_cursor(self) -> WSLiveSnapshotEnvelope:
        if self.next_seq > self.match.event_count:
            raise ValueError("live snapshot cursor exceeds event count")
        return self


class WSLiveEventEnvelope(_PublicModel):
    api_version: Literal["v1"] = API_VERSION
    type: Literal["live_event"] = "live_event"
    live_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    item: LiveStreamItem


class WSLiveCompleteEnvelope(_PublicModel):
    api_version: Literal["v1"] = API_VERSION
    type: Literal["live_complete"] = "live_complete"
    live_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    event_count: int = Field(ge=0, le=10_000)
    final_kind: LiveFinalKind
    final_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    final_match_ids: tuple[str, ...]

    @field_validator("final_match_ids")
    @classmethod
    def _validate_match_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) > 10_000 or len(value) != len(set(value)):
            raise ValueError("final match ids must be non-empty, bounded, and unique")
        if any(_SAFE_PUBLIC_ID_RE.fullmatch(match_id) is None for match_id in value):
            raise ValueError("final match id is invalid")
        return value

    @model_validator(mode="after")
    def _validate_final_reference(self) -> WSLiveCompleteEnvelope:
        if self.final_kind == "match" and self.final_match_ids != (self.final_id,):
            raise ValueError("completed match envelope has inconsistent archive references")
        if self.final_kind == "series" and len(self.final_match_ids) != 2:
            raise ValueError("completed series envelope must reference exactly two matches")
        if self.final_kind == "championship" and len(self.final_match_ids) not in {
            6,
            14,
            30,
        }:
            raise ValueError("completed championship envelope has invalid archive count")
        return self


class WSLiveInterruptedEnvelope(_PublicModel):
    api_version: Literal["v1"] = API_VERSION
    type: Literal["live_interrupted"] = "live_interrupted"
    live_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    event_count: int = Field(ge=0, le=10_000)


LiveWebSocketEnvelope: TypeAlias = Annotated[
    WSLiveSnapshotEnvelope
    | WSLiveEventEnvelope
    | WSLiveCompleteEnvelope
    | WSLiveInterruptedEnvelope,
    Field(discriminator="type"),
]


class MatchDetail(_PublicModel):
    api_version: Literal["v1"] = API_VERSION
    match: MatchSummary
    events: tuple[PublicEvent, ...]

    @classmethod
    def from_archive(
        cls,
        archive: MatchArchive | Mapping[str, object],
        *,
        summary: StorageMatchSummary | Mapping[str, object] | None = None,
    ) -> MatchDetail:
        if not isinstance(archive, MatchArchive):
            archive = MatchArchive.model_validate(archive)
        public_summary = (
            MatchSummary.from_storage(summary)
            if summary is not None
            else MatchSummary.from_archive(archive)
        )
        if public_summary.match_id != archive.match_id:
            raise ValueError("summary and archive match_id differ")
        return cls(
            match=public_summary,
            events=tuple(PublicEvent.from_event(event) for event in archive.events),
        )


class WSArchiveEnvelope(_PublicModel):
    api_version: Literal["v1"] = API_VERSION
    type: Literal["archive"] = "archive"
    match: MatchSummary
    event_count: int = Field(ge=0)

    @classmethod
    def from_archive(
        cls,
        archive: MatchArchive | Mapping[str, object],
        *,
        summary: StorageMatchSummary | Mapping[str, object] | None = None,
    ) -> WSArchiveEnvelope:
        if not isinstance(archive, MatchArchive):
            archive = MatchArchive.model_validate(archive)
        public_summary = (
            MatchSummary.from_storage(summary)
            if summary is not None
            else MatchSummary.from_archive(archive)
        )
        if public_summary.match_id != archive.match_id:
            raise ValueError("summary and archive match_id differ")
        return cls(match=public_summary, event_count=len(archive.events))


class WSEventEnvelope(_PublicModel):
    api_version: Literal["v1"] = API_VERSION
    type: Literal["event"] = "event"
    match_id: str
    event: PublicEvent

    @classmethod
    def from_event(cls, match_id: str, event: MatchEvent) -> WSEventEnvelope:
        return cls(match_id=match_id, event=PublicEvent.from_event(event))


class WSCompleteEnvelope(_PublicModel):
    api_version: Literal["v1"] = API_VERSION
    type: Literal["complete"] = "complete"
    match_id: str
    event_count: int = Field(ge=0)


class WSErrorEnvelope(_PublicModel):
    api_version: Literal["v1"] = API_VERSION
    type: Literal["error"] = "error"
    code: Literal["database_unavailable", "match_not_found", "invalid_request"]


WebSocketEnvelope: TypeAlias = Annotated[
    WSArchiveEnvelope | WSEventEnvelope | WSCompleteEnvelope | WSErrorEnvelope,
    Field(discriminator="type"),
]


# Explicit public aliases make disclosure intent clear at call sites.
MatchSummaryPublic = MatchSummary
LeaderboardEntryPublic = LeaderboardEntry


__all__ = [
    "API_VERSION",
    "ErrorDetail",
    "ErrorResponse",
    "GameInfo",
    "GameListResponse",
    "HealthResponse",
    "JudgingSummary",
    "LeaderboardEntry",
    "LeaderboardEntryPublic",
    "LeaderboardResponse",
    "LiveChampionshipBracket",
    "LiveChampionshipContext",
    "LiveChampionshipPairing",
    "LiveEventContext",
    "LiveEventItem",
    "LiveFinalKind",
    "LiveMatchDetail",
    "LiveMatchListResponse",
    "LiveMatchStatus",
    "LiveMatchSummary",
    "LivePairingCompletedItem",
    "LiveRoundCommittedItem",
    "LiveStreamItem",
    "LiveWebSocketEnvelope",
    "MatchDetail",
    "MatchFinishedData",
    "MatchListResponse",
    "MatchStartedData",
    "MatchSummary",
    "MatchSummaryPublic",
    "MoveReceivedData",
    "MoveRejectedData",
    "ParticipationRequest",
    "ParticipationRequestState",
    "ParticipationSnapshotResponse",
    "ParticipationStatus",
    "ParticipationSubmissionRequest",
    "ParticipationSubmissionResponse",
    "PublicEvent",
    "PublicEventData",
    "TurnPromptData",
    "WSArchiveEnvelope",
    "WSCompleteEnvelope",
    "WSErrorEnvelope",
    "WSEventEnvelope",
    "WSLiveCompleteEnvelope",
    "WSLiveEventEnvelope",
    "WSLiveInterruptedEnvelope",
    "WSLiveSnapshotEnvelope",
    "WebSocketEnvelope",
    "public_event",
]
