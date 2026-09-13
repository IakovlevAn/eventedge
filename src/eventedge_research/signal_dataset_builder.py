"""Build reproducible event labels with explicit entry and outcome timing limits."""

from __future__ import annotations

import bisect
import hashlib
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eventedge_research.research_artifacts import iter_jsonl_objects, read_json
from eventedge_research.signal_dataset import (
    FOUR_HOUR_HORIZON,
    MAX_OUTCOME_OBSERVATION_LAG,
    DatasetId,
    MarketOutcomeObservation,
    SignalDatasetExample,
    SignalFeatureSnapshot,
    SignalLabelSource,
    Ticker,
    signal_dataset_sha256,
)
from eventedge_research.signal_dataset_v2 import SignalDatasetExampleV2

PRE_EVENT_MOMENTUM_HORIZON = timedelta(hours=1)
PRE_EVENT_RETURN_1D_HORIZON = timedelta(days=1)
PRE_EVENT_RETURN_5D_HORIZON = timedelta(days=5)
MAX_PRE_EVENT_OBSERVATION_LAG = timedelta(minutes=20)
MAX_LONG_PRE_EVENT_OBSERVATION_LAG = timedelta(days=4)
MAX_ENTRY_OBSERVATION_LAG = timedelta(days=10)
MARKET_TIME_ZONE = ZoneInfo("Europe/Moscow")
SUPPORTED_BENCHMARK_IDS = frozenset({"IMOEX", "IMOEX2"})
MAXIMUM_MARKET_OPEN_ROWS = 2_000_000
_ASSUMED_RECEIPT_PROVENANCE_PATTERN = re.compile(
    r"^publication_plus_assumed_([1-9]|[1-5][0-9]|60)_minutes$"
)


