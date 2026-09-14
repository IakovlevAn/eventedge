"""Investor policy keeps materiality, direction and neutral evaluation separate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from signal_fixtures import signal_example

from eventedge_research.signal_benchmark import PredictionDirection, SignalPrediction
from eventedge_research.signal_dataset import SignalDatasetExample
from eventedge_research.signal_investor_policy import (
    ValidationLogitOffset,
    apply_investor_neutral_policy,
    binary_probability_diagnostics,
    joint_signal_confidences,
    materiality_selection_diagnostics,
    select_joint_confidence_threshold,
)


def _examples() -> list[SignalDatasetExample]:
    decision = datetime(2026, 1, 1, tzinfo=UTC)
    returns = (1.0, 0.1, -2.0, -0.2)
    return [
        SignalDatasetExample.model_validate(
            signal_example(index, decision_at=decision, return_pct=value)
        )
        for index, value in enumerate(returns)
    ]


def _templates(examples: list[SignalDatasetExample]) -> list[SignalPrediction]:
    return [
        SignalPrediction(
            example_id=row.id,
            event_id=row.event_id,
            ticker=row.ticker,
            decision_at=row.decision_at,
            feature_cutoff_at=row.features.as_of,
            direction=PredictionDirection.ABSTAIN,
            confidence=0.5,
            model_version="investor-fixture",
            config_version=1,
            partition="test",
            dataset_sha256="a" * 64,
            training_labels_through=row.decision_at - timedelta(seconds=1),
        )
        for row in examples
    ]


def test_logit_offset_matches_prevalence_and_preserves_order():
    probabilities = [0.1, 0.2, 0.3, 0.4]
    calibrator = ValidationLogitOffset.fit([False, False, True, True], probabilities)
    calibrated = calibrator.calibrate(probabilities)
    assert calibrated == sorted(calibrated)
    assert sum(calibrated) / len(calibrated) == pytest.approx(0.5)
    with pytest.raises(ValueError, match="both classes"):
        ValidationLogitOffset.fit([True, True], [0.2, 0.8])


def test_binary_probability_metrics_include_ranking_and_calibration():
    metrics = binary_probability_diagnostics(
        [False, False, True, True],
        [0.1, 0.2, 0.8, 0.9],
        positive_name="material",
        negative_name="quiet",
    )
    assert metrics["accuracy_pct"] == 100
    assert metrics["roc_auc"] == 1
    assert metrics["positive_precision_pct"] == 100
    assert metrics["positive_recall_pct"] == 100
    assert metrics["calibration_fitted_by_this_report"] is False


def test_joint_policy_selects_ties_and_maps_low_confidence_to_neutral():
    examples = _examples()
    materiality = [0.9, 0.8, 0.9, 0.8]
    direction_up = [0.9, 0.5, 0.1, 0.5]
    confidences = joint_signal_confidences(materiality, direction_up)
    threshold = select_joint_confidence_threshold(confidences, 50)
    predictions = apply_investor_neutral_policy(
        _templates(examples), materiality, direction_up, threshold
    )
    assert [row.direction for row in predictions] == [
        PredictionDirection.UP,
        PredictionDirection.ABSTAIN,
        PredictionDirection.DOWN,
        PredictionDirection.ABSTAIN,
    ]
    diagnostics = materiality_selection_diagnostics(examples, predictions)
    assert diagnostics["selection_coverage_pct"] == 50
    assert diagnostics["materiality_precision_pct"] == 100
    assert diagnostics["materiality_recall_pct"] == 100
    assert diagnostics["neutral_quiet_rate_pct"] == 100


def test_investor_policy_rejects_unaligned_heads():
    examples = _examples()
    with pytest.raises(ValueError, match="not aligned"):
        joint_signal_confidences([0.5], [0.5, 0.6])
    with pytest.raises(ValueError, match="invalid or unaligned"):
        apply_investor_neutral_policy(_templates(examples), [0.5], [0.5], threshold=0.5)
