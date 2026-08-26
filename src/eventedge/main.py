from __future__ import annotations

import asyncio
import csv
import hmac
import io
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, Header, Query, Request
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import AnyUrl, BaseModel, ConfigDict, Field, field_validator
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from eventedge import __version__
from eventedge.analysis import (
    CURRENT_NEWS_MODEL_VERSION,
    DEFAULT_MOEX_ALIASES,
    NewsAnalysisInput,
    RuleBasedNewsExtractor,
)
from eventedge.collectors import (
    RssItem,
    collect_cbr_press,
    collect_discovery_news,
    collect_fast_news,
    collect_market_news,
    collect_slow_news,
    is_market_event_candidate,
    is_market_signal_candidate,
    is_moex_equity_title,
    is_signal_analysis_candidate,
    signal_analysis_priority,
)
from eventedge.configs.collection import load_collection_config
from eventedge.evals import (
    ASSESSMENT_MODEL_VERSION,
    EVALUATION_METHODOLOGY_VERSION,
    MAX_HORIZON_OBSERVATION_LAG,
    MAX_LIVE_PROCESSING_LAG,
    build_assessment,
    deduplicate_eval_events,
    deduplicate_eval_signals,
    eval_breakdowns,
    eval_quality_series,
    eval_relationships,
    eval_summary,
    evaluate_signal,
    evaluation_eligibility,
    event_time_export_rows,
    outcome_export_rows,
)
from eventedge.events import classify_news_event, cluster_market_events, signal_target
from eventedge.llm import analyzer_from_environment
from eventedge.market import (
    EVALUATION_INDEX_BENCHMARKS,
    InstrumentNotFoundError,
    MarketDataUnavailableError,
    MoexMarketDataClient,
    scenario_range,
)
from eventedge.ml_router import (
    MlRouterDecision,
    MlRouterMode,
    MlRouterRuntime,
    runtime_from_environment,
)
from eventedge.source_registry import configured_sources
from eventedge.storage import (
    SIGNAL_REJECTION_REASONS,
    EvaluationEpochRecord,
    IdempotencyConflictError,
    MemoryNewsRepository,
    NewsDocument,
    NewsRecord,
    NewsRepository,
    SignalRecord,
    TelegramSourceRecord,
    YdbNewsRepository,
    canonical_payload_hash,
    deduplicate_signals,
    filter_signals,
    normalize_signal_freshness,
    stable_id,
    to_rfc3339,
)

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
SIGNAL_ID_PATTERN = re.compile(r"^sig_[0-9A-HJKMNP-TV-Z]{26}$")
JOB_ID_PATTERN = re.compile(r"^job_[0-9A-HJKMNP-TV-Z]{26}$")
PUBLIC_HIDDEN_SOURCE_IDS = frozenset({"eventedge_smoke"})
COLLECTION_CONFIG = load_collection_config()
NEWS_COLLECTION_LANES = tuple(lane.as_api_dict() for lane in COLLECTION_CONFIG.lanes)
NEWS_COLLECTION_INTERVAL_SECONDS = next(
    lane.interval_seconds for lane in COLLECTION_CONFIG.lanes if lane.id == "fast"
)
NEWS_CLIENT_REFRESH_INTERVAL_SECONDS = 30
NEWS_DELIVERY_TARGET_SECONDS = 120
EVALUATION_CACHE_TTL_SECONDS = 60
BACKFILL_BATCH_LIMIT = min(40, max(1, int(os.environ.get("BACKFILL_BATCH_LIMIT", "4"))))
BACKFILL_CONCURRENCY = min(4, max(1, int(os.environ.get("BACKFILL_CONCURRENCY", "1"))))
MAINTENANCE_DEADLINE_SECONDS = 20.0
EVALUATION_REFRESH_DEADLINE_SECONDS = 60.0
SIGNAL_READ_TIMEOUT_SECONDS = 12.0
SIGNAL_FEED_TTL_SECONDS = 30 if os.environ.get("APP_ENV") == "prod" else 0
SIGNAL_FEED_FAILURE_RETRY_SECONDS = 5.0
CONTENT_SNAPSHOT_TTL_SECONDS = 60 if os.environ.get("APP_ENV") == "prod" else 0
CANONICAL_NEWS_RESPONSE_LIMIT = 500
RECENT_REPOSITORY_SUCCESS_TTL_SECONDS = 120 if os.environ.get("APP_ENV") == "prod" else 0
SOURCE_REGISTRY_TTL_SECONDS = 60.0
SOURCE_REGISTRY_TIMEOUT_SECONDS = 2.0
MAX_TELEGRAM_CHANNELS = 18
DEFAULT_ASSESSMENT_TICKERS = (
    "SBER",
    "LKOH",
    "YDEX",
    "NVTK",
    "TATN",
    "ROSN",
    "GMKN",
    "MGNT",
    "SIBN",
    "GAZP",
    "VTBR",
    "PLZL",
    "CHMF",
    "ALRS",
    "MOEX",
    "AFLT",
    "NLMK",
    "PHOR",
    "OZON",
    "X5",
)
logger = logging.getLogger(__name__)


def latest_model_signal_per_news(signals: list[SignalRecord]) -> list[SignalRecord]:
    """Keep one visible signal revision for each news/instrument pair."""
    selected: dict[tuple[str, str], SignalRecord] = {}
    for signal in signals:
        key = (signal.news_id, signal.ticker)
        current = selected.get(key)
        if current is None or (
            signal.model_version,
            signal.created_at,
            signal.id,
        ) > (
            current.model_version,
            current.created_at,
            current.id,
        ):
            selected[key] = signal
    return sorted(selected.values(), key=lambda signal: (signal.as_of, signal.id), reverse=True)


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


class TelegramSourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: Annotated[str, Field(pattern=r"^@?[A-Za-z0-9_]{3,48}$")]
    display_name: Annotated[str | None, Field(min_length=2, max_length=80)] = None
    description: Annotated[str | None, Field(min_length=8, max_length=280)] = None

    @field_validator("channel")
    @classmethod
    def normalize_channel(cls, value: str) -> str:
        return value.removeprefix("@")


class SignalReprocessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: Annotated[int, Field(ge=1, le=20)] = 12
    news_ids: list[str] = Field(default_factory=list, max_length=20)


def repository_from_environment(environment: Mapping[str, str]) -> NewsRepository:
    analyzer = analyzer_from_environment(environment)
    endpoint = environment.get("YDB_ENDPOINT")
    database = environment.get("YDB_DATABASE")
    if endpoint and database:
        component = environment.get("EVENTEDGE_COMPONENT", "api")
        default_pool_size = 4 if component == "worker" else 8
        try:
            pool_size = int(environment.get("YDB_POOL_SIZE", str(default_pool_size)))
        except ValueError as exc:
            raise RuntimeError("YDB_POOL_SIZE must be an integer") from exc
        return YdbNewsRepository(
            endpoint=endpoint,
            database=database,
            analyzer=analyzer,
            pool_size=pool_size,
        )
    if endpoint or database:
        raise RuntimeError("YDB_ENDPOINT and YDB_DATABASE must be configured together")
    return MemoryNewsRepository(analyzer=analyzer)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    repository: NewsRepository = application.state.news_repository
    await repository.start()
    application.state.repository_last_success_at = time.monotonic()
    try:
        yield
    finally:
        for task_name in ("content_snapshot_inflight", "signal_feed_inflight"):
            inflight = getattr(application.state, task_name, None)
            if inflight is not None and not inflight.done():
                inflight.cancel()
                try:
                    await inflight
                except asyncio.CancelledError:
                    pass
        await repository.stop()


app = FastAPI(
    title="EventEdge API",
    version=__version__,
    description="API-first stock signal platform for the Russian market.",
    docs_url="/docs",
    redoc_url=None,
    lifespan=lifespan,
)


class RequestIdMiddleware:
    """Attach trace and cache headers without BaseHTTPMiddleware disconnect races."""

    def __init__(self, application: ASGIApp) -> None:
        self.application = application

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.application(scope, receive, send)
            return

        supplied = Headers(scope=scope).get("X-Request-Id", "")
        request_id = (
            supplied if REQUEST_ID_PATTERN.fullmatch(supplied) else f"req_{uuid.uuid4().hex}"
        )
        scope.setdefault("state", {})["request_id"] = request_id
        path = str(scope.get("path", ""))

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Request-Id"] = request_id
                if path.startswith("/assets/"):
                    headers["Cache-Control"] = "public, max-age=31536000, immutable"
                elif path.startswith(("/brands/", "/favicon", "/apple-touch-icon")):
                    headers["Cache-Control"] = "public, max-age=86400"
            await send(message)

        await self.application(scope, receive, send_with_headers)


