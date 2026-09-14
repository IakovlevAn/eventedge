from __future__ import annotations

import argparse
import itertools
import json
import pickle
import random
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import ExtraTreesClassifier, HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
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
from sklearn.preprocessing import OneHotEncoder

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.direction_stage4.run_experiment import (  # noqa: E402
    _best_threshold,
    _cluster_bootstrap,
    _load_frame,
)
from experiments.finbert_stage1.run_experiment import (  # noqa: E402
    CATEGORICAL_BASE_FEATURES,
    NUMERIC_BASE_FEATURES,
    SEED,
    binary_metrics,
    build_folds,
    sha256_file,
)
from experiments.market_stage2.run_experiment import (  # noqa: E402
    REACTION_CORE_FEATURES,
)
from experiments.paths import (  # noqa: E402
    dataset_path_from_environment,
    stage_artifact_directory,
)

EXPERIMENT_VERSION = "direction-stage5-1.0"
BOOTSTRAP_ITERATIONS = 2_000
PRIMARY_CHALLENGER = "hist_gradient_boosting"
DEPLOY_MIN_HIT_RATE = 0.58
DEPLOY_MIN_POSITIVE_FOLDS = 5

TREE_NUMERIC_FEATURES = (
    tuple(NUMERIC_BASE_FEATURES)
    + tuple(REACTION_CORE_FEATURES)
    + (
        "comparison_signal",
        "comparison_strength_log",
        "percent_change_signal",
        "percent_change_strength_log",
        "specific_event_signal",
        "structured_signal",
        "has_consensus",
        "has_previous_period",
    )
)
TREE_CATEGORICAL_FEATURES = tuple(CATEGORICAL_BASE_FEATURES) + (
    "sector",
    "event_subtype",
    "structured_signal_bin",
    "market_regime",
    "relative_momentum_regime",
    "reaction_regime",
    "decision_session",
)

HISTOGRAM_GRID = tuple(
    {
        "learning_rate": 0.05,
        "max_iter": 150,
        "max_leaf_nodes": leaves,
        "min_samples_leaf": minimum_leaf,
        "l2_regularization": regularization,
        "class_weight": class_weight,
    }
    for leaves, minimum_leaf, regularization, class_weight in itertools.product(
        (7, 15),
        (20, 40),
        (1.0, 5.0),
        (None, "balanced"),
    )
)
EXTRA_TREES_GRID = tuple(
    {
        "n_estimators": 128,
        "max_depth": depth,
        "min_samples_leaf": minimum_leaf,
        "max_features": max_features,
        "class_weight": class_weight,
    }
    for depth, minimum_leaf, max_features, class_weight in itertools.product(
        (3, 5, 8),
        (5, 15),
        (0.5, 1.0),
        (None, "balanced"),
    )
)

EVALUATED_MODELS = (
    "reaction_core",
    "structured_logistic",
    "hist_gradient_boosting",
    "extra_trees",
    "nonlinear_blend",
)
FORBIDDEN_FEATURE_FRAGMENTS = (
    "return_4h",
    "label",
    "entry_at",
    "outcome",
    "observed_at",
    "target_at",
    "model_text",
)


def assert_feature_contract() -> None:
    selected = TREE_NUMERIC_FEATURES + TREE_CATEGORICAL_FEATURES
    violations = [
        feature
        for feature in selected
        if any(fragment in feature for fragment in FORBIDDEN_FEATURE_FRAGMENTS)
    ]
    if violations:
        raise AssertionError(f"forbidden stage-5 features: {violations}")
    if len(selected) != len(set(selected)):
        raise AssertionError("duplicate stage-5 feature names")


def _preprocessor() -> ColumnTransformer:
    return ColumnTransformer(
        [
            (
                "numeric",
                SimpleImputer(
                    strategy="median",
                    add_indicator=True,
                    keep_empty_features=True,
                ),
                list(TREE_NUMERIC_FEATURES),
            ),
            (
                "categorical",
                Pipeline(
                    [
                        ("imputer", SimpleImputer(strategy="most_frequent")),
                        (
                            "one_hot",
                            OneHotEncoder(
                                handle_unknown="ignore",
                                min_frequency=5,
                                sparse_output=False,
                            ),
                        ),
                    ]
                ),
                list(TREE_CATEGORICAL_FEATURES),
            ),
        ],
        sparse_threshold=0,
    )


def _make_model(family: str, parameters: dict[str, Any]) -> Pipeline:
    if family == "hist_gradient_boosting":
        estimator = HistGradientBoostingClassifier(
            **parameters,
            early_stopping=False,
            random_state=SEED,
        )
    elif family == "extra_trees":
        estimator = ExtraTreesClassifier(
            **parameters,
            bootstrap=False,
            n_jobs=1,
            random_state=SEED,
        )
    else:
        raise ValueError(f"unsupported model family: {family}")
    return Pipeline([("features", _preprocessor()), ("model", estimator)])


