import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import ydb

import eventedge.storage as storage_module
from eventedge.analysis import (
    EventType,
    InstrumentMention,
    NewsAnalysisInput,
    RuleBasedNewsExtractor,
    SemanticFeatures,
    TemporalStatus,
)
from eventedge.storage import (
    DELETE_EXPIRED_ASSESSMENT_SNAPSHOTS_QUERY,
    INSERT_ASSESSMENT_SNAPSHOT_QUERY,
    SCHEMA_STATEMENTS,
    SCHEMA_TABLE_NAMES,
    SELECT_ASSESSMENT_SNAPSHOT_QUERY,
    SELECT_EVALUATION_EPOCHS_QUERY,
    SELECT_EVALUATION_SIGNALS_QUERY,
    SELECT_MATERIALITY_PREDICTIONS_BY_MODEL_QUERY,
    SELECT_MATERIALITY_PREDICTIONS_QUERY,
    UPSERT_MATERIALITY_PREDICTION_QUERY,
    EvaluationEpochRecord,
    MaterialityPredictionRecord,
    MemoryNewsRepository,
    NewsDocument,
    NewsRecord,
    YdbNewsRepository,
    _backfill_evaluation_observations_from_epochs,
    count_evaluation_observations_by_signal_query,
    deduplicate_signals,
    encode_evaluation_observation_cursor,
    evaluation_observation_bulk_upsert_rows,
    evaluation_observation_page_query,
    evaluation_observation_upsert_batches,
    filter_signals,
    materiality_prediction_from_row,
    materiality_prediction_parameters,
    migrate_ydb_schema,
    normalize_signal_freshness,
    process_document,
    select_signals_query,
    signal_from_row,
    signal_rejection_reason,
    stable_id,
)


def _materiality_prediction(
    *,
    prediction_id: str = "mat_example",
    news_id: str = "news_example",
    model_version: str = "update-5m-symmetric-materiality-logistic-1.0",
) -> MaterialityPredictionRecord:
    timestamp = datetime(2026, 9, 10, 9, 5, tzinfo=UTC)
    return MaterialityPredictionRecord(
        id=prediction_id,
        news_id=news_id,
        ticker="SBER",
        decision_at=timestamp,
        data_cutoff_at=timestamp,
        status="ready",
        reason=None,
        raw_probability=0.7,
        calibrated_probability=0.8,
        selected_at_coverage_pct={"raw": (20, 50, 100), "calibrated": (20, 50, 100)},
        eligible_for_ranking=True,
        missing_features=(),
        model_id="update_5m_symmetric_materiality_logistic",
        model_version=model_version,
        artifact_payload_sha256="a" * 64,
        feature_schema_version="news-materiality-runtime-features-1.0",
        created_at=timestamp,
        updated_at=timestamp,
    )


def test_memory_repository_attaches_idempotent_materiality_predictions() -> None:
    async def scenario() -> None:
        repository = MemoryNewsRepository()
        timestamp = datetime(2026, 9, 10, 9, tzinfo=UTC)
        document = NewsDocument(
            source_id="telegram_markettwits",
            external_id="materiality-example",
            published_at=timestamp,
            received_at=timestamp,
            title="Сбербанк сообщил о новом событии",
            url="https://example.com/materiality",
            content="Сбербанк сообщил о существенном событии.",
            language="ru",
            source_metadata={"tickers": ["SBER"], "analysis_candidate": True},
            payload_hash="materiality-payload",
        )
        await repository.ingest("materiality-ingest", document)
        news = await repository.list_news(source_id=None, limit=10)
        prediction = _materiality_prediction(news_id=news[0].id)

        await repository.upsert_materiality_prediction(prediction)
        await repository.upsert_materiality_prediction(prediction)
        attached = await repository.list_news(source_id=None, limit=10)
        selected = await repository.list_materiality_predictions(
            news_ids=frozenset({news[0].id}),
            model_version=prediction.model_version,
        )

        assert attached[0].materiality_predictions == (prediction,)
        assert selected == [prediction]

    asyncio.run(scenario())


def test_materiality_prediction_ydb_serialization_round_trip() -> None:
    prediction = replace(
        _materiality_prediction(),
        outcome_4h={
            "status": "evaluated",
            "actual_material": True,
            "predicted_material": True,
            "verdict": True,
        },
    )
    parameters = materiality_prediction_parameters(prediction)
    payload = json.loads(str(parameters["$payload"].value))
    row = SimpleNamespace(
        prediction_id=prediction.id,
        news_id=prediction.news_id,
        ticker=prediction.ticker,
        decision_at=prediction.decision_at,
        data_cutoff_at=prediction.data_cutoff_at,
        status=prediction.status,
        model_id=prediction.model_id,
        model_version=prediction.model_version,
        artifact_payload_sha256=prediction.artifact_payload_sha256,
        payload=json.dumps(payload),
        created_at=prediction.created_at,
        updated_at=prediction.updated_at,
    )

    assert materiality_prediction_from_row(row) == prediction
    assert payload["outcome_4h"]["verdict"] is True
    assert "UPSERT INTO `materiality_predictions`" in UPSERT_MATERIALITY_PREDICTION_QUERY
    assert "news_id IN $news_ids" in SELECT_MATERIALITY_PREDICTIONS_QUERY
    assert "model_version = $model_version" in SELECT_MATERIALITY_PREDICTIONS_BY_MODEL_QUERY
    materiality_schema = next(
        statement
        for statement in SCHEMA_STATEMENTS
        if "CREATE TABLE IF NOT EXISTS `materiality_predictions`" in statement
    )
    assert "`news_id`, `model_version`, `artifact_payload_sha256`" in materiality_schema
    assert "`ticker`, `decision_at`" in materiality_schema


