"""Training and evaluation for the retained EventEdge news models."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from eventedge_research.news_model_features import (
    NewsFeatureRow,
    deserialize_feature_row,
    fit_news_model,
    retained_feature_specs,
)
from eventedge_research.research_artifacts import iter_jsonl, read_json, write_json, write_jsonl
from eventedge_research.signal_dataset import signal_dataset_sha256
from eventedge_research.signal_extended_metrics import (
    probability_diagnostics,
    select_coverage_threshold,
)
from eventedge_research.signal_formula_comparison import (
    FormulaComparisonRow,
    compare_model_with_formula,
)
from eventedge_research.signal_paired_diagnostics import paired_auc_diagnostics

COVERAGE_LEVELS_PCT = (20, 50, 100)
MAXIMUM_INPUT_ROWS = 20_000
MAXIMUM_PREDICTION_ROWS = 20_000


@dataclass(frozen=True)
class NewsModelDefinition:
    """One retained model, its task and its point-in-time feature set."""

    model_id: str
    task: str
    target: str
    input_name: str
    feature_spec_name: str


MODEL_DEFINITIONS = (
    NewsModelDefinition(
        model_id="publication_materiality_logistic",
        task="materiality",
        target="materiality_4h",
        input_name="publication",
        feature_spec_name="publication_control",
    ),
    NewsModelDefinition(
        model_id="update_5m_market_control",
        task="materiality",
        target="materiality_4h",
        input_name="update_5m",
        feature_spec_name="update_5m_market_control",
    ),
    NewsModelDefinition(
        model_id="update_5m_symmetric_materiality_logistic",
        task="materiality",
        target="materiality_4h",
        input_name="update_5m",
        feature_spec_name="update_5m_symmetric_materiality",
    ),
    NewsModelDefinition(
        model_id="update_5m_market_direction_control",
        task="direction",
        target="remaining_abnormal_4h",
        input_name="update_5m",
        feature_spec_name="update_5m_market_control",
    ),
    NewsModelDefinition(
        model_id="update_5m_corrected_reaction_interactions",
        task="direction",
        target="remaining_abnormal_4h",
        input_name="update_5m",
        feature_spec_name="update_5m_corrected_reaction_interactions",
    ),
)


def fit_benchmark(
    fit_bundle_path: Path,
    output_directory: Path,
    *,
    expected_fit_bundle_sha256: str,
) -> dict[str, object]:
    """Fit retained models from one externally fingerprinted outcome-free bundle."""
    fit_bundle_sha256 = signal_dataset_sha256(fit_bundle_path)
    if fit_bundle_sha256 != expected_fit_bundle_sha256:
        raise ValueError("news model fit bundle does not match the expected digest")
    bundle = _load_fit_bundle(fit_bundle_path)
    if signal_dataset_sha256(fit_bundle_path) != fit_bundle_sha256:
        raise ValueError("news model fit bundle changed while it was read")
    feature_rows = _load_feature_inputs(bundle, fit_bundle_path.parent)
    specs = retained_feature_specs()
    predictions: list[dict[str, object]] = []
    fit_metrics: list[dict[str, object]] = []
    for fold in bundle["folds"]:
        for definition in MODEL_DEFINITIONS:
            metrics, rows = _fit_one_model(
                fold,
                definition,
                feature_rows[definition.input_name],
                specs[definition.feature_spec_name],
            )
            fit_metrics.append(metrics)
            predictions.extend(rows)

    output_directory.mkdir(parents=True, exist_ok=False)
    probability_path = output_directory / "frozen-probabilities.jsonl"
    write_jsonl(probability_path, predictions, maximum_rows=MAXIMUM_PREDICTION_ROWS)
    expected = bundle["expected_output"]
    probability_sha256 = signal_dataset_sha256(probability_path)
    if (
        len(fit_metrics) != expected["model_fits"]
        or len(predictions) != expected["prediction_rows"]
        or probability_sha256 != expected["frozen_probabilities_sha256"]
    ):
        raise ValueError("news model fit changed the frozen benchmark output")
    report = {
        "schema_version": "news-model-freeze-3.0",
        "fit_bundle_filename": fit_bundle_path.name,
        "fit_bundle_sha256": fit_bundle_sha256,
        "contract_sha256": bundle["contract_sha256"],
        "dataset_sha256": bundle["dataset_sha256"],
        "models": [definition.__dict__ for definition in MODEL_DEFINITIONS],
        "folds": len(bundle["folds"]),
        "model_fits": len(fit_metrics),
        "prediction_rows": len(predictions),
        "frozen_probabilities_sha256": probability_sha256,
        "expected_outcomes_sha256": expected["outcomes_sha256"],
        "fit_metrics": fit_metrics,
        "evaluation_outcomes_read": False,
        "production_accessed": False,
        "remote_ydb_accessed": False,
    }
    write_json(output_directory / "freeze.json", report)
    return report


def score_benchmark(
    output_directory: Path,
    outcomes_path: Path,
    scoring_seal_path: Path,
    contract_path: Path,
    *,
    expected_outcomes_sha256: str,
) -> dict[str, object]:
    """Score frozen probabilities against one explicitly fingerprinted ledger."""
    probability_path = output_directory / "frozen-probabilities.jsonl"
    probability_sha256 = signal_dataset_sha256(probability_path)
    outcomes_sha256 = signal_dataset_sha256(outcomes_path)
    contract_sha256 = signal_dataset_sha256(contract_path)
    freeze_path = output_directory / "freeze.json"
    freeze = read_json(freeze_path)
    scoring_seal = read_json(scoring_seal_path)
    contract = read_json(contract_path)
    expected_output = contract.get("expected_output")
    contract_dataset = contract.get("dataset")
    if not isinstance(expected_output, Mapping) or not isinstance(
        contract_dataset, Mapping
    ):
        raise ValueError("news model scoring inputs changed or are incompatible")
    formula_expectation = expected_output.get("formula_comparison")
    if (
        freeze.get("schema_version") != "news-model-freeze-3.0"
        or freeze.get("evaluation_outcomes_read") is not False
        or freeze.get("frozen_probabilities_sha256") != probability_sha256
        or expected_output.get("frozen_probabilities_sha256")
        != probability_sha256
        or freeze.get("dataset_sha256") != contract_dataset.get("sha256")
        or outcomes_sha256 != expected_outcomes_sha256
        or outcomes_sha256 != freeze.get("expected_outcomes_sha256")
        or scoring_seal.get("schema_version") != "news-model-scoring-seal-3.0"
        or scoring_seal.get("fit_bundle_sha256") != freeze.get("fit_bundle_sha256")
        or contract.get("schema_version") != "news-model-benchmark-contract-3.0"
        or contract_sha256 != freeze.get("contract_sha256")
        or contract_sha256 != scoring_seal.get("contract_sha256")
        or scoring_seal.get("outcomes_path") != outcomes_path.name
        or scoring_seal.get("outcomes_sha256") != outcomes_sha256
        or expected_output.get("outcomes_sha256") != outcomes_sha256
        or scoring_seal.get("formula_comparison") != formula_expectation
    ):
        raise ValueError("news model scoring inputs changed or are incompatible")
    predictions = list(iter_jsonl(probability_path, maximum_rows=MAXIMUM_PREDICTION_ROWS))
    outcomes = _load_outcomes(outcomes_path)
    if (
        signal_dataset_sha256(probability_path) != probability_sha256
        or signal_dataset_sha256(outcomes_path) != outcomes_sha256
        or signal_dataset_sha256(contract_path) != contract_sha256
    ):
        raise ValueError("news model scoring inputs changed while they were read")
    grouped = _group_predictions(predictions)
    direction = _direction_models(grouped, outcomes)
    formula_comparison = _direction_formula_comparison(
        grouped["update_5m_corrected_reaction_interactions"],
        outcomes,
    )
    _verify_formula_comparison(
        formula_comparison,
        formula_expectation,
    )
    direction["candidate_vs_stored_config6_formula"] = formula_comparison
    report = {
        "schema_version": "news-model-benchmark-report-2.0",
        "dataset_sha256": freeze["dataset_sha256"],
        "freeze_sha256": signal_dataset_sha256(freeze_path),
        "outcomes_sha256": outcomes_sha256,
        "evaluation_rows": len({row["example_id"] for row in predictions}),
        "materiality": _materiality_models(grouped, outcomes),
        "direction": direction,
        "evaluation_windows_are_reused_development_data": True,
        "production_accessed": False,
        "remote_ydb_accessed": False,
    }
    write_json(output_directory / "report.json", report)
    return report


def _load_fit_bundle(path: Path) -> Mapping[str, object]:
    bundle = read_json(path)
    expected = bundle.get("expected_output", {})
    if (
        bundle.get("schema_version") != "news-model-fit-bundle-1.0"
        or bundle.get("evaluation_outcomes_included") is not False
        or bundle.get("production_accessed") is not False
        or bundle.get("ydb_accessed") is not False
        or expected.get("model_fits") != len(MODEL_DEFINITIONS) * 7
        or expected.get("prediction_rows") != 2_400
        or not isinstance(expected.get("frozen_probabilities_sha256"), str)
        or not isinstance(expected.get("outcomes_sha256"), str)
    ):
        raise ValueError("fit bundle is unsafe or incompatible")
    return bundle


def _load_feature_inputs(
    bundle: Mapping[str, object],
    bundle_directory: Path,
) -> dict[str, dict[str, NewsFeatureRow]]:
    inputs = {}
    for name, path_key, hash_key in (
        ("publication", "publication_input_path", "publication_input_sha256"),
        ("update_5m", "update_input_path", "update_input_sha256"),
    ):
        path = _bundle_file(bundle_directory, bundle[path_key])
        if signal_dataset_sha256(path) != bundle[hash_key]:
            raise ValueError(f"feature input changed: {path}")
        rows = [
            deserialize_feature_row(row)
            for row in iter_jsonl(path, maximum_rows=MAXIMUM_INPUT_ROWS)
        ]
        inputs[name] = {row.example_id: row for row in rows}
        if len(inputs[name]) != len(rows):
            raise ValueError(f"feature input contains duplicate example ids: {path}")
    return inputs


def _bundle_file(directory: Path, value: object) -> Path:
    path = Path(str(value))
    if path.is_absolute() or len(path.parts) != 1 or path.suffix != ".jsonl":
        raise ValueError("news model bundle paths must be local JSONL filenames")
    resolved_directory = directory.resolve()
    resolved = (resolved_directory / path).resolve()
    if not resolved.is_relative_to(resolved_directory):
        raise ValueError("news model bundle file escapes its directory")
    return resolved


def _fit_one_model(fold, definition, feature_index, spec):
    labels = fold["labels"][definition.target]
    training, training_values = _aligned(labels["training"], feature_index)
    validation, validation_values = _aligned(labels["validation"], feature_index)
    evaluation = [feature_index[identity] for identity in labels["evaluation_ids"]]
    model = fit_news_model(
        training,
        training_values,
        validation,
        validation_values,
        spec=spec,
        regularization_c=0.1,
    )
    raw_validation = model.raw_probabilities_up(validation)
    calibrated_validation = model.probabilities_up(validation)
    raw_evaluation = model.raw_probabilities_up(evaluation)
    calibrated_evaluation = model.probabilities_up(evaluation)
    thresholds = {
        "raw": _coverage_thresholds(raw_validation, definition.task),
        "calibrated": _coverage_thresholds(calibrated_validation, definition.task),
    }
    past_only_majority = None
    if definition.task == "direction":
        nonflat_training_values = [value for value in training_values if value != 0]
        past_only_majority = (
            1
            if sum(value > 0 for value in nonflat_training_values) * 2
            >= len(nonflat_training_values)
            else -1
        )
    metadata = {
        "task": definition.task,
        "target": definition.target,
        "model_id": definition.model_id,
        "fold": fold["name"],
    }
    fit_metrics = {
        **metadata,
        "training_rows": len(training),
        "validation_rows": len(validation),
        "training_raw": _compact_metrics(
            training_values, model.raw_probabilities_up(training)
        ),
        "training_calibrated": _compact_metrics(
            training_values, model.probabilities_up(training)
        ),
        "validation_raw": _compact_metrics(validation_values, raw_validation),
        "validation_calibrated": _compact_metrics(
            validation_values, calibrated_validation
        ),
    }
    predictions = [
        {
            "schema_version": "news-model-probability-1.0",
            **metadata,
            "example_id": row.example_id,
            "decision_at": row.decision_at.isoformat(),
            "raw_probability": raw,
            "calibrated_probability": calibrated,
            "coverage_thresholds": thresholds,
            "past_only_majority_direction": past_only_majority,
        }
        for row, raw, calibrated in zip(
            evaluation, raw_evaluation, calibrated_evaluation, strict=True
        )
    ]
    return fit_metrics, predictions


def _coverage_thresholds(probabilities: Sequence[float], task: str) -> dict[str, float]:
    selector = (
        _select_positive_probability_threshold
        if task == "materiality"
        else select_coverage_threshold
    )
    return {
        str(coverage): selector(probabilities, coverage)
        for coverage in COVERAGE_LEVELS_PCT
    }


def _select_positive_probability_threshold(
    probabilities: Sequence[float], coverage_pct: float
) -> float:
    """Select a validation-only top-probability threshold, preserving ties."""
    if (
        not probabilities
        or not 0 < coverage_pct <= 100
        or any(not math.isfinite(value) or not 0 <= value <= 1 for value in probabilities)
    ):
        raise ValueError("materiality coverage requires finite probabilities and coverage")
    if coverage_pct == 100:
        return 0.0
    ordered = sorted(probabilities, reverse=True)
    selected_rows = max(1, math.ceil(len(ordered) * coverage_pct / 100))
    return ordered[selected_rows - 1]


def _compact_metrics(values, probabilities):
    metrics = probability_diagnostics(values, probabilities)["binary_metrics"]
    if metrics is None:
        return None
    return {
        key: metrics[key]
        for key in ("roc_auc", "brier_score", "log_loss", "directional_accuracy_pct")
    }


def _aligned(labels, index):
    return (
        [index[str(row["example_id"])] for row in labels],
        [float(row["return_pct"]) for row in labels],
    )


def _load_outcomes(path: Path) -> dict[str, dict[str, object]]:
    rows = list(iter_jsonl(path, maximum_rows=MAXIMUM_INPUT_ROWS))
    required_formula_fields = {
        "formula_config_version",
        "formula_confidence",
        "formula_direction",
        "formula_score",
    }
    if any(
        row.get("schema_version") != "news-model-outcome-2.0"
        or not required_formula_fields.issubset(row)
        for row in rows
    ):
        raise ValueError("outcome ledger schema or formula fields are incompatible")
    outcomes = {
        str(row["example_id"]): {
            key: value
            for key, value in row.items()
            if key not in {"schema_version", "example_id"}
        }
        for row in rows
    }
    if len(outcomes) != len(rows):
        raise ValueError("outcome ledger contains duplicate example ids")
    return outcomes


def _group_predictions(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[str(row["model_id"])].append(row)
    return grouped


def _materiality_models(grouped, outcomes):
    model_ids = [
        definition.model_id
        for definition in MODEL_DEFINITIONS
        if definition.task == "materiality"
    ]
    models = {
        model_id: {
            score: _materiality_report(
                grouped[model_id], outcomes, f"{score}_probability"
            )
            for score in ("raw", "calibrated")
        }
        for model_id in model_ids
    }
    candidate = "update_5m_symmetric_materiality_logistic"
    control = "update_5m_market_control"
    comparison = _paired_comparison(
        grouped[candidate],
        grouped[control],
        outcomes,
        "materiality_4h",
        "calibrated_probability",
    )
    return {
        "target": "abs(abnormal_4h_return_pct) >= 0.5",
        "models": models,
        "candidate_vs_market_control": comparison,
        "decision": "research_candidate_for_shadow_integration",
    }


def _direction_models(grouped, outcomes):
    candidate = "update_5m_corrected_reaction_interactions"
    control = "update_5m_market_direction_control"
    models = {
        model_id: {
            score: _direction_report(grouped[model_id], outcomes, f"{score}_probability")
            for score in ("raw", "calibrated")
        }
        for model_id in (control, candidate)
    }
    comparison = _paired_comparison(
        grouped[candidate],
        grouped[control],
        outcomes,
        "remaining_abnormal_4h",
        "calibrated_probability",
    )
    candidate_metrics = models[candidate]["calibrated"]["probability_metrics"]
    control_metrics = models[control]["calibrated"]["probability_metrics"]
    interval = comparison["paired_auc"].get("bootstrap_95")
    acceptance = {
        "paired_auc_interval_strictly_positive": bool(interval and interval[0] > 0),
        "brier_not_worse": _metric(candidate_metrics, "brier_score")
        <= _metric(control_metrics, "brier_score"),
        "log_loss_not_worse": _metric(candidate_metrics, "log_loss")
        <= _metric(control_metrics, "log_loss"),
    }
    acceptance["passed"] = all(acceptance.values())
    acceptance["decision"] = (
        "candidate_supported" if acceptance["passed"] else "directional_no_go"
    )
    return {
        "target": "remaining_abnormal_return_from_publication_plus_5m_to_plus_4h",
        "models": models,
        "controls_on_candidate_universe": _direction_controls(
            grouped[candidate], outcomes
        ),
        "candidate_vs_market_control": comparison,
        "acceptance": acceptance,
    }


def _direction_formula_comparison(
    predictions: Sequence[Mapping[str, object]],
    outcomes: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    """Compare the locked direction candidate with config-6 on common rows."""
    rows = _formula_comparison_rows(predictions, outcomes)
    return {
        "model_id": "update_5m_corrected_reaction_interactions",
        "target": "remaining_abnormal_4h",
        "formula_config_version": 6,
        "neutral_or_other_config_rows_excluded_for_both_systems": True,
        **compare_model_with_formula(rows, round_trip_cost_bps=10),
    }


def _formula_comparison_rows(
    predictions: Sequence[Mapping[str, object]],
    outcomes: Mapping[str, Mapping[str, object]],
) -> list[FormulaComparisonRow]:
    rows = []
    for prediction in predictions:
        identity = str(prediction["example_id"])
        outcome = outcomes[identity]
        direction = outcome.get("formula_direction")
        confidence = outcome.get("formula_confidence")
        score = outcome.get("formula_score")
        if (
            outcome.get("formula_config_version") != 6
            or direction not in {"up", "down"}
            or confidence is None
            or score is None
            or outcome.get("remaining_abnormal_4h") is None
            or outcome.get("remaining_raw_4h") is None
        ):
            continue
        formula_probability = float(confidence)
        if direction == "down":
            formula_probability = 1 - formula_probability
        rows.append(
            FormulaComparisonRow(
                example_id=identity,
                cluster_id=str(prediction["decision_at"])[:10],
                target_return_pct=float(outcome["remaining_abnormal_4h"]),
                raw_return_pct=float(outcome["remaining_raw_4h"]),
                abnormal_return_pct=float(outcome["remaining_abnormal_4h"]),
                model_probability_up=float(prediction["calibrated_probability"]),
                formula_probability_up=formula_probability,
                formula_score_probability_up=(float(score) + 100) / 200,
            )
        )
    return rows


def _verify_formula_comparison(
    comparison: Mapping[str, object],
    expectation: object,
) -> None:
    if not isinstance(expectation, Mapping):
        raise ValueError("formula comparison expectation is missing")
    binary_model = comparison["model"]["probability_metrics"]["binary_metrics"]
    binary_formula = comparison["formula"]["probability_metrics"]["binary_metrics"]
    actual = {
        "config_version": comparison["formula_config_version"],
        "model_id": comparison["model_id"],
        "target": comparison["target"],
        "rows": comparison["rows"],
        "model_hit_rate_pct": comparison["model"]["hit_rate_pct"],
        "formula_hit_rate_pct": comparison["formula"]["hit_rate_pct"],
        "model_roc_auc": binary_model["roc_auc"],
        "formula_confidence_roc_auc": binary_formula["roc_auc"],
        "paired_hit_rate_difference_95_pct": comparison["paired_hit_rate"]["bootstrap_95"][
            "difference_percentage_points"
        ],
        "scope": "historical_stored_config_6_rows_only",
    }
    if actual.keys() != expectation.keys() or any(
        not _same_expected_value(actual[name], expectation[name]) for name in actual
    ):
        raise ValueError("formula comparison changed from the sealed expectation")


def _same_expected_value(actual: object, expected: object) -> bool:
    if isinstance(actual, (float, int)) and isinstance(expected, (float, int)):
        return math.isclose(float(actual), float(expected), rel_tol=0, abs_tol=1e-12)
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _same_expected_value(left, right) for left, right in zip(actual, expected, strict=True)
        )
    return actual == expected


def _materiality_report(rows, outcomes, probability_name):
    from sklearn.metrics import average_precision_score

    target = str(rows[0]["target"])
    labels = [float(outcomes[row["example_id"]][target]) > 0 for row in rows]
    probabilities = [float(row[probability_name]) for row in rows]
    return {
        "probability_metrics": probability_diagnostics(
            [float(outcomes[row["example_id"]][target]) for row in rows],
            probabilities,
        ),
        "pr_auc": float(average_precision_score(labels, probabilities)),
        "prevalence_pct": statistics.fmean(labels) * 100,
        "coverage": {
            str(coverage): _materiality_coverage(
                rows, outcomes, probability_name, coverage
            )
            for coverage in COVERAGE_LEVELS_PCT
        },
        "daily_top_k": {
            str(k): _daily_top_k(rows, outcomes, probability_name, k)
            for k in (1, 3)
        },
        "per_fold_auc": _per_fold_auc(rows, outcomes, probability_name),
    }


def _direction_report(rows, outcomes, probability_name):
    target = str(rows[0]["target"])
    values = [float(outcomes[row["example_id"]][target]) for row in rows]
    return {
        "probability_metrics": probability_diagnostics(
            values, [float(row[probability_name]) for row in rows]
        ),
        "coverage": {
            str(coverage): _direction_coverage(
                rows, outcomes, probability_name, coverage
            )
            for coverage in COVERAGE_LEVELS_PCT
        },
        "per_fold_auc": _per_fold_auc(rows, outcomes, probability_name),
    }


def _materiality_coverage(rows, outcomes, probability_name, coverage):
    score_kind = probability_name.removesuffix("_probability")
    selected = [
        row
        for row in rows
        if float(row[probability_name])
        >= float(row["coverage_thresholds"][score_kind][str(coverage)])
    ]
    target = str(rows[0]["target"])
    all_labels = [float(outcomes[row["example_id"]][target]) > 0 for row in rows]
    labels = [float(outcomes[row["example_id"]][target]) > 0 for row in selected]
    precision = statistics.fmean(labels) if labels else None
    prevalence = statistics.fmean(all_labels)
    return {
        "selected_rows": len(selected),
        "coverage_pct": len(selected) / len(rows) * 100,
        "precision_pct": precision * 100 if precision is not None else None,
        "lift": precision / prevalence if precision is not None and prevalence else None,
    }


def _direction_coverage(rows, outcomes, probability_name, coverage):
    score_kind = probability_name.removesuffix("_probability")
    selected = [
        row
        for row in rows
        if max(float(row[probability_name]), 1 - float(row[probability_name]))
        >= float(row["coverage_thresholds"][score_kind][str(coverage)])
    ]
    values = [float(outcomes[row["example_id"]]["remaining_abnormal_4h"]) for row in selected]
    raw_values = [float(outcomes[row["example_id"]]["remaining_raw_4h"]) for row in selected]
    directions = [1 if float(row[probability_name]) >= 0.5 else -1 for row in selected]
    return {
        "selected_rows": len(selected),
        "coverage_pct": len(selected) / len(rows) * 100,
        **_hard_direction_metrics(values, raw_values, directions),
    }


def _daily_top_k(rows, outcomes, probability_name, k):
    grouped = defaultdict(list)
    for row in rows:
        grouped[str(row["decision_at"])[:10]].append(row)
    selected = [
        row
        for day_rows in grouped.values()
        for row in sorted(
            day_rows,
            key=lambda value: float(value[probability_name]),
            reverse=True,
        )[:k]
    ]
    target = str(rows[0]["target"])
    labels = [float(outcomes[row["example_id"]][target]) > 0 for row in selected]
    return {
        "days": len(grouped),
        "selected_rows": len(selected),
        "precision_pct": statistics.fmean(labels) * 100 if labels else None,
    }


def _per_fold_auc(rows, outcomes, probability_name):
    grouped = defaultdict(list)
    for row in rows:
        grouped[str(row["fold"])].append(row)
    return {
        fold: _metric(
            probability_diagnostics(
                [float(outcomes[row["example_id"]][row["target"]]) for row in values],
                [float(row[probability_name]) for row in values],
            ),
            "roc_auc",
        )
        for fold, values in sorted(grouped.items())
    }


def _paired_comparison(candidate, baseline, outcomes, target, probability_name):
    left = {row["example_id"]: row for row in candidate}
    right = {row["example_id"]: row for row in baseline}
    common = sorted(set(left) & set(right))
    return {
        "rows": len(common),
        "paired_auc": paired_auc_diagnostics(
            [float(outcomes[identity][target]) > 0 for identity in common],
            [float(left[identity][probability_name]) for identity in common],
            [float(right[identity][probability_name]) for identity in common],
            [str(left[identity]["decision_at"])[:10] for identity in common],
            repetitions=5_000,
            seed=20260911,
        ),
    }


def _direction_controls(rows, outcomes):
    values = [float(outcomes[row["example_id"]]["remaining_abnormal_4h"]) for row in rows]
    raw_values = [float(outcomes[row["example_id"]]["remaining_raw_4h"]) for row in rows]
    result = {
        "always_up": _hard_direction_metrics(values, raw_values, [1] * len(rows)),
        "always_down": _hard_direction_metrics(values, raw_values, [-1] * len(rows)),
        "past_only_training_majority": _hard_direction_metrics(
            values,
            raw_values,
            [int(row["past_only_majority_direction"]) for row in rows],
        ),
    }
    formula_rows = [
        (
            float(outcomes[row["example_id"]]["remaining_abnormal_4h"]),
            float(outcomes[row["example_id"]]["remaining_raw_4h"]),
            outcomes[row["example_id"]]["formula_direction"],
        )
        for row in rows
        if outcomes[row["example_id"]]["formula_direction"] in {"up", "down"}
    ]
    result["stored_formula"] = {
        **_hard_direction_metrics(
            [value for value, _, _ in formula_rows],
            [value for _, value, _ in formula_rows],
            [1 if direction == "up" else -1 for _, _, direction in formula_rows],
        ),
        "coverage_pct": len(formula_rows) / len(rows) * 100,
        "neutral_rows_abstained": len(rows) - len(formula_rows),
    }
    return result


def _hard_direction_metrics(values, raw_values, directions):
    signed = [value * side for value, side in zip(values, directions, strict=True)]
    signed_raw = [value * side for value, side in zip(raw_values, directions, strict=True)]
    nonflat = [
        (value, side)
        for value, side in zip(values, directions, strict=True)
        if value != 0
    ]
    labels_up = [value > 0 for value, _ in nonflat]
    nonflat_directions = [side for _, side in nonflat]
    true_up = sum(
        label and side > 0
        for label, side in zip(labels_up, nonflat_directions, strict=True)
    )
    true_down = sum(
        not label and side < 0
        for label, side in zip(labels_up, nonflat_directions, strict=True)
    )
    up_count = sum(labels_up)
    down_count = len(labels_up) - up_count
    return {
        "rows": len(values),
        "hit_rate_pct": _positive_pct(signed),
        "flat_rows_excluded_from_balanced_accuracy": len(values) - len(nonflat),
        "balanced_accuracy_pct": (
            (true_up / up_count + true_down / down_count) * 50
            if up_count and down_count
            else None
        ),
        "mean_abnormal_signed_return_pct": _mean_or_none(signed),
        "mean_raw_signed_return_pct": _mean_or_none(signed_raw),
        "mean_net_10bps_abnormal_signed_return_pct": _mean_or_none(
            [value - 0.1 for value in signed]
        ),
    }


def _metric(report, name):
    binary = report.get("binary_metrics")
    return binary.get(name) if binary else None


def _mean_or_none(values):
    return statistics.fmean(values) if values else None


def _positive_pct(values):
    return sum(value > 0 for value in values) / len(values) * 100 if values else None
