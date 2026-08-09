"""Thread-safe, in-memory hard-budget accounting for Provider calls.

All monetary values in this module are integer nanodollars.  A reservation is
charged before a call may be dispatched, so concurrent callers cannot all
observe and spend the same remaining capacity.  Persistence and Provider
integration intentionally live outside this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from threading import RLock
from typing import Self

SQLITE_INT_MAX = 2**63 - 1
NANODOLLARS_PER_USD = 1_000_000_000
TOKENS_PER_MILLION = 1_000_000
INPUT_BOUND_VERSION = "canonical-messages-utf8-v1"
COST_ROUNDING_VERSION = "nanodollar-ceiling-v1"

_ROUTE_ID_RE = re.compile(r"route:v1:[0-9a-f]{64}\Z")


class UsageError(Exception):
    """Base class for stable, reason-coded usage accounting failures."""

    reason_code = "usage_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.reason_code = type(self).reason_code


class UsageValidationError(UsageError):
    """One usage value or object did not satisfy the strict integer contract."""

    reason_code = "invalid_usage_value"


class UsageCounterOverflowError(UsageError):
    """An aggregate cannot be represented by a SQLite signed 64-bit integer."""

    reason_code = "usage_counter_overflow"


class BudgetExceededError(UsageError):
    """A reservation would exceed a configured hard limit."""

    reason_code = "budget_exceeded"

    def __init__(self, dimension: str) -> None:
        super().__init__(f"usage budget exceeded for {dimension}")
        self.dimension = dimension


class BudgetPoisonedError(UsageError):
    """The budget observed an impossible overrun and no longer permits dispatch."""

    reason_code = "budget_poisoned"


class ReservationStateError(UsageError):
    """A reservation transition conflicts with its durable accounting state."""

    reason_code = "invalid_reservation_state"


class UsageExceedsReservationError(UsageError):
    """Reported actual usage exceeded the bound authorized before dispatch."""

    reason_code = "usage_exceeds_reservation"


def _strict_counter(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise UsageValidationError(f"{field} must be an integer")
    if not 0 <= value <= SQLITE_INT_MAX:
        raise UsageValidationError(f"{field} must fit a non-negative SQLite integer")
    return value


def _optional_counter(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _strict_counter(value, field)


def usd_to_nanodollar_limit(value: Decimal) -> int:
    """Convert a USD cap to an integer nanodollar limit without rounding up."""

    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise UsageValidationError("USD limit must be a finite non-negative Decimal")
    numerator, denominator = value.as_integer_ratio()
    converted = numerator * NANODOLLARS_PER_USD // denominator
    return _strict_counter(converted, "estimated_cost")


def usd_per_million_to_nanodollars(value: Decimal) -> int:
    """Freeze a price conservatively as integer nanodollars per million tokens."""

    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise UsageValidationError("token price must be a finite non-negative Decimal")
    numerator, denominator = value.as_integer_ratio()
    scaled = numerator * NANODOLLARS_PER_USD
    converted = (scaled + denominator - 1) // denominator
    return _strict_counter(converted, "price_nanos_per_million")


@dataclass(frozen=True, slots=True)
class TokenPrice:
    """Frozen integer-nanodollar rates per one million input/output tokens."""

    input_nanos_per_million: int
    output_nanos_per_million: int

    def __post_init__(self) -> None:
        _strict_counter(self.input_nanos_per_million, "input_nanos_per_million")
        _strict_counter(self.output_nanos_per_million, "output_nanos_per_million")

    def estimate(self, *, input_tokens: int, output_tokens: int) -> int:
        """Return a per-call conservative cost rounded up to one nanodollar."""

        input_tokens = _strict_counter(input_tokens, "input_tokens")
        output_tokens = _strict_counter(output_tokens, "output_tokens")
        numerator = (
            input_tokens * self.input_nanos_per_million
            + output_tokens * self.output_nanos_per_million
        )
        result = (numerator + TOKENS_PER_MILLION - 1) // TOKENS_PER_MILLION
        if result > SQLITE_INT_MAX:
            raise UsageCounterOverflowError("estimated call cost exceeds SQLite integer range")
        return result


@dataclass(frozen=True, slots=True)
class RouteBudgetPolicy:
    """Safe frozen price metadata for one opaque Provider route."""

    route_id: str
    price: TokenPrice | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.route_id, str) or _ROUTE_ID_RE.fullmatch(self.route_id) is None:
            raise UsageValidationError("route_id must be a route:v1 SHA-256 identifier")
        if self.price is not None and not isinstance(self.price, TokenPrice):
            raise UsageValidationError("route price must be TokenPrice or None")


@dataclass(frozen=True, slots=True)
class ProviderBudgetPolicy:
    """Versioned, credential-free policy frozen for a durable budget scope."""

    max_output_tokens_per_call: int
    routes: tuple[RouteBudgetPolicy, ...]
    input_bound_version: str = INPUT_BOUND_VERSION
    cost_rounding_version: str = COST_ROUNDING_VERSION

    def __post_init__(self) -> None:
        output_cap = _strict_counter(
            self.max_output_tokens_per_call,
            "max_output_tokens_per_call",
        )
        if output_cap < 1:
            raise UsageValidationError("max_output_tokens_per_call must be at least one")
        if self.input_bound_version != INPUT_BOUND_VERSION:
            raise UsageValidationError("unsupported input-bound policy version")
        if self.cost_rounding_version != COST_ROUNDING_VERSION:
            raise UsageValidationError("unsupported cost-rounding policy version")
        try:
            routes = tuple(self.routes)
        except TypeError as exc:
            raise UsageValidationError("routes must be RouteBudgetPolicy values") from exc
        if any(not isinstance(route, RouteBudgetPolicy) for route in routes):
            raise UsageValidationError("routes must be RouteBudgetPolicy values")
        ordered = tuple(sorted(routes, key=lambda route: route.route_id))
        if len({route.route_id for route in ordered}) != len(ordered):
            raise UsageValidationError("budget policy route_id values must be unique")
        object.__setattr__(self, "routes", ordered)

    def price_for(self, route_id: str) -> TokenPrice | None:
        for route in self.routes:
            if route.route_id == route_id:
                return route.price
        raise UsageValidationError("route_id is not frozen in this budget policy")

    def canonical_json(self) -> str:
        payload = {
            "cost_rounding_version": self.cost_rounding_version,
            "input_bound_version": self.input_bound_version,
            "max_output_tokens_per_call": self.max_output_tokens_per_call,
            "routes": [
                {
                    "input_nanos_per_million": (
                        None if route.price is None else route.price.input_nanos_per_million
                    ),
                    "output_nanos_per_million": (
                        None if route.price is None else route.price.output_nanos_per_million
                    ),
                    "route_id": route.route_id,
                }
                for route in self.routes
            ],
            "version": 1,
        }
        return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_canonical_json(cls, value: str) -> ProviderBudgetPolicy:
        if not isinstance(value, str) or not value or len(value) > 65_536:
            raise UsageValidationError("budget policy JSON must be a bounded non-empty string")
        try:
            payload = json.loads(value)
        except (json.JSONDecodeError, TypeError) as exc:
            raise UsageValidationError("budget policy JSON is invalid") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "cost_rounding_version",
            "input_bound_version",
            "max_output_tokens_per_call",
            "routes",
            "version",
        }:
            raise UsageValidationError("budget policy JSON fields are invalid")
        if payload["version"] != 1 or isinstance(payload["version"], bool):
            raise UsageValidationError("budget policy version is unsupported")
        raw_routes = payload["routes"]
        if not isinstance(raw_routes, list):
            raise UsageValidationError("budget policy routes must be a list")
        routes: list[RouteBudgetPolicy] = []
        for item in raw_routes:
            if not isinstance(item, dict) or set(item) != {
                "input_nanos_per_million",
                "output_nanos_per_million",
                "route_id",
            }:
                raise UsageValidationError("budget policy route fields are invalid")
            input_rate = item["input_nanos_per_million"]
            output_rate = item["output_nanos_per_million"]
            if (input_rate is None) != (output_rate is None):
                raise UsageValidationError("budget policy route price is incomplete")
            price = (
                None
                if input_rate is None
                else TokenPrice(
                    input_nanos_per_million=input_rate,
                    output_nanos_per_million=output_rate,
                )
            )
            routes.append(RouteBudgetPolicy(route_id=item["route_id"], price=price))
        policy = cls(
            max_output_tokens_per_call=payload["max_output_tokens_per_call"],
            routes=tuple(routes),
            input_bound_version=payload["input_bound_version"],
            cost_rounding_version=payload["cost_rounding_version"],
        )
        if policy.canonical_json() != value:
            raise UsageValidationError("budget policy JSON is not canonical")
        return policy

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            b"llmolympic-provider-budget-policy-v1\0" + self.canonical_json().encode("ascii")
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """Optional hard limits; ``estimated_cost`` is measured in nanodollars."""

    calls: int | None = None
    input: int | None = None
    output: int | None = None
    estimated_cost: int | None = None

    def __post_init__(self) -> None:
        _optional_counter(self.calls, "calls")
        _optional_counter(self.input, "input")
        _optional_counter(self.output, "output")
        _optional_counter(self.estimated_cost, "estimated_cost")


@dataclass(frozen=True, slots=True)
class CallBounds:
    """Worst-case authorization for exactly one Provider transport attempt."""

    input: int = 0
    output: int = 0
    estimated_cost: int = 0
    route_id: str | None = None

    def __post_init__(self) -> None:
        _strict_counter(self.input, "input")
        _strict_counter(self.output, "output")
        _strict_counter(self.estimated_cost, "estimated_cost")
        if self.route_id is not None and (
            not isinstance(self.route_id, str) or _ROUTE_ID_RE.fullmatch(self.route_id) is None
        ):
            raise UsageValidationError("route_id must be a route:v1 SHA-256 identifier or None")

    def as_totals(self) -> UsageTotals:
        return UsageTotals(
            calls=1,
            input=self.input,
            output=self.output,
            estimated_cost=self.estimated_cost,
        )


@dataclass(frozen=True, slots=True)
class UsageTotals:
    """Aggregate calls, tokens and integer-nanodollar estimated cost."""

    calls: int
    input: int
    output: int
    estimated_cost: int

    def __post_init__(self) -> None:
        _strict_counter(self.calls, "calls")
        _strict_counter(self.input, "input")
        _strict_counter(self.output, "output")
        _strict_counter(self.estimated_cost, "estimated_cost")

    @classmethod
    def zero(cls) -> UsageTotals:
        return cls(calls=0, input=0, output=0, estimated_cost=0)


_DIMENSIONS = ("calls", "input", "output", "estimated_cost")


def _checked_add(left: UsageTotals, right: UsageTotals) -> UsageTotals:
    values: dict[str, int] = {}
    for dimension in _DIMENSIONS:
        value = getattr(left, dimension) + getattr(right, dimension)
        if value > SQLITE_INT_MAX:
            raise UsageCounterOverflowError(f"usage counter overflow for {dimension}")
        values[dimension] = value
    return UsageTotals(**values)


def _checked_subtract(left: UsageTotals, right: UsageTotals) -> UsageTotals:
    values: dict[str, int] = {}
    for dimension in _DIMENSIONS:
        value = getattr(left, dimension) - getattr(right, dimension)
        if value < 0:
            raise ReservationStateError(f"usage counter underflow for {dimension}")
        values[dimension] = value
    return UsageTotals(**values)


def _sum_bounds(bounds: tuple[CallBounds, ...]) -> UsageTotals:
    total = UsageTotals.zero()
    for bound in bounds:
        total = _checked_add(total, bound.as_totals())
    return total


class _ReservationState(str, Enum):
    RESERVED = "reserved"
    DISPATCHED = "dispatched"
    SETTLED = "settled"
    RELEASED = "released_pre_dispatch"
    CHARGED_UNKNOWN = "charged_unknown"
    VIOLATED = "usage_exceeded_reservation"


class UsageReservation:
    """One stateful call authorization owned by a :class:`UsageBudget`.

    It may be used as a synchronous or asynchronous context manager.  Entering
    dispatches it; leaving without a successful settlement charges the complete
    bound, including when the body raises ``CancelledError`` or another
    ``BaseException``.
    """

    __slots__ = ("_actual", "_budget", "_id", "_state", "bounds")

    def __init__(self, budget: UsageBudget, reservation_id: int, bounds: CallBounds) -> None:
        self._budget = budget
        self._id = reservation_id
        self.bounds = bounds
        self._state = _ReservationState.RESERVED
        self._actual: UsageTotals | None = None

    @property
    def reservation_id(self) -> int:
        return self._id

    @property
    def budget_id(self) -> object:
        return self._budget.budget_id

    @property
    def state(self) -> str:
        with self._budget._lock:
            return self._state.value

    @property
    def actual(self) -> UsageTotals | None:
        with self._budget._lock:
            return self._actual

    def dispatch(self) -> Self:
        self._budget._dispatch(self)
        return self

    def settle(self, usage: UsageTotals | None) -> UsageTotals:
        """Settle valid actual usage, or charge the bound when usage is unknown."""

        return self._budget._settle(self, usage)

    def release_pre_dispatch(self) -> None:
        self._budget._release_pre_dispatch(self)

    def charge_unknown(self) -> UsageTotals:
        return self._budget._charge_unknown(self)

    def __enter__(self) -> Self:
        return self.dispatch()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        self._budget._charge_context_if_unsettled(self)
        return False

    async def __aenter__(self) -> Self:
        return self.dispatch()

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        self._budget._charge_context_if_unsettled(self)
        return False


class UsageBudget:
    """Thread-safe reservation and settlement state for one hard-budget scope."""

    def __init__(self, limits: BudgetLimits) -> None:
        if not isinstance(limits, BudgetLimits):
            raise UsageValidationError("limits must be BudgetLimits")
        self._limits = limits
        self._budget_id = object()
        self._lock = RLock()
        self._spent = UsageTotals.zero()
        self._reserved = UsageTotals.zero()
        self._next_reservation_id = 1
        self._poison_reason_code: str | None = None

    @property
    def limits(self) -> BudgetLimits:
        return self._limits

    @property
    def budget_id(self) -> object:
        return self._budget_id

    @property
    def spent(self) -> UsageTotals:
        with self._lock:
            return self._spent

    @property
    def reserved(self) -> UsageTotals:
        with self._lock:
            return self._reserved

    @property
    def poisoned(self) -> bool:
        with self._lock:
            return self._poison_reason_code is not None

    @property
    def poison_reason_code(self) -> str | None:
        with self._lock:
            return self._poison_reason_code

    def reserve(self, bounds: CallBounds) -> UsageReservation:
        """Atomically reserve one call bound."""

        return self.reserve_many((bounds,))[0]

    def reserve_many(self, bounds: Iterable[CallBounds]) -> tuple[UsageReservation, ...]:
        """Atomically reserve an entire batch, or reserve none of it."""

        try:
            requested = tuple(bounds)
        except TypeError as exc:
            raise UsageValidationError("bounds must be an iterable of CallBounds") from exc
        for bound in requested:
            if not isinstance(bound, CallBounds):
                raise UsageValidationError("bounds must contain only CallBounds")
        if not requested:
            return ()
        batch = _sum_bounds(requested)

        with self._lock:
            self._require_healthy()
            committed = _checked_add(self._spent, self._reserved)
            prospective = _checked_add(committed, batch)
            for dimension in _DIMENSIONS:
                limit = getattr(self._limits, dimension)
                if limit is not None and getattr(prospective, dimension) > limit:
                    raise BudgetExceededError(dimension)

            first_id = self._next_reservation_id
            reservations = tuple(
                UsageReservation(self, first_id + offset, bound)
                for offset, bound in enumerate(requested)
            )
            self._reserved = _checked_add(self._reserved, batch)
            self._next_reservation_id += len(reservations)
            return reservations

    def _require_healthy(self) -> None:
        if self._poison_reason_code is not None:
            raise BudgetPoisonedError(f"usage budget is poisoned by {self._poison_reason_code}")

    def _dispatch(self, reservation: UsageReservation) -> None:
        with self._lock:
            if reservation._state is _ReservationState.DISPATCHED:
                return
            if reservation._budget is not self:
                raise ReservationStateError("reservation does not belong to this budget")
            if reservation._state is not _ReservationState.RESERVED:
                raise ReservationStateError(
                    f"cannot dispatch reservation in state {reservation._state.value}"
                )
            self._require_healthy()
            reservation._state = _ReservationState.DISPATCHED

    def owns(self, reservation: object) -> bool:
        return (
            isinstance(reservation, UsageReservation)
            and reservation._budget is self  # type: ignore[attr-defined]
        )

    def _settle(
        self,
        reservation: UsageReservation,
        usage: UsageTotals | None,
    ) -> UsageTotals:
        with self._lock:
            if reservation._state is _ReservationState.SETTLED:
                if usage == reservation._actual:
                    return reservation._actual  # type: ignore[return-value]
                raise ReservationStateError("settlement conflicts with settled usage")
            if reservation._state is _ReservationState.CHARGED_UNKNOWN:
                if usage is None:
                    return reservation.bounds.as_totals()
                raise ReservationStateError("unknown usage charge cannot be replaced")
            if reservation._state is _ReservationState.VIOLATED:
                if usage == reservation._actual:
                    raise UsageExceedsReservationError("usage exceeded reservation")
                raise ReservationStateError("settlement conflicts with recorded overrun")
            if reservation._state is not _ReservationState.DISPATCHED:
                raise ReservationStateError(
                    f"cannot settle reservation in state {reservation._state.value}"
                )
            if usage is None:
                return self._charge_unknown_locked(reservation)
            if not isinstance(usage, UsageTotals):
                self._charge_unknown_locked(reservation)
                raise UsageValidationError("settled usage must be UsageTotals or None")
            if usage.calls != 1:
                if usage.calls > 1:
                    self._record_overrun_locked(reservation, usage)
                    raise UsageExceedsReservationError("reported call count exceeded reservation")
                self._charge_unknown_locked(reservation)
                raise UsageValidationError("settled usage calls must equal one")

            bound = reservation.bounds.as_totals()
            if any(getattr(usage, name) > getattr(bound, name) for name in _DIMENSIONS):
                self._record_overrun_locked(reservation, usage)
                raise UsageExceedsReservationError("reported usage exceeded reservation")

            new_reserved = _checked_subtract(self._reserved, bound)
            new_spent = _checked_add(self._spent, usage)
            self._reserved = new_reserved
            self._spent = new_spent
            reservation._actual = usage
            reservation._state = _ReservationState.SETTLED
            return usage

    def _release_pre_dispatch(self, reservation: UsageReservation) -> None:
        with self._lock:
            if reservation._state is _ReservationState.RELEASED:
                return
            if reservation._state is not _ReservationState.RESERVED:
                raise ReservationStateError(
                    f"cannot release reservation in state {reservation._state.value}"
                )
            self._reserved = _checked_subtract(
                self._reserved,
                reservation.bounds.as_totals(),
            )
            reservation._state = _ReservationState.RELEASED

    def _charge_unknown(self, reservation: UsageReservation) -> UsageTotals:
        with self._lock:
            if reservation._state is _ReservationState.CHARGED_UNKNOWN:
                return reservation.bounds.as_totals()
            if reservation._state not in (
                _ReservationState.RESERVED,
                _ReservationState.DISPATCHED,
            ):
                raise ReservationStateError(
                    f"cannot charge reservation in state {reservation._state.value}"
                )
            return self._charge_unknown_locked(reservation)

    def _charge_unknown_locked(self, reservation: UsageReservation) -> UsageTotals:
        charged = reservation.bounds.as_totals()
        new_reserved = _checked_subtract(self._reserved, charged)
        try:
            new_spent = _checked_add(self._spent, charged)
        except UsageCounterOverflowError:
            self._poison_reason_code = UsageCounterOverflowError.reason_code
            raise
        self._reserved = new_reserved
        self._spent = new_spent
        reservation._state = _ReservationState.CHARGED_UNKNOWN
        return charged

    def _record_overrun_locked(
        self,
        reservation: UsageReservation,
        usage: UsageTotals,
    ) -> None:
        bound = reservation.bounds.as_totals()
        new_reserved = _checked_subtract(self._reserved, bound)
        try:
            new_spent = _checked_add(self._spent, usage)
        except UsageCounterOverflowError:
            # The authorized bound is always representable here.  Preserve a
            # conservative charge and poison the budget when observed totals are not.
            try:
                new_spent = _checked_add(self._spent, bound)
            except UsageCounterOverflowError:
                self._poison_reason_code = UsageCounterOverflowError.reason_code
                raise
            poison_reason_code = UsageCounterOverflowError.reason_code
        else:
            poison_reason_code = UsageExceedsReservationError.reason_code
        self._reserved = new_reserved
        self._spent = new_spent
        self._poison_reason_code = poison_reason_code
        reservation._actual = usage
        reservation._state = _ReservationState.VIOLATED

    def _charge_context_if_unsettled(self, reservation: UsageReservation) -> None:
        with self._lock:
            if reservation._state is _ReservationState.DISPATCHED:
                try:
                    self._charge_unknown_locked(reservation)
                except UsageCounterOverflowError:
                    # Preserve the Provider/cancellation exception.  The full
                    # reservation remains outstanding and the budget is poisoned.
                    pass


__all__ = [
    "COST_ROUNDING_VERSION",
    "INPUT_BOUND_VERSION",
    "NANODOLLARS_PER_USD",
    "SQLITE_INT_MAX",
    "TOKENS_PER_MILLION",
    "BudgetExceededError",
    "BudgetLimits",
    "BudgetPoisonedError",
    "CallBounds",
    "ProviderBudgetPolicy",
    "ReservationStateError",
    "RouteBudgetPolicy",
    "TokenPrice",
    "UsageBudget",
    "UsageCounterOverflowError",
    "UsageError",
    "UsageExceedsReservationError",
    "UsageReservation",
    "UsageTotals",
    "UsageValidationError",
    "usd_per_million_to_nanodollars",
    "usd_to_nanodollar_limit",
]
