from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eventedge_research.research_artifacts import iter_jsonl_objects, write_jsonl
from eventedge_research.signal_dataset import (
    SignalDatasetExample,
    signal_dataset_schema_version,
    signal_information_group_id,
)

DATASET_HASH_PATTERN = r"^[0-9a-f]{64}$"
WILSON_95_Z_SCORE = 1.959963984540054


class PredictionDirection(StrEnum):
    """A model decision; abstain is explicit so missing rows cannot game coverage."""

    UP = "up"
    DOWN = "down"
    ABSTAIN = "abstain"


class EvaluationTarget(StrEnum):
    """Market return whose sign defines a hit during training and evaluation."""

    RAW_RETURN = "raw_return"
    ABNORMAL_RETURN = "abnormal_return"


class SignalPrediction(BaseModel):
    """One point-in-time prediction for a signal dataset example."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    schema_version: Literal["signal-prediction-1.0"] = "signal-prediction-1.0"
    example_id: Annotated[str, Field(min_length=1, max_length=200)]
    event_id: Annotated[str, Field(min_length=1, max_length=200)]
    ticker: Annotated[str, Field(pattern=r"^[A-Z0-9._-]{1,32}$")]
    decision_at: datetime
    feature_cutoff_at: datetime
    direction: PredictionDirection
    confidence: Annotated[float, Field(ge=0, le=1)]
    model_version: Annotated[str, Field(min_length=1, max_length=100)]
    config_version: Annotated[int, Field(ge=1)]
    model_artifact_sha256: Annotated[str, Field(pattern=DATASET_HASH_PATTERN)] | None = None
    partition: Literal["validation", "test", "shadow"]
    dataset_sha256: Annotated[str, Field(pattern=DATASET_HASH_PATTERN)]
    training_labels_through: datetime | None = None

    @model_validator(mode="after")
    def require_point_in_time_provenance(self) -> SignalPrediction:
        _require_aware("decision_at", self.decision_at)
        _require_aware("feature_cutoff_at", self.feature_cutoff_at)
        if self.feature_cutoff_at > self.decision_at:
            raise ValueError("feature_cutoff_at cannot be later than decision_at")
        if self.training_labels_through is not None:
            _require_aware("training_labels_through", self.training_labels_through)
            if self.training_labels_through >= self.decision_at:
                raise ValueError("training labels must predate the prediction decision")
        return self


def load_signal_predictions(path: Path) -> list[SignalPrediction]:
    """Load JSONL predictions and require one row per example id."""
    predictions: list[SignalPrediction] = []
    seen_ids: set[str] = set()
    for line_number, payload in enumerate(iter_jsonl_objects(path), 1):
        try:
            prediction = SignalPrediction.model_validate(payload)
        except Exception as error:
            raise ValueError(
                f"invalid signal prediction at row {line_number}: {error}"
            ) from error
        if prediction.example_id in seen_ids:
            raise ValueError(
                f"duplicate signal prediction at row {line_number}: "
                f"{prediction.example_id}"
            )
        seen_ids.add(prediction.example_id)
        predictions.append(prediction)
    if not predictions:
        raise ValueError("signal predictions are empty")
    return predictions


def write_signal_predictions(path: Path, predictions: Sequence[SignalPrediction]) -> None:
    """Atomically write deterministic JSONL predictions."""
    if not predictions:
        raise ValueError("cannot write empty signal predictions")
    write_jsonl(
        path,
        (prediction.model_dump(mode="json") for prediction in predictions),
    )


def evaluate_signal_predictions(
    examples: Sequence[SignalDatasetExample],
    predictions: Sequence[SignalPrediction],
    *,
    dataset_sha256: str,
    expected_partition: Literal["validation", "test", "shadow"] | None = None,
    target: EvaluationTarget = EvaluationTarget.RAW_RETURN,
) -> dict[str, object]:
    """Evaluate a complete set of explicit up/down/abstain decisions."""
    if not examples:
        raise ValueError("cannot evaluate an empty signal dataset partition")
    prediction_by_id = {prediction.example_id: prediction for prediction in predictions}
    if len(prediction_by_id) != len(predictions):
        raise ValueError("predictions contain duplicate example ids")
    example_by_id = {example.id: example for example in examples}
    if len(example_by_id) != len(examples):
        raise ValueError("evaluation examples contain duplicate ids")
    event_tickers = {(example.event_id, example.ticker) for example in examples}
    if len(event_tickers) != len(examples):
        raise ValueError("evaluation examples contain duplicate event/ticker pairs")
    missing_targets = [
        example.id for example in examples if signal_target_return(example, target) is None
    ]
    if missing_targets:
        raise ValueError(
            f"evaluation target {target.value!r} is unavailable for "
            f"examples={missing_targets[:5]!r}"
        )
    missing = sorted(example_by_id.keys() - prediction_by_id.keys())
    unknown = sorted(prediction_by_id.keys() - example_by_id.keys())
    if missing or unknown:
        raise ValueError(
            "prediction coverage must be explicit; "
            f"missing={missing[:5]!r}, unknown={unknown[:5]!r}"
        )

    aligned = []
    for example in examples:
        prediction = prediction_by_id[example.id]
        _validate_prediction_alignment(example, prediction, dataset_sha256=dataset_sha256)
        aligned.append((example, prediction))
    identities = {
        (
            prediction.model_version,
            prediction.config_version,
            prediction.model_artifact_sha256,
            prediction.partition,
        )
        for _, prediction in aligned
    }
    if len(identities) != 1:
        raise ValueError(
            "one benchmark report cannot mix model, config, artifact or partition identities"
        )
    model_version, config_version, model_artifact_sha256, partition = identities.pop()
    if expected_partition is not None and partition != expected_partition:
        raise ValueError(
            f"prediction partition {partition!r} does not match {expected_partition!r}"
        )

    metrics = _metric_slice(aligned, target=target)
    return {
        "schema_version": "signal-benchmark-report-2.0",
        "dataset_schema_version": signal_dataset_schema_version(examples),
        "prediction_schema_version": "signal-prediction-1.0",
        "dataset_sha256": dataset_sha256,
        "model_version": model_version,
        "config_version": config_version,
        "model_artifact_sha256": model_artifact_sha256,
        "partition": partition,
        "evaluation_target": target.value,
        "label_sources": sorted({example.label_source.value for example, _ in aligned}),
        "metrics": metrics,
        "by_prediction": {
            direction.value: _direction_metrics(aligned, direction, target=target)
            for direction in (PredictionDirection.UP, PredictionDirection.DOWN)
        },
    }


def wilson_interval_95(hits: int, observations: int) -> tuple[float | None, float | None]:
    """Return the two-sided 95% Wilson interval as fractions."""
    if hits < 0 or observations < 0 or hits > observations:
        raise ValueError("Wilson counts must satisfy 0 <= hits <= observations")
    if observations == 0:
        return None, None
    probability = hits / observations
    z_squared = WILSON_95_Z_SCORE**2
    denominator = 1 + z_squared / observations
    center = probability + z_squared / (2 * observations)
    margin = WILSON_95_Z_SCORE * math.sqrt(
        probability * (1 - probability) / observations
        + z_squared / (4 * observations**2)
    )
    return (center - margin) / denominator, (center + margin) / denominator


def select_confidence_threshold(
    examples: Sequence[SignalDatasetExample],
    probabilities_up: Sequence[float],
    *,
    target: EvaluationTarget,
    minimum_predictions: int,
    minimum_coverage_pct: float,
) -> dict[str, int | float]:
    """Select a validation-only abstention threshold by the Wilson lower bound."""
    if not examples or len(examples) != len(probabilities_up):
        raise ValueError("validation examples and probabilities must be non-empty and aligned")
    if minimum_predictions < 1 or not 0 < minimum_coverage_pct <= 100:
        raise ValueError("confidence threshold constraints are invalid")
    if any(not math.isfinite(value) or not 0 <= value <= 1 for value in probabilities_up):
        raise ValueError("validation probabilities must be finite values in [0, 1]")
    target_values = [signal_target_return(example, target) for example in examples]
    if any(value is None for value in target_values):
        raise ValueError(f"validation target {target.value!r} is unavailable")

    candidates = sorted({0.5, *(max(value, 1 - value) for value in probabilities_up)})
    scored = []
    for threshold in candidates:
        selected = [
            (value, probability)
            for value, probability in zip(target_values, probabilities_up, strict=True)
            if max(probability, 1 - probability) >= threshold
        ]
        coverage = len(selected) / len(examples) * 100
        if len(selected) < minimum_predictions or coverage < minimum_coverage_pct:
            continue
        hits = sum(
            probability >= 0.5 and value > 0 or probability < 0.5 and value < 0
            for value, probability in selected
            if value is not None
        )
        hit_rate = hits / len(selected) * 100
        lower, _ = wilson_interval_95(hits, len(selected))
        scored.append((lower or 0, hit_rate, len(selected), threshold, hits, coverage))
    if not scored:
        raise ValueError("validation partition is too small for threshold selection constraints")
    lower, hit_rate, count, threshold, hits, coverage = max(scored)
    return {
        "confidence_threshold": threshold,
        "validation_observations": len(examples),
        "validation_directional_predictions": count,
        "validation_hits": hits,
        "validation_hit_rate_pct": round(hit_rate, 4),
        "validation_selection_coverage_pct": round(coverage, 4),
        "validation_wilson_lower_pct": round(lower * 100, 4),
    }


def select_asymmetric_probability_policy(
    examples: Sequence[SignalDatasetExample],
    probabilities_up: Sequence[float],
    *,
    target: EvaluationTarget,
    minimum_predictions: int,
    minimum_predictions_per_direction: int,
    minimum_coverage_pct: float,
    maximum_threshold_candidates: int = 25,
    required_abstention_probability: float | None = None,
) -> dict[str, int | float]:
    """Choose validation-only up/down cutoffs with an abstention band.

    Candidate down and up cutoffs are rank-subsampled before the Cartesian
    search. This keeps the policy deterministic and limits threshold overfit.
    """
    if not examples or len(examples) != len(probabilities_up):
        raise ValueError("validation examples and probabilities must be non-empty and aligned")
    if (
        minimum_predictions < 1
        or minimum_predictions_per_direction < 1
        or not 0 < minimum_coverage_pct <= 100
        or maximum_threshold_candidates < 2
    ):
        raise ValueError("asymmetric probability policy constraints are invalid")
    if any(not math.isfinite(value) or not 0 <= value <= 1 for value in probabilities_up):
        raise ValueError("validation probabilities must be finite values in [0, 1]")
    if required_abstention_probability is not None and (
        not math.isfinite(required_abstention_probability)
        or not 0 <= required_abstention_probability <= 1
    ):
        raise ValueError("required abstention probability must be finite and in [0, 1]")
    target_values = [signal_target_return(example, target) for example in examples]
    if any(value is None for value in target_values):
        raise ValueError(f"validation target {target.value!r} is unavailable")
    observations = [
        (probability, float(value))
        for probability, value in zip(probabilities_up, target_values, strict=True)
        if value is not None
    ]
    down_options = _directional_threshold_options(
        observations,
        direction=PredictionDirection.DOWN,
        maximum_candidates=maximum_threshold_candidates,
    )
    up_options = _directional_threshold_options(
        observations,
        direction=PredictionDirection.UP,
        maximum_candidates=maximum_threshold_candidates,
    )

    scored = []
    for down_threshold, down_count, down_hits in down_options:
        if down_count < minimum_predictions_per_direction:
            continue
        for up_threshold, up_count, up_hits in up_options:
            if up_count < minimum_predictions_per_direction or down_threshold >= up_threshold:
                continue
            if required_abstention_probability is not None and not (
                down_threshold < required_abstention_probability < up_threshold
            ):
                continue
            selected = down_count + up_count
            coverage = selected / len(examples) * 100
            if selected < minimum_predictions or coverage < minimum_coverage_pct:
                continue
            hits = down_hits + up_hits
            hit_rate = hits / selected * 100
            lower, _ = wilson_interval_95(hits, selected)
            scored.append(
                (
                    lower or 0,
                    hit_rate,
                    selected,
                    up_threshold - down_threshold,
                    down_threshold,
                    up_threshold,
                    down_count,
                    up_count,
                    hits,
                    coverage,
                )
            )
    if not scored:
        raise ValueError("validation partition is too small for asymmetric policy constraints")
    (
        lower,
        hit_rate,
        count,
        _,
        down_threshold,
        up_threshold,
        down_count,
        up_count,
        hits,
        coverage,
    ) = max(scored)
    logit_offset, confidence_threshold = _asymmetric_policy_transform(
        down_threshold,
        up_threshold,
    )
    return {
        "probability_logit_offset": logit_offset,
        "confidence_threshold": confidence_threshold,
        "down_raw_probability_threshold": down_threshold,
        "up_raw_probability_threshold": up_threshold,
        "validation_observations": len(examples),
        "validation_directional_predictions": count,
        "validation_down_predictions": down_count,
        "validation_up_predictions": up_count,
        "validation_hits": hits,
        "validation_hit_rate_pct": round(hit_rate, 4),
        "validation_selection_coverage_pct": round(coverage, 4),
        "validation_wilson_lower_pct": round(lower * 100, 4),
    }


def _directional_threshold_options(
    observations: Sequence[tuple[float, float]],
    *,
    direction: PredictionDirection,
    maximum_candidates: int,
) -> list[tuple[float, int, int]]:
    reverse = direction is PredictionDirection.UP
    ordered = sorted(observations, key=lambda row: row[0], reverse=reverse)
    options = []
    hits = 0
    for index, (probability, target_value) in enumerate(ordered, 1):
        hits += int(
            direction is PredictionDirection.UP and target_value > 0
            or direction is PredictionDirection.DOWN and target_value < 0
        )
        next_probability = ordered[index][0] if index < len(ordered) else None
        if next_probability != probability:
            options.append((probability, index, hits))
    if len(options) <= maximum_candidates:
        return options
    indices = {
        round(position * (len(options) - 1) / (maximum_candidates - 1))
        for position in range(maximum_candidates)
    }
    return [options[index] for index in sorted(indices)]


def _asymmetric_policy_transform(
    down_threshold: float,
    up_threshold: float,
) -> tuple[float, float]:
    epsilon = 1e-12
    down_logit = math.log(
        min(max(down_threshold, epsilon), 1 - epsilon)
        / (1 - min(max(down_threshold, epsilon), 1 - epsilon))
    )
    up_logit = math.log(
        min(max(up_threshold, epsilon), 1 - epsilon)
        / (1 - min(max(up_threshold, epsilon), 1 - epsilon))
    )
    decision_logit = (down_logit + up_logit) / 2
    confidence_threshold = 1 / (1 + math.exp(-(up_logit - down_logit) / 2))
    return -decision_logit, confidence_threshold


def _validate_prediction_alignment(
    example: SignalDatasetExample,
    prediction: SignalPrediction,
    *,
    dataset_sha256: str,
) -> None:
    if prediction.event_id != example.event_id or prediction.ticker != example.ticker:
        raise ValueError(f"prediction identity does not match example {example.id}")
    if prediction.decision_at != example.decision_at:
        raise ValueError(f"prediction decision_at does not match example {example.id}")
    if prediction.feature_cutoff_at > example.decision_at:
        raise ValueError(f"prediction for {example.id} uses future features")
    if prediction.dataset_sha256 != dataset_sha256:
        raise ValueError(f"prediction dataset fingerprint does not match example {example.id}")


def _metric_slice(
    aligned: Sequence[tuple[SignalDatasetExample, SignalPrediction]],
    *,
    target: EvaluationTarget,
) -> dict[str, object]:
    directional = [
        (example, prediction)
        for example, prediction in aligned
        if prediction.direction is not PredictionDirection.ABSTAIN
    ]
    verdicts = [
        _is_hit(example, prediction, target=target)
        for example, prediction in directional
    ]
    signed_returns = [_signed_return(example, prediction) for example, prediction in directional]
    abnormal_returns = [
        value
        for example, prediction in directional
        if (value := _signed_abnormal_return(example, prediction)) is not None
    ]
    hits = sum(verdicts)
    lower, upper = wilson_interval_95(hits, len(directional))
    return {
        "eligible_event_tickers": len(aligned),
        "independent_events": len({example.event_id for example, _ in aligned}),
        "information_groups": len(
            {signal_information_group_id(example) for example, _ in aligned}
        ),
        "directional_predictions": len(directional),
        "directional_events": len({example.event_id for example, _ in directional}),
        "abstentions": len(aligned) - len(directional),
        "hits": hits,
        "misses": len(directional) - hits,
        "hit_rate_pct": _percentage(hits, len(directional)),
        "selection_coverage_pct": _percentage(len(directional), len(aligned)) or 0.0,
        "wilson_95": {
            "lower_pct": round(lower * 100, 4) if lower is not None else None,
            "upper_pct": round(upper * 100, 4) if upper is not None else None,
        },
        "average_signed_return_pct": _mean(signed_returns),
        "median_signed_return_pct": _median(signed_returns),
        "average_signed_abnormal_return_pct": _mean(abnormal_returns),
        "brier_score_confidence": _brier_score(directional, verdicts),
    }


def _direction_metrics(
    aligned: Sequence[tuple[SignalDatasetExample, SignalPrediction]],
    direction: PredictionDirection,
    *,
    target: EvaluationTarget,
) -> dict[str, object]:
    selected = [
        (example, prediction)
        for example, prediction in aligned
        if prediction.direction is direction
    ]
    hits = sum(
        _is_hit(example, prediction, target=target)
        for example, prediction in selected
    )
    return {
        "predictions": len(selected),
        "hits": hits,
        "hit_rate_pct": _percentage(hits, len(selected)),
        "average_signed_return_pct": _mean(
            [_signed_return(example, prediction) for example, prediction in selected]
        ),
    }


def signal_target_return(
    example: SignalDatasetExample,
    target: EvaluationTarget,
) -> float | None:
    """Return the point-in-time market label selected for an experiment."""
    if target is EvaluationTarget.RAW_RETURN:
        return example.outcome_4h.return_pct
    return example.outcome_4h.abnormal_return_pct


def _is_hit(
    example: SignalDatasetExample,
    prediction: SignalPrediction,
    *,
    target: EvaluationTarget,
) -> bool:
    value = signal_target_return(example, target)
    if value is None:
        raise ValueError(f"evaluation target {target.value!r} is unavailable for {example.id}")
    return bool(
        prediction.direction is PredictionDirection.UP and value > 0
        or prediction.direction is PredictionDirection.DOWN and value < 0
    )


def _signed_return(example: SignalDatasetExample, prediction: SignalPrediction) -> float:
    multiplier = 1 if prediction.direction is PredictionDirection.UP else -1
    return example.outcome_4h.return_pct * multiplier


def _signed_abnormal_return(
    example: SignalDatasetExample,
    prediction: SignalPrediction,
) -> float | None:
    value = example.outcome_4h.abnormal_return_pct
    if value is None:
        return None
    multiplier = 1 if prediction.direction is PredictionDirection.UP else -1
    return value * multiplier


def _percentage(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator * 100, 4) if denominator else None


def _mean(values: Sequence[float]) -> float | None:
    return round(statistics.mean(values), 4) if values else None


def _median(values: Sequence[float]) -> float | None:
    return round(statistics.median(values), 4) if values else None


def _brier_score(
    directional: Sequence[tuple[SignalDatasetExample, SignalPrediction]],
    verdicts: Sequence[bool],
) -> float | None:
    if not directional:
        return None
    errors = [
        (prediction.confidence - float(verdict)) ** 2
        for (_, prediction), verdict in zip(directional, verdicts, strict=True)
    ]
    return round(statistics.mean(errors), 6)


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
