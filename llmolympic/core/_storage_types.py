"""Shared types, exceptions, constants and helpers for the SQLite store."""

from __future__ import annotations

import hashlib
import json
import os
import re
import warnings
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Self

from llmolympic.config import get as cfg_get
from llmolympic.core.archive import MatchArchive
from llmolympic.core.tournament import TournamentCheckpoint
from llmolympic.core.usage import (
    BudgetLimits,
    CallBounds,
    ProviderBudgetPolicy,
    ReservationStateError,
    UsageCounterOverflowError,
    UsageTotals,
    UsageValidationError,
)

if TYPE_CHECKING:
    from llmolympic.core.storage import SQLiteStore

SCHEMA_VERSION = 9

RatingSource = Literal["engine", "imported"]

SQLITE_INT_MIN = -(2**63)

SQLITE_INT_MAX = 2**63 - 1

MAX_QUERY_LIMIT = 1000

_PRIVATE_DIRECTORY_MODE = 0o700

_PRIVATE_FILE_MODE = 0o600

_SQLITE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")

_SAFE_PROFILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

_RUNNER_LEASE_TOKEN_RE = re.compile(r"[0-9a-f]{64}\Z")

_USAGE_LEDGER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")

DEFAULT_TOURNAMENT_RUNNER_LEASE_SECONDS = 60

MAX_TOURNAMENT_RUNNER_LEASE_SECONDS = 86_400

_IDENTITY_SAMPLING_KEYS = frozenset(
    {
        "frequency_penalty",
        "max_completion_tokens",
        "max_tokens",
        "n",
        "num_predict",
        "presence_penalty",
        "seed",
        "temperature",
        "top_p",
    }
)

_SENSITIVE_DESCRIPTOR_KEYS = frozenset(
    {
        "accesstoken",
        "apikey",
        "apikeyenv",
        "auth",
        "authorization",
        "authtoken",
        "baseurl",
        "bearertoken",
        "clientsecret",
        "credential",
        "credentials",
        "endpoint",
        "password",
        "passwordenv",
        "refreshtoken",
        "secret",
        "secretkey",
        "serverurl",
        "token",
    }
)

_SENSITIVE_DESCRIPTOR_SUFFIXES = (
    "accesstoken",
    "accesstokenenv",
    "apikey",
    "apikeyenv",
    "authheader",
    "authtoken",
    "authtokenenv",
    "baseurl",
    "bearertoken",
    "bearertokenenv",
    "clientsecret",
    "credential",
    "credentials",
    "endpoint",
    "password",
    "passwordenv",
    "refreshtoken",
    "refreshtokenenv",
    "secret",
    "secretkey",
    "serverurl",
)

_SERIES_ARCHIVE_FIELDS = frozenset(
    {
        "schema_version",
        "source",
        "series_id",
        "game",
        "seed",
        "players",
        "legs",
        "points",
        "started_at",
        "finished_at",
    }
)

_V3_REQUIRED_COLUMNS = {
    "matches": {
        "match_id",
        "schema_version",
        "game",
        "seed",
        "players_json",
        "scores_json",
        "started_at",
        "finished_at",
        "archive_source",
        "rating_source",
        "rated",
        "rating_policy",
        "archive_json",
    },
    "match_players": {
        "match_id",
        "position",
        "player",
        "entrant_id",
        "display_name",
        "descriptor_json",
        "score",
    },
    "ratings": {
        "rating_scope",
        "game",
        "entrant_id",
        "rating",
        "games_played",
        "wins",
        "draws",
        "losses",
        "updated_at",
    },
    "rating_history": {
        "match_id",
        "rating_scope",
        "game",
        "entrant_id",
        "display_name",
        "opponent_entrant_id",
        "opponent_display_name",
        "outcome",
        "rating_before",
        "rating_after",
        "created_at",
    },
    "series_archives": {
        "series_id",
        "schema_version",
        "game",
        "seed",
        "players_json",
        "points_json",
        "rating_policy",
        "started_at",
        "finished_at",
        "archive_source",
        "rating_source",
        "rated",
        "series_json",
    },
    "series_matches": {"series_id", "leg_number", "match_id"},
    "entrants": {
        "entrant_id",
        "display_name",
        "identity_json",
        "created_at",
        "updated_at",
    },
}