def test_ydb_news_reads_materiality_ledger_only_when_enabled() -> None:
    timestamp = datetime(2026, 9, 10, 9, tzinfo=UTC)
    row = SimpleNamespace(
        news_id="news_example",
        source_id="telegram_markettwits",
        external_id="example",
        published_at=timestamp,
        received_at=timestamp,
        title="Сбербанк сообщил о событии",
        url="https://example.com/news",
        content="Сбербанк сообщил о существенном событии.",
        language="ru",
        source_metadata=json.dumps(
            {"analysis_candidate": True, "tickers": ["SBER"]}
        ),
        created_at=timestamp,
    )

    class FakePool:
        def __init__(self) -> None:
            self.queries: list[str] = []

        async def execute_with_retries(
            self,
            query: str,
            parameters: dict[str, object] | None = None,
        ) -> list[SimpleNamespace]:
            self.queries.append(query)
            if "FROM `news_items`" in query:
                return [SimpleNamespace(rows=[row])]
            return [SimpleNamespace(rows=[])]

    async def query_count(include_materiality_predictions: bool) -> int:
        pool = FakePool()
        repository = YdbNewsRepository(
            endpoint="grpcs://localhost:2135",
            database="/local",
            credentials=ydb.AnonymousCredentials(),
            include_materiality_predictions=include_materiality_predictions,
        )
        repository._pool = pool  # type: ignore[assignment]

        records = await repository.list_news(source_id=None, limit=10)

        assert [item.id for item in records] == ["news_example"]
        return len(pool.queries)

    assert asyncio.run(query_count(False)) == 1
    assert asyncio.run(query_count(True)) == 2


def test_evaluation_signal_query_is_not_bounded_by_public_api_limit() -> None:
    assert "FROM `signals`" in SELECT_EVALUATION_SIGNALS_QUERY
    assert "LIMIT" not in SELECT_EVALUATION_SIGNALS_QUERY


def test_evaluation_observations_are_batched_into_idempotent_upserts() -> None:
    observations = [
        {
            "model_version": "signal-engine-0.6.1",
            "config_version": 3,
            "signal_id": f"sig_{index}",
            "observation_at": f"2026-08-31T12:{index:02d}:00Z",
            "close": 100.0 + index,
        }
        for index in range(3)
    ]
    batches = evaluation_observation_upsert_batches(
        observations,
        evaluated_at=datetime(2026, 8, 31, 13, tzinfo=UTC),
        batch_size=2,
    )

    assert len(batches) == 2
    assert all("UPSERT INTO `evaluation_observations`" in query for query, _ in batches)
    assert len(batches[0][1]) == 12
    assert len(batches[1][1]) == 6


def test_evaluation_observation_v2_bulk_rows_are_methodology_scoped_and_deduplicated() -> None:
    evaluated_at = datetime(2026, 9, 1, 10, tzinfo=UTC)
    original = {
        "model_version": "signal-engine-0.6.1",
        "config_version": 3,
        "signal_id": "sig_history",
        "observation_at": "2026-08-31T11:00:00Z",
        "close": 100.0,
    }
    rows = evaluation_observation_bulk_upsert_rows(
        [original, {**original, "close": 101.0}],
        evaluated_at=evaluated_at,
        evaluation_methodology="point-in-time-0.3.0",
    )

    assert len(rows) == 1
    assert rows[0]["evaluation_methodology"] == "point-in-time-0.3.0"
    assert rows[0]["evaluated_at"] == evaluated_at
    assert json.loads(str(rows[0]["payload"])) == {
        **original,
        "close": 101.0,
        "evaluation_methodology": "point-in-time-0.3.0",
    }


def test_evaluation_observation_page_query_filters_and_uses_keyset_cursor() -> None:
    cursor = encode_evaluation_observation_cursor(
        (
            "point-in-time-0.3.0",
            "signal-engine-0.6.1",
            3,
            "sig_001",
            "2026-08-31T11:00:00Z",
        )
    )
    query, parameters, limit = evaluation_observation_page_query(
        evaluation_methodology="point-in-time-0.3.0",
        model_version="signal-engine-0.6.1",
        config_version=3,
        limit=250,
        cursor=cursor,
    )

    assert "FROM `evaluation_observations_v2`" in query
    assert "SELECT COUNT(*) AS total_count" in query
    assert "signal_id > $cursor_signal_id" in query
    assert "observation_at > $cursor_observation_at" in query
    assert "LIMIT 251" in query
    assert parameters["$evaluation_methodology"] == "point-in-time-0.3.0"
    assert parameters["$model_version"] == "signal-engine-0.6.1"
    assert parameters["$cursor_signal_id"] == "sig_001"
    assert limit == 250


def test_evaluation_observation_signal_count_query_is_methodology_scoped() -> None:
    query, parameters = count_evaluation_observations_by_signal_query(
        evaluation_methodology="point-in-time-0.3.0",
        model_version="signal-engine-0.6.1",
        config_version=3,
    )

    assert "FROM `evaluation_observations_v2`" in query
    assert "evaluation_methodology = $evaluation_methodology" in query
    assert "GROUP BY signal_id" in query
    assert parameters["$evaluation_methodology"] == "point-in-time-0.3.0"

    targeted_query, targeted_parameters = count_evaluation_observations_by_signal_query(
        evaluation_methodology="point-in-time-0.3.0",
        model_version="signal-engine-0.6.1",
        config_version=3,
        signal_ids=frozenset({"sig_b", "sig_a"}),
    )

    assert "signal_id IN ($signal_id_0, $signal_id_1)" in targeted_query
    assert targeted_parameters["$signal_id_0"] == "sig_a"
    assert targeted_parameters["$signal_id_1"] == "sig_b"


def test_schema_migration_backfills_legacy_epoch_observations() -> None:
    captured: list[tuple[str, dict[str, object] | None]] = []

    class FakePool:
        async def execute_with_retries(
            self,
            query: str,
            parameters: dict[str, object] | None = None,
        ) -> list[SimpleNamespace]:
            captured.append((query, parameters))
            if query == SELECT_EVALUATION_EPOCHS_QUERY:
                return [
                    SimpleNamespace(
                        rows=[
                            SimpleNamespace(
                                epoch_id="eval_legacy",
                                model_version="signal-engine-0.6.1",
                                config_version=2,
                                evaluated_at=datetime(2026, 8, 31, 12, tzinfo=UTC),
                                outcomes=[],
                                observations=[
                                    {
                                        "signal_id": "sig_legacy",
                                        "observation_at": "2026-08-31T11:00:00Z",
                                        "close": 100.0,
                                    }
                                ],
                                observations_truncated=False,
                            )
                        ]
                    )
                ]
            return []

    asyncio.run(
        _backfill_evaluation_observations_from_epochs(FakePool())  # type: ignore[arg-type]
    )

    assert len(captured) == 2
    query, parameters = captured[1]
    assert "UPSERT INTO `evaluation_observations`" in query
    assert parameters is not None
    assert parameters["$model_0"] == "signal-engine-0.6.1"
    assert parameters["$signal_0"] == "sig_legacy"