class SignalEventCandidate(BaseModel):
    """One reviewed, outcome-free event/ticker row awaiting market labels."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    schema_version: Literal["signal-event-candidate-1.0"] = "signal-event-candidate-1.0"
    event_id: DatasetId
    ticker: Ticker
    news_ids: Annotated[tuple[DatasetId, ...], Field(min_length=1, max_length=100)]
    primary_news_id: DatasetId
    source_id: Annotated[str, Field(min_length=1, max_length=100)]
    corroborating_source_ids: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=100)], ...],
        Field(max_length=100),
    ] = ()
    title: Annotated[str, Field(min_length=1, max_length=2_000)]
    content: Annotated[str, Field(max_length=50_000)] = ""
    categories: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=200)], ...],
        Field(max_length=100),
    ] = ()
    published_at: datetime
    received_at: datetime
    decision_at: datetime
    features: SignalFeatureSnapshot
    corporate_action_status: Literal["unknown", "none", "adjusted"]
    overlapping_event_ids: Annotated[tuple[DatasetId, ...], Field(max_length=100)] = ()
    notes: Annotated[str, Field(max_length=2_000)] = ""

    @model_validator(mode="after")
    def validate_candidate(self) -> SignalEventCandidate:
        for name, value in (
            ("published_at", self.published_at),
            ("received_at", self.received_at),
            ("decision_at", self.decision_at),
        ):
            _require_aware(name, value)
        if not self.published_at <= self.received_at <= self.decision_at:
            raise ValueError("candidate timestamps must be chronological")
        if self.features.as_of > self.decision_at:
            raise ValueError("candidate features cannot postdate decision_at")
        if self.primary_news_id not in self.news_ids:
            raise ValueError("primary_news_id must be included in candidate news_ids")
        _require_unique("news_ids", self.news_ids)
        _require_unique("corroborating_source_ids", self.corroborating_source_ids)
        _require_unique("overlapping_event_ids", self.overlapping_event_ids)
        if self.source_id in self.corroborating_source_ids:
            raise ValueError("source_id cannot be repeated in corroborating_source_ids")
        if self.event_id in self.overlapping_event_ids:
            raise ValueError("event_id cannot overlap itself")
        return self


class SignalEventCandidateV2(SignalEventCandidate):
    """Telegram candidate with explicit current-view and admission provenance."""

    schema_version: Literal["signal-event-candidate-2.0"] = "signal-event-candidate-2.0"
    corporate_action_status: Literal["unknown"] = "unknown"
    event_group_id: DatasetId
    source_url: str = Field(pattern=r"^https://t\.me/")
    retrieved_at: datetime
    admission_version: Annotated[
        str,
        Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,99}$"),
    ]
    analysis_title: Annotated[str, Field(min_length=1, max_length=500)]
    analysis_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_provenance: str = Field(pattern=_ASSUMED_RECEIPT_PROVENANCE_PATTERN.pattern)
    text_provenance: Literal["no_edit_marker_current_view_not_first_snapshot"]

    @model_validator(mode="after")
    def validate_archive_provenance(self) -> SignalEventCandidateV2:
        """Bind the explicit receipt assumption to candidate timestamps."""
        _require_aware("retrieved_at", self.retrieved_at)
        if self.retrieved_at < self.published_at:
            raise ValueError("candidate archive retrieval precedes publication")
        match = _ASSUMED_RECEIPT_PROVENANCE_PATTERN.fullmatch(self.receipt_provenance)
        if match is None:
            raise ValueError("invalid candidate receipt provenance")
        assumed_lag = timedelta(minutes=int(match.group(1)))
        if self.received_at != self.published_at + assumed_lag:
            raise ValueError("candidate receipt does not match its explicit assumption")
        if self.decision_at != self.received_at:
            raise ValueError("archive candidate decision must equal assumed receipt")
        if self.analysis_title != self.title:
            raise ValueError("analysis_title must match the analyzed candidate title")
        if self.analysis_content_sha256 != hashlib.sha256(
            self.content.encode("utf-8")
        ).hexdigest():
            raise ValueError("analysis content fingerprint does not match candidate content")
        return self


class MarketOpenObservation(BaseModel):
    """A locally supplied market open known at the candle timestamp."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    schema_version: Literal["market-open-observation-1.0"] = "market-open-observation-1.0"
    ticker: Ticker
    at: datetime
    open: Annotated[float, Field(gt=0)]
    provider_id: Annotated[str, Field(min_length=1, max_length=100)]
    adjusted: bool
    label_source: SignalLabelSource

    @model_validator(mode="after")
    def validate_timestamp(self) -> MarketOpenObservation:
        _require_aware("market observation at", self.at)
        return self


@dataclass(frozen=True)
class _MarketSeries:
    timestamps: tuple[datetime, ...]
    observations: tuple[MarketOpenObservation, ...]


@dataclass(frozen=True)
class _BenchmarkOutcome:
    """Benchmark return and the time when that return became observable."""

    return_pct: float
    observed_at: datetime


def load_verified_benchmark_archive(
    directory: Path,
) -> tuple[list[MarketOpenObservation], str]:
    """Load one sealed MOEX benchmark archive with fixed provenance."""
    complete = read_json(directory / "complete.json")
    benchmark_id = complete.get("ticker")
    if benchmark_id not in SUPPORTED_BENCHMARK_IDS:
        raise ValueError("unsupported MOEX benchmark")
    path = directory / f"{benchmark_id}.jsonl"
    resolved_directory = directory.resolve()
    resolved_path = path.resolve()
    if (
        not resolved_path.is_relative_to(resolved_directory)
        or not resolved_path.is_file()
        or complete.get("sha256") != signal_dataset_sha256(resolved_path)
    ):
        raise ValueError("MOEX minute benchmark is incomplete or changed")
    rows = load_market_open_observations(resolved_path)
    if any(
        row.ticker != benchmark_id
        or row.provider_id != "moex-iss-sndx-index-1m"
        for row in rows
    ):
        raise ValueError("MOEX minute benchmark provenance differs")
    return rows, str(benchmark_id)


