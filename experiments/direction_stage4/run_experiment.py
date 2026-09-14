from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.direction_stage4.build_structured_features import (  # noqa: E402
    EXTRACTOR_VERSION,
    _model_text,
)
from experiments.finbert_stage1.run_experiment import (  # noqa: E402
    CATEGORICAL_BASE_FEATURES,
    NUMERIC_BASE_FEATURES,
    SEED,
    binary_metrics,
    build_folds,
    load_dataset,
    sha256_file,
)
from experiments.market_stage2.run_experiment import (  # noqa: E402
    REACTION_CORE_FEATURES,
)
from experiments.paths import (  # noqa: E402
    dataset_path_from_environment,
    stage_artifact_directory,
)

EXPERIMENT_VERSION = "direction-stage4-1.0"
C_GRID = (0.03, 0.1, 0.3, 1.0, 3.0)
CLASS_WEIGHT_GRID: tuple[str | None, ...] = (None, "balanced")
BOOTSTRAP_ITERATIONS = 2_000

DEPLOY_MIN_HIT_RATE = 0.58
DEPLOY_MIN_POSITIVE_FOLDS = 5

CONTEXT_CATEGORICAL = (
    "sector",
    "market_regime",
    "relative_momentum_regime",
    "reaction_regime",
    "decision_session",
)
NEWS_NUMERIC = (
    "text_char_count_log",
    "text_token_count_log",
    "number_count_log",
    "percent_token_count_log",
    "positive_marker_count_log",
    "negative_marker_count_log",
    "uncertainty_marker_count_log",
    "confirmation_marker_count_log",
    "lexical_signal",
    "comparison_count_log",
    "comparison_signal",
    "comparison_strength_log",
    "percent_change_count_log",
    "percent_change_signal",
    "percent_change_strength_log",
    "specific_event_signal",
    "structured_signal",
    "has_consensus",
    "has_previous_period",
    "has_negation",
)
NEWS_CATEGORICAL = (
    "event_subtype",
    "structured_signal_bin",
    "event_type_x_signal",
    "sector_x_signal",
)

FEATURE_SETS: dict[str, dict[str, Any]] = {
    "reaction_core": {
        "numeric": tuple(NUMERIC_BASE_FEATURES) + tuple(REACTION_CORE_FEATURES),
        "categorical": tuple(CATEGORICAL_BASE_FEATURES),
        "text": False,
    },
    "context": {
        "numeric": tuple(NUMERIC_BASE_FEATURES) + tuple(REACTION_CORE_FEATURES),
        "categorical": tuple(CATEGORICAL_BASE_FEATURES) + CONTEXT_CATEGORICAL,
        "text": False,
    },
    "news_structure": {
        "numeric": tuple(NUMERIC_BASE_FEATURES) + tuple(REACTION_CORE_FEATURES) + NEWS_NUMERIC,
        "categorical": tuple(CATEGORICAL_BASE_FEATURES) + NEWS_CATEGORICAL,
        "text": False,
    },
    "structured_full": {
        "numeric": tuple(NUMERIC_BASE_FEATURES) + tuple(REACTION_CORE_FEATURES) + NEWS_NUMERIC,
        "categorical": (
            tuple(CATEGORICAL_BASE_FEATURES) + CONTEXT_CATEGORICAL + NEWS_CATEGORICAL
        ),
        "text": False,
    },
    "structured_full_tfidf": {
        "numeric": tuple(NUMERIC_BASE_FEATURES) + tuple(REACTION_CORE_FEATURES) + NEWS_NUMERIC,
        "categorical": (
            tuple(CATEGORICAL_BASE_FEATURES) + CONTEXT_CATEGORICAL + NEWS_CATEGORICAL
        ),
        "text": True,
    },
}

PRIMARY_CHALLENGER = "structured_full"
DIAGNOSTIC_TEXT_CHALLENGER = "structured_full_tfidf"
FORBIDDEN_FEATURE_FRAGMENTS = (
    "return_4h",
    "label",
    "entry_at",
    "outcome",
    "observed_at",
    "target_at",
)


