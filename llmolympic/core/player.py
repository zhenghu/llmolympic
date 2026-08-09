"""选手抽象：引擎不区分人类与模型。"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import io
import json
import math
import os
import re
import select
import sys
import weakref
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from threading import Lock
from typing import Protocol

from llmolympic.core.archive import validate_entrant_id
from llmolympic.core.usage import (
    BudgetLimits,
    CallBounds,
    ProviderBudgetPolicy,
    TokenPrice,
    UsageError,
    UsageTotals,
    UsageValidationError,
)
from llmolympic.providers.base import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    Provider,
    ProviderChatResult,
    ProviderTimeoutError,
    ProviderUsage,
    UsageSupport,
    validate_route_id,
)

SYSTEM_PROMPT = (
    "你是 LLM Olympics 的一名参赛选手。请仔细阅读题面，"
    "只输出最终答案本身，不要解释过程，不要输出多余文字。"
)

DEFAULT_LLM_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_RESPONSE_CHARS = 4096

_REDACTED = "[REDACTED]"
_SAFE_PROFILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_DEFAULT_INPUT = input
_STDIN_LOCKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, weakref.ReferenceType[asyncio.Lock]
] = weakref.WeakKeyDictionary()
_STDIN_BUFFERS: weakref.WeakKeyDictionary[object, list[str]] = weakref.WeakKeyDictionary()
_ARCHIVE_SAFE_SAMPLING_KEYS = frozenset(
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
_BUDGET_FORBIDDEN_SAMPLING_KEYS = frozenset(
    {
        "extra_body",
        "extra_query",
        "extra_headers",
        "response_format",
        "tool_choice",
        "tools",
        "functions",
        "function_call",
    }
)


class UsageReservationProtocol(Protocol):
    """Structural reservation contract shared by memory and SQLite ledgers."""

    @property
    def budget_id(self) -> object: ...

    @property
    def bounds(self) -> CallBounds: ...

    @property
    def state(self) -> str: ...

    def dispatch(self) -> object: ...

    def settle(self, usage: UsageTotals | None) -> UsageTotals: ...

    def release_pre_dispatch(self) -> None: ...

    def charge_unknown(self) -> UsageTotals: ...


class UsageBudgetProtocol(Protocol):
    """Structural hard-budget contract; no concrete ledger type is required."""

    @property
    def limits(self) -> BudgetLimits: ...

    @property
    def budget_id(self) -> object: ...

    def owns(self, reservation: UsageReservationProtocol) -> bool: ...

    def reserve(self, bounds: CallBounds) -> UsageReservationProtocol: ...

    def reserve_many(
        self,
        bounds: Iterable[CallBounds],
    ) -> tuple[UsageReservationProtocol, ...]: ...


class _CancellableStdinUnavailable(Exception):
    """The active loop/stdin pair cannot provide a removable POSIX reader."""


def _stdin_lock(loop: asyncio.AbstractEventLoop) -> asyncio.Lock:
    lock_reference = _STDIN_LOCKS.get(loop)
    lock = None if lock_reference is None else lock_reference()
    if lock is None:
        lock = asyncio.Lock()
        # A contended asyncio.Lock retains its loop.  Storing the lock strongly as
        # the value of a WeakKeyDictionary would therefore keep the weak loop key
        # alive through a value -> key cycle.  Tasks using the lock already hold it
        # strongly, so a weak value is sufficient between calls.
        _STDIN_LOCKS[loop] = weakref.ref(lock)
    return lock


def _pop_stdin_line(buffer: list[str]) -> str | None:
    text = "".join(buffer)
    newline = text.find("\n")
    if newline < 0:
        return None
    line = text[:newline]
    remainder = text[newline + 1 :]
    buffer.clear()
    if remainder:
        buffer.append(remainder)
    return line.removesuffix("\r")


def _fd_ready(fd: int) -> bool:
    try:
        return bool(select.select((fd,), (), (), 0)[0])
    except (OSError, ValueError) as exc:
        raise _CancellableStdinUnavailable from exc


def _is_canonical_tty(fd: int) -> bool:
    if not os.isatty(fd):
        return False
    try:
        import termios

        return bool(termios.tcgetattr(fd)[3] & termios.ICANON)
    except (ImportError, OSError):
        # A selectable POSIX tty is normally canonical.  If its attributes are
        # unavailable, preserving Ctrl-D EOF semantics is safer than hanging.
        return True


def _read_text_stream_without_blocking(stream: object, fd: int) -> tuple[str, bool]:
    """Drain one text line/fragment while preserving TextIOWrapper read-ahead.

    ``TextIOWrapper`` can already hold a complete line even when the underlying
    descriptor is not readable.  Temporarily making the descriptor non-blocking
    lets ``readline`` consult that buffer without ever parking the event loop.
    The boolean result distinguishes a real EOF from an empty would-block read.
    """

    readline = getattr(stream, "readline", None)
    if not callable(readline):
        raise _CancellableStdinUnavailable
    try:
        was_blocking = os.get_blocking(fd)
        if was_blocking:
            os.set_blocking(fd, False)
    except OSError as exc:
        raise _CancellableStdinUnavailable from exc
    try:
        ready_before = _fd_ready(fd)
        canonical_tty = ready_before and _is_canonical_tty(fd)
        try:
            chunk = readline()
        except (BlockingIOError, InterruptedError):
            return "", False
        except UnicodeDecodeError as exc:
            # A strict TextIOWrapper may report a split multibyte character as
            # an incomplete decode.  It retains those bytes internally; wait for
            # more only for that exact condition.  Other malformed input must not
            # be silently discarded.  Canonical tty readiness already represents
            # a complete line or a Ctrl-D boundary, so it is never a split read.
            if (
                not canonical_tty
                and exc.reason == "unexpected end of data"
                and exc.end == len(exc.object)
                and not _fd_ready(fd)
            ):
                return "", False
            raise
    finally:
        if was_blocking:
            try:
                os.set_blocking(fd, True)
            except OSError as exc:
                raise _CancellableStdinUnavailable from exc
    if not isinstance(chunk, str):
        raise _CancellableStdinUnavailable
    if chunk:
        return chunk, canonical_tty and "\n" not in chunk
    if canonical_tty:
        return "", True
    return "", ready_before and _fd_ready(fd)


async def _read_default_stdin(prompt: str) -> str:
    """Read one line without leaving a worker blocked after cancellation.

    The descriptor is temporarily non-blocking while ``TextIOWrapper.readline``
    drains either its own read-ahead buffer or currently available bytes.  Thus a
    partial pipe line cannot block the event loop, while a line already prefetched
    by earlier text I/O remains visible.  A per-loop lock prevents concurrent
    ``HumanPlayer`` tasks from replacing each other's reader; it cannot make
    multiple humans' answers private from one another.
    """

    if os.name != "posix":
        raise _CancellableStdinUnavailable

    loop = asyncio.get_running_loop()
    stream = sys.stdin
    if not isinstance(stream, io.TextIOBase):
        raise _CancellableStdinUnavailable
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError) as exc:
        raise _CancellableStdinUnavailable from exc
    try:
        buffer = _STDIN_BUFFERS.setdefault(stream, [])
    except TypeError as exc:
        raise _CancellableStdinUnavailable from exc

    async with _stdin_lock(loop):
        if "\n" in "".join(buffer):
            sys.stdout.write(prompt)
            sys.stdout.flush()
            line = _pop_stdin_line(buffer)
            if line is None:  # pragma: no cover - guarded by the newline check
                raise RuntimeError("stdin line buffer changed unexpectedly")
            return line

        result: asyncio.Future[str] = loop.create_future()

        def read_ready() -> None:
            if result.done():
                return
            try:
                chunk, eof = _read_text_stream_without_blocking(stream, fd)
            except (OSError, UnicodeError, _CancellableStdinUnavailable, ValueError) as exc:
                result.set_exception(exc)
                return
            if chunk:
                buffer.append(chunk)
                if "\n" in "".join(buffer):
                    result.set_result("line")
                elif eof:
                    result.set_result("eof")
                return
            if eof:
                result.set_result("eof")

        try:
            loop.add_reader(fd, read_ready)
        except (AttributeError, NotImplementedError, OSError, RuntimeError) as exc:
            raise _CancellableStdinUnavailable from exc

        try:
            sys.stdout.write(prompt)
            sys.stdout.flush()
            # ``add_reader`` only observes the descriptor.  Drain once eagerly so
            # TextIOWrapper data prefetched by earlier input is not missed.
            read_ready()
            ready = await result
            if ready == "line":
                line = _pop_stdin_line(buffer)
                if line is None:  # pragma: no cover - callback saw the newline
                    raise RuntimeError("stdin line buffer changed unexpectedly")
                return line
            if buffer:
                line = "".join(buffer)
                buffer.clear()
                return line
            raise EOFError("EOF when reading a line")
        finally:
            loop.remove_reader(fd)


def _archive_sampling_params(params: dict[str, object]) -> dict[str, object]:
    """Only persist scalar sampling controls with no credential-bearing semantics."""

    return {
        key: value
        if key in _ARCHIVE_SAFE_SAMPLING_KEYS
        and (value is None or isinstance(value, (bool, int, float)))
        else _REDACTED
        for key, value in params.items()
    }


def _stable_entrant_id(namespace: str, *parts: str) -> str:
    payload = "\0".join((namespace, *parts)).encode("utf-8")
    return f"{namespace}:{hashlib.sha256(payload).hexdigest()}"


def _canonical_sampling_identity(params: dict[str, object]) -> str:
    """Return the deterministic, credential-free sampling identity payload."""

    return json.dumps(
        _archive_sampling_params(params),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _provider_profile_id(provider: object) -> str | None:
    value = getattr(provider, "profile_id", None)
    return value if isinstance(value, str) and _SAFE_PROFILE_ID.fullmatch(value) else None


def _provider_route_id(provider: object, model: str) -> str:
    route_id_for = getattr(provider, "route_id_for", None)
    if route_id_for is None:
        # Preserve the documented chat-only duck-provider compatibility while
        # giving it the same conservative, attribute-free fallback identity.
        route_id = Provider.route_id_for(provider, model)  # type: ignore[arg-type]
    elif callable(route_id_for):
        route_id = route_id_for(model)
    else:
        raise ValueError("Provider route_id_for 必须是可调用方法")
    return validate_route_id(route_id)


def _provider_usage_support(provider: object, model: str) -> UsageSupport:
    support_for = getattr(provider, "usage_support_for", None)
    if support_for is None:
        return UsageSupport.NONE
    if not callable(support_for):
        raise UsageValidationError("Provider usage_support_for must be callable")
    support = support_for(model)
    if not isinstance(support, UsageSupport):
        raise UsageValidationError("Provider usage_support_for returned an invalid capability")
    return support


def profile_entrant_id(profile_id: str, model: str) -> str:
    """Return the documented stable identity for one Profile/model pair."""

    if not isinstance(profile_id, str) or not _SAFE_PROFILE_ID.fullmatch(profile_id):
        raise ValueError("Profile ID 必须是 1 到 64 位字母、数字、点、下划线或连字符")
    if not isinstance(model, str) or not model:
        raise ValueError("Profile 模型名必须是非空字符串")
    return validate_entrant_id(f"profile:{profile_id}:{model}")


class PlayerActionError(Exception):
    """选手在生成走法时失败，并携带可存档的机器信息。"""

    reason_code = "player_error"

    def __init__(
        self,
        message: str,
        *,
        technical_loss: bool = False,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.technical_loss = technical_loss
        self.details = dict(details or {})


class PlayerTimeoutError(PlayerActionError):
    """选手未在限时内提交走法。"""

    reason_code = "timeout"


class PlayerProviderError(PlayerActionError):
    """模型 Provider 在生成走法时失败。"""

    reason_code = "provider_error"


class Player(ABC):
    """选手基类。

    ``get_move`` 是异步方法：人类选手将来可以经 API/WebSocket 远端提交
    走法，引擎无需改动（参见 DESIGN.md §4）。
    """

    kind: str = "abstract"

    def __init__(self, name: str, *, entrant_id: str | None = None) -> None:
        self.name = name
        self._entrant_id = validate_entrant_id(
            _stable_entrant_id(self.kind, str(name)) if entrant_id is None else entrant_id
        )

    @property
    def entrant_id(self) -> str:
        return self._entrant_id

    @property
    def display_name(self) -> str:
        """Public presentation name; ``name`` remains the in-match compatibility key."""

        return self.name

    @abstractmethod
    async def get_move(self, prompt: str) -> str:
        """看到题面后返回走法文本。超时抛 :class:`PlayerTimeoutError`。"""

    def describe(self) -> dict:
        """写入对局档案的选手描述（类型、模型、采样参数等）。"""
        return {
            "name": self.name,
            "display_name": self.display_name,
            "entrant_id": self.entrant_id,
            "kind": self.kind,
        }


class LLMPlayer(Player):
    """经 Provider 调用大模型的选手。"""

    kind = "llm"

    def __init__(
        self,
        name: str,
        provider: Provider,
        model: str,
        *,
        entrant_id: str | None = None,
        move_timeout_seconds: float | None = DEFAULT_LLM_TIMEOUT_SECONDS,
        max_response_chars: int = DEFAULT_MAX_RESPONSE_CHARS,
        usage_budget: UsageBudgetProtocol | None = None,
        budget_policy: ProviderBudgetPolicy | None = None,
        **sampling_params,
    ) -> None:
        profile_id = _provider_profile_id(provider)
        route_id = _provider_route_id(provider, model)
        sampling_identity = _canonical_sampling_identity(sampling_params)
        generated_entrant_id = (
            profile_entrant_id(profile_id, model)
            if profile_id is not None
            else _stable_entrant_id(
                "llm",
                "direct",
                str(provider.name),
                model,
                str(name),
                sampling_identity,
            )
        )
        super().__init__(
            name,
            entrant_id=generated_entrant_id if entrant_id is None else entrant_id,
        )
        reserved_params = {"request_timeout", "output_token_cap"} & set(sampling_params)
        if reserved_params:
            raise ValueError(
                f"{', '.join(sorted(reserved_params))} 是 Provider 内部保留参数"
            )
        if move_timeout_seconds is not None and (
            not math.isfinite(move_timeout_seconds) or move_timeout_seconds <= 0
        ):
            raise ValueError("LLM 单步超时必须是大于 0 的有限秒数")
        if (
            isinstance(max_response_chars, bool)
            or not isinstance(max_response_chars, int)
            or not 1 <= max_response_chars <= DEFAULT_MAX_RESPONSE_CHARS
        ):
            raise ValueError(f"LLM 响应字符上限必须是 1 到 {DEFAULT_MAX_RESPONSE_CHARS} 之间的整数")
        if (usage_budget is None) != (budget_policy is None):
            raise UsageValidationError("usage_budget and budget_policy must be configured together")
        if move_timeout_seconds is not None:
            async_implementation = getattr(type(provider), "achat", None)
            if (
                async_implementation is None
                or async_implementation is Provider.achat
                or not inspect.iscoroutinefunction(async_implementation)
            ):
                raise ValueError(
                    f"Provider {provider.name!r} 没有原生异步调用，不能启用可靠的 LLM 超时"
                )
        self.provider = provider
        self.model = model
        self.profile_id = profile_id
        self._route_id = route_id
        self._include_route_id_in_description = True
        self.move_timeout_seconds = move_timeout_seconds
        self.max_response_chars = max_response_chars
        self.sampling_params = sampling_params
        self._usage_budget: UsageBudgetProtocol | None = None
        self._budget_policy: ProviderBudgetPolicy | None = None
        self._usage_support = _provider_usage_support(provider, model)
        self._token_price: TokenPrice | None = None
        self._output_token_cap: int | None = None
        self._usage_calls_started = False
        self._usage_binding_lock = Lock()
        self._bound_model: str | None = None
        self._bound_sampling_params: dict[str, object] | None = None
        if usage_budget is not None:
            if budget_policy is None:  # pragma: no cover - pair guard above
                raise UsageValidationError("usage budget is missing its frozen policy")
            self.bind_usage_budget(usage_budget, budget_policy)

    @property
    def route_id(self) -> str:
        return self._route_id

    @property
    def usage_budget(self) -> UsageBudgetProtocol | None:
        return self._usage_budget

    @property
    def budget_policy(self) -> ProviderBudgetPolicy | None:
        return self._budget_policy

    @staticmethod
    def _snapshot_budget_sampling_params(
        sampling_params: dict[str, object],
    ) -> dict[str, object]:
        return dict(sampling_params)

    def _enforce_budget_request_stability(self) -> None:
        if self._usage_budget is None:
            return
        if (
            self._bound_model is None
            or self._bound_sampling_params is None
            or self.model != self._bound_model
            or self.sampling_params != self._bound_sampling_params
        ):
            raise UsageValidationError("budgeted LLM request parameters changed after binding")
        self._validate_budget_sampling_params(self._bound_sampling_params)

    def bind_usage_budget(
        self,
        usage_budget: UsageBudgetProtocol,
        budget_policy: ProviderBudgetPolicy,
    ) -> None:
        """Bind one shared ledger and frozen policy exactly once, before calls start."""

        with self._usage_binding_lock:
            if self._usage_calls_started:
                raise UsageValidationError("cannot bind a usage budget after an LLM call started")
            if self._usage_budget is not None or self._budget_policy is not None:
                raise UsageValidationError("LLMPlayer usage budget is already bound")
            try:
                limits = usage_budget.limits
            except AttributeError as exc:
                raise UsageValidationError(
                    "usage budget must expose immutable BudgetLimits"
                ) from exc

            if not isinstance(limits, BudgetLimits):
                raise UsageValidationError("usage budget limits must be BudgetLimits")
            if not callable(getattr(usage_budget, "reserve", None)) or not callable(
                getattr(usage_budget, "reserve_many", None)
            ):
                raise UsageValidationError(
                    "usage budget must support reserve and reserve_many"
                )
            if not callable(getattr(usage_budget, "owns", None)):
                raise UsageValidationError("usage budget must support reservation ownership checks")
            if not isinstance(budget_policy, ProviderBudgetPolicy):
                raise UsageValidationError("budget_policy must be ProviderBudgetPolicy")

            token_price = budget_policy.price_for(self.route_id)
            strict_resource_budget = any(
                limit is not None
                for limit in (limits.input, limits.output, limits.estimated_cost)
            ) or budget_policy.max_output_tokens_per_call != DEFAULT_MAX_OUTPUT_TOKENS
            bound_sampling_params = self._snapshot_budget_sampling_params(self.sampling_params)
            self._validate_budget_sampling_params(bound_sampling_params)
            if strict_resource_budget and self._usage_support is UsageSupport.NONE:
                raise UsageValidationError(
                    "Provider does not report usage required by the token or cost budget"
                )

            if (
                limits.estimated_cost is not None
                and token_price is None
                and self._usage_support is not UsageSupport.EXACT_ZERO
            ):
                raise UsageValidationError(
                    "the cost budget requires a frozen token price for every billed route"
                )

            output_token_cap: int | None = None
            if self._usage_support is UsageSupport.EXACT_ZERO:
                output_token_cap = 0
            elif self._usage_support is UsageSupport.REPORTED:
                resolve_cap = getattr(self.provider, "resolve_output_token_cap", None)
                if callable(resolve_cap):
                    output_token_cap = resolve_cap(
                        self.model,
                        requested_cap=budget_policy.max_output_tokens_per_call,
                        params=bound_sampling_params,
                    )

                if output_token_cap is None:
                    if strict_resource_budget:
                        raise UsageValidationError(
                            "Provider cannot enforce the output cap required by the budget"
                        )
                elif (
                    isinstance(output_token_cap, bool)
                    or not isinstance(output_token_cap, int)
                    or output_token_cap < 1
                ):
                    raise UsageValidationError("Provider returned an invalid output token cap")
                elif not callable(
                    getattr(self.provider, "achat_with_usage", None)
                ) and not callable(getattr(self.provider, "chat_with_usage", None)):
                    raise UsageValidationError(
                        "Provider cannot return usage for its negotiated output cap"
                    )

            self._usage_budget = usage_budget
            self._budget_policy = budget_policy
            self._token_price = token_price
            self._output_token_cap = output_token_cap
            self._bound_model = self.model
            self._bound_sampling_params = bound_sampling_params

    def _validate_budget_sampling_params(self, sampling_params: dict[str, object]) -> None:
        """Reject sampling parameters that can mutate request body semantics."""

        for key in sampling_params:
            lowered = key.lower()
            if lowered in _BUDGET_FORBIDDEN_SAMPLING_KEYS:
                raise UsageValidationError(
                    f"budgeted Provider calls do not accept sampling parameter {key!r}"
                )
            if lowered.startswith("extra_") or "tool" in lowered or "function" in lowered:
                raise UsageValidationError(
                    f"budgeted Provider calls do not accept sampling parameter {key!r}"
                )

    @staticmethod
    def _completion_messages(prompt: str, system_prompt: str) -> list[dict]:
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

    def call_bounds(
        self,
        prompt: str,
        *,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> CallBounds | None:
        """Return the frozen conservative authorization for one model call."""

        self._enforce_budget_request_stability()
        if self._usage_budget is None:
            return None
        if self._usage_support is UsageSupport.NONE or self._output_token_cap is None:
            # Calls-only budgets remain compatible with legacy adapters. They
            # deliberately make no token or cost claim.
            return CallBounds(route_id=self.route_id)
        if self._usage_support is UsageSupport.EXACT_ZERO:
            return CallBounds(route_id=self.route_id)

        messages = self._completion_messages(prompt, system_prompt)
        canonical_messages = json.dumps(
            messages,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        input_bound = len(canonical_messages)
        output_bound = self._output_token_cap
        estimated_cost = (
            0
            if self._token_price is None
            else self._token_price.estimate(
                input_tokens=input_bound,
                output_tokens=output_bound,
            )
        )
        return CallBounds(
            input=input_bound,
            output=output_bound,
            estimated_cost=estimated_cost,
            route_id=self.route_id,
        )

    def _usage_totals(self, usage: object) -> UsageTotals:
        if not isinstance(usage, ProviderUsage):
            raise UsageValidationError("Provider usage must be ProviderUsage")
        estimated_cost = (
            0
            if self._token_price is None
            else self._token_price.estimate(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )
        )
        return UsageTotals(
            calls=1,
            input=usage.input_tokens,
            output=usage.output_tokens,
            estimated_cost=estimated_cost,
        )

    def _use_legacy_route_description(self) -> None:
        """Keep old tournament checkpoint descriptors stable during resume."""

        self._include_route_id_in_description = False

    async def complete_with_usage(
        self,
        prompt: str,
        *,
        system_prompt: str = SYSTEM_PROMPT,
        reservation: UsageReservationProtocol | None = None,
    ) -> ProviderChatResult:
        """Return model text and optional usage under the existing safety boundary."""

        with self._usage_binding_lock:
            self._usage_calls_started = True
        messages = self._completion_messages(prompt, system_prompt)
        expected_bounds = self.call_bounds(prompt, system_prompt=system_prompt)
        if expected_bounds is None:
            if reservation is not None:
                raise UsageValidationError("an unbudgeted LLM call cannot accept a reservation")
        else:
            if reservation is None:
                budget = self._usage_budget
                if budget is None:  # pragma: no cover - narrowed by call_bounds
                    raise UsageValidationError("budgeted LLM call is missing its usage budget")
                reservation = budget.reserve(expected_bounds)
            elif reservation.budget_id != self._usage_budget.budget_id or not self._usage_budget.owns(
                reservation
            ):
                raise UsageValidationError("reservation does not belong to this player's usage budget")
            elif reservation.bounds != expected_bounds:
                raise UsageValidationError("call reservation does not match the frozen CallBounds")
            try:
                reservation.dispatch()
            except BaseException:
                if reservation.state == "reserved":
                    reservation.release_pre_dispatch()
                raise

        if self._usage_budget is None:
            call_model = self.model
            call_params = dict(self.sampling_params)
        else:
            if self._bound_model is None or self._bound_sampling_params is None:
                raise UsageValidationError("player budgeted state is incomplete")
            call_model = self._bound_model
            call_params = self._snapshot_budget_sampling_params(self._bound_sampling_params)
        if self._output_token_cap is not None:
            call_params["output_token_cap"] = self._output_token_cap
        try:
            async_chat_with_usage = getattr(self.provider, "achat_with_usage", None)
            if async_chat_with_usage is not None:
                call = async_chat_with_usage(
                    messages,
                    model=call_model,
                    request_timeout=self.move_timeout_seconds,
                    **call_params,
                )
                if self.move_timeout_seconds is None:
                    raw_result = await call
                else:
                    async with asyncio.timeout(self.move_timeout_seconds):
                        raw_result = await call
            else:
                async_chat = getattr(self.provider, "achat", None)
                if async_chat is None:
                    sync_chat_with_usage = getattr(self.provider, "chat_with_usage", None)
                    sync_chat = (
                        sync_chat_with_usage
                        if sync_chat_with_usage is not None
                        else self.provider.chat
                    )
                    raw_result = await asyncio.to_thread(
                        sync_chat,
                        messages,
                        model=call_model,
                        **call_params,
                    )
                else:
                    call = async_chat(
                        messages,
                        model=call_model,
                        request_timeout=self.move_timeout_seconds,
                        **call_params,
                    )
                    if self.move_timeout_seconds is None:
                        raw_result = await call
                    else:
                        async with asyncio.timeout(self.move_timeout_seconds):
                            raw_result = await call
        except UsageError:
            if reservation is not None:
                reservation.charge_unknown()
            raise
        except ProviderTimeoutError as exc:
            if reservation is not None:
                reservation.charge_unknown()
            raise self._timeout_error() from exc
        except TimeoutError as exc:
            if reservation is not None:
                reservation.charge_unknown()
            raise self._timeout_error() from exc
        except Exception as exc:
            if reservation is not None:
                reservation.charge_unknown()
            raise PlayerProviderError(
                f"{self.provider.name} 模型服务调用失败，判技术负",
                technical_loss=True,
                details={
                    "provider": self.provider.name,
                    "model": self.model,
                    "error_type": type(exc).__name__,
                },
            ) from exc
        except BaseException:
            if reservation is not None:
                reservation.charge_unknown()
            raise
        result = (
            raw_result
            if isinstance(raw_result, ProviderChatResult)
            else ProviderChatResult(text=raw_result)
        )
        if reservation is not None:
            try:
                reservation.settle(
                    None if result.usage is None else self._usage_totals(result.usage)
                )
            except UsageError:
                # Invalid counters can fail before the ledger sees an actual
                # value.  Once dispatched, unknown usage is always charged at
                # the authorized bound; ledger-recorded overruns are already a
                # terminal (and poisoned) state and must not be overwritten.
                if reservation.state in {"reserved", "dispatched"}:
                    reservation.charge_unknown()
                raise
        response = result.text
        if not isinstance(response, str):
            raise PlayerProviderError(
                f"{self.provider.name} 模型返回了非文本响应，判技术负",
                technical_loss=True,
                details={
                    "provider": self.provider.name,
                    "model": self.model,
                    "validation_error": "non_string_response",
                },
            )
        if len(response) > self.max_response_chars:
            raise PlayerProviderError(
                f"{self.provider.name} 模型响应超过 {self.max_response_chars} 字符上限，判技术负",
                technical_loss=True,
                details={
                    "provider": self.provider.name,
                    "model": self.model,
                    "validation_error": "response_too_long",
                    "max_response_chars": self.max_response_chars,
                },
            )
        return result

    async def complete(
        self,
        prompt: str,
        *,
        system_prompt: str = SYSTEM_PROMPT,
        reservation: UsageReservationProtocol | None = None,
    ) -> str:
        """调用底层模型并执行与参赛走法相同的超时、错误和大小隔离。"""

        return (
            await self.complete_with_usage(
                prompt,
                system_prompt=system_prompt,
                reservation=reservation,
            )
        ).text

    async def get_move(
        self,
        prompt: str,
        *,
        reservation: UsageReservationProtocol | None = None,
    ) -> str:
        return await self.complete(prompt, reservation=reservation)

    def _timeout_error(self) -> PlayerTimeoutError:
        details: dict[str, object] = {
            "provider": self.provider.name,
            "model": self.model,
        }
        if self.move_timeout_seconds is None:
            message = f"{self.provider.name} 模型请求超时，判技术负"
        else:
            message = (
                f"{self.provider.name} 模型未在 {self.move_timeout_seconds:g} 秒内响应，判技术负"
            )
            details["timeout_seconds"] = self.move_timeout_seconds
        return PlayerTimeoutError(
            message,
            technical_loss=True,
            details=details,
        )

    def describe(self) -> dict:
        description = {
            "name": self.name,
            "display_name": self.display_name,
            "entrant_id": self.entrant_id,
            "kind": self.kind,
            "provider": self.provider.name,
            "model": self.model,
            "sampling_params": _archive_sampling_params(self.sampling_params),
            "max_response_chars": self.max_response_chars,
        }
        if self._include_route_id_in_description:
            description["route_id"] = self.route_id
        if self.profile_id is not None:
            description["profile_id"] = self.profile_id
        if self.move_timeout_seconds is not None:
            description["move_timeout_seconds"] = self.move_timeout_seconds
        return description


def reserve_player_call_batch(
    requests: Iterable[tuple[Player, str, str]],
) -> tuple[UsageReservationProtocol | None, ...]:
    """Reserve every budgeted LLM request before any Provider task starts."""

    batch = tuple(requests)
    llm_players = [player for player, _, _ in batch if isinstance(player, LLMPlayer)]
    budgeted = [player for player in llm_players if player.usage_budget is not None]
    if not budgeted:
        return tuple(None for _ in batch)
    if len(budgeted) != len(llm_players):
        raise UsageValidationError(
            "one Provider call batch cannot mix budgeted and unbudgeted LLM players"
        )

    budget = budgeted[0].usage_budget
    policy = budgeted[0].budget_policy
    if budget is None or policy is None:  # pragma: no cover - narrowed by construction
        raise UsageValidationError("budgeted LLM player is missing its frozen policy")
    if any(player.usage_budget is not budget for player in budgeted[1:]):
        raise UsageValidationError("one Provider call batch must share one usage budget")
    if any(player.budget_policy != policy for player in budgeted[1:]):
        raise UsageValidationError("one Provider call batch must share one frozen budget policy")

    bounds: list[CallBounds] = []
    budgeted_indices: list[int] = []
    for index, (player, prompt, system_prompt) in enumerate(batch):
        if not isinstance(player, LLMPlayer):
            continue
        bound = player.call_bounds(prompt, system_prompt=system_prompt)
        if bound is None:  # pragma: no cover - rejected by the mixed-mode guard
            raise UsageValidationError("budgeted LLM call is missing CallBounds")
        budgeted_indices.append(index)
        bounds.append(bound)

    reservations = tuple(budget.reserve_many(bounds))
    if len(reservations) != len(bounds) or any(
        reservation.bounds != bound
        for reservation, bound in zip(reservations, bounds, strict=False)
    ):
        for reservation in reservations:
            reservation.release_pre_dispatch()
        raise UsageValidationError("usage budget returned invalid batch reservations")

    aligned: list[UsageReservationProtocol | None] = [None] * len(batch)
    for index, reservation in zip(budgeted_indices, reservations, strict=True):
        aligned[index] = reservation
    return tuple(aligned)


def release_undispatched_reservations(
    reservations: Iterable[UsageReservationProtocol | None],
) -> None:
    """Release only reservations whose Provider coroutine never dispatched."""

    for reservation in reservations:
        if reservation is not None and reservation.state == "reserved":
            reservation.release_pre_dispatch()


class HumanPlayer(Player):
    """人类选手：CLI 里等待键盘输入；接口已为将来 API 远端提交留好路。"""

    kind = "human"

    def __init__(
        self,
        name: str,
        timeout: float | None = 60.0,
        input_fn: Callable[[str], str] = _DEFAULT_INPUT,
        *,
        entrant_id: str | None = None,
    ) -> None:
        super().__init__(name, entrant_id=entrant_id)
        if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("人类行动超时必须是大于 0 的有限秒数")
        self.timeout = timeout
        self._input_fn = input_fn

    async def get_move(self, prompt: str) -> str:
        try:
            input_prompt = f"{self.name}，请输入你的答案 > "
            if self._input_fn is _DEFAULT_INPUT:

                async def read_input() -> str:
                    try:
                        return await _read_default_stdin(input_prompt)
                    except _CancellableStdinUnavailable:
                        return await asyncio.to_thread(self._input_fn, input_prompt)

                coro = read_input()
            else:
                coro = asyncio.to_thread(self._input_fn, input_prompt)
            return await asyncio.wait_for(coro, timeout=self.timeout)
        except TimeoutError as exc:  # 3.11+ 中 wait_for 超时抛内置 TimeoutError
            raise PlayerTimeoutError(
                f"{self.name} 超时未作答",
                details={"timeout_seconds": self.timeout},
            ) from exc
