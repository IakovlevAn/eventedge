from __future__ import annotations

from eventedge_research.ydb_production_signal_dataset import build_ydb_production_signal_dataset


def test_build_ydb_production_signal_dataset_keeps_strict_live_4h_rows() -> None:
    signals = [_signal("kept", "up"), _signal("neutral", "neutral"), _signal("old", "down")]
    outcomes = [
        _outcome("kept", "live", 1.25),
        _outcome("neutral", "live", None),
        _outcome("old", "retrospective", -0.5),
    ]
    news = {
        row["news_id"]: {
            "news_id": row["news_id"],
            "source_id": "rbc",
            "title": f"News {row['signal_id']}",
            "content": "Сбербанк сообщил о росте прибыли.",
            "published_at": "2026-09-01T09:59:00Z",
            "received_at": "2026-09-01T10:00:00Z",
            "source_metadata": {"categories": ["Финансы"]},
        }
        for row in signals
    }
    feature_sets = [_feature_set(row) for row in signals]
    epoch = {
        "epoch_id": "epoch-6",
        "model_version": "signal-engine-0.6.1",
        "config_version": 6,
        "evaluated_at": "2026-09-02T00:00:00Z",
        "observations_truncated": False,
        "outcomes": outcomes,
    }

    examples, report = build_ydb_production_signal_dataset(
        signals,
        [epoch],
        news,
        feature_sets,
        model_version="signal-engine-0.6.1",
        config_version=6,
    )

    assert [row.id for row in examples] == ["ydb-sig-kept"]
    assert examples[0].outcome_4h.return_pct == 1.25
    assert examples[0].features.semantic_feature_set_id == "feat-kept"
    assert examples[0].features.source_quality == 0.78
    assert examples[0].corporate_action_status == "unknown"
    assert report["examples"] == 1
    assert report["corporate_action_statuses"] == {"unknown": 1}
    assert report["skipped"] == {
        "cohort:retrospective": 1,
        "strict_4h_return_unavailable": 1,
    }


def _signal(identity: str, direction: str) -> dict[str, object]:
    return {
        "signal_id": f"sig-{identity}",
        "news_id": f"news-{identity}",
        "ticker": "SBER",
        "direction": direction,
        "score": 25.0 if direction == "up" else -25.0 if direction == "down" else 0.0,
        "confidence": 0.8,
        "model_version": "signal-engine-0.6.1",
        "config_version": 6,
        "created_at": "2026-09-01T10:01:00Z",
    }


def _feature_set(signal: dict[str, object]) -> dict[str, object]:
    identity = str(signal["signal_id"]).removeprefix("sig-")
    return {
        "feature_set_id": f"feat-{identity}",
        "news_id": signal["news_id"],
        "created_at": signal["created_at"],
        "extractor_version": "rules-fallback-0.2.0",
        "features": {
            "event_type": "financial_results",
            "polarity": 0.8,
            "materiality": 0.9,
            "novelty": 1.0,
            "facts": [{}],
        },
    }


def _outcome(identity: str, cohort: str, return_4h: float | None) -> dict[str, object]:
    observation = {
        "target_at": "2026-09-01T14:10:00Z",
        "observed_at": "2026-09-01T14:10:00Z",
        "timely": return_4h is not None,
    }
    return {
        "signal_id": f"sig-{identity}",
        "evaluation_methodology": "market-outcome-0.3.0",
        "eligibility": {
            "cohort": cohort,
            "decision_at": "2026-09-01T10:01:00Z",
        },
        "entry": {"at": "2026-09-01T10:10:00Z", "price": 100.0},
        "horizon_observations": {"4h": observation},
        "returns": {"4h": return_4h},
    }