def test_ydb_signal_query_applies_filters_and_bounded_limit() -> None:
    query, parameters = select_signals_query(
        ticker="SBER",
        directions=frozenset({"up", "down"}),
        status="active",
        min_confidence=0.7,
        limit=10_000,
        model_version="signal-engine-0.6.1",
    )

    assert "ticker = $ticker" in query
    assert "direction IN ($direction_0, $direction_1)" in query
    assert "status = $status" in query
    assert "expires_at > $now" in query
    assert "confidence >= $min_confidence" in query
    assert "model_version = $model_version" in query
    assert "LIMIT 1000" in query
    assert parameters["$ticker"] == "SBER"
    assert parameters["$direction_0"] == "down"
    assert parameters["$direction_1"] == "up"
    assert parameters["$status"] == "active"
    assert parameters["$min_confidence"] == 0.7
    assert parameters["$model_version"] == "signal-engine-0.6.1"


def test_ydb_expired_signal_query_includes_active_rows_for_publication_normalization() -> None:
    query, parameters = select_signals_query(
        ticker=None,
        directions=None,
        status="expired",
        min_confidence=None,
        limit=20,
        model_version=None,
    )

    assert "status IN ($status, $active_status)" in query
    assert "$now" not in parameters
    assert "LIMIT 20" in query
    assert parameters["$status"] == "expired"
    assert parameters["$active_status"] == "active"


def test_neutral_market_context_is_analyzed_without_becoming_a_signal() -> None:
    timestamp = datetime(2026, 8, 10, 12, tzinfo=UTC)
    document = NewsDocument(
        source_id="rbc",
        external_id="sports-noise",
        published_at=timestamp,
        received_at=timestamp,
        title="Информационное сообщение",
        url="https://example.com/sports-noise",
        content="Нет нового экономического факта.",
        language="ru",
        source_metadata={"analysis_candidate": True, "signal_candidate": False},
        payload_hash="sports-noise-payload",
    )
    features = SemanticFeatures(
        extractor_version="test-0.1.0",
        event_type=EventType.OTHER,
        instruments=[],
        facts=[],
        polarity=0,
        materiality=0.1,
        novelty=1,
        temporal_status=TemporalStatus.CURRENT,
        rationale="Событие не влияет на рынок.",
    )

    processed = process_document(document, features, now=timestamp, generate_signals=True)

    assert processed.signals == ()


@pytest.mark.parametrize("polarity", [-1.0, 1.0])
def test_unvalidated_market_context_is_stored_without_a_signal(polarity: float) -> None:
    timestamp = datetime(2026, 8, 10, 12, tzinfo=UTC)
    document = NewsDocument(
        source_id="interfax",
        external_id="broad-sanctions-context",
        published_at=timestamp,
        received_at=timestamp,
        title="Новые санкции затронули российский рынок",
        url="https://example.com/broad-sanctions-context",
        content="Ограничения создают отрицательный рыночный фон.",
        language="ru",
        source_metadata={"analysis_candidate": True, "event_candidate": True},
        payload_hash="broad-sanctions-context-payload",
    )
    features = SemanticFeatures(
        extractor_version="test-0.1.0",
        event_type=EventType.SANCTIONS,
        instruments=[],
        facts=[],
        polarity=polarity,
        materiality=0.9,
        novelty=1,
        temporal_status=TemporalStatus.CURRENT,
        rationale="Широкий негативный контекст без проверенного инструмента.",
    )

    processed = process_document(document, features, now=timestamp, generate_signals=True)

    assert processed.signals == ()
    assert signal_rejection_reason(document, features) == "unvalidated_context_signal"


def test_generate_signals_false_is_a_hard_storage_boundary() -> None:
    timestamp = datetime(2026, 8, 10, 12, tzinfo=UTC)
    document = NewsDocument(
        source_id="interfax",
        external_id="sber-results-no-signal",
        published_at=timestamp,
        received_at=timestamp,
        title="Сбербанк увеличил чистую прибыль",
        url="https://example.com/sber-results-no-signal",
        content="Чистая прибыль выросла на 20%.",
        language="ru",
        source_metadata={"signal_candidate": True},
        payload_hash="sber-results-no-signal-payload",
    )
    features = SemanticFeatures(
        extractor_version="test-0.1.0",
        event_type=EventType.FINANCIAL_RESULTS,
        instruments=[InstrumentMention(ticker="SBER", relevance=0.96, matched_alias="Сбербанк")],
        facts=[],
        polarity=1,
        materiality=0.9,
        novelty=1,
        temporal_status=TemporalStatus.CURRENT,
        rationale="Позитивный финансовый результат.",
    )

    processed = process_document(document, features, now=timestamp, generate_signals=False)

    assert processed.signals == ()


@pytest.mark.parametrize(
    ("title", "content", "expected_tickers"),
    [
        (
            "В ВТБ оценили негативный эффект для Wildberries и Ozon от атак БПЛА",
            "Выручка Ozon снизилась на 20%.",
            {"OZON"},
        ),
        (
            "ВТБ окажет поддержку продавцам Ozon, пострадавшим в результате атак",
            "Банк увеличил объём программы поддержки на 20%.",
            {"VTBR"},
        ),
    ],
)
def test_contextual_title_companies_do_not_create_signals(
    title: str,
    content: str,
    expected_tickers: set[str],
) -> None:
    timestamp = datetime(2026, 8, 31, 12, tzinfo=UTC)
    document = NewsDocument(
        source_id="rbc",
        external_id=title,
        published_at=timestamp,
        received_at=timestamp,
        title=title,
        url="https://example.com/role-aware-attribution",
        content=content,
        language="ru",
        source_metadata={"analysis_candidate": True, "signal_candidate": True},
        payload_hash="role-aware-attribution-payload",
    )
    features = RuleBasedNewsExtractor().extract(
        NewsAnalysisInput(source_id="rbc", title=title, content=content)
    )

    processed = process_document(document, features, now=timestamp, generate_signals=True)

    assert {signal.ticker for signal in processed.signals} == expected_tickers


