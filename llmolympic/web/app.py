"""Loopback-only browser participation plus live and archived observation."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from fastapi import FastAPI, Query, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect

from llmolympic import __version__
from llmolympic.config import ProviderProfile
from llmolympic.control import (
    MAX_CONTROL_BODY_BYTES,
    ControlCatalogResponse,
    ControlError,
    ControlJob,
    ControlJobListResponse,
    ControlJobResponse,
    ControlJobSpec,
    ControlProfileCredentialRequest,
    JobStore,
    control_catalog,
    validate_job_spec,
)
from llmolympic.games import GAME_REGISTRY
from llmolympic.human_input import HumanInputError, WebSubmissionStore
from llmolympic.web.live_reader import LiveReadError, LiveSQLiteReader
from llmolympic.web.models import (
    ErrorResponse,
    GameInfo,
    GameListResponse,
    HealthResponse,
    LeaderboardResponse,
    LiveMatchDetail,
    LiveMatchListResponse,
    MatchDetail,
    MatchListResponse,
    ParticipationSnapshotResponse,
    ParticipationSubmissionRequest,
    ParticipationSubmissionResponse,
    WSArchiveEnvelope,
    WSCompleteEnvelope,
    WSEventEnvelope,
    WSLiveCompleteEnvelope,
    WSLiveEventEnvelope,
    WSLiveInterruptedEnvelope,
    WSLiveSnapshotEnvelope,
)
from llmolympic.web.reader import MAX_ARCHIVE_EVENTS, WebReadError, WebSQLiteReader

MAX_CONCURRENT_READS = 16
MAX_CONCURRENT_REPLAYS = 8
MAX_CONCURRENT_LIVE_STREAMS = 16
WEBSOCKET_SEND_TIMEOUT_SECONDS = 5.0
LIVE_POLL_SECONDS = 0.2
LIVE_PAGE_LIMIT = 256
MAX_CONCURRENT_INPUT_REQUESTS = 8
MAX_INPUT_BODY_BYTES = 32 * 1024
MAX_CONCURRENT_CONTROL_REQUESTS = 4

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_SAFE_PATH_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SAFE_GAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")

_BASE_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
_API_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'none'; object-src 'none'"
)


def _security_headers(
    *,
    ui: bool = False,
    host: str | None = None,
    scheme: str = "http",
) -> dict[str, str]:
    headers = dict(_BASE_SECURITY_HEADERS)
    if not ui:
        headers["Content-Security-Policy"] = _API_CONTENT_SECURITY_POLICY
        return headers
    websocket_scheme = "wss" if scheme in {"https", "wss"} else "ws"
    headers["Content-Security-Policy"] = (
        "default-src 'none'; "
        "script-src 'self'; script-src-attr 'none'; "
        "style-src 'self'; style-src-attr 'none'; "
        "img-src 'self' data:; "
        f"connect-src 'self' {websocket_scheme}://{host}; "
        "font-src 'none'; object-src 'none'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'none'; manifest-src 'none'; worker-src 'none'"
    )
    return headers

_READ_ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Invalid request"},
    503: {"model": ErrorResponse, "description": "Database unavailable or invalid"},
}

_DETAIL_ERROR_RESPONSES = {
    **_READ_ERROR_RESPONSES,
    404: {"model": ErrorResponse, "description": "Match not found"},
    409: {"model": ErrorResponse, "description": "Archive is not replayable"},
    413: {"model": ErrorResponse, "description": "Archive exceeds Web limits"},
}

_LIVE_ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Invalid request"},
    404: {"model": ErrorResponse, "description": "Live session not found"},
    503: {"model": ErrorResponse, "description": "Live event stream unavailable"},
}

_PARTICIPATION_ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Invalid request"},
    404: {"model": ErrorResponse, "description": "Participation seat not found"},
    409: {"model": ErrorResponse, "description": "Submission conflicts with current request"},
    410: {"model": ErrorResponse, "description": "Participation request expired"},
    413: {"model": ErrorResponse, "description": "Submission body too large"},
    429: {"model": ErrorResponse, "description": "Participation input overloaded"},
    503: {"model": ErrorResponse, "description": "Participation input unavailable"},
}

_CONTROL_ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Invalid control request"},
    401: {"model": ErrorResponse, "description": "Admin capability required"},
    403: {"model": ErrorResponse, "description": "Same-origin request required"},
    404: {"model": ErrorResponse, "description": "Job not found"},
    409: {"model": ErrorResponse, "description": "Job state conflict"},
    413: {"model": ErrorResponse, "description": "Control body too large"},
    429: {"model": ErrorResponse, "description": "Control plane overloaded"},
    503: {"model": ErrorResponse, "description": "Control plane unavailable"},
}


def _error(code: str, status_code: int) -> JSONResponse:
    payload = ErrorResponse(error={"code": code})
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json"),
        headers=_security_headers(),
    )


def _public_read_error(exc: WebReadError) -> tuple[str, int]:
    code = exc.code
    if code in {"invalid_game_id", "invalid_limit", "invalid_match_id"}:
        return "invalid_request", 400
    if code == "match_not_found":
        return code, 404
    if code in {"archive_event_limit_exceeded", "archive_too_large"}:
        return "archive_too_large", 413
    if code == "database_busy":
        return "database_busy", 503
    if code.startswith("database_"):
        return "database_unavailable", 503
    if code == "match_detail_unsupported":
        return "archive_not_replayable", 409
    return "archive_invalid", 503


def _public_live_error(exc: LiveReadError) -> tuple[str, int]:
    if exc.code in {"invalid_game_id", "invalid_limit", "invalid_from_seq"}:
        return "invalid_request", 400
    if exc.code == "live_not_found":
        return exc.code, 404
    if exc.code == "database_busy":
        return "database_busy", 503
    return "live_unavailable", 503


def _public_input_error(exc: HumanInputError) -> tuple[str, int]:
    if exc.code == "invalid_request":
        return "invalid_request", 400
    if exc.code in {"input_forbidden", "capability_invalid", "participation_not_found"}:
        return "participation_not_found", 404
    if exc.code in {"request_not_found", "request_stale", "input_not_ready"}:
        return "request_stale", 409
    if exc.code == "already_submitted":
        return "submission_conflict", 409
    if exc.code == "request_expired":
        return "request_expired", 410
    if exc.code == "input_body_too_large":
        return "request_too_large", 413
    if exc.code in {"session_interrupted", "input_interrupted"}:
        return "participation_expired", 410
    if exc.code == "input_overloaded":
        return "overloaded", 429
    return "participation_unavailable", 503


def _public_control_error(exc: ControlError) -> tuple[str, int]:
    if exc.code in {
        "invalid_request",
        "large_tournament_confirmation_required",
        "profile_unavailable",
    }:
        return exc.code, 400
    if exc.code == "control_unauthorized":
        return exc.code, 401
    if exc.code == "cross_origin_forbidden":
        return exc.code, 403
    if exc.code == "job_not_found":
        return exc.code, 404
    if exc.code in {
        "budget_required",
        "idempotency_conflict",
        "job_conflict",
        "job_not_stoppable",
        "job_capacity",
        "provider_pricing_required",
        "resume_unavailable",
    }:
        public_code = (
            "job_state_conflict"
            if exc.code in {"job_conflict", "job_not_stoppable"}
            else exc.code
        )
        return public_code, 409
    if exc.code == "control_body_too_large":
        return "request_too_large", 413
    if exc.code in {"control_overloaded", "job_queue_full"}:
        return exc.code, 429
    return "control_unavailable", 503


class _ControlManager(Protocol):
    def public_job(self, job: ControlJob) -> ControlJob: ...

    def profile_credential_ready(self, profile: ProviderProfile) -> bool: ...

    async def set_profile_credential(self, profile_id: str, api_key: str) -> None: ...

    async def clear_profile_credential(self, profile_id: str) -> None: ...

    async def start(
        self,
        job_id: str,
        *,
        idempotency_key: str,
        web_base_url: str,
    ) -> ControlJob: ...

    async def cancel(
        self,
        job_id: str,
        *,
        idempotency_key: str,
    ) -> ControlJob: ...

    async def shutdown(self) -> None: ...


def _admin_capability(request: Request, expected: str | None) -> None:
    value = request.headers.get("authorization")
    if (
        expected is None
        or value is None
        or len(value) > 512
        or not value.startswith("Bearer ")
    ):
        raise ControlError("control_unauthorized")
    supplied = value.removeprefix("Bearer ")
    if (
        re.fullmatch(r"[A-Za-z0-9_-]{32,256}", supplied) is None
        or not secrets.compare_digest(supplied, expected)
    ):
        raise ControlError("control_unauthorized")


def _control_idempotency_key(request: Request) -> str:
    value = request.headers.get("idempotency-key")
    if value is None or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None:
        raise ControlError("invalid_request")
    return value


def _require_control_write_context(request: Request) -> None:
    origin = request.headers.get("origin")
    fetch_site = request.headers.get("sec-fetch-site")
    if not _same_loopback_origin(origin, request.headers.get("host"), request.url.scheme) or (
        fetch_site is not None and fetch_site.casefold() != "same-origin"
    ):
        raise ControlError("cross_origin_forbidden")


async def _bounded_control_body(request: Request) -> object:
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().casefold() != "application/json":
        raise ControlError("invalid_request")
    content_length = request.headers.get("content-length")
    if content_length is not None:
        if not content_length.isascii() or not content_length.isdigit():
            raise ControlError("invalid_request")
        if int(content_length) > MAX_CONTROL_BODY_BYTES:
            raise ControlError("control_body_too_large")
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > MAX_CONTROL_BODY_BYTES:
            raise ControlError("control_body_too_large")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlError("invalid_request") from exc


def _bearer_capability(request: Request) -> str:
    value = request.headers.get("authorization")
    if value is None or len(value) > 512 or not value.startswith("Bearer "):
        raise HumanInputError("input_forbidden")
    capability = value.removeprefix("Bearer ")
    if re.fullmatch(r"[A-Za-z0-9_-]{32,256}", capability) is None:
        raise HumanInputError("input_forbidden")
    return capability


async def _bounded_json_body(request: Request) -> object:
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().casefold() != "application/json":
        raise HumanInputError("invalid_request")
    content_length = request.headers.get("content-length")
    if content_length is not None:
        if not content_length.isascii() or not content_length.isdigit():
            raise HumanInputError("invalid_request")
        if int(content_length) > MAX_INPUT_BODY_BYTES:
            raise HumanInputError("input_body_too_large")
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > MAX_INPUT_BODY_BYTES:
            raise HumanInputError("input_body_too_large")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HumanInputError("invalid_request") from exc


def _same_loopback_origin(origin: str | None, host: str | None, scheme: str) -> bool:
    if not origin or len(origin) > 256 or any(ord(character) < 32 for character in origin):
        return False
    if not _loopback_host_header(host):
        return False
    try:
        parsed = urlsplit(origin)
        parsed_host = urlsplit(f"//{host}")
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        expected_secure = scheme in {"https", "wss"}
        expected_port = parsed_host.port or (443 if expected_secure else 80)
    except ValueError:
        return False
    return (
        parsed.scheme == ("https" if expected_secure else "http")
        and parsed.hostname is not None
        and parsed.hostname.casefold() in _LOOPBACK_HOSTS
        and parsed_host.hostname is not None
        and parsed.hostname.casefold() == parsed_host.hostname.casefold()
        and origin_port == expected_port
        and not parsed.username
        and not parsed.password
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
    )


def _loopback_host_header(host: str | None) -> bool:
    if not host or len(host) > 256 or any(ord(character) < 32 for character in host):
        return False
    candidate = host.strip()
    try:
        parsed = urlsplit(f"//{candidate}")
        port = parsed.port
    except ValueError:
        return False
    del port
    return (
        parsed.hostname is not None
        and parsed.hostname.casefold() in _LOOPBACK_HOSTS
        and not parsed.username
        and not parsed.password
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
    )


async def _send_json(websocket: WebSocket, payload: object) -> None:
    await asyncio.wait_for(
        websocket.send_json(payload),
        timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS,
    )


def create_app(
    database: str | Path,
    *,
    control_token: str | None = None,
    control_manager: _ControlManager | None = None,
) -> FastAPI:
    """Build the local Web app without opening any database at import time."""

    if control_token is not None and re.fullmatch(
        r"[A-Za-z0-9_-]{32,256}", control_token
    ) is None:
        raise ValueError("control_token must be a 32-256 character URL-safe capability")
    reader = WebSQLiteReader(database)
    live_reader = LiveSQLiteReader(database)
    input_store = WebSubmissionStore(database)
    job_store = JobStore(database) if control_token is not None else None
    if job_store is not None and control_manager is None:
        from llmolympic.control_runner import ControlJobManager

        control_manager = ControlJobManager(job_store)
    read_slots = asyncio.Semaphore(MAX_CONCURRENT_READS)
    replay_slots = asyncio.Semaphore(MAX_CONCURRENT_REPLAYS)
    live_slots = asyncio.Semaphore(MAX_CONCURRENT_LIVE_STREAMS)
    input_slots = asyncio.Semaphore(MAX_CONCURRENT_INPUT_REQUESTS)
    control_slots = asyncio.Semaphore(MAX_CONCURRENT_CONTROL_REQUESTS)
    static_root = Path(__file__).with_name("static")
    index_path = static_root / "index.html"
    assets_path = static_root / "assets"
    app = FastAPI(
        title="LLM Olympics local Web API",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    generated_openapi = app.openapi

    def public_openapi() -> dict:
        schema = generated_openapi()
        for path_item in schema.get("paths", {}).values():
            if not isinstance(path_item, dict):
                continue
            for operation in path_item.values():
                if isinstance(operation, dict):
                    operation.get("responses", {}).pop("422", None)
        components = schema.get("components", {}).get("schemas", {})
        components.pop("HTTPValidationError", None)
        components.pop("ValidationError", None)
        return schema

    app.openapi = public_openapi

    @app.middleware("http")
    async def secure_local_requests(
        request: Request,
        call_next: Callable[[Request], Awaitable[object]],
    ):
        if not _loopback_host_header(request.headers.get("host")):
            return _error("invalid_host", 400)
        origin = request.headers.get("origin")
        if origin is not None and not _same_loopback_origin(
            origin,
            request.headers.get("host"),
            request.url.scheme,
        ):
            return _error("cross_origin_forbidden", 403)
        if request.headers.get("sec-fetch-site", "").casefold() == "cross-site":
            return _error("cross_origin_forbidden", 403)
        response = await call_next(request)
        is_ui_document = response.headers.get("content-type", "").startswith("text/html")
        response.headers.update(
            _security_headers(
                ui=is_ui_document,
                host=request.headers.get("host"),
                scheme=request.url.scheme,
            )
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        del request, exc
        return _error("invalid_request", 400)

    @app.exception_handler(WebReadError)
    async def read_error_handler(request: Request, exc: WebReadError) -> JSONResponse:
        del request
        code, status = _public_read_error(exc)
        return _error(code, status)

    @app.exception_handler(LiveReadError)
    async def live_read_error_handler(request: Request, exc: LiveReadError) -> JSONResponse:
        del request
        code, status = _public_live_error(exc)
        return _error(code, status)

    @app.exception_handler(HumanInputError)
    async def human_input_error_handler(
        request: Request,
        exc: HumanInputError,
    ) -> JSONResponse:
        del request
        code, status = _public_input_error(exc)
        return _error(code, status)

    @app.exception_handler(ControlError)
    async def control_error_handler(request: Request, exc: ControlError) -> JSONResponse:
        del request
        code, status = _public_control_error(exc)
        return _error(code, status)

    async def read(callable_: Callable[[], object]) -> object:
        async with read_slots:
            return await run_in_threadpool(callable_)

    async def input_call(callable_: Callable[[], object]) -> object:
        try:
            await asyncio.wait_for(input_slots.acquire(), timeout=0.05)
        except TimeoutError as exc:
            raise HumanInputError("input_overloaded") from exc
        try:
            return await run_in_threadpool(callable_)
        finally:
            input_slots.release()

    async def control_call(callable_: Callable[[], object]) -> object:
        try:
            await asyncio.wait_for(control_slots.acquire(), timeout=0.05)
        except TimeoutError as exc:
            raise ControlError("control_overloaded") from exc
        try:
            return await run_in_threadpool(callable_)
        finally:
            control_slots.release()

    def require_control(request: Request) -> JobStore:
        _admin_capability(request, control_token)
        if job_store is None:
            raise ControlError("control_unavailable")
        return job_store

    def require_control_manager(request: Request) -> _ControlManager:
        _admin_capability(request, control_token)
        if control_manager is None:
            raise ControlError("control_unavailable")
        return control_manager

    def public_job(job: ControlJob) -> ControlJob:
        return control_manager.public_job(job) if control_manager is not None else job

    if control_manager is not None:
        app.router.add_event_handler("shutdown", control_manager.shutdown)

    app.mount("/assets", StaticFiles(directory=assets_path), name="observer-assets")

    @app.get("/", include_in_schema=False, response_class=FileResponse)
    async def observer_home() -> FileResponse:
        return FileResponse(index_path, media_type="text/html; charset=utf-8")

    @app.get("/matches/{match_id}", include_in_schema=False, response_class=FileResponse)
    async def observer_match(match_id: str) -> Response:
        if _SAFE_PATH_ID_RE.fullmatch(match_id) is None:
            return Response(status_code=404)
        return FileResponse(index_path, media_type="text/html; charset=utf-8")

    @app.get("/live/{live_id}", include_in_schema=False, response_class=FileResponse)
    async def observer_live(live_id: str) -> Response:
        if _SAFE_PATH_ID_RE.fullmatch(live_id) is None:
            return Response(status_code=404)
        return FileResponse(index_path, media_type="text/html; charset=utf-8")

    @app.get("/new", include_in_schema=False, response_class=FileResponse)
    async def control_new_page() -> FileResponse:
        return FileResponse(index_path, media_type="text/html; charset=utf-8")

    @app.get("/jobs/{job_id}", include_in_schema=False, response_class=FileResponse)
    async def control_job_page(job_id: str) -> Response:
        if _SAFE_PATH_ID_RE.fullmatch(job_id) is None:
            return Response(status_code=404)
        return FileResponse(index_path, media_type="text/html; charset=utf-8")

    @app.get(
        "/participate/{session_id}/{seat_id}",
        include_in_schema=False,
        response_class=FileResponse,
    )
    async def participate_page(session_id: str, seat_id: str) -> Response:
        if (
            _SAFE_PATH_ID_RE.fullmatch(session_id) is None
            or _SAFE_PATH_ID_RE.fullmatch(seat_id) is None
        ):
            return Response(status_code=404)
        return FileResponse(index_path, media_type="text/html; charset=utf-8")

    @app.get(
        "/api/v1/participation/{session_id}/{seat_id}",
        response_model=ParticipationSnapshotResponse,
        responses=_PARTICIPATION_ERROR_RESPONSES,
    )
    async def participation_snapshot(
        request: Request,
        session_id: str,
        seat_id: str,
    ) -> ParticipationSnapshotResponse:
        if (
            _SAFE_PATH_ID_RE.fullmatch(session_id) is None
            or _SAFE_PATH_ID_RE.fullmatch(seat_id) is None
        ):
            raise HumanInputError("participation_not_found")
        capability = _bearer_capability(request)
        snapshot = await input_call(
            lambda: input_store.load(
                session_id,
                seat_id,
                capability=capability,
            )
        )
        try:
            return ParticipationSnapshotResponse.from_input_snapshot(snapshot)
        except (TypeError, ValueError) as exc:
            raise HumanInputError("input_invalid") from exc

    @app.post(
        "/api/v1/participation/{session_id}/{seat_id}/requests/{request_id}/submissions",
        response_model=ParticipationSubmissionResponse,
        responses=_PARTICIPATION_ERROR_RESPONSES,
        status_code=202,
    )
    async def participation_submit(
        request: Request,
        session_id: str,
        seat_id: str,
        request_id: str,
    ) -> Response:
        host = request.headers.get("host")
        origin = request.headers.get("origin")
        fetch_site = request.headers.get("sec-fetch-site")
        if not _same_loopback_origin(origin, host, request.url.scheme) or (
            fetch_site is not None and fetch_site.casefold() != "same-origin"
        ):
            return _error("cross_origin_forbidden", 403)
        if any(
            _SAFE_PATH_ID_RE.fullmatch(value) is None
            for value in (session_id, seat_id, request_id)
        ):
            raise HumanInputError("invalid_request")
        capability = _bearer_capability(request)
        raw_payload = await _bounded_json_body(request)
        try:
            submission = ParticipationSubmissionRequest.model_validate(raw_payload)
        except (TypeError, ValueError) as exc:
            raise HumanInputError("invalid_request") from exc
        result = await input_call(
            lambda: input_store.submit(
                session_id,
                seat_id,
                request_id,
                capability=capability,
                submission_id=submission.submission_id,
                move=submission.move,
            )
        )
        public_status = "duplicate" if result.status == "idempotent" else "submitted"
        payload = ParticipationSubmissionResponse(
            request_id=result.request_id,
            status=public_status,
        )
        return JSONResponse(
            status_code=200 if public_status == "duplicate" else 202,
            content=payload.model_dump(mode="json"),
            headers=_security_headers(),
        )

    @app.get(
        "/api/v1/control/catalog",
        response_model=ControlCatalogResponse,
        responses=_CONTROL_ERROR_RESPONSES,
    )
    async def control_catalog_endpoint(request: Request) -> ControlCatalogResponse:
        require_control(request)
        credential_ready = (
            None
            if control_manager is None
            else control_manager.profile_credential_ready
        )
        return await control_call(
            lambda: control_catalog(credential_ready=credential_ready)
        )

    @app.put(
        "/api/v1/control/profiles/{profile_id}/credential",
        status_code=204,
        response_class=Response,
        responses=_CONTROL_ERROR_RESPONSES,
    )
    async def control_set_profile_credential(
        request: Request,
        profile_id: str,
    ) -> Response:
        manager = require_control_manager(request)
        _require_control_write_context(request)
        raw_payload = await _bounded_control_body(request)
        try:
            credential = ControlProfileCredentialRequest.model_validate(raw_payload)
        except (TypeError, ValueError) as exc:
            raise ControlError("invalid_request") from exc
        await manager.set_profile_credential(
            profile_id,
            credential.api_key.get_secret_value(),
        )
        return Response(status_code=204)

    @app.delete(
        "/api/v1/control/profiles/{profile_id}/credential",
        status_code=204,
        response_class=Response,
        responses=_CONTROL_ERROR_RESPONSES,
    )
    async def control_clear_profile_credential(
        request: Request,
        profile_id: str,
    ) -> Response:
        manager = require_control_manager(request)
        _require_control_write_context(request)
        await manager.clear_profile_credential(profile_id)
        return Response(status_code=204)

    @app.get(
        "/api/v1/control/jobs",
        response_model=ControlJobListResponse,
        responses=_CONTROL_ERROR_RESPONSES,
    )
    async def control_jobs(
        request: Request,
        limit: int = Query(default=20, ge=1, le=100),
    ) -> ControlJobListResponse:
        store = require_control(request)
        jobs = await control_call(lambda: store.list(limit=limit))
        return ControlJobListResponse(jobs=tuple(public_job(job) for job in jobs))

    @app.post(
        "/api/v1/control/jobs",
        response_model=ControlJobResponse,
        responses=_CONTROL_ERROR_RESPONSES,
        status_code=201,
    )
    async def control_prepare_job(request: Request) -> ControlJobResponse:
        store = require_control(request)
        _require_control_write_context(request)
        idempotency_key = _control_idempotency_key(request)
        raw_payload = await _bounded_control_body(request)
        try:
            spec = ControlJobSpec.model_validate(raw_payload)
        except (TypeError, ValueError) as exc:
            raise ControlError("invalid_request") from exc
        preview = await control_call(
            lambda: validate_job_spec(
                spec,
                archive_database=store.archive_database,
                credential_ready=(
                    None
                    if control_manager is None
                    else control_manager.profile_credential_ready
                ),
                require_current_pricing=True,
            )
        )
        job = await control_call(
            lambda: store.prepare(
                spec,
                preview,
                idempotency_key=idempotency_key,
            )
        )
        return ControlJobResponse(job=public_job(job))

    @app.get(
        "/api/v1/control/jobs/{job_id}",
        response_model=ControlJobResponse,
        responses=_CONTROL_ERROR_RESPONSES,
    )
    async def control_job(request: Request, job_id: str) -> ControlJobResponse:
        store = require_control(request)
        job = await control_call(lambda: store.get(job_id))
        return ControlJobResponse(job=public_job(job))

    @app.post(
        "/api/v1/control/jobs/{job_id}/start",
        response_model=ControlJobResponse,
        responses=_CONTROL_ERROR_RESPONSES,
    )
    async def control_start_job(request: Request, job_id: str) -> ControlJobResponse:
        require_control(request)
        _require_control_write_context(request)
        idempotency_key = _control_idempotency_key(request)
        raw_payload = await _bounded_control_body(request)
        if raw_payload != {}:
            raise ControlError("invalid_request")
        if control_manager is None:
            raise ControlError("control_unavailable")
        job = await control_manager.start(
            job_id,
            idempotency_key=idempotency_key,
            web_base_url=str(request.base_url).rstrip("/"),
        )
        return ControlJobResponse(job=public_job(job))

    @app.post(
        "/api/v1/control/jobs/{job_id}/cancel",
        response_model=ControlJobResponse,
        responses=_CONTROL_ERROR_RESPONSES,
    )
    async def control_cancel_job(request: Request, job_id: str) -> ControlJobResponse:
        require_control(request)
        _require_control_write_context(request)
        idempotency_key = _control_idempotency_key(request)
        raw_payload = await _bounded_control_body(request)
        if raw_payload != {}:
            raise ControlError("invalid_request")
        if control_manager is None:
            raise ControlError("control_unavailable")
        job = await control_manager.cancel(job_id, idempotency_key=idempotency_key)
        return ControlJobResponse(job=public_job(job))

    @app.get("/api/v1/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        try:
            info = await read(reader.health)
        except WebReadError:
            return HealthResponse(
                status="degraded",
                service_version=__version__,
                database_available=False,
            )
        return HealthResponse(
            status="ok",
            service_version=__version__,
            database_available=True,
            database_schema_version=info["schema_version"],
        )

    @app.get("/api/v1/games", response_model=GameListResponse)
    async def games() -> GameListResponse:
        return GameListResponse(
            games=tuple(
                GameInfo.from_game(name, game_class)
                for name, game_class in sorted(GAME_REGISTRY.items())
            )
        )

    @app.get(
        "/api/v1/matches",
        response_model=MatchListResponse,
        responses=_READ_ERROR_RESPONSES,
    )
    async def matches(
        game: str | None = Query(default=None, min_length=1, max_length=64),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> MatchListResponse:
        if game is not None and _SAFE_GAME_RE.fullmatch(game) is None:
            return _error("invalid_request", 400)
        rows = await read(lambda: reader.list_matches(game=game, limit=limit))
        try:
            return MatchListResponse.from_storage(rows)
        except (TypeError, ValueError) as exc:
            raise WebReadError("match_index_invalid") from exc

    @app.get(
        "/api/v1/live",
        response_model=LiveMatchListResponse,
        responses=_LIVE_ERROR_RESPONSES,
    )
    async def live_matches(
        game: str | None = Query(default=None, min_length=1, max_length=64),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> LiveMatchListResponse:
        if game is not None and _SAFE_GAME_RE.fullmatch(game) is None:
            return _error("invalid_request", 400)
        rows = await read(lambda: live_reader.list_live(game=game, limit=limit))
        return LiveMatchListResponse(matches=tuple(rows))

    @app.get(
        "/api/v1/live/{live_id}",
        response_model=LiveMatchDetail,
        responses=_LIVE_ERROR_RESPONSES,
    )
    async def live_match_detail(
        live_id: str,
        from_seq: int = Query(default=0, ge=0, le=MAX_ARCHIVE_EVENTS),
        limit: int = Query(default=LIVE_PAGE_LIMIT, ge=1, le=LIVE_PAGE_LIMIT),
    ) -> LiveMatchDetail:
        if _SAFE_PATH_ID_RE.fullmatch(live_id) is None:
            return _error("invalid_request", 400)
        return await read(
            lambda: live_reader.load_live(live_id, from_seq=from_seq, limit=limit)
        )

    @app.get(
        "/api/v1/leaderboard",
        response_model=LeaderboardResponse,
        responses=_READ_ERROR_RESPONSES,
    )
    async def leaderboard(
        game: str | None = Query(default=None, min_length=1, max_length=64),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> LeaderboardResponse:
        if game is not None and _SAFE_GAME_RE.fullmatch(game) is None:
            return _error("invalid_request", 400)
        rows = await read(lambda: reader.leaderboard(game=game, limit=limit))
        try:
            return LeaderboardResponse.from_storage(rows, game=game)
        except (TypeError, ValueError) as exc:
            raise WebReadError("leaderboard_invalid") from exc

    @app.get(
        "/api/v1/matches/{match_id}",
        response_model=MatchDetail,
        responses=_DETAIL_ERROR_RESPONSES,
    )
    async def match_detail(match_id: str) -> MatchDetail:
        if _SAFE_PATH_ID_RE.fullmatch(match_id) is None:
            return _error("invalid_request", 400)
        loaded = await read(lambda: reader.load_match(match_id))
        try:
            return MatchDetail.from_archive(loaded.archive, summary=loaded.summary)
        except (TypeError, ValueError) as exc:
            raise WebReadError("archive_invalid") from exc

    @app.websocket("/ws/v1/matches/{match_id}")
    async def replay_match(
        websocket: WebSocket,
        match_id: str,
    ) -> None:
        if not _loopback_host_header(websocket.headers.get("host")):
            await websocket.close(code=4403, reason="invalid_host")
            return
        if not _same_loopback_origin(
            websocket.headers.get("origin"),
            websocket.headers.get("host"),
            websocket.url.scheme,
        ):
            await websocket.close(code=4403, reason="cross_origin_forbidden")
            return
        if _SAFE_PATH_ID_RE.fullmatch(match_id) is None:
            await websocket.close(code=4400, reason="invalid_request")
            return
        raw_from_seq = websocket.query_params.get("from_seq", "0")
        if not raw_from_seq.isascii() or not raw_from_seq.isdigit() or len(raw_from_seq) > 6:
            await websocket.close(code=4400, reason="invalid_request")
            return
        from_seq = int(raw_from_seq)
        if from_seq > MAX_ARCHIVE_EVENTS:
            await websocket.close(code=4400, reason="invalid_request")
            return

        # Host, Origin, path, and query validation remain pre-accept so an
        # untrusted handshake is never upgraded.  Same-origin business errors
        # are sent after accept; browsers can then observe the stable public
        # close code/reason instead of an opaque HTTP 403 / close code 1006.
        await websocket.accept()
        if replay_slots.locked():
            await websocket.close(code=4429, reason="overloaded")
            return

        async with replay_slots:
            try:
                loaded = await read(lambda: reader.load_match(match_id))
            except WebReadError as exc:
                code, _status = _public_read_error(exc)
                close_code = 4404 if code == "match_not_found" else 4403
                await websocket.close(code=close_code, reason=code)
                return
            if from_seq > len(loaded.archive.events):
                await websocket.close(code=4400, reason="invalid_request")
                return

            try:
                archive_envelope = WSArchiveEnvelope.from_archive(
                    loaded.archive,
                    summary=loaded.summary,
                )
                await _send_json(websocket, archive_envelope.model_dump(mode="json"))
                for event in loaded.archive.events[from_seq:]:
                    envelope = WSEventEnvelope.from_event(loaded.archive.match_id, event)
                    await _send_json(websocket, envelope.model_dump(mode="json"))
                complete = WSCompleteEnvelope(
                    match_id=loaded.archive.match_id,
                    event_count=len(loaded.archive.events),
                )
                await _send_json(websocket, complete.model_dump(mode="json"))
                await websocket.close(code=1000)
            except (TimeoutError, TypeError, ValueError, WebSocketDisconnect):
                return

    @app.websocket("/ws/v1/live/{live_id}")
    async def stream_live_match(websocket: WebSocket, live_id: str) -> None:
        if not _loopback_host_header(websocket.headers.get("host")):
            await websocket.close(code=4403, reason="invalid_host")
            return
        if not _same_loopback_origin(
            websocket.headers.get("origin"),
            websocket.headers.get("host"),
            websocket.url.scheme,
        ):
            await websocket.close(code=4403, reason="cross_origin_forbidden")
            return
        if _SAFE_PATH_ID_RE.fullmatch(live_id) is None:
            await websocket.close(code=4400, reason="invalid_request")
            return
        raw_from_seq = websocket.query_params.get("from_seq", "0")
        if not raw_from_seq.isascii() or not raw_from_seq.isdigit() or len(raw_from_seq) > 6:
            await websocket.close(code=4400, reason="invalid_request")
            return
        from_seq = int(raw_from_seq)
        if from_seq > MAX_ARCHIVE_EVENTS:
            await websocket.close(code=4400, reason="invalid_request")
            return

        await websocket.accept()
        if live_slots.locked():
            await websocket.close(code=4429, reason="overloaded")
            return

        async with live_slots:
            try:
                first = await read(
                    lambda: live_reader.load_live(
                        live_id,
                        from_seq=from_seq,
                        limit=LIVE_PAGE_LIMIT,
                    )
                )
            except LiveReadError as exc:
                code, _status = _public_live_error(exc)
                close_code = (
                    4404
                    if code == "live_not_found"
                    else 4400
                    if code == "invalid_request"
                    else 4403
                )
                await websocket.close(code=close_code, reason=code)
                return

            next_seq = from_seq
            try:
                snapshot = WSLiveSnapshotEnvelope(match=first.match, next_seq=from_seq)
                await _send_json(websocket, snapshot.model_dump(mode="json"))
                detail = first
                while True:
                    for item in detail.events:
                        if item.seq != next_seq:
                            raise ValueError("non-contiguous live event page")
                        envelope = WSLiveEventEnvelope(live_id=live_id, item=item)
                        await _send_json(websocket, envelope.model_dump(mode="json"))
                        next_seq += 1

                    summary = detail.match
                    if next_seq == summary.event_count and summary.status == "completed":
                        complete = WSLiveCompleteEnvelope(
                            live_id=live_id,
                            event_count=summary.event_count,
                            final_kind=summary.final_kind,
                            final_id=summary.final_id,
                            final_match_ids=summary.final_match_ids,
                        )
                        await _send_json(websocket, complete.model_dump(mode="json"))
                        await websocket.close(code=1000)
                        return
                    if next_seq == summary.event_count and summary.status == "interrupted":
                        interrupted = WSLiveInterruptedEnvelope(
                            live_id=live_id,
                            event_count=summary.event_count,
                        )
                        await _send_json(websocket, interrupted.model_dump(mode="json"))
                        await websocket.close(code=1000)
                        return
                    if not detail.has_more:
                        await asyncio.sleep(LIVE_POLL_SECONDS)
                    detail = await read(
                        lambda _from_seq=next_seq: live_reader.load_live(
                            live_id,
                            from_seq=_from_seq,
                            limit=LIVE_PAGE_LIMIT,
                        )
                    )
            except LiveReadError as exc:
                code, _status = _public_live_error(exc)
                await websocket.close(
                    code=4400 if code == "invalid_request" else 4403,
                    reason=code,
                )
            except (TimeoutError, TypeError, ValueError, WebSocketDisconnect):
                return

    return app


__all__ = ["create_app"]