_V4_REQUIRED_COLUMNS = {
    **_V3_REQUIRED_COLUMNS,
    "tournament_archives": {
        "tournament_id",
        "schema_version",
        "format",
        "pairing_policy",
        "seed_policy",
        "game",
        "seed",
        "players_json",
        "points_json",
        "pairing_count",
        "rating_policy",
        "k_factor",
        "started_at",
        "finished_at",
        "archive_source",
        "rating_source",
        "rated",
        "tournament_json",
    },
    "tournament_entrants": {
        "tournament_id",
        "position",
        "entrant_id",
        "display_name",
        "descriptor_json",
        "points",
        "series_played",
        "series_wins",
        "series_draws",
        "series_losses",
        "games_played",
        "wins",
        "draws",
        "losses",
        "technical_losses",
    },
    "tournament_pairings": {
        "tournament_id",
        "pairing_number",
        "series_id",
        "entrant_a_id",
        "entrant_b_id",
    },
    "tournament_rating_snapshots": {
        "tournament_id",
        "rating_scope",
        "game",
        "entrant_id",
        "display_name",
        "rating_before",
        "rating_after",
        "games_added",
        "wins_added",
        "draws_added",
        "losses_added",
    },
    "tournament_rating_contributions": {
        "tournament_id",
        "sequence",
        "match_id",
        "rating_scope",
        "game",
        "entrant_id",
        "opponent_entrant_id",
        "frozen_rating",
        "opponent_frozen_rating",
        "expected_score",
        "rating_delta",
    },
}

_V5_REQUIRED_COLUMNS = {
    **_V4_REQUIRED_COLUMNS,
    "tournament_checkpoints": {
        "tournament_id",
        "schema_version",
        "source",
        "format",
        "pairing_policy",
        "seed_policy",
        "game",
        "seed",
        "players_json",
        "game_config_json",
        "schedule_json",
        "max_attempts",
        "pairing_count",
        "created_at",
        "updated_at",
        "status",
        "finalized_at",
        "final_tournament_id",
        "config_json",
    },
    "tournament_checkpoint_series": {
        "tournament_id",
        "pairing_number",
        "series_id",
        "match_1_id",
        "match_2_id",
        "completed_at",
        "series_json",
    },
}

_V6_REQUIRED_COLUMNS = {
    **_V5_REQUIRED_COLUMNS,
    "tournament_runner_leases": {
        "tournament_id",
        "generation",
        "token_digest",
        "acquired_at_epoch",
        "renewed_at_epoch",
        "expires_at_epoch",
    },
}

_REQUIRED_COLUMNS = {
    **_V6_REQUIRED_COLUMNS,
    "rating_operations": {
        "rating_operation_seq",
        "match_id",
        "series_id",
        "tournament_id",
    },
}

_V9_REQUIRED_COLUMNS = {
    **_REQUIRED_COLUMNS,
    "championship_archives": {
        "championship_id",
        "schema_version",
        "format",
        "pairing_policy",
        "seed_policy",
        "tiebreak_policy",
        "game",
        "seed",
        "players_json",
        "champion",
        "pairing_count",
        "rating_policy",
        "k_factor",
        "started_at",
        "finished_at",
        "archive_source",
        "rating_source",
        "rated",
        "championship_json",
    },
    "championship_entrants": {
        "championship_id",
        "position",
        "entrant_id",
        "display_name",
        "descriptor_json",
        "rank",
        "series_played",
        "series_wins",
        "series_draws",
        "series_losses",
        "games_played",
        "wins",
        "draws",
        "losses",
        "technical_losses",
    },
    "championship_pairings": {
        "championship_id",
        "round_number",
        "pairing_number",
        "series_id",
        "entrant_a_id",
        "entrant_b_id",
    },
}

