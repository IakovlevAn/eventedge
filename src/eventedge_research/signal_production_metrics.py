"""Metrics for the current production signal epoch exported from YDB."""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence

from eventedge.analysis import CURRENT_NEWS_MODEL_VERSION, CURRENT_SIGNAL_CONFIG_VERSION
from eventedge.evals import (
    EVALUATION_METHODOLOGY_VERSION,
    eval_breakdowns,
    eval_relationships,
    eval_summary,
)
from eventedge_research.signal_benchmark import wilson_interval_95
from eventedge_research.signal_extended_metrics import probability_diagnostics
from eventedge_research.signal_investor_policy import binary_probability_diagnostics

_DIRECTIONS = frozenset({"up", "down"})
_COSTS_BPS = (0, 10, 25, 50)


def build_current_production_metrics(
    signals: Sequence[Mapping[str, object]],
    epochs: Sequence[Mapping[str, object]],
    news_by_id: Mapping[str, Mapping[str, object]],
    feature_sets: Sequence[Mapping[str, object]],
    *,
    config_version: int | None = None,
) -> dict[str, object]:
    """Audit a stored production config without refitting or repricing."""
    epoch = current_production_epoch(epochs, config_version=config_version)
    selected_config = _integer(epoch.get("config_version"), "epoch config_version")
    current_signals = _current_signal_rows(signals, config_version=selected_config)
    outcomes = _epoch_outcomes(epoch)
    identity = _identity_audit(
        current_signals,
        outcomes,
        config_version=selected_config,
    )
    source_counts = Counter(
        str(news_by_id[str(signal["news_id"])]["source_id"]) for signal in current_signals
    )
    extractor_counts = _used_extractor_counts(current_signals, feature_sets)
    cohorts = {
        name: _cohort_metrics(_cohort(outcomes, name)) for name in ("live", "retrospective", "all")
    }
    return {
        "schema_version": "ydb-production-signal-metrics-1.0",
        "status": "read_only_extract_no_refit_no_repricing",
        "selection": {
            "model_version": CURRENT_NEWS_MODEL_VERSION,
            "config_version": selected_config,
            "basis": (
                "requested_ready_config_for_current_model"
                if config_version is not None
                else "highest_ready_config_for_current_model_matching_v1_evals"
            ),
            "repository_default_config_version": CURRENT_SIGNAL_CONFIG_VERSION,
            "repository_default_differs_from_ydb": (
                CURRENT_SIGNAL_CONFIG_VERSION != selected_config
            ),
            "epoch_id": epoch["epoch_id"],
            "epoch_evaluated_at": epoch["evaluated_at"],
            "evaluation_methodology": EVALUATION_METHODOLOGY_VERSION,
            **identity,
        },
        "inventory": {
            "signals": len(current_signals),
            "news_items": len({str(row["news_id"]) for row in current_signals}),
            "minimum_signal_as_of": min(str(row["as_of"]) for row in current_signals),
            "maximum_signal_as_of": max(str(row["as_of"]) for row in current_signals),
            "directions": dict(
                sorted(Counter(str(row["direction"]) for row in current_signals).items())
            ),
            "sources": dict(sorted(source_counts.items())),
            "signal_feature_extractors": dict(sorted(extractor_counts.items())),
        },
        "production_native": {
            "summary": eval_summary(outcomes),
            "breakdowns": eval_breakdowns(outcomes),
            "relationships": eval_relationships(outcomes),
        },
        "strict_4h": cohorts,
        "limitations": [
            "Only stored signal rows are represented; end-to-end coverage over all "
            "eligible news is unavailable.",
            "Live and retrospective cohorts are not pooled for the primary product metric.",
            "Stored confidence is a heuristic, not a calibrated probability; "
            "calibration metrics are diagnostics.",
            "Score-to-probability is a declared linear diagnostic mapping and was not fitted.",
            "Returns are stored raw MOEX candle returns without corporate-action adjustment.",
            "Cost scenarios, event-return sums and event Sharpe are not a "
            "capital-constrained portfolio backtest.",
        ],
    }


def current_production_epoch(
    epochs: Sequence[Mapping[str, object]],
    *,
    config_version: int | None = None,
) -> Mapping[str, object]:
    """Select the newest ready epoch for the current model and optional config."""
    candidates = [
        row
        for row in epochs
        if row.get("model_version") == CURRENT_NEWS_MODEL_VERSION
        and row.get("observations_truncated") is False
        and _epoch_methodology(row) == EVALUATION_METHODOLOGY_VERSION
        and (config_version is None or row.get("config_version") == config_version)
    ]
    if not candidates:
        requested = "" if config_version is None else f" for config {config_version}"
        raise ValueError(f"evaluation export has no ready current-method epoch{requested}")
    return max(
        candidates,
        key=lambda row: (
            _integer(row.get("config_version"), "epoch config_version"),
            str(row.get("evaluated_at", "")),
            str(row.get("epoch_id", "")),
        ),
    )


