from dataclasses import replace
from datetime import UTC, datetime, timedelta

from eventedge.evals import (
    build_assessment,
    deduplicate_eval_events,
    deduplicate_eval_signals,
    eval_breakdowns,
    eval_quality_series,
    eval_relationships,
    eval_summary,
    evaluate_signal,
    event_time_export_rows,
    outcome_export_rows,
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
        model_version="news-baseline-0.2.0",
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


def test_generic_market_digest_does_not_leak_reporting_score_between_companies() -> None:
    timestamp = datetime(2026, 8, 3, 11, tzinfo=UTC)
    digest = NewsRecord(
        id="news_digest",
        source_id="telegram_bcs_express",
        external_id="digest-1",
        published_at=timestamp,
        received_at=timestamp,
        title="Главное к открытию вторника",
        url="https://example.com/digest",
        content=(
            "ЛУКОЙЛ торгуется без заметных корпоративных событий. "
            "Сбербанк опубликовал отчётность: чистая прибыль выросла и превысила ожидания."
        ),
        language="ru",
        source_metadata={"tickers": ["LKOH", "SBER"]},
        created_at=timestamp,
    )

    lkoh_factors, _, _ = quant_factors("LKOH", market_snapshot(), [digest])
    sber_factors, _, _ = quant_factors("SBER", market_snapshot(), [digest])

    assert lkoh_factors[-1]["available"] is False
    assert lkoh_factors[-1]["contribution"] == 0
    assert sber_factors[-1]["available"] is True
    assert sber_factors[-1]["contribution"] > 0


def test_quant_bias_without_directional_news_stays_neutral() -> None:
    assessment = build_assessment("SBER", market_snapshot(), [reporting_news()], None)

    assert assessment["score"] > 18
    assert assessment["bias_direction"] == "up"
    assert assessment["direction"] == "neutral"
    assert assessment["action"] == "no_action"


def test_neutral_news_does_not_turn_quant_bias_into_green_signal() -> None:
    neutral = replace(
        signal_record(),
        direction="neutral",
        action="no_action",
        score=0,
    )

    assessment = build_assessment("SBER", market_snapshot(), [reporting_news()], neutral)

    assert assessment["bias_direction"] == "up"
    assert assessment["direction"] == "neutral"
    assert assessment["action"] == "no_action"


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


def test_eval_analytics_and_exports_preserve_signal_outcomes() -> None:
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
    breakdowns = eval_breakdowns([outcome])
    relationships = eval_relationships([outcome])
    quality_series = eval_quality_series([outcome])
    outcome_rows = outcome_export_rows([outcome])
    timeseries_rows, truncated = event_time_export_rows(
        [signal_record()],
        {"SBER": candles},
        {"news_report": reporting_news()},
    )

    assert outcome["returns"] == {"1h": 1.0, "4h": 3.0, "1d": 3.0, "3d": 6.0}
    assert outcome["verdict"] is True
    assert summary["hit_rate_pct"] == 100.0
    assert summary["median_signed_return_pct"] == 3.0
    assert breakdowns["by_horizon"][0]["hit_rate_pct"] == 100.0
    assert relationships[0]["value"] is None
    assert quality_series[-1]["cumulative_hit_rate_pct"] == 100.0
    assert outcome_rows[0]["return_3d_pct"] == 6.0
    assert outcome_rows[0]["return_4h_pct"] == 3.0
    assert timeseries_rows[-1]["offset_minutes"] == 3 * 24 * 60
    assert timeseries_rows[-1]["signed_return_pct"] == 6.0
    assert truncated is False


def test_eval_breakdowns_have_only_directional_signal_groups() -> None:
    outcomes = [
        {"direction": "up", "ticker": "SBER", "returns": {"4h": 1.0}},
        {"direction": "down", "ticker": "LKOH", "returns": {"4h": -1.0}},
    ]

    breakdowns = eval_breakdowns(outcomes)

    assert [item["direction"] for item in breakdowns["by_direction"]] == ["up", "down"]


def test_eval_summary_separates_partial_results_from_pending_signals() -> None:
    outcomes = [
        {
            "status": "evaluated",
            "direction": "up",
            "returns": {"1h": 0.4, "1d": 1.2, "3d": 2.1},
            "verdict": True,
        },
        {
            "status": "partial",
            "direction": "down",
            "returns": {"1h": -0.3, "1d": None, "3d": None},
            "verdict": True,
        },
        {
            "status": "partial",
            "direction": "up",
            "returns": {"1h": None, "1d": None, "3d": None},
            "verdict": None,
        },
        {
            "status": "unavailable",
            "direction": "up",
            "returns": {"1h": None, "1d": None, "3d": None},
            "verdict": None,
        },
    ]

    summary = eval_summary(outcomes)

    assert summary["signals_total"] == 4
    assert summary["evaluated"] == 2
    assert summary["complete"] == 1
    assert summary["partial"] == 1
    assert summary["pending"] == 1
    assert summary["unavailable"] == 1
    assert summary["coverage_pct"] == 50.0


def test_eval_counts_identical_decision_once_across_corroborating_news() -> None:
    first = signal_record()
    corroboration = replace(
        first,
        id="sig_corroboration",
        news_id="news_corroboration",
        confidence=first.confidence + 0.05,
        created_at=first.created_at + timedelta(minutes=1),
    )
    different_decision = replace(
        first,
        id="sig_different",
        news_id="news_different",
        score=41.0,
        created_at=first.created_at + timedelta(minutes=2),
    )

    result = deduplicate_eval_signals([first, corroboration, different_decision])

    assert len(result) == 2
    assert {signal.id for signal in result} == {"sig_corroboration", "sig_different"}


def test_eval_counts_one_market_event_and_prefers_latest_model() -> None:
    published = datetime(2026, 8, 6, 9, tzinfo=UTC)
    first_news = replace(
        reporting_news(),
        id="news_first",
        published_at=published,
        received_at=published + timedelta(minutes=1),
        title="Совет директоров Сбербанка рекомендовал дивиденды за полугодие",
    )
    corroborating_news = replace(
        reporting_news(),
        id="news_second",
        published_at=published + timedelta(minutes=20),
        received_at=published + timedelta(minutes=21),
        title="Сбербанк рекомендовал дивиденды за первое полугодие",
    )
    old_model = replace(
        signal_record(),
        id="sig_old",
        news_id=first_news.id,
        as_of=first_news.received_at,
        model_version="news-baseline-0.1.1",
    )
    current_model_first = replace(
        old_model,
        id="sig_current_first",
        model_version="news-baseline-0.2.0",
    )
    current_model_corroboration = replace(
        current_model_first,
        id="sig_current_second",
        news_id=corroborating_news.id,
        as_of=corroborating_news.received_at,
        confidence=0.95,
    )

    result = deduplicate_eval_events(
        [old_model, current_model_corroboration, current_model_first],
        {first_news.id: first_news, corroborating_news.id: corroborating_news},
    )

    assert [signal.id for signal in result] == ["sig_current_first"]

    epoch_result = deduplicate_eval_events(
        [old_model, current_model_corroboration, current_model_first],
        {first_news.id: first_news, corroborating_news.id: corroborating_news},
        preserve_model_epochs=True,
    )

    assert {signal.id for signal in epoch_result} == {"sig_old", "sig_current_first"}
