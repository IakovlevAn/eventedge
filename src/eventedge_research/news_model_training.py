"""Validated local training entrypoints for portable retained news models."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

from eventedge_research.news_model_artifact import (
    CANONICAL_DIRECTION_FEATURE_SPEC_NAME,
    COMMON_DIRECTION_FEATURE_SPEC_NAME,
    DIRECTION_TARGET,
    TARGET,
    build_portable_direction_artifact,
    build_portable_materiality_artifact,
)
from eventedge_research.news_model_features import (
    NewsFeatureRow,
    deserialize_feature_row,
    fit_news_model,
    retained_feature_specs,
)
from eventedge_research.research_artifacts import iter_jsonl, read_json, write_json
from eventedge_research.signal_dataset import signal_dataset_sha256

FIT_BUNDLE_SCHEMA_VERSION = "news-model-fit-bundle-1.0"
MAXIMUM_INPUT_ROWS = 20_000
MAXIMUM_FOLDS = 100
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")


def fit_latest_materiality_artifact(
    fit_bundle_path: Path,
    output_path: Path,
    *,
    expected_fit_bundle_sha256: str,
) -> dict[str, object]:
    """Fit the latest retained materiality fold and write a portable artifact."""
    return _fit_latest_artifact(
        fit_bundle_path,
        output_path,
        expected_fit_bundle_sha256=expected_fit_bundle_sha256,
        target=TARGET,
        default_feature_spec_name="update_5m_symmetric_materiality",
        allowed_feature_spec_names=("update_5m_symmetric_materiality",),
        artifact_builder=build_portable_materiality_artifact,
    )


def fit_latest_direction_artifact(
    fit_bundle_path: Path,
    output_path: Path,
    *,
    expected_fit_bundle_sha256: str,
) -> dict[str, object]:
    """Fit the latest retained direction fold and write a portable artifact."""
    return _fit_latest_artifact(
        fit_bundle_path,
        output_path,
        expected_fit_bundle_sha256=expected_fit_bundle_sha256,
        target=DIRECTION_TARGET,
        default_feature_spec_name=CANONICAL_DIRECTION_FEATURE_SPEC_NAME,
        allowed_feature_spec_names=(
            CANONICAL_DIRECTION_FEATURE_SPEC_NAME,
            COMMON_DIRECTION_FEATURE_SPEC_NAME,
        ),
        artifact_builder=build_portable_direction_artifact,
    )


def _fit_latest_artifact(
    fit_bundle_path: Path,
    output_path: Path,
    *,
    expected_fit_bundle_sha256: str,
    target: str,
    default_feature_spec_name: str,
    allowed_feature_spec_names: tuple[str, ...],
    artifact_builder: Callable[..., dict[str, object]],
) -> dict[str, object]:
    fit_bundle_path = Path(fit_bundle_path)
    expected_digest = _digest(
        expected_fit_bundle_sha256,
        "expected fit bundle",
    )
    fit_bundle_sha256 = signal_dataset_sha256(fit_bundle_path)
    if fit_bundle_sha256 != expected_digest:
        raise ValueError("news model fit bundle does not match the expected digest")
    bundle = _load_training_bundle(fit_bundle_path)
    feature_spec_name = _resolve_feature_spec(
        bundle,
        target=target,
        default_spec=default_feature_spec_name,
        allowed_specs=allowed_feature_spec_names,
    )
    if signal_dataset_sha256(fit_bundle_path) != fit_bundle_sha256:
        raise ValueError("news model fit bundle changed while it was read")
    feature_input_path = _bundle_file(
        fit_bundle_path.parent, bundle["update_input_path"]
    )
    feature_input_sha256 = signal_dataset_sha256(feature_input_path)
    if feature_input_sha256 != bundle["update_input_sha256"]:
        raise ValueError("news model training feature input changed")
    feature_index = _load_feature_index(feature_input_path)
    if signal_dataset_sha256(feature_input_path) != feature_input_sha256:
        raise ValueError("news model feature input changed while it was read")
    fold = _latest_fold(bundle["folds"])
    labels = _target_labels(fold, target)
    training, training_values = _aligned(labels["training"], feature_index, "training")
    validation, validation_values = _aligned(
        labels["validation"], feature_index, "validation"
    )
    evaluation_ids = _evaluation_identities(labels["evaluation_ids"], "target")
    if evaluation_ids != _evaluation_identities(fold.get("evaluation_ids"), "fold"):
        raise ValueError("news model fold and target evaluation identities differ")
    evaluation = _evaluation_rows(evaluation_ids, feature_index)
    _validate_partitions(fold, training, validation, evaluation)
    fitted = fit_news_model(
        training,
        training_values,
        validation,
        validation_values,
        spec=retained_feature_specs()[feature_spec_name],
    )
    artifact = artifact_builder(
        fitted,
        raw_validation_probabilities=fitted.raw_probabilities_up(validation),
        fold=fold,
        dataset_sha256=str(bundle["dataset_sha256"]),
        fit_bundle_sha256=fit_bundle_sha256,
        feature_input_filename=str(bundle["update_input_path"]),
        feature_input_sha256=feature_input_sha256,
        training_rows=len(training),
        validation_rows=len(validation),
    )
    write_json(output_path, artifact)
    return artifact


def _resolve_feature_spec(
    bundle: Mapping[str, object],
    *,
    target: str,
    default_spec: str,
    allowed_specs: tuple[str, ...],
) -> str:
    specs = bundle.get("portable_model_specs")
    if specs is None:
        return default_spec
    task = "direction" if target == DIRECTION_TARGET else "materiality"
    if not isinstance(specs, dict) or specs.get(task) not in allowed_specs:
        raise ValueError(f"fit bundle does not declare the retained {task} feature spec")
    return str(specs[task])


def _load_training_bundle(path: Path) -> Mapping[str, object]:
    bundle = read_json(path)
    if not isinstance(bundle, dict):
        raise ValueError("news model fit bundle must be a JSON object")
    if (
        bundle.get("schema_version") != FIT_BUNDLE_SCHEMA_VERSION
        or bundle.get("evaluation_outcomes_included") is not False
        or bundle.get("production_accessed") is not False
        or bundle.get("ydb_accessed") is not False
    ):
        raise ValueError("news model fit bundle is unsafe or incompatible")
    _digest(bundle.get("dataset_sha256"), "dataset")
    _digest(bundle.get("update_input_sha256"), "feature input")
    _bundle_file(path.parent, bundle.get("update_input_path"))
    folds = bundle.get("folds")
    if not isinstance(folds, list) or not 1 <= len(folds) <= MAXIMUM_FOLDS:
        raise ValueError("news model fit bundle has an invalid fold count")
    return bundle


def _load_feature_index(path: Path) -> dict[str, NewsFeatureRow]:
    rows = [
        deserialize_feature_row(row)
        for row in iter_jsonl(path, maximum_rows=MAXIMUM_INPUT_ROWS)
    ]
    index = {row.example_id: row for row in rows}
    if not rows or len(index) != len(rows):
        raise ValueError("news model feature input is empty or contains duplicate ids")
    return index


def _latest_fold(folds: object) -> Mapping[str, object]:
    if not isinstance(folds, list) or not folds:
        raise ValueError("news model fit bundle has no folds")
    parsed = []
    for value in folds:
        if not isinstance(value, dict):
            raise ValueError("news model fit bundle fold must be an object")
        parsed.append((_aware_datetime(value.get("evaluation_from")), value))
    if len({value for value, _ in parsed}) != len(parsed):
        raise ValueError("news model fit bundle contains duplicate evaluation cutoffs")
    return max(parsed, key=lambda item: item[0])[1]


def _target_labels(
    fold: Mapping[str, object],
    target: str,
) -> Mapping[str, object]:
    labels = fold.get("labels")
    if not isinstance(labels, dict) or not isinstance(labels.get(target), dict):
        raise ValueError(f"latest fold has no {target} labels")
    result = labels[target]
    if not {"training", "validation", "evaluation_ids"}.issubset(result):
        raise ValueError("latest fold target labels are incomplete")
    return result


def _evaluation_identities(value: object, source: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"news model {source} evaluation ids must be a non-empty list")
    identities = tuple(value)
    if any(not isinstance(identity, str) or not identity for identity in identities):
        raise ValueError(f"news model {source} evaluation identity is invalid")
    if len(identities) != len(set(identities)):
        raise ValueError(f"news model {source} evaluation identities are duplicated")
    return identities


def _evaluation_rows(
    identities: tuple[str, ...],
    feature_index: Mapping[str, NewsFeatureRow],
) -> list[NewsFeatureRow]:
    try:
        return [feature_index[identity] for identity in identities]
    except KeyError as error:
        raise ValueError("news model evaluation feature row is missing") from error


def _validate_partitions(
    fold: Mapping[str, object],
    training: Sequence[NewsFeatureRow],
    validation: Sequence[NewsFeatureRow],
    evaluation: Sequence[NewsFeatureRow],
) -> None:
    validation_from = _aware_datetime(fold.get("validation_from"))
    evaluation_from = _aware_datetime(fold.get("evaluation_from"))
    evaluation_until = _aware_datetime(fold.get("evaluation_until"))
    if not validation_from < evaluation_from < evaluation_until:
        raise ValueError("news model fold cutoffs are not chronological")

    partitions = {
        "training": training,
        "validation": validation,
        "evaluation": evaluation,
    }
    _validate_disjoint_partitions(partitions)
    _validate_partition_timestamps(
        training,
        "training",
        before=validation_from,
    )
    _validate_partition_timestamps(
        validation,
        "validation",
        from_inclusive=validation_from,
        before=evaluation_from,
    )
    _validate_partition_timestamps(
        evaluation,
        "evaluation",
        from_inclusive=evaluation_from,
        before=evaluation_until,
    )


def _validate_disjoint_partitions(
    partitions: Mapping[str, Sequence[NewsFeatureRow]],
) -> None:
    identity_sets = {
        name: {row.example_id for row in rows} for name, rows in partitions.items()
    }
    group_sets = {
        name: {row.event_group_id for row in rows} for name, rows in partitions.items()
    }
    for left, right in (
        ("training", "validation"),
        ("training", "evaluation"),
        ("validation", "evaluation"),
    ):
        if identity_sets[left] & identity_sets[right]:
            raise ValueError(f"news model {left} and {right} identities overlap")
        if group_sets[left] & group_sets[right]:
            raise ValueError(f"news model {left} and {right} groups overlap")


def _validate_partition_timestamps(
    rows: Sequence[NewsFeatureRow],
    partition: str,
    *,
    before: datetime,
    from_inclusive: datetime | None = None,
) -> None:
    for row in rows:
        decision_at = row.decision_at
        if decision_at.tzinfo is None or decision_at.utcoffset() is None:
            raise ValueError(f"news model {partition} timestamp must be timezone-aware")
        if decision_at >= before or (
            from_inclusive is not None and decision_at < from_inclusive
        ):
            raise ValueError(f"news model {partition} timestamp is outside its fold")


def _aligned(
    labels: object,
    feature_index: Mapping[str, NewsFeatureRow],
    partition: str,
) -> tuple[list[NewsFeatureRow], list[float]]:
    if not isinstance(labels, list) or not labels:
        raise ValueError(f"news model {partition} labels must be a non-empty list")
    rows = []
    values = []
    identities = set()
    for label in labels:
        if not isinstance(label, dict) or set(label) != {"example_id", "return_pct"}:
            raise ValueError(f"news model {partition} label is invalid")
        identity = label["example_id"]
        if not isinstance(identity, str) or not identity or identity in identities:
            raise ValueError(f"news model {partition} label identity is invalid")
        value = label["return_pct"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"news model {partition} target is invalid")
        value = float(value)
        if not math.isfinite(value) or value == 0:
            raise ValueError(f"news model {partition} target is invalid")
        try:
            rows.append(feature_index[identity])
        except KeyError as error:
            raise ValueError(f"news model {partition} feature row is missing") from error
        values.append(value)
        identities.add(identity)
    return rows, values


def _bundle_file(directory: Path, value: object) -> Path:
    if not isinstance(value, str):
        raise ValueError("news model bundle path must be a filename")
    path = Path(value)
    if path.is_absolute() or len(path.parts) != 1 or path.suffix != ".jsonl":
        raise ValueError("news model bundle path must be a local JSONL filename")
    resolved_directory = directory.resolve()
    resolved = (resolved_directory / path).resolve()
    if not resolved.is_relative_to(resolved_directory):
        raise ValueError("news model bundle file escapes its directory")
    return resolved


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or not _DIGEST_PATTERN.fullmatch(value):
        raise ValueError(f"news model {name} SHA-256 is invalid")
    return value


def _aware_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("news model fold cutoff must be an ISO timestamp")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("news model fold cutoff must be an ISO timestamp") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("news model fold cutoff must be timezone-aware")
    return result