def assert_feature_contract() -> None:
    for name, specification in FEATURE_SETS.items():
        selected = list(specification["numeric"]) + list(specification["categorical"])
        violations = [
            feature
            for feature in selected
            if any(fragment in feature for fragment in FORBIDDEN_FEATURE_FRAGMENTS)
        ]
        if violations:
            raise AssertionError(f"forbidden stage-4 features in {name}: {violations}")
    baseline = FEATURE_SETS["reaction_core"]
    for challenger in ("context", "news_structure", "structured_full"):
        candidate = FEATURE_SETS[challenger]
        if not set(baseline["numeric"]).issubset(candidate["numeric"]):
            raise AssertionError(f"numeric baseline missing from {challenger}")
        if not set(baseline["categorical"]).issubset(candidate["categorical"]):
            raise AssertionError(f"categorical baseline missing from {challenger}")


def _load_frame(
    dataset_path: Path,
    market_features_path: Path,
    structured_features_path: Path,
) -> pd.DataFrame:
    frame = load_dataset(dataset_path)
    market = pd.read_csv(market_features_path)
    structured = pd.read_csv(structured_features_path)
    if structured["id"].duplicated().any() or len(structured) != len(frame):
        raise AssertionError("invalid structured feature row mapping")
    expected_hashes = frame.apply(_model_text, axis=1).map(
        lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest()
    )
    observed_hashes = frame[["id"]].copy()
    observed_hashes["expected"] = expected_hashes
    observed_hashes = observed_hashes.merge(
        structured[["id", "text_sha256"]],
        on="id",
        validate="one_to_one",
    )
    if not (observed_hashes["expected"] == observed_hashes["text_sha256"]).all():
        raise AssertionError("structured feature text hash mismatch")
    frame = frame.merge(market, on="id", validate="one_to_one")
    frame = frame.merge(structured, on="id", validate="one_to_one")
    if frame[list(REACTION_CORE_FEATURES)].isna().all(axis=None):
        raise AssertionError("reaction-core market features are unavailable")
    return frame


def make_pipeline(feature_set: str, *, c: float, class_weight: str | None) -> Pipeline:
    specification = FEATURE_SETS[feature_set]
    transformers: list[tuple[str, Any, Any]] = [
        (
            "numeric",
            Pipeline(
                [
                    (
                        "imputer",
                        SimpleImputer(
                            strategy="median",
                            add_indicator=True,
                            keep_empty_features=True,
                        ),
                    ),
                    ("scale", StandardScaler()),
                ]
            ),
            list(specification["numeric"]),
        ),
        (
            "categorical",
            Pipeline(
                [
                    ("imputer", SimpleImputer(strategy="most_frequent")),
                    (
                        "one_hot",
                        OneHotEncoder(handle_unknown="ignore", min_frequency=2),
                    ),
                ]
            ),
            list(specification["categorical"]),
        ),
    ]
    if specification["text"]:
        transformers.append(
            (
                "text",
                TfidfVectorizer(
                    ngram_range=(1, 2),
                    min_df=3,
                    max_df=0.98,
                    max_features=4_000,
                    sublinear_tf=True,
                    token_pattern=r"(?u)\b[\w-]{2,}\b",
                ),
                "model_text",
            )
        )
    return Pipeline(
        [
            ("features", ColumnTransformer(transformers)),
            (
                "model",
                LogisticRegression(
                    C=c,
                    class_weight=class_weight,
                    max_iter=5_000,
                    random_state=SEED,
                ),
            ),
        ]
    )


def _best_threshold(y_true: np.ndarray, probability: np.ndarray) -> float:
    best_score = (float("-inf"), float("-inf"), float("-inf"))
    best_threshold = 0.5
    for threshold in np.unique(np.concatenate(([0.05, 0.95], probability))):
        predicted = (probability >= threshold).astype(int)
        score = (
            balanced_accuracy_score(y_true, predicted),
            accuracy_score(y_true, predicted),
            -abs(float(threshold) - 0.5),
        )
        if score > best_score:
            best_score = score
            best_threshold = float(threshold)
    return best_threshold


