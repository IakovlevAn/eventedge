from __future__ import annotations

import asyncio
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
from starlette.middleware.gzip import GZipMiddleware

from eventedge import __version__
from eventedge.analysis import DEFAULT_MOEX_ALIASES
from eventedge.collectors import (
    collect_cbr_press,
    collect_discovery_news,
    collect_fast_news,
    collect_market_news,
    collect_slow_news,
    is_moex_equity_title,
)
from eventedge.evals import (
    build_assessment,
    demo_account,
    eval_summary,
    evaluate_signal,
)
from eventedge.llm import analyzer_from_environment
from eventedge.market import (
    InstrumentNotFoundError,
    MarketDataUnavailableError,
    MoexMarketDataClient,
    scenario_range,
)
from eventedge.storage import (
    IdempotencyConflictError,
    MemoryNewsRepository,
    NewsDocument,
    NewsRecord,
    NewsRepository,
    SignalRecord,
    YdbNewsRepository,
    canonical_payload_hash,
    deduplicate_signals,
)

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
SIGNAL_ID_PATTERN = re.compile(r"^sig_[0-9A-HJKMNP-TV-Z]{26}$")
JOB_ID_PATTERN = re.compile(r"^job_[0-9A-HJKMNP-TV-Z]{26}$")
PUBLIC_HIDDEN_SOURCE_IDS = frozenset({"eventedge_smoke"})
NEWS_COLLECTION_INTERVAL_SECONDS = 60
NEWS_CLIENT_REFRESH_INTERVAL_SECONDS = 30
NEWS_DELIVERY_TARGET_SECONDS = 120
NEWS_COLLECTION_LANES = (
    {
        "id": "fast",
        "interval_seconds": 60,
        "source_ids": ["interfax", "tass", "rbc", "moex_news"],
    },
    {
        "id": "discovery",
        "interval_seconds": 300,
        "source_ids": ["google_news", "market_background"],
    },
    {
        "id": "slow",
        "interval_seconds": 900,
        "source_ids": ["cbr_press"],
    },
)


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
app.state.market_data_client = MoexMarketDataClient()
app.state.collectors = {
    "cbr_press": collect_cbr_press,
    "fast_news": collect_fast_news,
    "discovery_news": collect_discovery_news,
    "slow_news": collect_slow_news,
    # Keep the current trigger payload backward compatible while widening the
    # collector from exchange notices to the complete market-news surface.
    "moex_news": collect_market_news,
    "market_news": collect_market_news,
}
app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=5)


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def public_news(items: list[NewsRecord]) -> list[NewsRecord]:
    return [
        item
        for item in items
        if item.source_id not in PUBLIC_HIDDEN_SOURCE_IDS
        and (item.source_id != "moex_news" or is_moex_equity_title(item.title))
    ]