def load_signal_event_candidates(path: Path) -> list[SignalEventCandidate]:
    """Load one homogeneous candidate schema and reject duplicate units."""
    candidates: list[SignalEventCandidate] = []
    seen: set[tuple[str, str]] = set()
    for line_number, payload in enumerate(iter_jsonl_objects(path), 1):
        try:
            schema_version = payload.get("schema_version")
            if schema_version in {None, "signal-event-candidate-1.0"}:
                candidate = SignalEventCandidate.model_validate(payload)
            elif schema_version == "signal-event-candidate-2.0":
                candidate = SignalEventCandidateV2.model_validate(payload)
            else:
                raise ValueError(
                    f"unsupported signal event candidate schema: {schema_version!r}"
                )
        except Exception as error:
            raise ValueError(
                f"invalid signal event candidate at row {line_number}: {error}"
            ) from error
        identity = (candidate.event_id, candidate.ticker)
        if identity in seen:
            raise ValueError(
                f"duplicate event/ticker candidate at row {line_number}: "
                f"{candidate.event_id}/{candidate.ticker}"
            )
        seen.add(identity)
        candidates.append(candidate)
    if not candidates:
        raise ValueError("signal event candidates are empty")
    signal_candidate_schema_version(candidates)
    return candidates


def signal_candidate_schema_version(
    candidates: Sequence[SignalEventCandidate],
) -> str:
    """Return the single supported schema shared by candidate rows."""
    versions = {candidate.schema_version for candidate in candidates}
    if not versions:
        raise ValueError("signal event candidates are empty")
    if len(versions) != 1:
        raise ValueError("cannot use mixed signal event candidate schema versions")
    version = next(iter(versions))
    if version not in {
        "signal-event-candidate-1.0",
        "signal-event-candidate-2.0",
    }:
        raise ValueError(f"unsupported signal event candidate schema: {version}")
    if version == "signal-event-candidate-2.0" and not all(
        isinstance(candidate, SignalEventCandidateV2) for candidate in candidates
    ):
        raise ValueError("version 2 rows must use SignalEventCandidateV2")
    return version


def load_market_open_observations(
    path: Path,
    *,
    allow_synthetic: bool = False,
) -> list[MarketOpenObservation]:
    """Load local candle opens with explicit provider and label provenance."""
    observations: list[MarketOpenObservation] = []
    seen: set[tuple[str, datetime]] = set()
    for line_number, payload in enumerate(
        iter_jsonl_objects(path, maximum_rows=MAXIMUM_MARKET_OPEN_ROWS),
        1,
    ):
        try:
            observation = MarketOpenObservation.model_validate(payload)
        except Exception as error:
            raise ValueError(
                f"invalid market open observation at row {line_number}: {error}"
            ) from error
        if observation.label_source is SignalLabelSource.SYNTHETIC_TEST and not allow_synthetic:
            raise ValueError(
                f"synthetic market observation at row {line_number}; "
                "synthetic labels are allowed only for tests"
            )
        identity = (observation.ticker, observation.at)
        if identity in seen:
            raise ValueError(
                f"duplicate market observation at row {line_number}: "
                f"{observation.ticker}/{observation.at.isoformat()}"
            )
        seen.add(identity)
        observations.append(observation)
    if not observations:
        raise ValueError("market open observations are empty")
    return observations


