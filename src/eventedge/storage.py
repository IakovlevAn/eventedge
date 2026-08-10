from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

import ydb

from eventedge.analysis import (
    NewsAnalysisInput,
    SemanticFeatures,
    score_features,
)
from eventedge.llm import NewsAnalyzer, RuleBasedNewsAnalyzer

CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
LOGGER = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class NewsRecord:
    id: str
    source_id: str
    external_id: str
    published_at: datetime
    received_at: datetime
    title: str
    url: str
    content: str
    language: str
    source_metadata: Mapping[str, object]
    created_at: datetime

    def as_api_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "external_id": self.external_id,
            "published_at": to_rfc3339(self.published_at),
            "received_at": to_rfc3339(self.received_at),
            "title": self.title,
            "url": self.url,
            "content": self.content,
            "language": self.language,
            "source_metadata": dict(self.source_metadata),
            "created_at": to_rfc3339(self.created_at),
        }


@dataclass(frozen=True)
class SignalRecord:
    id: str
    news_id: str
    ticker: str
    as_of: datetime
    data_cutoff_at: datetime
    status: str
    direction: str
    action: str
    horizon_value: int
    horizon_unit: str
    score: float
    strength: float
    confidence: float
    summary: str
    factor_contributions: tuple[dict[str, object], ...]
    evidence_refs: tuple[str, ...]
    expires_at: datetime
    invalidation_conditions: tuple[str, ...]
    model_version: str
    config_version: int
    created_at: datetime

    def as_api_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "ticker": self.ticker,
            "as_of": to_rfc3339(self.as_of),
            "data_cutoff_at": to_rfc3339(self.data_cutoff_at),
            "status": self.status,
            "direction": self.direction,
            "action": self.action,
            "horizon": {"value": self.horizon_value, "unit": self.horizon_unit},
            "score": self.score,
            "strength": self.strength,
            "confidence": self.confidence,
            "summary": self.summary,
            "factor_contributions": list(self.factor_contributions),
            "evidence_refs": list(self.evidence_refs),
            "expires_at": to_rfc3339(self.expires_at),
            "invalidation_conditions": list(self.invalidation_conditions),
            "model_version": self.model_version,
            "config_version": self.config_version,
            "created_at": to_rfc3339(self.created_at),
        }


@dataclass(frozen=True)
class ProcessedNews:
    news_id: str
    feature_set_id: str
    features: SemanticFeatures
    signals: tuple[SignalRecord, ...]


@dataclass(frozen=True)
class TelegramSourceRecord:
    source_id: str
    channel: str
    display_name: str
    description: str
    enabled: bool
    created_at: datetime

    def as_api_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "channel": self.channel,
            "name": self.display_name,
            "kind": "Telegram",
            "url": f"https://t.me/s/{self.channel}",
            "enabled": self.enabled,
            "managed": True,
            "quality": 65,
            "freshness": "цель ≤ 2 мин",
            "description": self.description,
            "role": self.description,
            "created_at": to_rfc3339(self.created_at),
        }


@dataclass(frozen=True)
class EvaluationEpochRecord:
    epoch_id: str
    model_version: str
    config_version: int
    evaluated_at: datetime
    outcomes: tuple[dict[str, object], ...]
    observations: tuple[dict[str, object], ...]
    observations_truncated: bool = False

    def as_meta_dict(self) -> dict[str, object]:
        return {
            "epoch_id": self.epoch_id,
            "model_version": self.model_version,
            "config_version": self.config_version,
            "evaluated_at": to_rfc3339(self.evaluated_at),
            "signals": len(self.outcomes),
            "observations": len(self.observations),
            "observations_truncated": self.observations_truncated,
        }


