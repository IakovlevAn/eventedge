from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    balanced_accuracy_score,
    brier_score_loss,
    confusion_matrix,
    f1_score,
    log_loss,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.paths import (  # noqa: E402
    dataset_path_from_environment,
    stage_artifact_directory,
)

SEED = 20260913
MODEL_ID = "mxlcw/rubert-tiny2-russian-financial-sentiment"
MODEL_REVISION = "a02913e44597582218db7821d52dc15c331bf427"
TEST_MONTHS = tuple(f"2026-{month:02d}" for month in range(3, 10))
EMBARGO = timedelta(hours=72)
MATERIALITY_THRESHOLD_PCT = 0.5

NUMERIC_BASE_FEATURES = (
    "rule_score",
    "rule_confidence",
    "rule_materiality",
    "source_quality",
    "pre_event_return_1h_pct",
    "pre_event_return_1d_pct",
    "pre_event_return_5d_pct",
    "benchmark_pre_event_return_1h_pct",
    "abs_rule_score",
    "abs_pre_event_return_1h_pct",
    "abs_pre_event_return_1d_pct",
    "abs_pre_event_return_5d_pct",
    "abs_benchmark_pre_event_return_1h_pct",
)
CATEGORICAL_BASE_FEATURES = ("event_type", "source_id", "rule_direction")
SENTIMENT_FEATURES = (
    "sentiment_positive",
    "sentiment_neutral",
    "sentiment_negative",
    "sentiment_score",
    "sentiment_strength",
    "sentiment_confidence",
    "sentiment_entropy",
)
C_GRID = (0.03, 0.1, 0.3, 1.0, 3.0)
CLASS_WEIGHT_GRID: tuple[str | None, ...] = (None, "balanced")
FORBIDDEN_FEATURE_FRAGMENTS = (
    "return_4h",
    "label",
    "label_available_at",
    "entry_at",
)


def assert_feature_contract() -> None:
    """Fail closed if an outcome or post-decision field enters the model."""
    selected = NUMERIC_BASE_FEATURES + CATEGORICAL_BASE_FEATURES + SENTIMENT_FEATURES
    violations = [
        feature
        for feature in selected
        if any(fragment in feature for fragment in FORBIDDEN_FEATURE_FRAGMENTS)
    ]
    if violations:
        raise AssertionError(f"post-outcome features selected: {violations}")


@dataclass(frozen=True)
class Fold:
    name: str
    train_ids: tuple[str, ...]
    validation_ids: tuple[str, ...]
    test_ids: tuple[str, ...]
    train_cutoff: datetime
    validation_cutoff: datetime


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _month_start(value: str) -> pd.Timestamp:
    return pd.Timestamp(f"{value}-01", tz="UTC")


def _subtract_months(value: pd.Timestamp, count: int) -> pd.Timestamp:
    naive = value.tz_localize(None)
    return (naive - pd.DateOffset(months=count)).tz_localize("UTC")