_LEGACY_REQUIRED_COLUMNS = {
    "matches": {
        "match_id",
        "schema_version",
        "game",
        "seed",
        "players_json",
        "scores_json",
        "started_at",
        "finished_at",
        "archive_json",
    },
    "match_players": {
        "match_id",
        "position",
        "player",
        "descriptor_json",
        "score",
    },
    "ratings": {
        "rating_scope",
        "game",
        "player",
        "rating",
        "games_played",
        "wins",
        "draws",
        "losses",
        "updated_at",
    },
    "rating_history": {
        "match_id",
        "rating_scope",
        "game",
        "player",
        "opponent",
        "outcome",
        "rating_before",
        "rating_after",
        "created_at",
    },
}

_LEGACY_SERIES_REQUIRED_COLUMNS = {
    "series_archives": {
        "series_id",
        "schema_version",
        "game",
        "seed",
        "players_json",
        "points_json",
        "rating_policy",
        "started_at",
        "finished_at",
        "series_json",
    },
    "series_matches": {"series_id", "leg_number", "match_id"},
}

ProviderCallAttemptState = Literal[
    "reserved",
    "dispatched",
    "settled",
    "released_pre_dispatch",
    "charged_unknown",
    "violation",
]

class StorageError(RuntimeError):
    """Base exception for persistence failures."""

class MatchIdCollisionError(StorageError):
    """A match id is already attached to a different archive."""

class SeriesIdCollisionError(StorageError):
    """A series id is already attached to a different archive."""

class TournamentIdCollisionError(StorageError):
    """A tournament id is already attached to a different archive."""

class TournamentCheckpointCollisionError(StorageError):
    """A tournament checkpoint id is attached to different configuration or progress."""

class TournamentRunnerLeaseError(StorageError):
    """Base exception for runner lease coordination failures."""

class TournamentRunnerLeaseBusyError(TournamentRunnerLeaseError):
    """A different runner currently owns an unexpired tournament lease."""

class TournamentRunnerLeaseLostError(TournamentRunnerLeaseError):
    """A runner no longer owns the active fencing generation."""

class ProviderBudgetCollisionError(StorageError):
    """A durable budget id is already attached to different limits or scope."""

class ProviderCallAttemptCollisionError(StorageError):
    """A provider-call attempt id is already present in the durable ledger."""

class ProviderBudgetPendingError(StorageError):
    """A budget or tournament cannot finalize with unresolved call attempts."""

class UnsupportedSchemaError(StorageError):
    """The database was created by a newer, unsupported schema version."""

class TournamentAuditError(StorageError):
    """A stable, disclosure-safe failure from strict tournament auditing."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

@dataclass(frozen=True)
class RatingChange:
    """One player's ELO movement caused by a persisted match."""

    player: str
    opponent: str
    game: str | None
    outcome: float
    before: float
    after: float
    entrant_id: str = ""
    opponent_entrant_id: str = ""

    @property
    def display_name(self) -> str:
        return self.player

    @property
    def opponent_display_name(self) -> str:
        return self.opponent

@dataclass(frozen=True)
class SaveResult:
    """Result of saving an archive."""

    inserted: bool
    rated: bool
    rating_changes: tuple[RatingChange, ...] = ()

@dataclass(frozen=True)
class TournamentRatingChange:
    """One entrant's aggregate ELO movement across a complete tournament."""

    entrant_id: str
    display_name: str
    game: str | None
    before: float
    after: float
    games_added: int
    wins_added: int
    draws_added: int
    losses_added: int

@dataclass(frozen=True)
class TournamentSaveResult:
    """Result of atomically saving a complete round-robin tournament."""

    inserted: bool
    rated: bool
    pairing_count: int
    match_count: int
    rating_changes: tuple[TournamentRatingChange, ...] = ()

@dataclass(frozen=True)
class TournamentCheckpointSaveResult:
    """Result of creating or appending one resumable tournament checkpoint."""

    inserted: bool
    completed_pairing_count: int
    pairing_count: int

