"""Known-answer checks for the formula-only directional comparison."""

from __future__ import annotations

import pytest

from eventedge_research.signal_formula_comparison import (
    FormulaComparisonRow,
    compare_model_with_formula,
    directional_system_metrics,
)


def _row(
    identity: str,
    target: float,
    model_probability: float,
    formula_probability: float,
) -> FormulaComparisonRow:
    return FormulaComparisonRow(
        example_id=identity,
        cluster_id=f"day-{int(identity) // 2}",
        target_return_pct=target,
        raw_return_pct=target,
        abnormal_return_pct=target - 0.1,
        model_probability_up=model_probability,
        formula_probability_up=formula_probability,
        formula_score_probability_up=formula_probability,
    )


def test_compare_model_with_formula_uses_exact_common_rows() -> None:
    rows = [
        _row("0", 1.0, 0.9, 0.9),
        _row("1", -2.0, 0.8, 0.2),
        _row("2", -1.0, 0.2, 0.2),
        _row("3", 2.0, 0.1, 0.8),
    ]
    report = compare_model_with_formula(rows, bootstrap_repetitions=100, seed=7)

    assert report["rows"] == 4
    assert report["model"]["hits"] == 2
    assert report["formula"]["hits"] == 4
    assert report["paired_hit_rate"]["candidate_only_correct"] == 0
    assert report["paired_hit_rate"]["baseline_only_correct"] == 2
    assert report["model"]["net_10bps_returns_pct"]["raw"]["mean"] == pytest.approx(-0.6)
    assert report["formula"]["gross_returns_pct"]["raw"]["mean"] == 1.5


def test_directional_metrics_report_probability_and_return_metrics() -> None:
    rows = [_row("0", 1.0, 0.9, 0.9), _row("1", -1.0, 0.2, 0.2)]
    metrics = directional_system_metrics(rows, [0.9, 0.2])

    assert metrics["hit_rate_pct"] == 100
    assert metrics["probability_metrics"]["binary_metrics"]["roc_auc"] == 1
    assert metrics["gross_returns_pct"]["target"]["arithmetic_sum"] == 2
    assert metrics["net_10bps_returns_pct"]["target"]["mean"] == 0.9


def test_flat_target_is_a_miss_for_both_paired_systems() -> None:
    rows = [
        _row("0", 1.0, 0.9, 0.9),
        _row("1", -1.0, 0.2, 0.2),
        _row("2", 0.0, 0.2, 0.8),
    ]
    report = compare_model_with_formula(rows, bootstrap_repetitions=100, seed=7)

    assert report["model"]["hits"] == report["formula"]["hits"] == 2
    assert report["paired_hit_rate"]["candidate_hits"] == 2
    assert report["paired_hit_rate"]["baseline_hits"] == 2
    assert report["paired_auc_vs_formula_confidence"]["rows"] == 2


def test_all_flat_targets_are_rejected_for_paired_auc() -> None:
    rows = [_row("0", 0.0, 0.9, 0.1)]

    with pytest.raises(ValueError, match="at least one non-flat target"):
        compare_model_with_formula(rows, bootstrap_repetitions=100, seed=7)


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [_row("0", 1.0, 1.1, 0.9)],
        [_row("0", 1.0, 0.9, 0.9), _row("0", -1.0, 0.1, 0.1)],
    ],
)
def test_invalid_formula_comparison_is_rejected(rows) -> None:
    with pytest.raises(ValueError):
        compare_model_with_formula(rows, bootstrap_repetitions=100)
