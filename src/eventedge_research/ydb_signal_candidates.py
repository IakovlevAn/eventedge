from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eventedge.analysis import (
    DEFAULT_MOEX_ALIASES,
    SCORING_CONFIG,
    NewsAnalysisInput,
    RuleBasedNewsExtractor,
    SemanticFeatures,
    score_features,
)
from eventedge.events import TICKER_SECTORS, cluster_market_events
from eventedge_research.research_artifacts import iter_jsonl_objects, write_jsonl
from eventedge_research.signal_dataset import SignalFeatureSnapshot
from eventedge_research.signal_dataset_builder import (
    SignalEventCandidate,
    signal_candidate_schema_version,
)
from eventedge_research.ydb_signal_export import (
    verify_ydb_export,
    ydb_export_read_consistency,
)

MAX_EXPORTED_NEWS_ROWS = 100_000
MAX_EXPORTED_FEATURE_ROWS = 200_000
MAX_CANDIDATE_CONTENT_CHARS = 50_000
DEFAULT_MAXIMUM_FEATURE_LATENCY = timedelta(minutes=5)
DEFAULT_MAXIMUM_DECISION_DELAY = timedelta(minutes=5)
DEFAULT_MINIMUM_INSTRUMENT_RELEVANCE = 0.9
EXCLUDED_RESEARCH_SOURCES = frozenset({"eventedge_smoke"})


class SignalNewsSnapshot(BaseModel):
    """One validated point-in-time news snapshot used by candidate research."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    news_id: Annotated[str, Field(min_length=1, max_length=200)]
    source_id: Annotated[str, Field(min_length=1, max_length=100)]
    external_id: Annotated[str, Field(min_length=1)]
    published_at: datetime
    received_at: datetime
    title: Annotated[str, Field(min_length=1, max_length=2_000)]
    url: str
    content: str
    language: str
    source_metadata: dict[str, object]
    created_at: datetime


class SignalFeatureSetSnapshot(BaseModel):
    """One persisted semantic snapshot bound to a processed news payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    feature_set_id: Annotated[str, Field(min_length=1, max_length=200)]
    news_id: Annotated[str, Field(min_length=1, max_length=200)]
    schema_version: str
    extractor_version: Annotated[
        str,
        Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,63}$"),
    ]
    features: SemanticFeatures
    created_at: datetime

    @model_validator(mode="after")
    def validate_snapshot(self) -> SignalFeatureSetSnapshot:
        if not _is_aware(self.created_at):
            raise ValueError("stored feature created_at must be timezone-aware")
        if self.schema_version != self.features.schema_version:
            raise ValueError("stored feature schema versions do not match")
        if self.extractor_version != self.features.extractor_version:
            raise ValueError("stored feature extractor versions do not match")
        return self


@dataclass(frozen=True)
class _PreparedNews:
    row: SignalNewsSnapshot
    tickers: frozenset[str]
    features: SemanticFeatures
    rules_by_ticker: Mapping[str, object]
    feature_available_at: datetime
    feature_set_id: str | None


