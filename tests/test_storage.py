import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import ydb

from eventedge.analysis import EventType, InstrumentMention, SemanticFeatures, TemporalStatus
from eventedge.storage import (
    SCHEMA_STATEMENTS,
    SCHEMA_TABLE_NAMES,
    EvaluationEpochRecord,
    MemoryNewsRepository,
    NewsDocument,
    NewsRecord,
    YdbNewsRepository,
    deduplicate_signals,
    filter_signals,
    migrate_ydb_schema,
    normalize_signal_freshness,
    process_document,
    signal_from_row,
    stable_id,
)


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

    assert executed == [SCHEMA_STATEMENTS[0], *SCHEMA_STATEMENTS]
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