def hidden_news_ids(items: list[NewsRecord]) -> set[str]:
    return {
        item.id
        for item in items
        if item.source_id in PUBLIC_HIDDEN_SOURCE_IDS
        or (item.source_id == "moex_news" and not is_moex_equity_title(item.title))
    }


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
    if request.url.path.startswith("/assets/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif request.url.path.startswith(("/brands/", "/favicon", "/apple-touch-icon")):
        response.headers["Cache-Control"] = "public, max-age=86400"
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
    stored_news = await repository.list_news(source_id=source_id, limit=1000)
    visible_news = public_news(stored_news)
    news = visible_news[:limit]
    signals = await repository.list_signals(
        ticker=None,
        directions=None,
        status=None,
        min_confidence=None,
        limit=1000,
    )
    signals_by_news: dict[str, list[dict[str, object]]] = {}
    for signal in deduplicate_signals(signals):
        signals_by_news.setdefault(signal.news_id, []).append(
            {
                "id": signal.id,
                "ticker": signal.ticker,
                "direction": signal.direction,
                "action": signal.action,
                "score": signal.score,
                "confidence": signal.confidence,
                "status": signal.status,
            }
        )
    source_stats: dict[str, dict[str, object]] = {}
    for item in visible_news:
        stat = source_stats.setdefault(
            item.source_id,
            {
                "source_id": item.source_id,
                "count": 0,
                "signal_count": 0,
                "last_published_at": item.as_api_dict()["published_at"],
            },
        )
        stat["count"] = int(stat["count"]) + 1
        stat["signal_count"] = int(stat["signal_count"]) + len(
            signals_by_news.get(item.id, [])
        )
    data = []
    for item in news:
        record = item.as_api_dict()
        record["related_signals"] = signals_by_news.get(item.id, [])
        data.append(record)
    return JSONResponse(
        content={
            "data": data,
            "meta": {
                "limit": limit,
                "total": len(visible_news),
                "has_more": len(visible_news) > limit,
                "next_cursor": None,
                "last_ingested_at": (
                    max(item.created_at for item in visible_news)
                    .astimezone(UTC)
                    .isoformat()
                    .replace("+00:00", "Z")
                    if visible_news
                    else None
                ),
                "poll_interval_seconds": NEWS_COLLECTION_INTERVAL_SECONDS,
                "client_refresh_interval_seconds": NEWS_CLIENT_REFRESH_INTERVAL_SECONDS,
                "delivery_target_seconds": NEWS_DELIVERY_TARGET_SECONDS,
                "collection_lanes": NEWS_COLLECTION_LANES,
                "sources": sorted(
                    source_stats.values(),
                    key=lambda item: (-int(item["count"]), str(item["source_id"])),
                ),
            },
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
        limit=1000,
    )
    news = await repository.list_news(source_id=None, limit=1000)
    hidden_ids = hidden_news_ids(news)
    signals = deduplicate_signals(
        signal for signal in signals if signal.news_id not in hidden_ids
    )[:limit]
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


def instrument_data(
    ticker: str,
    market_snapshot: dict[str, object],
    active_signal: SignalRecord | None,
) -> dict[str, object]:
    scenario = None
    if active_signal is not None:
        scenario = scenario_range(
            market_snapshot,
            direction=active_signal.direction,
            score=active_signal.score,
            confidence=active_signal.confidence,
            horizon_value=active_signal.horizon_value,
            horizon_unit=active_signal.horizon_unit,
        )

    market = {
        key: value
        for key, value in market_snapshot.items()
        if key not in {"ticker", "name"}
    }
    return {
        "ticker": ticker,
        "name": market_snapshot["name"],
        "as_of": market_snapshot["observed_at"],
        "market": market,
        "scenario": scenario,
        "active_signal": active_signal.as_api_dict() if active_signal else None,
        "recent_events": [],
    }


async def active_signals_by_ticker(repository: NewsRepository) -> dict[str, SignalRecord]:
    signals, news = await asyncio.gather(
        repository.list_signals(
            ticker=None,
            directions=None,
            status="active",
            min_confidence=None,
            limit=1000,
        ),
        repository.list_news(source_id=None, limit=1000),
    )
    hidden_ids = hidden_news_ids(news)
    result: dict[str, SignalRecord] = {}
    for signal in deduplicate_signals(signals):
        if signal.news_id not in hidden_ids and signal.ticker not in result:
            result[signal.ticker] = signal
    return result


def _requested_assessment_tickers(value: str | None) -> list[str] | None:
    normalized = (
        list(DEFAULT_MOEX_ALIASES)
        if value is None
        else list(
            dict.fromkeys(
                part.strip().upper() for part in value.split(",") if part.strip()
            )
        )
    )
    if not normalized or len(normalized) > 20 or any(
        not re.fullmatch(r"[A-Z0-9]{1,12}", ticker) for ticker in normalized
    ):
        return None
    return normalized


@app.get("/v1/assessments", tags=["Signals"])
async def list_assessments(
    request: Request,
    tickers: Annotated[str | None, Query(max_length=260)] = None,
) -> Response:
    normalized = _requested_assessment_tickers(tickers)
    if normalized is None:
        return problem_response(
            request,
            status=400,
            code="INVALID_PARAMETER",
            title="Invalid assessment tickers",
            detail="tickers must contain 1 to 20 comma-separated MOEX tickers.",
        )

    repository: NewsRepository = request.app.state.news_repository
    market_data_client: MoexMarketDataClient = request.app.state.market_data_client
    stored_signals, stored_news, market_results = await asyncio.gather(
        repository.list_signals(
            ticker=None,
            directions=None,
            status="active",
            min_confidence=None,
            limit=1000,
        ),
        repository.list_news(source_id=None, limit=1000),
        asyncio.gather(
            *(market_data_client.snapshot(ticker) for ticker in normalized),
            return_exceptions=True,
        ),
    )
    hidden_ids = hidden_news_ids(stored_news)
    active_by_ticker: dict[str, SignalRecord] = {}
    for signal in deduplicate_signals(stored_signals):
        if signal.news_id not in hidden_ids and signal.ticker not in active_by_ticker:
            active_by_ticker[signal.ticker] = signal
    visible_news = public_news(stored_news)
    data = []
    errors = []
    for ticker, result in zip(normalized, market_results, strict=True):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, InstrumentNotFoundError):
            errors.append({"ticker": ticker, "code": "INSTRUMENT_NOT_FOUND"})
            continue
        if isinstance(result, BaseException):
            errors.append({"ticker": ticker, "code": "MARKET_DATA_UNAVAILABLE"})
            continue
        assessment = build_assessment(
            ticker,
            result,
            visible_news,
            active_by_ticker.get(ticker),
        )
        assessment["scenario"] = scenario_range(
            result,
            direction=str(assessment["direction"]),
            score=float(assessment["score"]),
            confidence=float(assessment["confidence"]),
            horizon_value=3,
            horizon_unit="trading_days",
        )
        data.append(assessment)

    return JSONResponse(
        content={
            "data": data,
            "errors": errors,
            "meta": {
                "requested": len(normalized),
                "returned": len(data),
                "directed": sum(item["direction"] != "neutral" for item in data),
                "market_biases": sum(item["bias_direction"] != "neutral" for item in data),
                "news_backed": sum(item["assessment_type"] == "hybrid" for item in data),
                "refresh_after_seconds": 30,
                "score_scale": {"min": -100, "neutral_low": -18, "neutral_high": 18, "max": 100},
            },
        }
    )


@app.get("/v1/evals", tags=["Evals"])
async def list_evals(request: Request) -> JSONResponse:
    repository: NewsRepository = request.app.state.news_repository
    market_data_client: MoexMarketDataClient = request.app.state.market_data_client
    stored_signals, stored_news = await asyncio.gather(
        repository.list_signals(
            ticker=None,
            directions=None,
            status=None,
            min_confidence=None,
            limit=1000,
        ),
        repository.list_news(source_id=None, limit=1000),
    )
    news_by_id = {item.id: item for item in public_news(stored_news)}
    signals = [
        signal
        for signal in deduplicate_signals(stored_signals)
        if signal.news_id in news_by_id
    ][:100]
    tickers = list(dict.fromkeys(signal.ticker for signal in signals))
    candle_results = await asyncio.gather(
        *(market_data_client.candles(ticker, interval=10, lookback_days=14) for ticker in tickers),
        return_exceptions=True,
    )
    candles_by_ticker = {
        ticker: result.get("candles", [])
        for ticker, result in zip(tickers, candle_results, strict=True)
        if isinstance(result, dict)
    }
    outcomes = [
        evaluate_signal(
            signal,
            candles_by_ticker.get(signal.ticker, []),
            news_by_id.get(signal.news_id),
        )
        for signal in signals
    ]
    summary = eval_summary(outcomes)
    account = demo_account(outcomes)
    return JSONResponse(
        content={
            "data": {
                "summary": summary,
                "demo_account": account,
                "outcomes": outcomes,
            },
            "meta": {
                "generated_at": utc_now(),
                "refresh_after_seconds": 60,
                "evaluation_window": "1h / 1d / 3d after the first tradable candle",
                "warning": (
                    "Prototype retrospective on the available MOEX window; "
                    "it is not a point-in-time calibrated backtest or proof of alpha."
                ),
            },
        }
    )


@app.get("/v1/instruments/snapshots", tags=["Instruments"])
async def list_instrument_snapshots(
    request: Request,
    tickers: Annotated[str, Query(min_length=1, max_length=260)],
) -> JSONResponse:
    normalized = list(
        dict.fromkeys(part.strip().upper() for part in tickers.split(",") if part.strip())
    )
    if not normalized or len(normalized) > 20 or any(
        not re.fullmatch(r"[A-Z0-9]{1,12}", ticker) for ticker in normalized
    ):
        return problem_response(
            request,
            status=400,
            code="INVALID_PARAMETER",
            title="Invalid instrument tickers",
            detail="tickers must contain 1 to 20 comma-separated MOEX tickers.",
        )

    repository: NewsRepository = request.app.state.news_repository
    market_data_client: MoexMarketDataClient = request.app.state.market_data_client
    active_by_ticker, market_results = await asyncio.gather(
        active_signals_by_ticker(repository),
        asyncio.gather(
            *(market_data_client.snapshot(ticker) for ticker in normalized),
            return_exceptions=True,
        ),
    )
    data = []
    errors = []
    for ticker, result in zip(normalized, market_results, strict=True):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, InstrumentNotFoundError):
            errors.append({"ticker": ticker, "code": "INSTRUMENT_NOT_FOUND"})
            continue
        if isinstance(result, BaseException):
            errors.append({"ticker": ticker, "code": "MARKET_DATA_UNAVAILABLE"})
            continue
        data.append(instrument_data(ticker, result, active_by_ticker.get(ticker)))

    return JSONResponse(
        content={
            "data": data,
            "errors": errors,
            "meta": {
                "requested": len(normalized),
                "returned": len(data),
                "refresh_after_seconds": 30,
            },
        }
    )