def build_signal_dataset(
    candidates: list[SignalEventCandidate],
    observations: list[MarketOpenObservation],
    *,
    benchmark_observations: list[MarketOpenObservation] | None = None,
    benchmark_id: str | None = None,
    maximum_entry_lag: timedelta = MAX_ENTRY_OBSERVATION_LAG,
) -> tuple[list[SignalDatasetExample], dict[str, object]]:
    """Join candidates to opens, without waiting deliberately before entry.

    The default preserves the historical next-session research protocol. Use
    one-minute opens and a 60-second limit for immediate-after-decision replay;
    this does not establish whether the candidate's decision time was observed.
    The separate outcome tolerance never delays the decision or entry.
    """
    if not timedelta(0) < maximum_entry_lag <= MAX_ENTRY_OBSERVATION_LAG:
        raise ValueError("maximum entry lag must be positive and no more than 10 days")
    candidate_schema_version = signal_candidate_schema_version(candidates)
    _validate_candidate_identities(candidates)
    dataset_schema_version = (
        "signal-dataset-example-2.0"
        if candidate_schema_version == "signal-event-candidate-2.0"
        else "signal-dataset-example-1.0"
    )
    market_by_ticker = _market_index(observations)
    benchmark_series = _benchmark_series(benchmark_observations, benchmark_id)
    examples: list[SignalDatasetExample] = []
    skipped: Counter[str] = Counter()
    skipped_samples: list[dict[str, str]] = []
    for candidate in sorted(
        candidates,
        key=lambda item: (item.decision_at, item.event_id, item.ticker),
    ):
        result = _build_example(
            candidate,
            market_by_ticker.get(candidate.ticker),
            benchmark_series=benchmark_series,
            benchmark_id=benchmark_id,
            maximum_entry_lag=maximum_entry_lag,
        )
        if isinstance(result, str):
            skipped[result] += 1
            if len(skipped_samples) < 100:
                skipped_samples.append(
                    {
                        "event_id": candidate.event_id,
                        "ticker": candidate.ticker,
                        "reason": result,
                    }
                )
        else:
            examples.append(result)
    report = {
        "schema_version": "signal-dataset-build-report-1.0",
        "candidate_schema_version": candidate_schema_version,
        "market_observation_schema_version": "market-open-observation-1.0",
        "dataset_schema_version": dataset_schema_version,
        "candidate_event_tickers": len(candidates),
        "candidate_events": len({candidate.event_id for candidate in candidates}),
        "market_observations": len(observations),
        "built_event_tickers": len(examples),
        "skipped_event_tickers": sum(skipped.values()),
        "skip_reasons": dict(sorted(skipped.items())),
        "skipped_samples": skipped_samples,
        "label_sources": sorted({example.label_source.value for example in examples}),
        "market_providers": sorted({observation.provider_id for observation in observations}),
        "benchmark": (
            {
                "id": benchmark_id,
                "observations": len(benchmark_observations or ()),
                "providers": sorted(
                    {observation.provider_id for observation in benchmark_observations or ()}
                ),
                "outcome_coverage": sum(
                    example.outcome_4h.benchmark_id is not None for example in examples
                ),
                "pre_event_return_1h_coverage": sum(
                    example.features.benchmark_pre_event_return_1h_pct is not None
                    for example in examples
                ),
            }
            if benchmark_series is not None
            else None
        ),
        "feature_coverage": {
            feature_name: sum(
                getattr(example.features, feature_name) is not None for example in examples
            )
            for feature_name in (
                "pre_event_return_1h_pct",
                "pre_event_return_1d_pct",
                "pre_event_return_5d_pct",
            )
        },
        "feature_policy": {
            "pre_event_return_1h_pct": {
                "current": "last_open_strictly_before_decision",
                "horizon_seconds": int(PRE_EVENT_MOMENTUM_HORIZON.total_seconds()),
                "maximum_endpoint_lag_seconds": int(MAX_PRE_EVENT_OBSERVATION_LAG.total_seconds()),
                "same_market_date": True,
                "price_field": "open",
            },
            "pre_event_return_1d_pct": {
                "current": "last_open_strictly_before_decision",
                "horizon_seconds": int(PRE_EVENT_RETURN_1D_HORIZON.total_seconds()),
                "maximum_current_lag_seconds": int(MAX_PRE_EVENT_OBSERVATION_LAG.total_seconds()),
                "maximum_past_endpoint_lag_seconds": int(
                    MAX_LONG_PRE_EVENT_OBSERVATION_LAG.total_seconds()
                ),
                "past_endpoint": "last_open_at_or_before_horizon",
                "price_field": "open",
            },
            "pre_event_return_5d_pct": {
                "current": "last_open_strictly_before_decision",
                "horizon_seconds": int(PRE_EVENT_RETURN_5D_HORIZON.total_seconds()),
                "maximum_current_lag_seconds": int(MAX_PRE_EVENT_OBSERVATION_LAG.total_seconds()),
                "maximum_past_endpoint_lag_seconds": int(
                    MAX_LONG_PRE_EVENT_OBSERVATION_LAG.total_seconds()
                ),
                "past_endpoint": "last_open_at_or_before_horizon",
                "price_field": "open",
            },
        },
        "outcome_policy": {
            "entry": "first_open_strictly_after_decision",
            "maximum_entry_lag_seconds": int(maximum_entry_lag.total_seconds()),
            "horizon_seconds": int(FOUR_HOUR_HORIZON.total_seconds()),
            "maximum_observation_lag_seconds": int(MAX_OUTCOME_OBSERVATION_LAG.total_seconds()),
            "price_field": "open",
        },
    }
    return examples, report


