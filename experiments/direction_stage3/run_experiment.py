from __future__ import annotations

import argparse
import json
import math
import random
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    f1_score,
    roc_auc_score,
)

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.direction_stage3.build_label_cache import (  # noqa: E402
    HORIZONS_MINUTES,
)
from experiments.finbert_stage1.run_experiment import (  # noqa: E402
    SEED,
    build_folds,
    load_dataset,
    sha256_file,
)
from experiments.market_stage2.run_experiment import (  # noqa: E402
    _fit_selected,
    make_pipeline,
)
from experiments.paths import (  # noqa: E402
    dataset_path_from_environment,
    stage_artifact_directory,
)

EXPERIMENT_VERSION = "direction-stage3-1.0"
PRIMARY_FEATURE_SET = "reaction_core"
TARGET_COVERAGES = (0.2, 0.3, 0.5, 1.0)
NEUTRAL_THRESHOLDS_PCT = (0.1, 0.25, 0.5)
BOOTSTRAP_ITERATIONS = 2_000
SUCCESS_MIN_HIT_RATE = 0.58
SUCCESS_MIN_COVERAGE = 0.25
SUCCESS_MIN_POSITIVE_FOLDS = 5
C_GRID = (0.03, 0.1, 0.3, 1.0, 3.0)
CLASS_WEIGHT_GRID: tuple[str | None, ...] = (None, "balanced")


def _wilson_interval(successes: int, total: int) -> tuple[float | None, float | None]:
    if total == 0:
        return None, None
    z = 1.959963984540054
    rate = successes / total
    denominator = 1 + z**2 / total
    center = (rate + z**2 / (2 * total)) / denominator
    half = z * math.sqrt(rate * (1 - rate) / total + z**2 / (4 * total**2)) / denominator
    return center - half, center + half


def _normalized_margin(probability: np.ndarray, threshold: float) -> np.ndarray:
    below_scale = max(threshold, 1e-9)
    above_scale = max(1 - threshold, 1e-9)
    return np.where(
        probability < threshold,
        (threshold - probability) / below_scale,
        (probability - threshold) / above_scale,
    )


def _coverage_threshold(margin: np.ndarray, target_coverage: float) -> float:
    if target_coverage >= 1:
        return -1.0
    accept_n = max(1, math.ceil(len(margin) * target_coverage))
    return float(np.sort(margin)[-accept_n])


def _cluster_bootstrap_accuracy(
    predictions: pd.DataFrame,
    *,
    accepted_column: str,
    iterations: int = BOOTSTRAP_ITERATIONS,
) -> dict[str, float | int | None]:
    groups = {key: group for key, group in predictions.groupby("event_group_id")}
    keys = np.array(list(groups), dtype=object)
    rng = np.random.default_rng(SEED)
    estimates: list[float] = []
    for _ in range(iterations):
        sampled = rng.choice(keys, size=len(keys), replace=True)
        sample = pd.concat([groups[key] for key in sampled], ignore_index=True)
        accepted = sample[sample[accepted_column]]
        if accepted.empty:
            continue
        estimates.append(
            float(accuracy_score(accepted["direction_label"], accepted["direction_prediction"]))
        )
    accepted = predictions[predictions[accepted_column]]
    if accepted.empty or not estimates:
        return {
            "estimate": None,
            "ci95_low": None,
            "ci95_high": None,
            "iterations": len(estimates),
        }
    low, high = np.percentile(estimates, [2.5, 97.5])
    return {
        "estimate": float(
            accuracy_score(accepted["direction_label"], accepted["direction_prediction"])
        ),
        "ci95_low": float(low),
        "ci95_high": float(high),
        "iterations": len(estimates),
    }