def _fit_selected(
    train: pd.DataFrame,
    validation: pd.DataFrame,
    *,
    feature_set: str,
) -> tuple[Pipeline, dict[str, Any], float]:
    best: tuple[float, float, str | None, Pipeline, np.ndarray] | None = None
    for c in C_GRID:
        for class_weight in CLASS_WEIGHT_GRID:
            random.seed(SEED)
            np.random.seed(SEED)
            model = make_pipeline(feature_set, c=c, class_weight=class_weight)
            model.fit(train, train["abnormal_direction_label"])
            probability = model.predict_proba(validation)[:, 1]
            score = roc_auc_score(validation["abnormal_direction_label"], probability)
            candidate = (float(score), -c, class_weight, model, probability)
            if best is None or candidate[:2] > best[:2]:
                best = candidate
    if best is None:
        raise AssertionError("no model candidate fitted")
    _, negative_c, class_weight, model, validation_probability = best
    threshold = _best_threshold(
        validation["abnormal_direction_label"].to_numpy(),
        validation_probability,
    )
    return model, {"C": -negative_c, "class_weight": class_weight}, threshold


def _rows(frame: pd.DataFrame, ids: tuple[str, ...]) -> pd.DataFrame:
    return frame.set_index("id", drop=False).loc[list(ids)].reset_index(drop=True)


