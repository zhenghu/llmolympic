"""FastAPI application for local, read-only archived-match observation."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Query, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect

from llmolympic import __version__
from llmolympic.games import GAME_REGISTRY
from llmolympic.web.models import (
    ErrorResponse,
    GameInfo,
    GameListResponse,
    HealthResponse,
    LeaderboardResponse,
    MatchDetail,
    MatchListResponse,
    WSArchiveEnvelope,
    WSCompleteEnvelope,
    WSEventEnvelope,
)
from llmolympic.web.reader import MAX_ARCHIVE_EVENTS, WebReadError, WebSQLiteReader

MAX_CONCURRENT_READS = 16
MAX_CONCURRENT_REPLAYS = 8
WEBSOCKET_SEND_TIMEOUT_SECONDS = 5.0

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_SAFE_PATH_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SAFE_GAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}

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


def _error(code: str, status_code: int) -> JSONResponse:
    payload = ErrorResponse(error={"code": code})
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json"),
        headers=_SECURITY_HEADERS,
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


def create_app(database: str | Path) -> FastAPI:
    """Build the optional local observer app without opening the database."""

    reader = WebSQLiteReader(database)
    read_slots = asyncio.Semaphore(MAX_CONCURRENT_READS)
    replay_slots = asyncio.Semaphore(MAX_CONCURRENT_REPLAYS)
    app = FastAPI(
        title="LLM Olympics local observer API",
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
        response.headers.update(_SECURITY_HEADERS)
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

    async def read(callable_: Callable[[], object]) -> object:
        async with read_slots:
            return await run_in_threadpool(callable_)

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

            await websocket.accept()
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

    return app


__all__ = ["create_app"]