def test_analyzed_news_records_bounded_signal_rejection_reason() -> None:
    timestamp = datetime(2026, 8, 10, 12, tzinfo=UTC)
    document = NewsDocument(
        source_id="interfax",
        external_id="sber-community-event",
        published_at=timestamp,
        received_at=timestamp,
        title="Сбербанк провел встречу сообщества",
        url="https://example.com/sber-community-event",
        content="Участники обсудили общественные инициативы.",
        language="ru",
        source_metadata={"analysis_candidate": True, "signal_candidate": True},
        payload_hash="sber-community-event-payload",
    )
    repository = MemoryNewsRepository()

    async def scenario() -> tuple[dict[str, object], dict[str, object]]:
        result = await repository.ingest("sber-community-event", document)
        news = await repository.list_news(source_id="interfax", limit=1)
        assert result.signal_outcome is not None
        stored = news[0].source_metadata["signal_outcome"]
        assert isinstance(stored, dict)
        return dict(result.signal_outcome), dict(stored)

    outcome, stored_outcome = asyncio.run(scenario())

    assert outcome == {
        "status": "rejected_after_analysis",
        "signal_count": 0,
        "model_version": "signal-engine-0.6.1",
        "reason": "event_other",
    }
    assert stored_outcome == outcome


@pytest.mark.parametrize(
    ("has_instrument", "event_type", "materiality", "polarity", "expected"),
    [
        (False, EventType.FINANCIAL_RESULTS, 0.9, 1.0, "no_instrument"),
        (True, EventType.OTHER, 0.9, 1.0, "event_other"),
        (True, EventType.PRODUCT, 0.4, 1.0, "low_materiality"),
        (True, EventType.FINANCIAL_RESULTS, 0.9, 0.0, "weak_direction"),
    ],
)
def test_signal_rejection_reason_is_bounded_and_explainable(
    has_instrument: bool,
    event_type: EventType,
    materiality: float,
    polarity: float,
    expected: str,
) -> None:
    timestamp = datetime(2026, 8, 10, 12, tzinfo=UTC)
    document = NewsDocument(
        source_id="interfax",
        external_id="reason-fixture",
        published_at=timestamp,
        received_at=timestamp,
        title="Корпоративное событие",
        url="https://example.com/reason-fixture",
        content="Опубликована новая информация.",
        language="ru",
        source_metadata={"analysis_candidate": True},
        payload_hash="reason-fixture-payload",
    )
    features = SemanticFeatures(
        extractor_version="test-0.1.0",
        event_type=event_type,
        instruments=(
            [InstrumentMention(ticker="SBER", relevance=0.96, matched_alias="Сбербанк")]
            if has_instrument
            else []
        ),
        facts=[],
        polarity=polarity,
        materiality=materiality,
        novelty=1,
        temporal_status=TemporalStatus.CURRENT,
        rationale="Проверка причины отказа.",
    )

    assert signal_rejection_reason(document, features) == expected


def test_evaluation_epochs_are_kept_independently_by_model() -> None:
    async def scenario() -> None:
        repository = MemoryNewsRepository()
        evaluated_at = datetime(2026, 8, 10, 12, tzinfo=UTC)
        old = EvaluationEpochRecord(
            epoch_id="eval_old",
            model_version="news-baseline-0.2.0",
            config_version=1,
            evaluated_at=evaluated_at,
            outcomes=({"signal_id": "sig_old"},),
            observations=({"signal_id": "sig_old", "offset_minutes": 60},),
        )
        current = EvaluationEpochRecord(
            epoch_id="eval_current",
            model_version="news-baseline-0.3.0",
            config_version=1,
            evaluated_at=evaluated_at,
            outcomes=({"signal_id": "sig_current"},),
            observations=({"signal_id": "sig_current", "offset_minutes": 60},),
        )

        await repository.upsert_evaluation_epoch(old)
        await repository.upsert_evaluation_epoch(current)
        epochs = await repository.list_evaluation_epochs()

        assert {epoch.model_version for epoch in epochs} == {
            "news-baseline-0.2.0",
            "news-baseline-0.3.0",
        }
        assert sum(len(epoch.observations) for epoch in epochs) == 2

        summaries = await repository.list_evaluation_epochs(include_observations=False)
        assert all(not epoch.observations for epoch in summaries)
        assert {epoch.model_version for epoch in summaries} == {
            "news-baseline-0.2.0",
            "news-baseline-0.3.0",
        }

    asyncio.run(scenario())


def test_evaluation_observations_are_append_only_and_deduplicated_by_identity() -> None:
    async def scenario() -> None:
        repository = MemoryNewsRepository()
        evaluated_at = datetime(2026, 8, 31, 12, tzinfo=UTC)
        original = {
            "model_version": "signal-engine-0.6.1",
            "config_version": 3,
            "signal_id": "sig_history",
            "observation_at": "2026-08-31T11:00:00Z",
            "close": 100.0,
        }
        updated = {**original, "close": 101.0}

        await repository.upsert_evaluation_observations(
            [original],
            evaluated_at=evaluated_at,
        )
        await repository.upsert_evaluation_observations(
            [updated],
            evaluated_at=evaluated_at,
        )

        assert await repository.list_evaluation_observations() == [
            {**updated, "evaluation_methodology": "legacy"}
        ]
        assert await repository.count_evaluation_observations() == {
            ("signal-engine-0.6.1", 3): 1
        }

    asyncio.run(scenario())


