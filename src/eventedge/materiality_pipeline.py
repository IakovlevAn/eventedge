"""Delayed materiality inference for freshly ingested company news."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from eventedge.analysis import DEFAULT_MOEX_ALIASES
from eventedge.market import InstrumentNotFoundError, MarketDataUnavailableError
from eventedge.materiality import (
    DAILY_LOOKBACK_DAYS,
    DECISION_DELAY,
    FEATURE_SCHEMA_VERSION,
    INTRADAY_LOOKBACK_DAYS,
    MATERIALITY_BENCHMARK_TICKER,
    MaterialityMode,
    MaterialityRuntime,
    PortableMaterialityModel,
    build_materiality_feature_row,
    missing_materiality_features,
)
from eventedge.storage import (
    MaterialityPredictionRecord,
    NewsRecord,
    NewsRepository,
    SignalRecord,
    stable_id,
)

LOGGER = logging.getLogger(__name__)
MAXIMUM_CANDIDATE_AGE = timedelta(days=7)
DEFERRED_RETRY_INTERVAL = timedelta(minutes=10)
MATERIALITY_CONCURRENCY = 2


class MaterialityMarketDataClient(Protocol):
    """Market-data operation required by online materiality inference."""

    async def candles(
        self,
        ticker: str,
        *,
        interval: int = 10,
        lookback_days: int = 14,
    ) -> dict[str, object]: ...


@dataclass(frozen=True)
class MaterialityCandidate:
    """A due company-news pair without a completed current-model decision."""

    news: NewsRecord
    ticker: str
    existing: MaterialityPredictionRecord | None

    @property
    def decision_at(self) -> datetime:
        return self.news.published_at.astimezone(UTC) + DECISION_DELAY


def select_materiality_candidates(
    news: Sequence[NewsRecord],
    signals: Sequence[SignalRecord],
    *,
    model_version: str,
    artifact_payload_sha256: str,
    now: datetime,
) -> list[MaterialityCandidate]:
    """Select bounded-age, due pairs while preserving retry idempotency."""
    _require_aware(now, "now")
    now = now.astimezone(UTC)
    signals_by_news: dict[str, set[str]] = {}
    for signal in signals:
        if signal.ticker in DEFAULT_MOEX_ALIASES and signal.ticker != "MOEX":
            signals_by_news.setdefault(signal.news_id, set()).add(signal.ticker)

    candidates = []
    for item in news:
        published_at = item.published_at.astimezone(UTC)
        if not published_at <= now - DECISION_DELAY:
            continue
        if published_at < now - MAXIMUM_CANDIDATE_AGE:
            continue
        tickers = materiality_candidate_tickers(
            item, signals_by_news.get(item.id, set())
        )
        for ticker in tickers:
            existing = _current_prediction(
                item,
                ticker,
                model_version,
                artifact_payload_sha256,
            )
            if existing is not None and existing.status == "ready":
                continue
            if (
                existing is not None
                and existing.status == "deferred"
                and existing.updated_at > now - DEFERRED_RETRY_INTERVAL
            ):
                continue
            candidates.append(
                MaterialityCandidate(news=item, ticker=ticker, existing=existing)
            )
    return sorted(
        candidates,
        key=lambda candidate: (
            candidate.decision_at,
            candidate.news.id,
            candidate.ticker,
        ),
    )


async def refresh_materiality_predictions(
    repository: NewsRepository,
    market_data: MaterialityMarketDataClient,
    runtime: MaterialityRuntime,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Infer and persist one bounded idempotent batch of due predictions."""
    if runtime.mode is MaterialityMode.DISABLED or runtime.model is None:
        return {
            "status": "disabled",
            "attempted": 0,
            "ready": 0,
            "deferred": 0,
            "failed": 0,
            "remaining": 0,
        }
    evaluated_at = (now or datetime.now(UTC)).astimezone(UTC)
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
    candidates = select_materiality_candidates(
        news,
        signals,
        model_version=runtime.model.model_version,
        artifact_payload_sha256=runtime.model.artifact_payload_sha256,
        now=evaluated_at,
    )
    selected = candidates[: runtime.batch_limit]
    semaphore = asyncio.Semaphore(MATERIALITY_CONCURRENCY)

    async def process(candidate: MaterialityCandidate) -> str:
        async with semaphore:
            try:
                prediction = await infer_materiality_prediction(
                    candidate,
                    market_data,
                    runtime.model,
                    now=evaluated_at,
                )
                await repository.upsert_materiality_prediction(prediction)
                return prediction.status
            except Exception:
                LOGGER.exception(
                    "Unexpected materiality inference failure for %s/%s",
                    candidate.news.id,
                    candidate.ticker,
                )
                return "failed"

    statuses = await asyncio.gather(*(process(candidate) for candidate in selected))
    return {
        "status": "ok" if "failed" not in statuses else "partial",
        "mode": runtime.mode.value,
        "model_version": runtime.model.model_version,
        "attempted": len(selected),
        "ready": statuses.count("ready"),
        "deferred": statuses.count("deferred"),
        "failed": statuses.count("failed"),
        "remaining": max(0, len(candidates) - len(selected)),
    }


