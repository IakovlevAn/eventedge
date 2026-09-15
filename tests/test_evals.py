from dataclasses import replace
from datetime import UTC, datetime, timedelta

from eventedge.evals import (
    EVALUATION_METHODOLOGY_VERSION,
    build_assessment,
    deduplicate_eval_events,
    deduplicate_eval_outcomes,
    deduplicate_eval_signals,
    eval_breakdowns,
    eval_quality_series,
    eval_relationships,
    eval_summary,
    evaluate_signal,
    event_time_export_rows,
    normalize_neutral_eval_outcome,
    outcome_export_rows,
    quant_factors,
    same_eval_event,
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
    timestamp = datetime(2026, 8, 3, 7, tzinfo=UTC)
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
    assert assessment["config_version"] == 3
    assert assessment["market_context"]["is_signal"] is False
    assert assessment["market_context"]["bias_direction"] == "up"
    assert assessment["market_context"]["score"] != assessment["score"]
    assert assessment["market_context"]["as_of"] == assessment["as_of"]
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

    assert outcome["returns"] == {"1h": 1.0, "4h": None, "1d": 3.0, "3d": 6.0}
    assert outcome["evaluation_methodology"] == EVALUATION_METHODOLOGY_VERSION
    assert outcome["eligibility"]["eligible"] is True
    assert outcome["entry"]["delay_seconds"] == 600
    assert outcome["horizon_observations"]["4h"]["timely"] is False
    assert outcome["verdict"] is True
    assert summary["metric_scope"] == "live"
    assert summary["hit_rate_pct"] == 100.0
    assert summary["median_signed_return_pct"] == 1.0
    assert breakdowns["by_horizon"][0]["hit_rate_pct"] == 100.0
    assert relationships[0]["value"] is None
    assert quality_series[-1]["cumulative_hit_rate_pct"] == 100.0
    assert outcome_rows[0]["return_3d_pct"] == 6.0
    assert outcome_rows[0]["return_4h_pct"] is None
    assert outcome_rows[0]["eligibility_cohort"] == "live"
    assert outcome_rows[0]["evaluation_anchor"] == "live_decision"
    assert outcome_rows[0]["verdict_status"] == "evaluated"
    assert outcome_rows[0]["outcome_terminal"] is True
    assert timeseries_rows[-1]["offset_minutes"] == 3 * 24 * 60
    assert timeseries_rows[-1]["signed_return_pct"] == 6.0
    assert timeseries_rows[-1]["eligibility_cohort"] == "live"
    assert timeseries_rows[-1]["evaluation_anchor"] == "live_decision"
    assert truncated is False


def test_retrospective_signal_is_retained_as_research_cohort() -> None:
    signal = replace(
        signal_record(),
        created_at=signal_record().as_of + timedelta(days=1),
    )
    news = replace(
        reporting_news(),
        created_at=signal.created_at,
    )
    entry = news.received_at + timedelta(minutes=10)
    outcome = evaluate_signal(
        signal,
        candles := [
            {"begin": entry.isoformat(), "open": 100, "close": 100},
            {
                "begin": (entry + timedelta(hours=4)).isoformat(),
                "open": 102,
                "close": 102,
            },
            {
                "begin": (entry + timedelta(days=3)).isoformat(),
                "open": 103,
                "close": 103,
            },
        ],
        news,
    )
    timeseries, truncated = event_time_export_rows(
        [signal],
        {signal.ticker: candles},
        {news.id: news},
    )
    summary = eval_summary([outcome])

    assert outcome["status"] == "evaluated"
    assert outcome["eligibility"]["reason"] == "retrospective_signal"
    assert outcome["eligibility"]["cohort"] == "retrospective"
    assert outcome["eligibility"]["evaluation_anchor"] == "evidence_cutoff_replay"
    assert outcome["eligibility"]["decision_at"] == news.received_at.isoformat().replace(
        "+00:00", "Z"
    )
    assert outcome["eligibility"]["live_decision_at"] == signal.created_at.isoformat().replace(
        "+00:00", "Z"
    )
    assert outcome["entry"]["at"] == entry.isoformat().replace("+00:00", "Z")
    assert outcome["verdict"] is True
    assert outcome["verdict_status"] == "evaluated"
    assert summary["signals_total"] == 1
    assert summary["eligible"] == 0
    assert summary["excluded"] == 1
    assert summary["live_eligible"] == 0
    assert summary["research_only"] == 1
    assert summary["evaluated"] == 1
    assert summary["exclusion_reasons"] == {"retrospective_signal": 1}
    assert summary["metric_scope"] == "live"
    assert summary["coverage_pct"] == 0.0
    assert summary["hit_rate_pct"] is None
    assert summary["cohorts"]["all"]["coverage_pct"] == 100.0
    assert summary["cohorts"]["retrospective"]["hit_rate_pct"] == 100.0
    assert truncated is False
    assert timeseries
    assert {row["signal_id"] for row in timeseries} == {signal.id}
    assert {row["eligibility_cohort"] for row in timeseries} == {"retrospective"}
    assert {row["evaluation_eligible"] for row in timeseries} == {False}
    assert {row["exclusion_reason"] for row in timeseries} == {"retrospective_signal"}


def test_eval_summary_top_level_quality_aliases_use_live_cohort() -> None:
    live = {
        "signal_id": "sig_live",
        "status": "evaluated",
        "direction": "up",
        "returns": {"4h": 1.0},
        "verdict": True,
        "eligibility": {"eligible": True, "reason": None, "cohort": "live"},
    }
    retrospective = {
        "signal_id": "sig_research",
        "status": "evaluated",
        "direction": "up",
        "returns": {"4h": -3.0},
        "verdict": False,
        "eligibility": {
            "eligible": False,
            "reason": "retrospective_signal",
            "cohort": "retrospective",
        },
    }
    neutral = {
        "signal_id": "sig_neutral_abstention",
        "status": "evaluated",
        "direction": "neutral",
        "returns": {"4h": 8.0},
        "verdict": None,
        "verdict_status": "not_applicable",
        "eligibility": {"eligible": True, "reason": None, "cohort": "live"},
    }

    summary = eval_summary([live, retrospective, neutral])

    assert summary["signals_total"] == 3
    assert summary["evaluated"] == 2
    assert summary["directional_signals"] == 2
    assert summary["neutral_signals"] == 1
    assert summary["not_applicable"] == 1
    assert summary["quality_methodology"] == "directional_up_down_only_v1"
    assert summary["metric_scope"] == "live"
    assert summary["hit_rate_pct"] == 100.0
    assert summary["average_signed_return_pct"] == 1.0
    assert summary["median_signed_return_pct"] == 1.0
    assert summary["coverage_pct"] == 100.0
    assert summary["cohorts"]["all"]["hit_rate_pct"] == 50.0
    assert summary["cohorts"]["all"]["average_signed_return_pct"] == -1.0
    assert summary["cohorts"]["all"]["directional_signals"] == 2
    assert summary["cohorts"]["live"]["directional_signals"] == 1
    assert summary["cohorts"]["live"]["neutral_signals"] == 1
    assert summary["cohorts"]["retrospective"]["hit_rate_pct"] == 0.0


def test_neutral_signal_is_abstention_not_flat_price_prediction() -> None:
    signal = replace(
        signal_record(),
        direction="neutral",
        action="no_action",
        score=0.0,
    )
    entry = signal.created_at + timedelta(minutes=10)
    outcome = evaluate_signal(
        signal,
        [
            {"begin": entry.isoformat(), "open": 100, "close": 100},
            {
                "begin": (entry + timedelta(hours=4)).isoformat(),
                "open": 112,
                "close": 112,
            },
            {
                "begin": (entry + timedelta(days=3)).isoformat(),
                "open": 115,
                "close": 115,
            },
        ],
        reporting_news(),
    )

    assert outcome["returns"]["4h"] == 12.0
    assert outcome["verdict"] is None
    assert outcome["verdict_status"] == "not_applicable"
    assert outcome["neutral_move_status"] == "material_move"
    summary = eval_summary([outcome])
    assert summary["directional_signals"] == 0
    assert summary["neutral_signals"] == 1
    assert summary["evaluated"] == 0
    assert summary["hit_rate_pct"] is None
    assert summary["coverage_pct"] == 0.0


def test_legacy_neutral_outcome_normalizes_without_changing_market_data() -> None:
    legacy = {
        "signal_id": "sig_legacy_neutral",
        "direction": "neutral",
        "returns": {"1h": 0.2, "4h": 0.3, "1d": 0.4, "3d": 0.5},
        "verdict": True,
        "verdict_status": "evaluated",
        "raw_observation_count": 42,
    }

    normalized = normalize_neutral_eval_outcome(legacy)

    assert normalized["verdict"] is None
    assert normalized["verdict_status"] == "not_applicable"
    assert normalized["neutral_move_status"] == "quiet"
    assert normalized["returns"] == legacy["returns"]
    assert normalized["raw_observation_count"] == 42


def test_eval_never_uses_evidence_received_after_signal_creation() -> None:
    signal = signal_record()
    future_evidence = replace(
        reporting_news(),
        received_at=signal.created_at + timedelta(seconds=1),
    )

    entry = signal.created_at + timedelta(minutes=10)
    outcome = evaluate_signal(
        signal,
        [
            {"begin": entry.isoformat(), "open": 100, "close": 100},
            {
                "begin": (entry + timedelta(hours=4)).isoformat(),
                "open": 101,
                "close": 101,
            },
        ],
        future_evidence,
    )

    assert outcome["status"] == "partial"
    assert outcome["eligibility"]["reason"] == "evidence_received_after_signal"
    assert outcome["eligibility"]["cohort"] == "retrospective"
    assert outcome["verdict"] is True
    assert outcome["verdict_status"] == "evaluated"


def test_horizon_return_requires_a_timely_market_observation() -> None:
    signal = signal_record()
    entry = signal.created_at + timedelta(minutes=10)
    candles = [
        {"begin": entry.isoformat(), "open": 100, "close": 100},
        {
            "begin": (entry + timedelta(hours=4, minutes=20)).isoformat(),
            "open": 102,
            "close": 103,
        },
        {
            "begin": (entry + timedelta(days=1)).isoformat(),
            "open": 104,
            "close": 105,
        },
    ]

    outcome = evaluate_signal(signal, candles, reporting_news())

    assert outcome["returns"]["4h"] == 2.0
    assert outcome["horizon_observations"]["4h"] == {
        "target_at": (entry + timedelta(hours=4)).isoformat().replace("+00:00", "Z"),
        "observed_at": (entry + timedelta(hours=4, minutes=20))
        .isoformat()
        .replace("+00:00", "Z"),
        "delay_seconds": 1200,
        "timely": True,
        "price_field": "open",
    }
    assert outcome["returns"]["1h"] is None
    assert outcome["horizon_observations"]["1h"]["timely"] is False


def test_eval_verdict_status_distinguishes_pending_from_missed_window() -> None:
    recent_at = datetime.now(UTC).replace(microsecond=0)
    recent_news = replace(
        reporting_news(),
        published_at=recent_at,
        received_at=recent_at,
        created_at=recent_at,
    )
    recent_signal = replace(
        signal_record(),
        as_of=recent_at,
        data_cutoff_at=recent_at,
        created_at=recent_at,
    )
    recent_entry = recent_at + timedelta(minutes=10)
    pending = evaluate_signal(
        recent_signal,
        [{"begin": recent_entry.isoformat(), "open": 100, "close": 100}],
        recent_news,
    )

    old_signal = signal_record()
    old_entry = old_signal.created_at + timedelta(minutes=10)
    missed = evaluate_signal(
        old_signal,
        [{"begin": old_entry.isoformat(), "open": 100, "close": 100}],
        reporting_news(),
    )

    assert pending["status"] == "partial"
    assert pending["verdict_status"] == "pending"
    assert pending["outcome_terminal"] is False
    assert missed["status"] == "partial"
    assert missed["verdict_status"] == "missed_window"
    assert missed["outcome_terminal"] is True


def test_eval_entry_is_after_the_signal_was_actually_created() -> None:
    signal = replace(
        signal_record(),
        created_at=signal_record().as_of + timedelta(minutes=5),
    )
    candles = [
        {"begin": signal.as_of.isoformat(), "open": 90, "close": 100},
        {
            "begin": (signal.as_of + timedelta(minutes=10)).isoformat(),
            "open": 110,
            "close": 111,
        },
    ]

    outcome = evaluate_signal(signal, candles, reporting_news())

    assert outcome["eligibility"]["decision_at"] == signal.created_at.isoformat().replace(
        "+00:00", "Z"
    )
    assert outcome["eligibility"]["live_decision_at"] == signal.created_at.isoformat().replace(
        "+00:00", "Z"
    )
    assert outcome["eligibility"]["evaluation_anchor"] == "live_decision"
    assert outcome["entry"] == {
        "at": (signal.as_of + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
        "price": 110.0,
        "delay_seconds": 300,
    }


def test_eval_uses_first_tradable_candle_after_a_long_holiday() -> None:
    decision_at = datetime(2026, 1, 1, 7, tzinfo=UTC)
    news = replace(
        reporting_news(),
        published_at=decision_at,
        received_at=decision_at,
        created_at=decision_at,
    )
    signal = replace(
        signal_record(),
        as_of=decision_at,
        data_cutoff_at=decision_at,
        created_at=decision_at,
    )
    entry = decision_at + timedelta(days=4, minutes=10)
    candles = [
        {"begin": entry.isoformat(), "open": 100, "close": 100},
        {
            "begin": (entry + timedelta(hours=1)).isoformat(),
            "open": 101,
            "close": 101,
        },
        {
            "begin": (entry + timedelta(hours=4)).isoformat(),
            "open": 102,
            "close": 102,
        },
        {
            "begin": (entry + timedelta(days=3)).isoformat(),
            "open": 103,
            "close": 103,
        },
    ]

    outcome = evaluate_signal(signal, candles, news)
    raw, truncated = event_time_export_rows(
        [signal],
        {signal.ticker: candles},
        {news.id: news},
    )

    assert outcome["entry"]["at"] == entry.isoformat().replace("+00:00", "Z")
    assert outcome["entry"]["delay_seconds"] == 4 * 24 * 60 * 60 + 10 * 60
    assert outcome["returns"]["1h"] == 1.0
    assert outcome["returns"]["4h"] == 2.0
    assert outcome["returns"]["3d"] == 3.0
    assert truncated is False
    assert len(raw) == len(candles)
    assert raw[0]["entry_at"] == entry.isoformat().replace("+00:00", "Z")


def test_eval_breakdowns_cover_every_signal_direction() -> None:
    outcomes = [
        {"direction": "up", "ticker": "SBER", "returns": {"4h": 1.0}},
        {"direction": "down", "ticker": "LKOH", "returns": {"4h": -1.0}},
        {"direction": "neutral", "ticker": "YDEX", "returns": {"4h": 12.0}},
    ]

    breakdowns = eval_breakdowns(outcomes)

    assert [item["direction"] for item in breakdowns["by_direction"]] == [
        "up",
        "down",
        "neutral",
    ]
    neutral = breakdowns["by_direction"][-1]
    assert neutral["signals"] == 1
    assert neutral["observations"] == 0
    assert neutral["hit_rate_pct"] is None


def test_eval_summary_separates_partial_results_from_pending_signals() -> None:
    outcomes = [
        {
            "status": "evaluated",
            "direction": "up",
            "returns": {"1h": 0.4, "1d": 1.2, "3d": 2.1},
            "verdict": True,
            "eligibility": {"eligible": True, "reason": None, "cohort": "live"},
        },
        {
            "status": "partial",
            "direction": "down",
            "returns": {"1h": -0.3, "1d": None, "3d": None},
            "verdict": True,
            "eligibility": {"eligible": True, "reason": None, "cohort": "live"},
        },
        {
            "status": "partial",
            "direction": "up",
            "returns": {"1h": None, "1d": None, "3d": None},
            "verdict": None,
            "eligibility": {"eligible": True, "reason": None, "cohort": "live"},
        },
        {
            "status": "unavailable",
            "direction": "up",
            "returns": {"1h": None, "1d": None, "3d": None},
            "verdict": None,
            "eligibility": {"eligible": True, "reason": None, "cohort": "live"},
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


def test_eval_event_matcher_normalizes_sanctions_wave_without_merging_other_context() -> None:
    published = datetime(2026, 9, 15, 8, tzinfo=UTC)
    first = replace(
        reporting_news(),
        id="news_vtbr_iran_first",
        published_at=published,
        received_at=published + timedelta(minutes=1),
        title="США ввели новые санкции против ВТБ за помощь Ирану - Голос Америки",
    )
    corroboration = replace(
        first,
        id="news_vtbr_iran_second",
        published_at=published + timedelta(minutes=8),
        received_at=published + timedelta(minutes=9),
        title="США внесли ВТБ в санкционный список по Ирану - meduza.io",
    )
    different_context = replace(
        first,
        id="news_vtbr_china",
        published_at=published + timedelta(minutes=10),
        received_at=published + timedelta(minutes=11),
        title="США ввели санкции против ВТБ за операции с Китаем - example.com",
    )
    late_repeat = replace(
        corroboration,
        id="news_vtbr_iran_late",
        published_at=published + timedelta(hours=73),
        received_at=published + timedelta(hours=73, minutes=1),
    )

    assert same_eval_event(first, corroboration) is True
    assert same_eval_event(first, different_context) is False
    assert same_eval_event(first, late_repeat) is False


def test_directional_eval_outcomes_collapse_screenshot_wave_and_keep_provenance() -> None:
    published = datetime(2026, 9, 15, 8, tzinfo=UTC)
    publications = [
        ("США внесли ВТБ в санкционный список по Ирану", "tass"),
        ("США внесли ВТБ в санкционный список по Ирану", "rbc"),
        ("США включили банк ВТБ в антииранский санкционный список - finam.ru", "google_news"),
        ("Минфин США внес ВТБ в иранские санкционные списки - interfax.ru", "google_news"),
        ("Минфин США внес ВТБ в иранские санкционные списки", "interfax"),
        ("🇺🇸🇷🇺Минфин США внес ВТБ в иранские санкционные списки", "telegram_interfaxonline"),
        ("⚡️Минфин США внес ВТБ в санкционный список по Ирану — ТАСС", "telegram_bbbreaking"),
        ("США внесли ВТБ в санкционный список по Ирану - meduza.io", "google_news"),
        ("США внесли ВТБ в санкционный список по Ирану - business-gazeta.ru", "google_news"),
        ("⚡️Минфин США внёс ВТБ в санкционный список по Ирану — ТАСС", "telegram_ru2ch"),
        ("США расширили санкции против ВТБ из-за операций с Ираном - bfm.ru", "google_news"),
        (
            "США ввели новые санкции против ВТБ за помощь в обходе санкций "
            "против Ирана - ru.themoscowtimes.com",
            "google_news",
        ),
        ("США внесли российский ВТБ в иранский санкционный список - DW.com", "google_news"),
        ("США внесли ВТБ в санкционный список по Ирану - meduza.io", "google_news"),
        ("США расширили санкции против ВТБ за помощь Ирану - frankmedia.ru", "google_news"),
        ("США ввели новые санкции против ВТБ за помощь Ирану - Голос Америки", "google_news"),
    ]
    outcomes = []
    for index, (title, source_id) in enumerate(publications):
        at = published + timedelta(minutes=index)
        outcomes.append(
            {
                "signal_id": f"sig_vtbr_{index}",
                "ticker": "VTBR",
                "as_of": at.isoformat().replace("+00:00", "Z"),
                "direction": "down",
                "score": -48.8,
                "news": {
                    "id": f"news_vtbr_{index}",
                    "title": title,
                    "source_id": source_id,
                    "url": f"https://example.com/news/{index}",
                    "published_at": at.isoformat().replace("+00:00", "Z"),
                },
            }
        )
    unrelated = {
        **outcomes[-1],
        "signal_id": "sig_vtbr_china",
        "as_of": (published + timedelta(minutes=9)).isoformat().replace("+00:00", "Z"),
        "news": {
            **outcomes[-1]["news"],
            "id": "news_vtbr_china",
            "title": "США ввели санкции против ВТБ за операции с Китаем - example.com",
            "published_at": (published + timedelta(minutes=9))
            .isoformat()
            .replace("+00:00", "Z"),
        },
    }

    result = deduplicate_eval_outcomes([*reversed(outcomes), unrelated])

    assert len(result) == 2
    iran_event = next(item for item in result if item["signal_id"] == "sig_vtbr_0")
    assert iran_event["event_signal_count"] == 16
    assert iran_event["event_publication_count"] == 16
    assert iran_event["event_source_count"] == 14
    assert iran_event["event_signal_ids"] == [f"sig_vtbr_{index}" for index in range(16)]
    assert len(iran_event["event_publications"]) == 16
    assert outcomes[0].get("event_signal_count") is None


def test_directional_eval_outcome_bridge_cannot_merge_conflicting_contexts() -> None:
    published = datetime(2026, 9, 15, 8, tzinfo=UTC)

    def outcome(signal_id: str, title: str, minute: int) -> dict[str, object]:
        at = published + timedelta(minutes=minute)
        return {
            "signal_id": signal_id,
            "ticker": "VTBR",
            "as_of": at.isoformat().replace("+00:00", "Z"),
            "news": {
                "id": f"news_{signal_id}",
                "title": title,
                "source_id": "google_news",
                "url": f"https://example.com/{signal_id}",
                "published_at": at.isoformat().replace("+00:00", "Z"),
            },
        }

    iran = outcome(
        "iran",
        "США ввели санкции против ВТБ за операции с Ираном - iran.example.com",
        0,
    )
    unspecified = outcome(
        "unspecified",
        "США ввели санкции против ВТБ - wire.example.com",
        1,
    )
    china = outcome(
        "china",
        "США ввели санкции против ВТБ за операции с Китаем - china.example.com",
        2,
    )

    result = deduplicate_eval_outcomes([china, unspecified, iran])

    assert len(result) == 2
    assert {tuple(item["event_signal_ids"]) for item in result} == {
        ("iran", "unspecified"),
        ("china",),
    }


def test_directional_eval_event_prefers_earliest_decision_for_same_publication() -> None:
    published = "2026-09-15T08:00:00Z"
    later = {
        "signal_id": "sig_a_later",
        "ticker": "VTBR",
        "as_of": "2026-09-15T08:10:00Z",
        "eligibility": {"decision_at": "2026-09-15T08:10:00Z"},
        "news": {
            "id": "news_shared",
            "title": "США внесли ВТБ в санкционный список по Ирану",
            "source_id": "tass",
            "url": "https://example.com/shared",
            "published_at": published,
        },
    }
    earlier = {
        **later,
        "signal_id": "sig_z_earlier",
        "as_of": "2026-09-15T08:05:00Z",
        "eligibility": {"decision_at": "2026-09-15T08:05:00Z"},
    }

    result = deduplicate_eval_outcomes([later, earlier])

    assert len(result) == 1
    assert result[0]["signal_id"] == "sig_z_earlier"
    assert result[0]["event_signal_ids"] == ["sig_z_earlier", "sig_a_later"]


def test_directional_eval_event_does_not_prefer_an_older_late_backfill() -> None:
    backfill = {
        "signal_id": "sig_backfill",
        "ticker": "VTBR",
        "as_of": "2026-09-15T10:00:00Z",
        "eligibility": {"decision_at": "2026-09-15T10:00:00Z"},
        "news": {
            "id": "news_backfill",
            "title": "США внесли ВТБ в санкционный список по Ирану",
            "source_id": "archive",
            "url": "https://example.com/backfill",
            "published_at": "2026-09-15T07:00:00Z",
        },
    }
    live = {
        **backfill,
        "signal_id": "sig_live",
        "as_of": "2026-09-15T08:01:00Z",
        "eligibility": {"decision_at": "2026-09-15T08:01:00Z"},
        "news": {
            **backfill["news"],
            "id": "news_live",
            "source_id": "tass",
            "url": "https://example.com/live",
            "published_at": "2026-09-15T08:00:00Z",
        },
    }

    result = deduplicate_eval_outcomes([backfill, live])

    assert len(result) == 1
    assert result[0]["signal_id"] == "sig_live"
