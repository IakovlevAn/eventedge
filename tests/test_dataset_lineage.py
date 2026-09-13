"""Tests for the frozen dataset-lineage contract verifier."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eventedge_research.research_artifacts import read_json
from eventedge_research.signal_dataset import signal_dataset_sha256
from scripts.verify_dataset_lineage import _resolve_local_path, verify_lineage

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _contract(
    tmp_path: Path,
    *,
    source_seals: list[dict[str, object]] | None = None,
    checkpoint_seals: list[dict[str, object]] | None = None,
) -> Path:
    dataset = tmp_path / ".local-artifacts/dataset.jsonl"
    dataset.parent.mkdir(parents=True, exist_ok=True)
    dataset.write_text('{"id": 1}\n', encoding="utf-8")
    value = {
        "schema_version": "eventedge-dataset-lineage-contract-2.0",
        "status": "historical_research_proxy",
        "exact_replay_starts_at": "repaired_592",
        "guards": {
            "production_access_allowed": False,
            "remote_ydb_access_allowed": False,
            "network_access_allowed": False,
        },
        "source_seals": source_seals or [],
        "checkpoint_seals": checkpoint_seals or [],
        "datasets": [
            {
                "id": "repaired_592",
                "path": ".local-artifacts/dataset.jsonl",
                "sha256": signal_dataset_sha256(dataset),
                "rows": 1,
                "inputs": [],
            }
        ],
        "rebuild": {
            "schema_version": "eventedge-canonical-dataset-rebuild-1.0",
            "steps": [{"dataset_id": "repaired_592", "operation": "merge"}],
        },
        "final_dataset_id": "repaired_592",
    }
    contract_path = tmp_path / "contract.json"
    _write_json(contract_path, value)
    return contract_path


def test_verify_lineage_checks_hash_rows_and_dag(tmp_path: Path) -> None:
    artifact = tmp_path / ".local-artifacts/dataset.jsonl"
    artifact.parent.mkdir()
    artifact.write_text('{"id": 1}\n{"id": 2}\n')
    contract = {
        "schema_version": "eventedge-dataset-lineage-contract-2.0",
        "status": "historical_research_proxy",
        "exact_replay_starts_at": "repaired_592",
        "guards": {
            "production_access_allowed": False,
            "remote_ydb_access_allowed": False,
            "network_access_allowed": False,
        },
        "source_seals": [],
        "checkpoint_seals": [],
        "datasets": [
            {
                "id": "repaired_592",
                "path": ".local-artifacts/dataset.jsonl",
                "sha256": signal_dataset_sha256(artifact),
                "rows": 2,
                "inputs": [],
            }
        ],
        "rebuild": {
            "schema_version": "eventedge-canonical-dataset-rebuild-1.0",
            "steps": [
                {"dataset_id": "repaired_592", "operation": "merge"},
            ],
        },
        "final_dataset_id": "repaired_592",
    }
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(contract))

    report = verify_lineage(contract_path, tmp_path)

    assert report["verified_files"] == 1
    assert report["verified_datasets"] == 1
    assert report["accepted_checkpoint_boundary"] == "repaired_592"
    assert report["raw_provenance_scope"].startswith(
        "declared_local_byte_integrity_only"
    )
    assert report["production_accessed"] is False


def test_verify_lineage_expands_telegram_manifest_and_counts_records(
    tmp_path: Path,
) -> None:
    archive = tmp_path / ".local-data/archive"
    data_path = archive / "messages.jsonl"
    data_path.parent.mkdir(parents=True)
    data_path.write_text('{"id": 1}\n{"id": 2}\n', encoding="utf-8")
    manifest_path = archive / "manifest.json"
    _write_json(
        manifest_path,
        {
            "schema_version": "eventedge-telegram-archive-batch-manifest-1.0",
            "channels": [
                {
                    "data_path": ".local-data/archive/messages.jsonl",
                    "data_sha256": signal_dataset_sha256(data_path),
                    "records_written": 2,
                }
            ],
        },
    )
    contract_path = _contract(
        tmp_path,
        source_seals=[
            {
                "id": "archive",
                "path": ".local-data/archive/manifest.json",
                "sha256": signal_dataset_sha256(manifest_path),
            }
        ],
    )

    report = verify_lineage(contract_path, tmp_path)

    assert report["verified_linked_files"] == 1
    assert report["verified_telegram_channels"] == 1
    assert report["verified_telegram_records"] == 2


def test_verify_lineage_rejects_telegram_record_count_mismatch(tmp_path: Path) -> None:
    archive = tmp_path / ".local-data/archive"
    data_path = archive / "messages.jsonl"
    data_path.parent.mkdir(parents=True)
    data_path.write_text('{"id": 1}\n', encoding="utf-8")
    manifest_path = archive / "manifest.json"
    _write_json(
        manifest_path,
        {
            "schema_version": "eventedge-telegram-archive-batch-manifest-1.0",
            "channels": [
                {
                    "data_path": ".local-data/archive/messages.jsonl",
                    "data_sha256": signal_dataset_sha256(data_path),
                    "records_written": 2,
                }
            ],
        },
    )
    contract_path = _contract(
        tmp_path,
        source_seals=[
            {
                "id": "archive",
                "path": ".local-data/archive/manifest.json",
                "sha256": signal_dataset_sha256(manifest_path),
            }
        ],
    )

    with pytest.raises(ValueError, match="record count changed"):
        verify_lineage(contract_path, tmp_path)


def test_verify_lineage_recursively_checks_completion_hashes(tmp_path: Path) -> None:
    checkpoint = tmp_path / ".local-artifacts/checkpoint"
    payload_path = checkpoint / "page/payload.jsonl"
    payload_path.parent.mkdir(parents=True)
    payload_path.write_text('{"id": 1}\n', encoding="utf-8")
    nested_manifest = checkpoint / "page/complete.json"
    _write_json(
        nested_manifest,
        {"sha256": {"payload.jsonl": signal_dataset_sha256(payload_path)}},
    )
    complete_path = checkpoint / "complete.json"
    _write_json(
        complete_path,
        {
            "sha256": {
                "page/complete.json": signal_dataset_sha256(nested_manifest),
            }
        },
    )
    contract_path = _contract(
        tmp_path,
        checkpoint_seals=[
            {
                "id": "checkpoint",
                "path": ".local-artifacts/checkpoint/complete.json",
                "sha256": signal_dataset_sha256(complete_path),
            }
        ],
    )

    report = verify_lineage(contract_path, tmp_path)
    assert report["verified_linked_files"] == 2

    payload_path.write_text('{"id": 9}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="linked artifact changed"):
        verify_lineage(contract_path, tmp_path)


def test_verify_lineage_rejects_completion_path_escape(tmp_path: Path) -> None:
    checkpoint = tmp_path / ".local-artifacts/checkpoint"
    outside = tmp_path / ".local-artifacts/outside.json"
    outside.parent.mkdir(parents=True)
    outside.write_text("{}\n", encoding="utf-8")
    complete_path = checkpoint / "complete.json"
    _write_json(
        complete_path,
        {"sha256": {"../outside.json": signal_dataset_sha256(outside)}},
    )
    contract_path = _contract(
        tmp_path,
        checkpoint_seals=[
            {
                "id": "checkpoint",
                "path": ".local-artifacts/checkpoint/complete.json",
                "sha256": signal_dataset_sha256(complete_path),
            }
        ],
    )

    with pytest.raises(ValueError, match="safe and relative"):
        verify_lineage(contract_path, tmp_path)


def test_verify_lineage_checks_linked_issuer_plan(tmp_path: Path) -> None:
    checkpoint = tmp_path / ".local-data/opens"
    plan_path = checkpoint / "plan.json"
    _write_json(plan_path, {"tickers": ["GAZP", "SBER"]})
    complete_path = checkpoint / "complete.json"
    _write_json(
        complete_path,
        {
            "completed_tickers": 2,
            "plan_sha256": signal_dataset_sha256(plan_path),
        },
    )
    contract_path = _contract(
        tmp_path,
        source_seals=[
            {
                "id": "opens",
                "path": ".local-data/opens/complete.json",
                "sha256": signal_dataset_sha256(complete_path),
            }
        ],
    )

    report = verify_lineage(contract_path, tmp_path)

    assert report["verified_plan_files"] == 1
    assert report["verified_linked_files"] == 1


def test_resolve_local_path_rejects_symlink_escape(tmp_path: Path) -> None:
    artifact_directory = tmp_path / ".local-artifacts"
    artifact_directory.mkdir()
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n")
    (artifact_directory / "dataset.jsonl").symlink_to(outside)

    with pytest.raises(ValueError, match="escapes root"):
        _resolve_local_path(tmp_path, ".local-artifacts/dataset.jsonl")


def test_tracked_manifest_locks_dataset_lineage_contract() -> None:
    contract_path = PROJECT_ROOT / "EventEdge/DATASET_LINEAGE_CONTRACT.json"
    manifest = read_json(PROJECT_ROOT / "EventEdge/ML_ARTIFACT_MANIFEST.json")

    assert manifest["tracked_evidence"]["dataset_lineage_contract"][
        "sha256"
    ] == signal_dataset_sha256(contract_path)


def test_tracked_contract_declares_complete_point_in_time_tail() -> None:
    contract = read_json(PROJECT_ROOT / "EventEdge/DATASET_LINEAGE_CONTRACT.json")

    assert contract["final_dataset_id"] == "canonical_v4_point_in_time_1238"
    assert [step["dataset_id"] for step in contract["rebuild"]["steps"]] == [
        "expanded_v1_1125",
        "expanded_v2_point_in_time_1125",
        "canonical_v3_point_in_time_2026_1238",
        "canonical_v4_history_point_in_time_1238",
        "canonical_v4_point_in_time_1238",
    ]
    final = next(
        row
        for row in contract["datasets"]
        if row["id"] == "canonical_v4_point_in_time_1238"
    )
    assert final["rows"] == 1238
    assert final["sha256"] == (
        "ee47437edd61f2f444f32e1d85e7562fb0a07c47750651217ffc898c55ca632c"
    )