def load_dataset(path: Path) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid JSON at line {line_number}") from error
            features = raw["features"]
            outcome = raw["outcome_4h"]
            abnormal_return = float(outcome["abnormal_return_pct"])
            raw_return = float(outcome["return_pct"])
            if abnormal_return == 0:
                raise ValueError("zero abnormal return has no binary direction label")
            row = {
                "id": str(raw["id"]),
                "event_id": str(raw["event_id"]),
                "event_group_id": str(raw["event_group_id"]),
                "published_at": pd.Timestamp(raw["published_at"]),
                "decision_at": pd.Timestamp(raw["decision_at"]),
                "features_as_of": pd.Timestamp(features["as_of"]),
                "entry_at": pd.Timestamp(raw["entry_at"]),
                "label_available_at": pd.Timestamp(raw["label_available_at"]),
                "source_id": str(raw["source_id"]),
                "ticker": str(raw["ticker"]),
                "title": str(raw["title"]),
                "content": str(raw["content"]),
                "event_type": str(features["event_type"]),
                "rule_direction": str(features["rule_direction"]),
                "rule_score": float(features["rule_score"]),
                "rule_confidence": float(features["rule_confidence"]),
                "rule_materiality": float(features["materiality"]),
                "source_quality": float(features["source_quality"]),
                "pre_event_return_1h_pct": features["pre_event_return_1h_pct"],
                "pre_event_return_1d_pct": features["pre_event_return_1d_pct"],
                "pre_event_return_5d_pct": features["pre_event_return_5d_pct"],
                "benchmark_pre_event_return_1h_pct": features["benchmark_pre_event_return_1h_pct"],
                "stock_return_4h_pct": raw_return,
                "benchmark_return_4h_pct": float(outcome["benchmark_return_pct"]),
                "abnormal_return_4h_pct": abnormal_return,
                "materiality_label": int(abs(abnormal_return) >= MATERIALITY_THRESHOLD_PCT),
                "abnormal_direction_label": int(abnormal_return > 0),
                "raw_direction_label": int(raw_return > 0),
            }
            rows.append(row)

    frame = pd.DataFrame(rows)
    if len(frame) != 1_238:
        raise AssertionError(f"expected 1238 rows, found {len(frame)}")
    if frame["id"].duplicated().any():
        raise AssertionError("row ids must be unique")
    if not (frame["decision_at"] - frame["published_at"] == pd.Timedelta(minutes=5)).all():
        raise AssertionError("decision_at must equal published_at + 5 minutes")
    if (frame["entry_at"] < frame["decision_at"]).any():
        raise AssertionError("entry_at precedes decision_at")
    if (frame["features_as_of"] > frame["decision_at"]).any():
        raise AssertionError("feature timestamp is later than decision_at")
    observed_formula = frame["stock_return_4h_pct"] - frame["benchmark_return_4h_pct"]
    if not np.allclose(observed_formula, frame["abnormal_return_4h_pct"], atol=1e-10):
        raise AssertionError("abnormal return formula mismatch")

    for feature in (
        "pre_event_return_1h_pct",
        "pre_event_return_1d_pct",
        "pre_event_return_5d_pct",
        "benchmark_pre_event_return_1h_pct",
    ):
        frame[feature] = pd.to_numeric(frame[feature], errors="coerce")
        frame[f"abs_{feature}"] = frame[feature].abs()
    frame["abs_rule_score"] = frame["rule_score"].abs()
    return frame


def _model_text(row: pd.Series) -> str:
    title = str(row["title"]).strip()
    content = str(row["content"]).strip()
    if not content:
        return title
    if title and content.casefold().startswith(title.casefold()):
        return content
    return f"{title}\n{content}" if title else content


def add_sentiment(
    frame: pd.DataFrame,
    *,
    cache_path: Path,
    dataset_sha256: str,
    batch_size: int = 32,
) -> pd.DataFrame:
    texts = frame.apply(_model_text, axis=1)
    text_hashes = texts.map(lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest())
    if cache_path.exists():
        cached = pd.read_csv(cache_path)
        valid = (
            len(cached) == len(frame)
            and set(cached["id"]) == set(frame["id"])
            and cached["dataset_sha256"].nunique() == 1
            and cached["dataset_sha256"].iloc[0] == dataset_sha256
            and cached["model_revision"].nunique() == 1
            and cached["model_revision"].iloc[0] == MODEL_REVISION
        )
        if valid:
            check = frame[["id"]].copy()
            check["text_sha256"] = text_hashes
            cached_hashes = cached[["id", "text_sha256"]]
            merged_hashes = check.merge(cached_hashes, on="id", suffixes=("_now", "_cached"))
            valid = (merged_hashes["text_sha256_now"] == merged_hashes["text_sha256_cached"]).all()
        if valid:
            return frame.merge(
                cached[
                    [
                        "id",
                        "sentiment_positive",
                        "sentiment_neutral",
                        "sentiment_negative",
                    ]
                ],
                on="id",
                validate="one_to_one",
            ).pipe(_derive_sentiment_features)

    try:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "uncached sentiment inference requires the research-nlp dependency group"
        ) from error

    random.seed(SEED)
    np.random.seed(SEED)
    torch.manual_seed(SEED)
    torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, revision=MODEL_REVISION)
    model = AutoModelForSequenceClassification.from_pretrained(
        MODEL_ID,
        revision=MODEL_REVISION,
    )
    expected_labels = {0: "neutral", 1: "positive", 2: "negative"}
    observed_labels = {int(key): str(value) for key, value in model.config.id2label.items()}
    if observed_labels != expected_labels:
        raise AssertionError(f"unexpected label mapping: {observed_labels}")
    model.eval()
    probabilities: list[np.ndarray] = []
    with torch.inference_mode():
        for offset in range(0, len(texts), batch_size):
            encoded = tokenizer(
                texts.iloc[offset : offset + batch_size].tolist(),
                padding=True,
                truncation=True,
                max_length=512,
                return_tensors="pt",
            )
            logits = model(**encoded).logits
            probabilities.append(torch.softmax(logits, dim=-1).cpu().numpy())
    matrix = np.concatenate(probabilities, axis=0)
    if matrix.shape != (len(frame), 3) or not np.allclose(matrix.sum(axis=1), 1, atol=1e-6):
        raise AssertionError("invalid sentiment probability matrix")

    cache = pd.DataFrame(
        {
            "id": frame["id"],
            "text_sha256": text_hashes,
            "dataset_sha256": dataset_sha256,
            "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION,
            "sentiment_neutral": matrix[:, 0],
            "sentiment_positive": matrix[:, 1],
            "sentiment_negative": matrix[:, 2],
        }
    )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache.to_csv(cache_path, index=False, float_format="%.10g")
    return frame.merge(
        cache[["id", "sentiment_positive", "sentiment_neutral", "sentiment_negative"]],
        on="id",
        validate="one_to_one",
    ).pipe(_derive_sentiment_features)