async def infer_materiality_prediction(
    candidate: MaterialityCandidate,
    market_data: MaterialityMarketDataClient,
    model: PortableMaterialityModel,
    *,
    now: datetime,
) -> MaterialityPredictionRecord:
    """Build causal features and score one news/ticker candidate."""
    _require_aware(now, "now")
    try:
        stock_intraday, benchmark_intraday, stock_daily, benchmark_daily = (
            await asyncio.gather(
                market_data.candles(
                    candidate.ticker,
                    interval=1,
                    lookback_days=INTRADAY_LOOKBACK_DAYS,
                ),
                market_data.candles(
                    MATERIALITY_BENCHMARK_TICKER,
                    interval=1,
                    lookback_days=INTRADAY_LOOKBACK_DAYS,
                ),
                market_data.candles(
                    candidate.ticker,
                    interval=24,
                    lookback_days=DAILY_LOOKBACK_DAYS,
                ),
                market_data.candles(
                    MATERIALITY_BENCHMARK_TICKER,
                    interval=24,
                    lookback_days=DAILY_LOOKBACK_DAYS,
                ),
            )
        )
        row = build_materiality_feature_row(
            news_id=candidate.news.id,
            ticker=candidate.ticker,
            source_id=candidate.news.source_id,
            published_at=candidate.news.published_at,
            stock_intraday=stock_intraday,
            benchmark_intraday=benchmark_intraday,
            stock_daily=stock_daily,
            benchmark_daily=benchmark_daily,
        )
        score = model.predict(row)
    except InstrumentNotFoundError:
        return _deferred_prediction(candidate, model, now, "instrument_not_found")
    except MarketDataUnavailableError:
        return _deferred_prediction(candidate, model, now, "market_data_unavailable")
    except ValueError:
        LOGGER.warning(
            "Invalid materiality market inputs for %s/%s",
            candidate.news.id,
            candidate.ticker,
            exc_info=True,
        )
        return _deferred_prediction(candidate, model, now, "invalid_market_data")

    return MaterialityPredictionRecord(
        id=_prediction_id(candidate, model),
        news_id=candidate.news.id,
        ticker=candidate.ticker,
        decision_at=row.decision_at,
        data_cutoff_at=row.decision_at,
        status="ready",
        reason=None,
        raw_probability=score.raw_probability,
        calibrated_probability=score.calibrated_probability,
        selected_at_coverage_pct=score.selected_at_coverage_pct,
        eligible_for_ranking=(
            model.category_was_seen("ticker", candidate.ticker)
            and model.category_was_seen("source_id", candidate.news.source_id)
            and row.numeric["reaction_abnormal_pct"] is not None
        ),
        missing_features=missing_materiality_features(row),
        model_id=score.model_id,
        model_version=score.model_version,
        artifact_payload_sha256=score.artifact_payload_sha256,
        feature_schema_version=row.feature_schema_version,
        created_at=(
            candidate.existing.created_at if candidate.existing is not None else now
        ),
        updated_at=now,
    )


def materiality_api_payload(
    item: NewsRecord,
    *,
    runtime: MaterialityRuntime,
) -> dict[str, object] | None:
    """Return current-model decisions without exposing stale model versions."""
    if runtime.mode is MaterialityMode.DISABLED or runtime.model is None:
        return None
    predictions = [
        prediction
        for prediction in item.materiality_predictions
        if prediction.model_version == runtime.model.model_version
        and prediction.artifact_payload_sha256
        == runtime.model.artifact_payload_sha256
    ]
    ready = [
        prediction
        for prediction in predictions
        if prediction.status == "ready"
        and prediction.calibrated_probability is not None
    ]
    due_at = item.published_at.astimezone(UTC) + DECISION_DELAY
    status = (
        "ready"
        if ready
        else "deferred"
        if predictions
        else "pending"
    )
    return {
        "status": status,
        "mode": runtime.mode.value,
        "available_after": due_at.isoformat().replace("+00:00", "Z"),
        "max_probability": (
            max(float(prediction.calibrated_probability) for prediction in ready)
            if ready
            else None
        ),
        "model_version": runtime.model.model_version,
        "predictions": [
            prediction.as_api_dict()
            for prediction in sorted(
                predictions,
                key=lambda value: (value.ticker, value.updated_at, value.id),
            )
        ],
    }