@dataclass(frozen=True)
class TournamentRunnerLease:
    """Opaque capability and fencing generation for one checkpoint runner."""

    tournament_id: str
    generation: int
    token: str = field(repr=False)
    acquired_at_epoch: int
    renewed_at_epoch: int
    expires_at_epoch: int

@dataclass(frozen=True)
class TournamentRunnerClaim:
    """A checkpoint reloaded under the same transaction that acquired its lease."""

    checkpoint: TournamentCheckpoint
    lease: TournamentRunnerLease

@dataclass(frozen=True)
class ProviderBudgetSnapshot:
    """Disclosure-safe aggregate view of one durable Provider budget."""

    budget_id: str
    limits: BudgetLimits
    policy: ProviderBudgetPolicy
    spent: UsageTotals
    reserved: UsageTotals
    tournament_id: str | None
    created_at_epoch: int
    finalized_at_epoch: int | None
    poison_reason_code: str | None

    @property
    def finalized(self) -> bool:
        return self.finalized_at_epoch is not None

    @property
    def poisoned(self) -> bool:
        return self.poison_reason_code is not None

    @property
    def state(self) -> Literal["open", "poisoned", "finalized"]:
        if self.finalized:
            return "finalized"
        if self.poisoned:
            return "poisoned"
        return "open"

@dataclass(frozen=True)
class ProviderCallAttempt:
    """One opaque, content-free Provider transport-attempt ledger row."""

    attempt_id: str
    budget_id: str
    route_id: str
    bounds: CallBounds
    state: ProviderCallAttemptState
    actual: UsageTotals | None
    charged: UsageTotals | None
    runner_generation: int | None
    created_at_epoch: int
    dispatched_at_epoch: int | None
    finished_at_epoch: int | None

    @property
    def reservation_id(self) -> str:
        """Compatibility alias for callers that call attempts reservations."""

        return self.attempt_id

class SQLiteUsageReservation:
    """Protocol adapter for one durable Provider call-attempt reservation."""

    __slots__ = ("_attempt", "_budget")

    def __init__(
        self,
        budget: SQLiteUsageBudget,
        attempt: ProviderCallAttempt,
    ) -> None:
        if not isinstance(budget, SQLiteUsageBudget):
            raise TypeError("budget must be SQLiteUsageBudget")
        if not isinstance(attempt, ProviderCallAttempt):
            raise TypeError("attempt must be ProviderCallAttempt")
        if attempt.budget_id != budget.budget_id:
            raise ReservationStateError(
                "Provider call attempt belongs to a different durable budget"
            )
        stored = budget._store.get_provider_call_attempt(attempt.attempt_id)
        if stored != attempt:
            raise ReservationStateError(
                "Provider call attempt does not match its durable route and bounds"
            )
        self._budget = budget
        self._attempt = attempt

    @property
    def reservation_id(self) -> str:
        return self._attempt.attempt_id

    @property
    def budget_id(self) -> str:
        return self._attempt.budget_id

    @property
    def bounds(self) -> CallBounds:
        return self._attempt.bounds

    def _reload(self) -> ProviderCallAttempt:
        attempt = self._budget._store.get_provider_call_attempt(self.reservation_id)
        if attempt is None or attempt.budget_id != self._budget.budget_id:
            raise StorageError("durable Provider call attempt disappeared")
        if (
            attempt.route_id != self._attempt.route_id
            or attempt.bounds != self._attempt.bounds
            or attempt.runner_generation != self._attempt.runner_generation
            or attempt.created_at_epoch != self._attempt.created_at_epoch
        ):
            raise ReservationStateError(
                "Provider call attempt immutable reservation fields changed"
            )
        self._attempt = attempt
        return attempt

    @property
    def state(self) -> str:
        return self._reload().state

    @property
    def actual(self) -> UsageTotals | None:
        return self._reload().actual

    def dispatch(self) -> Self:
        self._reload()
        self._attempt = self._budget._store.mark_provider_call_dispatched(
            self.reservation_id,
            lease=self._budget._lease,
        )
        return self

    def settle(self, usage: UsageTotals | None) -> UsageTotals:
        self._reload()
        self._attempt = self._budget._store.settle_provider_call(
            self.reservation_id,
            usage,
            lease=self._budget._lease,
        )
        if self._attempt.charged is None:
            raise StorageError("settled Provider call attempt has no durable charge")
        return self._attempt.charged

    def release_pre_dispatch(self) -> None:
        self._reload()
        self._attempt = self._budget._store.release_provider_call_pre_dispatch(
            self.reservation_id,
            lease=self._budget._lease,
        )

    def charge_unknown(self) -> UsageTotals:
        self._reload()
        self._attempt = self._budget._store.charge_provider_call_unknown(
            self.reservation_id,
            lease=self._budget._lease,
        )
        if self._attempt.charged is None:
            raise StorageError("unknown Provider call attempt has no durable charge")
        return self._attempt.charged

    def __enter__(self) -> Self:
        return self.dispatch()

    def _charge_context_if_dispatched(self, *, preserve_original: bool) -> None:
        try:
            if self.state == "dispatched":
                self.charge_unknown()
        except BaseException:
            if not preserve_original:
                raise

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        self._charge_context_if_dispatched(preserve_original=exc_type is not None)
        return False

    async def __aenter__(self) -> Self:
        return self.dispatch()

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        self._charge_context_if_dispatched(preserve_original=exc_type is not None)
        return False

