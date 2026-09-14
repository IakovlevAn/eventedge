"""Validation for the archived five-stage direction research contract."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

SCHEMA_VERSION = "news-direction-experiment-archive-1.0"
EXPECTED_STATUS = "legacy_historical_development_archive"
EXPECTED_STAGE_IDS = (
    "finbert-stage1-1.0",
    "market-stage2-1.0",
    "direction-stage3-1.0",
    "direction-stage4-1.0",
    "direction-stage5-1.0",
)
MAXIMUM_CONTRACT_BYTES = 100_000
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def load_direction_experiment_archive(
    path: Path,
    *,
    project_root: Path,
) -> dict[str, object]:
    """Load and fail closed on an ambiguous or unsafe archive contract."""
    if not path.is_file() or path.stat().st_size > MAXIMUM_CONTRACT_BYTES:
        raise ValueError("direction experiment archive is missing or too large")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("direction experiment archive must be an object")
    _validate_archive(value, project_root.resolve())
    return value


def archive_summary(value: Mapping[str, object]) -> dict[str, object]:
    """Return the small public status used by CLI checks and documentation."""
    stages = _list(value.get("stages"), "stages")
    return {
        "schema_version": value.get("schema_version"),
        "status": value.get("status"),
        "production_effect": value.get("production_effect"),
        "source_dataset_sha256": _mapping(value.get("source_dataset"), "source dataset").get(
            "sha256"
        ),
        "canonical_rerun_required": True,
        "stages": [
            {
                "number": _mapping(stage, "stage").get("number"),
                "id": _mapping(stage, "stage").get("id"),
                "decision": _mapping(stage, "stage").get("decision"),
            }
            for stage in stages
        ],
    }


def _validate_archive(value: Mapping[str, object], project_root: Path) -> None:
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported direction experiment archive schema")
    if value.get("status") != EXPECTED_STATUS or value.get("production_effect") != "none":
        raise ValueError("archive must be historical research with no production effect")
    if (
        value.get("source_digest_semantics")
        != "original_generated_files_before_portable_path_normalization"
    ):
        raise ValueError("archive source digest semantics are ambiguous")

    source = _mapping(value.get("source_dataset"), "source dataset")
    canonical = _mapping(value.get("canonical_dataset"), "canonical dataset")
    source_sha = _sha256(source.get("sha256"), "source dataset sha256")
    canonical_sha = _sha256(canonical.get("sha256"), "canonical dataset sha256")
    if source_sha == canonical_sha:
        raise ValueError("legacy and canonical datasets must not be represented as identical")
    if canonical.get("compatibility") != "rerun_required_before_canonical_comparison":
        raise ValueError("archive must require a canonical rerun")

    protocol = _mapping(value.get("protocol"), "protocol")
    if (
        protocol.get("evaluation_rows") != 480
        or protocol.get("validation_months") != 2
        or protocol.get("embargo_hours") != 72
        or protocol.get("group_key") != "event_group_id"
        or protocol.get("target_compatibility") != "not_identical"
    ):
        raise ValueError("direction experiment archive protocol is incompatible")

    stages = _list(value.get("stages"), "stages")
    stage_objects = [_mapping(stage, "stage") for stage in stages]
    if tuple(stage.get("number") for stage in stage_objects) != (1, 2, 3, 4, 5):
        raise ValueError("direction experiment stages must be ordered from 1 to 5")
    if tuple(stage.get("id") for stage in stage_objects) != EXPECTED_STAGE_IDS:
        raise ValueError("direction experiment stage identifiers are incompatible")
    for stage in stage_objects:
        decision = stage.get("decision")
        if decision not in {"no_go", "development_positive_materiality_only"}:
            raise ValueError("archive stages cannot contain a production decision")
        _sha256(stage.get("source_metrics_sha256"), "source metrics sha256")
        for name in ("source_market_cache_sha256", "source_outcome_cache_sha256"):
            if name in stage:
                _sha256(stage.get(name), name.replace("_", " "))
        report_path = _safe_project_path(stage.get("report"), project_root)
        if not report_path.is_file():
            raise ValueError(f"direction experiment report is missing: {report_path}")


def _safe_project_path(value: object, project_root: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("archive report path must be a non-empty string")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("archive report path must stay inside the project")
    resolved = project_root.joinpath(*relative.parts).resolve()
    if not resolved.is_relative_to(project_root):
        raise ValueError("archive report path escapes the project")
    return resolved


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    return value