class NewsRepository(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def ready(self) -> bool: ...

    async def ingest(
        self,
        idempotency_key: str,
        document: NewsDocument,
        *,
        generate_signals: bool = True,
    ) -> IngestResult: ...

    async def get_job(self, job_id: str) -> Job | None: ...

    async def list_news(
        self,
        *,
        source_id: str | None,
        limit: int,
    ) -> list[NewsRecord]: ...

    async def list_signals(
        self,
        *,
        ticker: str | None,
        directions: frozenset[str] | None,
        status: str | None,
        min_confidence: float | None,
        limit: int,
    ) -> list[SignalRecord]: ...

    async def get_signal(self, signal_id: str) -> SignalRecord | None: ...

    async def list_telegram_sources(self) -> list[TelegramSourceRecord]: ...

    async def upsert_telegram_source(
        self,
        source: TelegramSourceRecord,
    ) -> TelegramSourceRecord: ...

    async def upsert_evaluation_epoch(
        self,
        epoch: EvaluationEpochRecord,
    ) -> EvaluationEpochRecord: ...

    async def list_evaluation_epochs(self) -> list[EvaluationEpochRecord]: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_rfc3339(value: datetime) -> str:
    return ensure_utc(value).isoformat().replace("+00:00", "Z")


def ensure_utc(value: datetime) -> datetime:
    """YDB Timestamp values are naive UTC; normalize them at the storage edge."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


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


def process_document(
    document: NewsDocument,
    features: SemanticFeatures,
    *,
    now: datetime | None = None,
) -> ProcessedNews:
    created_at = now or utc_now()
    news_id = stable_id("news_", f"{document.source_id}\x00{document.external_id}")
    signal_instruments = [
        instrument
        for instrument in features.instruments
        if instrument.relevance >= 0.9
        and not (document.source_id == "moex_news" and instrument.ticker == "MOEX")
    ]
    if document.source_id == "cbr_press":
        signal_instruments = []
    signal_features = features.model_copy(update={"instruments": signal_instruments})
    baseline_signals = score_features(
        signal_features,
        source_id=document.source_id,
    )
    feature_set_id = stable_id(
        "feat_",
        f"{news_id}\x00{document.payload_hash}\x00{features.extractor_version}",
    )
    signal_as_of = max(document.received_at, document.published_at)
    expires_at = document.published_at + timedelta(days=3)
    signal_status = "active" if expires_at > created_at else "expired"
    signal_records = tuple(
        SignalRecord(
            id=stable_id(
                "sig_",
                (
                    f"{news_id}\x00{signal.ticker}\x00"
                    f"{signal.model_version}\x00{signal.config_version}"
                ),
            ),
            news_id=news_id,
            ticker=signal.ticker,
            as_of=signal_as_of,
            data_cutoff_at=signal_as_of,
            status=signal_status,
            direction=signal.direction.value,
            action=signal.action.value,
            horizon_value=3,
            horizon_unit="calendar_days",
            score=signal.score,
            strength=signal.strength,
            confidence=signal.confidence,
            summary=signal.summary,
            factor_contributions=tuple(
                contribution.model_dump(mode="json") for contribution in signal.factor_contributions
            ),
            evidence_refs=(news_id,),
            expires_at=expires_at,
            invalidation_conditions=(
                "Появилась новая существенная информация по компании.",
                "Истёк горизонт сигнала.",
            ),
            model_version=signal.model_version,
            config_version=signal.config_version,
            created_at=created_at,
        )
        for signal in baseline_signals
    )
    return ProcessedNews(
        news_id=news_id,
        feature_set_id=feature_set_id,
        features=features,
        signals=signal_records,
    )


async def extract_features(
    analyzer: NewsAnalyzer,
    document: NewsDocument,
    *,
    generate_signals: bool,
) -> SemanticFeatures:
    analysis_input = NewsAnalysisInput(
        source_id=document.source_id,
        title=document.title,
        content=document.content,
        language=document.language,
    )
    if generate_signals:
        return await analyzer.extract(analysis_input)

    # Broad news coverage should not spend LLM budget or create a trading
    # signal. A deterministic extraction still leaves an auditable feature set.
    features = await RuleBasedNewsAnalyzer().extract(analysis_input)
    return features.model_copy(update={"instruments": []})


def filter_signals(
    signals: list[SignalRecord],
    *,
    ticker: str | None,
    directions: frozenset[str] | None,
    status: str | None,
    min_confidence: float | None,
    limit: int,
) -> list[SignalRecord]:
    current_time = utc_now()
    normalized = (
        replace(signal, status="expired")
        if signal.status == "active" and signal.expires_at <= current_time
        else signal
        for signal in signals
    )
    filtered = (
        signal
        for signal in normalized
        if (ticker is None or signal.ticker == ticker)
        and (directions is None or signal.direction in directions)
        and (status is None or signal.status == status)
        and (min_confidence is None or signal.confidence >= min_confidence)
    )
    return sorted(filtered, key=lambda signal: (signal.as_of, signal.id), reverse=True)[:limit]


def deduplicate_signals(signals: Iterable[SignalRecord]) -> list[SignalRecord]:
    """Collapse legacy copies of one canonical news/ticker/model signal."""
    unique: dict[tuple[object, ...], SignalRecord] = {}
    for signal in signals:
        key = (
            signal.news_id,
            signal.ticker,
            signal.as_of,
            signal.model_version,
            signal.config_version,
        )
        previous = unique.get(key)
        if previous is None or (signal.created_at, signal.id) > (
            previous.created_at,
            previous.id,
        ):
            unique[key] = signal
    return sorted(unique.values(), key=lambda signal: (signal.as_of, signal.id), reverse=True)


def normalize_signal_freshness(
    signals: Iterable[SignalRecord],
    news_by_id: Mapping[str, NewsRecord],
    *,
    now: datetime | None = None,
) -> list[SignalRecord]:
    """Anchor signal lifetime to publication time, not delayed discovery time."""
    current_time = now or utc_now()
    normalized = []
    for signal in signals:
        news = news_by_id.get(signal.news_id)
        publication_expiry = news.published_at + timedelta(days=3) if news else signal.expires_at
        expires_at = min(signal.expires_at, publication_expiry)
        status = (
            "expired"
            if signal.status == "active" and expires_at <= current_time
            else signal.status
        )
        normalized.append(replace(signal, status=status, expires_at=expires_at))
    return normalized


class MemoryNewsRepository:
    def __init__(self, *, analyzer: NewsAnalyzer | None = None) -> None:
        self._analyzer = analyzer or RuleBasedNewsAnalyzer()
        self._requests: dict[str, tuple[str, str]] = {}
        self._jobs: dict[str, Job] = {}
        self._news: dict[str, NewsRecord] = {}
        self._signals: dict[str, SignalRecord] = {}
        self._telegram_sources: dict[str, TelegramSourceRecord] = {}
        self._evaluation_epochs: dict[str, EvaluationEpochRecord] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def ready(self) -> bool:
        return True

    async def ingest(
        self,
        idempotency_key: str,
        document: NewsDocument,
        *,
        generate_signals: bool = True,
    ) -> IngestResult:
        async with self._lock:
            existing = self._requests.get(idempotency_key)
            if existing:
                payload_hash, job_id = existing
                if payload_hash != document.payload_hash:
                    raise IdempotencyConflictError
                return IngestResult(job=self._jobs[job_id], replayed=True)

            now = utc_now()
            features = await extract_features(
                self._analyzer,
                document,
                generate_signals=generate_signals,
            )
            processed = process_document(document, features, now=now)
            result_ref = processed.signals[0].id if processed.signals else processed.feature_set_id
            job = Job(
                id=stable_id("job_", idempotency_key),
                kind="news_ingestion",
                status="succeeded",
                created_at=now,
                updated_at=now,
                progress=1.0,
                result_ref=result_ref,
                completed_at=now,
            )
            self._jobs[job.id] = job
            self._news[processed.news_id] = NewsRecord(
                id=processed.news_id,
                source_id=document.source_id,
                external_id=document.external_id,
                published_at=document.published_at,
                received_at=document.received_at,
                title=document.title,
                url=document.url,
                content=document.content,
                language=document.language,
                source_metadata=document.source_metadata,
                created_at=now,
            )
            self._signals.update({signal.id: signal for signal in processed.signals})
            self._requests[idempotency_key] = (document.payload_hash, job.id)
            return IngestResult(job=job, replayed=False)

    async def get_job(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    async def list_news(
        self,
        *,
        source_id: str | None,
        limit: int,
    ) -> list[NewsRecord]:
        records = (
            item for item in self._news.values() if source_id is None or item.source_id == source_id
        )
        return sorted(
            records,
            key=lambda item: (item.published_at, item.id),
            reverse=True,
        )[:limit]

    async def list_signals(
        self,
        *,
        ticker: str | None,
        directions: frozenset[str] | None,
        status: str | None,
        min_confidence: float | None,
        limit: int,
    ) -> list[SignalRecord]:
        return filter_signals(
            list(self._signals.values()),
            ticker=ticker,
            directions=directions,
            status=status,
            min_confidence=min_confidence,
            limit=limit,
        )

    async def get_signal(self, signal_id: str) -> SignalRecord | None:
        return self._signals.get(signal_id)

    async def list_telegram_sources(self) -> list[TelegramSourceRecord]:
        return sorted(
            self._telegram_sources.values(),
            key=lambda source: (source.created_at, source.source_id),
        )

    async def upsert_telegram_source(
        self,
        source: TelegramSourceRecord,
    ) -> TelegramSourceRecord:
        self._telegram_sources[source.source_id] = source
        return source

    async def upsert_evaluation_epoch(
        self,
        epoch: EvaluationEpochRecord,
    ) -> EvaluationEpochRecord:
        self._evaluation_epochs[epoch.epoch_id] = epoch
        return epoch

    async def list_evaluation_epochs(self) -> list[EvaluationEpochRecord]:
        return sorted(
            self._evaluation_epochs.values(),
            key=lambda epoch: (epoch.evaluated_at, epoch.epoch_id),
            reverse=True,
        )


class YdbNewsRepository:
    def __init__(
        self,
        *,
        endpoint: str,
        database: str,
        credentials: ydb.Credentials | None = None,
        analyzer: NewsAnalyzer | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._database = database
        self._credentials = credentials or ydb.iam.MetadataUrlCredentials()
        self._analyzer = analyzer or RuleBasedNewsAnalyzer()
        self._driver: ydb.aio.Driver | None = None
        self._pool: ydb.aio.QuerySessionPool | None = None

    async def start(self) -> None:
        try:
            config = ydb.DriverConfig(
                endpoint=self._endpoint,
                database=self._database,
                credentials=self._credentials,
                root_certificates=ydb.load_ydb_root_certificate(),
            )
            self._driver = ydb.aio.Driver(config)
            await self._driver.wait(timeout=15, fail_fast=True)
            self._pool = ydb.aio.QuerySessionPool(self._driver, size=2)
        except (Exception, asyncio.CancelledError):
            await self._close()
            raise

    async def stop(self) -> None:
        await self._close()

    async def _close(self) -> None:
        if self._pool is not None:
            await self._pool.stop()
            self._pool = None
        if self._driver is not None:
            await self._driver.stop(timeout=5)
            self._driver = None

    async def ready(self) -> bool:
        pool = self._require_pool()
        result_sets = await pool.execute_with_retries("SELECT 1 AS ready;")
        return bool(result_sets and result_sets[0].rows[0].ready == 1)

    async def ingest(
        self,
        idempotency_key: str,
        document: NewsDocument,
        *,
        generate_signals: bool = True,
    ) -> IngestResult:
        job_id = stable_id("job_", idempotency_key)

        existing = await self._get_ingest_request(idempotency_key)
        if existing:
            existing_hash, existing_job = existing
            if existing_hash != document.payload_hash:
                raise IdempotencyConflictError
            return IngestResult(job=existing_job, replayed=True)

        now = utc_now()
        features = await extract_features(
            self._analyzer,
            document,
            generate_signals=generate_signals,
        )
        processed = process_document(document, features, now=now)
        result_ref = processed.signals[0].id if processed.signals else processed.feature_set_id
        job = Job(
            id=job_id,
            kind="news_ingestion",
            status="succeeded",
            created_at=now,
            updated_at=now,
            progress=1.0,
            result_ref=result_ref,
            completed_at=now,
        )

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

            parameters = {
                "$idempotency_key": idempotency_key,
                "$payload_hash": document.payload_hash,
                "$job_id": job.id,
                "$news_id": processed.news_id,
                "$source_id": document.source_id,
                "$external_id": document.external_id,
                "$published_at": ydb.TypedValue(document.published_at, ydb.PrimitiveType.Timestamp),
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
                "$result_ref": result_ref,
            }
            async with await tx.execute(
                INSERT_REQUEST_QUERY,
                parameters,
            ) as result_sets:
                async for _ in result_sets:
                    pass
            async with await tx.execute(
                INSERT_FEATURE_SET_QUERY,
                {
                    "$feature_set_id": processed.feature_set_id,
                    "$news_id": processed.news_id,
                    "$schema_version": processed.features.schema_version,
                    "$extractor_version": processed.features.extractor_version,
                    "$features": ydb.TypedValue(
                        processed.features.model_dump_json(),
                        ydb.PrimitiveType.Json,
                    ),
                    "$created_at": ydb.TypedValue(now, ydb.PrimitiveType.Timestamp),
                },
            ) as result_sets:
                async for _ in result_sets:
                    pass
            for signal in processed.signals:
                async with await tx.execute(
                    INSERT_SIGNAL_QUERY,
                    signal_parameters(signal),
                ) as result_sets:
                    async for _ in result_sets:
                        pass
            await tx.commit()
            return IngestResult(job=job, replayed=False)

        return await self._require_pool().retry_operation_async(transaction)

    async def _get_ingest_request(
        self,
        idempotency_key: str,
    ) -> tuple[str, Job] | None:
        result_sets = await self._require_pool().execute_with_retries(
            SELECT_REQUEST_QUERY,
            {"$idempotency_key": idempotency_key},
        )
        if not result_sets or not result_sets[0].rows:
            return None
        row = result_sets[0].rows[0]
        return row.payload_hash, job_from_row(row)

    async def get_job(self, job_id: str) -> Job | None:
        result_sets = await self._require_pool().execute_with_retries(
            SELECT_JOB_QUERY,
            {"$job_id": job_id},
        )
        if not result_sets or not result_sets[0].rows:
            return None
        return job_from_row(result_sets[0].rows[0])

    async def list_news(
        self,
        *,
        source_id: str | None,
        limit: int,
    ) -> list[NewsRecord]:
        result_sets = await self._require_pool().execute_with_retries(SELECT_NEWS_QUERY)
        rows = result_sets[0].rows if result_sets else []
        records = (
            news_from_row(row) for row in rows if source_id is None or row.source_id == source_id
        )
        return list(records)[:limit]

    async def list_signals(
        self,
        *,
        ticker: str | None,
        directions: frozenset[str] | None,
        status: str | None,
        min_confidence: float | None,
        limit: int,
    ) -> list[SignalRecord]:
        result_sets = await self._require_pool().execute_with_retries(SELECT_SIGNALS_QUERY)
        rows = result_sets[0].rows if result_sets else []
        return filter_signals(
            [signal_from_row(row) for row in rows],
            ticker=ticker,
            directions=directions,
            status=status,
            min_confidence=min_confidence,
            limit=limit,
        )

    async def get_signal(self, signal_id: str) -> SignalRecord | None:
        result_sets = await self._require_pool().execute_with_retries(
            SELECT_SIGNAL_QUERY,
            {"$signal_id": signal_id},
        )
        if not result_sets or not result_sets[0].rows:
            return None
        return signal_from_row(result_sets[0].rows[0])

    async def list_telegram_sources(self) -> list[TelegramSourceRecord]:
        result_sets = await self._require_pool().execute_with_retries(SELECT_TELEGRAM_SOURCES_QUERY)
        rows = result_sets[0].rows if result_sets else []
        return [telegram_source_from_row(row) for row in rows]

    async def upsert_telegram_source(
        self,
        source: TelegramSourceRecord,
    ) -> TelegramSourceRecord:
        await self._require_pool().execute_with_retries(
            UPSERT_TELEGRAM_SOURCE_QUERY,
            {
                "$source_id": source.source_id,
                "$channel": source.channel,
                "$display_name": source.display_name,
                "$enabled": source.enabled,
                "$created_at": ydb.TypedValue(
                    source.created_at,
                    ydb.PrimitiveType.Timestamp,
                ),
            },
        )
        await self._require_pool().execute_with_retries(
            UPSERT_TELEGRAM_SOURCE_METADATA_QUERY,
            {
                "$source_id": source.source_id,
                "$description": source.description,
            },
        )
        return source

    async def upsert_evaluation_epoch(
        self,
        epoch: EvaluationEpochRecord,
    ) -> EvaluationEpochRecord:
        await self._require_pool().execute_with_retries(
            UPSERT_EVALUATION_EPOCH_QUERY,
            evaluation_epoch_parameters(epoch),
        )
        return epoch

    async def list_evaluation_epochs(self) -> list[EvaluationEpochRecord]:
        result_sets = await self._require_pool().execute_with_retries(
            SELECT_EVALUATION_EPOCHS_QUERY,
        )
        rows = result_sets[0].rows if result_sets else []
        return [evaluation_epoch_from_row(row) for row in rows]

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
        created_at=ensure_utc(row.created_at),
        updated_at=ensure_utc(row.updated_at),
        completed_at=ensure_utc(row.completed_at) if row.completed_at else None,
    )


def signal_parameters(signal: SignalRecord) -> dict[str, object]:
    return {
        "$signal_id": signal.id,
        "$news_id": signal.news_id,
        "$ticker": signal.ticker,
        "$as_of": ydb.TypedValue(signal.as_of, ydb.PrimitiveType.Timestamp),
        "$data_cutoff_at": ydb.TypedValue(signal.data_cutoff_at, ydb.PrimitiveType.Timestamp),
        "$status": signal.status,
        "$direction": signal.direction,
        "$action": signal.action,
        "$horizon_value": ydb.TypedValue(signal.horizon_value, ydb.PrimitiveType.Uint32),
        "$horizon_unit": signal.horizon_unit,
        "$score": signal.score,
        "$strength": signal.strength,
        "$confidence": signal.confidence,
        "$summary": signal.summary,
        "$factor_contributions": ydb.TypedValue(
            json.dumps(signal.factor_contributions, ensure_ascii=False),
            ydb.PrimitiveType.Json,
        ),
        "$evidence_refs": ydb.TypedValue(
            json.dumps(signal.evidence_refs, ensure_ascii=False),
            ydb.PrimitiveType.Json,
        ),
        "$expires_at": ydb.TypedValue(signal.expires_at, ydb.PrimitiveType.Timestamp),
        "$invalidation_conditions": ydb.TypedValue(
            json.dumps(signal.invalidation_conditions, ensure_ascii=False),
            ydb.PrimitiveType.Json,
        ),
        "$model_version": signal.model_version,
        "$config_version": ydb.TypedValue(signal.config_version, ydb.PrimitiveType.Uint32),
        "$created_at": ydb.TypedValue(signal.created_at, ydb.PrimitiveType.Timestamp),
    }


def json_list(value: object) -> list[object]:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        decoded = json.loads(value)
    else:
        decoded = value
    if not isinstance(decoded, (list, tuple)):
        raise ValueError("stored JSON value must be a list")
    return list(decoded)


def json_object(value: object) -> dict[str, object]:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        raise ValueError("stored JSON value must be an object")
    return {str(key): item for key, item in decoded.items()}


def news_from_row(row: object) -> NewsRecord:
    return NewsRecord(
        id=row.news_id,
        source_id=row.source_id,
        external_id=row.external_id,
        published_at=ensure_utc(row.published_at),
        received_at=ensure_utc(row.received_at),
        title=row.title,
        url=row.url,
        content=row.content,
        language=row.language,
        source_metadata=json_object(row.source_metadata),
        created_at=ensure_utc(row.created_at),
    )


def signal_from_row(row: object) -> SignalRecord:
    contributions = json_list(row.factor_contributions)
    return SignalRecord(
        id=row.signal_id,
        news_id=row.news_id,
        ticker=row.ticker,
        as_of=ensure_utc(row.as_of),
        data_cutoff_at=ensure_utc(row.data_cutoff_at),
        status=row.status,
        direction=row.direction,
        action=row.action,
        horizon_value=row.horizon_value,
        horizon_unit=row.horizon_unit,
        score=row.score,
        strength=row.strength,
        confidence=row.confidence,
        summary=row.summary,
        factor_contributions=tuple(dict(item) for item in contributions),
        evidence_refs=tuple(str(item) for item in json_list(row.evidence_refs)),
        expires_at=ensure_utc(row.expires_at),
        invalidation_conditions=tuple(str(item) for item in json_list(row.invalidation_conditions)),
        model_version=row.model_version,
        config_version=row.config_version,
        created_at=ensure_utc(row.created_at),
    )


def telegram_source_from_row(row: object) -> TelegramSourceRecord:
    return TelegramSourceRecord(
        source_id=row.source_id,
        channel=row.channel,
        display_name=row.display_name,
        description=getattr(row, "description", None) or "Пользовательский Telegram-канал",
        enabled=row.enabled,
        created_at=ensure_utc(row.created_at),
    )


def evaluation_epoch_parameters(epoch: EvaluationEpochRecord) -> dict[str, object]:
    return {
        "$epoch_id": epoch.epoch_id,
        "$model_version": epoch.model_version,
        "$config_version": ydb.TypedValue(epoch.config_version, ydb.PrimitiveType.Uint32),
        "$evaluated_at": ydb.TypedValue(epoch.evaluated_at, ydb.PrimitiveType.Timestamp),
        "$outcomes": ydb.TypedValue(
            json.dumps(epoch.outcomes, ensure_ascii=False),
            ydb.PrimitiveType.Json,
        ),
        "$observations": ydb.TypedValue(
            json.dumps(epoch.observations, ensure_ascii=False),
            ydb.PrimitiveType.Json,
        ),
        "$observations_truncated": epoch.observations_truncated,
    }


def evaluation_epoch_from_row(row: object) -> EvaluationEpochRecord:
    return EvaluationEpochRecord(
        epoch_id=row.epoch_id,
        model_version=row.model_version,
        config_version=row.config_version,
        evaluated_at=ensure_utc(row.evaluated_at),
        outcomes=tuple(dict(item) for item in json_list(row.outcomes)),
        observations=tuple(dict(item) for item in json_list(row.observations)),
        observations_truncated=row.observations_truncated,
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
    CREATE TABLE IF NOT EXISTS `feature_sets` (
        `feature_set_id` Utf8 NOT NULL,
        `news_id` Utf8 NOT NULL,
        `schema_version` Utf8 NOT NULL,
        `extractor_version` Utf8 NOT NULL,
        `features` Json NOT NULL,
        `created_at` Timestamp NOT NULL,
        PRIMARY KEY (`feature_set_id`)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS `signals` (
        `signal_id` Utf8 NOT NULL,
        `news_id` Utf8 NOT NULL,
        `ticker` Utf8 NOT NULL,
        `as_of` Timestamp NOT NULL,
        `data_cutoff_at` Timestamp NOT NULL,
        `status` Utf8 NOT NULL,
        `direction` Utf8 NOT NULL,
        `action` Utf8 NOT NULL,
        `horizon_value` Uint32 NOT NULL,
        `horizon_unit` Utf8 NOT NULL,
        `score` Double NOT NULL,
        `strength` Double NOT NULL,
        `confidence` Double NOT NULL,
        `summary` Utf8 NOT NULL,
        `factor_contributions` Json NOT NULL,
        `evidence_refs` Json NOT NULL,
        `expires_at` Timestamp NOT NULL,
        `invalidation_conditions` Json NOT NULL,
        `model_version` Utf8 NOT NULL,
        `config_version` Uint32 NOT NULL,
        `created_at` Timestamp NOT NULL,
        PRIMARY KEY (`signal_id`)
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
    """
    CREATE TABLE IF NOT EXISTS `telegram_sources` (
        `source_id` Utf8 NOT NULL,
        `channel` Utf8 NOT NULL,
        `display_name` Utf8 NOT NULL,
        `enabled` Bool NOT NULL,
        `created_at` Timestamp NOT NULL,
        PRIMARY KEY (`source_id`)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS `telegram_source_metadata` (
        `source_id` Utf8 NOT NULL,
        `description` Utf8 NOT NULL,
        PRIMARY KEY (`source_id`)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS `evaluation_epochs` (
        `epoch_id` Utf8 NOT NULL,
        `model_version` Utf8 NOT NULL,
        `config_version` Uint32 NOT NULL,
        `evaluated_at` Timestamp NOT NULL,
        `outcomes` Json NOT NULL,
        `observations` Json NOT NULL,
        `observations_truncated` Bool NOT NULL,
        PRIMARY KEY (`epoch_id`)
    );
    """,
)

SCHEMA_TABLE_NAMES = (
    "news_items",
    "feature_sets",
    "signals",
    "jobs",
    "ingestion_requests",
    "telegram_sources",
    "telegram_source_metadata",
    "evaluation_epochs",
)

SCHEMA_RATE_LIMIT_MESSAGE = "Request exceeded a limit on the number of schema operations"


async def migrate_ydb_schema(
    *,
    endpoint: str,
    database: str,
    credentials: ydb.Credentials,
    max_attempts: int = 6,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Apply YDB DDL once during deployment, outside serverless startup."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    config = ydb.DriverConfig(
        endpoint=endpoint,
        database=database,
        credentials=credentials,
        root_certificates=ydb.load_ydb_root_certificate(),
    )
    driver = ydb.aio.Driver(config)
    pool: ydb.aio.QuerySessionPool | None = None
    try:
        await driver.wait(timeout=15, fail_fast=True)
        directory = await driver.scheme_client.list_directory(database)
        existing_tables = {entry.name for entry in directory.children if entry.is_any_table()}
        missing_statements = [
            (table_name, statement)
            for table_name, statement in zip(
                SCHEMA_TABLE_NAMES,
                SCHEMA_STATEMENTS,
                strict=True,
            )
            if table_name not in existing_tables
        ]
        if not missing_statements:
            LOGGER.info("YDB schema is up to date; no DDL operations are required")
            return

        pool = ydb.aio.QuerySessionPool(driver, size=1)
        for table_name, statement in missing_statements:
            LOGGER.info("Creating missing YDB table %s", table_name)
            await _execute_schema_statement_with_backoff(
                pool,
                statement,
                max_attempts=max_attempts,
                sleep=sleep,
            )
    finally:
        if pool is not None:
            try:
                await pool.stop()
            finally:
                await driver.stop(timeout=5)
        else:
            await driver.stop(timeout=5)


async def _execute_schema_statement_with_backoff(
    pool: ydb.aio.QuerySessionPool,
    statement: str,
    *,
    max_attempts: int,
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    for attempt in range(1, max_attempts + 1):
        try:
            await pool.execute_with_retries(statement)
            return
        except Exception as exc:
            if SCHEMA_RATE_LIMIT_MESSAGE not in str(exc) or attempt == max_attempts:
                raise
            delay = float(2 ** (attempt - 1))
            LOGGER.warning(
                "YDB schema operation was rate-limited; retrying in %.0f seconds (attempt %d/%d)",
                delay,
                attempt + 1,
                max_attempts,
            )
            await sleep(delay)


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
DECLARE $result_ref AS Utf8;

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
    $job_id, "news_ingestion", "succeeded", 1.0, $result_ref,
    $created_at, $created_at, $created_at
);

INSERT INTO `ingestion_requests` (
    idempotency_key, payload_hash, job_id, news_id, created_at
) VALUES (
    $idempotency_key, $payload_hash, $job_id, $news_id, $created_at
);
"""

INSERT_FEATURE_SET_QUERY = """
DECLARE $feature_set_id AS Utf8;
DECLARE $news_id AS Utf8;
DECLARE $schema_version AS Utf8;
DECLARE $extractor_version AS Utf8;
DECLARE $features AS Json;
DECLARE $created_at AS Timestamp;

INSERT INTO `feature_sets` (
    feature_set_id, news_id, schema_version, extractor_version, features, created_at
) VALUES (
    $feature_set_id, $news_id, $schema_version, $extractor_version, $features, $created_at
);
"""

INSERT_SIGNAL_QUERY = """
DECLARE $signal_id AS Utf8;
DECLARE $news_id AS Utf8;
DECLARE $ticker AS Utf8;
DECLARE $as_of AS Timestamp;
DECLARE $data_cutoff_at AS Timestamp;
DECLARE $status AS Utf8;
DECLARE $direction AS Utf8;
DECLARE $action AS Utf8;
DECLARE $horizon_value AS Uint32;
DECLARE $horizon_unit AS Utf8;
DECLARE $score AS Double;
DECLARE $strength AS Double;
DECLARE $confidence AS Double;
DECLARE $summary AS Utf8;
DECLARE $factor_contributions AS Json;
DECLARE $evidence_refs AS Json;
DECLARE $expires_at AS Timestamp;
DECLARE $invalidation_conditions AS Json;
DECLARE $model_version AS Utf8;
DECLARE $config_version AS Uint32;
DECLARE $created_at AS Timestamp;

INSERT INTO `signals` (
    signal_id, news_id, ticker, as_of, data_cutoff_at, status, direction, action,
    horizon_value, horizon_unit, score, strength, confidence, summary,
    factor_contributions, evidence_refs, expires_at, invalidation_conditions,
    model_version, config_version, created_at
) VALUES (
    $signal_id, $news_id, $ticker, $as_of, $data_cutoff_at, $status, $direction,
    $action, $horizon_value, $horizon_unit, $score, $strength, $confidence, $summary,
    $factor_contributions, $evidence_refs, $expires_at, $invalidation_conditions,
    $model_version, $config_version, $created_at
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

SELECT_NEWS_QUERY = """
SELECT
    news_id,
    source_id,
    external_id,
    published_at,
    received_at,
    title,
    url,
    content,
    language,
    source_metadata,
    created_at
FROM `news_items`
ORDER BY published_at DESC, news_id DESC
LIMIT 1000;
"""

SIGNAL_SELECT_COLUMNS = """
    signal_id,
    news_id,
    ticker,
    as_of,
    data_cutoff_at,
    status,
    direction,
    action,
    horizon_value,
    horizon_unit,
    score,
    strength,
    confidence,
    summary,
    factor_contributions,
    evidence_refs,
    expires_at,
    invalidation_conditions,
    model_version,
    config_version,
    created_at
"""

SELECT_SIGNALS_QUERY = f"""
SELECT {SIGNAL_SELECT_COLUMNS}
FROM `signals`
ORDER BY as_of DESC, signal_id DESC
LIMIT 1000;
"""

SELECT_SIGNAL_QUERY = f"""
DECLARE $signal_id AS Utf8;

SELECT {SIGNAL_SELECT_COLUMNS}
FROM `signals`
WHERE signal_id = $signal_id;
"""

SELECT_TELEGRAM_SOURCES_QUERY = """
SELECT
    sources.source_id AS source_id,
    sources.channel AS channel,
    sources.display_name AS display_name,
    metadata.description AS description,
    sources.enabled AS enabled,
    sources.created_at AS created_at
FROM `telegram_sources` AS sources
LEFT JOIN `telegram_source_metadata` AS metadata
ON sources.source_id = metadata.source_id
ORDER BY sources.created_at ASC, sources.source_id ASC;
"""

UPSERT_TELEGRAM_SOURCE_QUERY = """
DECLARE $source_id AS Utf8;
DECLARE $channel AS Utf8;
DECLARE $display_name AS Utf8;
DECLARE $enabled AS Bool;
DECLARE $created_at AS Timestamp;

UPSERT INTO `telegram_sources` (
    source_id, channel, display_name, enabled, created_at
) VALUES (
    $source_id, $channel, $display_name, $enabled, $created_at
);
"""

UPSERT_TELEGRAM_SOURCE_METADATA_QUERY = """
DECLARE $source_id AS Utf8;
DECLARE $description AS Utf8;

UPSERT INTO `telegram_source_metadata` (
    source_id, description
) VALUES (
    $source_id, $description
);
"""

SELECT_EVALUATION_EPOCHS_QUERY = """
SELECT
    epoch_id,
    model_version,
    config_version,
    evaluated_at,
    outcomes,
    observations,
    observations_truncated
FROM `evaluation_epochs`
ORDER BY evaluated_at DESC, epoch_id DESC;
"""

UPSERT_EVALUATION_EPOCH_QUERY = """
DECLARE $epoch_id AS Utf8;
DECLARE $model_version AS Utf8;
DECLARE $config_version AS Uint32;
DECLARE $evaluated_at AS Timestamp;
DECLARE $outcomes AS Json;
DECLARE $observations AS Json;
DECLARE $observations_truncated AS Bool;

UPSERT INTO `evaluation_epochs` (
    epoch_id,
    model_version,
    config_version,
    evaluated_at,
    outcomes,
    observations,
    observations_truncated
) VALUES (
    $epoch_id,
    $model_version,
    $config_version,
    $evaluated_at,
    $outcomes,
    $observations,
    $observations_truncated
);
"""

VALIDATED_QUERIES = (
    SELECT_REQUEST_QUERY,
    INSERT_REQUEST_QUERY,
    INSERT_FEATURE_SET_QUERY,
    INSERT_SIGNAL_QUERY,
    SELECT_JOB_QUERY,
    SELECT_NEWS_QUERY,
    SELECT_SIGNALS_QUERY,
    SELECT_SIGNAL_QUERY,
    SELECT_TELEGRAM_SOURCES_QUERY,
    UPSERT_TELEGRAM_SOURCE_QUERY,
    UPSERT_TELEGRAM_SOURCE_METADATA_QUERY,
    SELECT_EVALUATION_EPOCHS_QUERY,
    UPSERT_EVALUATION_EPOCH_QUERY,
)