class SQLiteUsageBudget:
    """Durable adapter satisfying the Player usage-budget structural protocol."""

    __slots__ = ("_budget_id", "_lease", "_store")

    def __init__(
        self,
        store: SQLiteStore,
        budget_id: str,
        *,
        lease: TournamentRunnerLease | None = None,
    ) -> None:
        self._store = store
        self._budget_id = _validate_usage_ledger_id(budget_id, "budget_id")
        self._lease = lease
        if self._store.load_provider_budget(self.budget_id) is None:
            raise StorageError("Provider budget does not exist")

    @property
    def budget_id(self) -> str:
        return self._budget_id

    @property
    def snapshot(self) -> ProviderBudgetSnapshot:
        snapshot = self._store.load_provider_budget(self.budget_id)
        if snapshot is None:
            raise StorageError("Provider budget does not exist")
        return snapshot

    @property
    def limits(self) -> BudgetLimits:
        return self.snapshot.limits

    def owns(self, reservation: object) -> bool:
        return isinstance(reservation, SQLiteUsageReservation) and reservation._budget is self

    @property
    def policy(self) -> ProviderBudgetPolicy:
        return self.snapshot.policy

    @property
    def spent(self) -> UsageTotals:
        return self.snapshot.spent

    @property
    def reserved(self) -> UsageTotals:
        return self.snapshot.reserved

    @property
    def poisoned(self) -> bool:
        return self.snapshot.poisoned

    def reserve(self, bounds: CallBounds) -> SQLiteUsageReservation:
        return self.reserve_many((bounds,))[0]

    def reserve_many(
        self,
        bounds: Iterable[CallBounds],
    ) -> tuple[SQLiteUsageReservation, ...]:
        attempts = self._store.reserve_provider_call_batch(
            self.budget_id,
            bounds,
            lease=self._lease,
        )
        return tuple(SQLiteUsageReservation(self, attempt) for attempt in attempts)

    def finalize(self) -> ProviderBudgetSnapshot:
        return self._store.finalize_provider_budget(
            self.budget_id,
            lease=self._lease,
        )

@dataclass(frozen=True)
class RatingEntry:
    player: str
    rating: float
    games_played: int
    wins: int
    draws: int
    losses: int
    updated_at: datetime
    entrant_id: str = ""

    @property
    def display_name(self) -> str:
        return self.player

