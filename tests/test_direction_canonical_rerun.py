from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from eventedge_research.direction_canonical_rerun import (
    canonical_rerun_summary,
    load_canonical_rerun_contract,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = PROJECT_ROOT / "EventEdge/NEWS_DIRECTION_CANONICAL_RERUN_CONTRACT.json"


def _contract() -> dict[str, object]:
    value = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_tracked_canonical_direction_rerun_is_valid() -> None:
    value = load_canonical_rerun_contract(CONTRACT_PATH, project_root=PROJECT_ROOT)
    summary = canonical_rerun_summary(value)

    assert summary["direction_decision"] == "no_go"
    assert summary["materiality_decision"] == "confirmed_development_signal"
    assert summary["evaluation_rows"] == 480
    assert [stage["number"] for stage in summary["stages"]] == [1, 2, 3, 4, 5]


def test_contract_rejects_noncanonical_dataset(tmp_path: Path) -> None:
    value = copy.deepcopy(_contract())
    value["dataset"]["sha256"] = "a" * 64
    path = tmp_path / "contract.json"
    _write(path, value)

    with pytest.raises(ValueError, match="incomplete or inconsistent"):
        load_canonical_rerun_contract(path, project_root=PROJECT_ROOT)


def test_contract_rejects_direction_go_claim(tmp_path: Path) -> None:
    value = copy.deepcopy(_contract())
    value["overall_decision"]["direction"] = "go"
    path = tmp_path / "contract.json"
    _write(path, value)

    with pytest.raises(ValueError, match="incomplete or inconsistent"):
        load_canonical_rerun_contract(path, project_root=PROJECT_ROOT)


def test_contract_rejects_evidence_outside_project(tmp_path: Path) -> None:
    value = copy.deepcopy(_contract())
    value["evidence"]["report"] = "../outside.md"
    path = tmp_path / "contract.json"
    _write(path, value)

    with pytest.raises(ValueError, match="inside the project"):
        load_canonical_rerun_contract(path, project_root=PROJECT_ROOT)
