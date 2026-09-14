from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from eventedge_research.direction_experiment_archive import (
    archive_summary,
    load_direction_experiment_archive,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = PROJECT_ROOT / "EventEdge/NEWS_DIRECTION_EXPERIMENTS_CONTRACT.json"


def _contract() -> dict[str, object]:
    value = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write_contract(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_tracked_direction_experiment_archive_is_valid() -> None:
    value = load_direction_experiment_archive(CONTRACT_PATH, project_root=PROJECT_ROOT)
    summary = archive_summary(value)

    assert summary["production_effect"] == "none"
    assert summary["canonical_rerun_required"] is True
    assert [stage["number"] for stage in summary["stages"]] == [1, 2, 3, 4, 5]
    assert [stage["decision"] for stage in summary["stages"]].count(
        "development_positive_materiality_only"
    ) == 1


def test_archive_rejects_claim_that_legacy_dataset_is_canonical(tmp_path: Path) -> None:
    value = copy.deepcopy(_contract())
    value["canonical_dataset"]["sha256"] = value["source_dataset"]["sha256"]
    path = tmp_path / "contract.json"
    _write_contract(path, value)

    with pytest.raises(ValueError, match="must not be represented as identical"):
        load_direction_experiment_archive(path, project_root=PROJECT_ROOT)


def test_archive_rejects_report_path_outside_project(tmp_path: Path) -> None:
    value = copy.deepcopy(_contract())
    value["stages"][0]["report"] = "../REPORT.md"
    path = tmp_path / "contract.json"
    _write_contract(path, value)

    with pytest.raises(ValueError, match="must stay inside"):
        load_direction_experiment_archive(path, project_root=PROJECT_ROOT)


def test_archive_rejects_reordered_stages(tmp_path: Path) -> None:
    value = copy.deepcopy(_contract())
    value["stages"][0], value["stages"][1] = value["stages"][1], value["stages"][0]
    path = tmp_path / "contract.json"
    _write_contract(path, value)

    with pytest.raises(ValueError, match="ordered from 1 to 5"):
        load_direction_experiment_archive(path, project_root=PROJECT_ROOT)
