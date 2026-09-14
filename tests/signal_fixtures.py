from __future__ import annotations

from datetime import UTC, datetime, timedelta


def signal_example(
    index: int,
    *,
    decision_at: datetime | None = None,
    event_id: str | None = None,
    ticker: str = "SBER",
    return_pct: float = 1.0,
    benchmark_return_pct: float | None = 0.0,
    label_source: str = "synthetic_test",
    label_available_at: datetime | None = None,
    feature_as_of: datetime | None = None,
    rule_direction: str = "up",
    pre_event_return_1h_pct: float | None = 0.25,
) -> dict[str, object]:
    decision = decision_at or datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)
    published_at = decision - timedelta(minutes=5)
    received_at = decision - timedelta(minutes=2)
    entry_at = decision + timedelta(minutes=10)
    target_at = entry_at + timedelta(hours=4)
    entry_price = 100.0
    observed_price = entry_price * (1 + return_pct / 100)
    abnormal_return_pct = (
        return_pct - benchmark_return_pct if benchmark_return_pct is not None else None
    )
    benchmark_id = "IMOEX" if benchmark_return_pct is not None else None
    news_id = f"news-{index}"
    return {
        "schema_version": "signal-dataset-example-1.0",
        "id": f"signal-example-{index}-{ticker}",
        "event_id": event_id or f"event-{index}",
        "ticker": ticker,
        "news_ids": [news_id],
        "primary_news_id": news_id,
        "source_id": "fixture",
        "corroborating_source_ids": [],
        "title": f"Компания опубликовала результаты {index}",
        "content": "Финансовый результат изменился относительно прошлого периода.",
        "categories": ["Компании"],
        "published_at": published_at.isoformat(),
        "received_at": received_at.isoformat(),
        "decision_at": decision.isoformat(),
        "entry_at": entry_at.isoformat(),
        "entry_price": entry_price,
        "features": {
            "schema_version": "signal-features-1.0",
            "as_of": (feature_as_of or decision).isoformat(),
            "event_type": "financial_results",
            "polarity": 0.5,
            "materiality": 0.8,
            "novelty": 1.0,
            "source_quality": 0.9,
            "fact_count": 2,
            "rule_direction": rule_direction,
            "rule_score": 25.0 if rule_direction == "up" else -35.0,
            "rule_confidence": 0.85,
            "rule_model_version": "signal-engine-fixture",
            "rule_config_version": 1,
            "pre_event_return_1h_pct": pre_event_return_1h_pct,
            "pre_event_return_1d_pct": 0.5,
            "pre_event_return_5d_pct": 1.0,
            "benchmark_pre_event_return_1h_pct": 0.1,
            "volatility_20d_pct": 2.0,
            "volume_ratio": 1.2,
            "liquidity_status": "sufficient",
        },
        "outcome_4h": {
            "schema_version": "market-outcome-observation-1.0",
            "target_at": target_at.isoformat(),
            "observed_at": target_at.isoformat(),
            "price": observed_price,
            "return_pct": return_pct,
            "benchmark_id": benchmark_id,
            "benchmark_return_pct": benchmark_return_pct,
            "abnormal_return_pct": abnormal_return_pct,
            "price_field": "open",
            "timely": True,
        },
        "label_available_at": (label_available_at or target_at).isoformat(),
        "label_source": label_source,
        "corporate_action_status": "none",
        "overlapping_event_ids": [],
        "notes": "Synthetic contract fixture; not financial ground truth.",
    }
