from datetime import UTC, datetime, timedelta

from eventedge.evals import (
    build_assessment,
    demo_account,
    eval_summary,
    evaluate_signal,
    quant_factors,
)
from eventedge.storage import NewsRecord, SignalRecord


def market_snapshot() -> dict[str, object]:
    start = datetime(2026, 7, 29, tzinfo=UTC)
    closes = [100.0, 101.0, 100.5, 102.0, 103.0, 105.0]
    candles = [
        {
            "begin": (start + timedelta(days=index)).isoformat().replace("+00:00", "Z"),
            "open": close - 0.5,
            "close": close,
            "high": close + 1,
            "low": close - 1,
            "value_rub": 300_000_000.0,
            "volume_shares": 1_000_000 + index * 100_000,
        }
        for index, close in enumerate(closes)
    ]
    return {
        "ticker": "SBER",
        "name": "Сбербанк",
        "last_price": "105.0",
        "observed_at": "2026-08-03T15:40:00Z",
        "daily_change_pct": 1.94,
        "volume_shares": 2_500_000,
        "value_rub": 300_000_000.0,
        "liquidity_status": "sufficient",
        "daily_volatility_pct": 1.6,
        "candles": candles,
        "source": {"name": "MOEX ISS", "url": "https://iss.moex.com/iss/"},
    }


def reporting_news() -> NewsRecord:
    timestamp = datetime(2026, 8, 3, 10, tzinfo=UTC)
    return NewsRecord(
        id="news_report",
        source_id="interfax",
        external_id="report-1",
        published_at=timestamp,
        received_at=timestamp,
        title="Сбербанк опубликовал отчётность по МСФО",
        url="https://example.com/report",
        content="Чистая прибыль выросла и превысила ожидания рынка.",
        language="ru",
        source_metadata={"tickers": ["SBER"]},
        created_at=timestamp,
    )


def signal_record() -> SignalRecord:
    timestamp = datetime(2026, 8, 3, 7, tzinfo=UTC)
    return SignalRecord(
        id="sig_test",
        news_id="news_report",
        ticker="SBER",
        as_of=timestamp,
        data_cutoff_at=timestamp,
        status="active",
        direction="up",
        action="consider_buy",
        horizon_value=3,
        horizon_unit="calendar_days",
        score=40.0,
        strength=0.4,
        confidence=0.75,
        summary="Позитивный отчёт",
        factor_contributions=(),
        evidence_refs=("news_report",),
        expires_at=timestamp + timedelta(days=3),
        invalidation_conditions=(),
        model_version="news-baseline-0.1.1",
        config_version=1,
        created_at=timestamp,
    )


def test_quant_layer_uses_all_requested_non_llm_factors() -> None:
    factors, score, confidence = quant_factors(
        "SBER",
        market_snapshot(),
        [reporting_news()],
    )

    assert [factor["code"] for factor in factors] == [
        "price_reaction",
        "volume",
        "volatility",
        "liquidity",
        "reporting",
    ]
    assert score > 18
    assert confidence > 0.5
    assert factors[-1]["available"] is True
    assert factors[-1]["contribution"] > 0


def test_hybrid_assessment_is_not_an_llm_only_signal() -> None:
    assessment = build_assessment(
        "SBER",
        market_snapshot(),
        [reporting_news()],
        signal_record(),
    )

    assert assessment["assessment_type"] == "hybrid"
    assert assessment["bias_direction"] == "up"
    assert assessment["news_signal"]["id"] == "sig_test"
    codes = {factor["code"] for factor in assessment["factor_contributions"]}
    assert {
        "news_signal",
        "price_reaction",
        "volume",
        "volatility",
        "liquidity",
        "reporting",
    } == codes


def test_eval_and_demo_account_include_costs() -> None:
    entry = datetime(2026, 8, 3, 7, 10, tzinfo=UTC)
    candles = [
        {
            "begin": (entry + offset).isoformat().replace("+00:00", "Z"),
            "open": price,
            "close": price,
        }
        for offset, price in (
            (timedelta(), 100.0),
            (timedelta(hours=1), 101.0),
            (timedelta(days=1), 103.0),
            (timedelta(days=3), 106.0),
        )
    ]

    outcome = evaluate_signal(signal_record(), candles, reporting_news())
    summary = eval_summary([outcome])
    account = demo_account([outcome])

    assert outcome["returns"] == {"1h": 1.0, "1d": 3.0, "3d": 6.0}
    assert outcome["verdict"] is True
    assert summary["hit_rate_pct"] == 100.0
    assert account["closed_trades"] == 1
    assert account["total_commission_rub"] > 0
    assert 0 < account["net_return_pct"] < 0.6