@dataclass(frozen=True)
class MatchSummary:
    match_id: str
    game: str
    seed: int
    players: tuple[str, ...]
    scores: dict[str, float]
    started_at: datetime
    finished_at: datetime
    series_id: str | None = None
    leg_number: int | None = None
    entrant_ids: tuple[str, ...] = ()
    rating_source: RatingSource = "imported"
    rated: bool = False
    tournament_id: str | None = None
    pairing_number: int | None = None
    pairing_count: int | None = None

@dataclass(frozen=True)
class DatabaseInspection:
    """Read-only database compatibility result used by offline diagnostics."""

    path: Path
    exists: bool
    schema_version: int | None = None
    migration_required: bool = False
    private_permissions: bool = True
    limited_by_active_journal: bool = False

@dataclass(frozen=True)
class TournamentAuditReport:
    """Disclosure-safe result of deeply auditing one tournament."""

    tournament_id: str
    state: Literal["in_progress", "finalized"]
    game: str
    completed_pairings: int
    pairing_count: int
    technical_losses: int
    rated: bool
    resumable: bool
    checkpoint_present: bool
    leaderboard_replay_complete: bool | None

@dataclass(frozen=True)
class _EntrantRef:
    entrant_id: str
    display_name: str
    identity_json: str

@dataclass(frozen=True)
class _TournamentContribution:
    sequence: int
    archive: MatchArchive
    rating_scope: str
    game_key: str
    player: _EntrantRef
    opponent: _EntrantRef
    outcome: float
    frozen_rating: float
    opponent_frozen_rating: float
    expected: float
    delta: float
    before: float
    after: float

@dataclass(frozen=True)
class _TournamentAggregate:
    rating_scope: str
    game_key: str
    player: _EntrantRef
    before: float
    after: float
    outcomes: tuple[float, ...]

@dataclass(frozen=True)
class _TournamentRunnerLeaseState:
    generation: int
    token_digest: bytes | None
    acquired_at_epoch: int | None
    renewed_at_epoch: int | None
    expires_at_epoch: int | None

def database_path(path: str | Path | None = None) -> Path:
    """Resolve the database path from an override, config, or the default.

    Precedence is explicit argument, ``LLMOLYMPIC_DB``,
    ``[storage] database``, then ``~/.llmolympic/llmolympic.db``.
    Relative paths are resolved against the current working directory.
    """

    if path is None:
        configured = cfg_get("storage", "database", env="LLMOLYMPIC_DB")
        path = configured or Path.home() / ".llmolympic" / "llmolympic.db"
    return Path(os.path.expandvars(str(path))).expanduser().resolve()

def _validate_runner_lease_seconds(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_TOURNAMENT_RUNNER_LEASE_SECONDS
    ):
        raise ValueError(
            f"runner lease 秒数必须是 1 到 {MAX_TOURNAMENT_RUNNER_LEASE_SECONDS} 之间的整数"
        )
    return value

def _runner_lease_token_digest(token: str) -> bytes:
    if not isinstance(token, str) or not _RUNNER_LEASE_TOKEN_RE.fullmatch(token):
        raise ValueError("runner lease token 无效")
    return hashlib.sha256(bytes.fromhex(token)).digest()

def _validate_runner_lease_handle(
    lease: TournamentRunnerLease,
    tournament_id: str,
) -> bytes:
    if not isinstance(lease, TournamentRunnerLease):
        raise TypeError("必须提供 TournamentRunnerLease")
    if lease.tournament_id != tournament_id:
        raise ValueError("runner lease 不属于该循环赛")
    if (
        isinstance(lease.generation, bool)
        or not isinstance(lease.generation, int)
        or lease.generation < 1
    ):
        raise ValueError("runner lease generation 无效")
    return _runner_lease_token_digest(lease.token)

def _validate_usage_ledger_id(value: object, field: str) -> str:
    if not isinstance(value, str) or not _USAGE_LEDGER_ID_RE.fullmatch(value):
        raise UsageValidationError(
            f"{field} must be a 1-128 character opaque ASCII identifier"
        )
    return value

