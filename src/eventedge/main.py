from __future__ import annotations

import os
import re
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import AnyUrl, BaseModel, ConfigDict, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from eventedge import __version__
from eventedge.collectors import (
    collect_cbr_press,
    collect_moex_news,
    is_moex_equity_title,
)
from eventedge.llm import analyzer_from_environment
from eventedge.storage import (
    IdempotencyConflictError,
    MemoryNewsRepository,
    NewsDocument,
    NewsRepository,
    YdbNewsRepository,
    canonical_payload_hash,
)

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
SIGNAL_ID_PATTERN = re.compile(r"^sig_[0-9A-HJKMNP-TV-Z]{26}$")
JOB_ID_PATTERN = re.compile(r"^job_[0-9A-HJKMNP-TV-Z]{26}$")


class NewsIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")]
    external_id: Annotated[str, Field(min_length=1, max_length=500)]
    published_at: datetime
    received_at: datetime | None = None
    title: Annotated[str, Field(min_length=1, max_length=500)]
    url: AnyUrl
    content: Annotated[str, Field(min_length=1, max_length=200_000)]
    language: Literal["ru", "en"] = "ru"
    source_metadata: dict[str, object] = Field(default_factory=dict)

    @field_validator("published_at", "received_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        return value

    @field_validator("source_metadata")
    @classmethod
    def limit_source_metadata(cls, value: dict[str, object]) -> dict[str, object]:
        if len(value) > 50:
            raise ValueError("source_metadata must contain at most 50 properties")
        return value


class TimerEventMetadata(BaseModel):
    event_type: Literal["yandex.cloud.events.serverless.triggers.TimerMessage"]


class TimerDetails(BaseModel):
    payload: Annotated[str, Field(min_length=1, max_length=4096)]


class TimerMessage(BaseModel):
    event_metadata: TimerEventMetadata
    details: TimerDetails


class TimerEnvelope(BaseModel):
    messages: Annotated[list[TimerMessage], Field(min_length=1, max_length=100)]


def repository_from_environment(environment: Mapping[str, str]) -> NewsRepository:
    analyzer = analyzer_from_environment(environment)
    endpoint = environment.get("YDB_ENDPOINT")
    database = environment.get("YDB_DATABASE")
    if endpoint and database:
        return YdbNewsRepository(
            endpoint=endpoint,
            database=database,
            analyzer=analyzer,
        )
    if endpoint or database:
        raise RuntimeError("YDB_ENDPOINT and YDB_DATABASE must be configured together")
    return MemoryNewsRepository(analyzer=analyzer)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    repository: NewsRepository = application.state.news_repository
    await repository.start()
    try:
        yield
    finally:
        await repository.stop()

app = FastAPI(
    title="EventEdge API",
    version=__version__,
    description="API-first stock signal platform for the Russian market.",
    docs_url="/docs",
    redoc_url=None,
    lifespan=lifespan,
)
app.state.news_repository = repository_from_environment(os.environ)
app.state.collectors = {
    "cbr_press": collect_cbr_press,
    "moex_news": collect_moex_news,
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def health_payload() -> dict[str, str]:
    payload = {"status": "ok", "checked_at": utc_now()}
    if revision := os.environ.get("APP_REVISION"):
        payload["revision"] = revision
    return payload


def request_id_from(request: Request) -> str:
    return getattr(request.state, "request_id", f"req_{uuid.uuid4().hex}")


def problem_response(
    request: Request,
    *,
    status: int,
    code: str,
    title: str,
    detail: str | None = None,
    errors: list[dict[str, object]] | None = None,
) -> JSONResponse:
    request_id = request_id_from(request)
    body: dict[str, object] = {
        "type": f"https://eventedge.ru/problems/{code.lower().replace('_', '-')}",
        "title": title,
        "status": status,
        "code": code,
        "request_id": request_id,
    }
    if detail:
        body["detail"] = detail
    if errors:
        body["errors"] = errors
    return JSONResponse(
        status_code=status,
        content=body,
        media_type="application/problem+json",
        headers={"X-Request-Id": request_id},
    )


@app.middleware("http")
async def attach_request_id(request: Request, call_next):
    supplied = request.headers.get("X-Request-Id", "")
    request.state.request_id = (
        supplied if REQUEST_ID_PATTERN.fullmatch(supplied) else f"req_{uuid.uuid4().hex}"
    )
    response = await call_next(request)
    response.headers["X-Request-Id"] = request.state.request_id
    return response


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [
        {
            "location": ".".join(str(part) for part in error["loc"]),
            "message": error["msg"],
        }
        for error in exc.errors()
    ]
    return problem_response(
        request,
        status=400,
        code="INVALID_PARAMETER",
        title="Invalid request parameter",
        detail="One or more request parameters are invalid.",
        errors=errors,
    )


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code == 404:
        return problem_response(
            request,
            status=404,
            code="RESOURCE_NOT_FOUND",
            title="Resource not found",
            detail="The requested resource does not exist.",
        )
    return problem_response(
        request,
        status=exc.status_code,
        code="HTTP_ERROR",
        title="Request failed",
        detail=str(exc.detail),
    )


@app.get("/health/live", tags=["Health"])
async def liveness() -> dict[str, str]:
    return health_payload()


@app.get("/health/ready", tags=["Health"])
async def readiness(request: Request) -> Response:
    repository: NewsRepository = request.app.state.news_repository
    try:
        ready = await repository.ready()
    except Exception:
        ready = False
    if not ready:
        return problem_response(
            request,
            status=503,
            code="DEPENDENCY_UNAVAILABLE",
            title="Required dependency is unavailable",
            detail="The structured data store is not ready.",
        )
    return JSONResponse(health_payload())


@app.post("/", include_in_schema=False)
async def handle_timer(request: Request, envelope: TimerEnvelope) -> JSONResponse:
    repository: NewsRepository = request.app.state.news_repository
    collectors = request.app.state.collectors
    results: dict[str, dict[str, int]] = {}
    for collector_name in dict.fromkeys(
        message.details.payload for message in envelope.messages
    ):
        collector = collectors.get(collector_name)
        if collector is None:
            return problem_response(
                request,
                status=400,
                code="UNKNOWN_COLLECTOR",
                title="Unknown collector",
                detail=f"Collector {collector_name!r} is not configured.",
            )
        results[collector_name] = await collector(repository)
    return JSONResponse({"status": "ok", "collectors": results})


@app.post("/v1/internal/news", tags=["Internal ingestion"], status_code=202)
async def ingest_news(
    request: Request,
    payload: NewsIngestRequest,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=8,
            max_length=128,
            pattern=r"^[A-Za-z0-9._:-]+$",
        ),
    ],
) -> JSONResponse:
    hash_payload = payload.model_dump(mode="json")
    received_at = payload.received_at or datetime.now(UTC)
    document = NewsDocument(
        source_id=payload.source_id,
        external_id=payload.external_id,
        published_at=payload.published_at,
        received_at=received_at,
        title=payload.title,
        url=str(payload.url),
        content=payload.content,
        language=payload.language,
        source_metadata=payload.source_metadata,
        payload_hash=canonical_payload_hash(hash_payload),
    )
    repository: NewsRepository = request.app.state.news_repository
    try:
        result = await repository.ingest(idempotency_key, document)
    except IdempotencyConflictError:
        return problem_response(
            request,
            status=409,
            code="IDEMPOTENCY_CONFLICT",
            title="Idempotency key conflict",
            detail="The idempotency key was already used with a different request payload.",
        )

    headers = {"Location": f"/v1/jobs/{result.job.id}"}
    if result.replayed:
        headers["Idempotency-Replayed"] = "true"
    return JSONResponse(
        status_code=202,
        content={"data": result.job.as_api_dict()},
        headers=headers,
    )


