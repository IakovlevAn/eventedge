from __future__ import annotations

import pytest

from eventedge_research.signal_production_metrics import build_current_production_metrics


def test_current_production_metrics_selects_latest_config_and_live_cohort() -> None:
    signals = [
        _signal("old", 6, "2026-09-01T10:01:00Z", "up"),
        _signal("hit", 7, "2026-09-08T10:01:00Z", "up", source="rbc"),
        _signal("miss", 7, "2026-09-08T11:01:00Z", "down", source="tass"),
        _signal("neutral", 7, "2026-09-08T12:01:00Z", "neutral", source="rbc"),
    ]
    outcomes = [
        _outcome("hit", "up", 1.0, "live", 0.8, 40.0),
        _outcome("miss", "down", 0.5, "live", 0.9, -40.0),
        _outcome("neutral", "neutral", -0.25, "live", 0.7, 0.0),
    ]
    epoch = {
        "epoch_id": "epoch-7",
        "model_version": "signal-engine-0.6.1",
        "config_version": 7,
        "evaluated_at": "2026-09-09T09:00:00Z",
        "outcomes": outcomes,
        "observations_truncated": False,
    }
    later_old_config_epoch = {
        "epoch_id": "epoch-6-later",
        "model_version": "signal-engine-0.6.1",
        "config_version": 6,
        "evaluated_at": "2026-09-10T09:00:00Z",
        "outcomes": [_outcome("old", "up", 1.0, "live", 0.8, 40.0, config_version=6)],
        "observations_truncated": False,
    }
    news = {
        row["news_id"]: {"news_id": row["news_id"], "source_id": row["source"]}
        for row in signals
        if row["config_version"] == 7
    }
    features = [
        {
            "news_id": row["news_id"],
            "created_at": row["created_at"],
            "extractor_version": "rules-fallback-0.2.0",
        }
        for row in signals
        if row["config_version"] == 7
    ]

    report = build_current_production_metrics(
        signals,
        [later_old_config_epoch, epoch],
        news,
        features,
    )

    assert report["selection"]["config_version"] == 7
    assert report["selection"]["outcomes_match_exported_signals"] is True
    assert report["inventory"]["directions"] == {"down": 1, "neutral": 1, "up": 1}
    live = report["strict_4h"]["live"]
    assert live["hit_rate_pct"] == 50.0
    assert live["directional_share_of_stored_signals_pct"] == pytest.approx(200 / 3)
    assert live["signed_return_pct"]["mean"] == 0.25
    assert live["matched_direction_controls"]["always_up"]["hit_rate_pct"] == 100.0
    assert live["event_return_cost_scenarios"]["10"]["mean_event_return_pct"] == pytest.approx(0.15)
    assert live["confidence_as_probability_of_correct_direction"]["rows"] == 2
    assert report["production_native"]["summary"]["hit_rate_pct"] == 50.0


def test_current_production_metrics_rejects_outcomes_missing_from_signal_export() -> None:
    signal = _signal("known", 7, "2026-09-08T10:01:00Z", "up")
    epoch = {
        "epoch_id": "epoch-7",
        "model_version": "signal-engine-0.6.1",
        "config_version": 7,
        "evaluated_at": "2026-09-09T09:00:00Z",
        "outcomes": [_outcome("unknown", "up", 1.0, "live", 0.8, 40.0)],
        "observations_truncated": False,
    }
    news = {signal["news_id"]: {"news_id": signal["news_id"], "source_id": "rbc"}}
    features = [
        {
            "news_id": signal["news_id"],
            "created_at": signal["created_at"],
            "extractor_version": "rules-fallback-0.2.0",
        }
    ]

    with pytest.raises(ValueError, match="absent from the signal export"):
        build_current_production_metrics([signal], [epoch], news, features)


def test_current_production_metrics_can_select_an_explicit_config() -> None:
    old_signal = _signal("old", 6, "2026-09-01T10:01:00Z", "up")
    new_signal = _signal("new", 7, "2026-09-08T10:01:00Z", "down")
    old_epoch = {
        "epoch_id": "epoch-6",
        "model_version": "signal-engine-0.6.1",
        "config_version": 6,
        "evaluated_at": "2026-09-09T09:00:00Z",
        "outcomes": [_outcome("old", "up", 1.0, "live", 0.8, 40.0, config_version=6)],
        "observations_truncated": False,
    }
    new_epoch = {
        "epoch_id": "epoch-7",
        "model_version": "signal-engine-0.6.1",
        "config_version": 7,
        "evaluated_at": "2026-09-09T09:00:00Z",
        "outcomes": [_outcome("new", "down", -1.0, "live", 0.8, -40.0)],
        "observations_truncated": False,
    }
    news = {old_signal["news_id"]: {"source_id": "rbc"}}
    features = [
        {
            "news_id": old_signal["news_id"],
            "created_at": old_signal["created_at"],
            "extractor_version": "rules-fallback-0.2.0",
        }
    ]

    report = build_current_production_metrics(
        [old_signal, new_signal],
        [old_epoch, new_epoch],
        news,
        features,
        config_version=6,
    )

    assert report["selection"]["config_version"] == 6
    assert report["selection"]["basis"] == "requested_ready_config_for_current_model"
    assert report["inventory"]["signals"] == 1
    assert report["strict_4h"]["live"]["hit_rate_pct"] == 100.0


def _signal(
    identity: str,
    config_version: int,
    created_at: str,
    direction: str,
    *,
    source: str = "rbc",
) -> dict[str, object]:
    return {
        "signal_id": f"sig-{identity}",
        "news_id": f"news-{identity}",
        "source": source,
        "as_of": created_at,
        "created_at": created_at,
        "direction": direction,
        "model_version": "signal-engine-0.6.1",
        "config_version": config_version,
    }


def _outcome(
    identity: str,
    direction: str,
    return_4h: float,
    cohort: str,
    confidence: float,
    score: float,
    *,
    config_version: int = 7,
) -> dict[str, object]:
    verdict = None
    if direction == "up":
        verdict = return_4h > 0
    elif direction == "down":
        verdict = return_4h < 0
    return {
        "signal_id": f"sig-{identity}",
        "ticker": "SBER",
        "as_of": "2026-09-08T10:00:00Z",
        "direction": direction,
        "score": score,
        "confidence": confidence,
        "status": "partial",
        "verdict": verdict,
        "verdict_status": "not_applicable" if direction == "neutral" else "evaluated",
        "returns": {"1h": return_4h / 2, "4h": return_4h, "1d": None, "3d": None},
        "eligibility": {"cohort": cohort, "eligible": cohort == "live", "reason": None},
        "evaluation_methodology": "market-outcome-0.3.0",
        "model_version": "signal-engine-0.6.1",
        "config_version": config_version,
    }
