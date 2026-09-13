"""Prepare causal feature and outcome ledgers for retained news models."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

from eventedge_research.news_model_features import (
    MinuteOpenSeries,
    build_decision_feature_rows,
    deserialize_feature_row,
    remaining_four_hour_outcome,
    serialize_feature_row,
)
from eventedge_research.research_artifacts import iter_jsonl, read_json, write_json, write_jsonl
from eventedge_research.signal_dataset import load_signal_dataset, signal_dataset_sha256
from eventedge_research.signal_dataset_v2 import SignalDatasetExampleV2, split_information_updates

EXPECTED_EVALUATION_ROWS = 480
MAXIMUM_FEATURE_ROWS = 20_000
USED_DEPENDENCIES = ("feature_inputs",)


def prepare_benchmark(
    contract_path: Path,
    output_directory: Path,
    *,
    artifact_root: Path | None = None,
) -> dict[str, object]:
    """Freeze model inputs and a separately sealed outcome ledger."""
    contract = read_json(contract_path)
    root = (artifact_root or contract_path.resolve().parents[1]).resolve()
    _verify_contract(contract_path, contract, root)
    examples = _load_examples(contract, root)
    corrected_rows = _load_corrected_rows(contract, root, examples)
    publication_times = {row.id: row.published_at for row in examples}
    publication_rows = build_decision_feature_rows(
        corrected_rows, publication_times, mode="publication"
    )
    update_rows = build_decision_feature_rows(corrected_rows, publication_times, mode="update_5m")
    outcomes = _build_outcomes(contract, root, examples, update_rows)
    folds = _build_folds(examples, outcomes, contract)
    evaluation_ids = {identity for fold in folds for identity in fold["evaluation_ids"]}
    if len(evaluation_ids) != contract["expected_output"]["evaluation_rows"]:
        raise ValueError("news model preparation changed the frozen evaluation universe")

    output_directory.mkdir(parents=True, exist_ok=False)
    publication_path = output_directory / "publication-inputs.jsonl"
    update_path = output_directory / "update-5m-inputs.jsonl"
    outcomes_path = output_directory / "outcomes.jsonl"
    write_jsonl(
        publication_path,
        [serialize_feature_row(row) for row in publication_rows],
        maximum_rows=MAXIMUM_FEATURE_ROWS,
    )
    write_jsonl(
        update_path,
        [serialize_feature_row(row) for row in update_rows],
        maximum_rows=MAXIMUM_FEATURE_ROWS,
    )
    write_jsonl(
        outcomes_path,
        [
            {
                "schema_version": "news-model-outcome-2.0",
                "example_id": identity,
                **value,
            }
            for identity, value in sorted(outcomes.items())
        ],
        maximum_rows=MAXIMUM_FEATURE_ROWS,
    )
    if signal_dataset_sha256(outcomes_path) != contract["expected_output"]["outcomes_sha256"]:
        raise ValueError("news model outcomes changed")
    bundle = _fit_bundle(
        contract_path,
        contract,
        publication_path,
        update_path,
        folds,
    )
    bundle_path = output_directory / "fit-bundle.json"
    write_json(bundle_path, bundle)
    write_json(
        output_directory / "scoring-seal.json",
        {
            "schema_version": "news-model-scoring-seal-3.0",
            "contract_sha256": signal_dataset_sha256(contract_path),
            "fit_bundle_sha256": signal_dataset_sha256(bundle_path),
            "outcomes_path": outcomes_path.name,
            "outcomes_sha256": signal_dataset_sha256(outcomes_path),
            "formula_comparison": contract["expected_output"]["formula_comparison"],
        },
    )
    report = {
        "schema_version": "news-model-prepare-report-1.0",
        "rows": len(examples),
        "evaluation_rows": len(evaluation_ids),
        "outcome_coverage": {
            target: sum(row.get(target) is not None for row in outcomes.values())
            for target in ("materiality_4h", "remaining_abnormal_4h")
        },
        "folds": [_public_fold(fold) for fold in folds],
        "fit_bundle_sha256": signal_dataset_sha256(bundle_path),
        "outcomes_sha256": signal_dataset_sha256(outcomes_path),
        "outcomes_sealed_separately": True,
        "production_accessed": False,
        "remote_ydb_accessed": False,
    }
    write_json(output_directory / "prepare.json", report)
    return report


def _fit_bundle(contract_path, contract, publication_path, update_path, folds):
    return {
        "schema_version": "news-model-fit-bundle-1.0",
        "contract_filename": contract_path.name,
        "contract_sha256": signal_dataset_sha256(contract_path),
        "dataset_sha256": contract["dataset"]["sha256"],
        "expected_output": {
            key: contract["expected_output"][key]
            for key in (
                "model_fits",
                "prediction_rows",
                "frozen_probabilities_sha256",
                "outcomes_sha256",
            )
        },
        "publication_input_path": "publication-inputs.jsonl",
        "publication_input_sha256": signal_dataset_sha256(publication_path),
        "update_input_path": "update-5m-inputs.jsonl",
        "update_input_sha256": signal_dataset_sha256(update_path),
        "folds": folds,
        "evaluation_outcomes_included": False,
        "production_accessed": False,
        "ydb_accessed": False,
    }


def _build_outcomes(contract, root, examples, update_rows):
    row_index = {row.example_id: row for row in update_rows}
    issuer_directories, benchmark_paths = _market_paths(contract, root)
    benchmark = MinuteOpenSeries.from_paths(benchmark_paths)
    stocks = {
        ticker: MinuteOpenSeries.from_paths(
            [directory / f"{ticker}.jsonl.gz" for directory in issuer_directories]
        )
        for ticker in sorted({row.ticker for row in examples})
    }
    outcomes = {}
    for example in examples:
        _verify_label_availability(example, benchmark)
        abnormal = float(example.outcome_4h.abnormal_return_pct)
        volatility = row_index[example.id].numeric.get("risk_volatility_20d_pct")
        normalized = (
            abs(abnormal) / max(float(volatility), 0.05)
            if volatility is not None and volatility > 0
            else None
        )
        remaining = remaining_four_hour_outcome(example, stocks.get(example.ticker), benchmark)
        outcomes[example.id] = {
            "published_at": example.published_at.isoformat(),
            "raw_4h": float(example.outcome_4h.return_pct),
            "abnormal_4h": abnormal,
            "materiality_4h": 1.0 if abs(abnormal) >= 0.5 else -1.0,
            "materiality_normalized": (
                1.0 if normalized is not None and normalized >= 0.5 else -1.0
            )
            if normalized is not None
            else None,
            "available_4h_epoch": example.label_available_at.timestamp(),
            "formula_direction": str(
                getattr(
                    example.features.rule_direction,
                    "value",
                    example.features.rule_direction,
                )
            ),
            "formula_config_version": example.features.rule_config_version,
            "formula_confidence": example.features.rule_confidence,
            "formula_score": example.features.rule_score,
            **remaining,
        }
    if row_index.keys() != outcomes.keys():
        raise ValueError("feature and outcome identities differ")
    return outcomes


def _verify_label_availability(
    example: SignalDatasetExampleV2,
    benchmark: MinuteOpenSeries,
) -> None:
    """Reject an abnormal label sealed before its last price observation."""
    expected = example.outcome_4h.observed_at
    if example.outcome_4h.benchmark_return_pct is not None:
        benchmark_outcome = benchmark.first_at_or_after(example.outcome_4h.target_at)
        if benchmark_outcome is None:
            raise ValueError("benchmark outcome is missing for a sealed abnormal label")
        expected = max(expected, benchmark_outcome[0])
    if example.label_available_at != expected:
        raise ValueError(
            "label availability does not match stock and benchmark observations"
        )


def _build_folds(examples, outcomes, contract):
    folds = []
    embargo = timedelta(hours=contract["guards"]["embargo_hours"])
    for source_fold in contract["folds"]:
        evaluation_from = datetime.fromisoformat(source_fold["evaluation_from"])
        split = split_information_updates(
            examples,
            validation_from=datetime.fromisoformat(source_fold["validation_from"]),
            test_from=evaluation_from,
            test_until=datetime.fromisoformat(source_fold["evaluation_until"]),
            embargo=embargo,
        )
        evaluation_ids = [row.id for row in split.test]
        if (
            len(evaluation_ids) != source_fold["evaluation_rows"]
            or _identity_sequence_sha256(evaluation_ids) != source_fold["evaluation_ids_sha256"]
        ):
            raise ValueError("news model fold membership changed")
        folds.append(
            {
                "name": source_fold["name"],
                "validation_from": source_fold["validation_from"],
                "evaluation_from": source_fold["evaluation_from"],
                "evaluation_until": source_fold["evaluation_until"],
                "evaluation_ids": evaluation_ids,
                "labels": {
                    "materiality_4h": _past_labels(
                        split, outcomes, "materiality_4h", evaluation_from
                    ),
                    "remaining_abnormal_4h": _past_labels(
                        split, outcomes, "remaining_abnormal_4h", evaluation_from
                    ),
                },
            }
        )
    return folds


def _past_labels(split, outcomes, target, evaluation_from):
    availability = (
        "available_4h_epoch" if target == "materiality_4h" else "remaining_4h_available_epoch"
    )

    def eligible(rows):
        return [
            {"example_id": row.id, "return_pct": float(outcomes[row.id][target])}
            for row in rows
            if outcomes[row.id].get(target) is not None
            and outcomes[row.id].get(availability) is not None
            and float(outcomes[row.id][availability]) < evaluation_from.timestamp()
        ]

    return {
        "training": eligible(split.train),
        "validation": eligible(split.validation),
        "evaluation_ids": [
            row.id for row in split.test if outcomes[row.id].get(target) is not None
        ],
    }


def _market_paths(contract, root):
    manifests = contract["market_data"]["manifests"]
    issuer_directories = [
        _resolve_artifact_path(root, value["path"]).parent
        for value in manifests
        if str(value["role"]).startswith("issuer_")
    ]
    benchmark_paths = [
        _resolve_artifact_path(root, value["path"]).parent / "IMOEX2.jsonl"
        for value in manifests
        if str(value["role"]).startswith("benchmark_")
    ]
    return issuer_directories, benchmark_paths


def _load_corrected_rows(contract, root, examples):
    dependency = contract["dependencies"]["feature_inputs"]
    path = _resolve_artifact_path(root, dependency["path"])
    if signal_dataset_sha256(path) != dependency["sha256"]:
        raise ValueError("news model feature inputs changed")
    payloads = list(iter_jsonl(path, maximum_rows=MAXIMUM_FEATURE_ROWS))
    identities = [str(row.get("example_id", "")) for row in payloads]
    if (
        dependency.get("accepted_immutable_checkpoint") is not True
        or len(payloads) != dependency.get("rows")
        or len(identities) != len(set(identities))
        or any(row.get("schema_version") != dependency.get("schema_version") for row in payloads)
        or _identity_sequence_sha256(identities) != dependency.get("ordered_example_ids_sha256")
    ):
        raise ValueError("news model feature checkpoint is incompatible")
    rows = [deserialize_feature_row(row) for row in payloads]
    if {row.example_id for row in rows} != {row.id for row in examples}:
        raise ValueError("corrected model inputs do not match the dataset")
    return rows


def _verify_contract(path, contract, root):
    guards = contract.get("guards", {})
    expected = contract.get("expected_output", {})
    if (
        contract.get("schema_version") != "news-model-benchmark-contract-3.0"
        or contract.get("status") != "historical_research_proxy"
        or guards.get("production_access_allowed") is not False
        or guards.get("remote_ydb_access_allowed") is not False
        or guards.get("network_market_data_fetch_allowed") is not False
        or guards.get("information_groups_kept_whole") is not True
        or guards.get("embargo_hours") != 72
        or guards.get("evaluation_windows_are_reused_development_data") is not True
        or guards.get("evaluation_outcomes_for_feature_or_sample_selection_allowed") is not False
        or contract.get("market_data", {}).get("benchmark_id") != "IMOEX2"
        or expected.get("evaluation_rows") != EXPECTED_EVALUATION_ROWS
        or expected.get("model_fits") != 35
        or expected.get("prediction_rows") != 2_400
        or not _is_sha256(expected.get("frozen_probabilities_sha256"))
        or not _is_sha256(expected.get("outcomes_sha256"))
    ):
        raise ValueError("news model contract is unsafe or incompatible")
    _verify_fold_contract(contract.get("folds"), expected["evaluation_rows"])
    _verify_formula_expectation(expected.get("formula_comparison"))
    dependencies = contract.get("dependencies")
    if not isinstance(dependencies, dict):
        raise ValueError("news model dependencies are incompatible")
    feature_checkpoint = dependencies.get("feature_inputs")
    if (
        not isinstance(feature_checkpoint, dict)
        or len(dependencies) != 1
        or feature_checkpoint.get("accepted_immutable_checkpoint") is not True
        or feature_checkpoint.get("schema_version") != "direction-research-input-1.0"
        or feature_checkpoint.get("rows") != contract["dataset"].get("rows")
        or not _is_sha256(feature_checkpoint.get("sha256"))
        or not _is_sha256(feature_checkpoint.get("ordered_example_ids_sha256"))
    ):
        raise ValueError("news model feature checkpoint contract is incompatible")
    dataset_path = _resolve_artifact_path(root, contract["dataset"]["path"])
    if signal_dataset_sha256(dataset_path) != contract["dataset"]["sha256"]:
        raise ValueError("news model dataset changed")
    for name in USED_DEPENDENCIES:
        dependency = contract["dependencies"][name]
        dependency_path = _resolve_artifact_path(root, dependency["path"])
        if signal_dataset_sha256(dependency_path) != dependency["sha256"]:
            raise ValueError(f"frozen dependency changed: {dependency_path}")
    manifests = contract.get("market_data", {}).get("manifests", [])
    roles = [str(value.get("role", "")) for value in manifests]
    if (
        not any(role.startswith("issuer_") for role in roles)
        or not any(role.startswith("benchmark_") for role in roles)
        or len(roles) != len(set(roles))
    ):
        raise ValueError("news model market manifests are incomplete or duplicated")
    for manifest in manifests:
        manifest_path = _resolve_artifact_path(root, manifest["path"])
        if signal_dataset_sha256(manifest_path) != manifest["sha256"]:
            raise ValueError(f"market manifest changed: {manifest_path}")
        expected_pattern = (
            "*.jsonl.gz" if str(manifest["role"]).startswith("issuer_") else "IMOEX2.jsonl"
        )
        if manifest.get("data_glob") != expected_pattern:
            raise ValueError(f"market data pattern is incompatible: {manifest_path}")
        if _market_content_sha256(manifest_path.parent, expected_pattern) != manifest.get(
            "data_sha256"
        ):
            raise ValueError(f"market data changed: {manifest_path.parent}")


def _verify_fold_contract(folds: object, expected_rows: int) -> None:
    if not isinstance(folds, list) or len(folds) != 7:
        raise ValueError("news model fold contract must contain seven windows")
    names = []
    evaluation_rows = 0
    previous_evaluation_from = None
    for fold in folds:
        if not isinstance(fold, dict):
            raise ValueError("news model fold must be an object")
        try:
            validation_from = datetime.fromisoformat(fold["validation_from"])
            evaluation_from = datetime.fromisoformat(fold["evaluation_from"])
            evaluation_until = datetime.fromisoformat(fold["evaluation_until"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("news model fold timestamps are invalid") from error
        rows = fold.get("evaluation_rows")
        if (
            any(
                value.tzinfo is None or value.utcoffset() is None
                for value in (
                    validation_from,
                    evaluation_from,
                    evaluation_until,
                )
            )
            or not validation_from < evaluation_from < evaluation_until
            or previous_evaluation_from is not None
            and evaluation_from <= previous_evaluation_from
            or not isinstance(rows, int)
            or rows <= 0
            or not _is_sha256(fold.get("evaluation_ids_sha256"))
            or not isinstance(fold.get("name"), str)
            or not fold["name"]
        ):
            raise ValueError("news model fold contract is unsafe or incompatible")
        names.append(fold["name"])
        evaluation_rows += rows
        previous_evaluation_from = evaluation_from
    if len(names) != len(set(names)) or evaluation_rows != expected_rows:
        raise ValueError("news model fold names or row counts changed")


def _verify_formula_expectation(expectation: object) -> None:
    if not isinstance(expectation, dict):
        raise ValueError("news model formula expectation is missing")
    interval = expectation.get("paired_hit_rate_difference_95_pct")
    if (
        expectation.get("config_version") != 6
        or expectation.get("model_id") != "update_5m_corrected_reaction_interactions"
        or expectation.get("target") != "remaining_abnormal_4h"
        or expectation.get("rows") != 157
        or not isinstance(interval, list)
        or len(interval) != 2
        or any(
            not isinstance(expectation.get(name), (float, int))
            for name in (
                "model_hit_rate_pct",
                "formula_hit_rate_pct",
                "model_roc_auc",
                "formula_confidence_roc_auc",
            )
        )
        or any(not isinstance(value, (float, int)) for value in interval)
    ):
        raise ValueError("news model formula expectation is incompatible")


def _load_examples(contract, root):
    rows = load_signal_dataset(_resolve_artifact_path(root, contract["dataset"]["path"]))
    if not rows or any(not isinstance(row, SignalDatasetExampleV2) for row in rows):
        raise ValueError("news model preparation requires only v2 examples")
    return rows


def _resolve_artifact_path(root: Path, value: object) -> Path:
    """Resolve one repository-relative local artifact without allowing escape."""
    relative = PurePosixPath(str(value))
    if (
        relative.is_absolute()
        or not relative.parts
        or ".." in relative.parts
        or relative.parts[0] not in {".local-artifacts", ".local-data"}
    ):
        raise ValueError("news model artifact paths must be local and relative")
    allowed_directory = (root / relative.parts[0]).resolve()
    resolved = (root / Path(relative)).resolve()
    if not resolved.is_relative_to(allowed_directory):
        raise ValueError("news model artifact path escapes its root")
    return resolved


def _market_content_sha256(directory: Path, pattern: str) -> str:
    """Fingerprint file names and contents consumed from one market directory."""
    resolved_directory = directory.resolve()
    paths = sorted(resolved_directory.glob(pattern))
    if not paths:
        raise ValueError(f"market data files are missing: {directory / pattern}")
    digest = hashlib.sha256()
    for path in paths:
        resolved = path.resolve()
        if not resolved.is_relative_to(resolved_directory) or not resolved.is_file():
            raise ValueError(f"market data file escapes its directory: {path}")
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(signal_dataset_sha256(resolved).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _identity_sequence_sha256(identities: Sequence[str]) -> str:
    serialized = json.dumps(
        list(identities),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _public_fold(fold):
    return {
        "name": fold["name"],
        "evaluation_rows": len(fold["evaluation_ids"]),
        "targets": {
            target: {
                "training_rows": len(value["training"]),
                "validation_rows": len(value["validation"]),
                "evaluation_rows": len(value["evaluation_ids"]),
            }
            for target, value in fold["labels"].items()
        },
    }