def add_pre_event_market_context(
    examples: Sequence[SignalDatasetExample],
    observations: list[MarketOpenObservation],
    *,
    benchmark_observations: list[MarketOpenObservation] | None = None,
    benchmark_id: str | None = None,
) -> tuple[list[SignalDatasetExample], dict[str, object]]:
    """Join causal pre-decision returns without changing stored entries or labels.

    This supports immutable external cohorts whose outcome observations must
    remain identical to their source. Missing or stale context stays explicit
    instead of removing the labeled row.
    """
    if not examples:
        raise ValueError("market context enrichment requires labeled examples")
    market_by_ticker = _market_index(observations)
    benchmark_series = _benchmark_series(benchmark_observations, benchmark_id)
    enriched = []
    for example in examples:
        series = market_by_ticker.get(example.ticker)
        ticker_context = (
            _pre_event_returns(example.decision_at, series)
            if series is not None
            else {
                "pre_event_return_1h_pct": None,
                "pre_event_return_1d_pct": None,
                "pre_event_return_5d_pct": None,
            }
        )
        benchmark_context = (
            _pre_event_return_1h_pct(example.decision_at, benchmark_series)
            if benchmark_series is not None
            else None
        )
        enriched.append(
            example.model_copy(
                update={
                    "features": example.features.model_copy(
                        update={
                            **ticker_context,
                            "benchmark_pre_event_return_1h_pct": benchmark_context,
                        }
                    )
                }
            )
        )
    fields = (
        "pre_event_return_1h_pct",
        "pre_event_return_1d_pct",
        "pre_event_return_5d_pct",
        "benchmark_pre_event_return_1h_pct",
    )
    return enriched, {
        "schema_version": "pre-event-market-context-join-1.0",
        "examples": len(enriched),
        "feature_coverage": {
            name: sum(getattr(example.features, name) is not None for example in enriched)
            for name in fields
        },
        "ticker_observations": len(observations),
        "benchmark_id": benchmark_id,
        "benchmark_observations": len(benchmark_observations or ()),
        "stored_entries_and_outcomes_preserved": all(
            (
                before.entry_at,
                before.entry_price,
                before.outcome_4h,
                before.label_available_at,
            )
            == (
                after.entry_at,
                after.entry_price,
                after.outcome_4h,
                after.label_available_at,
            )
            for before, after in zip(examples, enriched, strict=True)
        ),
    }


