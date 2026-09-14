"""Point-in-time evaluation for news-materiality predictions."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from eventedge.materiality import (
    MATERIALITY_BENCHMARK_TICKER,
    TARGET_DEFINITION,
    MaterialityRuntime,
)
from eventedge.storage import MaterialityPredictionRecord, NewsRecord, to_rfc3339

MATERIALITY_EVALUATION_METHODOLOGY = "materiality-outcome-1.0"
MATERIALITY_HORIZON = timedelta(hours=4)
MATERIALITY_THRESHOLD_PCT = 0.5
MAXIMUM_ENTRY_LAG = timedelta(days=10)
MAXIMUM_OBSERVATION_LAG = timedelta(minutes=20)
PRIMARY_COVERAGE_PCT = 50
MAXIMUM_API_OUTCOMES = 100


def evaluate_materiality_outcome(
    prediction: MaterialityPredictionRecord,
    stock_market: Mapping[str, object],
    benchmark_market: Mapping[str, object],
    *,
    now: datetime,
) -> dict[str, object] | None:
    """Return a terminal four-hour outcome, or None while it is pending."""
    _require_aware(now, "now")
    current_time = now.astimezone(UTC)
    stock_rows = _market_rows(stock_market)
    benchmark_rows = _market_rows(benchmark_market)
    entry = next(
        (row for row in stock_rows if row[0] > prediction.decision_at),
        None,
    )
    if entry is None:
        if current_time <= prediction.decision_at + MAXIMUM_ENTRY_LAG:
            return None
        return _unavailable_outcome(prediction, current_time, "entry_unavailable")

    entry_at, entry_price = entry
    if entry_at - prediction.decision_at > MAXIMUM_ENTRY_LAG:
        return _unavailable_outcome(
            prediction,
            current_time,
            "entry_missed_window",
            entry_at=entry_at,
        )
    target_at = entry_at + MATERIALITY_HORIZON
    if current_time < target_at:
        return None

    stock_target = _first_at_or_after(stock_rows, target_at)
    benchmark_entry = _first_at_or_after(benchmark_rows, entry_at)
    benchmark_target = _first_at_or_after(benchmark_rows, target_at)
    observations = (stock_target, benchmark_entry, benchmark_target)
    if any(value is None for value in observations):
        if current_time <= target_at + MAXIMUM_OBSERVATION_LAG:
            return None
        return _unavailable_outcome(
            prediction,
            current_time,
            "outcome_unavailable",
            entry_at=entry_at,
            target_at=target_at,
        )

    assert stock_target is not None
    assert benchmark_entry is not None
    assert benchmark_target is not None
    if any(
        observed_at - expected_at > MAXIMUM_OBSERVATION_LAG
        for (observed_at, _), expected_at in (
            (stock_target, target_at),
            (benchmark_entry, entry_at),
            (benchmark_target, target_at),
        )
    ):
        return _unavailable_outcome(
            prediction,
            current_time,
            "outcome_missed_window",
            entry_at=entry_at,
            target_at=target_at,
        )

    stock_return = (stock_target[1] / entry_price - 1) * 100
    benchmark_return = (benchmark_target[1] / benchmark_entry[1] - 1) * 100
    abnormal_return = stock_return - benchmark_return
    actual_material = abs(abnormal_return) >= MATERIALITY_THRESHOLD_PCT
    predicted_material = _predicted_material(prediction)
    label_available_at = max(stock_target[0], benchmark_target[0])
    return {
        **_outcome_base(prediction, current_time),
        "status": "evaluated",
        "reason": None,
        "entry_at": to_rfc3339(entry_at),
        "target_at": to_rfc3339(target_at),
        "observed_at": to_rfc3339(label_available_at),
        "stock_return_pct": round(stock_return, 6),
        "benchmark_return_pct": round(benchmark_return, 6),
        "abnormal_return_pct": round(abnormal_return, 6),
        "actual_material": actual_material,
        "predicted_material": predicted_material,
        "verdict": predicted_material == actual_material,
    }


def materiality_eval_report(
    news: Sequence[NewsRecord],
    *,
    runtime: MaterialityRuntime,
) -> dict[str, object]:
    """Summarize current-model materiality outcomes for the Evals API."""
    if runtime.model is None:
        return {
            "status": "disabled",
            "mode": runtime.mode.value,
            "model_version": None,
            "target_definition": TARGET_DEFINITION,
            "summary": _empty_summary(),
            "outcomes": [],
        }

    predictions = _current_predictions(news, runtime)
    ready = [prediction for prediction in predictions if prediction.status == "ready"]
    terminal = [prediction for prediction in ready if prediction.outcome_4h is not None]
    evaluated = [
        prediction
        for prediction in terminal
        if prediction.outcome_4h is not None and prediction.outcome_4h.get("status") == "evaluated"
    ]
    confusion = _confusion(evaluated)
    probabilities = [
        float(prediction.calibrated_probability)
        for prediction in evaluated
        if prediction.calibrated_probability is not None
    ]
    labels = [
        bool(prediction.outcome_4h["actual_material"])
        for prediction in evaluated
        if prediction.outcome_4h is not None and prediction.calibrated_probability is not None
    ]
    summary = {
        "scored_news": len({prediction.news_id for prediction in ready}),
        "ready_predictions": len(ready),
        "evaluated": len(evaluated),
        "pending": len(ready) - len(terminal),
        "unavailable": len(terminal) - len(evaluated),
        "coverage_pct": _percentage(len(evaluated), len(ready), zero=0.0),
        "actual_material": confusion["true_positive"] + confusion["false_negative"],
        "predicted_material": confusion["true_positive"] + confusion["false_positive"],
        "actual_material_pct": _percentage(
            confusion["true_positive"] + confusion["false_negative"],
            len(evaluated),
        ),
        "accuracy_pct": _percentage(
            confusion["true_positive"] + confusion["true_negative"],
            len(evaluated),
        ),
        "precision_pct": _percentage(
            confusion["true_positive"],
            confusion["true_positive"] + confusion["false_positive"],
        ),
        "recall_pct": _percentage(
            confusion["true_positive"],
            confusion["true_positive"] + confusion["false_negative"],
        ),
        "specificity_pct": _percentage(
            confusion["true_negative"],
            confusion["true_negative"] + confusion["false_positive"],
        ),
        "majority_baseline_accuracy_pct": _majority_accuracy(labels),
        "roc_auc": _roc_auc(probabilities, labels),
        "brier_score": _brier_score(probabilities, labels),
        "confusion": confusion,
    }
    if summary["accuracy_pct"] is not None:
        baseline = summary["majority_baseline_accuracy_pct"]
        summary["accuracy_lift_pct_points"] = (
            round(float(summary["accuracy_pct"]) - float(baseline), 1)
            if baseline is not None
            else None
        )
    else:
        summary["accuracy_lift_pct_points"] = None

    news_by_id = {item.id: item for item in news}
    outcomes = [
        _api_outcome(prediction, news_by_id.get(prediction.news_id))
        for prediction in sorted(
            terminal,
            key=lambda item: (item.decision_at, item.id),
            reverse=True,
        )[:MAXIMUM_API_OUTCOMES]
    ]
    return {
        "status": "ready" if evaluated else "pending",
        "mode": runtime.mode.value,
        "model_version": runtime.model.model_version,
        "target_definition": TARGET_DEFINITION,
        "benchmark_ticker": MATERIALITY_BENCHMARK_TICKER,
        "decision_rule": "calibrated_validation_coverage_50",
        "decision_threshold_probability": runtime.model.coverage_thresholds["calibrated"][
            str(PRIMARY_COVERAGE_PCT)
        ],
        "summary": summary,
        "outcomes": outcomes,
    }


def _outcome_base(
    prediction: MaterialityPredictionRecord,
    evaluated_at: datetime,
) -> dict[str, object]:
    return {
        "schema_version": MATERIALITY_EVALUATION_METHODOLOGY,
        "target_definition": TARGET_DEFINITION,
        "benchmark_ticker": MATERIALITY_BENCHMARK_TICKER,
        "threshold_abs_abnormal_pct": MATERIALITY_THRESHOLD_PCT,
        "decision_rule": "calibrated_validation_coverage_50",
        "decision_at": to_rfc3339(prediction.decision_at),
        "evaluated_at": to_rfc3339(evaluated_at),
    }


def _unavailable_outcome(
    prediction: MaterialityPredictionRecord,
    now: datetime,
    reason: str,
    *,
    entry_at: datetime | None = None,
    target_at: datetime | None = None,
) -> dict[str, object]:
    return {
        **_outcome_base(prediction, now),
        "status": "unavailable",
        "reason": reason,
        "entry_at": to_rfc3339(entry_at) if entry_at is not None else None,
        "target_at": to_rfc3339(target_at) if target_at is not None else None,
        "observed_at": None,
        "stock_return_pct": None,
        "benchmark_return_pct": None,
        "abnormal_return_pct": None,
        "actual_material": None,
        "predicted_material": _predicted_material(prediction),
        "verdict": None,
    }


def _market_rows(payload: Mapping[str, object]) -> list[tuple[datetime, float]]:
    candles = payload.get("candles")
    if not isinstance(candles, list):
        return []
    rows = []
    for candle in candles:
        if not isinstance(candle, dict) or candle.get("begin") is None:
            continue
        price = _number(candle.get("open"))
        if price is None or price <= 0:
            continue
        rows.append((_timestamp(str(candle["begin"])), price))
    return sorted(rows, key=lambda item: item[0])


def _first_at_or_after(
    rows: Sequence[tuple[datetime, float]],
    target: datetime,
) -> tuple[datetime, float] | None:
    return next((row for row in rows if row[0] >= target), None)


def _predicted_material(prediction: MaterialityPredictionRecord) -> bool:
    return PRIMARY_COVERAGE_PCT in prediction.selected_at_coverage_pct.get("calibrated", ())


def _current_predictions(
    news: Sequence[NewsRecord],
    runtime: MaterialityRuntime,
) -> list[MaterialityPredictionRecord]:
    assert runtime.model is not None
    selected: dict[str, MaterialityPredictionRecord] = {}
    for item in news:
        for prediction in item.materiality_predictions:
            if (
                prediction.model_version != runtime.model.model_version
                or prediction.artifact_payload_sha256 != runtime.model.artifact_payload_sha256
            ):
                continue
            previous = selected.get(prediction.id)
            if previous is None or prediction.updated_at > previous.updated_at:
                selected[prediction.id] = prediction
    return list(selected.values())


def _api_outcome(
    prediction: MaterialityPredictionRecord,
    news: NewsRecord | None,
) -> dict[str, object]:
    assert prediction.outcome_4h is not None
    return {
        "prediction_id": prediction.id,
        "news_id": prediction.news_id,
        "ticker": prediction.ticker,
        "probability": prediction.calibrated_probability,
        "decision_at": to_rfc3339(prediction.decision_at),
        "news": (
            {
                "title": news.title,
                "source_id": news.source_id,
                "url": news.url,
                "published_at": to_rfc3339(news.published_at),
            }
            if news is not None
            else None
        ),
        **dict(prediction.outcome_4h),
    }


def _confusion(
    predictions: Sequence[MaterialityPredictionRecord],
) -> dict[str, int]:
    result = {
        "true_positive": 0,
        "true_negative": 0,
        "false_positive": 0,
        "false_negative": 0,
    }
    for prediction in predictions:
        assert prediction.outcome_4h is not None
        actual = bool(prediction.outcome_4h["actual_material"])
        predicted = bool(prediction.outcome_4h["predicted_material"])
        key = (
            "true_positive"
            if actual and predicted
            else "true_negative"
            if not actual and not predicted
            else "false_positive"
            if predicted
            else "false_negative"
        )
        result[key] += 1
    return result


def _percentage(
    numerator: int,
    denominator: int,
    *,
    zero: float | None = None,
) -> float | None:
    return round(numerator / denominator * 100, 1) if denominator else zero


def _majority_accuracy(labels: Sequence[bool]) -> float | None:
    if not labels:
        return None
    positives = sum(labels)
    return round(max(positives, len(labels) - positives) / len(labels) * 100, 1)


def _roc_auc(probabilities: Sequence[float], labels: Sequence[bool]) -> float | None:
    positives = sum(labels)
    negatives = len(labels) - positives
    if not positives or not negatives:
        return None
    ranked = sorted(zip(probabilities, labels, strict=True), key=lambda item: item[0])
    positive_rank_sum = 0.0
    index = 0
    while index < len(ranked):
        end = index + 1
        while end < len(ranked) and ranked[end][0] == ranked[index][0]:
            end += 1
        average_rank = (index + 1 + end) / 2
        positive_rank_sum += average_rank * sum(label for _, label in ranked[index:end])
        index = end
    auc = (positive_rank_sum - positives * (positives + 1) / 2) / (positives * negatives)
    return round(auc, 4)


def _brier_score(
    probabilities: Sequence[float],
    labels: Sequence[bool],
) -> float | None:
    if not probabilities:
        return None
    score = sum(
        (probability - float(label)) ** 2
        for probability, label in zip(probabilities, labels, strict=True)
    ) / len(probabilities)
    return round(score, 4)


def _empty_summary() -> dict[str, object]:
    return {
        "scored_news": 0,
        "ready_predictions": 0,
        "evaluated": 0,
        "pending": 0,
        "unavailable": 0,
        "coverage_pct": 0.0,
        "actual_material": 0,
        "predicted_material": 0,
        "actual_material_pct": None,
        "accuracy_pct": None,
        "precision_pct": None,
        "recall_pct": None,
        "specificity_pct": None,
        "majority_baseline_accuracy_pct": None,
        "accuracy_lift_pct_points": None,
        "roc_auc": None,
        "brier_score": None,
        "confusion": {
            "true_positive": 0,
            "true_negative": 0,
            "false_positive": 0,
            "false_negative": 0,
        },
    }


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require_aware(parsed, "candle timestamp")
    return parsed.astimezone(UTC)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