def rank_news_by_materiality(
    news: Sequence[NewsRecord],
    *,
    runtime: MaterialityRuntime,
) -> list[NewsRecord]:
    """Rerank comparable ready rows without burying fresh or out-of-domain news."""
    if runtime.mode is not MaterialityMode.RANK or runtime.model is None:
        return list(news)

    ranked = sorted(
        news,
        key=lambda item: (item.published_at.astimezone(UTC), item.id),
        reverse=True,
    )
    positions_by_day: dict[date, list[int]] = {}
    for index, item in enumerate(ranked):
        day = item.published_at.astimezone(UTC).date()
        positions_by_day.setdefault(day, []).append(index)

    for positions in positions_by_day.values():
        scored_positions = [
            index
            for index in positions
            if _ranking_probability(ranked[index], runtime.model) is not None
        ]
        scored_news = sorted(
            (ranked[index] for index in scored_positions),
            key=lambda item: (
                _ranking_probability(item, runtime.model),
                item.published_at.astimezone(UTC),
                item.id,
            ),
            reverse=True,
        )
        for index, item in zip(scored_positions, scored_news, strict=True):
            ranked[index] = item
    return ranked


def _ranking_probability(
    item: NewsRecord,
    model: PortableMaterialityModel,
) -> float | None:
    probabilities = [
        float(prediction.calibrated_probability)
        for prediction in item.materiality_predictions
        if prediction.model_version == model.model_version
        and prediction.artifact_payload_sha256 == model.artifact_payload_sha256
        and prediction.status == "ready"
        and prediction.eligible_for_ranking
        and prediction.calibrated_probability is not None
    ]
    return max(probabilities, default=None)


def _deferred_prediction(
    candidate: MaterialityCandidate,
    model: PortableMaterialityModel,
    now: datetime,
    reason: str,
) -> MaterialityPredictionRecord:
    return MaterialityPredictionRecord(
        id=_prediction_id(candidate, model),
        news_id=candidate.news.id,
        ticker=candidate.ticker,
        decision_at=candidate.decision_at,
        data_cutoff_at=candidate.decision_at,
        status="deferred",
        reason=reason,
        raw_probability=None,
        calibrated_probability=None,
        selected_at_coverage_pct={},
        eligible_for_ranking=False,
        missing_features=(),
        model_id=model.model_id,
        model_version=model.model_version,
        artifact_payload_sha256=model.artifact_payload_sha256,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        created_at=(
            candidate.existing.created_at if candidate.existing is not None else now
        ),
        updated_at=now,
    )


def _prediction_id(
    candidate: MaterialityCandidate,
    model: PortableMaterialityModel,
) -> str:
    return stable_id(
        "mat_",
        "\x00".join(
            (
                candidate.news.id,
                candidate.ticker,
                model.model_version,
                model.artifact_payload_sha256,
                candidate.decision_at.isoformat(),
            )
        ),
    )


def materiality_candidate_tickers(
    item: NewsRecord,
    signal_tickers: set[str],
) -> tuple[str, ...]:
    """Return supported issuer tickers eligible for delayed inference."""
    metadata_tickers = item.source_metadata.get("tickers")
    has_candidate_metadata = bool(
        item.source_metadata.get("analysis_candidate")
        or item.source_metadata.get("signal_candidate")
    )
    values: set[str] = set(signal_tickers)
    if has_candidate_metadata and isinstance(metadata_tickers, list | tuple):
        values.update(str(value).upper() for value in metadata_tickers[:20])
    return tuple(
        sorted(
            ticker
            for ticker in values
            if ticker in DEFAULT_MOEX_ALIASES and ticker != "MOEX"
        )
    )


def _current_prediction(
    item: NewsRecord,
    ticker: str,
    model_version: str,
    artifact_payload_sha256: str,
) -> MaterialityPredictionRecord | None:
    candidates = [
        prediction
        for prediction in item.materiality_predictions
        if prediction.ticker == ticker
        and prediction.model_version == model_version
        and prediction.artifact_payload_sha256 == artifact_payload_sha256
    ]
    return max(candidates, key=lambda value: (value.updated_at, value.id), default=None)


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