def replace_benchmark_context(
    examples: Sequence[SignalDatasetExample],
    benchmark_observations: list[MarketOpenObservation],
    *,
    benchmark_id: str,
) -> tuple[list[SignalDatasetExample], dict[str, object]]:
    """Replace benchmark-derived fields while preserving stock price outcomes."""
    if not examples:
        raise ValueError("benchmark replacement requires labeled examples")
    benchmark_series = _benchmark_series(benchmark_observations, benchmark_id)
    if benchmark_series is None:
        raise ValueError("benchmark replacement requires a benchmark series")

    enriched = []
    for example in examples:
        benchmark_outcome = _benchmark_outcome(
            example.entry_at,
            example.outcome_4h.target_at,
            benchmark_series,
        )
        benchmark_return = (
            benchmark_outcome.return_pct if benchmark_outcome is not None else None
        )
        benchmark_fields = {
            "benchmark_id": benchmark_id if benchmark_return is not None else None,
            "benchmark_return_pct": benchmark_return,
            "abnormal_return_pct": (
                example.outcome_4h.return_pct - benchmark_return
                if benchmark_return is not None
                else None
            ),
        }
        enriched.append(
            example.model_copy(
                update={
                    "features": example.features.model_copy(
                        update={
                            "benchmark_pre_event_return_1h_pct": (
                                _pre_event_return_1h_pct(
                                    example.decision_at,
                                    benchmark_series,
                                )
                            )
                        }
                    ),
                    "outcome_4h": example.outcome_4h.model_copy(
                        update=benchmark_fields
                    ),
                    "label_available_at": max(
                        example.outcome_4h.observed_at,
                        benchmark_outcome.observed_at,
                    )
                    if benchmark_outcome is not None
                    else example.outcome_4h.observed_at,
                }
            )
        )

    return enriched, {
        "schema_version": "benchmark-context-replacement-1.0",
        "examples": len(enriched),
        "benchmark_id": benchmark_id,
        "benchmark_observations": len(benchmark_observations),
        "previous_benchmark_ids": dict(
            Counter(example.outcome_4h.benchmark_id or "missing" for example in examples)
        ),
        "outcome_coverage": sum(
            example.outcome_4h.benchmark_return_pct is not None for example in enriched
        ),
        "pre_event_return_1h_coverage": sum(
            example.features.benchmark_pre_event_return_1h_pct is not None
            for example in enriched
        ),
        "stock_entries_and_price_outcomes_preserved": all(
            _stock_outcome_identity(before) == _stock_outcome_identity(after)
            for before, after in zip(examples, enriched, strict=True)
        ),
        "label_availability_changed_rows": sum(
            before.label_available_at != after.label_available_at
            for before, after in zip(examples, enriched, strict=True)
        ),
    }


def _stock_outcome_identity(example: SignalDatasetExample) -> tuple[object, ...]:
    """Return fields that a benchmark-only replacement must not change."""
    return (
        example.entry_at,
        example.entry_price,
        example.outcome_4h.target_at,
        example.outcome_4h.observed_at,
        example.outcome_4h.price,
        example.outcome_4h.return_pct,
        example.label_source,
    )


