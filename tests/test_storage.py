import asyncio
from datetime import UTC, datetime

import ydb

from eventedge.storage import MemoryNewsRepository, NewsDocument, stable_id


def test_concurrent_retries_create_one_job() -> None:
    async def scenario() -> None:
        repository = MemoryNewsRepository()
        document = NewsDocument(
            source_id="interfax",
            external_id="external-1",
            published_at=datetime(2026, 8, 8, 10, 18, tzinfo=UTC),
            received_at=datetime(2026, 8, 8, 10, 19, tzinfo=UTC),
            title="Новость",
            url="https://example.com/news/1",
            content="Текст новости",
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

    asyncio.run(scenario())


def test_stable_id_uses_eventedge_crockford_format() -> None:
    identifier = stable_id("job_", "collector-key-123")

    assert len(identifier) == 30
    assert identifier.startswith("job_")
    assert not set(identifier.removeprefix("job_")) & set("ILOU")


def test_runtime_metadata_credentials_can_be_constructed() -> None:
    credentials = ydb.iam.MetadataUrlCredentials()

    assert credentials is not None