def _cluster_bootstrap_ternary_precision(
    predictions: pd.DataFrame,
    *,
    iterations: int = BOOTSTRAP_ITERATIONS,
) -> dict[str, float | int | None]:
    groups = {key: group for key, group in predictions.groupby("event_group_id")}
    keys = np.array(list(groups), dtype=object)
    rng = np.random.default_rng(SEED)
    estimates: list[float] = []
    for _ in range(iterations):
        sampled = rng.choice(keys, size=len(keys), replace=True)
        sample = pd.concat([groups[key] for key in sampled], ignore_index=True)
        directional = sample[sample["ternary_prediction"] != "neutral"]
        if directional.empty:
            continue
        estimates.append(
            float(
                accuracy_score(
                    directional["ternary_label"],
                    directional["ternary_prediction"],
                )
            )
        )
    directional = predictions[predictions["ternary_prediction"] != "neutral"]
    if directional.empty or not estimates:
        return {
            "estimate": None,
            "ci95_low": None,
            "ci95_high": None,
            "iterations": len(estimates),
        }
    low, high = np.percentile(estimates, [2.5, 97.5])
    return {
        "estimate": float(
            accuracy_score(
                directional["ternary_label"],
                directional["ternary_prediction"],
            )
        ),
        "ci95_low": float(low),
        "ci95_high": float(high),
        "iterations": len(estimates),
    }


def _load_frame(
    dataset_path: Path,
    market_features_path: Path,
    labels_path: Path,
) -> pd.DataFrame:
    frame = load_dataset(dataset_path)
    market = pd.read_csv(market_features_path)
    labels = pd.read_csv(
        labels_path,
        parse_dates=[
            "entry_at",
            *[f"label_available_at_{horizon}m" for horizon in HORIZONS_MINUTES],
        ],
    )
    frame = frame.merge(market, on="id", validate="one_to_one")
    label_columns = ["id"]
    for horizon in HORIZONS_MINUTES:
        label_columns.extend(
            [
                f"abnormal_return_{horizon}m_pct",
                f"label_available_at_{horizon}m",
                f"timely_{horizon}m",
            ]
        )
    frame = frame.merge(labels[label_columns], on="id", validate="one_to_one")
    for horizon in HORIZONS_MINUTES:
        abnormal = frame[f"abnormal_return_{horizon}m_pct"]
        frame[f"direction_{horizon}m"] = np.where(
            abnormal.notna(), (abnormal > 0).astype(int), np.nan
        )
    return frame