def _fit_selected_family(
    train: pd.DataFrame,
    validation: pd.DataFrame,
    *,
    family: str,
) -> tuple[Pipeline, dict[str, Any], np.ndarray, float]:
    grid = HISTOGRAM_GRID if family == "hist_gradient_boosting" else EXTRA_TREES_GRID
    best_score = float("-inf")
    best_model: Pipeline | None = None
    best_parameters: dict[str, Any] | None = None
    best_probability: np.ndarray | None = None
    for parameters in grid:
        random.seed(SEED)
        np.random.seed(SEED)
        candidate = _make_model(family, parameters)
        candidate.fit(train, train["abnormal_direction_label"])
        probability = candidate.predict_proba(validation)[:, 1]
        score = float(
            roc_auc_score(validation["abnormal_direction_label"], probability)
        )
        # Fixed grid order is the deterministic low-complexity tie-breaker.
        if score > best_score:
            best_score = score
            best_model = candidate
            best_parameters = dict(parameters)
            best_probability = probability
    if best_model is None or best_parameters is None or best_probability is None:
        raise AssertionError(f"no {family} candidate fitted")
    return best_model, best_parameters, best_probability, best_score


def _model_diagnostics(model: Pipeline) -> dict[str, int]:
    estimator = model.named_steps["model"]
    transformed_features = len(model.named_steps["features"].get_feature_names_out())
    result = {
        "serialized_size_bytes": len(pickle.dumps(model, protocol=5)),
        "transformed_features": transformed_features,
    }
    if isinstance(estimator, HistGradientBoostingClassifier):
        result["iterations"] = int(estimator.n_iter_)
    elif isinstance(estimator, ExtraTreesClassifier):
        result["trees"] = len(estimator.estimators_)
    return result