def build_ydb_signal_candidates(
    export_directory: Path,
    *,
    max_rows: int = MAX_EXPORTED_NEWS_ROWS,
    include_telegram_with_permission: bool = False,
    use_stored_feature_sets: bool = False,
    maximum_feature_latency: timedelta = DEFAULT_MAXIMUM_FEATURE_LATENCY,
    maximum_decision_delay: timedelta = DEFAULT_MAXIMUM_DECISION_DELAY,
    maximum_publication_age: timedelta | None = None,
) -> tuple[list[SignalEventCandidate], dict[str, object]]:
    """Build outcome-free event/ticker candidates from a verified YDB export."""
    if max_rows < 1:
        raise ValueError("max_rows must be positive")
    if maximum_feature_latency <= timedelta(0) or maximum_feature_latency > timedelta(days=1):
        raise ValueError("maximum feature latency must be within (0, 1 day]")
    if maximum_decision_delay <= timedelta(0) or maximum_decision_delay > timedelta(days=1):
        raise ValueError("maximum decision delay must be within (0, 1 day]")
    if maximum_publication_age is not None and maximum_publication_age <= timedelta(0):
        raise ValueError("maximum publication age must be positive")
    required_tables = ("news_items", "feature_sets") if use_stored_feature_sets else ("news_items",)
    manifest = verify_ydb_export(export_directory, required_tables=required_tables)
    manifest_sha256 = _file_sha256(export_directory / "manifest.json")
    feature_sets = (
        _load_feature_sets(
            export_directory / "feature_sets.jsonl",
            max_rows=MAX_EXPORTED_FEATURE_ROWS,
        )
        if use_stored_feature_sets
        else None
    )
    prepared, preparation_report = _prepare_news_file(
        export_directory / "news_items.jsonl",
        max_rows=max_rows,
        include_telegram_with_permission=include_telegram_with_permission,
        feature_sets=feature_sets,
        maximum_feature_latency=maximum_feature_latency,
        maximum_decision_delay=maximum_decision_delay,
        maximum_publication_age=maximum_publication_age,
    )
    candidates, report = _build_candidates(
        prepared,
        preparation_report,
        candidate_notes=_candidate_notes(
            use_stored_feature_sets=use_stored_feature_sets,
            maximum_feature_latency=maximum_feature_latency,
            maximum_decision_delay=maximum_decision_delay,
            maximum_publication_age=maximum_publication_age,
        ),
    )
    if (
        verify_ydb_export(export_directory, required_tables=required_tables) != manifest
        or _file_sha256(export_directory / "manifest.json") != manifest_sha256
    ):
        raise ValueError("YDB export changed while candidates were being built")
    report.update(
        {
            "schema_version": "ydb-signal-candidate-build-1.0",
            "source_manifest_sha256": manifest_sha256,
            "source_database": manifest["database"],
            "source_manifest_schema_version": manifest["schema_version"],
            "source_read_consistency": ydb_export_read_consistency(manifest),
            "stored_feature_policy": {
                "enabled": use_stored_feature_sets,
                "payload_binding": (
                    "news_id_and_exact_processing_created_at" if use_stored_feature_sets else None
                ),
                "maximum_latency_seconds": (
                    maximum_feature_latency.total_seconds() if use_stored_feature_sets else None
                ),
            },
            "freshness_policy": {
                "maximum_publication_age_seconds": (
                    maximum_publication_age.total_seconds()
                    if maximum_publication_age is not None
                    else None
                ),
                "timestamp_pair": "published_at_to_received_at",
                "maximum_decision_delay_seconds": (
                    maximum_decision_delay.total_seconds()
                ),
                "decision_timestamp": "final_feature_available_at",
            },
        }
    )
    return candidates, report


def build_signal_candidates_from_news_snapshots(
    rows: Iterable[SignalNewsSnapshot],
    *,
    candidate_notes: str,
    max_rows: int = MAX_EXPORTED_NEWS_ROWS,
    include_telegram_with_permission: bool = False,
    maximum_publication_age: timedelta | None = None,
    minimum_instrument_relevance: float = DEFAULT_MINIMUM_INSTRUMENT_RELEVANCE,
) -> tuple[list[SignalEventCandidate], dict[str, object]]:
    """Build candidates from validated local snapshots with explicit provenance notes."""
    if max_rows < 1:
        raise ValueError("max_rows must be positive")
    if not candidate_notes.strip():
        raise ValueError("candidate_notes must not be empty")
    if maximum_publication_age is not None and maximum_publication_age <= timedelta(0):
        raise ValueError("maximum publication age must be positive")
    if not 0 <= minimum_instrument_relevance <= 1:
        raise ValueError("minimum instrument relevance must be between 0 and 1")
    prepared, preparation_report = _prepare_news_rows(
        rows,
        max_rows=max_rows,
        include_telegram_with_permission=include_telegram_with_permission,
        feature_sets=None,
        maximum_feature_latency=DEFAULT_MAXIMUM_FEATURE_LATENCY,
        maximum_decision_delay=DEFAULT_MAXIMUM_DECISION_DELAY,
        maximum_publication_age=maximum_publication_age,
        minimum_instrument_relevance=minimum_instrument_relevance,
    )
    return _build_candidates(
        prepared,
        preparation_report,
        candidate_notes=candidate_notes,
    )


