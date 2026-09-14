"""Create an immutable benchmark-only derivative of a labeled signal dataset."""

from __future__ import annotations

import argparse
import bisect
import json
import math
import re
import signal
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from eventedge_research import research_artifacts
from eventedge_research.signal_dataset import (
    FOUR_HOUR_HORIZON,
    MAX_OUTCOME_OBSERVATION_LAG,
    SignalDatasetExample,
    load_signal_dataset,
    signal_dataset_sha256,
    write_signal_dataset,
)
from eventedge_research.signal_dataset_builder import (
    MARKET_TIME_ZONE,
    MAX_PRE_EVENT_OBSERVATION_LAG,
    PRE_EVENT_MOMENTUM_HORIZON,
    MarketOpenObservation,
    load_verified_benchmark_archive,
    replace_benchmark_context,
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class _DerivativeBuild:
    input_sha256: str
    examples: list[SignalDatasetExample]
    cohort: list[SignalDatasetExample]
    benchmark: list[MarketOpenObservation]
    benchmark_id: str
    enriched_cohort: list[SignalDatasetExample]
    enriched: list[SignalDatasetExample]
    replacement: dict[str, object]
    temporal_quality: dict[str, object]
    integrity: dict[str, object]


@dataclass(frozen=True)
class _TemporalRowAudit:
    stock_entry_lag_seconds: float
    benchmark_entry_lag_seconds: float | None
    benchmark_target_lag_seconds: float | None
    label_availability_changed: bool
    feature_current_lag_seconds: float | None
    feature_missing_reason: str | None


def _run(args: argparse.Namespace) -> dict[str, object]:
    build = _prepare_derivative(args)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    dataset_path = args.output_dir / "dataset.jsonl"
    write_signal_dataset(dataset_path, build.enriched)
    artifact_version = getattr(args, "artifact_version", None) or args.output_dir.name
    report = _quality_report(args, build, dataset_path, artifact_version)
    quality_path = args.output_dir / "quality-report.json"
    research_artifacts.write_json(quality_path, report)
    _write_completion(args.output_dir, artifact_version, dataset_path, quality_path)
    return report


def _prepare_derivative(args: argparse.Namespace) -> _DerivativeBuild:
    input_sha256 = signal_dataset_sha256(args.dataset)
    expected_input_sha256 = getattr(args, "expected_input_sha256", None)
    if expected_input_sha256 is not None and input_sha256 != expected_input_sha256:
        raise ValueError("input dataset SHA-256 differs from the frozen expectation")

    examples = load_signal_dataset(args.dataset)
    decision_from = getattr(args, "decision_from", None)
    decision_before = getattr(args, "decision_before", None)
    _validate_bounds(decision_from, decision_before)
    cohort = [
        example
        for example in examples
        if _in_decision_window(example.decision_at, decision_from, decision_before)
    ]
    if not cohort:
        raise ValueError("benchmark replacement cohort is empty")
    _validate_expected_cohort_size(cohort, getattr(args, "expected_cohort_rows", None))

    benchmark, benchmark_id = load_verified_benchmark_archive(args.benchmark_dir)
    enriched_cohort, replacement = replace_benchmark_context(
        cohort,
        benchmark,
        benchmark_id=benchmark_id,
    )
    _validate_replacement(
        replacement,
        cohort_rows=len(cohort),
        require_complete=getattr(args, "require_complete_cohort", False),
    )

    temporal_quality = _audit_temporal_compatibility(
        cohort,
        enriched_cohort,
        benchmark,
        maximum_stock_entry_lag_seconds=getattr(args, "maximum_stock_entry_lag_seconds", None),
    )
    enriched_by_id = {example.id: example for example in enriched_cohort}
    enriched = [enriched_by_id.get(example.id, example) for example in examples]
    integrity = _audit_derivative_integrity(
        examples,
        enriched,
        cohort_ids=set(enriched_by_id),
    )

    return _DerivativeBuild(
        input_sha256=input_sha256,
        examples=examples,
        cohort=cohort,
        benchmark=benchmark,
        benchmark_id=benchmark_id,
        enriched_cohort=enriched_cohort,
        enriched=enriched,
        replacement=replacement,
        temporal_quality=temporal_quality,
        integrity=integrity,
    )


def _quality_report(
    args: argparse.Namespace,
    build: _DerivativeBuild,
    dataset_path: Path,
    artifact_version: str,
) -> dict[str, object]:
    benchmark_id = build.benchmark_id
    benchmark_path = args.benchmark_dir / f"{benchmark_id}.jsonl"
    return {
        "schema_version": "signal-dataset-benchmark-derivative-quality-1.0",
        "artifact_version": artifact_version,
        "input_dataset_sha256": build.input_sha256,
        "output_dataset_sha256": signal_dataset_sha256(dataset_path),
        "input_examples": len(build.examples),
        "output_examples": len(build.enriched),
        "cohort": _cohort_report(args, build),
        "benchmark_archive": _benchmark_report(args.benchmark_dir, build, benchmark_path),
        "derived_dataset": _dataset_benchmark_report(build.enriched),
        "benchmark_replacement": build.replacement,
        "temporal_quality": build.temporal_quality,
        "integrity": build.integrity,
        "selection_changed": False,
        "stock_price_return_labels_changed": False,
        "label_availability_changed_rows": build.integrity[
            "label_availability_changed_rows"
        ],
        "selection_used_prices_returns_or_labels": False,
        "production_accessed": False,
        "ydb_accessed": False,
        "source_sha256": {
            "scripts/rebenchmark_signal_dataset.py": signal_dataset_sha256(Path(__file__)),
            "src/eventedge_research/signal_dataset_builder.py": signal_dataset_sha256(
                Path("src/eventedge_research/signal_dataset_builder.py")
            ),
        },
    }


def _cohort_report(args: argparse.Namespace, build: _DerivativeBuild) -> dict[str, object]:
    decision_from = getattr(args, "decision_from", None)
    decision_before = getattr(args, "decision_before", None)
    return {
        "decision_from": decision_from.isoformat() if decision_from is not None else None,
        "decision_before": decision_before.isoformat() if decision_before is not None else None,
        "rows": len(build.cohort),
        "minimum_decision_at": min(row.decision_at for row in build.cohort).isoformat(),
        "maximum_decision_at": max(row.decision_at for row in build.cohort).isoformat(),
        "previous_benchmark_ids": _benchmark_id_counts(build.cohort),
        "derived_benchmark_ids": _benchmark_id_counts(build.enriched_cohort),
    }


def _benchmark_report(
    directory: Path,
    build: _DerivativeBuild,
    benchmark_path: Path,
) -> dict[str, object]:
    return {
        "id": build.benchmark_id,
        "rows": len(build.benchmark),
        "minimum_at": build.benchmark[0].at.isoformat(),
        "maximum_at": build.benchmark[-1].at.isoformat(),
        "data_sha256": signal_dataset_sha256(benchmark_path),
        "complete_sha256": signal_dataset_sha256(directory / "complete.json"),
        "plan_sha256": (
            signal_dataset_sha256(directory / "plan.json")
            if (directory / "plan.json").is_file()
            else None
        ),
        "provider_ids": sorted({row.provider_id for row in build.benchmark}),
        "price_field": "open",
    }


def _dataset_benchmark_report(
    examples: Sequence[SignalDatasetExample],
) -> dict[str, object]:
    return {
        "benchmark_ids": _benchmark_id_counts(examples),
        "abnormal_return_coverage": sum(
            row.outcome_4h.abnormal_return_pct is not None for row in examples
        ),
        "benchmark_pre_event_return_1h_coverage": sum(
            row.features.benchmark_pre_event_return_1h_pct is not None for row in examples
        ),
    }


def _benchmark_id_counts(
    examples: Sequence[SignalDatasetExample],
) -> dict[str, int]:
    return dict(
        sorted(Counter(row.outcome_4h.benchmark_id or "missing" for row in examples).items())
    )


def _write_completion(
    output_directory: Path,
    artifact_version: str,
    dataset_path: Path,
    quality_path: Path,
) -> None:
    research_artifacts.write_json(
        output_directory / "complete.json",
        {
            "schema_version": "signal-dataset-benchmark-derivative-completion-1.0",
            "artifact_version": artifact_version,
            "complete": True,
            "sha256": {
                "dataset.jsonl": signal_dataset_sha256(dataset_path),
                "quality-report.json": signal_dataset_sha256(quality_path),
            },
        },
    )


def _validate_expected_cohort_size(
    cohort: Sequence[SignalDatasetExample],
    expected_rows: int | None,
) -> None:
    if expected_rows is not None and len(cohort) != expected_rows:
        raise ValueError("benchmark replacement cohort row count differs from expectation")


def _validate_replacement(
    replacement: dict[str, object],
    *,
    cohort_rows: int,
    require_complete: bool,
) -> None:
    if replacement["stock_entries_and_price_outcomes_preserved"] is not True:
        raise ValueError("benchmark replacement changed stock outcomes")
    if replacement["outcome_coverage"] == 0:
        raise ValueError("benchmark archive does not overlap dataset outcomes")
    if require_complete and replacement["outcome_coverage"] != cohort_rows:
        raise ValueError("benchmark archive does not cover every cohort outcome")


def _audit_temporal_compatibility(
    before: Sequence[SignalDatasetExample],
    after: Sequence[SignalDatasetExample],
    benchmark: Sequence[MarketOpenObservation],
    *,
    maximum_stock_entry_lag_seconds: int | None,
) -> dict[str, object]:
    if len(before) != len(after):
        raise ValueError("benchmark replacement changed cohort size")
    if maximum_stock_entry_lag_seconds is not None and maximum_stock_entry_lag_seconds <= 0:
        raise ValueError("maximum stock entry lag must be positive")

    ordered = tuple(sorted(benchmark, key=lambda row: row.at))
    timestamps = tuple(row.at for row in ordered)
    audits = [
        _audit_temporal_row(
            source,
            derived,
            ordered,
            timestamps,
            maximum_stock_entry_lag_seconds=maximum_stock_entry_lag_seconds,
        )
        for source, derived in zip(before, after, strict=True)
    ]
    stock_entry_lags = [row.stock_entry_lag_seconds for row in audits]
    benchmark_entry_lags = [
        row.benchmark_entry_lag_seconds
        for row in audits
        if row.benchmark_entry_lag_seconds is not None
    ]
    benchmark_target_lags = [
        row.benchmark_target_lag_seconds
        for row in audits
        if row.benchmark_target_lag_seconds is not None
    ]
    feature_current_lags = [
        row.feature_current_lag_seconds
        for row in audits
        if row.feature_current_lag_seconds is not None
    ]
    availability_changed_rows = sum(row.label_availability_changed for row in audits)
    feature_missing_reasons = Counter(
        row.feature_missing_reason for row in audits if row.feature_missing_reason is not None
    )
    required_minimum = min(row.decision_at for row in before) - (
        PRE_EVENT_MOMENTUM_HORIZON + MAX_PRE_EVENT_OBSERVATION_LAG
    )
    required_maximum = max(row.outcome_4h.target_at for row in before) + (
        MAX_OUTCOME_OBSERVATION_LAG
    )

    return {
        "cohort_rows": len(before),
        "required_minimum_at": required_minimum.isoformat(),
        "required_maximum_at": required_maximum.isoformat(),
        "archive_covers_required_boundaries": (
            ordered[0].at <= required_minimum and ordered[-1].at >= required_maximum
        ),
        "stock_entry": {
            "policy": "stored_first_open_strictly_after_decision",
            "preserved_from_input": True,
            "raw_stock_series_rechecked": False,
            "timing_revalidated": True,
            "maximum_allowed_lag_seconds": maximum_stock_entry_lag_seconds,
            "minimum_lag_seconds": min(stock_entry_lags),
            "maximum_lag_seconds": max(stock_entry_lags),
            "strictly_after_decision_rows": len(stock_entry_lags),
        },
        "benchmark_entry": {
            "policy": "first_1m_open_at_or_after_stored_stock_entry",
            "covered_rows": len(benchmark_entry_lags),
            **_lag_summary(benchmark_entry_lags),
        },
        "benchmark_outcome": {
            "policy": "first_1m_open_at_or_after_entry_plus_4h",
            "horizon_seconds": int(FOUR_HOUR_HORIZON.total_seconds()),
            "maximum_allowed_lag_seconds": int(MAX_OUTCOME_OBSERVATION_LAG.total_seconds()),
            "covered_rows": len(benchmark_target_lags),
            **_lag_summary(benchmark_target_lags),
        },
        "label_availability": {
            "policy": "max_stock_and_benchmark_outcome_observation",
            "changed_rows": availability_changed_rows,
            "verified_rows": len(audits),
        },
        "pre_event_feature": {
            "policy": "last_open_strictly_before_decision_vs_past_1h_endpoint",
            "horizon_seconds": int(PRE_EVENT_MOMENTUM_HORIZON.total_seconds()),
            "maximum_endpoint_lag_seconds": int(MAX_PRE_EVENT_OBSERVATION_LAG.total_seconds()),
            "covered_rows": len(feature_current_lags),
            "missing_reasons": dict(sorted(feature_missing_reasons.items())),
            "all_current_observations_strictly_before_decision": True,
            **_lag_summary(feature_current_lags),
        },
        "lookahead_audit": {
            "selection_was_frozen_before_benchmark_join": True,
            "features_use_only_observations_strictly_before_decision": True,
            "post_decision_benchmark_values_are_label_fields_only": True,
            "label_target_is_entry_plus_exactly_4h": True,
        },
    }


def _audit_temporal_row(
    source: SignalDatasetExample,
    derived: SignalDatasetExample,
    benchmark: Sequence[MarketOpenObservation],
    timestamps: Sequence[datetime],
    *,
    maximum_stock_entry_lag_seconds: int | None,
) -> _TemporalRowAudit:
    if source.id != derived.id:
        raise ValueError("benchmark replacement changed cohort ordering")
    stock_entry_lag = (source.entry_at - source.decision_at).total_seconds()
    if stock_entry_lag <= 0:
        raise ValueError("stored stock entry is not strictly after decision")
    if (
        maximum_stock_entry_lag_seconds is not None
        and stock_entry_lag > maximum_stock_entry_lag_seconds
    ):
        raise ValueError("stored stock entry exceeds the requested protocol lag")
    if source.outcome_4h.target_at != source.entry_at + FOUR_HOUR_HORIZON:
        raise ValueError("stored stock target is not exactly four hours after entry")
    feature_lag, feature_missing_reason = _audit_pre_event_feature(
        source,
        derived,
        benchmark,
        timestamps,
    )
    entry_lag, target_lag = _audit_benchmark_label(
        source,
        derived,
        benchmark,
        timestamps,
    )
    return _TemporalRowAudit(
        stock_entry_lag_seconds=stock_entry_lag,
        benchmark_entry_lag_seconds=entry_lag,
        benchmark_target_lag_seconds=target_lag,
        label_availability_changed=(
            source.label_available_at != derived.label_available_at
        ),
        feature_current_lag_seconds=feature_lag,
        feature_missing_reason=feature_missing_reason,
    )


def _audit_pre_event_feature(
    source: SignalDatasetExample,
    derived: SignalDatasetExample,
    benchmark: Sequence[MarketOpenObservation],
    timestamps: Sequence[datetime],
) -> tuple[float | None, str | None]:
    expected, current_at, missing_reason = _past_only_return(
        benchmark,
        timestamps,
        source.decision_at,
    )
    actual = derived.features.benchmark_pre_event_return_1h_pct
    _require_same_optional_float(actual, expected, "pre-event benchmark")
    if actual is None:
        return None, missing_reason
    if current_at is None or current_at >= source.decision_at:
        raise ValueError("pre-event benchmark feature used look-ahead")
    return (source.decision_at - current_at).total_seconds(), None


def _audit_benchmark_label(
    source: SignalDatasetExample,
    derived: SignalDatasetExample,
    benchmark: Sequence[MarketOpenObservation],
    timestamps: Sequence[datetime],
) -> tuple[float | None, float | None]:
    entry = _first_at_or_after(benchmark, timestamps, source.entry_at)
    target = _first_at_or_after(benchmark, timestamps, source.outcome_4h.target_at)
    expected_return = None
    entry_lag = target_lag = None
    if entry is not None and target is not None:
        candidate_entry_lag = (entry.at - source.entry_at).total_seconds()
        candidate_target_lag = (target.at - source.outcome_4h.target_at).total_seconds()
        if (
            candidate_entry_lag <= MAX_OUTCOME_OBSERVATION_LAG.total_seconds()
            and candidate_target_lag <= MAX_OUTCOME_OBSERVATION_LAG.total_seconds()
            and _compatible(entry, target)
        ):
            expected_return = (target.open / entry.open - 1) * 100
            entry_lag = candidate_entry_lag
            target_lag = candidate_target_lag
            if entry.at <= source.decision_at:
                raise ValueError("benchmark entry is not after decision")
    _require_same_optional_float(
        derived.outcome_4h.benchmark_return_pct,
        expected_return,
        "four-hour benchmark",
    )
    expected_abnormal = (
        source.outcome_4h.return_pct - expected_return if expected_return is not None else None
    )
    _require_same_optional_float(
        derived.outcome_4h.abnormal_return_pct,
        expected_abnormal,
        "abnormal return",
    )
    expected_availability = source.outcome_4h.observed_at
    if expected_return is not None:
        assert target is not None
        expected_availability = max(expected_availability, target.at)
    if derived.label_available_at != expected_availability:
        raise ValueError("label availability differs from the independent timing audit")
    return entry_lag, target_lag


def _audit_derivative_integrity(
    before: Sequence[SignalDatasetExample],
    after: Sequence[SignalDatasetExample],
    *,
    cohort_ids: set[str],
) -> dict[str, object]:
    if len(before) != len(after) or [row.id for row in before] != [row.id for row in after]:
        raise ValueError("benchmark derivative changed row selection or order")
    outside_preserved = 0
    benchmark_only_changes = 0
    availability_changed = 0
    for source, derived in zip(before, after, strict=True):
        if source.id not in cohort_ids:
            if source != derived:
                raise ValueError("benchmark derivative changed a row outside the cohort")
            outside_preserved += 1
            continue
        if _without_benchmark_fields(source) != _without_benchmark_fields(derived):
            raise ValueError("benchmark derivative changed a non-benchmark field")
        availability_changed += source.label_available_at != derived.label_available_at
        benchmark_only_changes += 1
    return {
        "same_row_count": True,
        "same_ordered_ids": True,
        "same_event_ticker_selection": True,
        "rows_outside_cohort_preserved": outside_preserved,
        "cohort_rows_with_only_benchmark_fields_mutable": benchmark_only_changes,
        "stock_entries_and_price_outcomes_preserved": True,
        "label_availability_changed_rows": availability_changed,
    }


def _without_benchmark_fields(example: SignalDatasetExample) -> dict[str, object]:
    payload = example.model_dump(mode="json")
    features = payload["features"]
    outcome = payload["outcome_4h"]
    assert isinstance(features, dict) and isinstance(outcome, dict)
    features.pop("benchmark_pre_event_return_1h_pct", None)
    for name in ("benchmark_id", "benchmark_return_pct", "abnormal_return_pct"):
        outcome.pop(name, None)
    payload.pop("label_available_at", None)
    return payload


def _past_only_return(
    observations: Sequence[MarketOpenObservation],
    timestamps: Sequence[datetime],
    decision_at: datetime,
) -> tuple[float | None, datetime | None, str | None]:
    current_index = bisect.bisect_left(timestamps, decision_at) - 1
    if current_index < 0:
        return None, None, "current_open_missing"
    current = observations[current_index]
    if decision_at - current.at > MAX_PRE_EVENT_OBSERVATION_LAG:
        return None, None, "current_open_stale"
    endpoint = current.at - PRE_EVENT_MOMENTUM_HORIZON
    previous_index = bisect.bisect_right(timestamps, endpoint, hi=current_index) - 1
    if previous_index < 0:
        return None, None, "past_endpoint_missing"
    previous = observations[previous_index]
    if endpoint - previous.at > MAX_PRE_EVENT_OBSERVATION_LAG:
        return None, None, "past_endpoint_stale"
    if (
        current.at.astimezone(MARKET_TIME_ZONE).date()
        != previous.at.astimezone(MARKET_TIME_ZONE).date()
    ):
        return None, None, "past_endpoint_crosses_market_date"
    if not _compatible(current, previous):
        return None, None, "incompatible_observation_provenance"
    return (current.open / previous.open - 1) * 100, current.at, None


def _first_at_or_after(
    observations: Sequence[MarketOpenObservation],
    timestamps: Sequence[datetime],
    target_at: datetime,
) -> MarketOpenObservation | None:
    index = bisect.bisect_left(timestamps, target_at)
    return observations[index] if index < len(observations) else None


def _compatible(left: MarketOpenObservation, right: MarketOpenObservation) -> bool:
    return (
        left.provider_id == right.provider_id
        and left.label_source == right.label_source
        and left.adjusted == right.adjusted
    )


def _require_same_optional_float(
    actual: float | None,
    expected: float | None,
    name: str,
) -> None:
    if actual is None or expected is None:
        if actual is not expected:
            raise ValueError(f"{name} coverage differs from the independent timing audit")
        return
    if not math.isclose(actual, expected, rel_tol=0, abs_tol=1e-12):
        raise ValueError(f"{name} differs from the independent timing audit")


def _lag_summary(values: Sequence[float]) -> dict[str, float | None]:
    return {
        "minimum_lag_seconds": min(values) if values else None,
        "maximum_lag_seconds": max(values) if values else None,
    }


def _validate_bounds(
    decision_from: datetime | None,
    decision_before: datetime | None,
) -> None:
    for name, value in (("decision_from", decision_from), ("decision_before", decision_before)):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError(f"{name} must include a timezone")
    if (
        decision_from is not None
        and decision_before is not None
        and decision_from >= decision_before
    ):
        raise ValueError("decision window must be non-empty and ordered")


def _in_decision_window(
    value: datetime,
    decision_from: datetime | None,
    decision_before: datetime | None,
) -> bool:
    return (decision_from is None or value >= decision_from) and (
        decision_before is None or value < decision_before
    )


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed


def _sha256(value: str) -> str:
    normalized = value.strip().lower()
    if not _SHA256_PATTERN.fullmatch(normalized):
        raise argparse.ArgumentTypeError("expected a lowercase SHA-256 digest")
    return normalized


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--benchmark-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--artifact-version")
    parser.add_argument("--decision-from", type=_aware_datetime)
    parser.add_argument("--decision-before", type=_aware_datetime)
    parser.add_argument("--expected-input-sha256", type=_sha256)
    parser.add_argument("--expected-cohort-rows", type=_positive_int)
    parser.add_argument("--require-complete-cohort", action="store_true")
    parser.add_argument("--maximum-stock-entry-lag-seconds", type=_positive_int)
    args = parser.parse_args()
    previous = signal.signal(signal.SIGALRM, research_artifacts.raise_timeout)
    signal.alarm(1_800)
    try:
        print(json.dumps(_run(args), ensure_ascii=False, sort_keys=True), flush=True)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


if __name__ == "__main__":
    main()
