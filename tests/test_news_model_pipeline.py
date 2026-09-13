"""End-to-end tests for generic local news-model preparation."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eventedge_research.news_model_artifact import (
    load_portable_direction_model,
    load_portable_materiality_model,
    predict_direction_jsonl,
    predict_materiality_jsonl,
)
from eventedge_research.news_model_pipeline import prepare_news_model_dataset
from eventedge_research.news_model_training import (
    fit_latest_direction_artifact,
    fit_latest_materiality_artifact,
)
from eventedge_research.research_artifacts import write_jsonl
from eventedge_research.signal_dataset import (
    SignalDatasetExample,
    SignalFeatureSnapshot,
    signal_dataset_sha256,
    write_signal_dataset,
)
from eventedge_research.signal_dataset_builder import (
    MarketOpenObservation,
    SignalEventCandidate,
    SignalEventCandidateV2,
    build_signal_dataset,
)


@pytest.mark.parametrize(
    ("candidate_schema", "dataset_schema"),
    [
        ("v1", "signal-dataset-example-1.0"),
        ("v2", "signal-dataset-example-2.0"),
    ],
)
def test_prepare_news_model_dataset_builds_portable_models(
    tmp_path: Path,
    candidate_schema: str,
    dataset_schema: str,
) -> None:
    examples, stock, benchmark = _model_fixture(candidate_schema=candidate_schema)
    dataset_path = tmp_path / "dataset.jsonl"
    stock_path = tmp_path / "stock.jsonl"
    benchmark_path = tmp_path / "benchmark.jsonl"
    write_signal_dataset(dataset_path, examples)
    write_jsonl(stock_path, (row.model_dump(mode="json") for row in stock))
    write_jsonl(benchmark_path, (row.model_dump(mode="json") for row in benchmark))

    report = prepare_news_model_dataset(
        dataset_path,
        [stock_path],
        benchmark_path,
        tmp_path / "prepared",
        validation_from=datetime(2026, 4, 1, tzinfo=UTC),
        evaluation_from=datetime(2026, 6, 1, tzinfo=UTC),
        evaluation_until=datetime(2026, 7, 5, tzinfo=UTC),
        benchmark_id="IMOEX",
        allow_synthetic=True,
    )

    bundle = json.loads((tmp_path / "prepared/fit-bundle.json").read_text())
    assert report["dataset_schema_version"] == dataset_schema
    assert report["rows"] == 44
    assert bundle["portable_model_specs"] == {
        "direction": "update_5m_common_direction",
        "materiality": "update_5m_symmetric_materiality",
    }
    assert bundle["folds"][0]["partitions"] == {
        "evaluation": 8,
        "future": 0,
        "purged": 0,
        "training": 24,
        "validation": 12,
    }
    assert len(bundle["folds"][0]["evaluation_ids"]) == 8
    assert (tmp_path / "prepared/outcomes.jsonl").is_file()
    assert (tmp_path / "prepared/scoring-seal.json").is_file()

    bundle_path = tmp_path / "prepared/fit-bundle.json"
    artifact_path = tmp_path / "direction-model.json"
    artifact = fit_latest_direction_artifact(
        bundle_path,
        artifact_path,
        expected_fit_bundle_sha256=signal_dataset_sha256(bundle_path),
    )
    model = load_portable_direction_model(
        artifact_path,
        expected_payload_sha256=artifact["artifact_payload_sha256"],
    )
    inference_report = predict_direction_jsonl(
        model,
        tmp_path / "prepared/update-5m-inputs.jsonl",
        tmp_path / "direction-predictions.jsonl",
    )
    assert inference_report["rows"] == 44

    materiality_artifact_path = tmp_path / "materiality-model.json"
    materiality_artifact = fit_latest_materiality_artifact(
        bundle_path,
        materiality_artifact_path,
        expected_fit_bundle_sha256=signal_dataset_sha256(bundle_path),
    )
    materiality_model = load_portable_materiality_model(
        materiality_artifact_path,
        expected_payload_sha256=materiality_artifact["artifact_payload_sha256"],
    )
    materiality_report = predict_materiality_jsonl(
        materiality_model,
        tmp_path / "prepared/update-5m-inputs.jsonl",
        tmp_path / "materiality-predictions.jsonl",
    )
    assert materiality_report["rows"] == 44


def test_prepare_news_model_dataset_rejects_unrelated_market_opens(
    tmp_path: Path,
) -> None:
    examples, stock, benchmark = _model_fixture(candidate_schema="v2")
    dataset_path = tmp_path / "dataset.jsonl"
    stock_path = tmp_path / "stock.jsonl"
    benchmark_path = tmp_path / "benchmark.jsonl"
    write_signal_dataset(dataset_path, examples)
    stock[2] = stock[2].model_copy(update={"open": stock[2].open + 7.0})
    write_jsonl(stock_path, (row.model_dump(mode="json") for row in stock))
    write_jsonl(benchmark_path, (row.model_dump(mode="json") for row in benchmark))

    with pytest.raises(ValueError, match="do not reproduce dataset row"):
        prepare_news_model_dataset(
            dataset_path,
            [stock_path],
            benchmark_path,
            tmp_path / "prepared",
            validation_from=datetime(2026, 4, 1, tzinfo=UTC),
            evaluation_from=datetime(2026, 6, 1, tzinfo=UTC),
            evaluation_until=datetime(2026, 7, 5, tzinfo=UTC),
            benchmark_id="IMOEX",
            allow_synthetic=True,
        )


def _model_fixture(
    *,
    candidate_schema: str,
) -> tuple[
    list[SignalDatasetExample],
    list[MarketOpenObservation],
    list[MarketOpenObservation],
]:
    decisions = [
        *(datetime(2026, 1, 1, 10, tzinfo=UTC) + timedelta(days=3 * index) for index in range(24)),
        *(datetime(2026, 4, 5, 10, tzinfo=UTC) + timedelta(days=3 * index) for index in range(12)),
        *(datetime(2026, 6, 5, 10, tzinfo=UTC) + timedelta(days=3 * index) for index in range(8)),
    ]
    examples = []
    candidates = []
    stock = []
    benchmark = []
    for index, decision_at in enumerate(decisions):
        direction = 1 if index % 2 == 0 else -1
        magnitude = 1.0 if index % 4 < 2 else 0.1
        published_at = decision_at - timedelta(minutes=5)
        title = f"Компания опубликовала результаты {index}"
        content = "Финансовый результат изменился относительно прошлого периода."
        values = {
            "event_id": f"event-{index}",
            "ticker": "SBER",
            "news_ids": (f"news-{index}",),
            "primary_news_id": f"news-{index}",
            "source_id": "fixture",
            "title": title,
            "content": content,
            "published_at": published_at,
            "received_at": decision_at,
            "decision_at": decision_at,
            "features": SignalFeatureSnapshot(
                as_of=decision_at,
                event_type="financial_results",
                polarity=0.5,
                materiality=0.8,
                novelty=1.0,
                source_quality=0.9,
                fact_count=2,
                rule_direction="up",
                rule_score=25.0,
                rule_confidence=0.85,
                rule_model_version="signal-engine-fixture",
                rule_config_version=1,
            ),
            "corporate_action_status": "unknown",
            "notes": "Synthetic contract fixture; not financial ground truth.",
        }
        if candidate_schema == "v2":
            candidates.append(
                SignalEventCandidateV2(
                    **values,
                    event_group_id=f"event-{index}",
                    source_url=f"https://t.me/fixture/{index + 1}",
                    retrieved_at=decision_at + timedelta(days=1),
                    admission_version="telegram-archive-admission-1.0",
                    analysis_title=title,
                    analysis_content_sha256=hashlib.sha256(
                        content.encode("utf-8")
                    ).hexdigest(),
                    receipt_provenance="publication_plus_assumed_5_minutes",
                    text_provenance=(
                        "no_edit_marker_current_view_not_first_snapshot"
                    ),
                )
            )
        else:
            candidates.append(SignalEventCandidate(**values))
        entry_at = decision_at + timedelta(minutes=1)
        remaining_target = published_at + timedelta(hours=4)
        outcome_target = entry_at + timedelta(hours=4)
        outcome_price = 100.0 * (1 + direction * magnitude / 100)
        stock.extend(
            _opens(
                "SBER",
                {
                    published_at: 100.0,
                    decision_at: 100.2 if index % 3 else 99.8,
                    entry_at: 100.0,
                    remaining_target: outcome_price,
                    outcome_target: outcome_price,
                },
            )
        )
        benchmark.extend(
            _opens(
                "IMOEX",
                {
                    published_at: 100.0,
                    decision_at: 100.0,
                    entry_at: 100.0,
                    remaining_target: 100.0,
                    outcome_target: 100.0,
                },
            )
        )
    examples, report = build_signal_dataset(
        candidates,
        stock,
        benchmark_observations=benchmark,
        benchmark_id="IMOEX",
    )
    assert report["skipped_event_tickers"] == 0
    return examples, stock, benchmark


def _opens(
    ticker: str,
    values: dict[datetime, float],
) -> list[MarketOpenObservation]:
    return [
        MarketOpenObservation(
            ticker=ticker,
            at=timestamp,
            open=price,
            provider_id="synthetic-fixture",
            adjusted=False,
            label_source="synthetic_test",
        )
        for timestamp, price in values.items()
    ]
