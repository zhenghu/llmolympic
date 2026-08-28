"""Local, capability-gated Web job control primitives.

The control plane intentionally stores only normalized, disclosure-safe job
specifications.  Provider credentials remain volatile in controller memory and
are copied only to the selected fixed-argument child process environment.  The
official archive/ELO database remains owned by the existing competition commands.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import sys
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import parse_qs, quote, urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from llmolympic.config import ProviderProfile, load_profiles
from llmolympic.core.championship import ChampionshipCheckpoint
from llmolympic.core.game import (
    MAX_PLATFORM_PLAYERS,
    describe_game_config,
    validate_player_count,
)
from llmolympic.core.storage import SCHEMA_VERSION
from llmolympic.core.tournament import TournamentCheckpoint
from llmolympic.core.usage import ProviderBudgetPolicy, UsageValidationError
from llmolympic.games import GAME_REGISTRY, create_game, game_supports_mode
from llmolympic.providers.base import validate_base_url
from llmolympic.web.models import API_VERSION, GameInfo

CONTROL_SCHEMA_VERSION = 2
MAX_PREPARED_JOBS = 1
MAX_CONTROL_BODY_BYTES = 32 * 1024
JOB_RETENTION_SECONDS = 24 * 60 * 60
PREPARED_JOB_RETENTION_SECONDS = 30 * 60
MAX_UNCONFIRMED_TOURNAMENT_MATCHES = 30
MAX_UNCONFIRMED_TOURNAMENT_PROVIDER_CALLS = 5_000
TOURNAMENT_MOVE_ATTEMPTS = 3
MAX_PROFILE_API_KEY_BYTES = 8 * 1024

ControlMode = Literal["play", "series", "round_robin", "championship"]
ControlJobStatus = Literal[
    "prepared",
    "starting",
    "running",
    "finalizing",
    "cancel_requested",
    "cancelled",
    "completed",
    "failed",
    "interrupted",
]
ControlFinalKind = Literal["match", "series", "tournament", "championship"]
ProfileCredentialReady = Callable[[ProviderProfile], bool]
ControlFailureCode = Literal[
    "controller_restarted",
    "worker_failed",
    "worker_interrupted",
    "worker_missing",
    "worker_protocol_incomplete",
    "worker_shutdown_timeout",
    "worker_start_failed",
    "worker_start_interrupted",
    "worker_start_timeout",
]

_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_PROFILE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_IDEMPOTENCY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_CANONICAL_INTEGER_RE = re.compile(r"(?:0|-?[1-9][0-9]*)\Z")
_CANONICAL_UNSIGNED_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_BIDI_CONTROLS = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)
_SQLITE_INT_MIN = -(2**63)
_SQLITE_INT_MAX = 2**63 - 1
_MAX_COST_USD = Decimal(1_000_000)
_MOCK_PLAYER_STRATEGIES = ("random", "fixed", "illegal", "balanced")
_MOCK_JUDGE_STRATEGIES = ("strict", "balanced", "lenient")
_TERMINAL_STATUSES = frozenset({"cancelled", "completed", "failed", "interrupted"})
_CONTROL_JOBS_V1_COLUMNS = (
    ("job_id", "TEXT", 0, None, 1),
    ("prepare_key", "TEXT", 1, None, 0),
    ("request_digest", "TEXT", 1, None, 0),
    ("spec_json", "TEXT", 1, None, 0),
    ("preview_json", "TEXT", 1, None, 0),
    ("status", "TEXT", 1, None, 0),
    ("created_at", "TEXT", 1, None, 0),
    ("updated_at", "TEXT", 1, None, 0),
    ("started_at", "TEXT", 0, None, 0),
    ("finished_at", "TEXT", 0, None, 0),
    ("live_id", "TEXT", 0, None, 0),
    ("tournament_id", "TEXT", 0, None, 0),
    ("final_kind", "TEXT", 0, None, 0),
    ("final_id", "TEXT", 0, None, 0),
    ("final_match_ids_json", "TEXT", 1, "'[]'", 0),
    ("failure_code", "TEXT", 0, None, 0),
    ("child_pid", "INTEGER", 0, None, 0),
)
_CONTROL_JOBS_V2_COLUMNS = (
    *_CONTROL_JOBS_V1_COLUMNS[:12],
    ("championship_id", "TEXT", 0, None, 0),
    *_CONTROL_JOBS_V1_COLUMNS[12:],
)
_CONTROL_JOBS_V2_MIGRATED_COLUMNS = (
    *_CONTROL_JOBS_V1_COLUMNS,
    ("championship_id", "TEXT", 0, None, 0),
)
_CONTROL_OPERATIONS_COLUMNS = (
    ("idempotency_key", "TEXT", 0, None, 1),
    ("job_id", "TEXT", 1, None, 0),
    ("operation", "TEXT", 1, None, 0),
    ("request_digest", "TEXT", 1, None, 0),
    ("created_at", "TEXT", 1, None, 0),
)


class ControlError(RuntimeError):
    """Stable internal error code mapped by the local Web API."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _ControlModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def _safe_name(value: str, *, label: str) -> str:
    candidate = value.strip()
    if not candidate or len(candidate) > 128 or "," in candidate:
        raise ValueError(f"{label} must be 1-128 characters and contain no comma")
    if any(
        ord(char) < 32
        or 0x7F <= ord(char) <= 0x9F
        or char in _BIDI_CONTROLS
        for char in candidate
    ):
        raise ValueError(f"{label} contains unsafe control characters")
    return candidate


def _canonical_integer(value: str, *, minimum: int, maximum: int) -> str:
    if not isinstance(value, str) or _CANONICAL_INTEGER_RE.fullmatch(value) is None:
        raise ValueError("integer must use canonical decimal text")
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise ValueError("integer is outside the supported range")
    return value


class ControlPlayerSpec(_ControlModel):
    kind: Literal["human", "mock", "profile"]
    name: str | None = None
    strategy: Literal["random", "fixed", "illegal", "balanced"] | None = None
    profile_id: str | None = None

    @model_validator(mode="after")
    def validate_variant(self) -> Self:
        if self.kind == "human":
            if self.strategy is not None or self.profile_id is not None:
                raise ValueError("human player accepts only name")
            if self.name is None:
                raise ValueError("human player requires name")
            object.__setattr__(self, "name", _safe_name(self.name, label="player name"))
        elif self.kind == "mock":
            if self.name is not None or self.profile_id is not None:
                raise ValueError("mock player accepts only strategy")
            if self.strategy is None:
                raise ValueError("mock player requires strategy")
        else:
            if self.name is not None or self.strategy is not None:
                raise ValueError("profile player accepts only profile_id")
            if self.profile_id is None or _PROFILE_ID_RE.fullmatch(self.profile_id) is None:
                raise ValueError("profile player requires a safe profile_id")
        return self

    def cli_token(self) -> str:
        if self.kind == "human":
            return f"human:{self.name}"
        if self.kind == "mock":
            return f"mock:{self.strategy}"
        return f"profile:{self.profile_id}"

    def identity_key(self) -> str:
        return self.cli_token()


class ControlJudgeSpec(_ControlModel):
    kind: Literal["mock", "profile"]
    strategy: Literal["strict", "balanced", "lenient"] | None = None
    profile_id: str | None = None

    @model_validator(mode="after")
    def validate_variant(self) -> Self:
        if self.kind == "mock":
            if self.strategy is None or self.profile_id is not None:
                raise ValueError("mock judge requires only strategy")
        elif self.profile_id is None or self.strategy is not None:
            raise ValueError("profile judge requires only profile_id")
        elif _PROFILE_ID_RE.fullmatch(self.profile_id) is None:
            raise ValueError("profile judge requires a safe profile_id")
        return self

    def cli_token(self) -> str:
        return f"mock:{self.strategy}" if self.kind == "mock" else f"profile:{self.profile_id}"


