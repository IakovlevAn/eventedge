"""Tests for the offline canonical dataset rebuild command."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from signal_fixtures import signal_example

from eventedge_research.signal_dataset import (
    SignalDatasetExample,
    load_signal_dataset,
    signal_dataset_sha256,
    write_signal_dataset,
)
from scripts import rebuild_canonical_signal_dataset as rebuild_module
from scripts.rebenchmark_signal_dataset import _run as rebenchmark_dataset
from scripts.rebuild_canonical_signal_dataset import (
    _merge_datasets,
    rebuild_canonical_dataset,
)


def test_rebuild_canonical_dataset_reproduces_declared_tail(tmp_path: Path) -> None:
    contract_path, expected = _fixture_contract(tmp_path)
    contract_sha256 = signal_dataset_sha256(contract_path)
    output_root = tmp_path / "rebuilt"

    report = rebuild_canonical_dataset(contract_path, tmp_path, output_root)

    assert report["contract_sha256"] == contract_sha256
    assert report["final_dataset_id"] == "canonical_v3_1238"
    assert report["final_dataset_sha256"] == expected["canonical_v3_1238"]
    assert [row["id"] for row in report["verified_inputs"]] == [
        "accepted_history_385",
        "gap_113",
        "imoex2_opens_2026",
        "recovery_history_148",
        "repaired_592",
    ]
    for dataset_id, digest in expected.items():
        assert signal_dataset_sha256(output_root / dataset_id / "dataset.jsonl") == digest
    final_rows = load_signal_dataset(output_root / "canonical_v3_1238" / "dataset.jsonl")
    assert len(final_rows) == 4
    cohort = next(row for row in final_rows if row.decision_at.year == 2026)
    assert cohort.outcome_4h.benchmark_id == "IMOEX2"
    assert json.loads((output_root / "rebuild-report.json").read_text()) == report


def test_rebuild_canonical_dataset_refuses_existing_output(tmp_path: Path) -> None:
    contract_path, _ = _fixture_contract(tmp_path)
    output_root = tmp_path / "rebuilt"
    output_root.mkdir()

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        rebuild_canonical_dataset(contract_path, tmp_path, output_root)


def test_rebuild_canonical_dataset_preserves_stale_writer_lock(tmp_path: Path) -> None:
    contract_path, _ = _fixture_contract(tmp_path)
    output_root = tmp_path / "rebuilt"
    lock_path = tmp_path / ".rebuilt.lock"
    lock_path.write_text("unknown prior owner\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="do not remove it automatically"):
        rebuild_canonical_dataset(contract_path, tmp_path, output_root)

    assert lock_path.read_text(encoding="utf-8") == "unknown prior owner\n"
    assert not output_root.exists()


def test_rebuild_canonical_dataset_does_not_publish_hash_mismatch(
    tmp_path: Path,
) -> None:
    contract_path, _ = _fixture_contract(tmp_path)
    contract = json.loads(contract_path.read_text())
    contract["datasets"][-1]["sha256"] = "0" * 64
    contract_path.write_text(json.dumps(contract))
    output_root = tmp_path / "rebuilt"

    with pytest.raises(ValueError, match="rebuilt dataset SHA-256 changed"):
        rebuild_canonical_dataset(contract_path, tmp_path, output_root)

    assert not output_root.exists()


def test_rebuild_canonical_dataset_rejects_shortened_declared_chain(
    tmp_path: Path,
) -> None:
    contract_path, _ = _fixture_contract(tmp_path)
    contract = json.loads(contract_path.read_text())
    prebuilt_path = tmp_path / ".local-artifacts" / "prebuilt-expanded-v2" / "dataset.jsonl"
    prebuilt_path.parent.mkdir(parents=True)
    prebuilt_path.write_bytes(
        (tmp_path / "expected" / "expanded_v2_imoex2_1125" / "dataset.jsonl").read_bytes()
    )
    expanded_v2 = next(
        entry for entry in contract["datasets"] if entry["id"] == "expanded_v2_imoex2_1125"
    )
    expanded_v2["path"] = ".local-artifacts/prebuilt-expanded-v2/dataset.jsonl"
    contract["rebuild"]["steps"] = [contract["rebuild"]["steps"][-1]]
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    output_root = tmp_path / "rebuilt"

    with pytest.raises(ValueError, match="complete declared chain"):
        rebuild_canonical_dataset(contract_path, tmp_path, output_root)

    assert not output_root.exists()


def test_rebuild_canonical_dataset_does_not_publish_if_contract_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract_path, _ = _fixture_contract(tmp_path)
    output_root = tmp_path / "rebuilt"
    original_verify = rebuild_module._verify_dataset
    contract_mutated = False

    def verify_and_mutate(path, entry) -> None:
        nonlocal contract_mutated
        original_verify(path, entry)
        if contract_mutated:
            return
        contract = json.loads(contract_path.read_text())
        contract["changed_during_rebuild"] = True
        contract_path.write_text(json.dumps(contract), encoding="utf-8")
        contract_mutated = True

    monkeypatch.setattr(rebuild_module, "_verify_dataset", verify_and_mutate)

    with pytest.raises(ValueError, match="contract changed while it was executed"):
        rebuild_canonical_dataset(contract_path, tmp_path, output_root)

    assert contract_mutated
    assert not output_root.exists()


def test_rebuild_canonical_dataset_supports_module_invocation() -> None:
    project_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [sys.executable, "-m", "scripts.rebuild_canonical_signal_dataset", "--help"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "--output-root" in result.stdout


def _fixture_contract(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    inputs = _write_input_datasets(tmp_path)
    benchmark_complete = _write_benchmark(tmp_path, inputs["repaired_592"][0][0])
    expected_root = tmp_path / "expected"
    expected_root.mkdir()
    expected = _build_expected_tail(inputs, benchmark_complete.parent, expected_root)
    datasets = [
        _dataset_entry(identity, path, [], len(rows)) for identity, (rows, path) in inputs.items()
    ]
    datasets.extend(
        (
            _derived_entry(
                "expanded_v1_1125",
                expected,
                3,
                ["repaired_592", "accepted_history_385", "recovery_history_148"],
            ),
            _derived_entry(
                "expanded_v2_imoex2_1125",
                expected,
                3,
                ["expanded_v1_1125", "imoex2_opens_2026"],
            ),
            _derived_entry(
                "canonical_v3_1238",
                expected,
                4,
                ["expanded_v2_imoex2_1125", "gap_113"],
            ),
        )
    )
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
        "checkpoint_seals": [
            {
                "id": "imoex2_opens_2026",
                "path": ".local-data/benchmark/complete.json",
                "sha256": signal_dataset_sha256(benchmark_complete),
            }
        ],
        "datasets": datasets,
        "rebuild": {
            "schema_version": "eventedge-canonical-dataset-rebuild-1.0",
            "steps": [
                {"dataset_id": "expanded_v1_1125", "operation": "merge"},
                {
                    "dataset_id": "expanded_v2_imoex2_1125",
                    "operation": "rebenchmark",
                    "benchmark_input_id": "imoex2_opens_2026",
                    "parameters": {
                        "artifact_version": "fixture-v2",
                        "decision_from": "2026-01-01T00:00:00+00:00",
                        "decision_before": "2027-01-01T00:00:00+00:00",
                        "expected_cohort_rows": 1,
                        "maximum_stock_entry_lag_seconds": 600,
                        "require_complete_cohort": True,
                    },
                },
                {"dataset_id": "canonical_v3_1238", "operation": "merge"},
            ],
        },
        "final_dataset_id": "canonical_v3_1238",
    }
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(contract))
    return contract_path, expected


def _write_input_datasets(
    tmp_path: Path,
) -> dict[str, tuple[list[SignalDatasetExample], Path]]:
    decisions = {
        "repaired_592": datetime(2026, 1, 5, 10, tzinfo=UTC),
        "accepted_history_385": datetime(2025, 1, 5, 10, tzinfo=UTC),
        "recovery_history_148": datetime(2024, 1, 5, 10, tzinfo=UTC),
        "gap_113": datetime(2025, 6, 5, 10, tzinfo=UTC),
    }
    result = {}
    for index, (identity, decision_at) in enumerate(decisions.items(), 1):
        example = SignalDatasetExample.model_validate(
            signal_example(
                index,
                decision_at=decision_at,
                benchmark_return_pct=None,
                label_source="market_outcome",
            )
        )
        directory = tmp_path / ".local-artifacts" / identity
        directory.mkdir(parents=True)
        path = directory / "dataset.jsonl"
        write_signal_dataset(path, [example])
        result[identity] = ([example], path)
    return result


def _write_benchmark(tmp_path: Path, example: SignalDatasetExample) -> Path:
    directory = tmp_path / ".local-data" / "benchmark"
    directory.mkdir(parents=True)
    path = directory / "IMOEX2.jsonl"
    observations = (
        (example.decision_at - timedelta(minutes=61), 2_700),
        (example.decision_at - timedelta(minutes=1), 2_710),
        (example.entry_at, 2_720),
        (example.outcome_4h.target_at, 2_730),
    )
    path.write_text(
        "".join(
            json.dumps(
                {
                    "schema_version": "market-open-observation-1.0",
                    "ticker": "IMOEX2",
                    "at": at.isoformat(),
                    "open": price,
                    "provider_id": "moex-iss-sndx-index-1m",
                    "adjusted": False,
                    "label_source": "market_outcome",
                },
                sort_keys=True,
            )
            + "\n"
            for at, price in observations
        )
    )
    complete = directory / "complete.json"
    complete.write_text(
        json.dumps(
            {
                "schema_version": "checkpointed-index-minute-open-completion-1.0",
                "ticker": "IMOEX2",
                "rows": len(observations),
                "sha256": signal_dataset_sha256(path),
            }
        )
    )
    return complete


def _build_expected_tail(
    inputs: dict[str, tuple[list[SignalDatasetExample], Path]],
    benchmark_directory: Path,
    output_root: Path,
) -> dict[str, str]:
    v1_path = output_root / "expanded_v1_1125" / "dataset.jsonl"
    _merge_datasets(
        [
            inputs[name][1]
            for name in (
                "repaired_592",
                "accepted_history_385",
                "recovery_history_148",
            )
        ],
        v1_path,
    )
    v2_directory = output_root / "expanded_v2_imoex2_1125"
    rebenchmark_dataset(
        argparse.Namespace(
            dataset=v1_path,
            benchmark_dir=benchmark_directory,
            output_dir=v2_directory,
            artifact_version="fixture-v2",
            decision_from=datetime(2026, 1, 1, tzinfo=UTC),
            decision_before=datetime(2027, 1, 1, tzinfo=UTC),
            expected_input_sha256=signal_dataset_sha256(v1_path),
            expected_cohort_rows=1,
            require_complete_cohort=True,
            maximum_stock_entry_lag_seconds=600,
        )
    )
    final_path = output_root / "canonical_v3_1238" / "dataset.jsonl"
    _merge_datasets(
        [v2_directory / "dataset.jsonl", inputs["gap_113"][1]],
        final_path,
    )
    return {
        identity: signal_dataset_sha256(output_root / identity / "dataset.jsonl")
        for identity in (
            "expanded_v1_1125",
            "expanded_v2_imoex2_1125",
            "canonical_v3_1238",
        )
    }


def _dataset_entry(
    identity: str,
    path: Path,
    input_ids: list[str],
    rows: int,
) -> dict[str, object]:
    return {
        "id": identity,
        "path": f".local-artifacts/{identity}/dataset.jsonl",
        "sha256": signal_dataset_sha256(path),
        "rows": rows,
        "inputs": input_ids,
    }


def _derived_entry(
    identity: str,
    expected: dict[str, str],
    rows: int,
    input_ids: list[str],
) -> dict[str, object]:
    return {
        "id": identity,
        "path": f".local-artifacts/not-required/{identity}.jsonl",
        "sha256": expected[identity],
        "rows": rows,
        "inputs": input_ids,
    }