def _build_example(
    candidate: SignalEventCandidate,
    series: _MarketSeries | None,
    *,
    benchmark_series: _MarketSeries | None,
    benchmark_id: str | None,
    maximum_entry_lag: timedelta,
) -> SignalDatasetExample | str:
    if series is None:
        return "ticker_has_no_market_observations"
    entry_index = bisect.bisect_right(series.timestamps, candidate.decision_at)
    if entry_index == len(series.observations):
        return "entry_open_unavailable"
    entry = series.observations[entry_index]
    if entry.at - candidate.decision_at > maximum_entry_lag:
        return "entry_open_missed_window"
    target_at = entry.at + FOUR_HOUR_HORIZON
    outcome_index = bisect.bisect_left(
        series.timestamps,
        target_at,
        lo=entry_index + 1,
    )
    if outcome_index == len(series.observations):
        return "outcome_open_unavailable"
    outcome = series.observations[outcome_index]
    if outcome.at - target_at > MAX_OUTCOME_OBSERVATION_LAG:
        return "outcome_open_missed_window"
    if entry.provider_id != outcome.provider_id:
        return "mixed_market_providers"
    if entry.label_source != outcome.label_source:
        return "mixed_label_sources"
    if entry.adjusted != outcome.adjusted:
        return "mixed_adjustment_status"
    if candidate.corporate_action_status == "adjusted" and not entry.adjusted:
        return "unadjusted_corporate_action"
    return_pct = (outcome.open / entry.open - 1) * 100
    pre_event_returns = _pre_event_returns(candidate.decision_at, series)
    benchmark_pre_event_return_1h_pct = (
        _pre_event_return_1h_pct(candidate.decision_at, benchmark_series)
        if benchmark_series is not None
        else None
    )
    benchmark_outcome = (
        _benchmark_outcome(entry.at, target_at, benchmark_series)
        if benchmark_series is not None
        else None
    )
    benchmark_return_pct = (
        benchmark_outcome.return_pct if benchmark_outcome is not None else None
    )
    values: dict[str, object] = {
        "id": _example_id(candidate),
        "event_id": candidate.event_id,
        "ticker": candidate.ticker,
        "news_ids": candidate.news_ids,
        "primary_news_id": candidate.primary_news_id,
        "source_id": candidate.source_id,
        "corroborating_source_ids": candidate.corroborating_source_ids,
        "title": candidate.title,
        "content": candidate.content,
        "categories": candidate.categories,
        "published_at": candidate.published_at,
        "received_at": candidate.received_at,
        "decision_at": candidate.decision_at,
        "entry_at": entry.at,
        "entry_price": entry.open,
        "features": candidate.features.model_copy(
            update={
                **pre_event_returns,
                "benchmark_pre_event_return_1h_pct": (
                    benchmark_pre_event_return_1h_pct
                ),
            }
        ),
        "outcome_4h": MarketOutcomeObservation(
            target_at=target_at,
            observed_at=outcome.at,
            price=outcome.open,
            return_pct=return_pct,
            benchmark_id=benchmark_id if benchmark_return_pct is not None else None,
            benchmark_return_pct=benchmark_return_pct,
            abnormal_return_pct=(
                return_pct - benchmark_return_pct
                if benchmark_return_pct is not None
                else None
            ),
        ),
        "label_available_at": (
            max(outcome.at, benchmark_outcome.observed_at)
            if benchmark_outcome is not None
            else outcome.at
        ),
        "label_source": outcome.label_source,
        "corporate_action_status": candidate.corporate_action_status,
        "overlapping_event_ids": candidate.overlapping_event_ids,
        "notes": candidate.notes,
    }
    if isinstance(candidate, SignalEventCandidateV2):
        return SignalDatasetExampleV2.model_validate(
            {
                **values,
                "event_group_id": candidate.event_group_id,
                "source_url": candidate.source_url,
                "retrieved_at": candidate.retrieved_at,
                "admission_version": candidate.admission_version,
                "analysis_title": candidate.analysis_title,
                "analysis_content_sha256": candidate.analysis_content_sha256,
                "receipt_provenance": candidate.receipt_provenance,
                "text_provenance": candidate.text_provenance,
            }
        )
    return SignalDatasetExample.model_validate(values)


def _pre_event_return_1h_pct(
    decision_at: datetime,
    series: _MarketSeries,
) -> float | None:
    return _pre_event_return_pct(
        decision_at,
        series,
        horizon=PRE_EVENT_MOMENTUM_HORIZON,
        maximum_past_endpoint_lag=MAX_PRE_EVENT_OBSERVATION_LAG,
        require_same_market_date=True,
    )


def _pre_event_returns(
    decision_at: datetime,
    series: _MarketSeries,
) -> dict[str, float | None]:
    values = {"pre_event_return_1h_pct": _pre_event_return_1h_pct(decision_at, series)}
    for name, horizon in (
        ("pre_event_return_1d_pct", PRE_EVENT_RETURN_1D_HORIZON),
        ("pre_event_return_5d_pct", PRE_EVENT_RETURN_5D_HORIZON),
    ):
        values[name] = _pre_event_return_pct(
            decision_at,
            series,
            horizon=horizon,
            maximum_past_endpoint_lag=MAX_LONG_PRE_EVENT_OBSERVATION_LAG,
            require_same_market_date=False,
        )
    return values