def _usage_from_bounds(bounds: CallBounds) -> UsageTotals:
    if not isinstance(bounds, CallBounds):
        raise UsageValidationError("bounds must contain only CallBounds")
    return UsageTotals(
        calls=1,
        input=bounds.input,
        output=bounds.output,
        estimated_cost=bounds.estimated_cost,
    )

def _checked_usage_add(left: UsageTotals, right: UsageTotals) -> UsageTotals:
    values: dict[str, int] = {}
    for dimension in ("calls", "input", "output", "estimated_cost"):
        value = getattr(left, dimension) + getattr(right, dimension)
        if value > SQLITE_INT_MAX:
            raise UsageCounterOverflowError(f"usage counter overflow for {dimension}")
        values[dimension] = value
    return UsageTotals(**values)

def _checked_usage_subtract(left: UsageTotals, right: UsageTotals) -> UsageTotals:
    values: dict[str, int] = {}
    for dimension in ("calls", "input", "output", "estimated_cost"):
        value = getattr(left, dimension) - getattr(right, dimension)
        if value < 0:
            raise ReservationStateError(f"usage counter underflow for {dimension}")
        values[dimension] = value
    return UsageTotals(**values)

def _sum_call_bounds(bounds: tuple[CallBounds, ...]) -> UsageTotals:
    total = UsageTotals.zero()
    for bound in bounds:
        total = _checked_usage_add(total, _usage_from_bounds(bound))
    return total

def _validate_durable_budget_definition(
    limits: object,
    policy: object,
) -> tuple[BudgetLimits, ProviderBudgetPolicy]:
    if not isinstance(limits, BudgetLimits):
        raise UsageValidationError("limits must be BudgetLimits")
    if not isinstance(policy, ProviderBudgetPolicy):
        raise UsageValidationError("policy must be ProviderBudgetPolicy")
    if not policy.routes:
        raise UsageValidationError("budget policy must contain at least one route")
    if len(policy.canonical_json()) > 65_536:
        raise UsageValidationError("budget policy JSON exceeds the durable size limit")
    if limits.estimated_cost is not None and any(
        route.price is None for route in policy.routes
    ):
        raise UsageValidationError(
            "cost-limited budget requires a frozen price for every route"
        )
    return limits, policy

def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise StorageError(f"对局档案包含无法序列化的 JSON 数据：{exc}") from exc

def _set_private_mode(path: Path, mode: int, *, required: bool = False) -> None:
    """Tighten POSIX permissions, failing closed for required database artifacts."""

    if os.name != "posix" or not path.exists():
        return
    try:
        path.chmod(mode)
    except OSError as exc:
        message = f"无法把 {path} 的权限收紧为 {mode:04o}"
        if required:
            raise StorageError(message) from exc
        warnings.warn(message, RuntimeWarning, stacklevel=2)

def _validate_query_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_QUERY_LIMIT:
        raise ValueError(f"limit 必须是 1 到 {MAX_QUERY_LIMIT} 之间的整数")

def _normalized_descriptor_key(key: str) -> str:
    return "".join(character for character in key.casefold() if character.isalnum())

def _is_sensitive_descriptor_key(key: str) -> bool:
    normalized = _normalized_descriptor_key(key)
    return normalized in _SENSITIVE_DESCRIPTOR_KEYS or normalized.endswith(
        _SENSITIVE_DESCRIPTOR_SUFFIXES
    )

def _sensitive_descriptor_path(value: object) -> str | None:
    """Return the first sensitive key path without ever formatting its value."""

    pending: list[tuple[str, object]] = [("descriptor", value)]
    while pending:
        path, candidate = pending.pop()
        if isinstance(candidate, dict):
            for key, nested in candidate.items():
                if not isinstance(key, str):
                    pending.append((f"{path}[*]", nested))
                    continue
                nested_path = f"{path}.{key}"
                if _is_sensitive_descriptor_key(key) and nested != "[REDACTED]":
                    return nested_path
                pending.append((nested_path, nested))
        elif isinstance(candidate, (list, tuple)):
            pending.extend((f"{path}[{index}]", nested) for index, nested in enumerate(candidate))
    return None