def _current_signal_rows(
    signals: Sequence[Mapping[str, object]], *, config_version: int
) -> list[Mapping[str, object]]:
    model_rows = [row for row in signals if row.get("model_version") == CURRENT_NEWS_MODEL_VERSION]
    if not model_rows:
        raise ValueError("signal export has no rows for the current model version")
    selected = [row for row in model_rows if row.get("config_version") == config_version]
    if not selected:
        raise ValueError("current signal config has no rows")
    return selected


def _epoch_methodology(epoch: Mapping[str, object]) -> str:
    outcomes = epoch.get("outcomes")
    if not isinstance(outcomes, list):
        return "invalid"
    versions = {
        str(row.get("evaluation_methodology"))
        for row in outcomes
        if isinstance(row, dict) and row.get("evaluation_methodology")
    }
    return versions.pop() if len(versions) == 1 else "legacy-or-mixed"


def _epoch_outcomes(epoch: Mapping[str, object]) -> list[dict[str, object]]:
    outcomes = epoch.get("outcomes")
    if not isinstance(outcomes, list) or any(not isinstance(row, dict) for row in outcomes):
        raise ValueError("evaluation epoch outcomes must be JSON objects")
    return [dict(row) for row in outcomes]


def _identity_audit(
    signals: Sequence[Mapping[str, object]],
    outcomes: Sequence[Mapping[str, object]],
    *,
    config_version: int,
) -> dict[str, object]:
    if any(
        row.get("model_version") != CURRENT_NEWS_MODEL_VERSION
        or row.get("config_version") != config_version
        for row in (*signals, *outcomes)
    ):
        raise ValueError("current signal and outcome identities disagree")
    signal_ids = [str(row.get("signal_id", "")) for row in signals]
    outcome_ids = [str(row.get("signal_id", "")) for row in outcomes]
    if "" in signal_ids or len(signal_ids) != len(set(signal_ids)):
        raise ValueError("current signal rows have missing or duplicate IDs")
    if "" in outcome_ids or len(outcome_ids) != len(set(outcome_ids)):
        raise ValueError("current outcome rows have missing or duplicate IDs")
    unexpected = sorted(set(outcome_ids) - set(signal_ids))
    if unexpected:
        raise ValueError("current epoch contains outcomes absent from the signal export")
    missing = sorted(set(signal_ids) - set(outcome_ids))
    return {
        "signal_rows": len(signal_ids),
        "outcome_rows": len(outcome_ids),
        "signals_missing_from_epoch": len(missing),
        "signals_missing_from_epoch_ids": missing,
        "outcomes_match_exported_signals": not missing,
    }


def _used_extractor_counts(
    signals: Sequence[Mapping[str, object]],
    feature_sets: Sequence[Mapping[str, object]],
) -> Counter[str]:
    by_identity = {
        (str(row.get("news_id", "")), str(row.get("created_at", ""))): row for row in feature_sets
    }
    if len(by_identity) != len(feature_sets):
        raise ValueError("feature export has duplicate news/created identities")
    result: Counter[str] = Counter()
    for signal in signals:
        identity = (str(signal.get("news_id", "")), str(signal.get("created_at", "")))
        feature_set = by_identity.get(identity)
        if feature_set is None:
            raise ValueError("signal-producing feature set is absent from the export")
        result[str(feature_set.get("extractor_version", "unknown"))] += 1
    return result


def _cohort(outcomes: Sequence[dict[str, object]], name: str) -> list[dict[str, object]]:
    if name == "all":
        return list(outcomes)
    return [row for row in outcomes if _cohort_name(row) == name]


def _cohort_name(outcome: Mapping[str, object]) -> str:
    eligibility = outcome.get("eligibility")
    if not isinstance(eligibility, dict):
        return "retrospective"
    value = eligibility.get("cohort")
    return str(value) if value in {"live", "retrospective"} else "retrospective"