def _build_candidates(
    prepared: list[_PreparedNews],
    preparation_report: Mapping[str, object],
    *,
    candidate_notes: str,
) -> tuple[list[SignalEventCandidate], dict[str, object]]:
    event_items = [_event_item(item) for item in prepared]
    events = cluster_market_events(event_items)
    prepared_by_id = {item.row.news_id: item for item in prepared}
    candidates = [
        candidate
        for event in events
        for candidate in _event_candidates(
            event,
            prepared_by_id,
            candidate_notes=candidate_notes,
        )
    ]
    candidates.sort(
        key=lambda candidate: (
            candidate.decision_at,
            candidate.event_id,
            candidate.ticker,
        )
    )
    _validate_candidate_identities(candidates)
    direction_counts = Counter(
        candidate.features.rule_direction or "missing" for candidate in candidates
    )
    report: dict[str, object] = {
        "schema_version": "signal-candidate-build-1.0",
        **preparation_report,
        "clustered_events": len(events),
        "multi_publication_events": sum(len(event["news_ids"]) > 1 for event in events),
        "event_ticker_candidates": len(candidates),
        "candidate_tickers": dict(Counter(item.ticker for item in candidates).most_common()),
        "candidate_rule_directions": dict(direction_counts.most_common()),
        "decision_at": {
            "minimum": candidates[0].decision_at.isoformat() if candidates else None,
            "maximum": candidates[-1].decision_at.isoformat() if candidates else None,
        },
    }
    return candidates, report


def write_signal_event_candidates(
    path: Path,
    candidates: Iterable[SignalEventCandidate],
) -> str:
    """Write canonical candidate JSONL without overwriting an existing artifact."""
    ordered = sorted(
        candidates,
        key=lambda candidate: (
            candidate.decision_at,
            candidate.event_id,
            candidate.ticker,
        ),
    )
    if not ordered:
        raise ValueError("cannot write empty signal event candidates")
    _validate_candidate_identities(ordered)
    write_jsonl(
        path,
        (candidate.model_dump(mode="json") for candidate in ordered),
        maximum_rows=len(ordered),
    )
    return _file_sha256(path)


def _candidate_notes(
    *,
    use_stored_feature_sets: bool,
    maximum_feature_latency: timedelta,
    maximum_decision_delay: timedelta,
    maximum_publication_age: timedelta | None,
) -> str:
    freshness = (
        ""
        if maximum_publication_age is None
        else (
            " News received more than "
            f"{int(maximum_publication_age.total_seconds())}s after publication "
            "was excluded before event clustering."
        )
    )
    decision_delay_seconds = int(maximum_decision_delay.total_seconds())
    if not use_stored_feature_sets:
        return (
            "Built locally from the immutable YDB read-only extract; deterministic "
            "rule features were replayed from text available at received_at. "
            f"Decisions later than {decision_delay_seconds}s after publication "
            "were excluded."
            f"{freshness}"
        )
    latency_seconds = int(maximum_feature_latency.total_seconds())
    return (
        "Built locally from the immutable YDB read-only extract; semantic features "
        "are bound to the latest news payload by exact news_id/created_at, became "
        f"available no later than {latency_seconds}s after received_at, and "
        f"decision_at is their persisted availability time. Decisions later than "
        f"{decision_delay_seconds}s after publication were excluded.{freshness}"
    )


