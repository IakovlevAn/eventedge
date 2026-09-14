"""Portable, versioned JSON artifacts for retained offline news models."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

from eventedge_research.news_model_features import (
    COVERAGE_TARGETS_PCT,
    FittedNewsModel,
    NewsFeatureRow,
    NewsFeatureSpec,
    deserialize_feature_row,
    retained_feature_specs,
)
from eventedge_research.research_artifacts import iter_jsonl, read_json, write_jsonl
from eventedge_research.signal_dataset import signal_dataset_sha256
from eventedge_research.signal_extended_metrics import select_coverage_threshold

ARTIFACT_SCHEMA_VERSION = "news-materiality-model-artifact-1.0"
MODEL_ID = "update_5m_symmetric_materiality_logistic"
MODEL_VERSION = "update-5m-symmetric-materiality-logistic-1.0"
PREDICTION_SCHEMA_VERSION = "news-materiality-prediction-1.0"
TARGET = "materiality_4h"
TARGET_DEFINITION = (
    "abs(stock_return(entry_at,entry_at+4h)-"
    "benchmark_return(entry_at,entry_at+4h))>=0.5_pct_points"
)
DIRECTION_ARTIFACT_SCHEMA_VERSION = "news-direction-model-artifact-1.0"
DIRECTION_PREDICTION_SCHEMA_VERSION = "news-direction-prediction-1.0"
DIRECTION_TARGET = "remaining_abnormal_4h"
DIRECTION_TARGET_DEFINITION = (
    "stock_return(published_at+5m,published_at+4h)-"
    "benchmark_return(published_at+5m,published_at+4h)"
)
CANONICAL_DIRECTION_FEATURE_SPEC_NAME = "update_5m_corrected_reaction_interactions"
CANONICAL_DIRECTION_MODEL_ID = "update_5m_corrected_reaction_interactions"
CANONICAL_DIRECTION_MODEL_VERSION = (
    "update-5m-corrected-reaction-interactions-logistic-1.0"
)
COMMON_DIRECTION_FEATURE_SPEC_NAME = "update_5m_common_direction"
COMMON_DIRECTION_MODEL_ID = "update_5m_common_direction_logistic"
COMMON_DIRECTION_MODEL_VERSION = "update-5m-common-direction-logistic-1.0"
# Backward-compatible names identify the locked canonical benchmark candidate.
DIRECTION_FEATURE_SPEC_NAME = CANONICAL_DIRECTION_FEATURE_SPEC_NAME
DIRECTION_MODEL_ID = CANONICAL_DIRECTION_MODEL_ID
DIRECTION_MODEL_VERSION = CANONICAL_DIRECTION_MODEL_VERSION
MAXIMUM_ARTIFACT_BYTES = 5_000_000
MAXIMUM_PREDICTION_ROWS = 20_000
MAXIMUM_MODEL_FEATURES = 10_000
MAXIMUM_STRING_LENGTH = 10_000
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
_FEATURE_ROW_KEYS = {
    "schema_version",
    "example_id",
    "event_id",
    "event_group_id",
    "ticker",
    "source_id",
    "title",
    "content",
    "decision_at",
    "event_type",
    "temporal_status",
    "numeric",
    "categorical",
}
_ARTIFACT_KEYS = {
    "schema_version",
    "artifact_payload_sha256",
    "model_id",
    "model_version",
    "task",
    "target",
    "target_definition",
    "decision_mode",
    "feature_spec",
    "preprocessing",
    "classifier",
    "calibration",
    "coverage_thresholds",
    "provenance",
}


@dataclass(frozen=True)
class _DirectionArtifactContract:
    feature_spec_name: str
    model_id: str
    model_version: str


_DIRECTION_CONTRACTS = (
    _DirectionArtifactContract(
        feature_spec_name=CANONICAL_DIRECTION_FEATURE_SPEC_NAME,
        model_id=CANONICAL_DIRECTION_MODEL_ID,
        model_version=CANONICAL_DIRECTION_MODEL_VERSION,
    ),
    _DirectionArtifactContract(
        feature_spec_name=COMMON_DIRECTION_FEATURE_SPEC_NAME,
        model_id=COMMON_DIRECTION_MODEL_ID,
        model_version=COMMON_DIRECTION_MODEL_VERSION,
    ),
)


@dataclass(frozen=True)
class PortableNewsMaterialityModel:
    """Validated materiality model independent of sklearn and pickle."""

    artifact_payload_sha256: str
    model_id: str
    model_version: str
    spec: NewsFeatureSpec
    numeric_feature_names: tuple[str, ...]
    numeric_scale_factors: tuple[float, ...]
    categorical_feature_names: tuple[str, ...]
    coefficients: tuple[float, ...]
    intercept: float
    calibration_offset: float
    coverage_thresholds: Mapping[str, Mapping[str, float]]

    def raw_probability_material(self, row: NewsFeatureRow) -> float:
        """Return the uncalibrated probability of a material price move."""
        return _portable_probability(
            row,
            spec=self.spec,
            numeric_feature_names=self.numeric_feature_names,
            numeric_scale_factors=self.numeric_scale_factors,
            categorical_feature_names=self.categorical_feature_names,
            coefficients=self.coefficients,
            intercept=self.intercept,
        )

    def probability_material(self, row: NewsFeatureRow) -> float:
        """Return the validation-calibrated probability of a material move."""
        raw = self.raw_probability_material(row)
        return _sigmoid(_logit(raw) + self.calibration_offset)

    def predict(self, row: NewsFeatureRow) -> dict[str, object]:
        """Return one artifact-bound, outcome-free materiality prediction."""
        raw = self.raw_probability_material(row)
        calibrated = _sigmoid(_logit(raw) + self.calibration_offset)
        return {
            "schema_version": PREDICTION_SCHEMA_VERSION,
            "example_id": row.example_id,
            "event_id": row.event_id,
            "event_group_id": row.event_group_id,
            "ticker": row.ticker,
            "source_id": row.source_id,
            "decision_at": row.decision_at.isoformat(),
            "model_id": self.model_id,
            "model_version": self.model_version,
            "artifact_payload_sha256": self.artifact_payload_sha256,
            "raw_probability_material": raw,
            "calibrated_probability_material": calibrated,
            "selected_at_coverage_pct": {
                score: [
                    int(coverage)
                    for coverage, threshold in sorted(
                        thresholds.items(), key=lambda item: int(item[0])
                    )
                    if (raw if score == "raw" else calibrated) >= threshold
                ]
                for score, thresholds in self.coverage_thresholds.items()
            },
        }


@dataclass(frozen=True)
class PortableNewsDirectionModel:
    """Validated directional model independent of sklearn and pickle."""

    artifact_payload_sha256: str
    model_id: str
    model_version: str
    spec: NewsFeatureSpec
    numeric_feature_names: tuple[str, ...]
    numeric_scale_factors: tuple[float, ...]
    categorical_feature_names: tuple[str, ...]
    coefficients: tuple[float, ...]
    intercept: float
    calibration_offset: float
    coverage_thresholds: Mapping[str, Mapping[str, float]]

    def raw_probability_up(self, row: NewsFeatureRow) -> float:
        """Return the uncalibrated probability of a positive abnormal return."""
        return _portable_probability(
            row,
            spec=self.spec,
            numeric_feature_names=self.numeric_feature_names,
            numeric_scale_factors=self.numeric_scale_factors,
            categorical_feature_names=self.categorical_feature_names,
            coefficients=self.coefficients,
            intercept=self.intercept,
        )

    def probability_up(self, row: NewsFeatureRow) -> float:
        """Return the validation-calibrated probability of an upward move."""
        raw = self.raw_probability_up(row)
        return _sigmoid(_logit(raw) + self.calibration_offset)

    def predict(self, row: NewsFeatureRow) -> dict[str, object]:
        """Return one artifact-bound, outcome-free directional prediction."""
        raw = self.raw_probability_up(row)
        calibrated = _sigmoid(_logit(raw) + self.calibration_offset)
        return {
            "schema_version": DIRECTION_PREDICTION_SCHEMA_VERSION,
            "example_id": row.example_id,
            "event_id": row.event_id,
            "event_group_id": row.event_group_id,
            "ticker": row.ticker,
            "source_id": row.source_id,
            "decision_at": row.decision_at.isoformat(),
            "model_id": self.model_id,
            "model_version": self.model_version,
            "artifact_payload_sha256": self.artifact_payload_sha256,
            "raw_probability_up": raw,
            "raw_direction": _direction(raw),
            "raw_confidence": max(raw, 1 - raw),
            "calibrated_probability_up": calibrated,
            "calibrated_direction": _direction(calibrated),
            "calibrated_confidence": max(calibrated, 1 - calibrated),
            "selected_at_coverage_pct": {
                "raw": _selected_direction_coverages(
                    raw, self.coverage_thresholds["raw"]
                ),
                "calibrated": _selected_direction_coverages(
                    calibrated, self.coverage_thresholds["calibrated"]
                ),
            },
        }


def build_portable_materiality_artifact(
    fitted: FittedNewsModel,
    *,
    raw_validation_probabilities: Sequence[float],
    fold: Mapping[str, object],
    dataset_sha256: str,
    fit_bundle_sha256: str,
    feature_input_filename: str,
    feature_input_sha256: str,
    training_rows: int,
    validation_rows: int,
) -> dict[str, object]:
    """Serialize one fitted sklearn materiality model without executable code."""
    raw_validation_probabilities = tuple(float(value) for value in raw_validation_probabilities)
    if len(raw_validation_probabilities) != fitted.calibrator.observations:
        raise ValueError("raw validation probabilities are unaligned")
    artifact = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "model_id": MODEL_ID,
        "model_version": MODEL_VERSION,
        "task": "materiality",
        "target": TARGET,
        "target_definition": TARGET_DEFINITION,
        "decision_mode": "update_5m",
        "feature_spec": _serialize_spec(fitted.spec),
        "preprocessing": _serialize_preprocessing(fitted),
        "classifier": _serialize_classifier(fitted),
        "calibration": _serialize_calibration(fitted),
        "coverage_thresholds": {
            "raw": _materiality_coverage_thresholds(raw_validation_probabilities),
            "calibrated": _materiality_coverage_thresholds(
                fitted.validation_probabilities_up
            ),
        },
        "provenance": _provenance(
            fold,
            dataset_sha256=dataset_sha256,
            fit_bundle_sha256=fit_bundle_sha256,
            feature_input_filename=feature_input_filename,
            feature_input_sha256=feature_input_sha256,
            training_rows=training_rows,
            validation_rows=validation_rows,
        ),
    }
    artifact["artifact_payload_sha256"] = artifact_payload_sha256(artifact)
    load_portable_materiality_payload(artifact)
    return artifact


def build_portable_direction_artifact(
    fitted: FittedNewsModel,
    *,
    raw_validation_probabilities: Sequence[float],
    fold: Mapping[str, object],
    dataset_sha256: str,
    fit_bundle_sha256: str,
    feature_input_filename: str,
    feature_input_sha256: str,
    training_rows: int,
    validation_rows: int,
) -> dict[str, object]:
    """Serialize the retained directional classifier without executable code."""
    raw_probabilities = tuple(float(value) for value in raw_validation_probabilities)
    if len(raw_probabilities) != fitted.calibrator.observations:
        raise ValueError("direction raw validation probabilities are unaligned")
    contract = _direction_contract_for_spec(fitted.spec.name)
    if fitted.spec != retained_feature_specs()[contract.feature_spec_name]:
        raise ValueError("direction model uses an incompatible feature specification")
    artifact = {
        "schema_version": DIRECTION_ARTIFACT_SCHEMA_VERSION,
        "model_id": contract.model_id,
        "model_version": contract.model_version,
        "task": "direction",
        "target": DIRECTION_TARGET,
        "target_definition": DIRECTION_TARGET_DEFINITION,
        "decision_mode": "update_5m",
        "feature_spec": _serialize_spec(fitted.spec),
        "preprocessing": _serialize_preprocessing(fitted),
        "classifier": _serialize_classifier(fitted),
        "calibration": _serialize_calibration(fitted),
        "coverage_thresholds": {
            "raw": _direction_coverage_thresholds(raw_probabilities),
            "calibrated": _direction_coverage_thresholds(
                fitted.validation_probabilities_up
            ),
        },
        "provenance": _provenance(
            fold,
            dataset_sha256=dataset_sha256,
            fit_bundle_sha256=fit_bundle_sha256,
            feature_input_filename=feature_input_filename,
            feature_input_sha256=feature_input_sha256,
            training_rows=training_rows,
            validation_rows=validation_rows,
        ),
    }
    artifact["artifact_payload_sha256"] = artifact_payload_sha256(artifact)
    load_portable_direction_payload(artifact)
    return artifact


def load_portable_materiality_model(
    path: Path,
    *,
    expected_payload_sha256: str | None = None,
) -> PortableNewsMaterialityModel:
    """Load and strictly validate one portable materiality artifact."""
    payload = read_json(path, maximum_bytes=MAXIMUM_ARTIFACT_BYTES)
    if not isinstance(payload, dict):
        raise ValueError("materiality artifact must be a JSON object")
    model = load_portable_materiality_payload(payload)
    if expected_payload_sha256 is not None:
        expected = _digest(expected_payload_sha256, "expected artifact digest")
        if model.artifact_payload_sha256 != expected:
            raise ValueError("materiality artifact does not match the expected digest")
    return model


def load_portable_materiality_payload(
    payload: Mapping[str, object],
) -> PortableNewsMaterialityModel:
    """Validate an in-memory artifact payload and construct its runtime model."""
    _require_exact_keys(payload, _ARTIFACT_KEYS, "artifact")
    _require_constant_fields(payload)
    expected_digest = _digest(payload["artifact_payload_sha256"], "artifact digest")
    if artifact_payload_sha256(payload) != expected_digest:
        raise ValueError("materiality artifact payload digest mismatch")
    spec = _load_feature_spec(_object(payload["feature_spec"], "feature_spec"))
    preprocessing = _object(payload["preprocessing"], "preprocessing")
    classifier = _object(payload["classifier"], "classifier")
    calibration = _object(payload["calibration"], "calibration")
    thresholds = _load_thresholds(payload["coverage_thresholds"])
    numeric_names, scales, categorical_names = _load_preprocessing(preprocessing)
    coefficients, intercept = _load_classifier(
        classifier, len(numeric_names) + len(categorical_names)
    )
    _validate_calibration(calibration)
    provenance = _object(payload["provenance"], "provenance")
    _validate_provenance(provenance)
    if calibration["observations"] != provenance["validation_rows"]:
        raise ValueError("materiality calibration and validation rows are unaligned")
    _validate_threshold_calibration(thresholds, calibration["offset"])
    return PortableNewsMaterialityModel(
        artifact_payload_sha256=expected_digest,
        model_id=MODEL_ID,
        model_version=MODEL_VERSION,
        spec=spec,
        numeric_feature_names=numeric_names,
        numeric_scale_factors=scales,
        categorical_feature_names=categorical_names,
        coefficients=coefficients,
        intercept=intercept,
        calibration_offset=_finite_float(calibration["offset"], "calibration offset"),
        coverage_thresholds=MappingProxyType(
            {
                score: MappingProxyType(dict(values))
                for score, values in thresholds.items()
            }
        ),
    )


def load_portable_direction_model(
    path: Path,
    *,
    expected_payload_sha256: str | None = None,
) -> PortableNewsDirectionModel:
    """Load and strictly validate one portable directional artifact."""
    payload = read_json(path, maximum_bytes=MAXIMUM_ARTIFACT_BYTES)
    if not isinstance(payload, dict):
        raise ValueError("direction artifact must be a JSON object")
    model = load_portable_direction_payload(payload)
    if expected_payload_sha256 is not None:
        expected = _digest(expected_payload_sha256, "expected direction artifact digest")
        if model.artifact_payload_sha256 != expected:
            raise ValueError("direction artifact does not match the expected digest")
    return model


def load_portable_direction_payload(
    payload: Mapping[str, object],
) -> PortableNewsDirectionModel:
    """Validate an in-memory direction artifact and construct its runtime model."""
    _require_exact_keys(payload, _ARTIFACT_KEYS, "direction artifact")
    contract = _direction_contract_from_payload(payload)
    expected_digest = _digest(
        payload["artifact_payload_sha256"], "direction artifact digest"
    )
    if artifact_payload_sha256(payload) != expected_digest:
        raise ValueError("direction artifact payload digest mismatch")
    spec = _load_feature_spec(
        _object(payload["feature_spec"], "direction feature_spec"),
        expected_spec_name=contract.feature_spec_name,
    )
    preprocessing = _object(payload["preprocessing"], "direction preprocessing")
    classifier = _object(payload["classifier"], "direction classifier")
    calibration = _object(payload["calibration"], "direction calibration")
    thresholds = _load_direction_thresholds(payload["coverage_thresholds"])
    numeric_names, scales, categorical_names = _load_preprocessing(
        preprocessing,
        spec=spec,
    )
    coefficients, intercept = _load_classifier(
        classifier, len(numeric_names) + len(categorical_names)
    )
    _validate_calibration(calibration)
    provenance = _object(payload["provenance"], "direction provenance")
    _validate_provenance(provenance)
    if calibration["observations"] != provenance["validation_rows"]:
        raise ValueError("direction calibration and validation rows are unaligned")
    return PortableNewsDirectionModel(
        artifact_payload_sha256=expected_digest,
        model_id=contract.model_id,
        model_version=contract.model_version,
        spec=spec,
        numeric_feature_names=numeric_names,
        numeric_scale_factors=scales,
        categorical_feature_names=categorical_names,
        coefficients=coefficients,
        intercept=intercept,
        calibration_offset=_finite_float(calibration["offset"], "calibration offset"),
        coverage_thresholds=MappingProxyType(
            {
                score: MappingProxyType(dict(values))
                for score, values in thresholds.items()
            }
        ),
    )


def predict_materiality_jsonl(
    model: PortableNewsMaterialityModel,
    input_path: Path,
    output_path: Path,
) -> dict[str, object]:
    """Predict a bounded outcome-free JSONL file and bind rows to the artifact."""
    input_sha256 = signal_dataset_sha256(input_path)
    rows = (
        _deserialize_outcome_free_row(payload)
        for payload in iter_jsonl(input_path, maximum_rows=MAXIMUM_PREDICTION_ROWS)
    )
    count = 0
    seen_ids = set()

    def predictions():
        nonlocal count
        for row in rows:
            if row.example_id in seen_ids:
                raise ValueError("materiality inference input contains duplicate ids")
            seen_ids.add(row.example_id)
            count += 1
            yield model.predict(row)

    write_jsonl(output_path, predictions(), maximum_rows=MAXIMUM_PREDICTION_ROWS)
    if count == 0:
        output_path.unlink(missing_ok=True)
        raise ValueError("materiality inference requires at least one feature row")
    if signal_dataset_sha256(input_path) != input_sha256:
        output_path.unlink(missing_ok=True)
        raise ValueError("materiality inference input changed while it was read")
    return {
        "rows": count,
        "input_sha256": input_sha256,
        "output_sha256": signal_dataset_sha256(output_path),
        "artifact_payload_sha256": model.artifact_payload_sha256,
        "model_id": model.model_id,
        "model_version": model.model_version,
    }


def predict_direction_jsonl(
    model: PortableNewsDirectionModel,
    input_path: Path,
    output_path: Path,
) -> dict[str, object]:
    """Predict bounded outcome-free rows and bind them to a direction artifact."""
    input_sha256 = signal_dataset_sha256(input_path)
    rows = (
        _deserialize_outcome_free_row(payload)
        for payload in iter_jsonl(input_path, maximum_rows=MAXIMUM_PREDICTION_ROWS)
    )
    count = 0
    seen_ids = set()

    def predictions():
        nonlocal count
        for row in rows:
            if row.example_id in seen_ids:
                raise ValueError("direction inference input contains duplicate ids")
            seen_ids.add(row.example_id)
            count += 1
            yield model.predict(row)

    write_jsonl(output_path, predictions(), maximum_rows=MAXIMUM_PREDICTION_ROWS)
    if count == 0:
        output_path.unlink(missing_ok=True)
        raise ValueError("direction inference requires at least one feature row")
    if signal_dataset_sha256(input_path) != input_sha256:
        output_path.unlink(missing_ok=True)
        raise ValueError("direction inference input changed while it was read")
    return {
        "rows": count,
        "input_sha256": input_sha256,
        "output_sha256": signal_dataset_sha256(output_path),
        "artifact_payload_sha256": model.artifact_payload_sha256,
        "model_id": model.model_id,
        "model_version": model.model_version,
    }


def artifact_payload_sha256(payload: Mapping[str, object]) -> str:
    """Hash canonical artifact content while excluding the self-digest field."""
    content = dict(payload)
    content.pop("artifact_payload_sha256", None)
    encoded = json.dumps(
        content,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _serialize_spec(spec: NewsFeatureSpec) -> dict[str, object]:
    return {
        "name": spec.name,
        "numeric_prefixes": list(spec.numeric_prefixes),
        "numeric_names": list(spec.numeric_names),
        "categorical_names": list(spec.categorical_names),
        "include_text": spec.include_text,
    }


def _serialize_preprocessing(fitted: FittedNewsModel) -> dict[str, object]:
    categorical_vectorizer = fitted.transformer.categorical_vectorizer
    categorical_names = (
        list(categorical_vectorizer.get_feature_names_out())
        if categorical_vectorizer is not None
        else []
    )
    return {
        "numeric_feature_names": list(
            fitted.transformer.numeric_vectorizer.get_feature_names_out()
        ),
        "numeric_scale_factors": [
            float(value) for value in fitted.transformer.scaler.scale_
        ],
        "categorical_feature_names": categorical_names,
    }


def _serialize_classifier(fitted: FittedNewsModel) -> dict[str, object]:
    return {
        "kind": "binary_logistic_regression",
        "solver": "liblinear",
        "regularization_c": float(fitted.classifier.C),
        "classes": [int(value) for value in fitted.classifier.classes_],
        "coefficients": [float(value) for value in fitted.classifier.coef_[0]],
        "intercept": float(fitted.classifier.intercept_[0]),
    }


def _serialize_calibration(fitted: FittedNewsModel) -> dict[str, object]:
    return {
        "kind": "validation_logit_offset",
        "offset": float(fitted.calibrator.offset),
        "observed_prevalence": float(fitted.calibrator.observed_prevalence),
        "observations": int(fitted.calibrator.observations),
    }


def _provenance(
    fold: Mapping[str, object],
    **values: object,
) -> dict[str, object]:
    validation_from = str(fold["validation_from"])
    evaluation_from = str(fold["evaluation_from"])
    return {
        **values,
        "fold": str(fold["name"]),
        "fit_policy": "latest_walk_forward_fold_no_post_evaluation_refit",
        "training_until_exclusive": validation_from,
        "validation_from_inclusive": validation_from,
        "validation_until_exclusive": evaluation_from,
        "evaluation_from_inclusive": evaluation_from,
        "evaluation_until_exclusive": str(fold["evaluation_until"]),
    }


def _materiality_coverage_thresholds(
    probabilities: Sequence[float],
) -> dict[str, float]:
    values = [_probability(value, "validation probability") for value in probabilities]
    if not values:
        raise ValueError("materiality coverage requires validation probabilities")
    ordered = sorted(values, reverse=True)
    result = {}
    for coverage in COVERAGE_TARGETS_PCT:
        if coverage == 100:
            result[str(coverage)] = 0.0
            continue
        selected_rows = max(1, math.ceil(len(ordered) * coverage / 100))
        result[str(coverage)] = ordered[selected_rows - 1]
    return result


def _direction_coverage_thresholds(
    probabilities: Sequence[float],
) -> dict[str, float]:
    values = [_probability(value, "direction validation probability") for value in probabilities]
    if not values:
        raise ValueError("direction coverage requires validation probabilities")
    return {
        str(coverage): select_coverage_threshold(values, coverage)
        for coverage in COVERAGE_TARGETS_PCT
    }


def _selected_direction_coverages(
    probability_up: float,
    thresholds: Mapping[str, float],
) -> list[int]:
    confidence = max(probability_up, 1 - probability_up)
    return [
        int(coverage)
        for coverage, threshold in sorted(
            thresholds.items(), key=lambda item: int(item[0])
        )
        if confidence >= threshold
    ]


def _portable_probability(
    row: NewsFeatureRow,
    *,
    spec: NewsFeatureSpec,
    numeric_feature_names: Sequence[str],
    numeric_scale_factors: Sequence[float],
    categorical_feature_names: Sequence[str],
    coefficients: Sequence[float],
    intercept: float,
) -> float:
    numeric = _portable_numeric_record(row, spec)
    categorical = _portable_categorical_record(row, spec)
    linear = intercept
    for index, name in enumerate(numeric_feature_names):
        linear += (
            coefficients[index]
            * numeric.get(name, 0.0)
            / numeric_scale_factors[index]
        )
    offset = len(numeric_feature_names)
    for index, name in enumerate(categorical_feature_names, offset):
        linear += coefficients[index] * float(name in categorical)
    return _sigmoid(linear)


def _direction(probability_up: float) -> str:
    return "up" if probability_up >= 0.5 else "down"


def _portable_numeric_record(
    row: NewsFeatureRow,
    spec: NewsFeatureSpec,
) -> dict[str, float]:
    result = {}
    for name, value in row.numeric.items():
        if name not in spec.numeric_names and not any(
            name.startswith(prefix) for prefix in spec.numeric_prefixes
        ):
            continue
        if value is not None and not math.isfinite(float(value)):
            raise ValueError(f"numeric feature is not finite: {name}")
        result[name] = float(value or 0.0)
        result[f"missing_{name}"] = float(value is None)
    return result


def _portable_categorical_record(
    row: NewsFeatureRow,
    spec: NewsFeatureSpec,
) -> set[str]:
    missing = [name for name in spec.categorical_names if name not in row.categorical]
    if missing:
        raise ValueError(f"missing categorical features: {', '.join(missing)}")
    _validate_identity_categories(row)
    return {f"{name}={row.categorical[name]}" for name in spec.categorical_names}


def _validate_identity_categories(row: NewsFeatureRow) -> None:
    for name, expected in (("ticker", row.ticker), ("source_id", row.source_id)):
        if row.categorical.get(name) != expected:
            raise ValueError(f"feature row {name} identity differs from its category")


def _deserialize_outcome_free_row(payload: object) -> NewsFeatureRow:
    row = _object(payload, "feature row")
    _require_exact_keys(row, _FEATURE_ROW_KEYS, "feature row")
    for name in _FEATURE_ROW_KEYS - {"numeric", "categorical"}:
        if not isinstance(row[name], str):
            raise ValueError(f"feature row {name} must be a string")
    numeric = _object(row["numeric"], "feature row numeric")
    categorical = _object(row["categorical"], "feature row categorical")
    if len(numeric) + len(categorical) > MAXIMUM_MODEL_FEATURES:
        raise ValueError("feature row has too many features")
    for name, value in numeric.items():
        _string(name, "numeric feature name")
        if value is not None:
            _finite_float(value, f"numeric feature {name}")
    for name, value in categorical.items():
        _string(name, "categorical feature name")
        _string(value, f"categorical feature {name}")
    result = deserialize_feature_row(row)
    if result.decision_at.tzinfo is None or result.decision_at.utcoffset() is None:
        raise ValueError("feature row decision_at must be timezone-aware")
    return result


def _require_constant_fields(payload: Mapping[str, object]) -> None:
    expected = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "model_id": MODEL_ID,
        "model_version": MODEL_VERSION,
        "task": "materiality",
        "target": TARGET,
        "target_definition": TARGET_DEFINITION,
        "decision_mode": "update_5m",
    }
    if any(payload.get(name) != value for name, value in expected.items()):
        raise ValueError("materiality artifact identity is incompatible")


def _direction_contract_from_payload(
    payload: Mapping[str, object],
) -> _DirectionArtifactContract:
    expected = {
        "schema_version": DIRECTION_ARTIFACT_SCHEMA_VERSION,
        "task": "direction",
        "target": DIRECTION_TARGET,
        "target_definition": DIRECTION_TARGET_DEFINITION,
        "decision_mode": "update_5m",
    }
    if any(payload.get(name) != value for name, value in expected.items()):
        raise ValueError("direction artifact identity is incompatible")
    for contract in _DIRECTION_CONTRACTS:
        if (
            payload.get("model_id") == contract.model_id
            and payload.get("model_version") == contract.model_version
        ):
            return contract
    raise ValueError("direction artifact model identity is incompatible")


def _direction_contract_for_spec(spec_name: str) -> _DirectionArtifactContract:
    for contract in _DIRECTION_CONTRACTS:
        if contract.feature_spec_name == spec_name:
            return contract
    raise ValueError("direction model uses an unsupported feature specification")


def _load_feature_spec(
    payload: Mapping[str, object],
    *,
    expected_spec_name: str = "update_5m_symmetric_materiality",
) -> NewsFeatureSpec:
    expected_keys = {
        "name",
        "numeric_prefixes",
        "numeric_names",
        "categorical_names",
        "include_text",
    }
    _require_exact_keys(payload, expected_keys, "feature_spec")
    spec = NewsFeatureSpec(
        name=_string(payload["name"], "feature spec name"),
        numeric_prefixes=_string_tuple(payload["numeric_prefixes"], "numeric prefixes"),
        numeric_names=_string_tuple(payload["numeric_names"], "numeric names"),
        categorical_names=_string_tuple(
            payload["categorical_names"], "categorical names"
        ),
        include_text=payload["include_text"],
    )
    expected = retained_feature_specs()[expected_spec_name]
    if not isinstance(spec.include_text, bool) or spec != expected:
        raise ValueError("news model artifact feature spec is incompatible")
    return spec


def _load_preprocessing(
    payload: Mapping[str, object],
    *,
    spec: NewsFeatureSpec | None = None,
) -> tuple[tuple[str, ...], tuple[float, ...], tuple[str, ...]]:
    _require_exact_keys(
        payload,
        {
            "numeric_feature_names",
            "numeric_scale_factors",
            "categorical_feature_names",
        },
        "preprocessing",
    )
    numeric = _string_tuple(payload["numeric_feature_names"], "numeric features")
    categorical = _string_tuple(
        payload["categorical_feature_names"], "categorical features"
    )
    scales = _float_tuple(payload["numeric_scale_factors"], "numeric scales")
    if not numeric or len(numeric) != len(scales) or any(value <= 0 for value in scales):
        raise ValueError("materiality artifact numeric preprocessing is invalid")
    if len(numeric) + len(categorical) > MAXIMUM_MODEL_FEATURES:
        raise ValueError("materiality artifact has too many model features")
    _validate_feature_names(
        numeric,
        categorical,
        spec=spec or retained_feature_specs()["update_5m_symmetric_materiality"],
    )
    return numeric, scales, categorical


def _load_classifier(
    payload: Mapping[str, object],
    feature_count: int,
) -> tuple[tuple[float, ...], float]:
    _require_exact_keys(
        payload,
        {"kind", "solver", "regularization_c", "classes", "coefficients", "intercept"},
        "classifier",
    )
    classes = payload["classes"]
    if (
        payload["kind"] != "binary_logistic_regression"
        or payload["solver"] != "liblinear"
        or not isinstance(classes, list)
        or len(classes) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in classes)
        or classes != [0, 1]
        or _finite_float(payload["regularization_c"], "regularization C") <= 0
    ):
        raise ValueError("materiality artifact classifier is incompatible")
    coefficients = _float_tuple(payload["coefficients"], "classifier coefficients")
    if len(coefficients) != feature_count:
        raise ValueError("materiality artifact coefficient count is invalid")
    return coefficients, _finite_float(payload["intercept"], "classifier intercept")


def _validate_calibration(payload: Mapping[str, object]) -> None:
    _require_exact_keys(
        payload,
        {"kind", "offset", "observed_prevalence", "observations"},
        "calibration",
    )
    prevalence = _finite_float(payload["observed_prevalence"], "prevalence")
    observations = payload["observations"]
    if (
        payload["kind"] != "validation_logit_offset"
        or not 0 < prevalence < 1
        or isinstance(observations, bool)
        or not isinstance(observations, int)
        or observations <= 0
    ):
        raise ValueError("materiality artifact calibration is invalid")
    _finite_float(payload["offset"], "calibration offset")


def _load_thresholds(value: object) -> dict[str, dict[str, float]]:
    payload = _object(value, "coverage thresholds")
    _require_exact_keys(payload, {"raw", "calibrated"}, "coverage thresholds")
    result = {}
    for score in ("raw", "calibrated"):
        thresholds = _object(payload[score], f"{score} coverage thresholds")
        _require_exact_keys(thresholds, {"20", "50", "100"}, f"{score} thresholds")
        result[score] = {
            coverage: _probability(threshold, f"{score} {coverage} threshold")
            for coverage, threshold in thresholds.items()
        }
        if not (
            result[score]["20"] >= result[score]["50"] >= result[score]["100"]
            and result[score]["100"] == 0.0
        ):
            raise ValueError("materiality coverage thresholds are not monotonic")
    return result


def _load_direction_thresholds(value: object) -> dict[str, dict[str, float]]:
    payload = _object(value, "direction coverage thresholds")
    _require_exact_keys(
        payload,
        {"raw", "calibrated"},
        "direction coverage thresholds",
    )
    result = {}
    for score in ("raw", "calibrated"):
        thresholds = _object(
            payload[score], f"direction {score} coverage thresholds"
        )
        _require_exact_keys(
            thresholds,
            {"20", "50", "100"},
            f"direction {score} thresholds",
        )
        result[score] = {
            coverage: _probability(threshold, f"direction {score} {coverage} threshold")
            for coverage, threshold in thresholds.items()
        }
        if not (
            result[score]["20"] >= result[score]["50"] >= result[score]["100"]
            and result[score]["100"] == 0.5
        ):
            raise ValueError("direction coverage thresholds are not monotonic")
    return result


def _validate_feature_names(
    numeric: Sequence[str],
    categorical: Sequence[str],
    *,
    spec: NewsFeatureSpec,
) -> None:
    for feature in numeric:
        base = feature.removeprefix("missing_")
        if base not in spec.numeric_names and not any(
            base.startswith(prefix) for prefix in spec.numeric_prefixes
        ):
            raise ValueError("news model artifact has an unknown numeric feature")
    prefixes = tuple(f"{name}=" for name in spec.categorical_names)
    if any(not feature.startswith(prefixes) for feature in categorical):
        raise ValueError("news model artifact has an unknown categorical feature")


def _validate_threshold_calibration(
    thresholds: Mapping[str, Mapping[str, float]],
    offset: object,
) -> None:
    calibration_offset = _finite_float(offset, "calibration offset")
    for coverage in ("20", "50"):
        expected = _sigmoid(_logit(thresholds["raw"][coverage]) + calibration_offset)
        if not math.isclose(
            thresholds["calibrated"][coverage], expected, rel_tol=0.0, abs_tol=1e-12
        ):
            raise ValueError("materiality coverage threshold calibration is invalid")


def _validate_provenance(payload: Mapping[str, object]) -> None:
    keys = {
        "dataset_sha256",
        "fit_bundle_sha256",
        "feature_input_filename",
        "feature_input_sha256",
        "fold",
        "fit_policy",
        "training_rows",
        "validation_rows",
        "training_until_exclusive",
        "validation_from_inclusive",
        "validation_until_exclusive",
        "evaluation_from_inclusive",
        "evaluation_until_exclusive",
    }
    _require_exact_keys(payload, keys, "provenance")
    for name in ("dataset_sha256", "fit_bundle_sha256", "feature_input_sha256"):
        _digest(payload[name], name)
    filename = _string(payload["feature_input_filename"], "feature input filename")
    if Path(filename).name != filename or Path(filename).suffix != ".jsonl":
        raise ValueError("materiality artifact feature input filename is invalid")
    _string(payload["fold"], "fold")
    if payload["fit_policy"] != "latest_walk_forward_fold_no_post_evaluation_refit":
        raise ValueError("materiality artifact fit policy is incompatible")
    for name in ("training_rows", "validation_rows"):
        value = payload[name]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"materiality artifact {name} is invalid")
    cutoffs = [
        _aware_datetime(payload[name], name)
        for name in (
            "training_until_exclusive",
            "validation_from_inclusive",
            "validation_until_exclusive",
            "evaluation_from_inclusive",
            "evaluation_until_exclusive",
        )
    ]
    if (
        cutoffs[0] != cutoffs[1]
        or cutoffs[2] != cutoffs[3]
        or cutoffs != sorted(cutoffs)
        or cutoffs[1] >= cutoffs[2]
        or cutoffs[3] >= cutoffs[4]
    ):
        raise ValueError("materiality artifact temporal cutoffs are invalid")


def _require_exact_keys(
    payload: Mapping[str, object],
    expected: set[str],
    name: str,
) -> None:
    if set(payload) != expected:
        raise ValueError(f"materiality {name} fields are incompatible")


def _object(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"materiality {name} must be an object")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAXIMUM_STRING_LENGTH:
        raise ValueError(f"materiality {name} must be a non-empty string")
    return value


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"materiality {name} must be a list")
    result = tuple(_string(item, name) for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"materiality {name} must be unique")
    return result


def _float_tuple(value: object, name: str) -> tuple[float, ...]:
    if not isinstance(value, list):
        raise ValueError(f"materiality {name} must be a list")
    return tuple(_finite_float(item, name) for item in value)


def _finite_float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"materiality {name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"materiality {name} must be finite")
    return result


def _probability(value: object, name: str) -> float:
    result = _finite_float(value, name)
    if not 0 <= result <= 1:
        raise ValueError(f"materiality {name} must be within [0, 1]")
    return result


def _digest(value: object, name: str) -> str:
    result = _string(value, name)
    if not _DIGEST_PATTERN.fullmatch(result):
        raise ValueError(f"materiality {name} is not a SHA-256 digest")
    return result


def _aware_datetime(value: object, name: str) -> datetime:
    try:
        result = datetime.fromisoformat(_string(value, name))
    except ValueError as error:
        raise ValueError(f"materiality {name} is not an ISO timestamp") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"materiality {name} must be timezone-aware")
    return result


def _sigmoid(value: float) -> float:
    if value >= 0:
        exponent = math.exp(-value)
        return 1 / (1 + exponent)
    exponent = math.exp(value)
    return exponent / (1 + exponent)


def _logit(value: float) -> float:
    clipped = min(max(value, 1e-12), 1 - 1e-12)
    return math.log(clipped / (1 - clipped))