def _pre_event_return_pct(
    decision_at: datetime,
    series: _MarketSeries,
    *,
    horizon: timedelta,
    maximum_past_endpoint_lag: timedelta,
    require_same_market_date: bool,
) -> float | None:
    current_index = bisect.bisect_left(series.timestamps, decision_at) - 1
    if current_index < 0:
        return None
    current = series.observations[current_index]
    if decision_at - current.at > MAX_PRE_EVENT_OBSERVATION_LAG:
        return None

    target_at = current.at - horizon
    previous_index = (
        bisect.bisect_right(
            series.timestamps,
            target_at,
            hi=current_index,
        )
        - 1
    )
    if previous_index < 0:
        return None
    previous = series.observations[previous_index]
    if target_at - previous.at > maximum_past_endpoint_lag:
        return None
    if require_same_market_date and (
        current.at.astimezone(MARKET_TIME_ZONE).date()
        != previous.at.astimezone(MARKET_TIME_ZONE).date()
    ):
        return None
    if current.provider_id != previous.provider_id:
        return None
    if current.label_source != previous.label_source:
        return None
    if current.adjusted != previous.adjusted:
        return None
    return (current.open / previous.open - 1) * 100


def _benchmark_outcome(
    entry_at: datetime,
    target_at: datetime,
    series: _MarketSeries,
) -> _BenchmarkOutcome | None:
    entry = _first_timely_observation(series, entry_at)
    outcome = _first_timely_observation(series, target_at)
    if entry is None or outcome is None:
        return None
    if entry.provider_id != outcome.provider_id:
        return None
    if entry.label_source != outcome.label_source:
        return None
    if entry.adjusted != outcome.adjusted:
        return None
    return _BenchmarkOutcome(
        return_pct=(outcome.open / entry.open - 1) * 100,
        observed_at=outcome.at,
    )


def _first_timely_observation(
    series: _MarketSeries,
    target_at: datetime,
) -> MarketOpenObservation | None:
    index = bisect.bisect_left(series.timestamps, target_at)
    if index == len(series.observations):
        return None
    observation = series.observations[index]
    if observation.at - target_at > MAX_OUTCOME_OBSERVATION_LAG:
        return None
    return observation


def _market_index(
    observations: list[MarketOpenObservation],
) -> dict[str, _MarketSeries]:
    grouped: dict[str, list[MarketOpenObservation]] = defaultdict(list)
    seen: set[tuple[str, datetime]] = set()
    for observation in observations:
        identity = (observation.ticker, observation.at)
        if identity in seen:
            raise ValueError(
                f"duplicate market observation: {observation.ticker}/{observation.at.isoformat()}"
            )
        seen.add(identity)
        grouped[observation.ticker].append(observation)
    result = {}
    for ticker, rows in grouped.items():
        ordered = tuple(sorted(rows, key=lambda observation: observation.at))
        result[ticker] = _MarketSeries(
            timestamps=tuple(observation.at for observation in ordered),
            observations=ordered,
        )
    return result


def _benchmark_series(
    observations: list[MarketOpenObservation] | None,
    benchmark_id: str | None,
) -> _MarketSeries | None:
    if (observations is None) != (benchmark_id is None):
        raise ValueError("benchmark observations and benchmark_id must be supplied together")
    if observations is None or benchmark_id is None:
        return None
    indexed = _market_index(observations)
    if set(indexed) != {benchmark_id}:
        raise ValueError("benchmark observations must contain only benchmark_id")
    return indexed[benchmark_id]


def _validate_candidate_identities(candidates: list[SignalEventCandidate]) -> None:
    identities = {(candidate.event_id, candidate.ticker) for candidate in candidates}
    if len(identities) != len(candidates):
        raise ValueError("event candidates contain duplicate event/ticker pairs")


def _example_id(candidate: SignalEventCandidate) -> str:
    dataset_schema_version = (
        "signal-dataset-example-2.0"
        if isinstance(candidate, SignalEventCandidateV2)
        else "signal-dataset-example-1.0"
    )
    identity = "\0".join(
        (
            dataset_schema_version,
            candidate.event_id,
            candidate.ticker,
            candidate.decision_at.isoformat(),
        )
    )
    return f"sigex_{hashlib.sha256(identity.encode()).hexdigest()[:32]}"


def _require_unique(name: str, values: tuple[str, ...]) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"candidate {name} must be unique")


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
