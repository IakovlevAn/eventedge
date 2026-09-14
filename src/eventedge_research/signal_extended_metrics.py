"""Explicit probability, selective coverage and return diagnostics for local evals."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence

from eventedge_research.signal_benchmark import (
    PredictionDirection,
    SignalPrediction,
    wilson_interval_95,
)
from eventedge_research.signal_dataset import SignalDatasetExample


def probability_diagnostics(
    returns_pct: Sequence[float], probabilities_up: Sequence[float]
) -> dict[str, object]:
    """Assess P(up) on non-flat outcomes; flat misses remain explicit separately.

    No calibration is fitted here. Reliability bins and ECE describe the given
    probabilities, while Brier and log loss also depend on discrimination.
    """
    _validate_probabilities(returns_pct, probabilities_up)
    pairs = [
        (value > 0, probability)
        for value, probability in zip(returns_pct, probabilities_up, strict=True)
        if value != 0
    ]
    forced_hits = sum(
        (probability >= 0.5 and value > 0) or (probability < 0.5 and value < 0)
        for value, probability in zip(returns_pct, probabilities_up, strict=True)
    )
    base = {
        "rows": len(returns_pct),
        "nonflat_rows": len(pairs),
        "flat_rows_excluded_from_binary_metrics": len(returns_pct) - len(pairs),
        "forced_directional_accuracy_with_flat_misses_pct": forced_hits / len(returns_pct) * 100,
        "threshold": 0.5,
    }
    if not pairs:
        return {**base, "binary_metrics": None, "reason": "all_outcomes_flat"}
    labels, probabilities = zip(*pairs, strict=True)
    down_count, up_count = labels.count(False), labels.count(True)
    true_up = sum(label and probability >= 0.5 for label, probability in pairs)
    true_down = sum(not label and probability < 0.5 for label, probability in pairs)
    from sklearn.metrics import roc_auc_score

    bins = _reliability_bins(pairs)
    return {
        **base,
        "binary_metrics": {
            "directional_accuracy_pct": (true_up + true_down) / len(pairs) * 100,
            "balanced_accuracy_pct": (
                (true_up / up_count + true_down / down_count) * 50
                if up_count and down_count
                else None
            ),
            "roc_auc": float(roc_auc_score(labels, probabilities))
            if up_count and down_count
            else None,
            "class_counts": {"up": up_count, "down": down_count},
            "confusion_matrix_true_rows_predicted_columns_down_up": [
                [true_down, down_count - true_down],
                [up_count - true_up, true_up],
            ],
            "brier_score": statistics.fmean(
                (probability - label) ** 2 for label, probability in pairs
            ),
            "log_loss": statistics.fmean(
                -math.log(max(1e-15, probability if label else 1 - probability))
                for label, probability in pairs
            ),
            "ece_10_equal_width_bins": sum(
                bucket["count"] * abs(bucket["mean_probability_up"] - bucket["fraction_up"])
                for bucket in bins
                if bucket["count"]
            )
            / len(pairs),
            "reliability_bins": bins,
            "calibration_fitted_by_this_report": False,
        },
    }


def select_coverage_threshold(probabilities_up: Sequence[float], coverage_pct: float) -> float:
    """Choose confidence rank using validation inputs only, including all ties."""
    if not probabilities_up or not 0 < coverage_pct <= 100:
        raise ValueError("coverage selection needs probabilities and coverage in (0, 100]")
    _validate_probabilities([0.0] * len(probabilities_up), probabilities_up)
    confidences = sorted((max(value, 1 - value) for value in probabilities_up), reverse=True)
    count = max(1, math.ceil(len(confidences) * coverage_pct / 100))
    return confidences[count - 1] if coverage_pct < 100 else 0.5


def coverage_predictions(
    templates: Sequence[SignalPrediction], probabilities_up: Sequence[float], threshold: float
) -> list[SignalPrediction]:
    """Apply a fixed threshold; test rows never determine how many are selected."""
    if len(templates) != len(probabilities_up) or not 0.5 <= threshold <= 1:
        raise ValueError("coverage policy is invalid or unaligned")
    _validate_probabilities([0.0] * len(templates), probabilities_up)
    predictions = []
    for template, probability in zip(templates, probabilities_up, strict=True):
        confidence = max(probability, 1 - probability)
        direction = PredictionDirection.ABSTAIN
        if confidence >= threshold:
            direction = PredictionDirection.UP if probability >= 0.5 else PredictionDirection.DOWN
        predictions.append(
            template.model_copy(update={"direction": direction, "confidence": confidence})
        )
    return predictions


def selective_return_diagnostics(
    examples: Sequence[SignalDatasetExample],
    predictions: Sequence[SignalPrediction],
    *,
    round_trip_costs_bps: Sequence[float] = (0, 10, 25, 50),
) -> dict[str, object]:
    """Report unweighted event returns, not a compounded portfolio equity curve."""
    if len(examples) != len(predictions) or not examples:
        raise ValueError("return diagnostic rows must be non-empty and aligned")
    if any(not math.isfinite(cost) or cost < 0 for cost in round_trip_costs_bps):
        raise ValueError("round-trip costs must be finite and non-negative")
    grouped = {"up": [], "down": []}
    for example, prediction in zip(examples, predictions, strict=True):
        if (example.id, example.event_id, example.ticker, example.decision_at) != (
            prediction.example_id,
            prediction.event_id,
            prediction.ticker,
            prediction.decision_at,
        ):
            raise ValueError("return diagnostic prediction alignment mismatch")
        if prediction.direction is not PredictionDirection.ABSTAIN:
            grouped[prediction.direction.value].append(example.outcome_4h.return_pct)
    signed = [*grouped["up"], *(-value for value in grouped["down"])]
    hits = sum(value > 0 for value in signed)
    lower, upper = wilson_interval_95(hits, len(signed))
    return {
        "eligible_rows": len(examples),
        "predictions": len(signed),
        "hits": hits,
        "hit_rate_pct": hits / len(signed) * 100 if signed else None,
        "selection_coverage_pct": len(signed) / len(examples) * 100,
        "wilson_95_pct": [lower * 100, upper * 100] if signed else None,
        "gross_signed_return_pct": _distribution(signed),
        "raw_return_conditional_on_prediction_pct": {
            name: _distribution(values) for name, values in grouped.items()
        },
        "net_signed_return_scenarios_pct": {
            str(cost): _distribution([value - cost / 100 for value in signed])
            for cost in round_trip_costs_bps
        },
        "average_break_even_round_trip_bps": statistics.fmean(signed) * 100 if signed else None,
        "costs_are_assumptions_not_observed_execution": True,
        "returns_are_not_a_portfolio_pnl": True,
    }


def _distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "n": 0,
            "mean": None,
            "median": None,
            "p05": None,
            "p95": None,
            "minimum": None,
            "maximum": None,
        }
    import numpy as np

    return {
        "n": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "p05": float(np.quantile(values, 0.05)),
        "p95": float(np.quantile(values, 0.95)),
        "minimum": min(values),
        "maximum": max(values),
    }


def _reliability_bins(pairs: Sequence[tuple[bool, float]]) -> list[dict[str, object]]:
    buckets: list[list[tuple[bool, float]]] = [[] for _ in range(10)]
    for label, probability in pairs:
        buckets[min(9, int(probability * 10))].append((label, probability))
    return [
        {
            "lower": index / 10,
            "upper": (index + 1) / 10,
            "count": len(rows),
            "mean_probability_up": statistics.fmean(probability for _, probability in rows)
            if rows
            else None,
            "fraction_up": statistics.fmean(label for label, _ in rows) if rows else None,
        }
        for index, rows in enumerate(buckets)
    ]


def _validate_probabilities(returns: Sequence[float], probabilities: Sequence[float]) -> None:
    if not returns or len(returns) != len(probabilities):
        raise ValueError("probability metrics require non-empty aligned rows")
    if any(not math.isfinite(value) for value in returns) or any(
        not math.isfinite(value) or not 0 <= value <= 1 for value in probabilities
    ):
        raise ValueError("returns must be finite and probabilities must be in [0, 1]")
