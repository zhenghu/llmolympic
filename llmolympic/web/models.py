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
from llmolympic.core.storage import MatchSummary as StorageMatchSummary
from llmolympic.core.storage import RatingEntry

API_VERSION = "v1"
_MAX_PUBLIC_NESTING = 16
_DROP = object()
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
        return result
    if isinstance(value, (list, tuple)):
        result_list: list[object] = []
        for item in value:
            copied = _sanitize_public_value(item, depth=depth + 1)
            if copied is not _DROP:
                result_list.append(copied)
        return result_list
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


GameMode: TypeAlias = Literal["play", "series", "round_robin"]


class GameInfo(_PublicModel):
    name: str
    supported_modes: tuple[GameMode, ...]
    requires_judge_panel: bool = False

    @classmethod
    def from_game(cls, name: str, game_class: type) -> GameInfo:
        modes = getattr(game_class, "supported_modes", ("play", "series", "round_robin"))
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
    "MatchDetail",
    "MatchFinishedData",
    "MatchListResponse",
    "MatchStartedData",
    "MatchSummary",
    "MatchSummaryPublic",
    "MoveReceivedData",
    "MoveRejectedData",
    "PublicEvent",
    "PublicEventData",
    "TurnPromptData",
    "WSArchiveEnvelope",
    "WSCompleteEnvelope",
    "WSErrorEnvelope",
    "WSEventEnvelope",
    "WebSocketEnvelope",
    "public_event",
]