app.state.news_repository = repository_from_environment(os.environ)
app.state.ml_router_runtime = runtime_from_environment(os.environ)
app.state.market_data_client = MoexMarketDataClient()
app.state.evaluation_material_cache = None
app.state.evaluation_material_inflight = None
app.state.evaluation_material_lock = asyncio.Lock()
app.state.content_snapshot_cache = None
app.state.content_snapshot_lock = asyncio.Lock()
app.state.content_snapshot_inflight = None
app.state.signal_feed_cache = None
app.state.signal_feed_lock = asyncio.Lock()
app.state.signal_feed_inflight = None
app.state.news_response_cache = None
app.state.news_response_inflight = {}
app.state.event_response_cache = None
app.state.event_response_lock = asyncio.Lock()
app.state.repository_last_success_at = None
app.state.source_registry_cache = None
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
app.add_middleware(RequestIdMiddleware)


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


async def refresh_content_snapshot(
    application: FastAPI,
) -> tuple[list[NewsRecord], list[SignalRecord]]:
    """Refresh the shared market snapshot once for all public endpoints."""
    repository: NewsRepository = application.state.news_repository
    now = time.monotonic()
    cached = application.state.content_snapshot_cache
    if (
        CONTENT_SNAPSHOT_TTL_SECONDS > 0
        and cached is not None
        and cached["repository"] is repository
        and cached["expires_at"] > now
    ):
        return cached["news"], cached["signals"]

    async with application.state.content_snapshot_lock:
        now = time.monotonic()
        cached = application.state.content_snapshot_cache
        if (
            CONTENT_SNAPSHOT_TTL_SECONDS > 0
            and cached is not None
            and cached["repository"] is repository
            and cached["expires_at"] > now
        ):
            return cached["news"], cached["signals"]
        news, signals = await asyncio.gather(
            repository.list_news(source_id=None, limit=1000),
            repository.list_signals(
                ticker=None,
                directions=None,
                status=None,
                min_confidence=None,
                limit=1000,
            ),
        )
        completed_at = time.monotonic()
        application.state.repository_last_success_at = completed_at
        if CONTENT_SNAPSHOT_TTL_SECONDS > 0:
            application.state.content_snapshot_cache = {
                "repository": repository,
                "expires_at": completed_at + CONTENT_SNAPSHOT_TTL_SECONDS,
                "news": news,
                "signals": signals,
            }
        return news, signals