def _load_feature_sets(
    path: Path,
    *,
    max_rows: int,
) -> dict[str, tuple[SignalFeatureSetSnapshot, ...]]:
    grouped: dict[str, list[SignalFeatureSetSnapshot]] = {}
    seen_ids: set[str] = set()
    row_count = 0
    for row_count, payload in enumerate(
        iter_jsonl_objects(path, maximum_rows=max_rows),
        1,
    ):
        try:
            snapshot = SignalFeatureSetSnapshot.model_validate(payload)
        except Exception as error:
            raise ValueError(f"invalid feature snapshot row {row_count}: {error}") from error
        if snapshot.feature_set_id in seen_ids:
            raise ValueError(
                f"duplicate feature_set_id at row {row_count}: {snapshot.feature_set_id}"
            )
        seen_ids.add(snapshot.feature_set_id)
        grouped.setdefault(snapshot.news_id, []).append(snapshot)
    if row_count == 0:
        raise ValueError("stored feature snapshot is empty")
    return {
        news_id: tuple(sorted(rows, key=lambda row: (row.created_at, row.feature_set_id)))
        for news_id, rows in grouped.items()
    }


def _prepare_news_file(
    path: Path,
    *,
    max_rows: int,
    include_telegram_with_permission: bool,
    feature_sets: Mapping[str, tuple[SignalFeatureSetSnapshot, ...]] | None,
    maximum_feature_latency: timedelta,
    maximum_decision_delay: timedelta,
    maximum_publication_age: timedelta | None,
) -> tuple[list[_PreparedNews], dict[str, object]]:
    return _prepare_news_rows(
        _validated_news_file_rows(path, max_rows=max_rows),
        max_rows=max_rows,
        include_telegram_with_permission=include_telegram_with_permission,
        feature_sets=feature_sets,
        maximum_feature_latency=maximum_feature_latency,
        maximum_decision_delay=maximum_decision_delay,
        maximum_publication_age=maximum_publication_age,
        minimum_instrument_relevance=DEFAULT_MINIMUM_INSTRUMENT_RELEVANCE,
    )


def _validated_news_file_rows(
    path: Path,
    *,
    max_rows: int,
) -> Iterable[SignalNewsSnapshot]:
    for line_number, payload in enumerate(
        iter_jsonl_objects(path, maximum_rows=max_rows),
        1,
    ):
        try:
            yield SignalNewsSnapshot.model_validate(payload)
        except Exception as error:
            raise ValueError(f"invalid news snapshot row {line_number}: {error}") from error


def _select_stored_feature_set(
    row: SignalNewsSnapshot,
    *,
    feature_sets: Mapping[str, tuple[SignalFeatureSetSnapshot, ...]],
    maximum_feature_latency: timedelta,
) -> tuple[SignalFeatureSetSnapshot | None, str | None]:
    if not _is_aware(row.created_at):
        return None, "stored_feature_news_created_at_naive"
    matching = [
        snapshot
        for snapshot in feature_sets.get(row.news_id, ())
        if snapshot.created_at == row.created_at
    ]
    if not matching:
        return None, "stored_feature_payload_mismatch"
    if len(matching) > 1:
        raise ValueError(f"multiple feature snapshots match latest news payload={row.news_id!r}")
    snapshot = matching[0]
    if snapshot.created_at < row.received_at:
        return None, "stored_feature_before_received"
    if snapshot.created_at - row.received_at > maximum_feature_latency:
        return None, "stored_feature_too_late"
    return snapshot, None


