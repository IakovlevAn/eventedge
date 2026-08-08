import asyncio
from datetime import UTC, datetime

import pytest
import ydb

from eventedge.storage import (
    SCHEMA_STATEMENTS,
    VALIDATED_QUERIES,
    MemoryNewsRepository,
    NewsDocument,
    YdbNewsRepository,
    stable_id,
)


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


def test_runtime_metadata_credentials_can_be_constructed() -> None:
    credentials = ydb.iam.MetadataUrlCredentials()

    assert credentials is not None


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
            events.append("validate" if kwargs else "schema")
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
        *("schema" for _ in SCHEMA_STATEMENTS),
        *("validate" for _ in VALIDATED_QUERIES),
        "pool.stop",
        "driver.stop:5",
    ]