@app.get("/v1/jobs/{job_id}", tags=["Jobs"])
async def get_job(request: Request, job_id: str) -> JSONResponse:
    if not JOB_ID_PATTERN.fullmatch(job_id):
        return problem_response(
            request,
            status=400,
            code="INVALID_PARAMETER",
            title="Invalid job identifier",
            detail="job_id does not match the EventEdge identifier format.",
        )
    repository: NewsRepository = request.app.state.news_repository
    job = await repository.get_job(job_id)
    if not job:
        return problem_response(
            request,
            status=404,
            code="JOB_NOT_FOUND",
            title="Job not found",
            detail="The asynchronous job does not exist.",
        )
    return JSONResponse(content={"data": job.as_api_dict()})


@app.get("/v1/news", tags=["News"])
async def list_news(
    request: Request,
    source_id: Annotated[
        str | None,
        Query(pattern=r"^[a-z][a-z0-9_-]{2,63}$"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> JSONResponse:
    repository: NewsRepository = request.app.state.news_repository
    news = await repository.list_news(source_id=source_id, limit=1000)
    news = [
        item
        for item in news
        if item.source_id != "moex_news" or is_moex_equity_title(item.title)
    ][:limit]
    signals = await repository.list_signals(
        ticker=None,
        directions=None,
        status="active",
        min_confidence=None,
        limit=1000,
    )
    signals_by_news: dict[str, list[dict[str, object]]] = {}
    for signal in signals:
        signals_by_news.setdefault(signal.news_id, []).append(
            {
                "id": signal.id,
                "ticker": signal.ticker,
                "direction": signal.direction,
                "action": signal.action,
                "score": signal.score,
                "confidence": signal.confidence,
            }
        )
    data = []
    for item in news:
        record = item.as_api_dict()
        record["related_signals"] = signals_by_news.get(item.id, [])
        data.append(record)
    return JSONResponse(
        content={
            "data": data,
            "meta": {"limit": limit, "has_more": False, "next_cursor": None},
        }
    )


@app.get("/v1/signals", tags=["Signals"])
async def list_signals(
    request: Request,
    ticker: Annotated[str | None, Query(pattern=r"^[A-Z0-9]{1,12}$")] = None,
    direction: Annotated[str | None, Query()] = None,
    status: Annotated[
        Literal["active", "expired", "superseded", "invalidated"] | None, Query()
    ] = None,
    min_confidence: Annotated[float | None, Query(ge=0, le=1)] = None,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    requested_directions = (
        frozenset(part.strip() for part in direction.split(",") if part.strip())
        if direction
        else None
    )
    allowed_directions = {"up", "neutral", "down"}
    if requested_directions and not requested_directions <= allowed_directions:
        return problem_response(
            request,
            status=400,
            code="INVALID_PARAMETER",
            title="Invalid signal direction",
            detail="direction must contain only up, neutral or down.",
        )

    repository: NewsRepository = request.app.state.news_repository
    signals = await repository.list_signals(
        ticker=ticker,
        directions=requested_directions,
        status=status or "active",
        min_confidence=min_confidence,
        limit=limit,
    )
    data = [signal.as_api_dict() for signal in signals]
    cache_payload = {
        "ticker": ticker,
        "directions": sorted(requested_directions or ()),
        "status": status,
        "min_confidence": min_confidence,
        "cursor": cursor,
        "limit": limit,
        "signals": data,
    }
    etag = f'"{canonical_payload_hash(cache_payload)[:24]}"'
    if if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})

    return JSONResponse(
        content={
            "data": data,
            "meta": {"limit": limit, "has_more": False, "next_cursor": None},
        },
        headers={"ETag": etag},
    )


@app.get("/v1/signals/{signal_id}", tags=["Signals"])
async def get_signal(
    request: Request,
    signal_id: str,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    if not SIGNAL_ID_PATTERN.fullmatch(signal_id):
        return problem_response(
            request,
            status=400,
            code="INVALID_PARAMETER",
            title="Invalid signal identifier",
            detail="signal_id does not match the EventEdge identifier format.",
        )
    repository: NewsRepository = request.app.state.news_repository
    signal = await repository.get_signal(signal_id)
    if not signal:
        return problem_response(
            request,
            status=404,
            code="SIGNAL_NOT_FOUND",
            title="Signal not found",
            detail="The signal does not exist or is not visible.",
        )
    data = signal.as_api_dict()
    etag = f'"{canonical_payload_hash(data)[:24]}"'
    if if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(content={"data": data}, headers={"ETag": etag})


STATIC_DIR = Path(__file__).with_name("static")
if STATIC_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="web-assets")

    @app.get("/", include_in_schema=False)
    async def web_app() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")