def _prepare_news_rows(
    rows: Iterable[SignalNewsSnapshot],
    *,
    max_rows: int,
    include_telegram_with_permission: bool,
    feature_sets: Mapping[str, tuple[SignalFeatureSetSnapshot, ...]] | None,
    maximum_feature_latency: timedelta,
    maximum_decision_delay: timedelta,
    maximum_publication_age: timedelta | None,
    minimum_instrument_relevance: float,
) -> tuple[list[_PreparedNews], dict[str, object]]:
    prepared: list[_PreparedNews] = []
    skipped: Counter[str] = Counter()
    direct_match_drift = 0
    source_counts: Counter[str] = Counter()
    content_truncated = 0
    telegram_rows_seen = 0
    telegram_rows_included = 0
    extractor_counts: Counter[str] = Counter()
    row_count = 0
    extractor = RuleBasedNewsExtractor()
    for row_count, row in enumerate(rows, 1):
        if row_count > max_rows:
            raise ValueError(f"news snapshot exceeds the {max_rows}-row safety limit")
        if row.source_id in EXCLUDED_RESEARCH_SOURCES:
            skipped["excluded_source"] += 1
            continue
        if row.source_id.startswith("telegram_"):
            telegram_rows_seen += 1
            if not include_telegram_with_permission:
                skipped["telegram_permission_required"] += 1
                continue
            telegram_rows_included += 1
        if not _is_aware(row.published_at) or not _is_aware(row.received_at):
            skipped["naive_timestamp"] += 1
            continue
        if row.published_at > row.received_at:
            skipped["published_after_received"] += 1
            continue
        if (
            maximum_publication_age is not None
            and row.received_at - row.published_at > maximum_publication_age
        ):
            skipped["publication_too_old"] += 1
            continue
        tickers = _metadata_tickers(row.source_metadata)
        if not tickers:
            skipped["no_direct_ticker"] += 1
            continue
        feature_set_id = None
        feature_available_at = row.received_at
        if feature_sets is None:
            features = extractor.extract(
                NewsAnalysisInput(
                    source_id=row.source_id,
                    title=row.title,
                    content=row.content or row.title,
                    language=row.language,
                )
            )
        else:
            stored, reason = _select_stored_feature_set(
                row,
                feature_sets=feature_sets,
                maximum_feature_latency=maximum_feature_latency,
            )
            if stored is None:
                skipped[reason or "stored_feature_unavailable"] += 1
                continue
            features = stored.features
            feature_set_id = stored.feature_set_id
            feature_available_at = stored.created_at
        if feature_available_at - row.published_at > maximum_decision_delay:
            skipped["decision_after_admission_cutoff"] += 1
            continue
        direct_instruments = [
            instrument
            for instrument in features.instruments
            if instrument.relevance >= minimum_instrument_relevance and instrument.ticker in tickers
        ]
        direct_match_drift += tickers != {item.ticker for item in direct_instruments}
        rule_features = features.model_copy(update={"instruments": direct_instruments})
        rules_by_ticker = {
            signal.ticker: signal
            for signal in score_features(rule_features, source_id=row.source_id)
        }
        prepared.append(
            _PreparedNews(
                row=row,
                tickers=tickers,
                features=features,
                rules_by_ticker=rules_by_ticker,
                feature_available_at=feature_available_at,
                feature_set_id=feature_set_id,
            )
        )
        extractor_counts[features.extractor_version] += 1
        source_counts[row.source_id] += 1
        content_truncated += len(row.content) > MAX_CANDIDATE_CONTENT_CHARS
    return prepared, {
        "source_news_rows": row_count,
        "eligible_news_rows": len(prepared),
        "eligible_news_sources": dict(source_counts.most_common()),
        "direct_ticker_match_drift_rows": direct_match_drift,
        "content_truncated_rows": content_truncated,
        "semantic_extractors": dict(extractor_counts.most_common()),
        "minimum_instrument_relevance": minimum_instrument_relevance,
        "skipped_news_rows": dict(skipped.most_common()),
        "telegram_policy": {
            "permission_asserted": include_telegram_with_permission,
            "rows_seen": telegram_rows_seen,
            "rows_included": telegram_rows_included,
        },
    }


def _event_item(item: _PreparedNews) -> dict[str, object]:
    row = item.row
    return {
        "id": _event_id(row.news_id),
        "news_id": row.news_id,
        "source_id": row.source_id,
        "source_url": row.url,
        "title": row.title,
        "content": row.content,
        "summary": item.features.rationale,
        "published_at": row.published_at.isoformat(),
        "detected_at": item.feature_available_at.isoformat(),
        "scope": "company",
        "event_type": item.features.event_type.value,
        "tickers": sorted(item.tickers),
        "sectors": sorted(
            {TICKER_SECTORS[ticker] for ticker in item.tickers if ticker in TICKER_SECTORS}
        ),
        "materiality": item.features.materiality,
        "related_signals": [],
    }


