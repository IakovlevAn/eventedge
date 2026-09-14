"""Tests for point-in-time evaluation of materiality predictions."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from eventedge.materiality import runtime_from_environment
from eventedge.materiality_evals import (
    evaluate_materiality_outcome,
    materiality_eval_report,
)
from eventedge.storage import MaterialityPredictionRecord, NewsRecord


def _market(*rows: tuple[datetime, float]) -> dict[str, object]:
    return {
        "candles": [
            {
                "begin": timestamp.isoformat().replace("+00:00", "Z"),
                "open": price,
            }
            for timestamp, price in rows
        ]
    }


def _prediction(
    news_id: str,
    *,
    probability: float,
    selected: bool,
    outcome: dict[str, object] | None = None,
) -> MaterialityPredictionRecord:
    runtime = runtime_from_environment({"NEWS_MATERIALITY_MODE": "shadow"})
    assert runtime.model is not None
    timestamp = datetime(2026, 9, 14, 9, 5, tzinfo=UTC)
    return MaterialityPredictionRecord(
        id=f"mat_{news_id}",
        news_id=news_id,
        ticker="SBER",
        decision_at=timestamp,
        data_cutoff_at=timestamp,
        status="ready",
        reason=None,
        raw_probability=probability,
        calibrated_probability=probability,
        selected_at_coverage_pct={"calibrated": (50, 100) if selected else (100,)},
        eligible_for_ranking=True,
        missing_features=(),
        model_id=runtime.model.model_id,
        model_version=runtime.model.model_version,
        artifact_payload_sha256=runtime.model.artifact_payload_sha256,
        feature_schema_version="news-materiality-runtime-features-1.0",
        created_at=timestamp,
        updated_at=timestamp,
        outcome_4h=outcome,
    )


def _news(
    news_id: str,
    prediction: MaterialityPredictionRecord,
) -> NewsRecord:
    published_at = prediction.decision_at - timedelta(minutes=5)
    return NewsRecord(
        id=news_id,
        source_id="rbc",
        external_id=news_id,
        published_at=published_at,
        received_at=published_at,
        title=f"Новость {news_id}",
        url=f"https://example.com/{news_id}",
        content="Событие компании.",
        language="ru",
        source_metadata={"analysis_candidate": True, "tickers": ["SBER"]},
        created_at=published_at,
        materiality_predictions=(prediction,),
    )


def test_materiality_outcome_matches_training_target() -> None:
    prediction = _prediction("important", probability=0.8, selected=True)
    entry = prediction.decision_at + timedelta(minutes=1)
    target = entry + timedelta(hours=4)

    outcome = evaluate_materiality_outcome(
        prediction,
        _market((entry, 100.0), (target + timedelta(minutes=1), 101.0)),
        _market((entry, 200.0), (target + timedelta(minutes=2), 200.2)),
        now=target + timedelta(minutes=3),
    )

    assert outcome is not None
    assert outcome["status"] == "evaluated"
    assert outcome["stock_return_pct"] == pytest.approx(1.0)
    assert outcome["benchmark_return_pct"] == pytest.approx(0.1)
    assert outcome["abnormal_return_pct"] == pytest.approx(0.9)
    assert outcome["actual_material"] is True
    assert outcome["predicted_material"] is True
    assert outcome["verdict"] is True


def test_materiality_outcome_stays_pending_until_target() -> None:
    prediction = _prediction("pending", probability=0.4, selected=False)
    entry = prediction.decision_at + timedelta(minutes=1)
    target = entry + timedelta(hours=4)

    outcome = evaluate_materiality_outcome(
        prediction,
        _market((entry, 100.0)),
        _market((entry, 200.0)),
        now=target - timedelta(seconds=1),
    )

    assert outcome is None


def test_materiality_report_exposes_classification_and_calibration_metrics() -> None:
    positive = _prediction("positive", probability=0.8, selected=True)
    negative = _prediction("negative", probability=0.2, selected=False)
    positive = replace(
        positive,
        outcome_4h={
            "status": "evaluated",
            "actual_material": True,
            "predicted_material": True,
            "verdict": True,
        },
    )
    negative = replace(
        negative,
        outcome_4h={
            "status": "evaluated",
            "actual_material": False,
            "predicted_material": False,
            "verdict": True,
        },
    )
    runtime = runtime_from_environment({"NEWS_MATERIALITY_MODE": "shadow"})

    report = materiality_eval_report(
        [_news("positive", positive), _news("negative", negative)],
        runtime=runtime,
    )

    assert report["status"] == "ready"
    assert report["mode"] == "shadow"
    assert report["decision_rule"] == "calibrated_validation_coverage_50"
    summary = report["summary"]
    assert summary["evaluated"] == 2
    assert summary["accuracy_pct"] == 100.0
    assert summary["precision_pct"] == 100.0
    assert summary["recall_pct"] == 100.0
    assert summary["roc_auc"] == 1.0
    assert summary["brier_score"] == pytest.approx(0.04)
    assert len(report["outcomes"]) == 2
