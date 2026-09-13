"""Known-answer diagnostics for probabilities, costs and validation-only coverage."""

from __future__ import annotations

import math

import pytest
from signal_fixtures import signal_example

from eventedge_research.signal_baselines import SignalBaseline, build_baseline_predictions
from eventedge_research.signal_dataset import SignalDatasetExample
from eventedge_research.signal_extended_metrics import (
    coverage_predictions,
    probability_diagnostics,
    select_coverage_threshold,
    selective_return_diagnostics,
)


def test_probability_metrics_known_answers_and_flat_policy() -> None:
    report = probability_diagnostics([-1, 1, 1, 0], [0.1, 0.8, 0.4, 0.99])
    assert report["flat_rows_excluded_from_binary_metrics"] == 1
    assert report["forced_directional_accuracy_with_flat_misses_pct"] == 50
    metrics = report["binary_metrics"]
    assert metrics["roc_auc"] == 1
    assert metrics["directional_accuracy_pct"] == pytest.approx(200 / 3)
    assert metrics["balanced_accuracy_pct"] == 75
    assert metrics["brier_score"] == pytest.approx((0.01 + 0.04 + 0.36) / 3)
    assert metrics["log_loss"] == pytest.approx(-math.log(0.9 * 0.8 * 0.4) / 3)
    assert metrics["confusion_matrix_true_rows_predicted_columns_down_up"] == [[1, 0], [1, 1]]
    assert sum(row["count"] for row in metrics["reliability_bins"]) == 3
    assert metrics["ece_10_equal_width_bins"] == pytest.approx(0.3)


def test_constant_coin_probability_brier_and_auc() -> None:
    metrics = probability_diagnostics([-1, 1, 1], [0.5, 0.5, 0.5])["binary_metrics"]
    assert metrics["roc_auc"] == 0.5
    assert metrics["brier_score"] == 0.25
    assert metrics["log_loss"] == pytest.approx(math.log(2))


def test_undefined_binary_auc_and_all_flat_are_explicit() -> None:
    metrics = probability_diagnostics([1, 2], [0, 1])["binary_metrics"]
    assert metrics["roc_auc"] is None
    assert metrics["balanced_accuracy_pct"] is None
    assert math.isfinite(metrics["log_loss"])
    assert probability_diagnostics([0], [0.5])["binary_metrics"] is None


@pytest.mark.parametrize("probabilities", [[math.nan], [-0.1], [1.1], []])
def test_probability_metrics_reject_invalid_inputs(probabilities: list[float]) -> None:
    with pytest.raises(ValueError):
        probability_diagnostics([1], probabilities)


def test_coverage_threshold_uses_validation_and_keeps_test_ties() -> None:
    threshold = select_coverage_threshold([0.1, 0.8, 0.6, 0.55], 50)
    assert threshold == 0.8
    examples = [SignalDatasetExample.model_validate(signal_example(index)) for index in range(3)]
    templates = build_baseline_predictions(
        SignalBaseline.ALWAYS_UP,
        training_examples=[],
        evaluation_examples=examples,
        partition="test",
        dataset_sha256="a" * 64,
    )
    predictions = coverage_predictions(templates, [0.8, 0.9, 0.5], threshold)
    assert [row.direction.value for row in predictions] == ["up", "up", "abstain"]
    assert select_coverage_threshold([0.5], 100) == 0.5


def test_conditional_returns_costs_and_down_sign() -> None:
    examples = [
        SignalDatasetExample.model_validate(signal_example(index, return_pct=value))
        for index, value in enumerate([1, -2, 3])
    ]
    predictions = build_baseline_predictions(
        SignalBaseline.ALWAYS_UP,
        training_examples=[],
        evaluation_examples=examples,
        partition="test",
        dataset_sha256="a" * 64,
    )
    predictions[1] = predictions[1].model_copy(update={"direction": predictions[1].direction.DOWN})
    predictions[2] = predictions[2].model_copy(
        update={"direction": predictions[2].direction.ABSTAIN}
    )
    report = selective_return_diagnostics(examples, predictions, round_trip_costs_bps=[25])
    assert report["predictions"] == 2
    assert report["hit_rate_pct"] == 100
    assert report["gross_signed_return_pct"]["mean"] == 1.5
    assert report["raw_return_conditional_on_prediction_pct"]["down"]["mean"] == -2
    assert report["net_signed_return_scenarios_pct"]["25"]["mean"] == 1.25
    assert report["average_break_even_round_trip_bps"] == 150
    with pytest.raises(ValueError, match="alignment"):
        selective_return_diagnostics(examples, list(reversed(predictions)))
