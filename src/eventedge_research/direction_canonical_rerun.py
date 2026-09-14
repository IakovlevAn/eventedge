"""Validation helpers for the tracked canonical direction-rerun contract."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "news-direction-canonical-rerun-contract-1.0"
CANONICAL_DATASET_SHA256 = "ee47437edd61f2f444f32e1d85e7562fb0a07c47750651217ffc898c55ca632c"


def load_canonical_rerun_contract(
    path: Path,
    *,
    project_root: Path,
) -> dict[str, Any]:
    """Load the evidence contract and reject unsafe or inconsistent claims."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("canonical rerun contract must be an object")
    dataset = _mapping(value, "dataset")
    protocol = _mapping(value, "protocol")
    controls = _mapping(value, "controls")
    decision = _mapping(value, "overall_decision")
    evidence = _mapping(value, "evidence")
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or value.get("status") != "completed_historical_development_rerun"
        or value.get("production_effect") != "none"
        or dataset.get("sha256") != CANONICAL_DATASET_SHA256
        or dataset.get("rows") != 1238
        or dataset.get("evaluation_rows") != 480
        or protocol.get("folds") != 7
        or protocol.get("embargo_hours") != 72
        or protocol.get("evaluation_windows_are_reused_development_data") is not True
        or protocol.get("evaluation_outcomes_used_for_selection") is not False
        or controls.get("canonical_dataset_rebuilt_byte_exact") is not True
        or controls.get("evaluation_fold_hashes_match_canonical_contract") is not True
        or controls.get("remaining_target_coverage") != "480/480"
        or controls.get("config6_rows") != 157
        or decision.get("direction") != "no_go"
        or decision.get("materiality") != "confirmed_development_signal"
    ):
        raise ValueError("canonical rerun contract is incomplete or inconsistent")
    _validate_stages(value.get("stages"))
    _validate_evidence(evidence, project_root.resolve())
    return value


