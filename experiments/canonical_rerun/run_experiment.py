"""Rerun all five archived experiments under the canonical data contract."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from experiments.canonical_rerun.protocol import (
    CANONICAL_DATASET_SHA256,
    HORIZONS_MINUTES,
    CanonicalPaths,
    attach_remaining_direction,
    build_canonical_folds,
    build_remaining_labels,
    filter_direction_folds,
    rebuild_canonical_dataset,
    verify_formula_control,
)
from experiments.direction_stage3.run_experiment import (
    config6_horizon_summary,
    run_selective_binary,
    run_ternary,
)
from experiments.direction_stage4.build_structured_features import (
    EXTRACTOR_VERSION,
    _self_test_extractor,
    build_structured_features,
)
from experiments.direction_stage4.run_experiment import (
    PRIMARY_CHALLENGER as STAGE4_PRIMARY_CHALLENGER,
)
from experiments.direction_stage4.run_experiment import (
    _config6_comparison as stage4_config6_comparison,
)
from experiments.direction_stage4.run_experiment import (
    aggregate_metrics as aggregate_stage4,
)
from experiments.direction_stage4.run_experiment import (
    run_models as run_stage4_models,
)
from experiments.direction_stage5.run_experiment import (
    _config6_comparison as stage5_config6_comparison,
)
from experiments.direction_stage5.run_experiment import (
    _resource_summary,
)
from experiments.direction_stage5.run_experiment import (
    aggregate_metrics as aggregate_stage5,
)
from experiments.direction_stage5.run_experiment import (
    run_models as run_stage5_models,
)
from experiments.finbert_stage1.run_experiment import (
    MODEL_ID,
    MODEL_REVISION,
    _derive_sentiment_features,
    _direction_summary,
    _finbert_direction_full_population_summary,
    _materiality_summary,
    _model_text,
    load_dataset,
    sha256_file,
)
from experiments.finbert_stage1.run_experiment import (
    run_direction as run_stage1_direction,
)
from experiments.finbert_stage1.run_experiment import (
    run_materiality as run_stage1_materiality,
)
from experiments.market_stage2.run_experiment import (
    FEATURE_SETS,
    _fold_deltas,
    build_market_features,
)
from experiments.market_stage2.run_experiment import (
    _aggregate_metrics as aggregate_stage2,
)
from experiments.market_stage2.run_experiment import (
    _cluster_bootstrap as stage2_cluster_bootstrap,
)
from experiments.market_stage2.run_experiment import (
    _cluster_bootstrap_hit_rate as stage2_hit_rate_bootstrap,
)
from experiments.market_stage2.run_experiment import (
    _config6_comparison as stage2_config6_comparison,
)
from experiments.market_stage2.run_experiment import (
    run_models as run_stage2_models,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEGACY_DATASET = PROJECT_ROOT / ".local-data/news-direction-legacy/dataset.jsonl"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / ".local-artifacts/news-direction-canonical-rerun"
DEFAULT_MARKET_CACHE = (
    PROJECT_ROOT
    / ".local-artifacts/news-direction-legacy/market_stage2/cache/minute_candles.csv.gz"
)
DEFAULT_OUTCOME_CACHE = (
    PROJECT_ROOT
    / ".local-artifacts/news-direction-legacy/direction_stage3/cache/outcome_candles.csv.gz"
)
DEFAULT_SENTIMENT_CACHE = (
    PROJECT_ROOT / ".local-artifacts/news-direction-legacy/finbert_stage1/sentiment_predictions.csv"
)
BENCHMARK_CONTRACT = PROJECT_ROOT / "EventEdge/NEWS_MODEL_BENCHMARK_CONTRACT.json"
SENTIMENT_CACHE_SHA256 = "dbc2ff89b3cf6446485971c2cd2ca62a96cd4a7f5f14deb8721196f7f9875338"
EXPERIMENT_VERSION = "news-direction-canonical-rerun-1.0"


def run_all(paths: CanonicalPaths, output_root: Path, sentiment_cache: Path) -> dict[str, Any]:
    """Run the five ablations with one shared canonical frame and folds."""
    reconstruction = rebuild_canonical_dataset(paths)
    frame = load_dataset(paths.canonical_dataset)
    labels = build_remaining_labels(frame, paths.outcome_cache)
    frame = attach_remaining_direction(frame, labels)
    folds = build_canonical_folds(paths.canonical_dataset, paths.benchmark_contract)
    direction_folds = filter_direction_folds(frame, folds)
    formula_control = verify_formula_control(frame, folds)
    sentiment = _attach_frozen_sentiment(frame, sentiment_cache)

    output_root.mkdir(parents=True, exist_ok=True)
    labels_path = output_root / "remaining-labels.csv"
    labels.to_csv(labels_path, index=False, float_format="%.10g")

    stage1_dir = _stage_directory(output_root, "stage1")
    material1, folds1_material = run_stage1_materiality(sentiment, folds)
    direction1, folds1_direction = run_stage1_direction(sentiment, direction_folds, material1)
    _write_predictions(stage1_dir, material1, direction1, folds1_material, folds1_direction)
    stage1 = {
        "question": "Does frozen Russian financial sentiment add signal?",
        "model": {"id": MODEL_ID, "revision": MODEL_REVISION},
        "materiality": _materiality_summary(material1),
        "full_population_sentiment_direction": _finbert_direction_full_population_summary(
            material1
        ),
        "config6_population": _direction_summary(direction1),
    }

    market_cache = pd.read_csv(paths.market_cache, parse_dates=["begin_at"])
    market_features = build_market_features(frame, market_cache)
    market_path = output_root / "market-features.csv"
    market_features.to_csv(market_path, index=False, float_format="%.10g")
    market_frame = sentiment.merge(market_features, on="id", validate="one_to_one")

    stage2_dir = _stage_directory(output_root, "stage2")
    material2, folds2_material = run_stage2_models(
        market_frame,
        folds,
        target="materiality_label",
        selection_metric="pr_auc",
    )
    direction2, folds2_direction = run_stage2_models(
        market_frame,
        direction_folds,
        target="abnormal_direction_label",
        selection_metric="roc_auc",
    )
    _write_predictions(stage2_dir, material2, direction2, folds2_material, folds2_direction)
    stage2_comparisons = {
        challenger: {
            metric: stage2_cluster_bootstrap(
                material2,
                target="materiality_label",
                baseline="legacy",
                challenger=challenger,
                metric=metric,
            )
            for metric in ("pr_auc", "roc_auc")
        }
        for challenger in FEATURE_SETS
        if challenger != "legacy"
    }
    for challenger, comparison in stage2_comparisons.items():
        comparison["pr_auc_by_fold"] = _fold_deltas(
            material2,
            target="materiality_label",
            challenger=challenger,
            metric="pr_auc",
        )
    stage2 = {
        "question": "Do causal market features available by publication+5m add signal?",
        "materiality": aggregate_stage2(material2, target="materiality_label"),
        "materiality_paired_delta_vs_legacy": stage2_comparisons,
        "remaining_direction": aggregate_stage2(
            direction2,
            target="abnormal_direction_label",
        ),
        "remaining_direction_paired_delta_vs_reaction_core": {
            challenger: {
                "roc_auc": stage2_cluster_bootstrap(
                    direction2,
                    target="abnormal_direction_label",
                    baseline="reaction_core",
                    challenger=challenger,
                    metric="roc_auc",
                ),
                "accuracy": stage2_hit_rate_bootstrap(
                    direction2,
                    prediction_a="prediction_reaction_core",
                    prediction_b=f"prediction_{challenger}",
                ),
            }
            for challenger in FEATURE_SETS
            if challenger != "reaction_core"
        },
        "config6_population": stage2_config6_comparison(direction2),
    }

    stage3_frame = market_frame.copy()
    for horizon in HORIZONS_MINUTES:
        abnormal = stage3_frame[f"abnormal_return_{horizon}m_pct"]
        stage3_frame[f"direction_{horizon}m"] = np.where(
            abnormal.notna(),
            (abnormal > 0).astype(int),
            np.nan,
        )
    stage3_dir = _stage_directory(output_root, "stage3")
    selective_predictions, selective = run_selective_binary(stage3_frame, folds)
    ternary_predictions, ternary = run_ternary(stage3_frame, folds)
    selective_predictions.to_csv(
        stage3_dir / "selective-predictions.csv",
        index=False,
        float_format="%.10g",
    )
    ternary_predictions.to_csv(
        stage3_dir / "ternary-predictions.csv",
        index=False,
        float_format="%.10g",
    )
    stage3 = {
        "question": "Do alternate remaining horizons, abstention or neutral labels help?",
        "selective_binary": selective,
        "ternary": ternary,
        "config6_by_horizon": config6_horizon_summary(stage3_frame, folds),
        "passing_selective_configurations": _passing_selective(selective),
        "passing_ternary_configurations": _passing_ternary(ternary),
    }

    _self_test_extractor()
    structured_features = build_structured_features(frame, market_features)
    structured_path = output_root / "structured-features.csv"
    structured_features.to_csv(structured_path, index=False, float_format="%.10g")
    structured_frame = market_frame.merge(structured_features, on="id", validate="one_to_one")

    stage4_dir = _stage_directory(output_root, "stage4")
    direction4, folds4 = run_stage4_models(structured_frame, direction_folds)
    direction4.to_csv(
        stage4_dir / "direction-predictions.csv",
        index=False,
        float_format="%.10g",
    )
    folds4.to_csv(stage4_dir / "fold-metrics.csv", index=False, float_format="%.10g")
    metrics4, comparisons4 = aggregate_stage4(direction4, folds4)
    stage4 = {
        "question": "Do structured news features or TF-IDF improve remaining direction?",
        "extractor_version": EXTRACTOR_VERSION,
        "primary_challenger": STAGE4_PRIMARY_CHALLENGER,
        "metrics": metrics4,
        "paired_comparisons_vs_reaction_core": comparisons4,
        "config6_population": stage4_config6_comparison(direction4),
    }

    stage5_dir = _stage_directory(output_root, "stage5")
    direction5, folds5 = run_stage5_models(structured_frame, direction_folds)
    baselines = direction4[
        [
            "id",
            "probability_reaction_core",
            "prediction_reaction_core",
            "probability_structured_full",
            "prediction_structured_full",
        ]
    ].rename(
        columns={
            "probability_structured_full": "probability_structured_logistic",
            "prediction_structured_full": "prediction_structured_logistic",
        }
    )
    direction5 = direction5.merge(baselines, on="id", validate="one_to_one")
    direction5.to_csv(
        stage5_dir / "direction-predictions.csv",
        index=False,
        float_format="%.10g",
    )
    folds5.to_csv(stage5_dir / "fold-metrics.csv", index=False, float_format="%.10g")
    metrics5, comparisons5 = aggregate_stage5(direction5)
    stage5 = {
        "question": "Do small nonlinear tabular models improve remaining direction?",
        "metrics": metrics5,
        "paired_comparisons_vs_reaction_core": comparisons5,
        "config6_population": stage5_config6_comparison(direction5),
        "resources": _resource_summary(folds5),
    }

    result = {
        "schema_version": EXPERIMENT_VERSION,
        "status": "historical_development_rerun",
        "production_effect": "none",
        "dataset": {
            "sha256": CANONICAL_DATASET_SHA256,
            "rows": len(frame),
            "reconstruction": reconstruction,
        },
        "protocol": {
            "target": ("sign(abnormal return from publication+5m to publication+240m)"),
            "evaluation_rows": 480,
            "folds": [
                {
                    "name": fold.name,
                    "train_rows": len(fold.train_ids),
                    "validation_rows": len(fold.validation_ids),
                    "direction_train_rows": len(direction_fold.train_ids),
                    "direction_validation_rows": len(direction_fold.validation_ids),
                    "evaluation_rows": len(fold.test_ids),
                }
                for fold, direction_fold in zip(folds, direction_folds, strict=True)
            ],
            "validation_months": 2,
            "embargo_hours": 72,
            "group_key": "event_group_id",
            "evaluation_windows_are_reused_development_data": True,
            "evaluation_outcomes_used_for_selection": False,
        },
        "controls": {
            "formula_config6": formula_control,
            "expected_formula_hit_rate": 0.5095541401273885,
            "market_cache_sha256": sha256_file(paths.market_cache),
            "outcome_cache_sha256": sha256_file(paths.outcome_cache),
            "sentiment_cache_sha256": sha256_file(sentiment_cache),
            "remaining_labels_sha256": sha256_file(labels_path),
            "market_features_sha256": sha256_file(market_path),
            "structured_features_sha256": sha256_file(structured_path),
        },
        "stages": {
            "stage1_finbert": stage1,
            "stage2_market": stage2,
            "stage3_policy": stage3,
            "stage4_structure": stage4,
            "stage5_nonlinear": stage5,
        },
    }
    result_path = output_root / "results.json"
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def _attach_frozen_sentiment(frame: pd.DataFrame, cache_path: Path) -> pd.DataFrame:
    if sha256_file(cache_path) != SENTIMENT_CACHE_SHA256:
        raise ValueError("frozen sentiment cache differs from its expected SHA-256")
    cached = pd.read_csv(cache_path)
    if (
        len(cached) != len(frame)
        or cached["id"].duplicated().any()
        or set(cached["id"]) != set(frame["id"])
        or set(cached["model_id"]) != {MODEL_ID}
        or set(cached["model_revision"]) != {MODEL_REVISION}
    ):
        raise ValueError("frozen sentiment cache does not cover the canonical rows")
    expected = frame[["id"]].copy()
    expected["text_sha256"] = frame.apply(_model_text, axis=1).map(
        lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()
    )
    check = expected.merge(
        cached[["id", "text_sha256"]],
        on="id",
        suffixes=("_canonical", "_cached"),
        validate="one_to_one",
    )
    if not (check["text_sha256_canonical"] == check["text_sha256_cached"]).all():
        raise ValueError("canonical text differs from frozen sentiment inference input")
    columns = ["id", "sentiment_positive", "sentiment_neutral", "sentiment_negative"]
    return _derive_sentiment_features(frame.merge(cached[columns], on="id", validate="one_to_one"))


def _stage_directory(root: Path, stage: str) -> Path:
    path = root / stage
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_predictions(
    output: Path,
    material: pd.DataFrame,
    direction: pd.DataFrame,
    material_folds: pd.DataFrame,
    direction_folds: pd.DataFrame,
) -> None:
    material.to_csv(output / "materiality-predictions.csv", index=False, float_format="%.10g")
    direction.to_csv(output / "direction-predictions.csv", index=False, float_format="%.10g")
    pd.concat([material_folds, direction_folds], ignore_index=True).to_csv(
        output / "fold-metrics.csv",
        index=False,
        float_format="%.10g",
    )


def _passing_selective(values: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"horizon": horizon, "coverage": coverage, **metrics}
        for horizon, horizon_metrics in values.items()
        for coverage, metrics in horizon_metrics["coverage_points"].items()
        if metrics["passes_success_gate"]
    ]


def _passing_ternary(values: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"configuration": name, **metrics}
        for name, metrics in values.items()
        if metrics["passes_success_gate"]
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-dataset", type=Path, default=DEFAULT_LEGACY_DATASET)
    parser.add_argument("--market-cache", type=Path, default=DEFAULT_MARKET_CACHE)
    parser.add_argument("--outcome-cache", type=Path, default=DEFAULT_OUTCOME_CACHE)
    parser.add_argument("--sentiment-cache", type=Path, default=DEFAULT_SENTIMENT_CACHE)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    arguments = parser.parse_args()
    result = run_all(
        CanonicalPaths(
            legacy_dataset=arguments.legacy_dataset,
            market_cache=arguments.market_cache,
            outcome_cache=arguments.outcome_cache,
            benchmark_contract=BENCHMARK_CONTRACT,
            canonical_dataset=arguments.output_root / "dataset.jsonl",
        ),
        arguments.output_root,
        arguments.sentiment_cache,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