def _cohort_metrics(outcomes: Sequence[dict[str, object]]) -> dict[str, object]:
    if any(row.get("direction") not in {*_DIRECTIONS, "neutral"} for row in outcomes):
        raise ValueError("outcome has an unsupported direction")
    directional = [row for row in outcomes if row.get("direction") in _DIRECTIONS]
    observed = [row for row in directional if _horizon_return(row, "4h") is not None]
    raw_returns = [_required_horizon_return(row, "4h") for row in observed]
    signed_returns = [
        value if row["direction"] == "up" else -value
        for row, value in zip(observed, raw_returns, strict=True)
    ]
    hits = [value > 0 for value in signed_returns]
    lower, upper = wilson_interval_95(sum(hits), len(hits)) if hits else (None, None)
    return {
        "signals_total": len(outcomes),
        "directional_signals": len(directional),
        "neutral_signals": len(outcomes) - len(directional),
        "directional_share_of_stored_signals_pct": _percentage(len(directional), len(outcomes)),
        "observed_directional_4h": len(observed),
        "observed_share_of_directional_pct": _percentage(len(observed), len(directional)),
        "hit_rate_pct": _percentage(sum(hits), len(hits)),
        "wilson_95_pct": (
            [lower * 100, upper * 100] if lower is not None and upper is not None else None
        ),
        "signed_return_pct": _distribution(signed_returns),
        "raw_return_conditional_on_prediction_pct": {
            direction: _distribution(
                [
                    value
                    for row, value in zip(observed, raw_returns, strict=True)
                    if row["direction"] == direction
                ]
            )
            for direction in ("up", "down")
        },
        "event_return_cost_scenarios": _cost_scenarios(signed_returns),
        "matched_direction_controls": {
            "always_up": _payoff(raw_returns),
            "always_down": _payoff([-value for value in raw_returns]),
        },
        "direction_and_confidence_as_probability_up": _direction_probability_metrics(
            observed, raw_returns
        ),
        "confidence_as_probability_of_correct_direction": _confidence_metrics(observed, hits),
        "score_ranking_on_all_observed_signals": _score_metrics(outcomes),
    }


def _direction_probability_metrics(
    outcomes: Sequence[dict[str, object]], returns: Sequence[float]
) -> dict[str, object] | None:
    if not outcomes:
        return None
    probabilities = [
        _confidence(row) if row["direction"] == "up" else 1 - _confidence(row) for row in outcomes
    ]
    return probability_diagnostics(returns, probabilities)


def _confidence_metrics(
    outcomes: Sequence[dict[str, object]], hits: Sequence[bool]
) -> dict[str, object] | None:
    if not outcomes:
        return None
    return binary_probability_diagnostics(
        hits,
        [_confidence(row) for row in outcomes],
        positive_name="hit",
        negative_name="miss",
    )


def _score_metrics(outcomes: Sequence[dict[str, object]]) -> dict[str, object] | None:
    observed = [row for row in outcomes if _horizon_return(row, "4h") is not None]
    if not observed:
        return None
    returns = [_required_horizon_return(row, "4h") for row in observed]
    probabilities = [(_score(row) + 100) / 200 for row in observed]
    return {
        "mapping": "p_up=(score+100)/200_not_fitted",
        **probability_diagnostics(returns, probabilities),
    }


def _cost_scenarios(signed_returns: Sequence[float]) -> dict[str, object]:
    result = {}
    for cost in _COSTS_BPS:
        values = [value - cost / 100 for value in signed_returns]
        result[str(cost)] = {
            "round_trip_cost_bps": cost,
            "sum_event_returns_pct_not_portfolio_pnl": sum(values),
            "mean_event_return_pct": statistics.fmean(values) if values else None,
            "event_sharpe_not_annualized": _event_sharpe(values),
        }
    return result


def _payoff(values: Sequence[float]) -> dict[str, object]:
    return {
        "rows": len(values),
        "hits": sum(value > 0 for value in values),
        "hit_rate_pct": _percentage(sum(value > 0 for value in values), len(values)),
        "mean_return_pct": statistics.fmean(values) if values else None,
        "median_return_pct": statistics.median(values) if values else None,
    }


def _distribution(values: Sequence[float]) -> dict[str, object]:
    return {
        "n": len(values),
        "mean": statistics.fmean(values) if values else None,
        "median": statistics.median(values) if values else None,
        "minimum": min(values) if values else None,
        "maximum": max(values) if values else None,
    }


def _event_sharpe(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    deviation = statistics.stdev(values)
    return statistics.fmean(values) / deviation if deviation else None


def _horizon_return(outcome: Mapping[str, object], horizon: str) -> float | None:
    returns = outcome.get("returns")
    if not isinstance(returns, dict):
        return None
    value = returns.get(horizon)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(float(value)) else None


def _required_horizon_return(outcome: Mapping[str, object], horizon: str) -> float:
    value = _horizon_return(outcome, horizon)
    if value is None:
        raise ValueError(f"outcome has no finite {horizon} return")
    return value


def _confidence(outcome: Mapping[str, object]) -> float:
    value = outcome.get("confidence")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("outcome confidence is not numeric")
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise ValueError("outcome confidence is outside [0, 1]")
    return number


def _score(outcome: Mapping[str, object]) -> float:
    value = outcome.get("score")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("outcome score is not numeric")
    number = float(value)
    if not math.isfinite(number) or not -100 <= number <= 100:
        raise ValueError("outcome score is outside [-100, 100]")
    return number


def _integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be an integer")
    return value


def _percentage(numerator: int, denominator: int) -> float | None:
    return numerator / denominator * 100 if denominator else None
