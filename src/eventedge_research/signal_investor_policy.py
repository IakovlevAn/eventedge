"""Materiality, direction and neutral-policy primitives for investor signals."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from eventedge_research.signal_benchmark import (
    PredictionDirection,
    SignalPrediction,
    wilson_interval_95,
)
from eventedge_research.signal_dataset import SignalDatasetExample

MATERIAL_MOVE_ABS_RETURN_PCT = 0.5


@dataclass(frozen=True)
class ValidationLogitOffset:
    """Validation-fitted intercept calibration that preserves probability order."""

    offset: float
    observed_prevalence: float
    observations: int

    @classmethod
    def fit(cls, labels: Sequence[bool], probabilities: Sequence[float]) -> ValidationLogitOffset:
        """Match mean calibrated probability to validation prevalence."""
        _validate_binary_inputs(labels, probabilities)
        prevalence = statistics.fmean(labels)
        if prevalence in {0.0, 1.0}:
            raise ValueError("logit-offset calibration requires both classes")
        logits = [_logit(value) for value in probabilities]
        lower, upper = -20.0, 20.0
        for _ in range(100):
            midpoint = (lower + upper) / 2
            mean = statistics.fmean(_sigmoid(value + midpoint) for value in logits)
            if mean < prevalence:
                lower = midpoint
            else:
                upper = midpoint
        offset = (lower + upper) / 2
        return cls(
            offset=offset,
            observed_prevalence=prevalence,
            observations=len(labels),
        )

    def calibrate(self, probabilities: Sequence[float]) -> list[float]:
        """Apply the frozen order-preserving offset to later probabilities."""
        _validate_probabilities(probabilities)
        return [_sigmoid(_logit(value) + self.offset) for value in probabilities]


def materiality_labels(
    examples: Sequence[SignalDatasetExample],
    *,
    threshold_pct: float = MATERIAL_MOVE_ABS_RETURN_PCT,
) -> list[bool]:
    """Return the predeclared raw-return material-move label."""
    if not examples or not math.isfinite(threshold_pct) or threshold_pct <= 0:
        raise ValueError("materiality labels require rows and a positive threshold")
    return [abs(row.outcome_4h.return_pct) >= threshold_pct for row in examples]


def binary_probability_diagnostics(
    labels: Sequence[bool],
    probabilities: Sequence[float],
    *,
    positive_name: str,
    negative_name: str,
) -> dict[str, object]:
    """Evaluate a general binary probability without fitting calibration."""
    _validate_binary_inputs(labels, probabilities)
    if not positive_name or not negative_name or positive_name == negative_name:
        raise ValueError("binary metric class names must be distinct")
    positives = sum(labels)
    negatives = len(labels) - positives
    true_positive = sum(
        label and probability >= 0.5
        for label, probability in zip(labels, probabilities, strict=True)
    )
    true_negative = sum(
        not label and probability < 0.5
        for label, probability in zip(labels, probabilities, strict=True)
    )
    predicted_positive = sum(value >= 0.5 for value in probabilities)
    bins = _reliability_bins(labels, probabilities)
    from sklearn.metrics import roc_auc_score

    return {
        "rows": len(labels),
        "threshold": 0.5,
        "class_counts": {positive_name: positives, negative_name: negatives},
        "accuracy_pct": (true_positive + true_negative) / len(labels) * 100,
        "balanced_accuracy_pct": (
            (true_positive / positives + true_negative / negatives) * 50
            if positives and negatives
            else None
        ),
        "positive_precision_pct": (
            true_positive / predicted_positive * 100 if predicted_positive else None
        ),
        "positive_recall_pct": true_positive / positives * 100 if positives else None,
        "roc_auc": (
            float(roc_auc_score(labels, probabilities)) if positives and negatives else None
        ),
        "brier_score": statistics.fmean(
            (probability - label) ** 2
            for label, probability in zip(labels, probabilities, strict=True)
        ),
        "log_loss": statistics.fmean(
            -math.log(max(1e-15, probability if label else 1 - probability))
            for label, probability in zip(labels, probabilities, strict=True)
        ),
        "ece_10_equal_width_bins": sum(
            bucket["count"] * abs(bucket["mean_probability_positive"] - bucket["fraction_positive"])
            for bucket in bins
            if bucket["count"]
        )
        / len(labels),
        "confusion_matrix_true_rows_predicted_columns_negative_positive": [
            [true_negative, negatives - true_negative],
            [positives - true_positive, true_positive],
        ],
        "reliability_bins": bins,
        "calibration_fitted_by_this_report": False,
    }


def joint_signal_confidences(
    materiality_probabilities: Sequence[float],
    direction_probabilities_up: Sequence[float],
) -> list[float]:
    """Rank signals by P(material move) times conditional direction confidence."""
    if len(materiality_probabilities) != len(direction_probabilities_up):
        raise ValueError("investor signal probability heads are not aligned")
    _validate_probabilities(materiality_probabilities)
    _validate_probabilities(direction_probabilities_up)
    return [
        materiality * max(direction_up, 1 - direction_up)
        for materiality, direction_up in zip(
            materiality_probabilities, direction_probabilities_up, strict=True
        )
    ]


def select_joint_confidence_threshold(confidences: Sequence[float], coverage_pct: float) -> float:
    """Freeze a validation-only neutral threshold, retaining all boundary ties."""
    _validate_probabilities(confidences)
    if not 0 < coverage_pct <= 100:
        raise ValueError("coverage must be in (0, 100]")
    ordered = sorted(confidences, reverse=True)
    if coverage_pct == 100:
        return 0.0
    count = max(1, math.ceil(len(ordered) * coverage_pct / 100))
    return ordered[count - 1]


def apply_investor_neutral_policy(
    templates: Sequence[SignalPrediction],
    materiality_probabilities: Sequence[float],
    direction_probabilities_up: Sequence[float],
    threshold: float,
) -> list[SignalPrediction]:
    """Map low joint confidence to neutral/abstain and retain one row per input."""
    if (
        len(templates) != len(materiality_probabilities)
        or len(templates) != len(direction_probabilities_up)
        or not 0 <= threshold <= 1
    ):
        raise ValueError("investor neutral policy is invalid or unaligned")
    confidences = joint_signal_confidences(materiality_probabilities, direction_probabilities_up)
    predictions = []
    for template, confidence, probability_up in zip(
        templates, confidences, direction_probabilities_up, strict=True
    ):
        direction = PredictionDirection.ABSTAIN
        if confidence >= threshold:
            direction = (
                PredictionDirection.UP if probability_up >= 0.5 else PredictionDirection.DOWN
            )
        predictions.append(
            template.model_copy(update={"direction": direction, "confidence": confidence})
        )
    return predictions


def materiality_selection_diagnostics(
    examples: Sequence[SignalDatasetExample],
    predictions: Sequence[SignalPrediction],
    *,
    threshold_pct: float = MATERIAL_MOVE_ABS_RETURN_PCT,
) -> dict[str, object]:
    """Assess selected material moves and quiet neutral decisions separately."""
    if len(examples) != len(predictions) or not examples:
        raise ValueError("materiality selection rows must be non-empty and aligned")
    labels = materiality_labels(examples, threshold_pct=threshold_pct)
    selected = []
    neutral = []
    for example, prediction, material in zip(examples, predictions, labels, strict=True):
        if (
            example.id,
            example.event_id,
            example.ticker,
            example.decision_at,
        ) != (
            prediction.example_id,
            prediction.event_id,
            prediction.ticker,
            prediction.decision_at,
        ):
            raise ValueError("materiality selection prediction alignment mismatch")
        (neutral if prediction.direction is PredictionDirection.ABSTAIN else selected).append(
            material
        )
    selected_material = sum(selected)
    total_material = sum(labels)
    quiet_neutral = sum(not value for value in neutral)
    selected_interval = wilson_interval_95(selected_material, len(selected))
    neutral_interval = wilson_interval_95(quiet_neutral, len(neutral))
    return {
        "material_move_definition": f"abs_raw_4h_return_gte_{threshold_pct}_pct",
        "rows": len(examples),
        "material_rows": total_material,
        "selected_rows": len(selected),
        "selection_coverage_pct": len(selected) / len(examples) * 100,
        "selected_material_rows": selected_material,
        "materiality_precision_pct": (
            selected_material / len(selected) * 100 if selected else None
        ),
        "materiality_precision_wilson_95_pct": _percent_interval(selected_interval),
        "materiality_recall_pct": (
            selected_material / total_material * 100 if total_material else None
        ),
        "neutral_rows": len(neutral),
        "quiet_neutral_rows": quiet_neutral,
        "neutral_quiet_rate_pct": quiet_neutral / len(neutral) * 100 if neutral else None,
        "neutral_quiet_rate_wilson_95_pct": _percent_interval(neutral_interval),
        "material_moves_missed_as_neutral": sum(neutral),
    }


def _validate_binary_inputs(labels: Sequence[bool], probabilities: Sequence[float]) -> None:
    if not labels or len(labels) != len(probabilities):
        raise ValueError("binary probabilities require non-empty aligned labels")
    if any(type(label) is not bool for label in labels):
        raise ValueError("binary labels must be booleans")
    _validate_probabilities(probabilities)


def _validate_probabilities(probabilities: Sequence[float]) -> None:
    if not probabilities or any(
        not math.isfinite(value) or not 0 <= value <= 1 for value in probabilities
    ):
        raise ValueError("probabilities must be finite values in [0, 1]")


def _logit(probability: float) -> float:
    bounded = min(max(probability, 1e-12), 1 - 1e-12)
    return math.log(bounded / (1 - bounded))


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1 / (1 + math.exp(-value))
    exponential = math.exp(value)
    return exponential / (1 + exponential)


def _reliability_bins(
    labels: Sequence[bool], probabilities: Sequence[float]
) -> list[dict[str, object]]:
    buckets: list[list[tuple[bool, float]]] = [[] for _ in range(10)]
    for label, probability in zip(labels, probabilities, strict=True):
        buckets[min(9, int(probability * 10))].append((label, probability))
    return [
        {
            "lower": index / 10,
            "upper": (index + 1) / 10,
            "count": len(rows),
            "mean_probability_positive": (
                statistics.fmean(probability for _, probability in rows) if rows else None
            ),
            "fraction_positive": (statistics.fmean(label for label, _ in rows) if rows else None),
        }
        for index, rows in enumerate(buckets)
    ]


def _percent_interval(
    interval: tuple[float | None, float | None],
) -> list[float] | None:
    lower, upper = interval
    return [lower * 100, upper * 100] if lower is not None and upper is not None else None
