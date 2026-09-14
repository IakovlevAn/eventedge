"""Tests for delayed materiality inference and product ranking."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from eventedge.market import MarketDataUnavailableError
from eventedge.materiality import MaterialityMode, runtime_from_environment
from eventedge.materiality_pipeline import (
    DEFERRED_RETRY_INTERVAL,
    MaterialityCandidate,
    infer_materiality_prediction,
    materiality_api_payload,
    rank_news_by_materiality,
    refresh_materiality_predictions,
    select_materiality_candidates,
    select_materiality_outcome_candidates,
)
from eventedge.storage import (
    MaterialityPredictionRecord,
    MemoryNewsRepository,
    NewsDocument,
    NewsRecord,
)


def _market_payload(
    observations: list[tuple[datetime, float]],
) -> dict[str, object]:
    return {
        "candles": [
            {
                "begin": timestamp.isoformat().replace("+00:00", "Z"),
                "open": price,
            }
            for timestamp, price in observations
        ]
    }


def _daily_payload(last_day: date, *, multiplier: float) -> dict[str, object]:
    observations = []
    price = 100.0
    for offset in range(70, 0, -1):
        day = last_day - timedelta(days=offset)
        price *= 1 + multiplier * (1 + offset % 5) / 1_000
        observations.append(
            (
                datetime(day.year, day.month, day.day, 21, tzinfo=UTC)
                - timedelta(days=1),
                price,
            )
        )
    return _market_payload(observations)


class FakeMarketData:
    def __init__(self, published_at: datetime) -> None:
        self.published_at = published_at
        self.calls: list[tuple[str, int, int]] = []

    async def candles(
        self,
        ticker: str,
        *,
        interval: int = 10,
        lookback_days: int = 14,
    ) -> dict[str, object]:
        self.calls.append((ticker, interval, lookback_days))
        if interval == 24:
            return _daily_payload(
                self.published_at.date(),
                multiplier=2.0 if ticker != "IMOEX2" else 1.0,
            )
        start_price = 200.0 if ticker == "IMOEX2" else 100.0
        return _market_payload(
            [
                (self.published_at - timedelta(days=5), start_price * 0.94),
                (self.published_at - timedelta(days=1), start_price * 0.97),
                (self.published_at - timedelta(hours=1), start_price * 0.99),
                (self.published_at, start_price),
                (
                    self.published_at + timedelta(minutes=4),
                    start_price * (1.015 if ticker != "IMOEX2" else 1.002),
                ),
                (
                    self.published_at + timedelta(minutes=5),
                    start_price * (1.02 if ticker != "IMOEX2" else 1.003),
                ),
            ]
        )


class UnavailableMarketData:
    async def candles(
        self,
        ticker: str,
        *,
        interval: int = 10,
        lookback_days: int = 14,
    ) -> dict[str, object]:
        raise MarketDataUnavailableError(f"No candles for {ticker}")


class MissingReactionMarketData(FakeMarketData):
    async def candles(
        self,
        ticker: str,
        *,
        interval: int = 10,
        lookback_days: int = 14,
    ) -> dict[str, object]:
        if interval == 24:
            return await super().candles(
                ticker,
                interval=interval,
                lookback_days=lookback_days,
            )
        return _market_payload(
            [(self.published_at - timedelta(hours=1), 100.0)]
        )


def _news(
    news_id: str,
    published_at: datetime,
    *,
    source_id: str = "telegram_markettwits",
    predictions: tuple[MaterialityPredictionRecord, ...] = (),
) -> NewsRecord:
    return NewsRecord(
        id=news_id,
        source_id=source_id,
        external_id=news_id,
        published_at=published_at,
        received_at=published_at,
        title="Сбербанк сообщил о событии",
        url=f"https://example.com/{news_id}",
        content="Сбербанк сообщил о событии.",
        language="ru",
        source_metadata={"analysis_candidate": True, "tickers": ["SBER"]},
        created_at=published_at,
        materiality_predictions=predictions,
    )


def _prediction(
    news: NewsRecord,
    *,
    probability: float,
    status: str = "ready",
    updated_at: datetime | None = None,
) -> MaterialityPredictionRecord:
    runtime = runtime_from_environment({"NEWS_MATERIALITY_MODE": "shadow"})
    assert runtime.model is not None
    decision_at = news.published_at + timedelta(minutes=5)
    return MaterialityPredictionRecord(
        id=f"mat_{news.id}",
        news_id=news.id,
        ticker="SBER",
        decision_at=decision_at,
        data_cutoff_at=decision_at,
        status=status,
        reason=None if status == "ready" else "market_data_unavailable",
        raw_probability=probability if status == "ready" else None,
        calibrated_probability=probability if status == "ready" else None,
        selected_at_coverage_pct={"calibrated": (100,)},
        eligible_for_ranking=status == "ready",
        missing_features=(),
        model_id=runtime.model.model_id,
        model_version=runtime.model.model_version,
        artifact_payload_sha256=runtime.model.artifact_payload_sha256,
        feature_schema_version="news-materiality-runtime-features-1.0",
        created_at=decision_at,
        updated_at=updated_at or decision_at,
    )


def test_candidate_selection_waits_five_minutes_and_respects_retry_state() -> None:
    runtime = runtime_from_environment({"NEWS_MATERIALITY_MODE": "shadow"})
    assert runtime.model is not None
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    due = _news("news_due", now - timedelta(minutes=6))
    future = _news("news_future", now - timedelta(minutes=4))
    expired = _news("news_expired", now - timedelta(days=8))
    ready_news = _news("news_ready", now - timedelta(minutes=20))
    ready_news = replace(
        ready_news,
        materiality_predictions=(_prediction(ready_news, probability=0.8),),
    )
    deferred_news = _news("news_deferred", now - timedelta(minutes=20))
    deferred_news = replace(
        deferred_news,
        materiality_predictions=(
            _prediction(
                deferred_news,
                probability=0.0,
                status="deferred",
                updated_at=now - DEFERRED_RETRY_INTERVAL / 2,
            ),
        ),
    )
    stale_artifact = _news("news_stale_artifact", now - timedelta(minutes=20))
    stale_artifact = replace(
        stale_artifact,
        materiality_predictions=(
            replace(
                _prediction(stale_artifact, probability=0.7),
                artifact_payload_sha256="b" * 64,
            ),
        ),
    )

    candidates = select_materiality_candidates(
        [future, ready_news, deferred_news, stale_artifact, expired, due],
        [],
        model_version=runtime.model.model_version,
        artifact_payload_sha256=runtime.model.artifact_payload_sha256,
        now=now,
    )

    assert [(candidate.news.id, candidate.ticker) for candidate in candidates] == [
        ("news_stale_artifact", "SBER"),
        ("news_due", "SBER"),
    ]


def test_inference_uses_imoex2_and_marks_training_domain() -> None:
    runtime = runtime_from_environment({"NEWS_MATERIALITY_MODE": "shadow"})
    assert runtime.model is not None
    published_at = datetime(2026, 9, 10, 9, tzinfo=UTC)
    news = _news("news_inference", published_at)
    market = FakeMarketData(published_at)

    prediction = asyncio.run(
        infer_materiality_prediction(
            MaterialityCandidate(news=news, ticker="SBER", existing=None),
            market,
            runtime.model,
            now=published_at + timedelta(minutes=10),
        )
    )

    assert prediction.status == "ready"
    assert prediction.eligible_for_ranking
    assert prediction.data_cutoff_at == published_at + timedelta(minutes=5)
    assert prediction.calibrated_probability is not None
    assert sorted(market.calls) == [
        ("IMOEX2", 1, 14),
        ("IMOEX2", 24, 120),
        ("SBER", 1, 14),
        ("SBER", 24, 120),
    ]


def test_inference_defers_unavailable_market_data_without_losing_provenance() -> None:
    runtime = runtime_from_environment({"NEWS_MATERIALITY_MODE": "shadow"})
    assert runtime.model is not None
    published_at = datetime(2026, 9, 10, 9, tzinfo=UTC)
    news = _news("news_deferred_market", published_at)
    now = published_at + timedelta(minutes=10)

    prediction = asyncio.run(
        infer_materiality_prediction(
            MaterialityCandidate(news=news, ticker="SBER", existing=None),
            UnavailableMarketData(),
            runtime.model,
            now=now,
        )
    )

    assert prediction.status == "deferred"
    assert prediction.reason == "market_data_unavailable"
    assert prediction.calibrated_probability is None
    assert prediction.data_cutoff_at == published_at + timedelta(minutes=5)
    assert prediction.model_version == runtime.model.model_version


def test_unseen_source_is_scored_and_admitted_to_ranking() -> None:
    runtime = runtime_from_environment({"NEWS_MATERIALITY_MODE": "shadow"})
    assert runtime.model is not None
    published_at = datetime(2026, 9, 10, 9, tzinfo=UTC)
    news = _news("news_unseen_source", published_at, source_id="interfax")

    prediction = asyncio.run(
        infer_materiality_prediction(
            MaterialityCandidate(news=news, ticker="SBER", existing=None),
            FakeMarketData(published_at),
            runtime.model,
            now=published_at + timedelta(minutes=10),
        )
    )

    assert prediction.status == "ready"
    assert prediction.eligible_for_ranking


def test_current_policy_admits_stored_unseen_source_prediction() -> None:
    runtime = runtime_from_environment({"NEWS_MATERIALITY_MODE": "shadow"})
    published_at = datetime(2026, 9, 10, 9, tzinfo=UTC)
    news = _news("news_stored_unseen_source", published_at, source_id="interfax")
    news = replace(
        news,
        materiality_predictions=(
            replace(_prediction(news, probability=0.7), eligible_for_ranking=False),
        ),
    )

    payload = materiality_api_payload(news, runtime=runtime)

    assert payload is not None
    assert payload["predictions"][0]["eligible_for_ranking"] is True


def test_missing_five_minute_reaction_is_not_admitted_to_ranking() -> None:
    runtime = runtime_from_environment({"NEWS_MATERIALITY_MODE": "shadow"})
    assert runtime.model is not None
    published_at = datetime(2026, 9, 10, 9, tzinfo=UTC)
    news = _news("news_missing_reaction", published_at)

    prediction = asyncio.run(
        infer_materiality_prediction(
            MaterialityCandidate(news=news, ticker="SBER", existing=None),
            MissingReactionMarketData(published_at),
            runtime.model,
            now=published_at + timedelta(minutes=10),
        )
    )

    assert prediction.status == "ready"
    assert "reaction_abnormal_pct" in prediction.missing_features
    assert not prediction.eligible_for_ranking


def test_refresh_is_idempotent_after_ready_prediction() -> None:
    async def scenario() -> tuple[dict[str, object], dict[str, object], NewsRecord]:
        published_at = datetime(2026, 9, 10, 9, tzinfo=UTC)
        repository = MemoryNewsRepository()
        await repository.ingest(
            "materiality-pipeline-ingest",
            NewsDocument(
                source_id="telegram_markettwits",
                external_id="pipeline-example",
                published_at=published_at,
                received_at=published_at,
                title="Сбербанк сообщил о событии",
                url="https://example.com/pipeline",
                content="Сбербанк сообщил о существенном событии.",
                language="ru",
                source_metadata={"analysis_candidate": True, "tickers": ["SBER"]},
                payload_hash="pipeline-payload",
            ),
            generate_signals=False,
        )
        runtime = runtime_from_environment(
            {
                "NEWS_MATERIALITY_MODE": "shadow",
                "NEWS_MATERIALITY_BATCH_LIMIT": "2",
            }
        )
        market = FakeMarketData(published_at)
        now = published_at + timedelta(minutes=10)
        first = await refresh_materiality_predictions(
            repository, market, runtime, now=now
        )
        second = await refresh_materiality_predictions(
            repository, market, runtime, now=now
        )
        stored = (await repository.list_news(source_id=None, limit=10))[0]
        return first, second, stored

    first, second, stored = asyncio.run(scenario())

    assert first["ready"] == 1
    assert second["attempted"] == 0
    assert len(stored.materiality_predictions) == 1


def test_refresh_persists_due_materiality_outcome() -> None:
    async def scenario() -> tuple[dict[str, object], MaterialityPredictionRecord]:
        published_at = datetime(2026, 9, 10, 9, tzinfo=UTC)
        news = _news("news_outcome", published_at)
        prediction = _prediction(news, probability=0.8)
        repository = MemoryNewsRepository()
        await repository.ingest(
            "materiality-outcome-ingest",
            NewsDocument(
                source_id=news.source_id,
                external_id=news.external_id,
                published_at=news.published_at,
                received_at=news.received_at,
                title=news.title,
                url=news.url,
                content=news.content,
                language=news.language,
                source_metadata=news.source_metadata,
                payload_hash="materiality-outcome-payload",
            ),
            generate_signals=False,
        )
        stored_news = (await repository.list_news(source_id=None, limit=1))[0]
        prediction = replace(prediction, news_id=stored_news.id)
        await repository.upsert_materiality_prediction(prediction)
        entry = prediction.decision_at + timedelta(minutes=1)
        target = entry + timedelta(hours=4)

        class OutcomeMarketData:
            async def candles(
                self,
                ticker: str,
                *,
                interval: int = 10,
                lookback_days: int = 14,
            ) -> dict[str, object]:
                assert interval == 1
                start = 200.0 if ticker == "IMOEX2" else 100.0
                finish = 200.2 if ticker == "IMOEX2" else 101.0
                return _market_payload([(entry, start), (target, finish)])

        runtime = runtime_from_environment({"NEWS_MATERIALITY_MODE": "shadow"})
        result = await refresh_materiality_predictions(
            repository,
            OutcomeMarketData(),
            runtime,
            now=target + timedelta(minutes=1),
        )
        current = (await repository.list_news(source_id=None, limit=1))[0]
        return result, current.materiality_predictions[0]

    result, prediction = asyncio.run(scenario())

    assert result["attempted"] == 0
    assert result["outcomes_evaluated"] == 1
    assert prediction.outcome_4h is not None
    assert prediction.outcome_4h["actual_material"] is True
    assert prediction.outcome_4h["verdict"] is False


def test_outcome_candidate_selection_requires_due_current_ready_prediction() -> None:
    runtime = runtime_from_environment({"NEWS_MATERIALITY_MODE": "shadow"})
    assert runtime.model is not None
    now = datetime(2026, 9, 10, 14, tzinfo=UTC)
    due = _news("due_outcome", now - timedelta(hours=5))
    due = replace(due, materiality_predictions=(_prediction(due, probability=0.7),))
    pending = _news("pending_outcome", now - timedelta(hours=2))
    pending = replace(
        pending,
        materiality_predictions=(_prediction(pending, probability=0.7),),
    )

    selected = select_materiality_outcome_candidates(
        [pending, due],
        model_version=runtime.model.model_version,
        artifact_payload_sha256=runtime.model.artifact_payload_sha256,
        now=now,
    )

    assert [candidate.prediction.news_id for candidate in selected] == ["due_outcome"]


def test_rank_mode_uses_probability_only_within_publication_day() -> None:
    runtime = runtime_from_environment({"NEWS_MATERIALITY_MODE": "rank"})
    day_one = datetime(2026, 9, 10, 8, tzinfo=UTC)
    low = _news("news_low", day_one + timedelta(hours=3))
    low = replace(
        low,
        materiality_predictions=(
            _prediction(low, probability=0.2),
            replace(
                _prediction(low, probability=0.99),
                id="mat_stale_artifact",
                artifact_payload_sha256="b" * 64,
            ),
        ),
    )
    high = _news("news_high", day_one + timedelta(hours=1))
    high = replace(high, materiality_predictions=(_prediction(high, probability=0.9),))
    next_day = _news("news_next_day", day_one + timedelta(days=1))

    ranked = rank_news_by_materiality([low, next_day, high], runtime=runtime)

    assert [item.id for item in ranked] == ["news_next_day", "news_high", "news_low"]
    payload = materiality_api_payload(low, runtime=runtime)
    assert payload is not None
    assert payload["status"] == "ready"
    assert payload["max_probability"] == 0.2
    assert len(payload["predictions"]) == 1


def test_disabled_mode_returns_no_api_materiality() -> None:
    runtime = runtime_from_environment({"NEWS_MATERIALITY_MODE": "disabled"})
    news = _news("news_disabled", datetime(2026, 9, 10, tzinfo=UTC))

    assert runtime.mode is MaterialityMode.DISABLED
    assert materiality_api_payload(news, runtime=runtime) is None