async def refresh_content_snapshot_in_background(application: FastAPI) -> None:
    current_task = asyncio.current_task()
    try:
        news, signals = await refresh_content_snapshot(application)
        payload = await asyncio.to_thread(
            build_news_response_payload,
            news,
            signals,
            source_id=None,
            scope=None,
            limit=CANONICAL_NEWS_RESPONSE_LIMIT,
        )
        store_news_response(
            application,
            news,
            signals,
            (None, None, CANONICAL_NEWS_RESPONSE_LIMIT),
            payload,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Background content snapshot refresh failed")
    finally:
        if application.state.content_snapshot_inflight is current_task:
            application.state.content_snapshot_inflight = None


async def load_content_snapshot(request: Request) -> tuple[list[NewsRecord], list[SignalRecord]]:
    """Serve a stale snapshot immediately while one background refresh runs."""
    repository: NewsRepository = request.app.state.news_repository
    now = time.monotonic()
    cached = request.app.state.content_snapshot_cache
    if cached is not None and cached["repository"] is repository:
        if cached["expires_at"] <= now:
            inflight = request.app.state.content_snapshot_inflight
            if inflight is None or inflight.done():
                request.app.state.content_snapshot_inflight = asyncio.create_task(
                    refresh_content_snapshot_in_background(request.app)
                )
        return cached["news"], cached["signals"]
    return await refresh_content_snapshot(request.app)


async def refresh_signal_feed(
    application: FastAPI,
) -> tuple[list[NewsRecord], list[SignalRecord]]:
    """Refresh the current active signal feed without the broad content snapshot."""
    repository: NewsRepository = application.state.news_repository
    async with application.state.signal_feed_lock:
        now = time.monotonic()
        cached = application.state.signal_feed_cache
        if (
            SIGNAL_FEED_TTL_SECONDS > 0
            and cached is not None
            and cached["repository"] is repository
            and cached["expires_at"] > now
        ):
            return cached["news"], cached["signals"]

        async with asyncio.timeout(SIGNAL_READ_TIMEOUT_SECONDS):
            signals = await repository.list_signals(
                ticker=None,
                directions=None,
                status="active",
                min_confidence=None,
                limit=1000,
                model_version=CURRENT_NEWS_MODEL_VERSION,
            )
            requested_news_ids = frozenset(signal.news_id for signal in signals)
            news = await repository.get_news_by_ids(requested_news_ids)
        if {item.id for item in news} != requested_news_ids:
            raise RuntimeError("signal feed contains unresolved news references")

        completed_at = time.monotonic()
        application.state.repository_last_success_at = completed_at
        if (
            not signals
            and cached is not None
            and cached["repository"] is repository
            and cached["signals"]
        ):
            logger.warning("Ignoring anomalous empty signal feed refresh")
            cached["expires_at"] = completed_at + SIGNAL_FEED_TTL_SECONDS
            return cached["news"], cached["signals"]
        if SIGNAL_FEED_TTL_SECONDS > 0:
            application.state.signal_feed_cache = {
                "repository": repository,
                "expires_at": completed_at + SIGNAL_FEED_TTL_SECONDS,
                "news": news,
                "signals": signals,
            }
        return news, signals


async def refresh_signal_feed_in_background(application: FastAPI) -> None:
    current_task = asyncio.current_task()
    try:
        await refresh_signal_feed(application)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Background signal feed refresh failed")
        cached = application.state.signal_feed_cache
        if cached is not None and cached["repository"] is application.state.news_repository:
            cached["expires_at"] = time.monotonic() + SIGNAL_FEED_FAILURE_RETRY_SECONDS
    finally:
        if application.state.signal_feed_inflight is current_task:
            application.state.signal_feed_inflight = None


async def load_signal_feed(request: Request) -> tuple[list[NewsRecord], list[SignalRecord]]:
    """Serve the last complete feed while a single background refresh runs."""
    repository: NewsRepository = request.app.state.news_repository
    now = time.monotonic()
    cached = request.app.state.signal_feed_cache
    if cached is not None and cached["repository"] is repository:
        if cached["expires_at"] <= now:
            inflight = request.app.state.signal_feed_inflight
            if inflight is None or inflight.done():
                request.app.state.signal_feed_inflight = asyncio.create_task(
                    refresh_signal_feed_in_background(request.app)
                )
        return cached["news"], cached["signals"]
    return await refresh_signal_feed(request.app)


def invalidate_content_snapshot(request: Request) -> None:
    inflight = request.app.state.content_snapshot_inflight
    if inflight is not None and not inflight.done():
        inflight.cancel()
    request.app.state.content_snapshot_inflight = None
    request.app.state.content_snapshot_cache = None
    signal_inflight = request.app.state.signal_feed_inflight
    if signal_inflight is not None and not signal_inflight.done():
        signal_inflight.cancel()
    request.app.state.signal_feed_inflight = None
    request.app.state.signal_feed_cache = None
    request.app.state.news_response_cache = None
    request.app.state.event_response_cache = None


def cached_news_response(
    application: FastAPI,
    stored_news: list[NewsRecord],
    stored_signals: list[SignalRecord],
    key: tuple[str | None, str | None, int],
) -> dict[str, object] | None:
    if CONTENT_SNAPSHOT_TTL_SECONDS <= 0:
        return None
    cached = application.state.news_response_cache
    if (
        cached is None
        or cached["news"] is not stored_news
        or cached["signals"] is not stored_signals
    ):
        inflight = application.state.content_snapshot_inflight
        if cached is not None and inflight is not None and not inflight.done():
            stale_response = cached_news_response_for_key(cached["responses"], key)
            if stale_response is not None:
                return stale_response
        application.state.news_response_cache = {
            "news": stored_news,
            "signals": stored_signals,
            "responses": {},
        }
        return None
    return cached_news_response_for_key(cached["responses"], key)


def cached_news_response_for_key(
    responses: dict[tuple[str | None, str | None, int], dict[str, object]],
    key: tuple[str | None, str | None, int],
) -> dict[str, object] | None:
    response = responses.get(key)
    if response is not None:
        return response
    source_id, scope, limit = key
    if source_id is not None or scope is not None:
        return None
    canonical = responses.get((None, None, CANONICAL_NEWS_RESPONSE_LIMIT))
    if canonical is None:
        return None
    meta = dict(canonical["meta"])
    total = int(meta["total"])
    meta.update({"limit": limit, "has_more": total > limit, "next_cursor": None})
    derived = {"data": list(canonical["data"][:limit]), "meta": meta}
    responses[key] = derived
    return derived


def store_news_response(
    application: FastAPI,
    stored_news: list[NewsRecord],
    stored_signals: list[SignalRecord],
    key: tuple[str | None, str | None, int],
    payload: dict[str, object],
) -> None:
    if CONTENT_SNAPSHOT_TTL_SECONDS <= 0:
        return
    cached = application.state.news_response_cache
    if (
        cached is None
        or cached["news"] is not stored_news
        or cached["signals"] is not stored_signals
    ):
        cached = {
            "news": stored_news,
            "signals": stored_signals,
            "responses": {},
        }
        application.state.news_response_cache = cached
    cached["responses"][key] = payload


def build_news_response_payload(
    stored_news: list[NewsRecord],
    stored_signals: list[SignalRecord],
    *,
    source_id: str | None,
    scope: Literal["market", "sector", "company"] | None,
    limit: int,
) -> dict[str, object]:
    """Render the expensive news projection independently of the request loop."""
    if source_id is not None:
        stored_news = [item for item in stored_news if item.source_id == source_id]
    visible_news = public_news(stored_news)
    news_by_id = {item.id: item for item in stored_news}
    signals = normalize_signal_freshness(stored_signals, news_by_id)
    signals_by_news: dict[str, list[dict[str, object]]] = {}
    for signal in latest_model_signal_per_news(deduplicate_signals(signals)):
        if signal.model_version != CURRENT_NEWS_MODEL_VERSION:
            continue
        signals_by_news.setdefault(signal.news_id, []).append(
            {
                "id": signal.id,
                "ticker": signal.ticker,
                "target": signal_target(signal.ticker),
                "direction": signal.direction,
                "action": signal.action,
                "score": signal.score,
                "confidence": signal.confidence,
                "status": signal.status,
            }
        )
    event_by_news = {
        item.id: classify_news_event(
            title=item.title,
            content=item.content,
            source_metadata=item.source_metadata,
            related_signals=signals_by_news.get(item.id, []),
        )
        for item in visible_news
    }
    scoped_news = [
        item for item in visible_news if scope is None or event_by_news[item.id]["scope"] == scope
    ]
    processing_rows = [item.as_api_dict()["processing"] for item in scoped_news]
    signaled_count = sum(bool(signals_by_news.get(item.id)) for item in scoped_news)
    relevant_count = sum(
        bool(row["event_candidate"] or row["analysis_candidate"] or signals_by_news.get(item.id))
        for item, row in zip(scoped_news, processing_rows, strict=True)
    )
    analysis_candidate_count = sum(
        bool(row["analysis_candidate"] or signals_by_news.get(item.id))
        for item, row in zip(scoped_news, processing_rows, strict=True)
    )
    rejection_reasons: dict[str, int] = {}
    for item in scoped_news:
        outcome = item.source_metadata.get("signal_outcome")
        reason = outcome.get("reason") if isinstance(outcome, Mapping) else None
        if isinstance(reason, str) and reason in SIGNAL_REJECTION_REASONS:
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
    processing_coverage = {
        "stored": len(scoped_news),
        "relevant": relevant_count,
        "analysis_candidates": analysis_candidate_count,
        "signaled": signaled_count,
        "candidate_coverage_pct": (
            round(analysis_candidate_count / relevant_count * 100, 1) if relevant_count else 0.0
        ),
        "signal_yield_pct": (
            round(signaled_count / analysis_candidate_count * 100, 1)
            if analysis_candidate_count
            else 0.0
        ),
        "rejection_reasons": dict(sorted(rejection_reasons.items())),
        "signal_model_version": CURRENT_NEWS_MODEL_VERSION,
    }
    news = scoped_news[:limit]
    source_stats: dict[str, dict[str, object]] = {}
    for item in scoped_news:
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
        stat["signal_count"] = int(stat["signal_count"]) + len(signals_by_news.get(item.id, []))
    data = []
    for item in news:
        record = item.as_api_dict()
        record["related_signals"] = signals_by_news.get(item.id, [])
        record["event"] = event_by_news[item.id]
        data.append(record)
    scope_counts = {"market": 0, "sector": 0, "company": 0}
    for event in event_by_news.values():
        scope_counts[str(event["scope"])] += 1
    return {
        "data": data,
        "meta": {
            "limit": limit,
            "total": len(scoped_news),
            "has_more": len(scoped_news) > limit,
            "next_cursor": None,
            "scope": scope,
            "scope_counts": scope_counts,
            "processing_coverage": processing_coverage,
            "last_ingested_at": (
                max(item.received_at for item in visible_news)
                .astimezone(UTC)
                .isoformat()
                .replace("+00:00", "Z")
                if visible_news
                else None
            ),
            "poll_interval_seconds": NEWS_COLLECTION_INTERVAL_SECONDS,
            "managed_telegram_poll_interval_seconds": 300,
            "client_refresh_interval_seconds": NEWS_CLIENT_REFRESH_INTERVAL_SECONDS,
            "delivery_target_seconds": NEWS_DELIVERY_TARGET_SECONDS,
            "collection_lanes": NEWS_COLLECTION_LANES,
            "sources": sorted(
                source_stats.values(),
                key=lambda item: (-int(item["count"]), str(item["source_id"])),
            ),
        },
    }


async def get_or_build_news_response(
    application: FastAPI,
    stored_news: list[NewsRecord],
    stored_signals: list[SignalRecord],
    *,
    source_id: str | None,
    scope: Literal["market", "sector", "company"] | None,
    limit: int,
) -> dict[str, object]:
    """Build one projection per snapshot/query while concurrent callers wait."""
    cache_key = (source_id, scope, limit)
    cached_response = cached_news_response(
        application,
        stored_news,
        stored_signals,
        cache_key,
    )
    if cached_response is not None:
        return cached_response

    task_key = (id(stored_news), id(stored_signals), *cache_key)
    task = application.state.news_response_inflight.get(task_key)
    if task is None:
        task = asyncio.create_task(
            asyncio.to_thread(
                build_news_response_payload,
                stored_news,
                stored_signals,
                source_id=source_id,
                scope=scope,
                limit=limit,
            )
        )
        application.state.news_response_inflight[task_key] = task

        def clear_inflight(done: asyncio.Task[dict[str, object]]) -> None:
            if application.state.news_response_inflight.get(task_key) is done:
                application.state.news_response_inflight.pop(task_key, None)

        task.add_done_callback(clear_inflight)

    response_content = await asyncio.shield(task)
    current_snapshot = application.state.content_snapshot_cache
    if current_snapshot is None or (
        current_snapshot["news"] is stored_news
        and current_snapshot["signals"] is stored_signals
    ):
        store_news_response(
            application,
            stored_news,
            stored_signals,
            cache_key,
            response_content,
        )
    return response_content


def build_market_event_records(
    stored_news: list[NewsRecord],
    stored_signals: list[SignalRecord],
) -> list[dict[str, object]]:
    """Build event groups from one immutable content snapshot."""
    news = public_news(stored_news)
    signals = latest_model_signal_per_news(
        deduplicate_signals(
            normalize_signal_freshness(stored_signals, {item.id: item for item in news})
        )
    )
    signals_by_news: dict[str, list[dict[str, object]]] = {}
    for signal in signals:
        signals_by_news.setdefault(signal.news_id, []).append(
            {
                "id": signal.id,
                "ticker": signal.ticker,
                "target": signal_target(signal.ticker),
                "direction": signal.direction,
                "score": signal.score,
                "confidence": signal.confidence,
                "status": signal.status,
            }
        )

    candidates: list[dict[str, object]] = []
    for item in news:
        related_signals = signals_by_news.get(item.id, [])
        projection = classify_news_event(
            title=item.title,
            content=item.content,
            source_metadata=item.source_metadata,
            related_signals=related_signals,
        )
        record = item.as_api_dict()
        candidates.append(
            {
                "id": f"evt_{item.id.removeprefix('news_')}",
                "title": item.title,
                "summary": item.content,
                "published_at": record["published_at"],
                "detected_at": record["created_at"],
                "source_id": item.source_id,
                "source_url": item.url,
                "news_id": item.id,
                "evidence": {
                    "news_id": item.id,
                    "source_id": item.source_id,
                    "published_at": record["published_at"],
                    "title": item.title,
                    "url": item.url,
                    "content_hash": canonical_payload_hash(
                        {
                            "title": item.title,
                            "content": item.content,
                            "language": item.language,
                        }
                    ),
                },
                "related_signals": related_signals,
                **projection,
            }
        )
    return cluster_market_events(candidates)


async def get_or_build_market_event_records(
    application: FastAPI,
    stored_news: list[NewsRecord],
    stored_signals: list[SignalRecord],
) -> list[dict[str, object]]:
    """Reuse one event projection per snapshot and serialize first-build work."""
    cached = application.state.event_response_cache
    if (
        CONTENT_SNAPSHOT_TTL_SECONDS > 0
        and cached is not None
        and cached["news"] is stored_news
        and cached["signals"] is stored_signals
    ):
        return cached["events"]

    async with application.state.event_response_lock:
        cached = application.state.event_response_cache
        if (
            CONTENT_SNAPSHOT_TTL_SECONDS > 0
            and cached is not None
            and cached["news"] is stored_news
            and cached["signals"] is stored_signals
        ):
            return cached["events"]
        events = await asyncio.to_thread(
            build_market_event_records,
            stored_news,
            stored_signals,
        )
        if CONTENT_SNAPSHOT_TTL_SECONDS > 0:
            application.state.event_response_cache = {
                "news": stored_news,
                "signals": stored_signals,
                "events": events,
            }
        return events


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
    last_success_at = request.app.state.repository_last_success_at
    recently_succeeded = (
        RECENT_REPOSITORY_SUCCESS_TTL_SECONDS > 0
        and last_success_at is not None
        and time.monotonic() - last_success_at <= RECENT_REPOSITORY_SUCCESS_TTL_SECONDS
    )
    if recently_succeeded:
        ready = True
    else:
        try:
            ready = await asyncio.wait_for(repository.ready(), timeout=2.5)
        except Exception:
            ready = False
        if ready:
            request.app.state.repository_last_success_at = time.monotonic()
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
    results: dict[str, dict[str, object]] = {}
    for collector_name in dict.fromkeys(message.details.payload for message in envelope.messages):
        if collector_name == "maintenance":
            async def bounded_reprocess() -> dict[str, object] | None:
                try:
                    async with asyncio.timeout(MAINTENANCE_DEADLINE_SECONDS):
                        return await reprocess_signal_candidates_batch(
                            repository,
                            limit=BACKFILL_BATCH_LIMIT,
                            concurrency=BACKFILL_CONCURRENCY,
                            ml_router=request.app.state.ml_router_runtime,
                        )
                except TimeoutError:
                    logger.warning(
                        "Maintenance signal reprocessing deadline exceeded; "
                        "unfinished idempotent work was deferred"
                    )
                    return None

            async def bounded_eval_refresh() -> tuple[object, ...] | None:
                try:
                    async with asyncio.timeout(EVALUATION_REFRESH_DEADLINE_SECONDS):
                        return await _load_evaluation_material(
                            repository,
                            request.app.state.market_data_client,
                        )
                except TimeoutError:
                    logger.warning(
                        "Maintenance eval refresh deadline exceeded; "
                        "unfinished idempotent work was deferred"
                    )
                    return None

            reprocess, evaluation = await asyncio.gather(
                bounded_reprocess(),
                bounded_eval_refresh(),
            )
            maintenance_result: dict[str, object] = {}
            deferred: list[str] = []
            if reprocess is None:
                deferred.append("signal_reprocessing")
            else:
                reprocess_meta = reprocess["meta"]
                assert isinstance(reprocess_meta, dict)
                maintenance_result.update(
                    {
                        "reprocessed": int(reprocess_meta["completed"]),
                        "failed": int(reprocess_meta.get("failed", 0)),
                        "failure_types": list(reprocess_meta.get("failure_types", [])),
                        "remaining": int(reprocess_meta["remaining_candidates"]),
                        "rejection_reasons": dict(
                            reprocess_meta.get("rejection_reasons", {})
                        ),
                    }
                )
            if evaluation is None:
                deferred.append("eval_refresh")
            else:
                outcomes, _, _, _, epochs = evaluation
                maintenance_result.update(
                    {
                        "outcomes": len(outcomes),
                        "epochs": len(epochs),
                    }
                )
            if deferred:
                maintenance_result.update(
                    {
                        "status": "deferred" if len(deferred) == 2 else "partial",
                        "deferred": deferred,
                        "deadline_seconds": max(
                            MAINTENANCE_DEADLINE_SECONDS,
                            EVALUATION_REFRESH_DEADLINE_SECONDS,
                        ),
                    }
                )
            results[collector_name] = maintenance_result
            continue
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
    invalidate_content_snapshot(request)
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
    invalidate_content_snapshot(request)
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
    scope: Annotated[Literal["market", "sector", "company"] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 20,
) -> JSONResponse:
    stored_news, stored_signals = await load_content_snapshot(request)
    response_content = await get_or_build_news_response(
        request.app,
        stored_news,
        stored_signals,
        source_id=source_id,
        scope=scope,
        limit=limit,
    )
    return JSONResponse(content=response_content)


@app.get("/v1/events", tags=["Events"])
async def list_market_events(
    request: Request,
    scope: Annotated[Literal["market", "sector", "company"] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> JSONResponse:
    stored_news, stored_signals = await load_content_snapshot(request)
    all_events = await get_or_build_market_event_records(
        request.app,
        stored_news,
        stored_signals,
    )
    events = [event for event in all_events if scope is None or event["scope"] == scope]
    return JSONResponse(
        content={
            "data": events[:limit],
            "meta": {
                "limit": limit,
                "scope": scope,
                "total": len(events),
                "has_more": len(events) > limit,
            },
        }
    )


@app.get("/v1/events/{event_id}", tags=["Events"])
async def get_market_event(
    request: Request,
    event_id: Annotated[
        str,
        PathParam(pattern=r"^evt_[0-9A-HJKMNP-TV-Z]{26}$"),
    ],
) -> JSONResponse:
    stored_news, stored_signals = await load_content_snapshot(request)
    events = await get_or_build_market_event_records(
        request.app,
        stored_news,
        stored_signals,
    )
    event = next((item for item in events if item["id"] == event_id), None)
    if event is None:
        raise StarletteHTTPException(status_code=404)
    return JSONResponse(content={"data": event})


@app.get("/v1/sources", tags=["Sources"])
async def list_sources(request: Request) -> JSONResponse:
    repository: NewsRepository = request.app.state.news_repository
    cached = request.app.state.content_snapshot_cache
    stored_news = (
        public_news(cached["news"])
        if cached is not None and cached["repository"] is repository
        else []
    )
    stats: dict[str, dict[str, object]] = {}
    for item in stored_news:
        stat = stats.setdefault(
            item.source_id,
            {"count": 0, "last_published_at": item.as_api_dict()["published_at"]},
        )
        stat["count"] = int(stat["count"]) + 1

    now = time.monotonic()
    registry_cache = request.app.state.source_registry_cache
    if (
        registry_cache is not None
        and registry_cache["repository"] is repository
        and registry_cache["expires_at"] > now
    ):
        dynamic_sources = registry_cache["sources"]
        registry_status = "cached"
    else:
        try:
            dynamic_sources = await asyncio.wait_for(
                repository.list_telegram_sources(),
                timeout=SOURCE_REGISTRY_TIMEOUT_SECONDS,
            )
        except Exception:
            if registry_cache is not None and registry_cache["repository"] is repository:
                dynamic_sources = registry_cache["sources"]
                registry_status = "stale"
            else:
                dynamic_sources = []
                registry_status = "unavailable"
        else:
            request.app.state.source_registry_cache = {
                "repository": repository,
                "expires_at": now + SOURCE_REGISTRY_TTL_SECONDS,
                "sources": dynamic_sources,
            }
            registry_status = "live"

    sources = configured_sources()
    configured_ids = {str(source["source_id"]) for source in sources}
    for source in dynamic_sources:
        if source.source_id not in configured_ids:
            sources.append(source.as_api_dict())
    for source in sources:
        source.update(stats.get(str(source["source_id"]), {"count": 0, "last_published_at": None}))
    return JSONResponse(
        content={
            "data": sources,
            "meta": {
                "telegram_limit": MAX_TELEGRAM_CHANNELS,
                "telegram_active": sum(
                    source.get("kind") == "Telegram" and source.get("enabled") for source in sources
                ),
                "poll_interval_seconds": NEWS_COLLECTION_INTERVAL_SECONDS,
                "registry_status": registry_status,
            },
        }
    )


@app.post("/v1/sources/telegram", tags=["Sources"], status_code=201)
async def create_telegram_source(
    payload: TelegramSourceCreate,
    request: Request,
    admin_key: Annotated[str | None, Header(alias="X-EventEdge-Admin-Key")] = None,
) -> JSONResponse:
    expected_key = os.environ.get("EVENTEDGE_ADMIN_KEY")
    if not expected_key:
        return problem_response(
            request,
            status=503,
            code="SOURCE_ADMIN_NOT_CONFIGURED",
            title="Source administration is not configured",
            detail="Set EVENTEDGE_ADMIN_KEY for the production runtime.",
        )
    if not admin_key or not hmac.compare_digest(admin_key, expected_key):
        return problem_response(
            request,
            status=401,
            code="INVALID_ADMIN_KEY",
            title="Invalid admin key",
            detail="A valid X-EventEdge-Admin-Key header is required.",
        )

    repository: NewsRepository = request.app.state.news_repository
    configured = configured_sources()
    dynamic = await repository.list_telegram_sources()
    static_telegram = [source for source in configured if source.get("kind") == "Telegram"]
    normalized_channel = payload.channel.casefold()
    existing_channels = {
        str(source.get("channel", "")).casefold() for source in static_telegram
    } | {source.channel.casefold() for source in dynamic}
    if normalized_channel in existing_channels:
        return problem_response(
            request,
            status=409,
            code="SOURCE_ALREADY_EXISTS",
            title="Telegram source already exists",
            detail=f"@{payload.channel} is already in the source registry.",
        )
    if (
        len(static_telegram) + len([source for source in dynamic if source.enabled])
        >= MAX_TELEGRAM_CHANNELS
    ):
        return problem_response(
            request,
            status=409,
            code="SOURCE_LIMIT_REACHED",
            title="Telegram source limit reached",
            detail=f"At most {MAX_TELEGRAM_CHANNELS} Telegram channels can be active.",
        )

    source = TelegramSourceRecord(
        source_id=f"telegram_{normalized_channel}",
        channel=payload.channel,
        display_name=payload.display_name or f"@{payload.channel}",
        description=payload.description or "Пользовательский Telegram-канал",
        enabled=True,
        created_at=datetime.now(UTC),
    )
    await repository.upsert_telegram_source(source)
    request.app.state.source_registry_cache = None
    return JSONResponse(status_code=201, content={"data": source.as_api_dict()})


async def reprocess_signal_candidates_batch(
    repository: NewsRepository,
    *,
    limit: int,
    news_ids: list[str] | None = None,
    concurrency: int = BACKFILL_CONCURRENCY,
    ml_router: MlRouterRuntime | None = None,
) -> dict[str, object]:
    """Re-run current candidate policy in a bounded, idempotent batch."""
    stored_news, stored_signals = await asyncio.gather(
        repository.list_news(source_id=None, limit=1000),
        repository.list_signals(
            ticker=None,
            directions=None,
            status=None,
            min_confidence=None,
            limit=1000,
        ),
    )
    current_news_ids = {
        signal.news_id
        for signal in stored_signals
        if signal.model_version == CURRENT_NEWS_MODEL_VERSION
    }
    requested_news_ids = set(news_ids or [])
    candidates: list[tuple[int, NewsRecord, RssItem, MlRouterDecision | None]] = []
    for item in public_news(stored_news):
        if requested_news_ids and item.id not in requested_news_ids:
            continue
        if item.id in current_news_ids or (
            item.source_metadata.get("reprocess_version") == CURRENT_NEWS_MODEL_VERSION
        ):
            continue
        categories = item.source_metadata.get("categories", [])
        candidate = RssItem(
            external_id=item.external_id,
            published_at=item.published_at,
            title=item.title,
            url=item.url,
            content=item.content,
            categories=tuple(str(value) for value in categories)
            if isinstance(categories, list | tuple)
            else (),
        )
        if is_signal_analysis_candidate(candidate):
            decision = (
                ml_router.predict(
                    title=candidate.title,
                    content=candidate.content,
                    categories=candidate.categories,
                )
                if ml_router is not None
                else None
            )
            candidates.append((signal_analysis_priority(candidate), item, candidate, decision))

    candidates.sort(key=lambda row: row[0], reverse=True)
    selected = candidates[:limit]
    semaphore = asyncio.Semaphore(min(max(concurrency, 1), 4))

    async def reprocess_one(
        priority: int,
        item: NewsRecord,
        candidate: RssItem,
        decision: MlRouterDecision | None,
    ) -> dict[str, object]:
        async with semaphore:
            for attempt in range(2):
                try:
                    return await ingest_reprocessed(priority, item, candidate, decision)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if attempt == 1:
                        raise
                    await asyncio.sleep(1.5)
            raise AssertionError("unreachable retry loop")

    async def ingest_reprocessed(
        priority: int,
        item: NewsRecord,
        candidate: RssItem,
        decision: MlRouterDecision | None,
    ) -> dict[str, object]:
        features = RuleBasedNewsExtractor().extract(
            NewsAnalysisInput(
                source_id=item.source_id,
                title=item.title,
                content=item.content,
                language=item.language,
            )
        )
        direct_signal_candidate = is_market_signal_candidate(candidate)
        event_candidate = is_market_event_candidate(candidate)
        ml_rejected = bool(
            ml_router is not None
            and ml_router.mode is MlRouterMode.ENFORCE
            and decision is not None
            and decision.action == "reject"
        )
        metadata = dict(item.source_metadata)
        metadata.update(
            {
                "processing_status": "processed",
                "signal_candidate": direct_signal_candidate and not ml_rejected,
                "analysis_candidate": not ml_rejected,
                "event_candidate": event_candidate,
                "classification_status": (
                    "ml_rejected"
                    if ml_rejected
                    else (
                        "signal_candidate"
                        if direct_signal_candidate
                        else "semantic_candidate"
                    )
                ),
                "classification_reason": (
                    "ml_router_high_confidence_irrelevant"
                    if ml_rejected
                    else (
                        "eligible_for_direct_signal_analysis"
                        if direct_signal_candidate
                        else "eligible_for_semantic_signal_analysis"
                    )
                ),
                "classification_version": "candidate-gate-0.6.0",
                "analysis_priority": priority,
                "reprocess_version": CURRENT_NEWS_MODEL_VERSION,
                "tickers": [
                    instrument.ticker
                    for instrument in features.instruments
                    if instrument.relevance >= 0.9 and instrument.ticker != "MOEX"
                ],
            }
        )
        if ml_router is not None and decision is not None:
            metadata["ml_router"] = decision.as_metadata(mode=ml_router.mode)
        hash_payload = {
            "source_id": item.source_id,
            "external_id": item.external_id,
            "published_at": item.published_at.isoformat(),
            "title": item.title,
            "url": item.url,
            "content": item.content,
            "language": item.language,
            "source_metadata": metadata,
            "reprocess_version": CURRENT_NEWS_MODEL_VERSION,
        }
        payload_hash = canonical_payload_hash(hash_payload)
        result = await repository.ingest(
            (
                f"reprocess:v5:{item.id}:{CURRENT_NEWS_MODEL_VERSION}:"
                f"{payload_hash[:16]}"
            ),
            NewsDocument(
                source_id=item.source_id,
                external_id=item.external_id,
                published_at=item.published_at,
                received_at=item.received_at,
                title=item.title,
                url=item.url,
                content=item.content,
                language=item.language,
                source_metadata=metadata,
                payload_hash=payload_hash,
            ),
            generate_signals=not ml_rejected,
        )
        return {
            "news_id": item.id,
            "job_id": result.job.id,
            "result_ref": result.job.result_ref,
            "replayed": result.replayed,
            **(
                {"signal_outcome": dict(result.signal_outcome)}
                if result.signal_outcome is not None
                else {}
            ),
            **({"ml_router_action": decision.action} if decision is not None else {}),
        }

    results = await asyncio.gather(
        *(
            reprocess_one(priority, item, candidate, decision)
            for priority, item, candidate, decision in selected
        ),
        return_exceptions=True,
    )
    completed = [result for result in results if isinstance(result, dict)]
    failed = [type(result).__name__ for result in results if isinstance(result, BaseException)]
    for (_, item, _, _), result in zip(selected, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning(
                "Signal reprocess failed news_id=%s error=%s",
                item.id,
                type(result).__name__,
            )
    rejection_reasons: dict[str, int] = {}
    for result in completed:
        outcome = result.get("signal_outcome")
        reason = outcome.get("reason") if isinstance(outcome, Mapping) else None
        if isinstance(reason, str) and reason in SIGNAL_REJECTION_REASONS:
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
    priority_counts = {
        str(priority): sum(1 for selected_priority, *_ in selected if selected_priority == priority)
        for priority in sorted({row[0] for row in selected}, reverse=True)
    }
    return {
        "data": completed,
        "meta": {
            "selected": len(selected),
            "completed": len(completed),
            "failed": len(failed),
            "failure_types": failed,
            "remaining_candidates": max(0, len(candidates) - len(completed)),
            "model_version": CURRENT_NEWS_MODEL_VERSION,
            "batch_limit": limit,
            "concurrency": min(max(concurrency, 1), 4),
            "candidate_policy": "material-event-priority-0.5.0",
            "selected_priority_counts": priority_counts,
            "rejection_reasons": dict(sorted(rejection_reasons.items())),
            **({"ml_router": ml_router.status()} if ml_router is not None else {}),
        },
    }


@app.post("/v1/admin/signals/reprocess", tags=["Signals"])
async def reprocess_signal_candidates(
    payload: SignalReprocessRequest,
    request: Request,
    admin_key: Annotated[str | None, Header(alias="X-EventEdge-Admin-Key")] = None,
) -> JSONResponse:
    """Re-run the current candidate policy on stored news in small budget-capped batches."""
    expected_key = os.environ.get("EVENTEDGE_ADMIN_KEY")
    if not expected_key:
        return problem_response(
            request,
            status=503,
            code="SOURCE_ADMIN_NOT_CONFIGURED",
            title="Signal administration is not configured",
            detail="Set EVENTEDGE_ADMIN_KEY for the production runtime.",
        )
    if not admin_key or not hmac.compare_digest(admin_key, expected_key):
        return problem_response(
            request,
            status=401,
            code="INVALID_ADMIN_KEY",
            title="Invalid admin key",
            detail="A valid X-EventEdge-Admin-Key header is required.",
        )

    repository: NewsRepository = request.app.state.news_repository
    result = await reprocess_signal_candidates_batch(
        repository,
        limit=payload.limit,
        news_ids=payload.news_ids,
        ml_router=request.app.state.ml_router_runtime,
    )
    request.app.state.evaluation_material_cache = None
    invalidate_content_snapshot(request)
    return JSONResponse(content=result)


@app.get("/v1/signals", tags=["Signals"])
async def list_signals(
    request: Request,
    ticker: Annotated[str | None, Query(pattern=r"^[A-Z0-9]{1,12}$")] = None,
    direction: Annotated[str | None, Query()] = None,
    status: Annotated[
        Literal["active", "expired", "superseded", "invalidated"] | None, Query()
    ] = None,
    min_confidence: Annotated[float | None, Query(ge=0, le=1)] = None,
    model_version: Annotated[str | None, Query(pattern=r"^[a-z0-9._-]{3,80}$")] = None,
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

    selected_model_version = model_version or (
        CURRENT_NEWS_MODEL_VERSION if (status or "active") == "active" else None
    )
    repository: NewsRepository = request.app.state.news_repository
    try:
        if (
            (status or "active") == "active"
            and selected_model_version == CURRENT_NEWS_MODEL_VERSION
        ):
            news, stored_signals = await load_signal_feed(request)
        else:
            async with asyncio.timeout(SIGNAL_READ_TIMEOUT_SECONDS):
                stored_signals = await repository.list_signals(
                    ticker=ticker,
                    directions=requested_directions,
                    status=status or "active",
                    min_confidence=min_confidence,
                    limit=1000,
                    model_version=selected_model_version,
                )
                requested_news_ids = frozenset(signal.news_id for signal in stored_signals)
                news = await repository.get_news_by_ids(requested_news_ids)
    except TimeoutError:
        logger.warning("Signal feed read exceeded %.1fs", SIGNAL_READ_TIMEOUT_SECONDS)
        return problem_response(
            request,
            status=503,
            code="DEPENDENCY_UNAVAILABLE",
            title="Signal feed is temporarily unavailable",
            detail="The structured data store did not complete the signal read in time.",
        )
    except Exception:
        logger.exception("Signal feed read failed")
        return problem_response(
            request,
            status=503,
            code="DEPENDENCY_UNAVAILABLE",
            title="Signal feed is temporarily unavailable",
            detail="The structured data store did not return a complete signal feed.",
        )

    requested_news_ids = frozenset(signal.news_id for signal in stored_signals)
    if {item.id for item in news} != requested_news_ids:
        logger.error("Signal feed read returned incomplete news references")
        return problem_response(
            request,
            status=503,
            code="DEPENDENCY_UNAVAILABLE",
            title="Signal feed is temporarily unavailable",
            detail="The structured data store did not return a complete signal feed.",
        )
    request.app.state.repository_last_success_at = time.monotonic()
    hidden_ids = hidden_news_ids(news)
    news_by_id = {item.id: item for item in news}
    signals = filter_signals(
        normalize_signal_freshness(stored_signals, news_by_id),
        ticker=ticker,
        directions=requested_directions,
        status=status or "active",
        min_confidence=min_confidence,
        limit=1000,
        model_version=selected_model_version,
    )
    signals = deduplicate_eval_events(
        deduplicate_signals(
            signal
            for signal in signals
            if signal.news_id not in hidden_ids
        ),
        news_by_id,
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
            "meta": {
                "limit": limit,
                "has_more": False,
                "next_cursor": None,
                "model_scope": "news_event",
                "final_assessment_endpoint": "/v1/assessments",
                "final_assessment_model_version": ASSESSMENT_MODEL_VERSION,
                "model_version": selected_model_version,
            },
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
    stored_news = await repository.list_news(source_id=None, limit=1000)
    signal = normalize_signal_freshness(
        [signal],
        {item.id: item for item in stored_news},
    )[0]
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

    market = {key: value for key, value in market_snapshot.items() if key not in {"ticker", "name"}}
    return {
        "ticker": ticker,
        "name": market_snapshot["name"],
        "as_of": market_snapshot["observed_at"],
        "market": market,
        "scenario": scenario,
        "active_signal": active_signal.as_api_dict() if active_signal else None,
        "recent_events": [],
    }


def active_signals_from_content(
    news: list[NewsRecord],
    signals: list[SignalRecord],
) -> dict[str, SignalRecord]:
    """Project current instrument signals from the shared content snapshot."""
    hidden_ids = hidden_news_ids(news)
    news_by_id = {item.id: item for item in news}
    active = filter_signals(
        normalize_signal_freshness(signals, news_by_id),
        ticker=None,
        directions=None,
        status="active",
        min_confidence=None,
        limit=1000,
    )
    result: dict[str, SignalRecord] = {}
    for signal in deduplicate_eval_events(deduplicate_signals(active), news_by_id):
        if (
            signal.model_version == CURRENT_NEWS_MODEL_VERSION
            and signal.news_id not in hidden_ids
        ):
            current = result.get(signal.ticker)
            if current is None or (
                current.direction == "neutral" and signal.direction in {"up", "down"}
            ):
                result[signal.ticker] = signal
    return result


async def active_signals_by_ticker(repository: NewsRepository) -> dict[str, SignalRecord]:
    signals, news = await asyncio.gather(
        repository.list_signals(
            ticker=None,
            directions=None,
            status=None,
            min_confidence=None,
            limit=1000,
        ),
        repository.list_news(source_id=None, limit=1000),
    )
    return active_signals_from_content(news, signals)


def _requested_assessment_tickers(value: str | None) -> list[str] | None:
    normalized = (
        list(DEFAULT_ASSESSMENT_TICKERS)
        if value is None
        else list(dict.fromkeys(part.strip().upper() for part in value.split(",") if part.strip()))
    )
    if (
        not normalized
        or len(normalized) > 20
        or any(not re.fullmatch(r"[A-Z0-9]{1,12}", ticker) for ticker in normalized)
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

    market_data_client: MoexMarketDataClient = request.app.state.market_data_client
    content, market_results = await asyncio.gather(
        load_content_snapshot(request),
        asyncio.gather(
            *(market_data_client.snapshot(ticker) for ticker in normalized),
            return_exceptions=True,
        ),
    )
    stored_news, stored_signals = content
    hidden_ids = hidden_news_ids(stored_news)
    news_by_id = {item.id: item for item in stored_news}
    active_signals = filter_signals(
        normalize_signal_freshness(stored_signals, news_by_id),
        ticker=None,
        directions=None,
        status="active",
        min_confidence=None,
        limit=1000,
    )
    active_by_ticker: dict[str, SignalRecord] = {}
    for signal in deduplicate_eval_events(deduplicate_signals(active_signals), news_by_id):
        if (
            signal.model_version == CURRENT_NEWS_MODEL_VERSION
            and signal.news_id not in hidden_ids
        ):
            current = active_by_ticker.get(signal.ticker)
            if current is None or (
                current.direction == "neutral" and signal.direction in {"up", "down"}
            ):
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


def evaluation_epoch_methodology(epoch: EvaluationEpochRecord) -> str:
    versions = {
        str(outcome.get("evaluation_methodology"))
        for outcome in epoch.outcomes
        if outcome.get("evaluation_methodology")
    }
    return versions.pop() if len(versions) == 1 else "legacy-or-mixed"


def latest_evaluation_epochs(
    epochs: list[EvaluationEpochRecord],
) -> list[EvaluationEpochRecord]:
    """Expose one epoch per model/config while retaining old snapshots in storage."""
    selected: dict[tuple[str, int], EvaluationEpochRecord] = {}
    for epoch in epochs:
        key = (epoch.model_version, epoch.config_version)
        previous = selected.get(key)
        if previous is None or (
            evaluation_epoch_methodology(epoch) == EVALUATION_METHODOLOGY_VERSION,
            epoch.evaluated_at,
            epoch.epoch_id,
        ) > (
            evaluation_epoch_methodology(previous) == EVALUATION_METHODOLOGY_VERSION,
            previous.evaluated_at,
            previous.epoch_id,
        ):
            selected[key] = epoch
    return sorted(
        selected.values(),
        key=lambda epoch: (epoch.evaluated_at, epoch.epoch_id),
        reverse=True,
    )


async def _load_evaluation_material(
    repository: NewsRepository,
    market_data_client: MoexMarketDataClient,
) -> tuple[
    list[dict[str, object]],
    list[SignalRecord],
    dict[str, list[dict[str, object]]],
    dict[str, NewsRecord],
    list[EvaluationEpochRecord],
]:
    stored_signals, stored_epochs = await asyncio.gather(
        repository.list_signals(
            ticker=None,
            directions=None,
            status=None,
            min_confidence=None,
            limit=1000,
        ),
        repository.list_evaluation_epochs(),
    )
    stored_news = await repository.get_news_by_ids(
        frozenset(signal.news_id for signal in stored_signals)
    )
    news_by_id = {item.id: item for item in public_news(stored_news)}
    signals = deduplicate_eval_events(
        (
            signal
            for signal in deduplicate_eval_signals(deduplicate_signals(stored_signals))
            if signal.news_id in news_by_id
            and (
                signal.ticker in DEFAULT_MOEX_ALIASES
                or signal.ticker in EVALUATION_INDEX_BENCHMARKS
            )
            and signal.direction in {"up", "down"}
        ),
        news_by_id,
        preserve_model_epochs=True,
    )[:200]
    current_method_epochs = [
        epoch
        for epoch in stored_epochs
        if evaluation_epoch_methodology(epoch) == EVALUATION_METHODOLOGY_VERSION
    ]
    epoch_by_model: dict[tuple[str, int], EvaluationEpochRecord] = {}
    for epoch in current_method_epochs:
        epoch_by_model.setdefault((epoch.model_version, epoch.config_version), epoch)
    complete_outcomes: dict[str, dict[str, object]] = {}
    for epoch in current_method_epochs:
        for outcome in epoch.outcomes:
            if _is_complete_eval_outcome(outcome):
                complete_outcomes.setdefault(str(outcome["signal_id"]), dict(outcome))
    refresh_signals = [signal for signal in signals if signal.id not in complete_outcomes]
    market_refresh_signals = [
        signal
        for signal in refresh_signals
        if evaluation_eligibility(signal, news_by_id.get(signal.news_id))["eligible"]
    ]
    tickers = list(dict.fromkeys(signal.ticker for signal in market_refresh_signals))
    candle_results = await asyncio.gather(
        *(market_data_client.candles(ticker, interval=10, lookback_days=14) for ticker in tickers),
        return_exceptions=True,
    )
    candles_by_ticker = {
        ticker: result.get("candles", [])
        for ticker, result in zip(tickers, candle_results, strict=True)
        if isinstance(result, dict)
    }
    outcomes = []
    for signal in signals:
        outcome = complete_outcomes.get(signal.id) or evaluate_signal(
            signal,
            candles_by_ticker.get(signal.ticker, []),
            news_by_id.get(signal.news_id),
        )
        benchmark_ticker = EVALUATION_INDEX_BENCHMARKS.get(signal.ticker)
        if benchmark_ticker:
            outcome = {
                **outcome,
                "evaluation_benchmark": {
                    "ticker": benchmark_ticker,
                    "source": "MOEX ISS",
                    "kind": "index",
                },
            }
        outcomes.append(outcome)
    generated_at = datetime.now(UTC)
    epoch_records = []
    for model_version, config_version in sorted(
        {(signal.model_version, signal.config_version) for signal in signals}
    ):
        epoch_signals = [
            signal
            for signal in signals
            if signal.model_version == model_version and signal.config_version == config_version
        ]
        signal_ids = {signal.id for signal in epoch_signals}
        epoch_outcomes = [outcome for outcome in outcomes if outcome["signal_id"] in signal_ids]
        previous_epoch = epoch_by_model.get((model_version, config_version))
        complete_signal_ids = {
            signal.id for signal in epoch_signals if signal.id in complete_outcomes
        }
        preserved_observations = [
            dict(row)
            for row in previous_epoch.observations
            if row.get("signal_id") in complete_signal_ids
        ] if previous_epoch else []
        refreshed_epoch_signals = [
            signal for signal in epoch_signals if signal.id not in complete_signal_ids
        ]
        fresh_observations, fresh_truncated = event_time_export_rows(
            refreshed_epoch_signals,
            candles_by_ticker,
            news_by_id,
        )
        fresh_observations = [
            {
                **row,
                "evaluation_benchmark": EVALUATION_INDEX_BENCHMARKS.get(
                    str(row.get("ticker", ""))
                ),
            }
            for row in fresh_observations
        ]
        observations, observations_truncated = _merge_eval_observations(
            preserved_observations,
            fresh_observations,
            already_truncated=bool(previous_epoch and previous_epoch.observations_truncated)
            or fresh_truncated,
        )
        epoch_records.append(
            EvaluationEpochRecord(
                epoch_id=stable_id(
                    "eval_",
                    (
                        f"{model_version}\x00{config_version}\x00"
                        f"{EVALUATION_METHODOLOGY_VERSION}"
                    ),
                ),
                model_version=model_version,
                config_version=config_version,
                evaluated_at=generated_at,
                outcomes=tuple(epoch_outcomes),
                observations=tuple(observations),
                observations_truncated=observations_truncated,
            )
        )
    if epoch_records:
        await asyncio.gather(
            *(repository.upsert_evaluation_epoch(epoch) for epoch in epoch_records)
        )
    stored_epochs = await repository.list_evaluation_epochs()
    return outcomes, signals, candles_by_ticker, news_by_id, stored_epochs


def _is_complete_eval_outcome(outcome: Mapping[str, object]) -> bool:
    returns = outcome.get("returns")
    eligibility = outcome.get("eligibility")
    if (
        outcome.get("evaluation_methodology") != EVALUATION_METHODOLOGY_VERSION
        or not isinstance(eligibility, dict)
    ):
        return False
    if outcome.get("status") == "excluded":
        return eligibility.get("eligible") is False and bool(eligibility.get("reason"))
    observations = outcome.get("horizon_observations")
    three_day = observations.get("3d") if isinstance(observations, dict) else None
    return (
        outcome.get("status") == "evaluated"
        and eligibility.get("eligible") is True
        and isinstance(returns, dict)
        and returns.get("3d") is not None
        and isinstance(three_day, dict)
        and three_day.get("timely") is True
    )


def _merge_eval_observations(
    preserved: list[dict[str, object]],
    fresh: list[dict[str, object]],
    *,
    already_truncated: bool,
    max_rows: int = 5000,
) -> tuple[list[dict[str, object]], bool]:
    unique = {
        (str(row.get("signal_id")), str(row.get("observation_at"))): row
        for row in [*preserved, *fresh]
    }
    ordered = sorted(
        unique.values(),
        key=lambda row: (
            str(row.get("signal_as_of", "")),
            str(row.get("signal_id", "")),
            str(row.get("observation_at", "")),
        ),
    )
    return ordered[:max_rows], already_truncated or len(ordered) > max_rows


async def evaluation_material(
    request: Request,
) -> tuple[
    list[dict[str, object]],
    list[SignalRecord],
    dict[str, list[dict[str, object]]],
    dict[str, NewsRecord],
    list[EvaluationEpochRecord],
]:
    state = request.app.state
    repository: NewsRepository = state.news_repository
    market_data_client: MoexMarketDataClient = state.market_data_client
    cache_key = (id(repository), id(market_data_client))
    cached = state.evaluation_material_cache
    if (
        cached is not None
        and cached[1] == cache_key
        and time.monotonic() - cached[0] < EVALUATION_CACHE_TTL_SECONDS
    ):
        return cached[2]

    async with state.evaluation_material_lock:
        cached = state.evaluation_material_cache
        if (
            cached is not None
            and cached[1] == cache_key
            and time.monotonic() - cached[0] < EVALUATION_CACHE_TTL_SECONDS
        ):
            return cached[2]
        inflight = state.evaluation_material_inflight
        if inflight is not None and inflight[0] == cache_key:
            task = inflight[1]
        else:
            task = asyncio.create_task(_load_evaluation_material(repository, market_data_client))
            state.evaluation_material_inflight = (cache_key, task)

            def cache_result(done: asyncio.Task) -> None:
                current = state.evaluation_material_inflight
                if current is None or current[1] is not done:
                    return
                state.evaluation_material_inflight = None
                if done.cancelled() or done.exception() is not None:
                    return
                state.evaluation_material_cache = (
                    time.monotonic(),
                    cache_key,
                    done.result(),
                )

            task.add_done_callback(cache_result)

    return await asyncio.shield(task)


def directional_epoch_outcomes(
    epoch: EvaluationEpochRecord | None,
) -> list[dict[str, object]]:
    if epoch is None:
        return []
    return [outcome for outcome in epoch.outcomes if outcome.get("direction") in {"up", "down"}]


def evaluation_epoch_meta(epoch: EvaluationEpochRecord) -> dict[str, object]:
    meta = epoch.as_meta_dict()
    meta["signals"] = len(directional_epoch_outcomes(epoch))
    meta["evaluation_methodology"] = evaluation_epoch_methodology(epoch)
    return meta


def latest_model_evaluation_epoch(
    epochs: list[EvaluationEpochRecord],
    model_version: str,
) -> EvaluationEpochRecord | None:
    """Select the newest snapshot of the highest config for one model."""
    candidates = [epoch for epoch in epochs if epoch.model_version == model_version]
    return max(
        candidates,
        key=lambda epoch: (epoch.config_version, epoch.evaluated_at, epoch.epoch_id),
        default=None,
    )


@app.get("/v1/evals", tags=["Evals"])
async def list_evals(
    request: Request,
    model_version: Annotated[
        str | None,
        Query(pattern=r"^[a-z0-9._-]{3,80}$"),
    ] = None,
) -> JSONResponse:
    repository: NewsRepository = request.app.state.news_repository
    epochs = latest_evaluation_epochs(
        await repository.list_evaluation_epochs(include_observations=False)
    )
    selected_model_version = model_version or CURRENT_NEWS_MODEL_VERSION
    selected_epoch = latest_model_evaluation_epoch(epochs, selected_model_version)
    selected_outcomes = directional_epoch_outcomes(selected_epoch)
    return JSONResponse(
        content={
            "data": {
                "summary": eval_summary(selected_outcomes),
                "breakdowns": eval_breakdowns(selected_outcomes),
                "relationships": eval_relationships(selected_outcomes),
                "quality_series": eval_quality_series(selected_outcomes),
                "outcomes": selected_outcomes,
            },
            "meta": {
                "generated_at": (
                    to_rfc3339(selected_epoch.evaluated_at) if selected_epoch else None
                ),
                "refresh_after_seconds": 600,
                "primary_horizon": "4h",
                "evaluation_window": "1h / 4h for product metrics; raw 1d / 3d retained",
                "evaluation_scope": "directional_signals_only",
                "evaluation_methodology": EVALUATION_METHODOLOGY_VERSION,
                "live_processing_lag_limit_seconds": int(
                    MAX_LIVE_PROCESSING_LAG.total_seconds()
                ),
                "horizon_observation_lag_limit_seconds": int(
                    MAX_HORIZON_OBSERVATION_LAG.total_seconds()
                ),
                "snapshot_status": "ready" if selected_epoch else "pending",
                "selected_model_version": selected_model_version,
                "selected_config_version": (
                    selected_epoch.config_version if selected_epoch else None
                ),
                "model_epochs": [evaluation_epoch_meta(epoch) for epoch in epochs],
                "warning": (
                    "Only point-in-time timing-eligible live signals enter quality metrics. "
                    "Returns use raw MOEX candles without fees, slippage or corporate-action "
                    "adjustment; this is not a calibrated backtest or proof of alpha."
                ),
            },
        }
    )


@app.get("/v1/evals/export", tags=["Evals"])
async def export_evals(
    request: Request,
    format: Literal["csv", "json"] = "csv",
    dataset: Literal["outcomes", "timeseries"] = "outcomes",
    model_version: Annotated[
        str,
        Query(pattern=r"^(all|[a-z0-9._-]{3,80})$"),
    ] = "all",
) -> Response:
    repository: NewsRepository = request.app.state.news_repository
    epochs = latest_evaluation_epochs(await repository.list_evaluation_epochs())
    selected_epochs = [
        epoch for epoch in epochs if model_version == "all" or epoch.model_version == model_version
    ]
    if dataset == "timeseries":
        directional_signal_ids = {
            str(outcome["signal_id"])
            for epoch in selected_epochs
            for outcome in directional_epoch_outcomes(epoch)
        }
        rows = [
            row
            for epoch in selected_epochs
            for row in epoch.observations
            if row.get("signal_id") in directional_signal_ids
        ]
        truncated = any(epoch.observations_truncated for epoch in selected_epochs)
        fieldnames = [
            "signal_id",
            "ticker",
            "signal_as_of",
            "signal_data_cutoff_at",
            "signal_created_at",
            "decision_at",
            "processing_lag_seconds",
            "evaluation_methodology",
            "direction",
            "score",
            "confidence",
            "model_version",
            "config_version",
            "evaluation_benchmark",
            "news_id",
            "news_source_id",
            "entry_at",
            "entry_price",
            "entry_delay_seconds",
            "observation_at",
            "offset_minutes",
            "open",
            "high",
            "low",
            "close",
            "value_rub",
            "volume_shares",
            "return_pct",
            "signed_return_pct",
        ]
    else:
        selected_outcomes = [
            outcome for epoch in selected_epochs for outcome in directional_epoch_outcomes(epoch)
        ]
        rows = outcome_export_rows(selected_outcomes)
        truncated = False
        fieldnames = [
            "signal_id",
            "ticker",
            "signal_as_of",
            "signal_data_cutoff_at",
            "signal_created_at",
            "decision_at",
            "processing_lag_seconds",
            "evaluation_methodology",
            "evaluation_eligible",
            "exclusion_reason",
            "direction",
            "score",
            "confidence",
            "model_version",
            "config_version",
            "evaluation_benchmark",
            "status",
            "news_id",
            "news_source_id",
            "news_url",
            "news_published_at",
            "news_received_at",
            "delivery_lag_seconds",
            "entry_at",
            "entry_price",
            "entry_delay_seconds",
            "return_1h_pct",
            "return_4h_pct",
            "return_1d_pct",
            "return_3d_pct",
            "observed_1h_at",
            "observed_4h_at",
            "observed_1d_at",
            "observed_3d_at",
            "observation_1h_delay_seconds",
            "observation_4h_delay_seconds",
            "observation_1d_delay_seconds",
            "observation_3d_delay_seconds",
            "latest_price",
            "latest_return_pct",
            "verdict",
        ]

    generated_at = utc_now()
    filename = f"eventedge-{dataset}-{generated_at[:10]}.{format}"
    headers = {
        "Cache-Control": "no-store",
        "Content-Disposition": f'attachment; filename="{filename}"',
    }
    if format == "json":
        return JSONResponse(
            content={
                "data": rows,
                "meta": {
                    "dataset": dataset,
                    "rows": len(rows),
                    "truncated": truncated,
                    "generated_at": generated_at,
                    "model_version": model_version,
                },
            },
            headers=headers,
        )

    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            **headers,
            "X-EventEdge-Rows": str(len(rows)),
            "X-EventEdge-Truncated": str(truncated).lower(),
        },
    )


@app.get("/v1/instruments/snapshots", tags=["Instruments"])
async def list_instrument_snapshots(
    request: Request,
    tickers: Annotated[str, Query(min_length=1, max_length=260)],
) -> JSONResponse:
    normalized = list(
        dict.fromkeys(part.strip().upper() for part in tickers.split(",") if part.strip())
    )
    if (
        not normalized
        or len(normalized) > 20
        or any(not re.fullmatch(r"[A-Z0-9]{1,12}", ticker) for ticker in normalized)
    ):
        return problem_response(
            request,
            status=400,
            code="INVALID_PARAMETER",
            title="Invalid instrument tickers",
            detail="tickers must contain 1 to 20 comma-separated MOEX tickers.",
        )

    market_data_client: MoexMarketDataClient = request.app.state.market_data_client
    content, market_results = await asyncio.gather(
        load_content_snapshot(request),
        asyncio.gather(
            *(market_data_client.snapshot(ticker) for ticker in normalized),
            return_exceptions=True,
        ),
    )
    active_by_ticker = active_signals_from_content(*content)
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

    news, signals = await load_content_snapshot(request)
    active_signal = active_signals_from_content(news, signals).get(normalized_ticker)
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
