from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import ydb

CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class IdempotencyConflictError(Exception):
    """The same idempotency key was used for a different command payload."""


@dataclass(frozen=True)
class NewsDocument:
    source_id: str
    external_id: str
    published_at: datetime
    received_at: datetime
    title: str
    url: str
    content: str
    language: str
    source_metadata: Mapping[str, object]
    payload_hash: str


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    status: str
    created_at: datetime
    updated_at: datetime
    progress: float | None = None
    result_ref: str | None = None
    completed_at: datetime | None = None

    def as_api_dict(self) -> dict[str, object | None]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "progress": self.progress,
            "result_ref": self.result_ref,
            "error": None,
            "created_at": to_rfc3339(self.created_at),
            "updated_at": to_rfc3339(self.updated_at),
            "completed_at": to_rfc3339(self.completed_at) if self.completed_at else None,
        }


@dataclass(frozen=True)
class IngestResult:
    job: Job
    replayed: bool


class NewsRepository(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def ready(self) -> bool: ...

    async def ingest(self, idempotency_key: str, document: NewsDocument) -> IngestResult: ...

    async def get_job(self, job_id: str) -> Job | None: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def stable_id(prefix: str, material: str) -> str:
    value = int.from_bytes(hashlib.sha256(material.encode("utf-8")).digest()[:16], "big")
    encoded = []
    for _ in range(26):
        encoded.append(CROCKFORD_ALPHABET[value & 31])
        value >>= 5
    return prefix + "".join(reversed(encoded))


def canonical_payload_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class MemoryNewsRepository:
    def __init__(self) -> None:
        self._requests: dict[str, tuple[str, str]] = {}
        self._jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def ready(self) -> bool:
        return True

    async def ingest(self, idempotency_key: str, document: NewsDocument) -> IngestResult:
        async with self._lock:
            existing = self._requests.get(idempotency_key)
            if existing:
                payload_hash, job_id = existing
                if payload_hash != document.payload_hash:
                    raise IdempotencyConflictError
                return IngestResult(job=self._jobs[job_id], replayed=True)

            now = utc_now()
            job = Job(
                id=stable_id("job_", idempotency_key),
                kind="news_ingestion",
                status="queued",
                created_at=now,
                updated_at=now,
            )
            self._jobs[job.id] = job
            self._requests[idempotency_key] = (document.payload_hash, job.id)
            return IngestResult(job=job, replayed=False)

    async def get_job(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)


class YdbNewsRepository:
    def __init__(
        self,
        *,
        endpoint: str,
        database: str,
        credentials: ydb.Credentials | None = None,
    ) -> None:
        credentials = credentials or ydb.iam.MetadataUrlCredentials()
        config = ydb.DriverConfig(
            endpoint=endpoint,
            database=database,
            credentials=credentials,
            root_certificates=ydb.load_ydb_root_certificate(),
        )
        self._driver = ydb.aio.Driver(config)
        self._pool: ydb.aio.QuerySessionPool | None = None

    async def start(self) -> None:
        try:
            await self._driver.wait(timeout=10, fail_fast=True)
            self._pool = ydb.aio.QuerySessionPool(self._driver, size=2)
            for statement in SCHEMA_STATEMENTS:
                await self._pool.execute_with_retries(statement)
        except Exception:
            if self._pool is not None:
                await self._pool.stop()
                self._pool = None
            await self._driver.stop(timeout=5)
            raise

    async def stop(self) -> None:
        if self._pool is not None:
            await self._pool.stop()
            self._pool = None
        await self._driver.stop(timeout=5)

    async def ready(self) -> bool:
        pool = self._require_pool()
        result_sets = await pool.execute_with_retries("SELECT 1 AS ready;")
        return bool(result_sets and result_sets[0].rows[0].ready == 1)

    async def ingest(self, idempotency_key: str, document: NewsDocument) -> IngestResult:
        job_id = stable_id("job_", idempotency_key)
        news_id = stable_id("news_", f"{document.source_id}\x00{document.external_id}")

        async def transaction(session: ydb.aio.QuerySession) -> IngestResult:
            tx = session.transaction()
            existing_job: Job | None = None
            existing_hash: str | None = None
            async with await tx.execute(
                SELECT_REQUEST_QUERY,
                {"$idempotency_key": idempotency_key},
            ) as result_sets:
                async for result_set in result_sets:
                    if result_set.rows:
                        row = result_set.rows[0]
                        existing_hash = row.payload_hash
                        existing_job = job_from_row(row)

            if existing_job:
                await tx.commit()
                if existing_hash != document.payload_hash:
                    raise IdempotencyConflictError
                return IngestResult(job=existing_job, replayed=True)

            now = utc_now()
            job = Job(
                id=job_id,
                kind="news_ingestion",
                status="queued",
                created_at=now,
                updated_at=now,
            )
            parameters = {
                "$idempotency_key": idempotency_key,
                "$payload_hash": document.payload_hash,
                "$job_id": job.id,
                "$news_id": news_id,
                "$source_id": document.source_id,
                "$external_id": document.external_id,
                "$published_at": ydb.TypedValue(
                    document.published_at, ydb.PrimitiveType.Timestamp
                ),
                "$received_at": ydb.TypedValue(document.received_at, ydb.PrimitiveType.Timestamp),
                "$title": document.title,
                "$url": document.url,
                "$content": document.content,
                "$language": document.language,
                "$source_metadata": ydb.TypedValue(
                    json.dumps(document.source_metadata, ensure_ascii=False, sort_keys=True),
                    ydb.PrimitiveType.Json,
                ),
                "$created_at": ydb.TypedValue(now, ydb.PrimitiveType.Timestamp),
            }
            async with await tx.execute(
                INSERT_REQUEST_QUERY,
                parameters,
                commit_tx=True,
            ) as result_sets:
                async for _ in result_sets:
                    pass
            return IngestResult(job=job, replayed=False)

        return await self._require_pool().retry_operation_async(transaction)

    async def get_job(self, job_id: str) -> Job | None:
        result_sets = await self._require_pool().execute_with_retries(
            SELECT_JOB_QUERY,
            {"$job_id": job_id},
        )
        if not result_sets or not result_sets[0].rows:
            return None
        return job_from_row(result_sets[0].rows[0])

    def _require_pool(self) -> ydb.aio.QuerySessionPool:
        if self._pool is None:
            raise RuntimeError("YDB repository has not been started")
        return self._pool


def job_from_row(row: object) -> Job:
    return Job(
        id=row.job_id,
        kind=row.kind,
        status=row.status,
        progress=row.progress,
        result_ref=row.result_ref,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
    )


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS `news_items` (
        `news_id` Utf8 NOT NULL,
        `source_id` Utf8 NOT NULL,
        `external_id` Utf8 NOT NULL,
        `published_at` Timestamp NOT NULL,
        `received_at` Timestamp NOT NULL,
        `title` Utf8 NOT NULL,
        `url` Utf8 NOT NULL,
        `content` Utf8 NOT NULL,
        `language` Utf8 NOT NULL,
        `source_metadata` Json NOT NULL,
        `created_at` Timestamp NOT NULL,
        PRIMARY KEY (`news_id`)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS `jobs` (
        `job_id` Utf8 NOT NULL,
        `kind` Utf8 NOT NULL,
        `status` Utf8 NOT NULL,
        `progress` Double,
        `result_ref` Utf8,
        `created_at` Timestamp NOT NULL,
        `updated_at` Timestamp NOT NULL,
        `completed_at` Timestamp,
        PRIMARY KEY (`job_id`)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS `ingestion_requests` (
        `idempotency_key` Utf8 NOT NULL,
        `payload_hash` Utf8 NOT NULL,
        `job_id` Utf8 NOT NULL,
        `news_id` Utf8 NOT NULL,
        `created_at` Timestamp NOT NULL,
        PRIMARY KEY (`idempotency_key`)
    );
    """,
)

SELECT_REQUEST_QUERY = """
DECLARE $idempotency_key AS Utf8;

SELECT
    r.payload_hash AS payload_hash,
    j.job_id AS job_id,
    j.kind AS kind,
    j.status AS status,
    j.progress AS progress,
    j.result_ref AS result_ref,
    j.created_at AS created_at,
    j.updated_at AS updated_at,
    j.completed_at AS completed_at
FROM `ingestion_requests` AS r
INNER JOIN `jobs` AS j ON r.job_id = j.job_id
WHERE r.idempotency_key = $idempotency_key;
"""

INSERT_REQUEST_QUERY = """
DECLARE $idempotency_key AS Utf8;
DECLARE $payload_hash AS Utf8;
DECLARE $job_id AS Utf8;
DECLARE $news_id AS Utf8;
DECLARE $source_id AS Utf8;
DECLARE $external_id AS Utf8;
DECLARE $published_at AS Timestamp;
DECLARE $received_at AS Timestamp;
DECLARE $title AS Utf8;
DECLARE $url AS Utf8;
DECLARE $content AS Utf8;
DECLARE $language AS Utf8;
DECLARE $source_metadata AS Json;
DECLARE $created_at AS Timestamp;

UPSERT INTO `news_items` (
    news_id, source_id, external_id, published_at, received_at, title, url,
    content, language, source_metadata, created_at
) VALUES (
    $news_id, $source_id, $external_id, $published_at, $received_at, $title, $url,
    $content, $language, $source_metadata, $created_at
);

INSERT INTO `jobs` (
    job_id, kind, status, progress, result_ref, created_at, updated_at, completed_at
) VALUES (
    $job_id, "news_ingestion", "queued", NULL, NULL, $created_at, $created_at, NULL
);

INSERT INTO `ingestion_requests` (
    idempotency_key, payload_hash, job_id, news_id, created_at
) VALUES (
    $idempotency_key, $payload_hash, $job_id, $news_id, $created_at
);
"""

SELECT_JOB_QUERY = """
DECLARE $job_id AS Utf8;

SELECT
    job_id,
    kind,
    status,
    progress,
    result_ref,
    created_at,
    updated_at,
    completed_at
FROM `jobs`
WHERE job_id = $job_id;
"""