def run_models(
    frame: pd.DataFrame,
    folds: list[Any],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    outputs: list[pd.DataFrame] = []
    fold_rows: list[dict[str, Any]] = []
    for fold in folds:
        indexed = frame.set_index("id", drop=False)
        train = indexed.loc[list(fold.train_ids)].reset_index(drop=True)
        validation = indexed.loc[list(fold.validation_ids)].reset_index(drop=True)
        test = indexed.loc[list(fold.test_ids)].reset_index(drop=True)
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
        selected: dict[str, dict[str, Any]] = {}
        for family in ("hist_gradient_boosting", "extra_trees"):
            model, parameters, validation_probability, validation_auc = (
                _fit_selected_family(train, validation, family=family)
            )
            test_probability = model.predict_proba(test)[:, 1]
            threshold = _best_threshold(
                validation["abnormal_direction_label"].to_numpy(),
                validation_probability,
            )
            output[f"probability_{family}"] = test_probability
            output[f"threshold_{family}"] = threshold
            output[f"prediction_{family}"] = (test_probability >= threshold).astype(int)
            diagnostics = _model_diagnostics(model)
            selected[family] = {
                "validation_probability": validation_probability,
                "test_probability": test_probability,
                "diagnostics": diagnostics,
            }
            fold_rows.append(
                {
                    "fold": fold.name,
                    "model": family,
                    "train_n": len(train),
                    "validation_n": len(validation),
                    "test_n": len(test),
                    "validation_roc_auc": validation_auc,
                    "threshold": threshold,
                    "parameters": json.dumps(parameters, sort_keys=True),
                    **diagnostics,
                    "test_roc_auc": float(
                        roc_auc_score(test["abnormal_direction_label"], test_probability)
                    ),
                    "test_accuracy": float(
                        accuracy_score(
                            test["abnormal_direction_label"],
                            output[f"prediction_{family}"],
                        )
                    ),
                }
            )

        blend_validation = np.mean(
            [
                selected[family]["validation_probability"]
                for family in ("hist_gradient_boosting", "extra_trees")
            ],
            axis=0,
        )
        blend_test = np.mean(
            [
                selected[family]["test_probability"]
                for family in ("hist_gradient_boosting", "extra_trees")
            ],
            axis=0,
        )
        blend_threshold = _best_threshold(
            validation["abnormal_direction_label"].to_numpy(),
            blend_validation,
        )
        output["probability_nonlinear_blend"] = blend_test
        output["threshold_nonlinear_blend"] = blend_threshold
        output["prediction_nonlinear_blend"] = (
            blend_test >= blend_threshold
        ).astype(int)
        fold_rows.append(
            {
                "fold": fold.name,
                "model": "nonlinear_blend",
                "train_n": len(train),
                "validation_n": len(validation),
                "test_n": len(test),
                "validation_roc_auc": float(
                    roc_auc_score(
                        validation["abnormal_direction_label"],
                        blend_validation,
                    )
                ),
                "threshold": blend_threshold,
                "parameters": '{"weights": [0.5, 0.5]}',
                "serialized_size_bytes": sum(
                    selected[family]["diagnostics"]["serialized_size_bytes"]
                    for family in ("hist_gradient_boosting", "extra_trees")
                ),
                "transformed_features": max(
                    selected[family]["diagnostics"]["transformed_features"]
                    for family in ("hist_gradient_boosting", "extra_trees")
                ),
                "iterations": np.nan,
                "trees": 128,
                "test_roc_auc": float(
                    roc_auc_score(test["abnormal_direction_label"], blend_test)
                ),
                "test_accuracy": float(
                    accuracy_score(
                        test["abnormal_direction_label"],
                        output["prediction_nonlinear_blend"],
                    )
                ),
            }
        )
        outputs.append(output)
    predictions = pd.concat(outputs, ignore_index=True)
    if len(predictions) != 480 or predictions["id"].nunique() != 480:
        raise AssertionError("stage-5 evaluation must contain 480 unique rows")
    return predictions, pd.DataFrame(fold_rows)


def _attach_locked_baselines(predictions: pd.DataFrame) -> pd.DataFrame:
    stage4 = pd.read_csv(
        stage_artifact_directory("direction_stage4") / "direction_predictions.csv"
    )[
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
    merged = predictions.merge(stage4, on="id", validate="one_to_one")
    if len(merged) != 480:
        raise AssertionError("locked stage-4 baselines do not cover stage 5")
    return merged


def aggregate_metrics(predictions: pd.DataFrame) -> tuple[dict[str, Any], dict[str, Any]]:
    y = predictions["abnormal_direction_label"]
    metrics: dict[str, Any] = {}
    comparisons: dict[str, Any] = {}
    for model in EVALUATED_MODELS:
        probability_column = f"probability_{model}"
        prediction_column = f"prediction_{model}"
        probability = predictions[probability_column]
        predicted = predictions[prediction_column]
        fold_accuracy = predictions.groupby("fold", sort=True).apply(
            lambda group, column=prediction_column: accuracy_score(
                group["abnormal_direction_label"],
                group[column],
            ),
            include_groups=False,
        )
        summary = binary_metrics(y.to_numpy(), probability.to_numpy())
        summary.update(
            {
                "threshold": "selected_per_fold",
                "accuracy": float(accuracy_score(y, predicted)),
                "balanced_accuracy": float(balanced_accuracy_score(y, predicted)),
                "precision": float(precision_score(y, predicted, zero_division=0)),
                "recall": float(recall_score(y, predicted, zero_division=0)),
                "f1": float(f1_score(y, predicted, zero_division=0)),
                "confusion_matrix": confusion_matrix(y, predicted, labels=[0, 1]).tolist(),
                "positive_folds": int((fold_accuracy > 0.5).sum()),
                "fold_accuracy": {
                    str(fold): float(value) for fold, value in fold_accuracy.items()
                },
                "roc_auc_cluster_bootstrap": _cluster_bootstrap(
                    predictions,
                    probability_column=probability_column,
                    iterations=BOOTSTRAP_ITERATIONS,
                ),
                "accuracy_cluster_bootstrap": _cluster_bootstrap(
                    predictions,
                    prediction_column=prediction_column,
                    iterations=BOOTSTRAP_ITERATIONS,
                ),
            }
        )
        metrics[model] = summary
        if model == "reaction_core":
            continue
        comparisons[model] = {
            "roc_auc_delta": _cluster_bootstrap(
                predictions,
                probability_column=probability_column,
                baseline_probability="probability_reaction_core",
                iterations=BOOTSTRAP_ITERATIONS,
            ),
            "accuracy_delta": _cluster_bootstrap(
                predictions,
                prediction_column=prediction_column,
                baseline_prediction="prediction_reaction_core",
                iterations=BOOTSTRAP_ITERATIONS,
            ),
        }
    return metrics, comparisons


def _deployment_gate(
    metrics: dict[str, Any],
    comparisons: dict[str, Any],
    model: str,
) -> dict[str, Any]:
    model_metrics = metrics[model]
    checks = {
        "hit_rate_at_least_58pct": model_metrics["accuracy"] >= DEPLOY_MIN_HIT_RATE,
        "accuracy_cluster_ci_low_above_50pct": (
            model_metrics["accuracy_cluster_bootstrap"]["ci95_low"] > 0.5
        ),
        "at_least_5_of_7_positive_folds": (
            model_metrics["positive_folds"] >= DEPLOY_MIN_POSITIVE_FOLDS
        ),
        "roc_auc_delta_cluster_ci_low_above_zero": (
            comparisons[model]["roc_auc_delta"]["ci95_low"] > 0
        ),
    }
    return {"passed": all(checks.values()), "checks": checks}


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
    for model in EVALUATED_MODELS:
        prediction_column = f"prediction_{model}"
        result["models"][model] = {
            "hit_rate": float(accuracy_score(y, paired[prediction_column])),
            "delta_vs_config_6": _cluster_bootstrap(
                paired,
                prediction_column=prediction_column,
                baseline_prediction="prediction_config_6",
                iterations=BOOTSTRAP_ITERATIONS,
            ),
        }
    return result


def _resource_summary(fold_metrics: pd.DataFrame) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for model, group in fold_metrics.groupby("model", sort=True):
        result[str(model)] = {
            "selected_model_size_median_kib": float(
                group["serialized_size_bytes"].median() / 1024
            ),
            "selected_model_size_max_kib": float(
                group["serialized_size_bytes"].max() / 1024
            ),
            "transformed_features_median": float(group["transformed_features"].median()),
        }
    return result


def save_plot(metrics: dict[str, Any], output_path: Path) -> None:
    names = list(EVALUATED_MODELS)
    x = np.arange(len(names))
    width = 0.36
    figure, axis = plt.subplots(figsize=(10, 5))
    axis.bar(x - width / 2, [metrics[name]["roc_auc"] for name in names], width, label="ROC-AUC")
    axis.bar(x + width / 2, [metrics[name]["accuracy"] for name in names], width, label="Hit rate")
    axis.axhline(0.5, color="#6b7280", linestyle="--", linewidth=1)
    axis.axhline(DEPLOY_MIN_HIT_RATE, color="#dc2626", linestyle=":", linewidth=1)
    axis.set_ylim(0.4, 0.65)
    axis.set_xticks(x, names, rotation=18, ha="right")
    axis.set_title("Stage 5: small nonlinear tabular models")
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
    predictions = _attach_locked_baselines(predictions)
    predictions.to_csv(output_dir / "direction_predictions.csv", index=False, float_format="%.10g")
    fold_metrics.to_csv(output_dir / "fold_metrics.csv", index=False, float_format="%.10g")
    metrics, comparisons = aggregate_metrics(predictions)
    save_plot(metrics, output_dir / "model_comparison.png")

    gates = {
        model: _deployment_gate(metrics, comparisons, model)
        for model in ("hist_gradient_boosting", "extra_trees", "nonlinear_blend")
    }
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
            "stage4_predictions_sha256": sha256_file(
                stage_artifact_directory("direction_stage4") / "direction_predictions.csv"
            ),
        },
        "protocol": {
            "primary_target": "abnormal_direction_240m",
            "primary_challenger": PRIMARY_CHALLENGER,
            "diagnostic_challengers": ["extra_trees", "nonlinear_blend"],
            "locked_baselines": ["reaction_core", "structured_logistic"],
            "test_months": [fold.name for fold in folds],
            "validation_months": 2,
            "embargo_hours": 72,
            "group_key": "event_group_id",
            "model_selection_metric": "validation ROC-AUC",
            "threshold_selection_metric": "validation balanced accuracy",
            "numeric_features": list(TREE_NUMERIC_FEATURES),
            "categorical_features": list(TREE_CATEGORICAL_FEATURES),
            "histogram_grid": list(HISTOGRAM_GRID),
            "extra_trees_grid": list(EXTRA_TREES_GRID),
            "deployment_gate": {
                "minimum_hit_rate": DEPLOY_MIN_HIT_RATE,
                "accuracy_cluster_ci95_low_above": 0.5,
                "minimum_positive_folds": DEPLOY_MIN_POSITIVE_FOLDS,
                "roc_auc_delta_cluster_ci95_low_above": 0,
            },
        },
        "metrics": metrics,
        "reference_baselines": {
            "actual_up_rate": float(predictions["abnormal_direction_label"].mean()),
            "majority_class": "down",
            "majority_class_accuracy": float(
                1 - predictions["abnormal_direction_label"].mean()
            ),
            "majority_class_balanced_accuracy": 0.5,
        },
        "paired_comparisons_vs_reaction_core": comparisons,
        "deployment_gates": gates,
        "primary_deployment_gate": gates[PRIMARY_CHALLENGER],
        "config6_population": _config6_comparison(predictions),
        "resources": _resource_summary(fold_metrics),
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
    parser = argparse.ArgumentParser(description="Run EventEdge direction stage 5")
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
        default=stage_artifact_directory("direction_stage5"),
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
