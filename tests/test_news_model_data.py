"""Tests for causal news-model dataset preparation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from signal_fixtures import signal_example

from eventedge_research.news_model_data import (
    _fit_bundle,
    _identity_sequence_sha256,
    _market_content_sha256,
    _past_labels,
    _resolve_artifact_path,
    _verify_contract,
    _verify_fold_contract,
    _verify_label_availability,
)
from eventedge_research.news_model_features import MinuteOpenSeries
from eventedge_research.research_artifacts import read_json
from eventedge_research.signal_dataset import SignalDatasetExample, signal_dataset_sha256

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_past_labels_exclude_outcomes_not_available_at_evaluation() -> None:
    early = SimpleNamespace(id="early")
    late = SimpleNamespace(id="late")
    evaluation = SimpleNamespace(id="evaluation")
    split = SimpleNamespace(
        train=[early, late],
        validation=[],
        test=[evaluation],
    )
    evaluation_from = datetime(2026, 3, 1, tzinfo=UTC)
    outcomes = {
        "early": {
            "materiality_4h": 1.0,
            "available_4h_epoch": evaluation_from.timestamp() - 1,
        },
        "late": {
            "materiality_4h": -1.0,
            "available_4h_epoch": evaluation_from.timestamp(),
        },
        "evaluation": {
            "materiality_4h": 1.0,
            "available_4h_epoch": evaluation_from.timestamp() + 1,
        },
    }

    labels = _past_labels(split, outcomes, "materiality_4h", evaluation_from)

    assert labels == {
        "training": [{"example_id": "early", "return_pct": 1.0}],
        "validation": [],
        "evaluation_ids": ["evaluation"],
    }


def test_label_availability_must_include_benchmark_observation() -> None:
    example = SignalDatasetExample.model_validate(signal_example(1))
    benchmark_at = example.outcome_4h.target_at + timedelta(minutes=5)
    benchmark = MinuteOpenSeries(
        times=(benchmark_at,),
        prices=(2_700.0,),
        daily_times={},
        daily_prices={},
    )

    with pytest.raises(ValueError, match="stock and benchmark observations"):
        _verify_label_availability(example, benchmark)

    corrected = example.model_copy(update={"label_available_at": benchmark_at})
    _verify_label_availability(corrected, benchmark)


@pytest.mark.parametrize(
    "value",
    (
        "/tmp/dataset.jsonl",
        "../dataset.jsonl",
        ".local-artifacts/../dataset.jsonl",
        "EventEdge/dataset.jsonl",
    ),
)
def test_resolve_artifact_path_rejects_non_local_or_escaping_paths(
    tmp_path: Path,
    value: str,
) -> None:
    with pytest.raises(ValueError, match="local and relative"):
        _resolve_artifact_path(tmp_path, value)


def test_resolve_artifact_path_accepts_local_artifact(tmp_path: Path) -> None:
    path = _resolve_artifact_path(
        tmp_path,
        ".local-artifacts/news-model/dataset.jsonl",
    )

    assert path == tmp_path / ".local-artifacts/news-model/dataset.jsonl"


def test_resolve_artifact_path_rejects_symlink_outside_local_directory(
    tmp_path: Path,
) -> None:
    artifact_directory = tmp_path / ".local-artifacts"
    artifact_directory.mkdir()
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n")
    (artifact_directory / "dataset.jsonl").symlink_to(outside)

    with pytest.raises(ValueError, match="escapes its root"):
        _resolve_artifact_path(tmp_path, ".local-artifacts/dataset.jsonl")


def test_market_content_hash_rejects_symlink_escape(tmp_path: Path) -> None:
    market_directory = tmp_path / "market"
    market_directory.mkdir()
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n")
    (market_directory / "IMOEX2.jsonl").symlink_to(outside)

    with pytest.raises(ValueError, match="escapes its directory"):
        _market_content_sha256(market_directory, "IMOEX2.jsonl")


def test_identity_sequence_hash_is_order_sensitive() -> None:
    assert _identity_sequence_sha256(["a", "b"]) != _identity_sequence_sha256(["b", "a"])


def test_verify_fold_contract_rejects_changed_membership_hash() -> None:
    folds = [
        {
            "name": f"fold_{index}",
            "validation_from": f"2025-{index:02d}-01T00:00:00+00:00",
            "evaluation_from": f"2026-{index:02d}-01T00:00:00+00:00",
            "evaluation_until": f"2026-{index + 1:02d}-01T00:00:00+00:00",
            "evaluation_rows": 1,
            "evaluation_ids_sha256": "not-a-sha",
        }
        for index in range(1, 8)
    ]

    with pytest.raises(ValueError, match="unsafe or incompatible"):
        _verify_fold_contract(folds, 7)


@pytest.mark.parametrize(
    "schema_version",
    ("news-model-benchmark-contract-1.0", "news-model-benchmark-contract-2.0"),
)
def test_legacy_news_model_contract_is_rejected(
    tmp_path: Path,
    schema_version: str,
) -> None:
    contract_path = tmp_path / "contract.json"
    contract_path.write_text("{}")

    with pytest.raises(ValueError, match="unsafe or incompatible"):
        _verify_contract(
            contract_path,
            {"schema_version": schema_version},
            tmp_path,
        )


def test_fit_bundle_excludes_formula_comparison_expectation(tmp_path: Path) -> None:
    contract_path = tmp_path / "contract.json"
    publication_path = tmp_path / "publication-inputs.jsonl"
    update_path = tmp_path / "update-5m-inputs.jsonl"
    contract_path.write_text("{}")
    publication_path.write_text("")
    update_path.write_text("")
    contract = {
        "dataset": {"sha256": "d" * 64},
        "expected_output": {
            "model_fits": 35,
            "prediction_rows": 2400,
            "frozen_probabilities_sha256": "p" * 64,
            "outcomes_sha256": "o" * 64,
            "formula_comparison": {"rows": 157},
        },
    }

    bundle = _fit_bundle(
        contract_path,
        contract,
        publication_path,
        update_path,
        folds=[],
    )

    assert bundle["schema_version"] == "news-model-fit-bundle-1.0"
    assert bundle["evaluation_outcomes_included"] is False
    assert "formula_comparison" not in bundle["expected_output"]


def test_tracked_manifest_locks_news_model_contract() -> None:
    contract_path = PROJECT_ROOT / "EventEdge/NEWS_MODEL_BENCHMARK_CONTRACT.json"
    manifest = read_json(PROJECT_ROOT / "EventEdge/ML_ARTIFACT_MANIFEST.json")

    assert manifest["tracked_evidence"]["benchmark_contract"]["sha256"] == signal_dataset_sha256(
        contract_path
    )
