"""Paired directional diagnostics against an existing formula on common rows."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from eventedge_research.signal_benchmark import wilson_interval_95
from eventedge_research.signal_extended_metrics import probability_diagnostics
from eventedge_research.signal_paired_diagnostics import (
    paired_auc_diagnostics,
    paired_hit_diagnostics,
)

MAXIMUM_COMPARISON_ROWS = 20_000


@dataclass(frozen=True)
class FormulaComparisonRow:
    """One frozen model/formula comparison with aligned market outcomes."""

    example_id: str
    cluster_id: str
    target_return_pct: float
    raw_return_pct: float
    abnormal_return_pct: float | None
    model_probability_up: float
    formula_probability_up: float
    formula_score_probability_up: float


def directional_system_metrics(
    rows: Sequence[FormulaComparisonRow],
    probabilities_up: Sequence[float],
    *,
    round_trip_cost_bps: float = 10,
) -> dict[str, object]:
    """Evaluate one forced-direction system without introducing a new policy."""
    _validate_rows(rows, probabilities_up, round_trip_cost_bps)
    target = [row.target_return_pct for row in rows]
    multipliers = [1 if probability >= 0.5 else -1 for probability in probabilities_up]
    target_signed = _signed([row.target_return_pct for row in rows], multipliers)
    raw_signed = _signed([row.raw_return_pct for row in rows], multipliers)
    abnormal_pairs = [
        (row.abnormal_return_pct, multiplier)
        for row, multiplier in zip(rows, multipliers, strict=True)
        if row.abnormal_return_pct is not None
    ]
    abnormal_signed = [float(value) * multiplier for value, multiplier in abnormal_pairs]
    hits = sum(value > 0 for value in target_signed)
    lower, upper = wilson_interval_95(hits, len(rows))
    return {
        "rows": len(rows),
        "predicted_up": sum(multiplier == 1 for multiplier in multipliers),
        "predicted_down": sum(multiplier == -1 for multiplier in multipliers),
        "hits": hits,
        "hit_rate_pct": hits / len(rows) * 100,
        "wilson_95_pct": [float(lower) * 100, float(upper) * 100],
        "probability_metrics": probability_diagnostics(target, probabilities_up),
        "gross_returns_pct": {
            "target": _return_summary(target_signed),
            "raw": _return_summary(raw_signed),
            "abnormal": _return_summary(abnormal_signed),
        },
        "net_10bps_returns_pct": {
            "target": _return_summary(_after_cost(target_signed, round_trip_cost_bps)),
            "raw": _return_summary(_after_cost(raw_signed, round_trip_cost_bps)),
            "abnormal": _return_summary(_after_cost(abnormal_signed, round_trip_cost_bps)),
        },
        "round_trip_cost_bps": round_trip_cost_bps,
        "event_returns_are_not_a_capital_constrained_portfolio": True,
    }


def compare_model_with_formula(
    rows: Sequence[FormulaComparisonRow],
    *,
    round_trip_cost_bps: float = 10,
    bootstrap_repetitions: int = 5000,
    seed: int = 20260911,
) -> dict[str, object]:
    """Compare frozen model and formula outputs on exactly the same rows."""
    model_probabilities = [row.model_probability_up for row in rows]
    formula_probabilities = [row.formula_probability_up for row in rows]
    formula_score_probabilities = [row.formula_score_probability_up for row in rows]
    _validate_rows(rows, model_probabilities, round_trip_cost_bps)
    _validate_probabilities(formula_probabilities)
    _validate_probabilities(formula_score_probabilities)
    target = [row.target_return_pct for row in rows]
    model_hits = [
        (probability >= 0.5 and value > 0) or (probability < 0.5 and value < 0)
        for probability, value in zip(model_probabilities, target, strict=True)
    ]
    formula_hits = [
        (probability >= 0.5 and value > 0) or (probability < 0.5 and value < 0)
        for probability, value in zip(formula_probabilities, target, strict=True)
    ]
    clusters = [row.cluster_id for row in rows]
    auc_rows = [
        (value > 0, model, formula, score, cluster)
        for value, model, formula, score, cluster in zip(
            target,
            model_probabilities,
            formula_probabilities,
            formula_score_probabilities,
            clusters,
            strict=True,
        )
        if value != 0
    ]
    if not auc_rows:
        raise ValueError("paired AUC requires at least one non-flat target")
    auc_labels, auc_model, auc_formula, auc_score, auc_clusters = zip(
        *auc_rows, strict=True
    )
    return {
        "rows": len(rows),
        "model": directional_system_metrics(
            rows,
            model_probabilities,
            round_trip_cost_bps=round_trip_cost_bps,
        ),
        "formula": directional_system_metrics(
            rows,
            formula_probabilities,
            round_trip_cost_bps=round_trip_cost_bps,
        ),
        "formula_score_linear_mapping_not_fitted": probability_diagnostics(
            target, formula_score_probabilities
        ),
        "paired_hit_rate": paired_hit_diagnostics(
            model_hits,
            formula_hits,
            clusters,
            repetitions=bootstrap_repetitions,
            seed=seed,
        ),
        "paired_auc_vs_formula_confidence": paired_auc_diagnostics(
            auc_labels,
            auc_model,
            auc_formula,
            auc_clusters,
            repetitions=bootstrap_repetitions,
            seed=seed,
        ),
        "paired_auc_vs_formula_score": paired_auc_diagnostics(
            auc_labels,
            auc_model,
            auc_score,
            auc_clusters,
            repetitions=bootstrap_repetitions,
            seed=seed,
        ),
        "same_rows_and_forced_direction_for_both_systems": True,
    }


def _validate_rows(
    rows: Sequence[FormulaComparisonRow],
    probabilities_up: Sequence[float],
    cost_bps: float,
) -> None:
    if not 1 <= len(rows) <= MAXIMUM_COMPARISON_ROWS or len(rows) != len(probabilities_up):
        raise ValueError("formula comparison needs aligned rows within the bounded limit")
    if len({row.example_id for row in rows}) != len(rows):
        raise ValueError("formula comparison example ids must be unique")
    if any(not row.example_id or not row.cluster_id for row in rows):
        raise ValueError("formula comparison identities and clusters must be nonempty")
    values = [
        value
        for row in rows
        for value in (row.target_return_pct, row.raw_return_pct, row.abnormal_return_pct)
        if value is not None
    ]
    if any(not math.isfinite(value) for value in values):
        raise ValueError("formula comparison returns must be finite")
    if not math.isfinite(cost_bps) or cost_bps < 0:
        raise ValueError("formula comparison cost must be finite and nonnegative")
    _validate_probabilities(probabilities_up)


def _validate_probabilities(values: Sequence[float]) -> None:
    if any(not math.isfinite(value) or not 0 <= value <= 1 for value in values):
        raise ValueError("formula comparison probabilities must be finite values in [0, 1]")


def _signed(values: Sequence[float], multipliers: Sequence[int]) -> list[float]:
    return [
        value * multiplier
        for value, multiplier in zip(values, multipliers, strict=True)
    ]


def _after_cost(values: Sequence[float], cost_bps: float) -> list[float]:
    return [value - cost_bps / 100 for value in values]


def _return_summary(values: Sequence[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "rows": 0,
            "mean": None,
            "median": None,
            "arithmetic_sum": None,
            "event_sharpe_not_annualized": None,
        }
    deviation = statistics.stdev(values) if len(values) >= 2 else 0
    return {
        "rows": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "arithmetic_sum": sum(values),
        "event_sharpe_not_annualized": (
            statistics.fmean(values) / deviation if deviation > 0 else None
        ),
    }