def _event_candidates(
    event: Mapping[str, object],
    prepared_by_id: Mapping[str, _PreparedNews],
    *,
    candidate_notes: str,
) -> list[SignalEventCandidate]:
    event_id = str(event["id"])
    member_ids = event["news_ids"]
    if not isinstance(member_ids, list):
        raise ValueError("clustered event news_ids must be a list")
    members = [prepared_by_id[str(news_id)] for news_id in member_ids]
    tickers = sorted({ticker for member in members for ticker in member.tickers})
    candidates = []
    for ticker in tickers:
        ticker_members = [member for member in members if ticker in member.tickers]
        primary = min(
            ticker_members,
            key=lambda item: (
                item.feature_available_at,
                item.row.published_at,
                item.row.news_id,
            ),
        )
        decision_at = primary.feature_available_at
        available = sorted(
            (member for member in ticker_members if member.feature_available_at <= decision_at),
            key=lambda item: (item.feature_available_at, item.row.news_id),
        )
        available_news_ids = tuple(
            dict.fromkeys([primary.row.news_id, *(member.row.news_id for member in available)])
        )
        corroborating_sources = tuple(
            dict.fromkeys(
                member.row.source_id
                for member in available
                if member.row.source_id != primary.row.source_id
            )
        )
        categories = _string_values(primary.row.source_metadata.get("categories"), limit=100)
        rule = primary.rules_by_ticker.get(ticker)
        candidates.append(
            SignalEventCandidate(
                event_id=event_id,
                ticker=ticker,
                news_ids=available_news_ids,
                primary_news_id=primary.row.news_id,
                source_id=primary.row.source_id,
                corroborating_source_ids=corroborating_sources,
                title=primary.row.title,
                content=primary.row.content[:MAX_CANDIDATE_CONTENT_CHARS],
                categories=categories,
                published_at=primary.row.published_at,
                received_at=primary.row.received_at,
                decision_at=decision_at,
                features=SignalFeatureSnapshot(
                    as_of=decision_at,
                    event_type=primary.features.event_type.value,
                    polarity=primary.features.polarity,
                    materiality=primary.features.materiality,
                    novelty=primary.features.novelty,
                    source_quality=SCORING_CONFIG.source_quality.get(
                        primary.row.source_id,
                        SCORING_CONFIG.default_source_quality,
                    ),
                    fact_count=len(primary.features.facts),
                    semantic_extractor_version=primary.features.extractor_version,
                    semantic_feature_set_id=primary.feature_set_id,
                    rule_direction=(rule.direction.value if rule is not None else None),
                    rule_score=(rule.score if rule is not None else None),
                    rule_confidence=(rule.confidence if rule is not None else None),
                    rule_model_version=(rule.model_version if rule is not None else None),
                    rule_config_version=(rule.config_version if rule is not None else None),
                    liquidity_status="unavailable",
                ),
                corporate_action_status="unknown",
                notes=candidate_notes,
            )
        )
    return candidates


def _metadata_tickers(metadata: Mapping[str, object]) -> frozenset[str]:
    values = metadata.get("tickers", ())
    if not isinstance(values, (list, tuple)):
        return frozenset()
    return frozenset(
        ticker for value in values if (ticker := str(value).upper()) in DEFAULT_MOEX_ALIASES
    )


def _string_values(value: object, *, limit: int) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(
        dict.fromkeys(text for item in value if (text := str(item).strip()) and len(text) <= 200)
    )[:limit]


def _event_id(news_id: str) -> str:
    digest = hashlib.sha256(f"signal-event-1.0\0{news_id}".encode()).hexdigest()[:32]
    return f"event_{digest}"


def _validate_candidate_identities(candidates: Iterable[SignalEventCandidate]) -> None:
    candidates = tuple(candidates)
    if candidates:
        signal_candidate_schema_version(candidates)
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        identity = (candidate.event_id, candidate.ticker)
        if identity in seen:
            raise ValueError(
                f"duplicate event/ticker candidate: {candidate.event_id}/{candidate.ticker}"
            )
        seen.add(identity)


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