def test_memory_evaluation_observation_pages_preserve_methodology_and_total() -> None:
    async def scenario() -> None:
        repository = MemoryNewsRepository()
        evaluated_at = datetime(2026, 9, 1, 12, tzinfo=UTC)
        observations = [
            {
                "model_version": "signal-engine-0.6.1",
                "config_version": 3,
                "signal_id": "sig_a" if index < 2 else "sig_b",
                "observation_at": f"2026-09-01T10:{index:02d}:00Z",
                "close": 100.0 + index,
            }
            for index in range(3)
        ]
        await repository.upsert_evaluation_observations(
            observations,
            evaluated_at=evaluated_at,
            evaluation_methodology="point-in-time-0.3.0",
        )
        await repository.upsert_evaluation_observations(
            [observations[0]],
            evaluated_at=evaluated_at,
            evaluation_methodology="point-in-time-0.2.0",
        )

        first = await repository.list_evaluation_observations_page(
            evaluation_methodology="point-in-time-0.3.0",
            model_version="signal-engine-0.6.1",
            config_version=3,
            limit=2,
        )
        assert first.total_count == 3
        assert len(first.items) == 2
        assert first.next_cursor is not None
        assert {item["evaluation_methodology"] for item in first.items} == {
            "point-in-time-0.3.0"
        }

        second = await repository.list_evaluation_observations_page(
            evaluation_methodology="point-in-time-0.3.0",
            model_version="signal-engine-0.6.1",
            config_version=3,
            limit=2,
            cursor=first.next_cursor,
        )
        assert second.total_count == 3
        assert len(second.items) == 1
        assert second.next_cursor is None
        assert await repository.count_evaluation_observations_by_signal(
            evaluation_methodology="point-in-time-0.3.0",
            model_version="signal-engine-0.6.1",
            config_version=3,
        ) == {"sig_a": 2, "sig_b": 1}
        assert await repository.count_evaluation_observations_by_signal(
            evaluation_methodology="point-in-time-0.3.0",
            model_version="signal-engine-0.6.1",
            config_version=3,
            signal_ids=frozenset({"sig_b"}),
        ) == {"sig_b": 1}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("model_version", "config_version", "message"),
    (
        ("signal-engine-0.7.0", 3, "another model"),
        ("signal-engine-0.6.1", 4, "another config"),
    ),
)
def test_memory_evaluation_observation_page_rejects_cursor_from_other_filter(
    model_version: str,
    config_version: int,
    message: str,
) -> None:
    cursor = encode_evaluation_observation_cursor(
        (
            "point-in-time-0.3.0",
            "signal-engine-0.6.1",
            3,
            "sig_001",
            "2026-09-01T10:00:00Z",
        )
    )
    repository = MemoryNewsRepository()

    with pytest.raises(ValueError, match=message):
        asyncio.run(
            repository.list_evaluation_observations_page(
                evaluation_methodology="point-in-time-0.3.0",
                model_version=model_version,
                config_version=config_version,
                cursor=cursor,
            )
        )


def test_ydb_evaluation_observation_bulk_upsert_is_bounded_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTableClient:
        def __init__(self) -> None:
            self.active = 0
            self.maximum_active = 0
            self.calls: list[tuple[str, list[dict[str, object]], object]] = []

        async def bulk_upsert(
            self,
            table_path: str,
            rows: list[dict[str, object]],
            columns: object,
        ) -> None:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            await asyncio.sleep(0)
            self.calls.append((table_path, rows, columns))
            self.active -= 1

    table_client = FakeTableClient()
    repository = YdbNewsRepository(
        endpoint="grpcs://localhost:2135",
        database="/local",
        credentials=ydb.AnonymousCredentials(),
    )
    repository._driver = SimpleNamespace(table_client=table_client)  # type: ignore[assignment]
    monkeypatch.setattr(storage_module, "EVALUATION_OBSERVATION_BULK_BATCH_SIZE", 2)
    monkeypatch.setattr(storage_module, "EVALUATION_OBSERVATION_BULK_CONCURRENCY", 2)
    monkeypatch.setattr(storage_module, "EVALUATION_OBSERVATION_BULK_ATTEMPTS", 1)
    observations = [
        {
            "model_version": "signal-engine-0.6.1",
            "config_version": 3,
            "signal_id": f"sig_{index}",
            "observation_at": f"2026-09-01T10:{index:02d}:00Z",
            "close": 100.0 + index,
        }
        for index in range(5)
    ]
    observations.append({**observations[0], "close": 999.0})

    asyncio.run(
        repository.upsert_evaluation_observations(
            observations,
            evaluated_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
            evaluation_methodology="point-in-time-0.3.0",
        )
    )

    assert len(table_client.calls) == 3
    assert table_client.maximum_active <= 2
    assert {call[0] for call in table_client.calls} == {
        "/local/evaluation_observations_v2"
    }
    written = [row for _, rows, _ in table_client.calls for row in rows]
    assert len(written) == 5
    assert all(row["evaluation_methodology"] == "point-in-time-0.3.0" for row in written)
    duplicate = next(row for row in written if row["signal_id"] == "sig_0")
    assert json.loads(str(duplicate["payload"]))["close"] == 999.0


def test_ydb_evaluation_observation_page_reads_only_one_server_page() -> None:
    captured: list[tuple[str, dict[str, object]]] = []

    class FakePool:
        async def execute_with_retries(
            self,
            query: str,
            parameters: dict[str, object],
        ) -> list[SimpleNamespace]:
            captured.append((query, parameters))
            page_rows = [
                SimpleNamespace(
                    evaluation_methodology="point-in-time-0.3.0",
                    model_version="signal-engine-0.6.1",
                    config_version=3,
                    signal_id=f"sig_{index}",
                    observation_at=f"2026-09-01T10:{index:02d}:00Z",
                    payload=json.dumps({"signal_id": f"sig_{index}"}),
                )
                for index in range(3)
            ]
            return [
                SimpleNamespace(rows=[SimpleNamespace(total_count=191_000)]),
                SimpleNamespace(rows=page_rows),
            ]

    repository = YdbNewsRepository(
        endpoint="grpcs://localhost:2135",
        database="/local",
        credentials=ydb.AnonymousCredentials(),
    )
    repository._pool = FakePool()  # type: ignore[assignment]

    page = asyncio.run(
        repository.list_evaluation_observations_page(
            evaluation_methodology="point-in-time-0.3.0",
            model_version="signal-engine-0.6.1",
            config_version=3,
            limit=2,
        )
    )

    assert page.total_count == 191_000
    assert [item["signal_id"] for item in page.items] == ["sig_0", "sig_1"]
    assert page.next_cursor is not None
    assert len(captured) == 1
    assert "LIMIT 3" in captured[0][0]
    assert "FROM `evaluation_observations_v2`" in captured[0][0]