def run_selective_binary(
    frame: pd.DataFrame,
    folds: list[Any],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    prediction_rows: list[pd.DataFrame] = []
    for horizon in HORIZONS_MINUTES:
        target = f"direction_{horizon}m"
        label_available = f"label_available_at_{horizon}m"
        for fold in folds:
            train = frame.set_index("id").loc[list(fold.train_ids)].reset_index()
            validation = frame.set_index("id").loc[list(fold.validation_ids)].reset_index()
            test = frame.set_index("id").loc[list(fold.test_ids)].reset_index()
            train = train[train[target].notna()].copy()
            validation = validation[validation[target].notna()].copy()
            test = test[test[target].notna()].copy()
            if (train[label_available] > pd.Timestamp(fold.train_cutoff)).any():
                raise AssertionError(f"training label leakage at {horizon}m/{fold.name}")
            if (validation[label_available] > pd.Timestamp(fold.validation_cutoff)).any():
                raise AssertionError(f"validation label leakage at {horizon}m/{fold.name}")
            for partition in (train, validation, test):
                partition[target] = partition[target].astype(int)
            model, selected, direction_threshold = _fit_selected(
                train,
                validation,
                target=target,
                feature_set=PRIMARY_FEATURE_SET,
                selection_metric="roc_auc",
            )
            validation_probability = model.predict_proba(validation)[:, 1]
            test_probability = model.predict_proba(test)[:, 1]
            validation_margin = _normalized_margin(validation_probability, direction_threshold)
            test_margin = _normalized_margin(test_probability, direction_threshold)
            output = test[["id", "event_group_id", "published_at", target]].rename(
                columns={target: "direction_label"}
            )
            output["fold"] = fold.name
            output["horizon_minutes"] = horizon
            output["direction_probability"] = test_probability
            output["direction_threshold"] = direction_threshold
            output["direction_prediction"] = (test_probability >= direction_threshold).astype(int)
            output["confidence_margin"] = test_margin
            output["selected_C"] = selected["C"]
            output["selected_class_weight"] = selected["class_weight"]
            for coverage in TARGET_COVERAGES:
                threshold = _coverage_threshold(validation_margin, coverage)
                suffix = str(int(coverage * 100))
                output[f"acceptance_threshold_{suffix}"] = threshold
                output[f"accepted_{suffix}"] = test_margin >= threshold
            prediction_rows.append(output)
    predictions = pd.concat(prediction_rows, ignore_index=True)
    expected = 480 * len(HORIZONS_MINUTES)
    if len(predictions) != expected:
        raise AssertionError(f"expected {expected} binary predictions, got {len(predictions)}")

    summary: dict[str, Any] = {}
    for horizon in HORIZONS_MINUTES:
        horizon_rows = predictions[predictions["horizon_minutes"] == horizon]
        coverage_rows: dict[str, Any] = {}
        for coverage in TARGET_COVERAGES:
            suffix = str(int(coverage * 100))
            accepted_column = f"accepted_{suffix}"
            accepted = horizon_rows[horizon_rows[accepted_column]]
            successes = int((accepted["direction_label"] == accepted["direction_prediction"]).sum())
            low, high = _wilson_interval(successes, len(accepted))
            fold_metrics = []
            for fold, group in horizon_rows.groupby("fold", sort=True):
                fold_accepted = group[group[accepted_column]]
                fold_metrics.append(
                    {
                        "fold": fold,
                        "n": len(fold_accepted),
                        "coverage": float(len(fold_accepted) / len(group)),
                        "hit_rate": (
                            float(
                                accuracy_score(
                                    fold_accepted["direction_label"],
                                    fold_accepted["direction_prediction"],
                                )
                            )
                            if len(fold_accepted)
                            else None
                        ),
                    }
                )
            bootstrap = _cluster_bootstrap_accuracy(
                horizon_rows,
                accepted_column=accepted_column,
            )
            hit_rate = successes / len(accepted) if len(accepted) else None
            positive_folds = sum(
                row["hit_rate"] is not None and row["hit_rate"] > 0.5 for row in fold_metrics
            )
            passed = bool(
                hit_rate is not None
                and hit_rate >= SUCCESS_MIN_HIT_RATE
                and len(accepted) / len(horizon_rows) >= SUCCESS_MIN_COVERAGE
                and bootstrap["ci95_low"] is not None
                and bootstrap["ci95_low"] > 0.5
                and positive_folds >= SUCCESS_MIN_POSITIVE_FOLDS
            )
            coverage_rows[suffix] = {
                "target_coverage": coverage,
                "n": len(accepted),
                "actual_coverage": float(len(accepted) / len(horizon_rows)),
                "hit_rate": hit_rate,
                "balanced_accuracy": (
                    float(
                        balanced_accuracy_score(
                            accepted["direction_label"],
                            accepted["direction_prediction"],
                        )
                    )
                    if len(accepted) and accepted["direction_label"].nunique() == 2
                    else None
                ),
                "wilson95_low": low,
                "wilson95_high": high,
                "cluster_bootstrap": bootstrap,
                "positive_folds": positive_folds,
                "folds": fold_metrics,
                "passes_success_gate": passed,
            }
        summary[str(horizon)] = {
            "n": len(horizon_rows),
            "roc_auc": float(
                roc_auc_score(
                    horizon_rows["direction_label"],
                    horizon_rows["direction_probability"],
                )
            ),
            "coverage_points": coverage_rows,
        }
    return predictions, summary


def _ternary_target(abnormal: pd.Series, neutral_threshold: float) -> pd.Series:
    return pd.Series(
        np.select(
            [abnormal <= -neutral_threshold, abnormal >= neutral_threshold],
            ["down", "up"],
            default="neutral",
        ),
        index=abnormal.index,
    )


def _fit_ternary(
    train: pd.DataFrame,
    validation: pd.DataFrame,
    *,
    target: str,
) -> tuple[Any, dict[str, Any]]:
    best: tuple[float, float, str | None, Any] | None = None
    for c in C_GRID:
        for class_weight in CLASS_WEIGHT_GRID:
            random.seed(SEED)
            np.random.seed(SEED)
            model = make_pipeline(
                PRIMARY_FEATURE_SET,
                c=c,
                class_weight=class_weight,
            )
            model.fit(train, train[target])
            predicted = model.predict(validation)
            score = f1_score(validation[target], predicted, average="macro")
            candidate = (float(score), -c, class_weight, model)
            if best is None or candidate[:2] > best[:2]:
                best = candidate
    if best is None:
        raise AssertionError("no ternary model candidate fitted")
    _, negative_c, class_weight, model = best
    return model, {"C": -negative_c, "class_weight": class_weight}


def run_ternary(
    frame: pd.DataFrame,
    folds: list[Any],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    prediction_rows: list[pd.DataFrame] = []
    for horizon in HORIZONS_MINUTES:
        abnormal_column = f"abnormal_return_{horizon}m_pct"
        available_column = f"label_available_at_{horizon}m"
        for neutral_threshold in NEUTRAL_THRESHOLDS_PCT:
            target = f"ternary_{horizon}m_{neutral_threshold:g}"
            working = frame[frame[abnormal_column].notna()].copy()
            working[target] = _ternary_target(working[abnormal_column], neutral_threshold)
            indexed = working.set_index("id", drop=False)
            for fold in folds:
                train_ids = [row_id for row_id in fold.train_ids if row_id in indexed.index]
                validation_ids = [
                    row_id for row_id in fold.validation_ids if row_id in indexed.index
                ]
                test_ids = [row_id for row_id in fold.test_ids if row_id in indexed.index]
                train = indexed.loc[train_ids].reset_index(drop=True)
                validation = indexed.loc[validation_ids].reset_index(drop=True)
                test = indexed.loc[test_ids].reset_index(drop=True)
                if (train[available_column] > pd.Timestamp(fold.train_cutoff)).any():
                    raise AssertionError("ternary training label leakage")
                if (validation[available_column] > pd.Timestamp(fold.validation_cutoff)).any():
                    raise AssertionError("ternary validation label leakage")
                model, selected = _fit_ternary(train, validation, target=target)
                predicted = model.predict(test)
                probabilities = model.predict_proba(test)
                classes = list(model.named_steps["model"].classes_)
                output = test[["id", "event_group_id", "published_at", target]].rename(
                    columns={target: "ternary_label"}
                )
                output["fold"] = fold.name
                output["horizon_minutes"] = horizon
                output["neutral_threshold_pct"] = neutral_threshold
                output["ternary_prediction"] = predicted
                output["prediction_confidence"] = probabilities.max(axis=1)
                output["selected_C"] = selected["C"]
                output["selected_class_weight"] = selected["class_weight"]
                for class_name in ("down", "neutral", "up"):
                    output[f"probability_{class_name}"] = probabilities[
                        :, classes.index(class_name)
                    ]
                prediction_rows.append(output)
    predictions = pd.concat(prediction_rows, ignore_index=True)
    summary: dict[str, Any] = {}
    for (horizon, neutral_threshold), group in predictions.groupby(
        ["horizon_minutes", "neutral_threshold_pct"], sort=True
    ):
        directional = group[group["ternary_prediction"] != "neutral"]
        correct_directional = int(
            (directional["ternary_prediction"] == directional["ternary_label"]).sum()
        )
        low, high = _wilson_interval(correct_directional, len(directional))
        fold_rows = []
        for fold, fold_group in group.groupby("fold", sort=True):
            accepted = fold_group[fold_group["ternary_prediction"] != "neutral"]
            fold_rows.append(
                {
                    "fold": fold,
                    "n": len(accepted),
                    "directional_precision": (
                        float(
                            accuracy_score(
                                accepted["ternary_label"],
                                accepted["ternary_prediction"],
                            )
                        )
                        if len(accepted)
                        else None
                    ),
                }
            )
        positive_folds = sum(
            row["directional_precision"] is not None and row["directional_precision"] > 0.5
            for row in fold_rows
        )
        directional_precision = correct_directional / len(directional) if len(directional) else None
        bootstrap = _cluster_bootstrap_ternary_precision(group)
        key = f"{int(horizon)}m_deadzone_{neutral_threshold:g}"
        summary[key] = {
            "horizon_minutes": int(horizon),
            "neutral_threshold_pct": float(neutral_threshold),
            "n": len(group),
            "true_directional_rate": float((group["ternary_label"] != "neutral").mean()),
            "predicted_directional_n": len(directional),
            "predicted_directional_coverage": float(len(directional) / len(group)),
            "directional_precision": directional_precision,
            "directional_wilson95_low": low,
            "directional_wilson95_high": high,
            "directional_cluster_bootstrap": bootstrap,
            "overall_accuracy": float(
                accuracy_score(group["ternary_label"], group["ternary_prediction"])
            ),
            "macro_f1": float(
                f1_score(
                    group["ternary_label"],
                    group["ternary_prediction"],
                    average="macro",
                )
            ),
            "positive_folds": positive_folds,
            "folds": fold_rows,
            "passes_success_gate": bool(
                directional_precision is not None
                and directional_precision >= SUCCESS_MIN_HIT_RATE
                and len(directional) / len(group) >= SUCCESS_MIN_COVERAGE
                and bootstrap["ci95_low"] is not None
                and bootstrap["ci95_low"] > 0.5
                and positive_folds >= SUCCESS_MIN_POSITIVE_FOLDS
            ),
        }
    return predictions, summary


def config6_horizon_summary(
    frame: pd.DataFrame,
    folds: list[Any],
) -> dict[str, Any]:
    test_ids = {row_id for fold in folds for row_id in fold.test_ids}
    test = frame[frame["id"].isin(test_ids) & frame["rule_direction"].isin(["up", "down"])].copy()
    if len(test) != 157:
        raise AssertionError(f"expected 157 config-6 test rows, got {len(test)}")
    test["direction_prediction"] = (test["rule_direction"] == "up").astype(int)
    test["accepted"] = True
    result: dict[str, Any] = {}
    for horizon in HORIZONS_MINUTES:
        current = test.copy()
        current["direction_label"] = (current[f"abnormal_return_{horizon}m_pct"] > 0).astype(int)
        successes = int((current["direction_label"] == current["direction_prediction"]).sum())
        low, high = _wilson_interval(successes, len(current))
        fold_rows = []
        for fold, group in current.groupby(current["published_at"].dt.strftime("%Y-%m"), sort=True):
            fold_rows.append(
                {
                    "fold": fold,
                    "n": len(group),
                    "hit_rate": float(
                        accuracy_score(group["direction_label"], group["direction_prediction"])
                    ),
                }
            )
        result[str(horizon)] = {
            "n": len(current),
            "coverage_of_all_events": float(len(current) / len(test_ids)),
            "hit_rate": float(successes / len(current)),
            "balanced_accuracy": float(
                balanced_accuracy_score(current["direction_label"], current["direction_prediction"])
            ),
            "wilson95_low": low,
            "wilson95_high": high,
            "cluster_bootstrap": _cluster_bootstrap_accuracy(
                current,
                accepted_column="accepted",
            ),
            "positive_folds": sum(row["hit_rate"] > 0.5 for row in fold_rows),
            "folds": fold_rows,
        }
    return result


def save_risk_coverage_plot(summary: dict[str, Any], output_path: Path) -> None:
    figure, axis = plt.subplots(figsize=(8, 5))
    for horizon in HORIZONS_MINUTES:
        points = summary[str(horizon)]["coverage_points"]
        x = [points[str(int(value * 100))]["actual_coverage"] for value in TARGET_COVERAGES]
        y = [points[str(int(value * 100))]["hit_rate"] for value in TARGET_COVERAGES]
        axis.plot(x, y, marker="o", label=f"{horizon}m")
    axis.axhline(0.5, linestyle="--", color="#9ca3af", label="random")
    axis.axhline(
        SUCCESS_MIN_HIT_RATE,
        linestyle=":",
        color="#dc2626",
        label="success gate",
    )
    axis.set_xlabel("Actual coverage")
    axis.set_ylabel("Directional hit rate")
    axis.set_ylim(0.35, 0.7)
    axis.set_title("Selective direction: risk–coverage")
    axis.legend()
    figure.tight_layout()
    figure.savefig(output_path, dpi=160)
    plt.close(figure)


def run_experiment(
    dataset_path: Path,
    output_dir: Path,
    *,
    market_features_path: Path,
    labels_path: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    frame = _load_frame(dataset_path, market_features_path, labels_path)
    folds = build_folds(frame)
    binary_predictions, selective_summary = run_selective_binary(frame, folds)
    ternary_predictions, ternary_summary = run_ternary(frame, folds)
    binary_predictions.to_csv(
        output_dir / "selective_predictions.csv", index=False, float_format="%.10g"
    )
    ternary_predictions.to_csv(
        output_dir / "ternary_predictions.csv", index=False, float_format="%.10g"
    )
    save_risk_coverage_plot(
        selective_summary,
        output_dir / "risk_coverage.png",
    )
    passing_selective = [
        {"horizon": horizon, "coverage": coverage, **metrics}
        for horizon, horizon_metrics in selective_summary.items()
        for coverage, metrics in horizon_metrics["coverage_points"].items()
        if metrics["passes_success_gate"]
    ]
    passing_ternary = [
        {"configuration": key, **metrics}
        for key, metrics in ternary_summary.items()
        if metrics["passes_success_gate"]
    ]
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
            "horizon_labels_sha256": sha256_file(labels_path),
        },
        "protocol": {
            "feature_set": PRIMARY_FEATURE_SET,
            "horizons_minutes": list(HORIZONS_MINUTES),
            "target_coverages": list(TARGET_COVERAGES),
            "neutral_thresholds_pct": list(NEUTRAL_THRESHOLDS_PCT),
            "test_months": [fold.name for fold in folds],
            "validation_months": 2,
            "embargo_hours": 72,
            "group_key": "event_group_id",
            "acceptance_rule": (
                "Per-fold normalized probability-margin threshold is the validation "
                "quantile for the requested coverage; no test outcomes choose it."
            ),
            "success_gate": {
                "minimum_hit_rate": SUCCESS_MIN_HIT_RATE,
                "minimum_coverage": SUCCESS_MIN_COVERAGE,
                "cluster_bootstrap_ci95_low_above": 0.5,
                "minimum_positive_folds": SUCCESS_MIN_POSITIVE_FOLDS,
            },
        },
        "selective_binary": selective_summary,
        "ternary": ternary_summary,
        "config6_by_horizon": config6_horizon_summary(frame, folds),
        "passing_selective_configurations": passing_selective,
        "passing_ternary_configurations": passing_ternary,
        "artifact_hashes": {},
    }
    for filename in (
        "selective_predictions.csv",
        "ternary_predictions.csv",
        "risk_coverage.png",
    ):
        result["artifact_hashes"][filename] = sha256_file(output_dir / filename)
    (output_dir / "metrics.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run EventEdge direction stage 3")
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
        "--labels",
        type=Path,
        default=stage_artifact_directory("direction_stage3") / "horizon_labels.csv",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=stage_artifact_directory("direction_stage3"),
    )
    arguments = parser.parse_args()
    result = run_experiment(
        arguments.dataset,
        arguments.output_dir,
        market_features_path=arguments.market_features,
        labels_path=arguments.labels,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