class ControlBudgetSpec(_ControlModel):
    max_provider_calls: str | None = None
    max_input_tokens: str | None = None
    max_output_tokens_per_call: str | None = None
    max_total_output_tokens: str | None = None
    max_estimated_cost_usd: str | None = None

    @field_validator(
        "max_provider_calls",
        "max_input_tokens",
        "max_total_output_tokens",
    )
    @classmethod
    def validate_unsigned(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if _CANONICAL_UNSIGNED_RE.fullmatch(value) is None:
            raise ValueError("budget count must use canonical unsigned decimal text")
        if int(value) > _SQLITE_INT_MAX:
            raise ValueError("budget count is outside the SQLite integer range")
        return value

    @field_validator("max_output_tokens_per_call")
    @classmethod
    def validate_positive(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if _CANONICAL_UNSIGNED_RE.fullmatch(value) is None or int(value) < 1:
            raise ValueError("per-call output budget must be a positive integer")
        if int(value) > _SQLITE_INT_MAX:
            raise ValueError("per-call output budget is outside the SQLite integer range")
        return value

    @field_validator("max_estimated_cost_usd")
    @classmethod
    def validate_cost(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = Decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("cost budget must be decimal text") from exc
        if (
            not parsed.is_finite()
            or parsed < 0
            or parsed > _MAX_COST_USD
            or parsed.as_tuple().exponent < -6
        ):
            raise ValueError(
                "cost budget must be finite, between 0 and 1000000, and use at most 6 decimals"
            )
        if str(parsed) != value and format(parsed, "f") != value:
            raise ValueError("cost budget must use canonical decimal text")
        return value

    def is_complete_hard_limit(self) -> bool:
        return all(
            value is not None
            for value in (
                self.max_provider_calls,
                self.max_input_tokens,
                self.max_output_tokens_per_call,
                self.max_total_output_tokens,
                self.max_estimated_cost_usd,
            )
        )


class ControlJobSpec(_ControlModel):
    mode: ControlMode
    game: str = ""
    players: Annotated[tuple[ControlPlayerSpec, ...], Field(max_length=16)] = ()
    judges: Annotated[tuple[ControlJudgeSpec, ...], Field(max_length=9)] = ()
    rounds: Annotated[int | None, Field(ge=1, le=100)] = None
    seed: str = "0"
    human_timeout_seconds: Annotated[float, Field(ge=0.001, le=86_400)] = 120.0
    llm_timeout_seconds: Annotated[float | None, Field(ge=0.001, le=86_400)] = None
    budget: ControlBudgetSpec = ControlBudgetSpec()
    allow_large_tournament: bool = False
    resume_tournament_id: str | None = None
    resume_championship_id: str | None = None

    @field_validator("seed")
    @classmethod
    def validate_seed(cls, value: str) -> str:
        return _canonical_integer(value, minimum=_SQLITE_INT_MIN, maximum=_SQLITE_INT_MAX)

    @field_validator("game")
    @classmethod
    def validate_game_name(cls, value: str) -> str:
        if value and value not in GAME_REGISTRY:
            raise ValueError("unknown game")
        return value

    @field_validator("human_timeout_seconds", "llm_timeout_seconds")
    @classmethod
    def validate_timeout(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("timeout must be finite")
        return value

    @model_validator(mode="after")
    def validate_semantics(self) -> Self:
        resume_id = (
            self.resume_tournament_id
            if self.resume_tournament_id is not None
            else self.resume_championship_id
        )
        if self.resume_tournament_id is not None and self.resume_championship_id is not None:
            raise ValueError("only one competition checkpoint can be resumed")
        if resume_id is not None:
            expected_mode = (
                "round_robin" if self.resume_tournament_id is not None else "championship"
            )
            if self.mode != expected_mode or _SAFE_ID_RE.fullmatch(resume_id) is None:
                raise ValueError(f"only {expected_mode} can resume its safe competition ID")
            if (
                self.players
                or self.judges
                or self.rounds is not None
                or self.game
                or self.seed != "0"
                or self.human_timeout_seconds != 120.0
                or self.llm_timeout_seconds is not None
                or self.budget != ControlBudgetSpec()
                or self.allow_large_tournament
            ):
                raise ValueError("resume must not include a new competition configuration")
            return self

        if not self.game or len(self.players) < 2:
            raise ValueError("new jobs require a game and at least two players")
        game_mode = "play" if self.mode == "championship" else self.mode
        if not game_supports_mode(self.game, game_mode):
            raise ValueError("game does not support selected mode")
        if self.mode == "series" and len(self.players) != 2:
            raise ValueError("series requires exactly two players")
        if self.mode == "round_robin" and not 3 <= len(self.players) <= MAX_PLATFORM_PLAYERS:
            raise ValueError("round_robin requires 3-16 players")
        if self.mode == "championship" and len(self.players) not in {4, 8, 16}:
            raise ValueError("championship requires exactly 4, 8, or 16 players")
        if self.mode != "play" and any(player.kind == "human" for player in self.players):
            raise ValueError("human players are supported only in play mode")
        identities = [player.identity_key() for player in self.players]
        if len(set(identities)) != len(identities):
            raise ValueError("player identities must be unique")
        if len({judge.cli_token() for judge in self.judges}) != len(self.judges):
            raise ValueError("judge identities must be unique")

        game_class = GAME_REGISTRY[self.game]
        requires_judges = bool(getattr(game_class, "requires_judge_panel", False))
        if requires_judges and not 3 <= len(self.judges) <= 9:
            raise ValueError("this game requires 3-9 judges")
        if not requires_judges and self.judges:
            raise ValueError("this game does not accept judges")

        kwargs: dict[str, int] = {}
        if self.rounds is not None:
            kwargs["rounds"] = self.rounds
        game = create_game(self.game, mode=game_mode, **kwargs)
        validate_player_count(game, 2 if self.mode == "championship" else len(self.players))
        return self

    def uses_profiles(self) -> bool:
        return any(player.kind == "profile" for player in self.players) or any(
            judge.kind == "profile" for judge in self.judges
        )


class ControlPreparedProfile(_ControlModel):
    """Disclosure-safe snapshot used to bind prepare and worker start."""

    profile_id: str
    display_name: str
    provider: Literal["openai", "ollama"]
    default_model: str
    effective_models: Annotated[tuple[str, ...], Field(min_length=1, max_length=25)]
    configuration_digest: str

    @field_validator("profile_id")
    @classmethod
    def validate_profile_id(cls, value: str) -> str:
        if _PROFILE_ID_RE.fullmatch(value) is None:
            raise ValueError("prepared profile ID is invalid")
        return value

    @field_validator("display_name", "default_model")
    @classmethod
    def validate_text(cls, value: str) -> str:
        candidate = value.strip()
        if not candidate or len(candidate) > 256:
            raise ValueError("prepared profile text is invalid")
        if any(
            ord(char) < 32
            or 0x7F <= ord(char) <= 0x9F
            or char in _BIDI_CONTROLS
            for char in candidate
        ):
            raise ValueError("prepared profile text contains unsafe controls")
        return candidate

    @field_validator("effective_models")
    @classmethod
    def validate_effective_models(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(cls.validate_text(item) for item in value)
        if normalized != tuple(sorted(set(normalized))):
            raise ValueError("effective Profile models must be unique and sorted")
        return normalized

    @field_validator("configuration_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("prepared profile digest is invalid")
        return value


class ControlPreview(_ControlModel):
    player_count: int
    human_count: int
    match_count: int
    pairing_count: int | None = None
    rated: bool
    requires_provider_budget: bool
    frozen_game: str | None = None
    frozen_players: Annotated[tuple[str, ...], Field(max_length=MAX_PLATFORM_PLAYERS)] = ()
    frozen_judges: Annotated[tuple[str, ...], Field(max_length=9)] = ()
    frozen_rounds: Annotated[int | None, Field(ge=1, le=100)] = None
    frozen_seed: str | None = None
    frozen_llm_timeout_seconds: Annotated[float | None, Field(gt=0, le=86_400)] = None
    uses_frozen_budget: bool = False
    prepared_profiles: Annotated[tuple[ControlPreparedProfile, ...], Field(max_length=25)] = ()
    warnings: tuple[str, ...] = ()

    @field_validator("frozen_game")
    @classmethod
    def validate_frozen_game(cls, value: str | None) -> str | None:
        if value is not None and value not in GAME_REGISTRY:
            raise ValueError("frozen game is invalid")
        return value

    @field_validator("frozen_players")
    @classmethod
    def validate_frozen_players(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_safe_name(item, label="frozen player name") for item in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("frozen player names must be unique")
        return normalized

    @field_validator("frozen_judges")
    @classmethod
    def validate_frozen_judges(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_safe_name(item, label="frozen judge name") for item in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("frozen judge names must be unique")
        return normalized

    @field_validator("frozen_seed")
    @classmethod
    def validate_frozen_seed(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _canonical_integer(value, minimum=_SQLITE_INT_MIN, maximum=_SQLITE_INT_MAX)

    @field_validator("prepared_profiles")
    @classmethod
    def validate_prepared_profiles(
        cls,
        value: tuple[ControlPreparedProfile, ...],
    ) -> tuple[ControlPreparedProfile, ...]:
        identifiers = tuple(item.profile_id for item in value)
        if identifiers != tuple(sorted(set(identifiers))):
            raise ValueError("prepared profiles must be unique and sorted")
        return value


class ControlProfileInfo(_ControlModel):
    profile_id: str
    display_name: str
    provider: Literal["openai", "ollama"]
    default_model: str | None
    credential_ready: bool


class ControlProfileCredentialRequest(_ControlModel):
    """One runtime-only credential accepted by the local admin control plane."""

    api_key: SecretStr

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if (
            not secret
            or len(secret.encode("utf-8")) > MAX_PROFILE_API_KEY_BYTES
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in secret)
        ):
            raise ValueError("api_key must contain printable ASCII without whitespace")
        return value


class ControlGameInfo(_ControlModel):
    name: str
    supported_modes: tuple[ControlMode, ...]
    requires_judge_panel: bool
    min_players: int
    max_players: int
    rounds_supported: bool


class ControlCatalogResponse(_ControlModel):
    api_version: Literal["v1"] = API_VERSION
    games: tuple[ControlGameInfo, ...]
    profiles: tuple[ControlProfileInfo, ...]
    mock_player_strategies: tuple[str, ...] = _MOCK_PLAYER_STRATEGIES
    mock_judge_strategies: tuple[str, ...] = _MOCK_JUDGE_STRATEGIES
    max_active_jobs: int = 1


class ControlParticipationLink(_ControlModel):
    player_name: str
    url: str

    @field_validator("player_name")
    @classmethod
    def validate_player_name(cls, value: str) -> str:
        return _safe_name(value, label="participation player name")

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        if not isinstance(value, str) or len(value) > 2048:
            raise ValueError("participation URL is invalid")
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise ValueError("participation URL is invalid") from exc
        path = parsed.path.split("/")
        fragment = parse_qs(parsed.fragment, keep_blank_values=True)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            # Every URL userinfo form, including ``:password@host``, gives
            # urllib a non-None username.  One check therefore rejects both
            # usernames and passwords without treating a rejected password as
            # application data.
            or parsed.username is not None
            or parsed.query
            or len(path) != 4
            or path[1] != "participate"
            or any(_SAFE_ID_RE.fullmatch(part) is None for part in path[2:])
            or set(fragment) != {"capability"}
            or len(fragment["capability"]) != 1
            or re.fullmatch(r"[A-Za-z0-9_-]{32,256}", fragment["capability"][0]) is None
        ):
            raise ValueError("participation URL is invalid")
        return value


class ControlJob(_ControlModel):
    job_id: str
    status: ControlJobStatus
    spec: ControlJobSpec
    preview: ControlPreview
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    live_id: str | None = None
    tournament_id: str | None = None
    championship_id: str | None = None
    final_kind: ControlFinalKind | None = None
    final_id: str | None = None
    final_match_ids: Annotated[tuple[str, ...], Field(max_length=4096)] = ()
    failure_code: ControlFailureCode | None = None
    resumable: bool = False
    participation_links: Annotated[
        tuple[ControlParticipationLink, ...], Field(max_length=MAX_PLATFORM_PLAYERS)
    ] = ()

    @field_validator("job_id")
    @classmethod
    def validate_job_id(cls, value: str) -> str:
        if _SAFE_ID_RE.fullmatch(value) is None:
            raise ValueError("job ID is invalid")
        return value

    @field_validator("live_id", "tournament_id", "championship_id", "final_id")
    @classmethod
    def validate_optional_id(cls, value: str | None) -> str | None:
        if value is not None and _SAFE_ID_RE.fullmatch(value) is None:
            raise ValueError("control result ID is invalid")
        return value

    @field_validator("final_match_ids")
    @classmethod
    def validate_final_match_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(_SAFE_ID_RE.fullmatch(item) is None for item in value):
            raise ValueError("final match ID is invalid")
        if len(set(value)) != len(value):
            raise ValueError("final match IDs must be unique")
        return value

    @model_validator(mode="after")
    def validate_final_shape(self) -> Self:
        present = self.final_kind is not None or self.final_id is not None or bool(
            self.final_match_ids
        )
        if present and (self.final_kind is None or self.final_id is None):
            raise ValueError("final result fields must be complete")
        if self.status == "completed" and not present:
            raise ValueError("completed jobs require a final result")
        if self.final_kind == "match" and (
            self.final_match_ids != (self.final_id,)
        ):
            raise ValueError("match result must reference its one archive")
        if self.final_kind == "series" and len(self.final_match_ids) != 2:
            raise ValueError("series result must reference two match archives")
        if self.final_kind == "tournament" and not self.final_match_ids:
            raise ValueError("tournament result must reference match archives")
        if self.final_kind == "championship" and not self.final_match_ids:
            raise ValueError("championship result must reference match archives")
        if self.final_kind == "championship" and (
            len(self.final_match_ids) not in {6, 14, 30}
            or len(self.final_match_ids) != self.preview.match_count
        ):
            raise ValueError(
                "championship result must contain every canonical bracket match"
            )
        expected_kind = {
            "play": "match",
            "series": "series",
            "round_robin": "tournament",
            "championship": "championship",
        }[self.spec.mode]
        if self.final_kind is not None and self.final_kind != expected_kind:
            raise ValueError("final result kind does not match the job mode")
        if self.final_kind == "championship" and (
            self.championship_id is None or self.final_id != self.championship_id
        ):
            raise ValueError("championship result must match the running championship ID")
        if self.spec.mode == "round_robin":
            if self.championship_id is not None:
                raise ValueError("round_robin jobs cannot carry a championship ID")
        elif self.tournament_id is not None:
            raise ValueError("only round_robin jobs can carry a tournament ID")
        if self.spec.mode != "championship" and self.championship_id is not None:
            raise ValueError("only championship jobs can carry a championship ID")
        return self


class ControlJobResponse(_ControlModel):
    api_version: Literal["v1"] = API_VERSION
    job: ControlJob


class ControlJobListResponse(_ControlModel):
    api_version: Literal["v1"] = API_VERSION
    jobs: tuple[ControlJob, ...]


def derive_jobs_database_path(archive_database: str | Path) -> Path:
    archive = Path(archive_database).expanduser()
    return archive.with_name(f"{archive.name}.jobs.db")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_jobs_directory_chain(directory: Path) -> None:
    if os.name != "posix":
        return
    current_uid = os.getuid()
    current = directory
    while True:
        info = current.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or current.is_symlink()
            or info.st_uid not in {0, current_uid}
            or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)
        ):
            # Every ancestor must be owned by this account (or root) and must
            # not be replaceable by another unprivileged OS account.  Checking
            # only the immediate parent would let an attacker rename a safe
            # child directory through an ancestor it owns.
            raise ControlError("control_unavailable")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _secure_jobs_file(path: Path) -> tuple[int, int]:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _validate_jobs_directory_chain(path.parent)
        flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
    except ControlError:
        raise
    except OSError as exc:
        raise ControlError("control_unavailable") from exc
    try:
        info = os.fstat(descriptor)
        link_info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or not stat.S_ISREG(link_info.st_mode)
            or path.is_symlink()
            or info.st_dev != link_info.st_dev
            or info.st_ino != link_info.st_ino
            or info.st_nlink != 1
            or link_info.st_nlink != 1
        ):
            raise ControlError("control_unavailable")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise ControlError("control_unavailable")
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        else:  # pragma: no cover - Windows permission semantics.
            path.chmod(0o600)
        return (info.st_dev, info.st_ino)
    except OSError as exc:
        raise ControlError("control_unavailable") from exc
    finally:
        os.close(descriptor)


class JobStore:
    """Small private SQLite state machine for local Web-created jobs."""

    def __init__(self, archive_database: str | Path) -> None:
        self.archive_database = Path(archive_database).expanduser().resolve()
        self.path = derive_jobs_database_path(self.archive_database)
        self._file_identity = _secure_jobs_file(self.path)
        with self._database() as connection:
            self._initialize(connection)
            self._cleanup(connection)

    def _validate_file_identity(self) -> None:
        try:
            _validate_jobs_directory_chain(self.path.parent)
        except OSError as exc:
            raise ControlError("control_unavailable") from exc
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags)
        except OSError as exc:
            raise ControlError("control_unavailable") from exc
        try:
            info = os.fstat(descriptor)
            link_info = self.path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or not stat.S_ISREG(link_info.st_mode)
                or self.path.is_symlink()
                or (info.st_dev, info.st_ino) != self._file_identity
                or (link_info.st_dev, link_info.st_ino) != self._file_identity
                or info.st_nlink != 1
                or link_info.st_nlink != 1
                or (os.name == "posix" and stat.S_IMODE(info.st_mode) != 0o600)
                or (hasattr(os, "getuid") and info.st_uid != os.getuid())
            ):
                raise ControlError("control_unavailable")
        except OSError as exc:
            raise ControlError("control_unavailable") from exc
        finally:
            os.close(descriptor)

    def _connect(self) -> sqlite3.Connection:
        self._validate_file_identity()
        connection = sqlite3.connect(self.path, timeout=2.0, isolation_level=None)
        try:
            self._validate_file_identity()
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=2000")
            connection.execute("PRAGMA secure_delete=ON")
            connection.execute("PRAGMA journal_mode=DELETE")
            return connection
        except Exception:
            connection.close()
            raise

    @contextmanager
    def _database(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            yield connection
        except ControlError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise ControlError("control_unavailable") from exc
        finally:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass

    @staticmethod
    def _create_current_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS control_jobs (
                job_id TEXT PRIMARY KEY,
                prepare_key TEXT NOT NULL UNIQUE,
                request_digest TEXT NOT NULL,
                spec_json TEXT NOT NULL,
                preview_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                live_id TEXT,
                tournament_id TEXT,
                championship_id TEXT,
                final_kind TEXT,
                final_id TEXT,
                final_match_ids_json TEXT NOT NULL DEFAULT '[]',
                failure_code TEXT,
                child_pid INTEGER,
                CHECK (status IN (
                    'prepared','starting','running','finalizing','cancel_requested',
                    'cancelled','completed','failed','interrupted'
                ))
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS control_operations (
                idempotency_key TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (job_id) REFERENCES control_jobs(job_id)
            )
            """
        )

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, CONTROL_SCHEMA_VERSION):
            raise ControlError("control_unavailable")
        connection.execute("BEGIN IMMEDIATE")
        try:
            if version == 1:
                tables = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_schema "
                        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
                job_columns = tuple(
                    tuple(row[1:])
                    for row in connection.execute("PRAGMA table_info(control_jobs)")
                )
                operation_columns = tuple(
                    tuple(row[1:])
                    for row in connection.execute("PRAGMA table_info(control_operations)")
                )
                operation_foreign_keys = tuple(
                    tuple(row[2:])
                    for row in connection.execute(
                        "PRAGMA foreign_key_list(control_operations)"
                    )
                )
                job_indexes = tuple(
                    sorted(
                        (row["unique"], row["origin"], row["partial"])
                        for row in connection.execute("PRAGMA index_list(control_jobs)")
                    )
                )
                operation_indexes = tuple(
                    sorted(
                        (row["unique"], row["origin"], row["partial"])
                        for row in connection.execute(
                            "PRAGMA index_list(control_operations)"
                        )
                    )
                )
                if (
                    tables != {"control_jobs", "control_operations"}
                    or job_columns != _CONTROL_JOBS_V1_COLUMNS
                    or operation_columns != _CONTROL_OPERATIONS_COLUMNS
                    or operation_foreign_keys
                    != (("control_jobs", "job_id", "job_id", "NO ACTION", "NO ACTION", "NONE"),)
                    or job_indexes != ((1, "pk", 0), (1, "u", 0))
                    or operation_indexes != ((1, "pk", 0),)
                ):
                    raise ControlError("control_unavailable")
                legacy_rows = connection.execute(
                    "SELECT job_id, spec_json, request_digest FROM control_jobs"
                ).fetchall()
                for legacy_row in legacy_rows:
                    raw_spec = legacy_row["spec_json"]
                    if (
                        not isinstance(raw_spec, str)
                        or hashlib.sha256(raw_spec.encode()).hexdigest()
                        != legacy_row["request_digest"]
                    ):
                        raise ControlError("control_unavailable")
                    legacy_payload = json.loads(
                        raw_spec,
                        parse_constant=_reject_nonfinite_json,
                    )
                    if (
                        not isinstance(legacy_payload, dict)
                        or "resume_championship_id" in legacy_payload
                    ):
                        raise ControlError("control_unavailable")
                    legacy_payload["resume_championship_id"] = None
                    normalized_spec = ControlJobSpec.model_validate(legacy_payload)
                    normalized_json = _canonical_json(
                        normalized_spec.model_dump(mode="json")
                    )
                    connection.execute(
                        "UPDATE control_jobs SET spec_json = ?, request_digest = ? "
                        "WHERE job_id = ?",
                        (
                            normalized_json,
                            hashlib.sha256(normalized_json.encode()).hexdigest(),
                            legacy_row["job_id"],
                        ),
                    )
                connection.execute(
                    "ALTER TABLE control_jobs ADD COLUMN championship_id TEXT"
                )
            JobStore._create_current_schema(connection)
            current_columns = tuple(
                tuple(row[1:])
                for row in connection.execute("PRAGMA table_info(control_jobs)")
            )
            current_operation_columns = tuple(
                tuple(row[1:])
                for row in connection.execute("PRAGMA table_info(control_operations)")
            )
            if (
                current_columns
                not in {_CONTROL_JOBS_V2_COLUMNS, _CONTROL_JOBS_V2_MIGRATED_COLUMNS}
                or current_operation_columns != _CONTROL_OPERATIONS_COLUMNS
            ):
                raise ControlError("control_unavailable")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ControlError("control_unavailable")
            connection.execute(f"PRAGMA user_version={CONTROL_SCHEMA_VERSION}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _row_to_job(self, row: sqlite3.Row) -> ControlJob:
        try:
            spec = ControlJobSpec.model_validate_json(row["spec_json"])
            preview = ControlPreview.model_validate_json(row["preview_json"])
            final_ids = json.loads(row["final_match_ids_json"])
            resumable = False
            if (
                row["status"] in {"cancelled", "failed", "interrupted"}
                and spec.mode in {"round_robin", "championship"}
            ):
                resume_id = (
                    row["tournament_id"]
                    if spec.mode == "round_robin"
                    else row["championship_id"]
                )
                try:
                    if resume_id is None:
                        raise ControlError("resume_unavailable")
                    if spec.mode == "round_robin":
                        _resume_preview(self.archive_database, resume_id)
                    else:
                        _resume_championship_preview(self.archive_database, resume_id)
                except ControlError:
                    pass
                else:
                    resumable = True
            return ControlJob(
                job_id=row["job_id"],
                status=row["status"],
                spec=spec,
                preview=preview,
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                live_id=row["live_id"],
                tournament_id=row["tournament_id"],
                championship_id=row["championship_id"],
                final_kind=row["final_kind"],
                final_id=row["final_id"],
                final_match_ids=tuple(final_ids),
                failure_code=row["failure_code"],
                resumable=resumable,
            )
        except (IndexError, KeyError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ControlError("control_unavailable") from exc

    @staticmethod
    def _cleanup(connection: sqlite3.Connection, *, now: str | None = None) -> None:
        current = datetime.now(UTC) if now is None else datetime.fromisoformat(now)
        current_text = current.isoformat(timespec="microseconds").replace("+00:00", "Z")
        prepared_cutoff = (current - timedelta(seconds=PREPARED_JOB_RETENTION_SECONDS))
        prepared_cutoff_text = prepared_cutoff.isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        retention_cutoff = current - timedelta(seconds=JOB_RETENTION_SECONDS)
        retention_cutoff_text = retention_cutoff.isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        connection.execute(
            "UPDATE control_jobs SET status = 'cancelled', updated_at = ?, "
            "finished_at = ?, failure_code = NULL "
            "WHERE status = 'prepared' AND created_at < ?",
            (current_text, current_text, prepared_cutoff_text),
        )
        expired = connection.execute(
            "SELECT job_id FROM control_jobs WHERE status IN "
            "('cancelled','completed','failed','interrupted') AND finished_at < ?",
            (retention_cutoff_text,),
        ).fetchall()
        if not expired:
            return
        job_ids = tuple(row["job_id"] for row in expired)
        placeholders = ",".join("?" for _ in job_ids)
        connection.execute(
            f"DELETE FROM control_operations WHERE job_id IN ({placeholders})",  # noqa: S608
            job_ids,
        )
        connection.execute(
            f"DELETE FROM control_jobs WHERE job_id IN ({placeholders})",  # noqa: S608
            job_ids,
        )

    def prepare(
        self,
        spec: ControlJobSpec,
        preview: ControlPreview,
        *,
        idempotency_key: str,
    ) -> ControlJob:
        if _IDEMPOTENCY_RE.fullmatch(idempotency_key) is None:
            raise ControlError("invalid_request")
        spec_json = _canonical_json(spec.model_dump(mode="json"))
        preview_json = _canonical_json(preview.model_dump(mode="json"))
        digest = hashlib.sha256(spec_json.encode()).hexdigest()
        now = _utc_now()
        job_id = os.urandom(16).hex()
        with self._database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._cleanup(connection, now=now)
                existing = connection.execute(
                    "SELECT * FROM control_jobs WHERE prepare_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    if existing["request_digest"] != digest:
                        raise ControlError("idempotency_conflict")
                    connection.commit()
                    return self._row_to_job(existing)
                count = connection.execute(
                    "SELECT COUNT(*) FROM control_jobs WHERE status NOT IN "
                    "('cancelled','completed','failed','interrupted')"
                ).fetchone()[0]
                if count >= MAX_PREPARED_JOBS:
                    raise ControlError("job_capacity")
                connection.execute(
                    """
                    INSERT INTO control_jobs (
                        job_id, prepare_key, request_digest, spec_json, preview_json,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'prepared', ?, ?)
                    """,
                    (job_id, idempotency_key, digest, spec_json, preview_json, now, now),
                )
                row = connection.execute(
                    "SELECT * FROM control_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self._row_to_job(row)

    def expire_stale_jobs(self) -> None:
        """Apply bounded draft/terminal retention before a control action."""

        with self._database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._cleanup(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def get(self, job_id: str) -> ControlJob:
        if _SAFE_ID_RE.fullmatch(job_id) is None:
            raise ControlError("job_not_found")
        with self._database() as connection:
            row = connection.execute(
                "SELECT * FROM control_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise ControlError("job_not_found")
        return self._row_to_job(row)

    def list(self, *, limit: int = 20) -> tuple[ControlJob, ...]:
        if isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ControlError("invalid_request")
        with self._database() as connection:
            rows = connection.execute(
                "SELECT * FROM control_jobs ORDER BY created_at DESC, job_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(self._row_to_job(row) for row in rows)

    def child_pid(self, job_id: str) -> int | None:
        """Return the private worker PID for liveness checks, never for signaling."""

        if _SAFE_ID_RE.fullmatch(job_id) is None:
            raise ControlError("job_not_found")
        with self._database() as connection:
            row = connection.execute(
                "SELECT child_pid FROM control_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise ControlError("job_not_found")
        value = row["child_pid"]
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise ControlError("control_unavailable")
        return value

    def claim_operation(
        self,
        job_id: str,
        operation: str,
        idempotency_key: str,
    ) -> bool:
        if _IDEMPOTENCY_RE.fullmatch(idempotency_key) is None:
            raise ControlError("invalid_request")
        digest = hashlib.sha256(f"{operation}:{job_id}".encode()).hexdigest()
        with self._database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM control_operations WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if row is not None:
                    if (
                        row["job_id"] != job_id
                        or row["operation"] != operation
                        or row["request_digest"] != digest
                    ):
                        raise ControlError("idempotency_conflict")
                    connection.commit()
                    return False
                connection.execute(
                    "INSERT INTO control_operations VALUES (?, ?, ?, ?, ?)",
                    (idempotency_key, job_id, operation, digest, _utc_now()),
                )
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def transition(
        self,
        job_id: str,
        *,
        expected: Iterable[ControlJobStatus],
        status: ControlJobStatus,
        started_at: str | None = None,
        finished_at: str | None = None,
        live_id: str | None = None,
        tournament_id: str | None = None,
        championship_id: str | None = None,
        final_kind: ControlFinalKind | None = None,
        final_id: str | None = None,
        final_match_ids: Sequence[str] | None = None,
        failure_code: str | None = None,
        child_pid: int | None = None,
    ) -> ControlJob:
        expected_values = tuple(expected)
        if not expected_values:
            raise ValueError("expected statuses must not be empty")
        placeholders = ",".join("?" for _ in expected_values)
        values: list[object] = [status, _utc_now()]
        assignments = ["status = ?", "updated_at = ?"]
        for column, value in (
            ("started_at", started_at),
            ("finished_at", finished_at),
            ("live_id", live_id),
            ("tournament_id", tournament_id),
            ("championship_id", championship_id),
            ("final_kind", final_kind),
            ("final_id", final_id),
            ("failure_code", failure_code),
            ("child_pid", child_pid),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                values.append(value)
        if final_match_ids is not None:
            assignments.append("final_match_ids_json = ?")
            values.append(_canonical_json(list(final_match_ids)))
        values.extend((job_id, *expected_values))
        with self._database() as connection:
            cursor = connection.execute(
                f"UPDATE control_jobs SET {', '.join(assignments)} "  # noqa: S608 - columns fixed
                f"WHERE job_id = ? AND status IN ({placeholders})",
                values,
            )
        if cursor.rowcount != 1:
            current = self.get(job_id)
            if current.status == status or current.status in _TERMINAL_STATUSES:
                return current
            raise ControlError("job_conflict")
        return self.get(job_id)


def _profile_ready(
    profile: ProviderProfile,
    credential_ready: ProfileCredentialReady | None = None,
) -> bool:
    if not profile.default_model:
        return False
    if profile.provider == "ollama":
        return True
    if credential_ready is not None:
        return bool(credential_ready(profile))
    return bool(profile.api_key_env and os.environ.get(profile.api_key_env))


def _credential_free_profile_base_url(profile: ProviderProfile) -> str | None:
    """Return a validated endpoint that cannot carry embedded credentials."""

    if profile.base_url is None:
        return None
    validate_base_url(
        profile.base_url,
        source=f"Provider Profile {profile.profile_id!r} 的 base_url",
        require_https_for_remote=profile.provider == "openai",
    )
    # Bind the exact configured spelling, not merely validate a canonicalized
    # equivalent, so any edit between prepare and start changes the digest.
    return profile.base_url


def profile_configuration_digest(profile: ProviderProfile) -> str:
    """Hash a credential-free Profile configuration for prepare/start binding."""

    payload = {
        "api_key_env": profile.api_key_env,
        "base_url": _credential_free_profile_base_url(profile),
        "default_model": profile.default_model,
        "display_name": profile.display_name,
        "profile_id": profile.profile_id,
        "provider": profile.provider,
    }
    encoded = _canonical_json(payload).encode("utf-8")
    # ``api_key_env`` is a validated environment-variable *name*, never its
    # value, and the endpoint above has already rejected credential-bearing
    # URL forms.  SHA-256 is intentionally a collision-resistant TOCTOU
    # fingerprint here, not a password-storage primitive.
    return hashlib.sha256(b"llmolympic-profile-configuration-v1\0" + encoded).hexdigest()


def _prepared_profile(
    profile: ProviderProfile,
    *,
    effective_models: Sequence[str] | None = None,
) -> ControlPreparedProfile:
    if not profile.default_model:
        raise ControlError("profile_unavailable")
    try:
        return ControlPreparedProfile(
            profile_id=profile.profile_id,
            display_name=profile.display_name or profile.profile_id,
            provider=profile.provider,
            default_model=profile.default_model,
            effective_models=(
                (profile.default_model,)
                if effective_models is None
                else tuple(sorted(set(effective_models)))
            ),
            configuration_digest=profile_configuration_digest(profile),
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise ControlError("profile_unavailable") from exc


def control_catalog(
    *,
    credential_ready: ProfileCredentialReady | None = None,
) -> ControlCatalogResponse:
    try:
        profiles = load_profiles()
    except (OSError, TypeError, ValueError) as exc:
        raise ControlError("catalog_unavailable") from exc
    profile_items = tuple(
        ControlProfileInfo(
            profile_id=profile.profile_id,
            display_name=profile.display_name or profile.profile_id,
            provider=profile.provider,
            default_model=profile.default_model,
            credential_ready=_profile_ready(profile, credential_ready),
        )
        for profile in sorted(profiles.values(), key=lambda item: item.profile_id)
    )
    games: list[ControlGameInfo] = []
    for name, game_class in sorted(GAME_REGISTRY.items()):
        declared_maximum = getattr(game_class, "max_players", None)
        game_info = GameInfo.from_game(name, game_class)
        supported_modes: tuple[ControlMode, ...] = tuple(game_info.supported_modes)
        games.append(
            ControlGameInfo(
                name=name,
                supported_modes=supported_modes,
                requires_judge_panel=bool(
                    getattr(game_class, "requires_judge_panel", False)
                ),
                min_players=max(2, int(getattr(game_class, "min_players", 2))),
                max_players=(
                    MAX_PLATFORM_PLAYERS
                    if declared_maximum is None
                    else min(MAX_PLATFORM_PLAYERS, int(declared_maximum))
                ),
                rounds_supported=name
                in {"math_quiz", "knowledge_quiz", "reasoning_quiz", "riddle_quiz"},
            )
        )
    return ControlCatalogResponse(games=games, profiles=profile_items)


def _reject_nonfinite_json(value: str) -> None:
    del value
    raise ValueError("non-finite JSON value")


def _resume_preview(
    archive_database: str | Path,
    tournament_id: str,
    *,
    credential_ready: ProfileCredentialReady | None = None,
) -> ControlPreview:
    """Read and validate a checkpoint summary without mutating the archive DB."""

    try:
        path = Path(archive_database).expanduser().resolve(strict=False)
        if not path.is_file():
            raise ControlError("resume_unavailable")
        uri = f"file:{quote(str(path), safe='/')}?mode=ro"
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=2.0,
            isolation_level=None,
        )
    except ControlError:
        raise
    except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
        raise ControlError("resume_unavailable") from exc

    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=2000")
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ControlError("resume_unavailable")
        table = connection.execute(
            "SELECT type FROM sqlite_schema WHERE name = 'tournament_checkpoints'"
        ).fetchone()
        if table is None or table["type"] != "table":
            raise ControlError("resume_unavailable")
        row = connection.execute(
            """
            SELECT tournament_id, game, seed, players_json, game_config_json,
                   schedule_json, pairing_count, updated_at, status, config_json
            FROM tournament_checkpoints
            WHERE tournament_id = ?
            """,
            (tournament_id,),
        ).fetchone()
        if row is None or row["status"] != "in_progress":
            raise ControlError("resume_unavailable")
        lease = connection.execute(
            "SELECT token_digest, expires_at_epoch "
            "FROM tournament_runner_leases WHERE tournament_id = ?",
            (tournament_id,),
        ).fetchone()
        if lease is not None and lease["token_digest"] is not None:
            token_digest = lease["token_digest"]
            expires_at_epoch = lease["expires_at_epoch"]
            if (
                not isinstance(token_digest, bytes)
                or len(token_digest) != 32
                or isinstance(expires_at_epoch, bool)
                or not isinstance(expires_at_epoch, int)
            ):
                raise ControlError("resume_unavailable")
            if expires_at_epoch > int(datetime.now(UTC).timestamp()):
                raise ControlError("resume_unavailable")
        raw_config = row["config_json"]
        if not isinstance(raw_config, str) or len(raw_config.encode("utf-8")) > 1_048_576:
            raise ControlError("resume_unavailable")
        payload = json.loads(raw_config, parse_constant=_reject_nonfinite_json)
        if not isinstance(payload, dict):
            raise ControlError("resume_unavailable")
        payload["updated_at"] = row["updated_at"]
        payload["completed_series"] = []
        checkpoint = TournamentCheckpoint.model_validate(payload)
        if (
            checkpoint.tournament_id != row["tournament_id"]
            or checkpoint.tournament_id != tournament_id
            or checkpoint.game != row["game"]
            or checkpoint.seed != row["seed"]
            or len(checkpoint.schedule) != row["pairing_count"]
            or _canonical_json(checkpoint.players) != row["players_json"]
            or _canonical_json(checkpoint.game_config) != row["game_config_json"]
            or _canonical_json(
                tuple(item.model_dump(mode="json") for item in checkpoint.schedule)
            )
            != row["schedule_json"]
        ):
            raise ControlError("resume_unavailable")
        names = tuple(
            _safe_name(descriptor.get("display_name") or descriptor.get("name"), label="player")
            for descriptor in checkpoint.players
        )
        timeout_values = tuple(
            descriptor.get("move_timeout_seconds") for descriptor in checkpoint.players
        )
        if any(value != timeout_values[0] for value in timeout_values[1:]):
            raise ControlError("resume_unavailable")
        llm_timeout = timeout_values[0]
        if llm_timeout is not None and (
            isinstance(llm_timeout, bool)
            or not isinstance(llm_timeout, (int, float))
            or not math.isfinite(llm_timeout)
            or not 0 < llm_timeout <= 86_400
        ):
            raise ControlError("resume_unavailable")
        judge_labels: tuple[str, ...] = ()
        if checkpoint.judge_panel is not None:
            judge_labels = tuple(
                _safe_name(
                    (
                        f"Profile · {item.profile_id}"
                        if item.profile_id is not None
                        else f"{item.provider}:{item.model}"
                    ),
                    label="judge",
                )
                for item in checkpoint.judge_panel.panel
            )
        rounds = checkpoint.game_config.get("rounds")
        if rounds is not None and (
            isinstance(rounds, bool) or not isinstance(rounds, int) or not 1 <= rounds <= 100
        ):
            raise ControlError("resume_unavailable")
        budget_row = connection.execute(
            "SELECT count(*) AS count FROM provider_budgets WHERE tournament_id = ?",
            (tournament_id,),
        ).fetchone()
        has_budget = budget_row is not None and budget_row["count"] == 1
        if budget_row is None or budget_row["count"] not in (0, 1):
            raise ControlError("resume_unavailable")
        providers: list[str] = []
        profile_ids: set[str] = set()
        profile_models: dict[str, set[str]] = {}
        for descriptor in checkpoint.players:
            provider = descriptor.get("provider")
            if not isinstance(provider, str) or not provider:
                raise ControlError("resume_unavailable")
            providers.append(provider)
            profile_id = descriptor.get("profile_id")
            if profile_id is not None:
                if not isinstance(profile_id, str) or _PROFILE_ID_RE.fullmatch(profile_id) is None:
                    raise ControlError("resume_unavailable")
                profile_ids.add(profile_id)
                model = descriptor.get("model")
                if not isinstance(model, str) or not model:
                    raise ControlError("resume_unavailable")
                profile_models.setdefault(profile_id, set()).add(model)
            elif provider != "mock":
                # The local Web control plane only resumes named Profiles.  A
                # legacy direct-provider checkpoint can still be resumed from
                # the CLI, but it has no safe configuration identity to bind
                # across Web prepare/start.
                raise ControlError("resume_unavailable")
        if checkpoint.judge_panel is not None:
            for descriptor in checkpoint.judge_panel.panel:
                providers.append(descriptor.provider)
                if descriptor.profile_id is not None:
                    if _PROFILE_ID_RE.fullmatch(descriptor.profile_id) is None:
                        raise ControlError("resume_unavailable")
                    profile_ids.add(descriptor.profile_id)
                    profile_models.setdefault(descriptor.profile_id, set()).add(
                        descriptor.model
                    )
                elif descriptor.provider != "mock":
                    raise ControlError("resume_unavailable")
        requires_budget = any(provider != "mock" for provider in providers)
        if requires_budget and not has_budget:
            # A resume inherits its immutable checkpoint budget; the Web API
            # deliberately has no path to attach a new budget after creation.
            raise ControlError("resume_unavailable")
        try:
            profiles = load_profiles() if profile_ids else {}
        except (OSError, TypeError, ValueError) as exc:
            raise ControlError("profile_unavailable") from exc
        prepared_profiles: list[ControlPreparedProfile] = []
        for profile_id in sorted(profile_ids):
            profile = profiles.get(profile_id)
            if profile is None or not _profile_ready(profile, credential_ready):
                raise ControlError("profile_unavailable")
            prepared_profiles.append(
                _prepared_profile(
                    profile,
                    effective_models=tuple(profile_models[profile_id]),
                )
            )
        pairing_count = len(checkpoint.schedule)
        return ControlPreview(
            player_count=len(checkpoint.players),
            human_count=0,
            match_count=pairing_count * 2,
            pairing_count=pairing_count,
            rated=True,
            requires_provider_budget=requires_budget,
            frozen_game=checkpoint.game,
            frozen_players=names,
            frozen_judges=judge_labels,
            frozen_rounds=rounds,
            frozen_seed=str(checkpoint.seed),
            frozen_llm_timeout_seconds=(
                None if llm_timeout is None else float(llm_timeout)
            ),
            uses_frozen_budget=has_budget,
            prepared_profiles=tuple(prepared_profiles),
            warnings=("resume_uses_frozen_configuration",),
        )
    except ControlError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError, sqlite3.Error) as exc:
        raise ControlError("resume_unavailable") from exc
    finally:
        connection.close()


def _resume_championship_preview(
    archive_database: str | Path,
    championship_id: str,
    *,
    credential_ready: ProfileCredentialReady | None = None,
) -> ControlPreview:
    """Read and validate one knockout checkpoint without mutating the archive DB."""

    try:
        path = Path(archive_database).expanduser().resolve(strict=False)
        if not path.is_file():
            raise ControlError("resume_unavailable")
        uri = f"file:{quote(str(path), safe='/')}?mode=ro"
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=2.0,
            isolation_level=None,
        )
    except ControlError:
        raise
    except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
        raise ControlError("resume_unavailable") from exc

    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=2000")
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ControlError("resume_unavailable")
        for table_name in (
            "championship_checkpoints",
            "championship_checkpoint_series",
            "championship_runner_leases",
            "provider_budgets",
        ):
            table = connection.execute(
                "SELECT type FROM sqlite_schema WHERE name = ?",
                (table_name,),
            ).fetchone()
            if table is None or table["type"] != "table":
                raise ControlError("resume_unavailable")

        row = connection.execute(
            """
            SELECT championship_id, game, seed, players_json, game_config_json,
                   schedule_json, max_attempts, pairing_count, created_at,
                   updated_at, status, finalized_at, final_championship_id,
                   config_json
            FROM championship_checkpoints
            WHERE championship_id = ?
            """,
            (championship_id,),
        ).fetchone()
        if (
            row is None
            or row["status"] != "in_progress"
            or row["finalized_at"] is not None
            or row["final_championship_id"] is not None
        ):
            raise ControlError("resume_unavailable")

        lease = connection.execute(
            "SELECT token_digest, expires_at_epoch "
            "FROM championship_runner_leases WHERE championship_id = ?",
            (championship_id,),
        ).fetchone()
        if lease is not None and lease["token_digest"] is not None:
            token_digest = lease["token_digest"]
            expires_at_epoch = lease["expires_at_epoch"]
            if (
                not isinstance(token_digest, bytes)
                or len(token_digest) != 32
                or isinstance(expires_at_epoch, bool)
                or not isinstance(expires_at_epoch, int)
            ):
                raise ControlError("resume_unavailable")
            if expires_at_epoch > int(datetime.now(UTC).timestamp()):
                raise ControlError("resume_unavailable")

        raw_config = row["config_json"]
        if not isinstance(raw_config, str) or len(raw_config.encode("utf-8")) > 1_048_576:
            raise ControlError("resume_unavailable")
        config_payload = json.loads(raw_config, parse_constant=_reject_nonfinite_json)
        if (
            not isinstance(config_payload, dict)
            or "completed_series" in config_payload
            or "updated_at" in config_payload
        ):
            raise ControlError("resume_unavailable")

        series_rows = connection.execute(
            """
            SELECT pairing_number, series_id, match_1_id, match_2_id,
                   completed_at, series_json
            FROM championship_checkpoint_series
            WHERE championship_id = ?
            ORDER BY pairing_number
            """,
            (championship_id,),
        ).fetchall()
        completed_payloads: list[object] = []
        for expected_pairing, series_row in enumerate(series_rows, start=1):
            raw_series = series_row["series_json"]
            if (
                series_row["pairing_number"] != expected_pairing
                or not isinstance(raw_series, str)
                or len(raw_series.encode("utf-8")) > 1_048_576
            ):
                raise ControlError("resume_unavailable")
            completed_payloads.append(
                json.loads(raw_series, parse_constant=_reject_nonfinite_json)
            )

        payload = dict(config_payload)
        payload["updated_at"] = row["updated_at"]
        payload["completed_series"] = completed_payloads
        checkpoint = ChampionshipCheckpoint.model_validate(payload)
        expected_config = checkpoint.model_dump(mode="json")
        expected_config.pop("completed_series")
        expected_config.pop("updated_at")
        if (
            checkpoint.championship_id != row["championship_id"]
            or checkpoint.championship_id != championship_id
            or checkpoint.game != row["game"]
            or checkpoint.seed != row["seed"]
            or checkpoint.max_attempts != row["max_attempts"]
            or len(checkpoint.schedule) != row["pairing_count"]
            or _canonical_json(checkpoint.players) != row["players_json"]
            or _canonical_json(checkpoint.game_config) != row["game_config_json"]
            or _canonical_json(
                tuple(item.model_dump(mode="json") for item in checkpoint.schedule)
            )
            != row["schedule_json"]
            or _canonical_json(expected_config) != _canonical_json(config_payload)
            or datetime.fromisoformat(row["created_at"]) != checkpoint.created_at
            or datetime.fromisoformat(row["updated_at"]) != checkpoint.updated_at
        ):
            raise ControlError("resume_unavailable")
        for series_row, series in zip(series_rows, checkpoint.completed_series):
            if (
                series_row["series_id"] != series.series_id
                or series_row["match_1_id"] != series.legs[0].match_id
                or series_row["match_2_id"] != series.legs[1].match_id
                or datetime.fromisoformat(series_row["completed_at"]) != series.finished_at
                or _canonical_json(series.model_dump(mode="json"))
                != _canonical_json(
                    json.loads(
                        series_row["series_json"],
                        parse_constant=_reject_nonfinite_json,
                    )
                )
            ):
                raise ControlError("resume_unavailable")

        names = tuple(
            _safe_name(descriptor.get("display_name") or descriptor.get("name"), label="player")
            for descriptor in checkpoint.players
        )
        timeout_values = tuple(
            descriptor.get("move_timeout_seconds") for descriptor in checkpoint.players
        )
        if any(value != timeout_values[0] for value in timeout_values[1:]):
            raise ControlError("resume_unavailable")
        llm_timeout = timeout_values[0]
        if llm_timeout is not None and (
            isinstance(llm_timeout, bool)
            or not isinstance(llm_timeout, (int, float))
            or not math.isfinite(llm_timeout)
            or not 0 < llm_timeout <= 86_400
        ):
            raise ControlError("resume_unavailable")

        judge_labels: tuple[str, ...] = ()
        if checkpoint.judge_panel is not None:
            judge_labels = tuple(
                _safe_name(
                    (
                        f"Profile · {item.profile_id}"
                        if item.profile_id is not None
                        else f"{item.provider}:{item.model}"
                    ),
                    label="judge",
                )
                for item in checkpoint.judge_panel.panel
            )
        rounds = checkpoint.game_config.get("rounds")
        if rounds is not None and (
            isinstance(rounds, bool) or not isinstance(rounds, int) or not 1 <= rounds <= 100
        ):
            raise ControlError("resume_unavailable")

        providers: list[str] = []
        expected_route_ids: set[str] = set()
        profile_ids: set[str] = set()
        profile_models: dict[str, set[str]] = {}
        for descriptor in checkpoint.players:
            provider = descriptor.get("provider")
            if not isinstance(provider, str) or not provider:
                raise ControlError("resume_unavailable")
            providers.append(provider)
            route_id = descriptor.get("route_id")
            if not isinstance(route_id, str) or not route_id:
                raise ControlError("resume_unavailable")
            expected_route_ids.add(route_id)
            profile_id = descriptor.get("profile_id")
            if profile_id is not None:
                if not isinstance(profile_id, str) or _PROFILE_ID_RE.fullmatch(profile_id) is None:
                    raise ControlError("resume_unavailable")
                profile_ids.add(profile_id)
                model = descriptor.get("model")
                if not isinstance(model, str) or not model:
                    raise ControlError("resume_unavailable")
                profile_models.setdefault(profile_id, set()).add(model)
            elif provider != "mock":
                raise ControlError("resume_unavailable")
        if checkpoint.judge_panel is not None:
            for descriptor in checkpoint.judge_panel.panel:
                providers.append(descriptor.provider)
                if descriptor.route_id is None:
                    raise ControlError("resume_unavailable")
                expected_route_ids.add(descriptor.route_id)
                if descriptor.profile_id is not None:
                    if _PROFILE_ID_RE.fullmatch(descriptor.profile_id) is None:
                        raise ControlError("resume_unavailable")
                    profile_ids.add(descriptor.profile_id)
                    profile_models.setdefault(descriptor.profile_id, set()).add(
                        descriptor.model
                    )
                elif descriptor.provider != "mock":
                    raise ControlError("resume_unavailable")

        budget_rows = connection.execute(
            """
            SELECT budget_id, policy_json, policy_digest, limit_calls,
                   limit_input_tokens, limit_output_tokens,
                   limit_estimated_cost_nanos, poison_reason_code,
                   finalized_at_epoch
            FROM provider_budgets
            WHERE championship_id = ?
            """,
            (championship_id,),
        ).fetchall()
        if len(budget_rows) > 1:
            raise ControlError("resume_unavailable")
        has_budget = len(budget_rows) == 1
        requires_budget = any(provider != "mock" for provider in providers)
        if requires_budget:
            if not has_budget:
                raise ControlError("resume_unavailable")
            budget_row = budget_rows[0]
            limits = (
                budget_row["limit_calls"],
                budget_row["limit_input_tokens"],
                budget_row["limit_output_tokens"],
                budget_row["limit_estimated_cost_nanos"],
            )
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in limits
            ):
                raise ControlError("resume_unavailable")
            if (
                budget_row["poison_reason_code"] is not None
                or budget_row["finalized_at_epoch"] is not None
            ):
                raise ControlError("resume_unavailable")
            try:
                policy = ProviderBudgetPolicy.from_canonical_json(budget_row["policy_json"])
            except (TypeError, ValueError, UsageValidationError) as exc:
                raise ControlError("resume_unavailable") from exc
            if (
                policy.digest != budget_row["policy_digest"]
                or {route.route_id for route in policy.routes} != expected_route_ids
                or any(route.price is None for route in policy.routes)
            ):
                raise ControlError("resume_unavailable")

        try:
            profiles = load_profiles() if profile_ids else {}
        except (OSError, TypeError, ValueError) as exc:
            raise ControlError("profile_unavailable") from exc
        prepared_profiles: list[ControlPreparedProfile] = []
        for profile_id in sorted(profile_ids):
            profile = profiles.get(profile_id)
            if profile is None or not _profile_ready(profile, credential_ready):
                raise ControlError("profile_unavailable")
            prepared_profiles.append(
                _prepared_profile(
                    profile,
                    effective_models=tuple(profile_models[profile_id]),
                )
            )

        pairing_count = len(checkpoint.schedule)
        return ControlPreview(
            player_count=len(checkpoint.players),
            human_count=0,
            match_count=pairing_count * 2,
            pairing_count=pairing_count,
            rated=False,
            requires_provider_budget=requires_budget,
            frozen_game=checkpoint.game,
            frozen_players=names,
            frozen_judges=judge_labels,
            frozen_rounds=rounds,
            frozen_seed=str(checkpoint.seed),
            frozen_llm_timeout_seconds=(
                None if llm_timeout is None else float(llm_timeout)
            ),
            uses_frozen_budget=has_budget,
            prepared_profiles=tuple(prepared_profiles),
            warnings=("resume_uses_frozen_configuration",),
        )
    except ControlError:
        raise
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeError,
        sqlite3.Error,
    ) as exc:
        raise ControlError("resume_unavailable") from exc
    finally:
        connection.close()


def validate_job_spec(
    spec: ControlJobSpec,
    *,
    archive_database: str | Path | None = None,
    credential_ready: ProfileCredentialReady | None = None,
) -> ControlPreview:
    if spec.resume_tournament_id is not None:
        if archive_database is None:
            raise ControlError("resume_unavailable")
        return _resume_preview(
            archive_database,
            spec.resume_tournament_id,
            credential_ready=credential_ready,
        )
    if spec.resume_championship_id is not None:
        if archive_database is None:
            raise ControlError("resume_unavailable")
        return _resume_championship_preview(
            archive_database,
            spec.resume_championship_id,
            credential_ready=credential_ready,
        )
    profile_ids = sorted(
        {
            item.profile_id
            for item in (*spec.players, *spec.judges)
            if item.kind == "profile" and item.profile_id is not None
        }
    )
    try:
        profiles = load_profiles() if profile_ids else {}
    except (OSError, TypeError, ValueError) as exc:
        raise ControlError("profile_unavailable") from exc
    prepared_profiles: list[ControlPreparedProfile] = []
    for profile_id in profile_ids:
        profile = profiles.get(profile_id)
        if profile is None or not _profile_ready(profile, credential_ready):
            raise ControlError("profile_unavailable")
        prepared_profiles.append(_prepared_profile(profile))
    if profile_ids and not spec.budget.is_complete_hard_limit():
        raise ControlError("budget_required")
    participant_tokens = {player.cli_token() for player in spec.players}
    if participant_tokens & {judge.cli_token() for judge in spec.judges}:
        raise ControlError("invalid_request")
    count = len(spec.players)
    if spec.mode == "round_robin":
        pairing_count = count * (count - 1) // 2
    elif spec.mode == "championship":
        pairing_count = count - 1
    else:
        pairing_count = None
    match_count = 1 if spec.mode == "play" else 2
    if pairing_count is not None:
        match_count = pairing_count * 2
    warnings: list[str] = []
    if spec.mode == "round_robin":
        game_kwargs: dict[str, int] = {}
        if spec.rounds is not None:
            game_kwargs["rounds"] = spec.rounds
        game = create_game(spec.game, mode=spec.mode, **game_kwargs)
        if bool(getattr(game, "requires_judge_panel", False)):
            max_calls: int | None = match_count * (
                2 * TOURNAMENT_MOVE_ATTEMPTS + 2 * len(spec.judges)
            )
        else:
            rounds = describe_game_config(game).get("rounds")
            max_calls = (
                match_count * 2 * rounds * TOURNAMENT_MOVE_ATTEMPTS
                if isinstance(rounds, int) and not isinstance(rounds, bool) and rounds >= 1
                else None
            )
        too_large = match_count > MAX_UNCONFIRMED_TOURNAMENT_MATCHES or (
            max_calls is not None
            and max_calls > MAX_UNCONFIRMED_TOURNAMENT_PROVIDER_CALLS
        )
        if too_large:
            if not spec.allow_large_tournament:
                raise ControlError("large_tournament_confirmation_required")
            warnings.append("large_tournament")
    return ControlPreview(
        player_count=count,
        human_count=sum(player.kind == "human" for player in spec.players),
        match_count=match_count,
        pairing_count=pairing_count,
        rated=spec.mode == "round_robin" or (
            spec.mode in {"play", "series"} and count == 2
        ),
        requires_provider_budget=bool(profile_ids),
        prepared_profiles=tuple(prepared_profiles),
        warnings=tuple(warnings),
    )


def build_job_argv(
    spec: ControlJobSpec,
    *,
    archive_database: str | Path,
    web_base_url: str,
    python_executable: str | Path = sys.executable,
) -> tuple[str, ...]:
    """Build a fixed argv vector; caller must always use ``shell=False``."""

    command = "round-robin" if spec.mode == "round_robin" else spec.mode
    argv = [str(python_executable), "-m", "llmolympic", command]
    if spec.resume_tournament_id is not None:
        argv.extend(("--resume", spec.resume_tournament_id, "--db", str(archive_database)))
        return tuple(argv)
    if spec.resume_championship_id is not None:
        argv.extend(("--resume", spec.resume_championship_id, "--db", str(archive_database)))
        return tuple(argv)
    argv.extend(
        (
            "--game",
            spec.game,
            "--players",
            ",".join(player.cli_token() for player in spec.players),
            "--seed",
            spec.seed,
        )
    )
    if spec.rounds is not None:
        argv.extend(("--rounds", str(spec.rounds)))
    if spec.llm_timeout_seconds is not None:
        argv.extend(("--llm-timeout", str(spec.llm_timeout_seconds)))
    if spec.mode == "play":
        argv.extend(("--timeout", str(spec.human_timeout_seconds)))
        if any(player.kind == "human" for player in spec.players):
            argv.extend(("--human-input", "web", "--web-url", web_base_url))
    for judge in spec.judges:
        argv.extend(("--judge", judge.cli_token()))
    budget_flags = (
        ("--max-provider-calls", spec.budget.max_provider_calls),
        ("--max-input-tokens", spec.budget.max_input_tokens),
        ("--max-output-tokens-per-call", spec.budget.max_output_tokens_per_call),
        ("--max-total-output-tokens", spec.budget.max_total_output_tokens),
        ("--max-estimated-cost-usd", spec.budget.max_estimated_cost_usd),
    )
    for flag, value in budget_flags:
        if value is not None:
            argv.extend((flag, value))
    if spec.mode == "round_robin" and spec.allow_large_tournament:
        argv.append("--allow-large-tournament")
    argv.extend(("--db", str(archive_database)))
    return tuple(argv)


__all__ = [
    "CONTROL_SCHEMA_VERSION",
    "MAX_CONTROL_BODY_BYTES",
    "PREPARED_JOB_RETENTION_SECONDS",
    "ControlBudgetSpec",
    "ControlCatalogResponse",
    "ControlError",
    "ControlFailureCode",
    "ControlJob",
    "ControlJobListResponse",
    "ControlJobResponse",
    "ControlJobSpec",
    "ControlParticipationLink",
    "ControlPlayerSpec",
    "ControlPreparedProfile",
    "ControlPreview",
    "JobStore",
    "build_job_argv",
    "control_catalog",
    "derive_jobs_database_path",
    "profile_configuration_digest",
    "validate_job_spec",
]