@app.get("/v1/instruments/{ticker}/candles", tags=["Instruments"])
async def get_instrument_candles(
    request: Request,
    ticker: str,
    interval: Annotated[int, Query(ge=10, le=10)] = 10,
    lookback_days: Annotated[int, Query(ge=1, le=14)] = 14,
) -> Response:
    normalized_ticker = ticker.upper()
    if not re.fullmatch(r"[A-Z0-9]{1,12}", normalized_ticker):
        return problem_response(
            request,
            status=400,
            code="INVALID_PARAMETER",
            title="Invalid instrument ticker",
            detail="ticker must contain only uppercase Latin letters and digits.",
        )

    market_data_client: MoexMarketDataClient = request.app.state.market_data_client
    try:
        chart = await market_data_client.candles(
            normalized_ticker,
            interval=interval,
            lookback_days=lookback_days,
        )
    except InstrumentNotFoundError:
        return problem_response(
            request,
            status=404,
            code="INSTRUMENT_NOT_FOUND",
            title="Instrument not found",
            detail="The instrument is not available on the MOEX TQBR board.",
        )
    except MarketDataUnavailableError:
        return problem_response(
            request,
            status=503,
            code="MARKET_DATA_UNAVAILABLE",
            title="Market data is unavailable",
            detail="The MOEX ISS source did not return usable intraday candles.",
        )

    data = {
        "ticker": chart["ticker"],
        "interval_minutes": chart["interval_minutes"],
        "candles": chart["candles"],
        "observed_at": chart["observed_at"],
        "source": chart["source"],
    }
    etag = f'"{canonical_payload_hash(data)[:24]}"'
    return JSONResponse(
        content={"data": data, "meta": {"refresh_after_seconds": 60}},
        headers={"ETag": etag},
    )


