import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import ydb
from ydb.query.base import QueryExecMode

from eventedge.storage import (
    SCHEMA_STATEMENTS,
    VALIDATED_QUERIES,
    MemoryNewsRepository,
    NewsDocument,
    YdbNewsRepository,
    deduplicate_signals,
    filter_signals,
    migrate_ydb_schema,
    signal_from_row,
    stable_id,
)


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
            model_version="news-baseline-0.1.1",
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
        document = NewsDocument(
            source_id="interfax",
            external_id="external-1",
            published_at=datetime(2026, 8, 8, 10, 18, tzinfo=UTC),
            received_at=datetime(2026, 8, 8, 10, 19, tzinfo=UTC),
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
        model_version="news-baseline-0.1.1",
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
            if kwargs:
                assert kwargs == {"exec_mode": QueryExecMode.EXPLAIN}
                events.append("explain")
            else:
                events.append("schema")
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
        "pool.init:2",
        *("explain" for _ in VALIDATED_QUERIES),
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

        async def wait(self, *, timeout: int, fail_fast: bool) -> None:
            events.append(f"driver.wait:{timeout}:{fail_fast}")

        async def stop(self, *, timeout: int) -> None:
            events.append(f"driver.stop:{timeout}")

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
        "pool.init:1",
        "pool.stop",
        "driver.stop:5",
    ]