def _derive_sentiment_features(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["sentiment_score"] = result["sentiment_positive"] - result["sentiment_negative"]
    result["sentiment_strength"] = 1 - result["sentiment_neutral"]
    probabilities = result[
        ["sentiment_positive", "sentiment_neutral", "sentiment_negative"]
    ].to_numpy()
    result["sentiment_confidence"] = probabilities.max(axis=1)
    safe = np.clip(probabilities, 1e-12, 1)
    result["sentiment_entropy"] = -(safe * np.log(safe)).sum(axis=1) / math.log(3)
    return result


def build_folds(frame: pd.DataFrame) -> list[Fold]:
    folds: list[Fold] = []
    for month in TEST_MONTHS:
        test_start = _month_start(month)
        test_end = test_start + pd.DateOffset(months=1)
        validation_start = _subtract_months(test_start, 2)
        train_cutoff = validation_start - EMBARGO
        validation_cutoff = test_start - EMBARGO

        train = frame[
            (frame["published_at"] < train_cutoff) & (frame["label_available_at"] <= train_cutoff)
        ].copy()
        validation = frame[
            (frame["published_at"] >= validation_start)
            & (frame["published_at"] < validation_cutoff)
            & (frame["label_available_at"] <= validation_cutoff)
        ].copy()
        test = frame[
            (frame["published_at"] >= test_start) & (frame["published_at"] < test_end)
        ].copy()

        # Preserve every test row, but purge earlier examples from any group
        # visible in a later partition. This prevents repeated publications from
        # leaking while keeping the declared 480-row evaluation population.
        test_groups = set(test["event_group_id"])
        validation = validation[~validation["event_group_id"].isin(test_groups)]
        validation_groups = set(validation["event_group_id"])
        train = train[~train["event_group_id"].isin(test_groups | validation_groups)]

        train_groups = set(train["event_group_id"])
        has_group_overlap = (
            bool(train_groups & validation_groups)
            or bool(train_groups & test_groups)
            or bool(validation_groups & test_groups)
        )
        if has_group_overlap:
            raise AssertionError(f"event group overlap in fold {month}")
        if train["label_available_at"].max() > train_cutoff:
            raise AssertionError(f"training label leakage in fold {month}")
        if validation["label_available_at"].max() > validation_cutoff:
            raise AssertionError(f"validation label leakage in fold {month}")
        for partition_name, partition in (
            ("train", train),
            ("validation", validation),
            ("test", test),
        ):
            if partition.empty:
                raise AssertionError(f"empty {partition_name} partition in fold {month}")

        folds.append(
            Fold(
                name=month,
                train_ids=tuple(train["id"]),
                validation_ids=tuple(validation["id"]),
                test_ids=tuple(test["id"]),
                train_cutoff=train_cutoff.to_pydatetime(),
                validation_cutoff=validation_cutoff.to_pydatetime(),
            )
        )

    test_ids = [row_id for fold in folds for row_id in fold.test_ids]
    if len(test_ids) != 480 or len(set(test_ids)) != 480:
        raise AssertionError(f"expected 480 unique test rows, found {len(test_ids)}")
    return folds


def _rows(frame: pd.DataFrame, ids: tuple[str, ...]) -> pd.DataFrame:
    indexed = frame.set_index("id", drop=False)
    return indexed.loc[list(ids)].reset_index(drop=True)


def make_pipeline(*, use_sentiment: bool, c: float, class_weight: str | None) -> Pipeline:
    numeric = list(NUMERIC_BASE_FEATURES)
    if use_sentiment:
        numeric.extend(SENTIMENT_FEATURES)
    categorical = list(CATEGORICAL_BASE_FEATURES)
    preprocessing = ColumnTransformer(
        [
            (
                "numeric",
                Pipeline(
                    [
                        ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
                        ("scale", StandardScaler()),
                    ]
                ),
                numeric,
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
                categorical,
            ),
        ]
    )
    return Pipeline(
        [
            ("features", preprocessing),
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
    candidates = np.unique(np.concatenate(([0.05, 0.95], probability)))
    best = (float("-inf"), 0.5)
    for threshold in candidates:
        predicted = (probability >= threshold).astype(int)
        score = f1_score(y_true, predicted, zero_division=0)
        candidate = (score, -abs(float(threshold) - 0.5))
        incumbent = (best[0], -abs(best[1] - 0.5))
        if candidate > incumbent:
            best = (score, float(threshold))
    return best[1]


def _fit_selected_model(
    train: pd.DataFrame,
    validation: pd.DataFrame,
    *,
    target: str,
    use_sentiment: bool,
    selection_metric: str,
) -> tuple[Pipeline, dict[str, Any], float]:
    best: tuple[float, float, str | None, Pipeline, np.ndarray] | None = None
    for c in C_GRID:
        for class_weight in CLASS_WEIGHT_GRID:
            candidate = make_pipeline(
                use_sentiment=use_sentiment,
                c=c,
                class_weight=class_weight,
            )
            candidate.fit(train, train[target])
            probability = candidate.predict_proba(validation)[:, 1]
            if selection_metric == "average_precision":
                score = average_precision_score(validation[target], probability)
            elif selection_metric == "roc_auc":
                score = roc_auc_score(validation[target], probability)
            else:
                raise ValueError(f"unsupported selection metric: {selection_metric}")
            record = (float(score), -c, class_weight, candidate, probability)
            if best is None or record[:2] > best[:2]:
                best = record
    if best is None:
        raise AssertionError("no candidate model was fitted")
    _, negative_c, class_weight, selected_model, validation_probability = best
    selected_c = -negative_c
    threshold = _best_threshold(validation[target].to_numpy(), validation_probability)
    # Keep the exact train-fitted model whose validation probabilities selected
    # the threshold. Refitting on validation would change the probability scale
    # and invalidate that threshold.
    return (
        selected_model,
        {"C": selected_c, "class_weight": class_weight},
        threshold,
    )


def binary_metrics(
    y_true: np.ndarray,
    probability: np.ndarray,
    *,
    threshold: float = 0.5,
) -> dict[str, Any]:
    predicted = (probability >= threshold).astype(int)
    matrix = confusion_matrix(y_true, predicted, labels=[0, 1])
    return {
        "n": int(len(y_true)),
        "positive_rate": float(np.mean(y_true)),
        "threshold": float(threshold),
        "roc_auc": float(roc_auc_score(y_true, probability)),
        "pr_auc": float(average_precision_score(y_true, probability)),
        "brier": float(brier_score_loss(y_true, probability)),
        "log_loss": float(log_loss(y_true, np.column_stack([1 - probability, probability]))),
        "accuracy": float(accuracy_score(y_true, predicted)),
        "balanced_accuracy": float(balanced_accuracy_score(y_true, predicted)),
        "precision": float(precision_score(y_true, predicted, zero_division=0)),
        "recall": float(recall_score(y_true, predicted, zero_division=0)),
        "f1": float(f1_score(y_true, predicted, zero_division=0)),
        "confusion_matrix": matrix.tolist(),
    }


def _cluster_bootstrap_delta(
    predictions: pd.DataFrame,
    *,
    metric: str,
    probability_a: str,
    probability_b: str,
    iterations: int = 2_000,
) -> dict[str, float]:
    grouped = {key: group for key, group in predictions.groupby("event_group_id")}
    keys = np.array(list(grouped), dtype=object)
    rng = np.random.default_rng(SEED)
    values: list[float] = []
    for _ in range(iterations):
        sample_keys = rng.choice(keys, size=len(keys), replace=True)
        sample = pd.concat([grouped[key] for key in sample_keys], ignore_index=True)
        y = sample["materiality_label"].to_numpy()
        if len(np.unique(y)) != 2:
            continue
        if metric == "pr_auc":
            scorer = average_precision_score
        elif metric == "roc_auc":
            scorer = roc_auc_score
        else:
            raise ValueError(metric)
        values.append(float(scorer(y, sample[probability_b]) - scorer(y, sample[probability_a])))
    low, high = np.percentile(values, [2.5, 97.5])
    return {
        "estimate": float(
            (average_precision_score if metric == "pr_auc" else roc_auc_score)(
                predictions["materiality_label"], predictions[probability_b]
            )
            - (average_precision_score if metric == "pr_auc" else roc_auc_score)(
                predictions["materiality_label"], predictions[probability_a]
            )
        ),
        "ci95_low": float(low),
        "ci95_high": float(high),
        "iterations": len(values),
    }


def _cluster_bootstrap_accuracy_delta(
    predictions: pd.DataFrame,
    *,
    prediction_a: str,
    prediction_b: str,
    iterations: int = 2_000,
) -> dict[str, float]:
    grouped = {key: group for key, group in predictions.groupby("event_group_id")}
    keys = np.array(list(grouped), dtype=object)
    rng = np.random.default_rng(SEED)
    values: list[float] = []
    for _ in range(iterations):
        sample_keys = rng.choice(keys, size=len(keys), replace=True)
        sample = pd.concat([grouped[key] for key in sample_keys], ignore_index=True)
        y = sample["abnormal_direction_label"].to_numpy()
        values.append(
            float(accuracy_score(y, sample[prediction_b]) - accuracy_score(y, sample[prediction_a]))
        )
    low, high = np.percentile(values, [2.5, 97.5])
    y = predictions["abnormal_direction_label"].to_numpy()
    return {
        "estimate": float(
            accuracy_score(y, predictions[prediction_b])
            - accuracy_score(y, predictions[prediction_a])
        ),
        "ci95_low": float(low),
        "ci95_high": float(high),
        "iterations": len(values),
    }


def run_materiality(frame: pd.DataFrame, folds: list[Fold]) -> tuple[pd.DataFrame, pd.DataFrame]:
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
                "materiality_label",
                "abnormal_direction_label",
                "raw_direction_label",
                "rule_direction",
                "sentiment_positive",
                "sentiment_neutral",
                "sentiment_negative",
                "sentiment_score",
                "sentiment_confidence",
            ]
        ].copy()
        output["fold"] = fold.name
        output["material_probability_finbert_direct"] = 1 - output["sentiment_neutral"]
        for label, use_sentiment in (("baseline", False), ("with_finbert", True)):
            model, selected, threshold = _fit_selected_model(
                train,
                validation,
                target="materiality_label",
                use_sentiment=use_sentiment,
                selection_metric="average_precision",
            )
            probability = model.predict_proba(test)[:, 1]
            output[f"material_probability_{label}"] = probability
            output[f"material_threshold_{label}"] = threshold
            metrics = binary_metrics(
                test["materiality_label"].to_numpy(),
                probability,
                threshold=threshold,
            )
            fold_rows.append(
                {
                    "task": "materiality",
                    "fold": fold.name,
                    "model": label,
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
    if len(predictions) != 480 or predictions["id"].duplicated().any():
        raise AssertionError("materiality predictions do not match the evaluation population")
    return predictions, pd.DataFrame(fold_rows)


def run_direction(
    frame: pd.DataFrame,
    folds: list[Fold],
    material_predictions: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    prediction_rows: list[pd.DataFrame] = []
    fold_rows: list[dict[str, Any]] = []
    for fold in folds:
        train = _rows(frame, fold.train_ids)
        validation = _rows(frame, fold.validation_ids)
        test = _rows(frame, fold.test_ids)
        train = train[train["rule_direction"].isin(["up", "down"])].copy()
        validation = validation[validation["rule_direction"].isin(["up", "down"])].copy()
        test = test[test["rule_direction"].isin(["up", "down"])].copy()
        if test.empty:
            continue

        output = test[
            [
                "id",
                "event_group_id",
                "published_at",
                "abnormal_direction_label",
                "raw_direction_label",
                "rule_direction",
                "sentiment_positive",
                "sentiment_neutral",
                "sentiment_negative",
                "sentiment_score",
                "sentiment_confidence",
            ]
        ].copy()
        output["fold"] = fold.name
        output["rule_prediction"] = (output["rule_direction"] == "up").astype(int)
        denominator = output["sentiment_positive"] + output["sentiment_negative"]
        output["finbert_direction_probability"] = output["sentiment_positive"] / denominator
        output["finbert_prediction"] = (output["finbert_direction_probability"] >= 0.5).astype(int)
        output["agreement"] = output["finbert_prediction"] == output["rule_prediction"]

        for label, use_sentiment in (("baseline", False), ("with_finbert", True)):
            model, selected, threshold = _fit_selected_model(
                train,
                validation,
                target="abnormal_direction_label",
                use_sentiment=use_sentiment,
                selection_metric="roc_auc",
            )
            probability = model.predict_proba(test)[:, 1]
            output[f"direction_probability_{label}"] = probability
            output[f"direction_threshold_{label}"] = threshold
            output[f"direction_prediction_{label}"] = (probability >= threshold).astype(int)
            metrics = binary_metrics(
                test["abnormal_direction_label"].to_numpy(),
                probability,
                threshold=threshold,
            )
            fold_rows.append(
                {
                    "task": "abnormal_direction",
                    "fold": fold.name,
                    "model": label,
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
    if len(predictions) != 157 or predictions["id"].duplicated().any():
        raise AssertionError(
            f"expected 157 config-6 directional evaluation rows, found {len(predictions)}"
        )
    expected_ids = set(
        material_predictions.loc[material_predictions["rule_direction"].isin(["up", "down"]), "id"]
    )
    if set(predictions["id"]) != expected_ids:
        raise AssertionError("direction and materiality evaluation ids differ")
    return predictions, pd.DataFrame(fold_rows)


def _direction_summary(predictions: pd.DataFrame) -> dict[str, Any]:
    y = predictions["abnormal_direction_label"].to_numpy()
    raw_y = predictions["raw_direction_label"].to_numpy()
    summary: dict[str, Any] = {}
    for label, probability, threshold in (
        ("config_6", predictions["rule_prediction"].to_numpy(dtype=float), 0.5),
        (
            "finbert_direct",
            predictions["finbert_direction_probability"].to_numpy(),
            0.5,
        ),
        (
            "logistic_baseline",
            predictions["direction_probability_baseline"].to_numpy(),
            float("nan"),
        ),
        (
            "logistic_with_finbert",
            predictions["direction_probability_with_finbert"].to_numpy(),
            float("nan"),
        ),
    ):
        if math.isnan(threshold):
            thresholds = predictions[
                "direction_threshold_with_finbert"
                if label == "logistic_with_finbert"
                else "direction_threshold_baseline"
            ].to_numpy()
            predicted = (probability >= thresholds).astype(int)
            threshold_for_metrics = 0.5
            ranking_probability = probability
        else:
            predicted = (probability >= threshold).astype(int)
            threshold_for_metrics = threshold
            ranking_probability = probability
        metrics = binary_metrics(y, ranking_probability, threshold=threshold_for_metrics)
        # Replace thresholded fields when thresholds differ by fold.
        if math.isnan(threshold):
            metrics.update(
                {
                    "accuracy": float(accuracy_score(y, predicted)),
                    "balanced_accuracy": float(balanced_accuracy_score(y, predicted)),
                    "precision": float(precision_score(y, predicted, zero_division=0)),
                    "recall": float(recall_score(y, predicted, zero_division=0)),
                    "f1": float(f1_score(y, predicted, zero_division=0)),
                    "threshold": "selected_per_fold",
                    "confusion_matrix": confusion_matrix(y, predicted, labels=[0, 1]).tolist(),
                }
            )
        metrics["raw_direction_hit_rate"] = float(accuracy_score(raw_y, predicted))
        summary[label] = metrics

    agreement = predictions[predictions["agreement"]].copy()
    agreement_y = agreement["abnormal_direction_label"].to_numpy()
    agreement_pred = agreement["rule_prediction"].to_numpy()
    summary["config_6_finbert_agreement"] = {
        "n": int(len(agreement)),
        "coverage": float(len(agreement) / len(predictions)),
        "hit_rate": float(accuracy_score(agreement_y, agreement_pred)) if len(agreement) else None,
        "balanced_accuracy": (
            float(balanced_accuracy_score(agreement_y, agreement_pred))
            if len(np.unique(agreement_y)) == 2
            else None
        ),
    }
    summary["paired_hit_rate_delta_vs_config_6"] = {
        "finbert_direct": _cluster_bootstrap_accuracy_delta(
            predictions,
            prediction_a="rule_prediction",
            prediction_b="finbert_prediction",
        ),
        "logistic_baseline": _cluster_bootstrap_accuracy_delta(
            predictions,
            prediction_a="rule_prediction",
            prediction_b="direction_prediction_baseline",
        ),
        "logistic_with_finbert": _cluster_bootstrap_accuracy_delta(
            predictions,
            prediction_a="rule_prediction",
            prediction_b="direction_prediction_with_finbert",
        ),
    }
    return summary


def _finbert_direction_full_population_summary(
    predictions: pd.DataFrame,
) -> dict[str, Any]:
    probability = predictions["sentiment_positive"] / (
        predictions["sentiment_positive"] + predictions["sentiment_negative"]
    )
    return binary_metrics(
        predictions["abnormal_direction_label"].to_numpy(),
        probability.to_numpy(),
    )


def _materiality_summary(predictions: pd.DataFrame) -> dict[str, Any]:
    y = predictions["materiality_label"].to_numpy()
    result: dict[str, Any] = {
        "finbert_direct": binary_metrics(
            y,
            predictions["material_probability_finbert_direct"].to_numpy(),
        )
    }
    for label in ("baseline", "with_finbert"):
        probability = predictions[f"material_probability_{label}"].to_numpy()
        thresholds = predictions[f"material_threshold_{label}"].to_numpy()
        predicted = (probability >= thresholds).astype(int)
        metrics = binary_metrics(y, probability)
        metrics.update(
            {
                "accuracy": float(accuracy_score(y, predicted)),
                "balanced_accuracy": float(balanced_accuracy_score(y, predicted)),
                "precision": float(precision_score(y, predicted, zero_division=0)),
                "recall": float(recall_score(y, predicted, zero_division=0)),
                "f1": float(f1_score(y, predicted, zero_division=0)),
                "threshold": "selected_per_fold",
                "confusion_matrix": confusion_matrix(y, predicted, labels=[0, 1]).tolist(),
            }
        )
        result[label] = metrics
    result["paired_delta_with_finbert"] = {
        "pr_auc": _cluster_bootstrap_delta(
            predictions,
            metric="pr_auc",
            probability_a="material_probability_baseline",
            probability_b="material_probability_with_finbert",
        ),
        "roc_auc": _cluster_bootstrap_delta(
            predictions,
            metric="roc_auc",
            probability_a="material_probability_baseline",
            probability_b="material_probability_with_finbert",
        ),
    }
    per_fold = []
    for fold, group in predictions.groupby("fold", sort=True):
        y_fold = group["materiality_label"]
        baseline = average_precision_score(y_fold, group["material_probability_baseline"])
        with_finbert = average_precision_score(y_fold, group["material_probability_with_finbert"])
        per_fold.append(
            {
                "fold": fold,
                "baseline_pr_auc": float(baseline),
                "with_finbert_pr_auc": float(with_finbert),
                "delta": float(with_finbert - baseline),
            }
        )
    result["pr_auc_delta_by_fold"] = per_fold
    result["positive_pr_auc_delta_folds"] = sum(row["delta"] > 0 for row in per_fold)
    return result


def save_plots(material_predictions: pd.DataFrame, output_dir: Path) -> None:
    from sklearn.calibration import calibration_curve
    from sklearn.metrics import PrecisionRecallDisplay, RocCurveDisplay

    y = material_predictions["materiality_label"]
    figure, axes = plt.subplots(1, 3, figsize=(15, 4.5))
    for label, color in (("baseline", "#6b7280"), ("with_finbert", "#2563eb")):
        probability = material_predictions[f"material_probability_{label}"]
        RocCurveDisplay.from_predictions(y, probability, name=label, ax=axes[0])
        PrecisionRecallDisplay.from_predictions(
            y,
            probability,
            name=label,
            ax=axes[1],
        )
        observed, predicted = calibration_curve(y, probability, n_bins=8, strategy="quantile")
        axes[2].plot(predicted, observed, marker="o", label=label, color=color)
    axes[0].set_title("Materiality ROC")
    axes[1].set_title("Materiality precision-recall")
    axes[2].plot([0, 1], [0, 1], linestyle="--", color="#9ca3af")
    axes[2].set_title("Calibration")
    axes[2].set_xlabel("Predicted probability")
    axes[2].set_ylabel("Observed material rate")
    axes[2].legend()
    figure.tight_layout()
    figure.savefig(output_dir / "materiality_diagnostics.png", dpi=160)
    plt.close(figure)


def run_experiment(dataset_path: Path, output_dir: Path) -> dict[str, Any]:
    assert_feature_contract()
    output_dir.mkdir(parents=True, exist_ok=True)
    dataset_sha256 = sha256_file(dataset_path)
    frame = load_dataset(dataset_path)
    frame = add_sentiment(
        frame,
        cache_path=output_dir / "sentiment_predictions.csv",
        dataset_sha256=dataset_sha256,
    )
    folds = build_folds(frame)
    material_predictions, material_fold_metrics = run_materiality(frame, folds)
    direction_predictions, direction_fold_metrics = run_direction(
        frame,
        folds,
        material_predictions,
    )

    material_predictions.to_csv(
        output_dir / "materiality_predictions.csv",
        index=False,
        float_format="%.10g",
    )
    direction_predictions.to_csv(
        output_dir / "direction_predictions.csv",
        index=False,
        float_format="%.10g",
    )
    fold_metrics = pd.concat(
        [material_fold_metrics, direction_fold_metrics],
        ignore_index=True,
    )
    fold_metrics.to_csv(output_dir / "fold_metrics.csv", index=False, float_format="%.10g")
    save_plots(material_predictions, output_dir)

    result = {
        "experiment": "finbert-stage1-1.0",
        "created_at": datetime.now(UTC).isoformat(),
        "seed": SEED,
        "dataset": {
            "path": str(dataset_path),
            "sha256": dataset_sha256,
            "rows": len(frame),
            "event_ids": int(frame["event_id"].nunique()),
            "event_group_ids": int(frame["event_group_id"].nunique()),
        },
        "model": {"id": MODEL_ID, "revision": MODEL_REVISION},
        "protocol": {
            "test_months": list(TEST_MONTHS),
            "validation_months": 2,
            "embargo_hours": 72,
            "group_key": "event_group_id",
            "materiality_threshold_pct": MATERIALITY_THRESHOLD_PCT,
            "materiality_selection_metric": "average_precision",
            "direction_selection_metric": "roc_auc",
            "note": (
                "The supplied JSONL lacks exact first-five-minute reaction features; "
                "this experiment estimates the incremental value of frozen FinBERT "
                "over an identical baseline built only from supplied as-of features."
            ),
            "raw_zero_return_policy": (
                "For the auxiliary raw-direction metric, an exact zero stock return "
                "is mapped to non-up, matching the pre-existing binary convention; "
                "the abnormal-direction target contains no exact zeros."
            ),
        },
        "folds": [
            {
                "name": fold.name,
                "train_n": len(fold.train_ids),
                "validation_n": len(fold.validation_ids),
                "test_n": len(fold.test_ids),
                "train_cutoff": fold.train_cutoff.isoformat(),
                "validation_cutoff": fold.validation_cutoff.isoformat(),
            }
            for fold in folds
        ],
        "materiality": _materiality_summary(material_predictions),
        "finbert_direction_full_population": (
            _finbert_direction_full_population_summary(material_predictions)
        ),
        "abnormal_direction_config6_population": _direction_summary(direction_predictions),
        "artifact_hashes": {},
    }
    for filename in (
        "sentiment_predictions.csv",
        "materiality_predictions.csv",
        "direction_predictions.csv",
        "fold_metrics.csv",
        "materiality_diagnostics.png",
    ):
        result["artifact_hashes"][filename] = sha256_file(output_dir / filename)
    metrics_path = output_dir / "metrics.json"
    metrics_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run EventEdge FinBERT stage-1 experiment")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=dataset_path_from_environment(),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=stage_artifact_directory("finbert_stage1"),
    )
    arguments = parser.parse_args()
    result = run_experiment(arguments.dataset, arguments.output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