@app.get("/v1/instruments/{ticker}/snapshot", tags=["Instruments"])
async def get_instrument_snapshot(
    request: Request,
    ticker: str,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    normalized_ticker = ticker.upper()
    if not re.fullmatch(r"[A-Z0-9]{1,12}", normalized_ticker):
        return problem_response(
            request,
            status=400,
            code="INVALID_PARAMETER",
            title="Invalid instrument ticker",
            detail="ticker must contain only uppercase Latin letters and digits.",
        )

    market_data_client: MoexMarketDataClient = request.app.state.market_data_client
    try:
        market_snapshot = await market_data_client.snapshot(normalized_ticker)
    except InstrumentNotFoundError:
        return problem_response(
            request,
            status=404,
            code="INSTRUMENT_NOT_FOUND",
            title="Instrument not found",
            detail="The instrument is not available on the MOEX TQBR board.",
        )
    except MarketDataUnavailableError:
        return problem_response(
            request,
            status=503,
            code="MARKET_DATA_UNAVAILABLE",
            title="Market data is unavailable",
            detail="The MOEX ISS market-data source did not return a usable snapshot.",
        )

    repository: NewsRepository = request.app.state.news_repository
    active_signal = (await active_signals_by_ticker(repository)).get(normalized_ticker)
    data = instrument_data(normalized_ticker, market_snapshot, active_signal)
    etag = f'"{canonical_payload_hash(data)[:24]}"'
    if if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(content={"data": data}, headers={"ETag": etag})


STATIC_DIR = Path(__file__).with_name("static")
if STATIC_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="web-assets")
    if (STATIC_DIR / "brands").is_dir():
        app.mount("/brands", StaticFiles(directory=STATIC_DIR / "brands"), name="brand-assets")

    @app.get("/favicon.png", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(STATIC_DIR / "favicon.png")

    @app.get("/apple-touch-icon.png", include_in_schema=False)
    async def apple_touch_icon() -> FileResponse:
        return FileResponse(STATIC_DIR / "apple-touch-icon.png")

    @app.get("/", include_in_schema=False)
    async def web_app() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")