def canonical_rerun_summary(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Return a small CI- and CLI-friendly status projection."""
    return {
        "status": contract["status"],
        "production_effect": contract["production_effect"],
        "dataset_sha256": contract["dataset"]["sha256"],
        "evaluation_rows": contract["dataset"]["evaluation_rows"],
        "direction_decision": contract["overall_decision"]["direction"],
        "materiality_decision": contract["overall_decision"]["materiality"],
        "stages": [
            {
                "number": stage["number"],
                "id": stage["id"],
                "decision": stage["decision"],
            }
            for stage in contract["stages"]
        ],
    }


def verify_canonical_rerun_results(
    contract: Mapping[str, Any],
    results_path: Path,
) -> None:
    """Match every tracked headline number to the complete ignored result ledger."""
    results = json.loads(results_path.read_text(encoding="utf-8"))
    if (
        not isinstance(results, Mapping)
        or results.get("schema_version") != "news-direction-canonical-rerun-1.0"
        or _nested(results, "dataset", "sha256") != contract["dataset"]["sha256"]
        or _nested(results, "protocol", "evaluation_rows") != contract["dataset"]["evaluation_rows"]
    ):
        raise ValueError("complete canonical rerun results are incompatible")
    mappings = (
        (
            0,
            "finbert_direct_full_population_roc_auc",
            ("stages", "stage1_finbert", "full_population_sentiment_direction", "roc_auc"),
        ),
        (
            0,
            "finbert_direct_full_population_hit_rate",
            ("stages", "stage1_finbert", "full_population_sentiment_direction", "accuracy"),
        ),
        (
            0,
            "config6_logistic_without_finbert_hit_rate",
            ("stages", "stage1_finbert", "config6_population", "logistic_baseline", "accuracy"),
        ),
        (
            0,
            "config6_logistic_with_finbert_hit_rate",
            ("stages", "stage1_finbert", "config6_population", "logistic_with_finbert", "accuracy"),
        ),
        (
            1,
            "materiality_legacy_pr_auc",
            ("stages", "stage2_market", "materiality", "legacy", "pr_auc"),
        ),
        (
            1,
            "materiality_reaction_core_pr_auc",
            ("stages", "stage2_market", "materiality", "reaction_core", "pr_auc"),
        ),
        (
            1,
            "post_hoc_best_direction_roc_auc",
            (
                "stages",
                "stage2_market",
                "remaining_direction",
                "reaction_microstructure_finbert",
                "roc_auc",
            ),
        ),
        (
            1,
            "post_hoc_best_direction_hit_rate",
            (
                "stages",
                "stage2_market",
                "remaining_direction",
                "reaction_microstructure_finbert",
                "accuracy",
            ),
        ),
        (
            2,
            "best_selective_hit_rate",
            (
                "stages",
                "stage3_policy",
                "selective_binary",
                "60",
                "coverage_points",
                "20",
                "hit_rate",
            ),
        ),
        (
            2,
            "remaining_240m_full_coverage_hit_rate",
            (
                "stages",
                "stage3_policy",
                "selective_binary",
                "240",
                "coverage_points",
                "100",
                "hit_rate",
            ),
        ),
        (
            3,
            "reaction_core_roc_auc",
            ("stages", "stage4_structure", "metrics", "reaction_core", "roc_auc"),
        ),
        (
            3,
            "reaction_core_hit_rate",
            ("stages", "stage4_structure", "metrics", "reaction_core", "accuracy"),
        ),
        (
            3,
            "structured_full_roc_auc",
            ("stages", "stage4_structure", "metrics", "structured_full", "roc_auc"),
        ),
        (
            3,
            "structured_full_hit_rate",
            ("stages", "stage4_structure", "metrics", "structured_full", "accuracy"),
        ),
        (
            3,
            "structured_full_tfidf_roc_auc",
            ("stages", "stage4_structure", "metrics", "structured_full_tfidf", "roc_auc"),
        ),
        (
            3,
            "structured_full_tfidf_hit_rate",
            ("stages", "stage4_structure", "metrics", "structured_full_tfidf", "accuracy"),
        ),
        (
            4,
            "reaction_core_roc_auc",
            ("stages", "stage5_nonlinear", "metrics", "reaction_core", "roc_auc"),
        ),
        (
            4,
            "reaction_core_hit_rate",
            ("stages", "stage5_nonlinear", "metrics", "reaction_core", "accuracy"),
        ),
        (
            4,
            "hist_gradient_boosting_roc_auc",
            ("stages", "stage5_nonlinear", "metrics", "hist_gradient_boosting", "roc_auc"),
        ),
        (
            4,
            "hist_gradient_boosting_hit_rate",
            ("stages", "stage5_nonlinear", "metrics", "hist_gradient_boosting", "accuracy"),
        ),
        (
            4,
            "extra_trees_roc_auc",
            ("stages", "stage5_nonlinear", "metrics", "extra_trees", "roc_auc"),
        ),
        (
            4,
            "extra_trees_hit_rate",
            ("stages", "stage5_nonlinear", "metrics", "extra_trees", "accuracy"),
        ),
        (
            4,
            "nonlinear_blend_roc_auc",
            ("stages", "stage5_nonlinear", "metrics", "nonlinear_blend", "roc_auc"),
        ),
        (
            4,
            "nonlinear_blend_hit_rate",
            ("stages", "stage5_nonlinear", "metrics", "nonlinear_blend", "accuracy"),
        ),
    )
    for stage_index, headline_name, result_path in mappings:
        expected = contract["stages"][stage_index]["headline"][headline_name]
        observed = _nested(results, *result_path)
        if expected != observed:
            raise ValueError(f"tracked headline differs from results: {headline_name}")
    compound = (
        (
            1,
            "materiality_pr_auc_delta_ci95",
            [
                _nested(
                    results,
                    "stages",
                    "stage2_market",
                    "materiality_paired_delta_vs_legacy",
                    "reaction_core",
                    "pr_auc",
                    "ci95_low",
                ),
                _nested(
                    results,
                    "stages",
                    "stage2_market",
                    "materiality_paired_delta_vs_legacy",
                    "reaction_core",
                    "pr_auc",
                    "ci95_high",
                ),
            ],
        ),
        (
            1,
            "direction_roc_auc_delta_vs_reaction_core_ci95",
            [
                _nested(
                    results,
                    "stages",
                    "stage2_market",
                    "remaining_direction_paired_delta_vs_reaction_core",
                    "reaction_microstructure_finbert",
                    "roc_auc",
                    "ci95_low",
                ),
                _nested(
                    results,
                    "stages",
                    "stage2_market",
                    "remaining_direction_paired_delta_vs_reaction_core",
                    "reaction_microstructure_finbert",
                    "roc_auc",
                    "ci95_high",
                ),
            ],
        ),
        (
            1,
            "direction_accuracy_delta_vs_reaction_core_ci95",
            [
                _nested(
                    results,
                    "stages",
                    "stage2_market",
                    "remaining_direction_paired_delta_vs_reaction_core",
                    "reaction_microstructure_finbert",
                    "accuracy",
                    "ci95_low",
                ),
                _nested(
                    results,
                    "stages",
                    "stage2_market",
                    "remaining_direction_paired_delta_vs_reaction_core",
                    "reaction_microstructure_finbert",
                    "accuracy",
                    "ci95_high",
                ),
            ],
        ),
        (
            2,
            "best_selective_cluster_ci95",
            [
                _nested(
                    results,
                    "stages",
                    "stage3_policy",
                    "selective_binary",
                    "60",
                    "coverage_points",
                    "20",
                    "cluster_bootstrap",
                    "ci95_low",
                ),
                _nested(
                    results,
                    "stages",
                    "stage3_policy",
                    "selective_binary",
                    "60",
                    "coverage_points",
                    "20",
                    "cluster_bootstrap",
                    "ci95_high",
                ),
            ],
        ),
    )
    for stage_index, headline_name, observed in compound:
        if contract["stages"][stage_index]["headline"][headline_name] != observed:
            raise ValueError(f"tracked headline differs from results: {headline_name}")
    stage3 = contract["stages"][2]["headline"]
    selective = _nested(results, "stages", "stage3_policy", "passing_selective_configurations")
    ternary = _nested(results, "stages", "stage3_policy", "passing_ternary_configurations")
    controls = contract["controls"]
    if (
        stage3["best_selective_horizon_minutes"] != 60
        or stage3["best_selective_actual_coverage"]
        != _nested(
            results,
            "stages",
            "stage3_policy",
            "selective_binary",
            "60",
            "coverage_points",
            "20",
            "actual_coverage",
        )
        or stage3["passing_selective_configurations"] != len(selective)
        or stage3["passing_ternary_configurations"] != len(ternary)
        or controls["config6_rows"] != _nested(results, "controls", "formula_config6", "rows")
        or controls["config6_hit_rate"]
        != _nested(results, "controls", "formula_config6", "hit_rate")
    ):
        raise ValueError("tracked controls differ from complete canonical rerun results")


def _nested(value: Mapping[str, Any], *path: str) -> Any:
    current: Any = value
    for name in path:
        if not isinstance(current, Mapping) or name not in current:
            raise ValueError(f"canonical result path is missing: {'.'.join(path)}")
        current = current[name]
    return current


def _validate_stages(value: object) -> None:
    if not isinstance(value, list) or len(value) != 5:
        raise ValueError("canonical rerun must contain five stages")
    if [stage.get("number") for stage in value if isinstance(stage, Mapping)] != list(range(1, 6)):
        raise ValueError("canonical rerun stages must be ordered from 1 to 5")
    for stage in value:
        if not isinstance(stage, Mapping):
            raise ValueError("canonical rerun stage must be an object")
        headline = stage.get("headline")
        if (
            not isinstance(stage.get("id"), str)
            or stage.get("decision") not in {"no_go", "materiality_confirmed_direction_unconfirmed"}
            or not isinstance(headline, Mapping)
            or not isinstance(stage.get("interpretation"), str)
        ):
            raise ValueError("canonical rerun stage is incomplete")
        _validate_numbers(headline)


def _validate_numbers(value: Mapping[str, Any]) -> None:
    for name, metric in value.items():
        if isinstance(metric, bool) or isinstance(metric, str):
            continue
        values = metric if isinstance(metric, list) else [metric]
        if not values or any(
            not isinstance(number, (int, float)) or not math.isfinite(number) for number in values
        ):
            raise ValueError(f"invalid canonical rerun headline metric: {name}")
        if not name.endswith(("_minutes", "_configurations")) and any(
            not -1 <= number <= 1 for number in values
        ):
            raise ValueError(f"out-of-range canonical rerun metric: {name}")
        if isinstance(metric, list) and len(metric) == 2 and metric[0] > metric[1]:
            raise ValueError(f"reversed canonical rerun interval: {name}")


def _validate_evidence(value: Mapping[str, Any], project_root: Path) -> None:
    for name in ("report", "runner", "protocol", "notebook"):
        relative = value.get(name)
        if not isinstance(relative, str) or Path(relative).is_absolute():
            raise ValueError("canonical rerun evidence path must be project-relative")
        resolved = (project_root / relative).resolve()
        if not resolved.is_relative_to(project_root) or not resolved.is_file():
            raise ValueError("canonical rerun evidence must stay inside the project")


def _mapping(value: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    result = value.get(name)
    if not isinstance(result, Mapping):
        raise ValueError(f"canonical rerun {name} must be an object")
    return result