def run_models(
    frame: pd.DataFrame,
    folds: list[Any],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    prediction_rows: list[pd.DataFrame] = []
    fold_rows: list[dict[str, Any]] = []
    for fold in folds:
        train = _rows(frame, fold.train_ids)
        validation = _rows(frame, fold.validation_ids)
        test = _rows(frame, fold.test_ids)
        output = test[
            [
                "id",
                "event_group_id",
                "published_at",
                "ticker",
                "event_type",
                "event_subtype",
                "sector",
                "structured_signal",
                "structured_signal_bin",
                "materiality_label",
                "abnormal_direction_label",
                "rule_direction",
            ]
        ].copy()
        output["fold"] = fold.name
        for feature_set in FEATURE_SETS:
            model, selected, threshold = _fit_selected(
                train,
                validation,
                feature_set=feature_set,
            )
            probability = model.predict_proba(test)[:, 1]
            prediction = (probability >= threshold).astype(int)
            output[f"probability_{feature_set}"] = probability
            output[f"threshold_{feature_set}"] = threshold
            output[f"prediction_{feature_set}"] = prediction
            metrics = binary_metrics(
                test["abnormal_direction_label"].to_numpy(),
                probability,
                threshold=threshold,
            )
            fold_rows.append(
                {
                    "fold": fold.name,
                    "feature_set": feature_set,
                    "train_n": len(train),
                    "validation_n": len(validation),
                    "test_n": len(test),
                    "selected_C": selected["C"],
                    "selected_class_weight": selected["class_weight"],
                    **{key: value for key, value in metrics.items() if key != "confusion_matrix"},
                }
            )
        prediction_rows.append(output)
    predictions = pd.concat(prediction_rows, ignore_index=True)
    if len(predictions) != 480 or predictions["id"].nunique() != 480:
        raise AssertionError("stage-4 evaluation must contain 480 unique rows")
    return predictions, pd.DataFrame(fold_rows)


def _cluster_bootstrap(
    predictions: pd.DataFrame,
    *,
    probability_column: str | None = None,
    prediction_column: str | None = None,
    baseline_probability: str | None = None,
    baseline_prediction: str | None = None,
    iterations: int = BOOTSTRAP_ITERATIONS,
) -> dict[str, float | int]:
    if (probability_column is None) == (prediction_column is None):
        raise ValueError("select exactly one metric type")
    required_columns = ["abnormal_direction_label"]
    if probability_column is not None:
        required_columns.append(probability_column)
    if prediction_column is not None:
        required_columns.append(prediction_column)
    if baseline_probability is not None:
        required_columns.append(baseline_probability)
    if baseline_prediction is not None:
        required_columns.append(baseline_prediction)
    groups = [
        group[required_columns].to_numpy()
        for _, group in predictions.groupby("event_group_id", sort=True)
    ]
    column_index = {name: index for index, name in enumerate(required_columns)}
    rng = np.random.default_rng(SEED)

    def score(sample: np.ndarray) -> float:
        y = sample[:, column_index["abnormal_direction_label"]].astype(int)
        if probability_column is not None:
            value = roc_auc_score(y, sample[:, column_index[probability_column]])
            if baseline_probability is not None:
                value -= roc_auc_score(y, sample[:, column_index[baseline_probability]])
            return float(value)
        if prediction_column is None:
            raise AssertionError("prediction column is unavailable")
        value = accuracy_score(y, sample[:, column_index[prediction_column]])
        if baseline_prediction is not None:
            value -= accuracy_score(y, sample[:, column_index[baseline_prediction]])
        return float(value)

    values: list[float] = []
    for _ in range(iterations):
        sampled = rng.choice(len(groups), size=len(groups), replace=True)
        sample = np.concatenate([groups[index] for index in sampled], axis=0)
        if probability_column is not None and len(np.unique(sample[:, 0])) != 2:
            continue
        values.append(score(sample))
    low, high = np.percentile(values, [2.5, 97.5])
    full_sample = predictions[required_columns].to_numpy()
    return {
        "estimate": score(full_sample),
        "ci95_low": float(low),
        "ci95_high": float(high),
        "iterations": len(values),
    }


def aggregate_metrics(
    predictions: pd.DataFrame,
    fold_metrics: pd.DataFrame,
) -> tuple[dict[str, Any], dict[str, Any]]:
    result: dict[str, Any] = {}
    comparisons: dict[str, Any] = {}
    y = predictions["abnormal_direction_label"]
    for feature_set in FEATURE_SETS:
        probability_column = f"probability_{feature_set}"
        prediction_column = f"prediction_{feature_set}"
        probability = predictions[probability_column]
        predicted = predictions[prediction_column]
        positive_folds = int(
            (
                fold_metrics.loc[
                    fold_metrics["feature_set"] == feature_set,
                    "accuracy",
                ]
                > 0.5
            ).sum()
        )
        metrics = binary_metrics(y.to_numpy(), probability.to_numpy())
        metrics.update(
            {
                "threshold": "selected_per_fold",
                "accuracy": float(accuracy_score(y, predicted)),
                "balanced_accuracy": float(balanced_accuracy_score(y, predicted)),
                "precision": float(precision_score(y, predicted, zero_division=0)),
                "recall": float(recall_score(y, predicted, zero_division=0)),
                "f1": float(f1_score(y, predicted, zero_division=0)),
                "confusion_matrix": confusion_matrix(y, predicted, labels=[0, 1]).tolist(),
                "positive_folds": positive_folds,
                "roc_auc_cluster_bootstrap": _cluster_bootstrap(
                    predictions,
                    probability_column=probability_column,
                ),
                "accuracy_cluster_bootstrap": _cluster_bootstrap(
                    predictions,
                    prediction_column=prediction_column,
                ),
            }
        )
        result[feature_set] = metrics
        if feature_set == "reaction_core":
            continue
        comparisons[feature_set] = {
            "roc_auc_delta": _cluster_bootstrap(
                predictions,
                probability_column=probability_column,
                baseline_probability="probability_reaction_core",
            ),
            "accuracy_delta": _cluster_bootstrap(
                predictions,
                prediction_column=prediction_column,
                baseline_prediction="prediction_reaction_core",
            ),
            "roc_auc_delta_by_fold": [
                {
                    "fold": fold,
                    "baseline": float(
                        roc_auc_score(
                            group["abnormal_direction_label"],
                            group["probability_reaction_core"],
                        )
                    ),
                    "challenger": float(
                        roc_auc_score(group["abnormal_direction_label"], group[probability_column])
                    ),
                }
                for fold, group in predictions.groupby("fold", sort=True)
            ],
        }
        for row in comparisons[feature_set]["roc_auc_delta_by_fold"]:
            row["delta"] = row["challenger"] - row["baseline"]
    return result, comparisons


def _config6_comparison(predictions: pd.DataFrame) -> dict[str, Any]:
    paired = predictions[predictions["rule_direction"].isin(["up", "down"])].copy()
    if len(paired) != 157:
        raise AssertionError(f"expected 157 config-6 rows, got {len(paired)}")
    paired["prediction_config_6"] = (paired["rule_direction"] == "up").astype(int)
    y = paired["abnormal_direction_label"]
    result: dict[str, Any] = {
        "n": len(paired),
        "config_6_hit_rate": float(accuracy_score(y, paired["prediction_config_6"])),
        "models": {},
    }
    for feature_set in FEATURE_SETS:
        prediction_column = f"prediction_{feature_set}"
        result["models"][feature_set] = {
            "hit_rate": float(accuracy_score(y, paired[prediction_column])),
            "delta_vs_config_6": _cluster_bootstrap(
                paired,
                prediction_column=prediction_column,
                baseline_prediction="prediction_config_6",
            ),
        }
    return result


def _deployment_gate(
    aggregate: dict[str, Any],
    comparisons: dict[str, Any],
    feature_set: str,
) -> dict[str, Any]:
    metrics = aggregate[feature_set]
    delta = comparisons[feature_set]["roc_auc_delta"]
    checks = {
        "hit_rate_at_least_58pct": metrics["accuracy"] >= DEPLOY_MIN_HIT_RATE,
        "accuracy_cluster_ci_low_above_50pct": (
            metrics["accuracy_cluster_bootstrap"]["ci95_low"] > 0.5
        ),
        "at_least_5_of_7_positive_folds": (
            metrics["positive_folds"] >= DEPLOY_MIN_POSITIVE_FOLDS
        ),
        "roc_auc_delta_cluster_ci_low_above_zero": delta["ci95_low"] > 0,
    }
    return {"passed": all(checks.values()), "checks": checks}


def save_plot(metrics: dict[str, Any], output_path: Path) -> None:
    names = list(FEATURE_SETS)
    auc = [metrics[name]["roc_auc"] for name in names]
    hit = [metrics[name]["accuracy"] for name in names]
    x = np.arange(len(names))
    figure, axis = plt.subplots(figsize=(10, 5))
    width = 0.36
    axis.bar(x - width / 2, auc, width, label="ROC-AUC")
    axis.bar(x + width / 2, hit, width, label="Hit rate")
    axis.axhline(0.5, color="#6b7280", linestyle="--", linewidth=1)
    axis.axhline(DEPLOY_MIN_HIT_RATE, color="#dc2626", linestyle=":", linewidth=1)
    axis.set_ylim(0.4, 0.65)
    axis.set_xticks(x, names, rotation=18, ha="right")
    axis.set_title("Stage 4: structured direction features")
    axis.legend()
    figure.tight_layout()
    figure.savefig(output_path, dpi=160)
    plt.close(figure)


def run_experiment(
    dataset_path: Path,
    output_dir: Path,
    *,
    market_features_path: Path,
    structured_features_path: Path,
) -> dict[str, Any]:
    assert_feature_contract()
    output_dir.mkdir(parents=True, exist_ok=True)
    frame = _load_frame(dataset_path, market_features_path, structured_features_path)
    folds = build_folds(frame)
    predictions, fold_metrics = run_models(frame, folds)

    # A clean-room copy of stage 2's reaction_core must reproduce exactly.
    stage2_predictions = pd.read_csv(
        stage_artifact_directory("market_stage2") / "direction_predictions.csv"
    )[["id", "probability_reaction_core", "prediction_reaction_core"]]
    baseline_check = predictions[
        ["id", "probability_reaction_core", "prediction_reaction_core"]
    ].merge(stage2_predictions, on="id", suffixes=("_stage4", "_stage2"), validate="one_to_one")
    max_probability_error = float(
        (
            baseline_check["probability_reaction_core_stage4"]
            - baseline_check["probability_reaction_core_stage2"]
        )
        .abs()
        .max()
    )
    prediction_matches = int(
        (
            baseline_check["prediction_reaction_core_stage4"]
            == baseline_check["prediction_reaction_core_stage2"]
        ).sum()
    )
    # Stage-2 probabilities came from a previous fit and were serialized with
    # 10 significant digits; tolerate only immaterial numerical solver drift.
    if max_probability_error > 1e-6 or prediction_matches != 480:
        raise AssertionError(
            "stage-4 reaction_core does not reproduce stage 2: "
            f"max_probability_error={max_probability_error}, "
            f"prediction_matches={prediction_matches}/480"
        )

    predictions.to_csv(output_dir / "direction_predictions.csv", index=False, float_format="%.10g")
    fold_metrics.to_csv(output_dir / "fold_metrics.csv", index=False, float_format="%.10g")
    aggregate, comparisons = aggregate_metrics(predictions, fold_metrics)
    save_plot(aggregate, output_dir / "model_comparison.png")

    result: dict[str, Any] = {
        "experiment": EXPERIMENT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "seed": SEED,
        "dataset": {
            "path": str(dataset_path),
            "sha256": sha256_file(dataset_path),
            "rows": len(frame),
        },
        "inputs": {
            "market_features_sha256": sha256_file(market_features_path),
            "structured_features_sha256": sha256_file(structured_features_path),
            "extractor_version": EXTRACTOR_VERSION,
        },
        "protocol": {
            "primary_target": "abnormal_direction_240m",
            "primary_challenger": PRIMARY_CHALLENGER,
            "diagnostic_text_challenger": DIAGNOSTIC_TEXT_CHALLENGER,
            "test_months": [fold.name for fold in folds],
            "validation_months": 2,
            "embargo_hours": 72,
            "group_key": "event_group_id",
            "model_selection_metric": "validation ROC-AUC",
            "threshold_selection_metric": "validation balanced accuracy",
            "feature_sets": FEATURE_SETS,
            "tfidf": {
                "ngram_range": [1, 2],
                "min_df": 3,
                "max_df": 0.98,
                "max_features": 4_000,
            },
            "deployment_gate": {
                "minimum_hit_rate": DEPLOY_MIN_HIT_RATE,
                "accuracy_cluster_ci95_low_above": 0.5,
                "minimum_positive_folds": DEPLOY_MIN_POSITIVE_FOLDS,
                "roc_auc_delta_cluster_ci95_low_above": 0,
            },
        },
        "baseline_reproduction": {
            "stage2_probability_max_abs_error": max_probability_error,
            "stage2_prediction_matches": prediction_matches,
        },
        "metrics": aggregate,
        "paired_comparisons_vs_reaction_core": comparisons,
        "primary_deployment_gate": _deployment_gate(
            aggregate,
            comparisons,
            PRIMARY_CHALLENGER,
        ),
        "tfidf_deployment_gate": _deployment_gate(
            aggregate,
            comparisons,
            DIAGNOSTIC_TEXT_CHALLENGER,
        ),
        "config6_population": _config6_comparison(predictions),
        "feature_diagnostics": {
            "evaluation_rows": len(predictions),
            "structured_signal_nonzero_rate": float(
                (predictions["structured_signal"] != 0).mean()
            ),
            "event_subtype_counts": (
                predictions["event_subtype"].value_counts().sort_index().to_dict()
            ),
            "sector_counts": predictions["sector"].value_counts().sort_index().to_dict(),
            "direct_structured_signal_roc_auc": float(
                roc_auc_score(
                    predictions["abnormal_direction_label"],
                    predictions["structured_signal"],
                )
            ),
            "structured_signal_calibration": {
                str(signal_bin): {
                    "n": len(group),
                    "actual_up_rate": float(group["abnormal_direction_label"].mean()),
                }
                for signal_bin, group in predictions.groupby(
                    "structured_signal_bin",
                    sort=True,
                )
            },
        },
        "artifact_hashes": {},
    }
    for filename in ("direction_predictions.csv", "fold_metrics.csv", "model_comparison.png"):
        result["artifact_hashes"][filename] = sha256_file(output_dir / filename)
    (output_dir / "metrics.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run EventEdge direction stage 4")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=dataset_path_from_environment(),
    )
    parser.add_argument(
        "--market-features",
        type=Path,
        default=stage_artifact_directory("market_stage2") / "market_features.csv",
    )
    parser.add_argument(
        "--structured-features",
        type=Path,
        default=stage_artifact_directory("direction_stage4") / "structured_features.csv",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=stage_artifact_directory("direction_stage4"),
    )
    arguments = parser.parse_args()
    result = run_experiment(
        arguments.dataset,
        arguments.output_dir,
        market_features_path=arguments.market_features,
        structured_features_path=arguments.structured_features,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
