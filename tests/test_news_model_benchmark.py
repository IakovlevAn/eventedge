"""Contract tests for the retained news-model benchmark."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from eventedge_research.news_model_benchmark import (
    MODEL_DEFINITIONS,
    _bundle_file,
    _formula_comparison_rows,
    _hard_direction_metrics,
    _load_outcomes,
    fit_benchmark,
    score_benchmark,
)
from eventedge_research.signal_dataset import signal_dataset_sha256

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_retained_models_cover_materiality_and_direction_controls() -> None:
    identities = {definition.model_id for definition in MODEL_DEFINITIONS}

    assert identities == {
        "publication_materiality_logistic",
        "update_5m_market_control",
        "update_5m_symmetric_materiality_logistic",
        "update_5m_market_direction_control",
        "update_5m_corrected_reaction_interactions",
    }
    assert {definition.task for definition in MODEL_DEFINITIONS} == {
        "materiality",
        "direction",
    }


def test_hard_direction_metrics_include_returns_after_costs() -> None:
    metrics = _hard_direction_metrics(
        values=[1.0, -0.5],
        raw_values=[1.2, -0.4],
        directions=[1, -1],
    )

    assert metrics["hit_rate_pct"] == 100.0
    assert metrics["balanced_accuracy_pct"] == 100.0
    assert metrics["mean_abnormal_signed_return_pct"] == 0.75
    assert metrics["mean_net_10bps_abnormal_signed_return_pct"] == 0.65


def test_hard_direction_metrics_exclude_flat_from_balanced_accuracy() -> None:
    metrics = _hard_direction_metrics(
        values=[1.0, -1.0, 0.0],
        raw_values=[1.0, -1.0, 0.0],
        directions=[1, -1, -1],
    )

    assert metrics["hit_rate_pct"] == pytest.approx(200 / 3)
    assert metrics["balanced_accuracy_pct"] == 100.0
    assert metrics["flat_rows_excluded_from_balanced_accuracy"] == 1


def test_fit_rejects_bundle_not_matching_external_digest_before_json_decode(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "fit-bundle.json"
    bundle.write_text("not JSON", encoding="utf-8")

    with pytest.raises(ValueError, match="does not match the expected digest"):
        fit_benchmark(
            bundle,
            tmp_path / "output",
            expected_fit_bundle_sha256="0" * 64,
        )


def test_fit_cli_requires_expected_bundle_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import evaluate_news_models

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluate_news_models",
            "fit",
            "--fit-bundle",
            "fit-bundle.json",
            "--output",
            "output",
        ],
    )

    with pytest.raises(SystemExit):
        evaluate_news_models._arguments()


@pytest.mark.parametrize(
    "value",
    ("/tmp/inputs.jsonl", "../inputs.jsonl", "nested/inputs.jsonl", "inputs.json"),
)
def test_bundle_file_rejects_non_local_jsonl_filename(
    tmp_path: Path,
    value: str,
) -> None:
    with pytest.raises(ValueError, match="local JSONL filenames"):
        _bundle_file(tmp_path, value)


def test_bundle_file_resolves_filename_next_to_bundle(tmp_path: Path) -> None:
    assert _bundle_file(tmp_path, "inputs.jsonl") == tmp_path / "inputs.jsonl"


def test_bundle_file_rejects_symlink_escape(tmp_path: Path) -> None:
    bundle_directory = tmp_path / "bundle"
    bundle_directory.mkdir()
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n")
    (bundle_directory / "inputs.jsonl").symlink_to(outside)

    with pytest.raises(ValueError, match="escapes its directory"):
        _bundle_file(bundle_directory, "inputs.jsonl")


def test_score_rejects_outcomes_not_locked_by_fit(tmp_path: Path) -> None:
    output = tmp_path / "run"
    output.mkdir()
    probability_path = output / "frozen-probabilities.jsonl"
    probability_path.write_text("")
    outcomes = tmp_path / "outcomes.jsonl"
    outcomes.write_text("")
    scoring_seal, contract = _write_scoring_contract(tmp_path, outcomes)
    (output / "freeze.json").write_text(
        json.dumps(
            {
                "schema_version": "news-model-freeze-3.0",
                "evaluation_outcomes_read": False,
                "frozen_probabilities_sha256": signal_dataset_sha256(probability_path),
                "expected_outcomes_sha256": "0" * 64,
                "fit_bundle_sha256": None,
                "contract_sha256": signal_dataset_sha256(contract),
            }
        )
    )

    with pytest.raises(ValueError, match="scoring inputs changed"):
        score_benchmark(
            output,
            outcomes,
            scoring_seal,
            contract,
            expected_outcomes_sha256=signal_dataset_sha256(outcomes),
        )


def test_score_rejects_legacy_freeze_schema(tmp_path: Path) -> None:
    output = tmp_path / "run"
    output.mkdir()
    probability_path = output / "frozen-probabilities.jsonl"
    probability_path.write_text("")
    outcomes = tmp_path / "outcomes.jsonl"
    outcomes.write_text("")
    digest = signal_dataset_sha256(outcomes)
    scoring_seal, contract = _write_scoring_contract(tmp_path, outcomes)
    (output / "freeze.json").write_text(
        json.dumps(
            {
                "schema_version": "news-model-freeze-1.0",
                "evaluation_outcomes_read": False,
                "frozen_probabilities_sha256": signal_dataset_sha256(probability_path),
                "expected_outcomes_sha256": digest,
                "fit_bundle_sha256": None,
                "contract_sha256": signal_dataset_sha256(contract),
            }
        )
    )

    with pytest.raises(ValueError, match="scoring inputs changed"):
        score_benchmark(
            output,
            outcomes,
            scoring_seal,
            contract,
            expected_outcomes_sha256=digest,
        )


def test_score_rejects_formula_expectation_not_bound_to_contract(
    tmp_path: Path,
) -> None:
    output = tmp_path / "run"
    output.mkdir()
    probability_path = output / "frozen-probabilities.jsonl"
    probability_path.write_text("")
    outcomes = tmp_path / "outcomes.jsonl"
    outcomes.write_text("")
    digest = signal_dataset_sha256(outcomes)
    scoring_seal, contract = _write_scoring_contract(tmp_path, outcomes)
    seal = json.loads(scoring_seal.read_text())
    seal["formula_comparison"] = {"rows": 999}
    scoring_seal.write_text(json.dumps(seal))
    (output / "freeze.json").write_text(
        json.dumps(
            {
                "schema_version": "news-model-freeze-3.0",
                "evaluation_outcomes_read": False,
                "frozen_probabilities_sha256": signal_dataset_sha256(probability_path),
                "expected_outcomes_sha256": digest,
                "fit_bundle_sha256": None,
                "contract_sha256": signal_dataset_sha256(contract),
            }
        )
    )

    with pytest.raises(ValueError, match="scoring inputs changed"):
        score_benchmark(
            output,
            outcomes,
            scoring_seal,
            contract,
            expected_outcomes_sha256=digest,
        )


def test_score_rejects_probability_file_resealed_only_in_freeze(
    tmp_path: Path,
) -> None:
    output = tmp_path / "run"
    output.mkdir()
    probability_path = output / "frozen-probabilities.jsonl"
    probability_path.write_text('{"probability": 0.7}\n', encoding="utf-8")
    original_probability_sha256 = signal_dataset_sha256(probability_path)
    outcomes = tmp_path / "outcomes.jsonl"
    outcomes.write_text("", encoding="utf-8")
    scoring_seal, contract = _write_scoring_contract(
        tmp_path,
        outcomes,
        probability_sha256=original_probability_sha256,
    )
    probability_path.write_text('{"probability": 0.5}\n', encoding="utf-8")
    (output / "freeze.json").write_text(
        json.dumps(
            {
                "schema_version": "news-model-freeze-3.0",
                "evaluation_outcomes_read": False,
                "frozen_probabilities_sha256": signal_dataset_sha256(
                    probability_path
                ),
                "expected_outcomes_sha256": signal_dataset_sha256(outcomes),
                "fit_bundle_sha256": None,
                "contract_sha256": signal_dataset_sha256(contract),
                "dataset_sha256": "1" * 64,
            }
        )
    )

    with pytest.raises(ValueError, match="scoring inputs changed"):
        score_benchmark(
            output,
            outcomes,
            scoring_seal,
            contract,
            expected_outcomes_sha256=signal_dataset_sha256(outcomes),
        )


def test_score_rejects_freeze_dataset_not_bound_to_contract(tmp_path: Path) -> None:
    output = tmp_path / "run"
    output.mkdir()
    probability_path = output / "frozen-probabilities.jsonl"
    probability_path.write_text("", encoding="utf-8")
    outcomes = tmp_path / "outcomes.jsonl"
    outcomes.write_text("", encoding="utf-8")
    digest = signal_dataset_sha256(probability_path)
    scoring_seal, contract = _write_scoring_contract(
        tmp_path,
        outcomes,
        probability_sha256=digest,
    )
    (output / "freeze.json").write_text(
        json.dumps(
            {
                "schema_version": "news-model-freeze-3.0",
                "evaluation_outcomes_read": False,
                "frozen_probabilities_sha256": digest,
                "expected_outcomes_sha256": signal_dataset_sha256(outcomes),
                "fit_bundle_sha256": None,
                "contract_sha256": signal_dataset_sha256(contract),
                "dataset_sha256": "2" * 64,
            }
        )
    )

    with pytest.raises(ValueError, match="scoring inputs changed"):
        score_benchmark(
            output,
            outcomes,
            scoring_seal,
            contract,
            expected_outcomes_sha256=signal_dataset_sha256(outcomes),
        )


def test_formula_comparison_rows_use_only_directional_config6_intersection() -> None:
    predictions = [
        {
            "example_id": identity,
            "decision_at": f"2026-03-0{index}T10:00:00+03:00",
            "calibrated_probability": probability,
        }
        for index, (identity, probability) in enumerate(
            (("up", 0.8), ("down", 0.2), ("neutral", 0.6), ("config7", 0.4)),
            1,
        )
    ]
    outcomes = {
        "up": _formula_outcome("up", 0.9, 70, 1.0),
        "down": _formula_outcome("down", 0.8, -60, -1.0),
        "neutral": _formula_outcome("neutral", 0.5, 0, 0.5),
        "config7": {
            **_formula_outcome("down", 0.7, -40, -0.5),
            "formula_config_version": 7,
        },
    }

    rows = _formula_comparison_rows(predictions, outcomes)

    assert [row.example_id for row in rows] == ["up", "down"]
    assert [row.formula_probability_up for row in rows] == pytest.approx([0.9, 0.2])
    assert [row.formula_score_probability_up for row in rows] == pytest.approx([0.85, 0.2])


def test_load_outcomes_rejects_legacy_rows_without_formula_fields(
    tmp_path: Path,
) -> None:
    path = tmp_path / "outcomes.jsonl"
    path.write_text(
        json.dumps(
            {
                "schema_version": "signal-improvement-outcome-1.0",
                "example_id": "legacy",
                "remaining_abnormal_4h": 1.0,
            }
        )
        + "\n"
    )

    with pytest.raises(ValueError, match="schema or formula fields"):
        _load_outcomes(path)


def test_contract_locks_config6_formula_comparison_parity() -> None:
    contract = json.loads(
        (PROJECT_ROOT / "EventEdge/NEWS_MODEL_BENCHMARK_CONTRACT.json").read_text()
    )

    assert contract["schema_version"] == "news-model-benchmark-contract-3.0"
    assert contract["expected_output"]["formula_comparison"] == {
        "config_version": 6,
        "formula_confidence_roc_auc": 0.5236464448793215,
        "formula_hit_rate_pct": 50.955414012738856,
        "model_hit_rate_pct": 51.59235668789809,
        "model_id": "update_5m_corrected_reaction_interactions",
        "model_roc_auc": 0.4969015003261578,
        "paired_hit_rate_difference_95_pct": [
            -12.101910828025474,
            12.41594827586204,
        ],
        "rows": 157,
        "scope": "historical_stored_config_6_rows_only",
        "target": "remaining_abnormal_4h",
    }


def _formula_outcome(
    direction: str,
    confidence: float,
    score: float,
    target: float,
) -> dict[str, object]:
    return {
        "formula_config_version": 6,
        "formula_direction": direction,
        "formula_confidence": confidence,
        "formula_score": score,
        "remaining_abnormal_4h": target,
        "remaining_raw_4h": target + 0.1,
    }


def _write_scoring_contract(
    tmp_path: Path,
    outcomes: Path,
    *,
    probability_sha256: str | None = None,
) -> tuple[Path, Path]:
    probability_sha256 = probability_sha256 or "0" * 64
    contract = tmp_path / "contract.json"
    contract.write_text(
        json.dumps(
            {
                "schema_version": "news-model-benchmark-contract-3.0",
                "dataset": {"sha256": "1" * 64},
                "expected_output": {
                    "outcomes_sha256": signal_dataset_sha256(outcomes),
                    "frozen_probabilities_sha256": probability_sha256,
                    "formula_comparison": {},
                },
            }
        )
    )
    seal = tmp_path / "scoring-seal.json"
    seal.write_text(
        json.dumps(
            {
                "schema_version": "news-model-scoring-seal-3.0",
                "fit_bundle_sha256": None,
                "contract_sha256": signal_dataset_sha256(contract),
                "outcomes_path": outcomes.name,
                "outcomes_sha256": signal_dataset_sha256(outcomes),
                "formula_comparison": {},
            }
        )
    )
    return seal, contract