def test_memory_assessment_snapshot_is_first_writer_wins_and_expires() -> None:
    async def scenario() -> None:
        repository = MemoryNewsRepository(cache_assessment_snapshots=True)
        created_at = datetime(2026, 8, 26, 19, tzinfo=UTC)
        first = {"meta": {"snapshot_id": "market_first"}, "data": [{"ticker": "SBER"}]}
        competing = {
            "meta": {"snapshot_id": "market_competing"},
            "data": [{"ticker": "SBER"}],
        }

        winner = await repository.get_or_create_assessment_snapshot(
            "bucket:sber",
            first,
            created_at=created_at,
            expires_before=created_at - timedelta(minutes=10),
        )
        loser = await repository.get_or_create_assessment_snapshot(
            "bucket:sber",
            competing,
            created_at=created_at,
            expires_before=created_at - timedelta(minutes=10),
        )
        assert winner == loser == first

        replacement = await repository.get_or_create_assessment_snapshot(
            "next:sber",
            competing,
            created_at=created_at + timedelta(minutes=11),
            expires_before=created_at + timedelta(minutes=1),
        )
        assert replacement == competing
        assert "bucket:sber" not in repository._assessment_snapshots

    asyncio.run(scenario())


def test_ydb_assessment_snapshot_returns_transaction_winner() -> None:
    stored_payload = '{"data": [], "meta": {"snapshot_id": "market_winner"}}'
    executed: list[str] = []

    class FakeResultSets:
        def __init__(self, rows: list[SimpleNamespace]) -> None:
            self.rows = rows

        async def __aenter__(self) -> "FakeResultSets":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def __aiter__(self):  # type: ignore[no-untyped-def]
            async def iterate():  # type: ignore[no-untyped-def]
                yield SimpleNamespace(rows=self.rows)

            return iterate()

    class FakeTransaction:
        committed = False

        async def execute(
            self,
            statement: str,
            parameters: dict[str, object],
        ) -> FakeResultSets:
            executed.append(statement)
            assert parameters == {"$snapshot_key": "bucket:tickers"}
            return FakeResultSets([SimpleNamespace(payload=stored_payload)])

        async def commit(self) -> None:
            self.committed = True

    transaction = FakeTransaction()

    class FakeSession:
        def transaction(self) -> FakeTransaction:
            return transaction

    class FakePool:
        async def retry_operation_async(self, operation):  # type: ignore[no-untyped-def]
            return await operation(FakeSession())

    repository = YdbNewsRepository(
        endpoint="grpcs://localhost:2135",
        database="/local",
        credentials=ydb.AnonymousCredentials(),
    )
    repository._pool = FakePool()  # type: ignore[assignment]

    async def scenario() -> None:
        result = await repository.get_or_create_assessment_snapshot(
            "bucket:tickers",
            {"data": [], "meta": {"snapshot_id": "market_loser"}},
            created_at=datetime(2026, 8, 26, 19, tzinfo=UTC),
            expires_before=datetime(2026, 8, 26, 18, 50, tzinfo=UTC),
        )
        assert result["meta"] == {"snapshot_id": "market_winner"}

    asyncio.run(scenario())

    assert executed == [SELECT_ASSESSMENT_SNAPSHOT_QUERY]
    assert transaction.committed is True


def test_ydb_assessment_snapshot_inserts_candidate_after_cleanup() -> None:
    executed: list[str] = []
    parameter_sets: list[dict[str, object]] = []

    class FakeResultSets:
        async def __aenter__(self) -> "FakeResultSets":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def __aiter__(self):  # type: ignore[no-untyped-def]
            async def iterate():  # type: ignore[no-untyped-def]
                yield SimpleNamespace(rows=[])

            return iterate()

    class FakeTransaction:
        committed = False

        async def execute(
            self,
            statement: str,
            parameters: dict[str, object],
        ) -> FakeResultSets:
            executed.append(statement)
            parameter_sets.append(parameters)
            return FakeResultSets()

        async def commit(self) -> None:
            self.committed = True

    transaction = FakeTransaction()

    class FakeSession:
        def transaction(self) -> FakeTransaction:
            return transaction

    class FakePool:
        async def retry_operation_async(self, operation):  # type: ignore[no-untyped-def]
            return await operation(FakeSession())

    repository = YdbNewsRepository(
        endpoint="grpcs://localhost:2135",
        database="/local",
        credentials=ydb.AnonymousCredentials(),
    )
    repository._pool = FakePool()  # type: ignore[assignment]
    candidate = {"data": [], "meta": {"snapshot_id": "market_candidate"}}

    async def scenario() -> None:
        result = await repository.get_or_create_assessment_snapshot(
            "bucket:tickers",
            candidate,
            created_at=datetime(2026, 8, 26, 19, tzinfo=UTC),
            expires_before=datetime(2026, 8, 26, 18, 50, tzinfo=UTC),
        )
        assert result == candidate

    asyncio.run(scenario())

    assert executed == [
        SELECT_ASSESSMENT_SNAPSHOT_QUERY,
        DELETE_EXPIRED_ASSESSMENT_SNAPSHOTS_QUERY,
        INSERT_ASSESSMENT_SNAPSHOT_QUERY,
    ]
    assert parameter_sets[0] == {"$snapshot_key": "bucket:tickers"}
    assert transaction.committed is True


def test_legacy_signal_copies_are_collapsed() -> None:
    first = signal_from_row(
        SimpleNamespace(
            signal_id="sig_first",
            news_id="news_same",
            ticker="SBER",
            as_of=datetime(2026, 8, 8, 10),
            data_cutoff_at=datetime(2026, 8, 8, 10),
            status="active",
            direction="up",
            action="consider_buy",
            horizon_value=3,
            horizon_unit="calendar_days",
            score=25.0,
            strength=0.25,
            confidence=0.75,
            summary="First",
            factor_contributions="[]",
            evidence_refs="[]",
            expires_at=datetime(2026, 8, 11, 10),
            invalidation_conditions="[]",
            model_version="news-baseline-0.2.0",
            config_version=1,
            created_at=datetime(2026, 8, 8, 10),
        )
    )
    newer = replace(
        first,
        id="sig_newer",
        created_at=datetime(2026, 8, 8, 10, 5, tzinfo=UTC),
    )

    result = deduplicate_signals([first, newer])

    assert [signal.id for signal in result] == ["sig_newer"]


