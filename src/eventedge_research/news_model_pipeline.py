"""Prepare reproducible raw-derived inputs for portable EventEdge news models."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eventedge_research.news_model_features import (
    MinuteOpenSeries,
    NewsFeatureRow,
    build_dataset_feature_rows,
    build_decision_feature_rows,
    remaining_four_hour_outcome,
    serialize_feature_row,
)
from eventedge_research.research_artifacts import write_json, write_jsonl
from eventedge_research.signal_dataset import (
    SignalDatasetExample,
    load_signal_dataset,
    signal_dataset_schema_version,
    signal_dataset_sha256,
    signal_information_group_id,
)
from eventedge_research.signal_dataset_builder import (
    MarketOpenObservation,
    SignalEventCandidate,
    SignalEventCandidateV2,
    build_signal_dataset,
    load_market_open_observations,
)
from eventedge_research.signal_dataset_v2 import SignalDatasetExampleV2

MAXIMUM_EXAMPLES = 20_000
DEFAULT_EMBARGO = timedelta(hours=72)


def prepare_news_model_dataset(
    dataset_path: Path,
    stock_open_paths: Sequence[Path],
    benchmark_open_path: Path,
    output_directory: Path,
    *,
    validation_from: datetime,
    evaluation_from: datetime,
    evaluation_until: datetime,
    benchmark_id: str = "IMOEX2",
    embargo: timedelta = DEFAULT_EMBARGO,
    allow_synthetic: bool = False,
) -> dict[str, object]:
    """Build a generic outcome-free fit bundle from one homogeneous dataset.

    Unlike the locked historical benchmark, this entrypoint accepts only rows
    reproducible by the current candidate and market-label contracts. This
    includes legacy v1 rows and fresh-admission v2 rows, but deliberately not
    older normalized v2 checkpoints. It binds every supplied local input by
    SHA-256 and keeps evaluation outcomes in a separate ledger.
    """
    _validate_boundaries(
        validation_from,
        evaluation_from,
        evaluation_until,
        embargo,
    )
    if not stock_open_paths:
        raise ValueError("at least one stock-open file is required")
    input_fingerprints = _input_fingerprints(
        dataset_path,
        stock_open_paths,
        benchmark_open_path,
    )
    examples = load_signal_dataset(dataset_path, allow_synthetic=allow_synthetic)
    if len(examples) > MAXIMUM_EXAMPLES:
        raise ValueError(f"news model dataset exceeds {MAXIMUM_EXAMPLES} rows")
    dataset_schema = signal_dataset_schema_version(examples)
    stock_observations = [
        row
        for path in stock_open_paths
        for row in load_market_open_observations(
            path,
            allow_synthetic=allow_synthetic,
        )
    ]
    benchmark_observations = load_market_open_observations(
        benchmark_open_path,
        allow_synthetic=allow_synthetic,
    )
    _validate_benchmark(benchmark_observations, benchmark_id)
    reconciliation = _reconcile_market_labels(
        examples,
        stock_observations,
        benchmark_observations,
        benchmark_id,
    )
    stock_series = _stock_series(stock_observations)
    benchmark_series = MinuteOpenSeries.from_observations(benchmark_observations)
    base_rows = build_dataset_feature_rows(
        examples,
        stock_series,
        benchmark_series,
    )
    publication_times = {row.id: row.published_at for row in examples}
    publication_rows = build_decision_feature_rows(
        base_rows,
        publication_times,
        mode="publication",
    )
    update_rows = build_decision_feature_rows(
        base_rows,
        publication_times,
        mode="update_5m",
    )
    outcomes = _outcomes(examples, stock_series, benchmark_series, benchmark_id)
    fold = _build_fold(
        examples,
        update_rows,
        outcomes,
        validation_from=validation_from,
        evaluation_from=evaluation_from,
        evaluation_until=evaluation_until,
        embargo=embargo,
    )
    _verify_input_fingerprints(input_fingerprints)

    output_directory.mkdir(parents=True, exist_ok=False)
    publication_path = output_directory / "publication-inputs.jsonl"
    update_path = output_directory / "update-5m-inputs.jsonl"
    outcomes_path = output_directory / "outcomes.jsonl"
    write_jsonl(
        publication_path,
        (serialize_feature_row(row) for row in publication_rows),
        maximum_rows=MAXIMUM_EXAMPLES,
    )
    write_jsonl(
        update_path,
        (serialize_feature_row(row) for row in update_rows),
        maximum_rows=MAXIMUM_EXAMPLES,
    )
    write_jsonl(
        outcomes_path,
        (
            {
                "schema_version": "news-model-outcome-2.0",
                "example_id": identity,
                **value,
            }
            for identity, value in sorted(outcomes.items())
        ),
        maximum_rows=MAXIMUM_EXAMPLES,
    )
    bundle = _fit_bundle(
        str(input_fingerprints["dataset"]["sha256"]),
        dataset_schema,
        publication_path,
        update_path,
        fold,
    )
    bundle_path = output_directory / "fit-bundle.json"
    write_json(bundle_path, bundle)
    write_json(
        output_directory / "scoring-seal.json",
        {
            "schema_version": "news-model-generic-scoring-seal-1.0",
            "fit_bundle_sha256": signal_dataset_sha256(bundle_path),
            "outcomes_path": outcomes_path.name,
            "outcomes_sha256": signal_dataset_sha256(outcomes_path),
        },
    )
    report = _prepare_report(
        examples,
        dataset_schema,
        fold,
        outcomes,
        input_fingerprints,
        bundle_path,
        outcomes_path,
        reconciliation,
    )
    write_json(output_directory / "prepare.json", report)
    return report


def _fit_bundle(
    dataset_sha256: str,
    dataset_schema: str,
    publication_path: Path,
    update_path: Path,
    fold: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema_version": "news-model-fit-bundle-1.0",
        "dataset_schema_version": dataset_schema,
        "dataset_sha256": dataset_sha256,
        "publication_input_path": publication_path.name,
        "publication_input_sha256": signal_dataset_sha256(publication_path),
        "update_input_path": update_path.name,
        "update_input_sha256": signal_dataset_sha256(update_path),
        "portable_model_specs": {
            "materiality": "update_5m_symmetric_materiality",
            "direction": "update_5m_common_direction",
        },
        "folds": [dict(fold)],
        "evaluation_outcomes_included": False,
        "production_accessed": False,
        "ydb_accessed": False,
    }


def _outcomes(
    examples: Sequence[SignalDatasetExample],
    stock_series: Mapping[str, MinuteOpenSeries],
    benchmark_series: MinuteOpenSeries,
    benchmark_id: str,
) -> dict[str, dict[str, object]]:
    result = {}
    for example in examples:
        if (
            example.outcome_4h.benchmark_id != benchmark_id
            or example.outcome_4h.abnormal_return_pct is None
        ):
            raise ValueError("dataset row has no matching abnormal-return label")
        remaining = remaining_four_hour_outcome(
            example,
            stock_series.get(example.ticker),
            benchmark_series,
        )
        abnormal = float(example.outcome_4h.abnormal_return_pct)
        result[example.id] = {
            "published_at": example.published_at.isoformat(),
            "raw_4h": float(example.outcome_4h.return_pct),
            "abnormal_4h": abnormal,
            "materiality_4h": 1.0 if abs(abnormal) >= 0.5 else -1.0,
            "available_4h_epoch": example.label_available_at.timestamp(),
            **remaining,
        }
    return result


def _reconcile_market_labels(
    examples: Sequence[SignalDatasetExample],
    stock_observations: list[MarketOpenObservation],
    benchmark_observations: list[MarketOpenObservation],
    benchmark_id: str,
) -> dict[str, object]:
    """Prove that the supplied opens reproduce every stored label exactly."""
    candidates = [_candidate_from_example(example) for example in examples]
    rebuilt, report = build_signal_dataset(
        candidates,
        stock_observations,
        benchmark_observations=benchmark_observations,
        benchmark_id=benchmark_id,
    )
    expected = {example.id: example for example in examples}
    actual = {example.id: example for example in rebuilt}
    if expected.keys() != actual.keys():
        raise ValueError(
            "supplied market opens do not reproduce the dataset identities"
        )
    for identity, example in expected.items():
        if example.model_dump(mode="json") != actual[identity].model_dump(mode="json"):
            raise ValueError(
                "supplied market opens do not reproduce dataset row " f"{identity}"
            )
    return {
        "verified": True,
        "rows": len(rebuilt),
        "builder_schema_version": report["schema_version"],
        "outcome_policy": report["outcome_policy"],
    }


def _candidate_from_example(
    example: SignalDatasetExample,
) -> SignalEventCandidate:
    values = {
        name: getattr(example, name)
        for name in (
            "event_id",
            "ticker",
            "news_ids",
            "primary_news_id",
            "source_id",
            "corroborating_source_ids",
            "title",
            "content",
            "categories",
            "published_at",
            "received_at",
            "decision_at",
            "features",
            "corporate_action_status",
            "overlapping_event_ids",
            "notes",
        )
    }
    if isinstance(example, SignalDatasetExampleV2):
        return SignalEventCandidateV2.model_validate(
            {
                **values,
                **{
                    name: getattr(example, name)
                    for name in (
                        "event_group_id",
                        "source_url",
                        "retrieved_at",
                        "admission_version",
                        "analysis_title",
                        "analysis_content_sha256",
                        "receipt_provenance",
                        "text_provenance",
                    )
                },
            }
        )
    return SignalEventCandidate.model_validate(values)


def _build_fold(
    examples: Sequence[SignalDatasetExample],
    feature_rows: Sequence[NewsFeatureRow],
    outcomes: Mapping[str, Mapping[str, object]],
    *,
    validation_from: datetime,
    evaluation_from: datetime,
    evaluation_until: datetime,
    embargo: timedelta,
) -> dict[str, object]:
    feature_index = {row.example_id: row for row in feature_rows}
    example_index = {row.id: row for row in examples}
    if feature_index.keys() != example_index.keys():
        raise ValueError("dataset and feature identities differ")
    partitions = _partition_groups(
        examples,
        feature_index,
        outcomes,
        validation_from=validation_from,
        evaluation_from=evaluation_from,
        evaluation_until=evaluation_until,
        embargo=embargo,
    )
    evaluation_ids = [
        identity
        for identity in partitions["evaluation"]
        if outcomes[identity].get("materiality_4h") is not None
        and outcomes[identity].get("remaining_abnormal_4h") not in {None, 0.0}
    ]
    if not evaluation_ids:
        raise ValueError("generic news model split has no common evaluation rows")
    return {
        "name": "generic_latest",
        "validation_from": validation_from.isoformat(),
        "evaluation_from": evaluation_from.isoformat(),
        "evaluation_until": evaluation_until.isoformat(),
        "evaluation_ids": evaluation_ids,
        "labels": {
            "materiality_4h": _labels(
                partitions,
                outcomes,
                "materiality_4h",
                "available_4h_epoch",
                evaluation_from,
                evaluation_ids,
            ),
            "remaining_abnormal_4h": _labels(
                partitions,
                outcomes,
                "remaining_abnormal_4h",
                "remaining_4h_available_epoch",
                evaluation_from,
                evaluation_ids,
            ),
        },
        "partitions": {name: len(rows) for name, rows in partitions.items()},
    }


def _partition_groups(
    examples: Sequence[SignalDatasetExample],
    feature_index: Mapping[str, NewsFeatureRow],
    outcomes: Mapping[str, Mapping[str, object]],
    *,
    validation_from: datetime,
    evaluation_from: datetime,
    evaluation_until: datetime,
    embargo: timedelta,
) -> dict[str, list[str]]:
    grouped: defaultdict[str, list[str]] = defaultdict(list)
    for example in examples:
        grouped[signal_information_group_id(example)].append(example.id)
    partitions: defaultdict[str, list[str]] = defaultdict(list)
    for identities in grouped.values():
        times = [feature_index[identity].decision_at for identity in identities]
        available = max(
            _latest_label_availability(outcomes[identity]) for identity in identities
        )
        start, end = min(times), max(times)
        if end < validation_from - embargo and available < validation_from:
            partition = "training"
        elif (
            start >= validation_from
            and end < evaluation_from - embargo
            and available < evaluation_from
        ):
            partition = "validation"
        elif start >= evaluation_from and end < evaluation_until:
            partition = "evaluation"
        elif start >= evaluation_until:
            partition = "future"
        else:
            partition = "purged"
        partitions[partition].extend(identities)
    for rows in partitions.values():
        rows.sort(key=lambda identity: (feature_index[identity].decision_at, identity))
    return {
        name: partitions[name]
        for name in ("training", "validation", "evaluation", "purged", "future")
    }


def _labels(
    partitions: Mapping[str, Sequence[str]],
    outcomes: Mapping[str, Mapping[str, object]],
    target: str,
    availability: str,
    evaluation_from: datetime,
    evaluation_ids: Sequence[str],
) -> dict[str, object]:
    def eligible(partition: str) -> list[dict[str, object]]:
        return [
            {"example_id": identity, "return_pct": float(outcomes[identity][target])}
            for identity in partitions[partition]
            if outcomes[identity].get(target) not in {None, 0.0}
            and float(outcomes[identity][availability]) < evaluation_from.timestamp()
        ]

    return {
        "training": eligible("training"),
        "validation": eligible("validation"),
        "evaluation_ids": list(evaluation_ids),
    }


def _latest_label_availability(outcome: Mapping[str, object]) -> datetime:
    values = [
        float(value)
        for name in ("available_4h_epoch", "remaining_4h_available_epoch")
        if (value := outcome.get(name)) is not None
    ]
    if not values:
        raise ValueError("dataset row has no observable model target")
    return datetime.fromtimestamp(max(values), tz=UTC)


def _stock_series(
    observations: Sequence[MarketOpenObservation],
) -> dict[str, MinuteOpenSeries]:
    grouped: defaultdict[str, list[MarketOpenObservation]] = defaultdict(list)
    for row in observations:
        grouped[row.ticker].append(row)
    return {
        ticker: MinuteOpenSeries.from_observations(rows)
        for ticker, rows in grouped.items()
    }


def _validate_benchmark(
    observations: Sequence[MarketOpenObservation],
    benchmark_id: str,
) -> None:
    if not observations or any(row.ticker != benchmark_id for row in observations):
        raise ValueError("benchmark-open file does not match --benchmark-id")


def _validate_boundaries(
    validation_from: datetime,
    evaluation_from: datetime,
    evaluation_until: datetime,
    embargo: timedelta,
) -> None:
    if any(
        value.tzinfo is None or value.utcoffset() is None
        for value in (validation_from, evaluation_from, evaluation_until)
    ):
        raise ValueError("news model split boundaries must be timezone-aware")
    if not validation_from < evaluation_from < evaluation_until:
        raise ValueError("news model split boundaries must be chronological")
    if embargo < timedelta(0):
        raise ValueError("news model embargo cannot be negative")


def _input_fingerprints(
    dataset_path: Path,
    stock_open_paths: Sequence[Path],
    benchmark_open_path: Path,
) -> dict[str, object]:
    """Hash every input before parsing so a later check can detect replacement."""
    stock = {
        str(path): signal_dataset_sha256(path)
        for path in stock_open_paths
    }
    if len(stock) != len(stock_open_paths):
        raise ValueError("stock-open paths must be unique")
    return {
        "dataset": {
            "path": str(dataset_path),
            "sha256": signal_dataset_sha256(dataset_path),
        },
        "stock_opens": stock,
        "benchmark_opens": {
            "path": str(benchmark_open_path),
            "sha256": signal_dataset_sha256(benchmark_open_path),
        },
    }


def _verify_input_fingerprints(fingerprints: Mapping[str, object]) -> None:
    """Reject any source file changed while the preparation was reading it."""
    dataset = fingerprints["dataset"]
    benchmark = fingerprints["benchmark_opens"]
    stock = fingerprints["stock_opens"]
    if not isinstance(dataset, dict) or not isinstance(benchmark, dict):
        raise ValueError("news model input fingerprint contract is invalid")
    if not isinstance(stock, dict):
        raise ValueError("news model stock fingerprint contract is invalid")
    expected = {
        Path(str(dataset["path"])): str(dataset["sha256"]),
        Path(str(benchmark["path"])): str(benchmark["sha256"]),
        **{Path(str(path)): str(digest) for path, digest in stock.items()},
    }
    changed = [
        str(path)
        for path, digest in expected.items()
        if signal_dataset_sha256(path) != digest
    ]
    if changed:
        raise ValueError(f"news model input changed while being read: {changed!r}")


def _prepare_report(
    examples: Sequence[SignalDatasetExample],
    dataset_schema: str,
    fold: Mapping[str, object],
    outcomes: Mapping[str, Mapping[str, object]],
    input_fingerprints: Mapping[str, object],
    bundle_path: Path,
    outcomes_path: Path,
    reconciliation: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema_version": "news-model-generic-prepare-report-1.0",
        "dataset_schema_version": dataset_schema,
        "rows": len(examples),
        "information_groups": len(
            {signal_information_group_id(row) for row in examples}
        ),
        "fold": {
            key: fold[key]
            for key in (
                "name",
                "validation_from",
                "evaluation_from",
                "evaluation_until",
                "partitions",
            )
        },
        "outcome_coverage": {
            target: sum(value.get(target) is not None for value in outcomes.values())
            for target in ("materiality_4h", "remaining_abnormal_4h")
        },
        "inputs": dict(input_fingerprints),
        "fit_bundle_sha256": signal_dataset_sha256(bundle_path),
        "outcomes_sha256": signal_dataset_sha256(outcomes_path),
        "outcomes_sealed_separately": True,
        "market_label_reconciliation": dict(reconciliation),
        "production_accessed": False,
        "remote_ydb_accessed": False,
    }