def test_concurrent_retries_create_one_job() -> None:
    async def scenario() -> None:
        repository = MemoryNewsRepository()
        current_time = datetime.now(UTC)
        document = NewsDocument(
            source_id="interfax",
            external_id="external-1",
            published_at=current_time,
            received_at=current_time,
            title="Сбербанк опубликовал отчётность",
            url="https://example.com/news/1",
            content="Чистая прибыль выросла на 15% и оказалась выше ожиданий.",
            language="ru",
            source_metadata={},
            payload_hash="same-payload-hash",
        )

        results = await asyncio.gather(
            *(repository.ingest("concurrent-key", document) for _ in range(20))
        )

        assert len({result.job.id for result in results}) == 1
        assert sum(not result.replayed for result in results) == 1
        assert sum(result.replayed for result in results) == 19
        assert {result.job.status for result in results} == {"succeeded"}

        signals = await repository.list_signals(
            ticker="SBER",
            directions=frozenset({"up"}),
            status="active",
            min_confidence=None,
            limit=10,
        )
        assert len(signals) == 1
        assert signals[0].id == results[0].job.result_ref

    asyncio.run(scenario())


def test_stable_id_uses_eventedge_crockford_format() -> None:
    identifier = stable_id("job_", "collector-key-123")

    assert len(identifier) == 30
    assert identifier.startswith("job_")
    assert not set(identifier.removeprefix("job_")) & set("ILOU")


def test_retroactive_news_creates_historical_not_active_signal() -> None:
    async def scenario() -> None:
        repository = MemoryNewsRepository()
        document = NewsDocument(
            source_id="interfax",
            external_id="historical-1",
            published_at=datetime(2026, 7, 1, 10, tzinfo=UTC),
            received_at=datetime(2026, 7, 1, 10, tzinfo=UTC),
            title="Сбербанк опубликовал отчётность",
            url="https://example.com/historical-1",
            content="Чистая прибыль выросла на 15% и превысила ожидания.",
            language="ru",
            source_metadata={},
            payload_hash="historical-payload-hash",
        )
        await repository.ingest("historical-key", document)

        active = await repository.list_signals(
            ticker="SBER",
            directions=None,
            status="active",
            min_confidence=None,
            limit=10,
        )
        expired = await repository.list_signals(
            ticker="SBER",
            directions=None,
            status="expired",
            min_confidence=None,
            limit=10,
        )

        assert active == []
        assert len(expired) == 1
        assert expired[0].status == "expired"

    asyncio.run(scenario())


def test_delayed_discovery_does_not_revive_old_publication() -> None:
    async def scenario() -> None:
        repository = MemoryNewsRepository()
        document = NewsDocument(
            source_id="google_news",
            external_id="delayed-1",
            published_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
            received_at=datetime(2026, 8, 9, 10, tzinfo=UTC),
            title="Сбербанк опубликовал отчётность",
            url="https://example.com/delayed-1",
            content="Чистая прибыль выросла на 15% и превысила ожидания.",
            language="ru",
            source_metadata={},
            payload_hash="delayed-payload-hash",
        )
        await repository.ingest("delayed-key", document)

        active = await repository.list_signals(
            ticker="SBER",
            directions=None,
            status="active",
            min_confidence=None,
            limit=10,
        )
        expired = await repository.list_signals(
            ticker="SBER",
            directions=None,
            status="expired",
            min_confidence=None,
            limit=10,
        )

        assert active == []
        assert len(expired) == 1
        assert expired[0].as_of == document.received_at
        assert expired[0].expires_at == datetime(2026, 8, 4, 10, tzinfo=UTC)

    asyncio.run(scenario())


def test_legacy_signal_freshness_is_reanchored_to_publication() -> None:
    signal = signal_from_row(
        SimpleNamespace(
            signal_id="sig_legacy",
            news_id="news_legacy",
            ticker="SBER",
            as_of=datetime(2026, 8, 9, 10),
            data_cutoff_at=datetime(2026, 8, 9, 10),
            status="active",
            direction="up",
            action="consider_buy",
            horizon_value=3,
            horizon_unit="calendar_days",
            score=25.0,
            strength=0.25,
            confidence=0.75,
            summary="Legacy",
            factor_contributions="[]",
            evidence_refs="[]",
            expires_at=datetime(2026, 8, 12, 10),
            invalidation_conditions="[]",
            model_version="news-baseline-0.1.1",
            config_version=1,
            created_at=datetime(2026, 8, 9, 10),
        )
    )
    news = NewsRecord(
        id="news_legacy",
        source_id="google_news",
        external_id="legacy",
        published_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
        received_at=datetime(2026, 8, 9, 10, tzinfo=UTC),
        title="Сбербанк опубликовал отчётность",
        url="https://example.com/legacy",
        content="Чистая прибыль выросла.",
        language="ru",
        source_metadata={},
        created_at=datetime(2026, 8, 9, 10, tzinfo=UTC),
    )

    normalized = normalize_signal_freshness(
        [signal],
        {news.id: news},
        now=datetime(2026, 8, 10, tzinfo=UTC),
    )

    assert normalized[0].status == "expired"
    assert normalized[0].expires_at == datetime(2026, 8, 4, 10, tzinfo=UTC)


def test_cbr_context_news_does_not_create_direct_company_signal() -> None:
    async def scenario() -> None:
        repository = MemoryNewsRepository()
        document = NewsDocument(
            source_id="cbr_press",
            external_id="cbr-rates-1",
            published_at=datetime(2026, 8, 8, 10, tzinfo=UTC),
            received_at=datetime(2026, 8, 8, 10, tzinfo=UTC),
            title="Банк России опубликовал мониторинг ставок",
            url="https://www.cbr.ru/example",
            content="В расчет вошли ставки Сбербанка и ВТБ, показатель снизился.",
            language="ru",
            source_metadata={},
            payload_hash="cbr-rates-payload",
        )
        await repository.ingest("cbr-rates-key", document)

        signals = await repository.list_signals(
            ticker=None,
            directions=None,
            status=None,
            min_confidence=None,
            limit=10,
        )
        news = await repository.list_news(source_id="cbr_press", limit=10)

        assert len(news) == 1
        assert signals == []

    asyncio.run(scenario())


def test_runtime_metadata_credentials_can_be_constructed() -> None:
    credentials = ydb.iam.MetadataUrlCredentials()

    assert credentials is not None


def test_ydb_naive_timestamps_are_normalized_before_expiry_filter() -> None:
    naive = datetime(2026, 8, 1, 10)
    row = SimpleNamespace(
        signal_id="sig_test",
        news_id="news_test",
        ticker="SBER",
        as_of=naive,
        data_cutoff_at=naive,
        status="active",
        direction="up",
        action="consider_buy",
        horizon_value=3,
        horizon_unit="calendar_days",
        score=25.0,
        strength=0.25,
        confidence=0.75,
        summary="Test signal",
        factor_contributions="[]",
        evidence_refs="[]",
        expires_at=datetime(2026, 8, 4, 10),
        invalidation_conditions="[]",
        model_version="news-baseline-0.2.0",
        config_version=1,
        created_at=naive,
    )

    signal = signal_from_row(row)
    expired = filter_signals(
        [signal],
        ticker=None,
        directions=None,
        status="expired",
        min_confidence=None,
        limit=10,
    )

    assert signal.as_of.tzinfo is UTC
    assert signal.expires_at.tzinfo is UTC
    assert len(expired) == 1
    assert expired[0].status == "expired"


def test_ydb_runtime_is_created_inside_running_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeDriver:
        def __init__(self, config: ydb.DriverConfig) -> None:
            asyncio.get_running_loop()
            events.append("driver.init")

        async def wait(self, *, timeout: int, fail_fast: bool) -> None:
            asyncio.get_running_loop()
            events.append(f"driver.wait:{timeout}:{fail_fast}")

        async def stop(self, *, timeout: int) -> None:
            events.append(f"driver.stop:{timeout}")

    class FakePool:
        def __init__(self, driver: FakeDriver, *, size: int) -> None:
            asyncio.get_running_loop()
            events.append(f"pool.init:{size}")

        async def execute_with_retries(
            self, statement: str, **kwargs: object
        ) -> list[object]:
            events.append("query")
            return []

        async def stop(self) -> None:
            events.append("pool.stop")

    monkeypatch.setattr(ydb.aio, "Driver", FakeDriver)
    monkeypatch.setattr(ydb.aio, "QuerySessionPool", FakePool)

    repository = YdbNewsRepository(
        endpoint="grpcs://localhost:2135",
        database="/local",
        credentials=ydb.AnonymousCredentials(),
    )
    assert events == []

    async def scenario() -> None:
        await repository.start()
        await repository.stop()

    asyncio.run(scenario())

    assert events == [
        "driver.init",
        "driver.wait:15:True",
        "pool.init:4",
        "pool.stop",
        "driver.stop:5",
    ]


def test_ydb_schema_migration_retries_rate_limit_and_closes_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    executed: list[str] = []
    delays: list[float] = []

    class FakeDriver:
        def __init__(self, config: ydb.DriverConfig) -> None:
            events.append("driver.init")
            self.scheme_client = FakeSchemeClient()

        async def wait(self, *, timeout: int, fail_fast: bool) -> None:
            events.append(f"driver.wait:{timeout}:{fail_fast}")

        async def stop(self, *, timeout: int) -> None:
            events.append(f"driver.stop:{timeout}")

    class FakeSchemeClient:
        async def list_directory(self, path: str) -> SimpleNamespace:
            events.append(f"scheme.list:{path}")
            return SimpleNamespace(children=[])

    class FakePool:
        def __init__(self, driver: FakeDriver, *, size: int) -> None:
            events.append(f"pool.init:{size}")

        async def execute_with_retries(self, statement: str) -> list[object]:
            if not executed:
                executed.append(statement)
                raise RuntimeError(
                    "Request exceeded a limit on the number of schema operations, "
                    "try again later"
                )
            executed.append(statement)
            return []

        async def stop(self) -> None:
            events.append("pool.stop")

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(ydb.aio, "Driver", FakeDriver)
    monkeypatch.setattr(ydb.aio, "QuerySessionPool", FakePool)

    asyncio.run(
        migrate_ydb_schema(
            endpoint="grpcs://localhost:2135",
            database="/local",
            credentials=ydb.AnonymousCredentials(),
            sleep=fake_sleep,
        )
    )

    assert executed == [
        SCHEMA_STATEMENTS[0],
        *SCHEMA_STATEMENTS,
        SELECT_EVALUATION_EPOCHS_QUERY,
    ]
    assert delays == [1.0]
    assert events == [
        "driver.init",
        "driver.wait:15:True",
        "scheme.list:/local",
        "pool.init:1",
        "pool.stop",
        "driver.stop:5",
    ]


def test_ydb_schema_migration_skips_existing_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class ExistingTable:
        def __init__(self, name: str) -> None:
            self.name = name

        def is_any_table(self) -> bool:
            return True

    class FakeSchemeClient:
        async def list_directory(self, path: str) -> SimpleNamespace:
            events.append(f"scheme.list:{path}")
            return SimpleNamespace(
                children=[ExistingTable(name) for name in SCHEMA_TABLE_NAMES]
            )

    class FakeDriver:
        def __init__(self, config: ydb.DriverConfig) -> None:
            events.append("driver.init")
            self.scheme_client = FakeSchemeClient()

        async def wait(self, *, timeout: int, fail_fast: bool) -> None:
            events.append(f"driver.wait:{timeout}:{fail_fast}")

        async def stop(self, *, timeout: int) -> None:
            events.append(f"driver.stop:{timeout}")

    class UnexpectedPool:
        def __init__(self, driver: FakeDriver, *, size: int) -> None:
            raise AssertionError("No query pool should be created for an up-to-date schema")

    monkeypatch.setattr(ydb.aio, "Driver", FakeDriver)
    monkeypatch.setattr(ydb.aio, "QuerySessionPool", UnexpectedPool)

    asyncio.run(
        migrate_ydb_schema(
            endpoint="grpcs://localhost:2135",
            database="/local",
            credentials=ydb.AnonymousCredentials(),
        )
    )

    assert events == [
        "driver.init",
        "driver.wait:15:True",
        "scheme.list:/local",
        "driver.stop:5",
    ]
