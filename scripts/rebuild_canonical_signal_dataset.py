"""Rebuild the canonical research dataset from frozen local checkpoints.

The command is deliberately offline. It accepts only the operations and local
artifact roots declared by the dataset-lineage contract, verifies every direct
input before writing, and publishes results only after all expected hashes and
row counts match.
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from types import FrameType, SimpleNamespace

from eventedge_research.research_artifacts import (
    exclusive_run_lock,
    iter_jsonl,
    read_json,
    write_json,
)
from eventedge_research.signal_dataset import (
    SignalDatasetExample,
    load_signal_dataset,
    signal_dataset_sha256,
    write_signal_dataset,
)
from scripts.merge_signal_datasets import (
    _cross_dataset_event_overlap,
    _validate_cross_dataset_identities,
)
from scripts.rebenchmark_signal_dataset import _run as rebenchmark_dataset
from scripts.verify_dataset_lineage import _resolve_local_path

_CONTRACT_SCHEMA = "eventedge-dataset-lineage-contract-2.0"
_REBUILD_SCHEMA = "eventedge-canonical-dataset-rebuild-1.0"
_DATASET_ID = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_MAXIMUM_DATASET_ROWS = 5_000
_MAXIMUM_REBUILD_STEPS = 10
_SUPPORTED_OPERATIONS = frozenset({"merge", "rebenchmark"})


def rebuild_canonical_dataset(
    contract_path: Path,
    artifact_root: Path,
    output_root: Path,
) -> dict[str, object]:
    """Rebuild and verify the contract's derived canonical dataset.

    Args:
        contract_path: Dataset-lineage contract to execute.
        artifact_root: Root holding frozen ``.local-data`` and
            ``.local-artifacts`` inputs.
        output_root: New directory in which to publish rebuilt derivatives.

    Returns:
        A bounded report describing verified inputs and rebuilt datasets.

    Raises:
        FileExistsError: If ``output_root`` already exists.
        ValueError: If the contract, an input, or rebuilt output differs from
            its frozen expectation.
    """
    output_root = Path(output_root)
    output_root.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output_root.with_name(f".{output_root.name}.lock")
    with exclusive_run_lock(lock_path):
        return _rebuild_canonical_dataset_locked(
            contract_path,
            artifact_root,
            output_root,
        )


def _rebuild_canonical_dataset_locked(
    contract_path: Path,
    artifact_root: Path,
    output_root: Path,
) -> dict[str, object]:
    """Rebuild the dataset while holding its output-specific writer lock."""
    if output_root.exists() or output_root.is_symlink():
        raise FileExistsError(f"refusing to overwrite rebuild output: {output_root}")
    contract_sha256 = signal_dataset_sha256(contract_path)
    contract = read_json(contract_path)
    datasets, entries, steps = _validate_contract(contract)
    root = artifact_root.resolve()
    verified_inputs = _verify_direct_inputs(root, datasets, entries, steps)

    with tempfile.TemporaryDirectory(
        prefix=f".{output_root.name}.",
        dir=output_root.parent,
    ) as temporary_name:
        staging_root = Path(temporary_name)
        generated: dict[str, Path] = {}
        rebuilt = []
        for step in steps:
            dataset_id = str(step["dataset_id"])
            output = staging_root / dataset_id / "dataset.jsonl"
            entry = datasets[dataset_id]
            if step["operation"] == "merge":
                input_paths = [
                    _dataset_input_path(root, datasets, generated, input_id)
                    for input_id in entry["inputs"]
                ]
                _merge_datasets(input_paths, output)
            else:
                input_path, benchmark_directory = _rebenchmark_inputs(
                    root,
                    datasets,
                    entries,
                    generated,
                    step,
                )
                _rebenchmark(step, entry, input_path, benchmark_directory, output.parent)
            _verify_dataset(output, entry)
            generated[dataset_id] = output
            rebuilt.append(
                {
                    "id": dataset_id,
                    "rows": entry["rows"],
                    "sha256": entry["sha256"],
                    "operation": step["operation"],
                }
            )

        final_id = str(contract["final_dataset_id"])
        report = {
            "schema_version": "eventedge-canonical-dataset-rebuild-report-1.0",
            "contract_sha256": contract_sha256,
            "verified_inputs": verified_inputs,
            "rebuilt": rebuilt,
            "final_dataset_id": final_id,
            "final_dataset_relative_path": f"{final_id}/dataset.jsonl",
            "final_dataset_sha256": datasets[final_id]["sha256"],
            "production_accessed": False,
            "remote_ydb_accessed": False,
            "network_accessed": False,
        }
        write_json(staging_root / "rebuild-report.json", report)
        if signal_dataset_sha256(contract_path) != contract_sha256:
            raise ValueError("dataset rebuild contract changed while it was executed")
        staging_root.replace(output_root)
    return report


def _validate_contract(
    contract: Mapping[str, object],
) -> tuple[
    dict[str, Mapping[str, object]],
    dict[str, Mapping[str, object]],
    list[Mapping[str, object]],
]:
    guards = contract.get("guards")
    rebuild = contract.get("rebuild")
    if (
        contract.get("schema_version") != _CONTRACT_SCHEMA
        or contract.get("status") != "historical_research_proxy"
        or contract.get("exact_replay_starts_at") != "repaired_592"
        or not isinstance(guards, Mapping)
        or guards.get("production_access_allowed") is not False
        or guards.get("remote_ydb_access_allowed") is not False
        or guards.get("network_access_allowed") is not False
        or not isinstance(rebuild, Mapping)
        or rebuild.get("schema_version") != _REBUILD_SCHEMA
    ):
        raise ValueError("dataset rebuild contract is unsafe or incompatible")
    sections = ("source_seals", "checkpoint_seals", "datasets")
    if any(not isinstance(contract.get(name), list) for name in sections):
        raise ValueError("dataset rebuild contract sections are incomplete")
    all_entries = [entry for name in sections for entry in contract[name]]
    if any(not isinstance(entry, Mapping) for entry in all_entries):
        raise ValueError("dataset rebuild contract entries must be objects")
    entries = {str(entry["id"]): entry for entry in all_entries}
    if len(entries) != len(all_entries):
        raise ValueError("dataset rebuild contract contains duplicate ids")
    datasets = {str(entry["id"]): entry for entry in contract["datasets"]}
    steps = rebuild.get("steps")
    if (
        not isinstance(steps, list)
        or not steps
        or len(steps) > _MAXIMUM_REBUILD_STEPS
        or any(not isinstance(step, Mapping) for step in steps)
    ):
        raise ValueError("dataset rebuild steps are missing or unexpectedly large")
    _validate_steps(contract, datasets, entries, steps)
    return datasets, entries, steps


def _validate_steps(
    contract: Mapping[str, object],
    datasets: Mapping[str, Mapping[str, object]],
    entries: Mapping[str, Mapping[str, object]],
    steps: Sequence[Mapping[str, object]],
) -> None:
    produced: set[str] = set()
    for step in steps:
        dataset_id = str(step.get("dataset_id", ""))
        operation = step.get("operation")
        if (
            not _DATASET_ID.fullmatch(dataset_id)
            or dataset_id not in datasets
            or dataset_id in produced
            or operation not in _SUPPORTED_OPERATIONS
        ):
            raise ValueError("dataset rebuild step is invalid or duplicated")
        inputs = datasets[dataset_id].get("inputs")
        if not isinstance(inputs, list) or not inputs:
            raise ValueError(f"dataset rebuild inputs are missing: {dataset_id}")
        unknown = set(inputs) - set(entries)
        later_dependencies = {
            str(other.get("dataset_id")) for other in steps if other is not step
        } - produced
        if unknown or set(inputs) & later_dependencies:
            raise ValueError(f"dataset rebuild inputs are unknown or unordered: {dataset_id}")
        if operation == "merge" and len(inputs) < 2:
            raise ValueError("dataset merge requires at least two inputs")
        if operation == "rebenchmark":
            _validate_rebenchmark_step(step, inputs, entries)
        produced.add(dataset_id)
    if str(contract.get("final_dataset_id")) != str(steps[-1]["dataset_id"]):
        raise ValueError("dataset rebuild must finish with the canonical dataset")
    if produced != _required_rebuild_ids(contract, datasets):
        raise ValueError("dataset rebuild steps do not cover the complete declared chain")


def _required_rebuild_ids(
    contract: Mapping[str, object],
    datasets: Mapping[str, Mapping[str, object]],
) -> set[str]:
    """Return dataset IDs on the declared replay-start-to-final lineage."""
    start_id = str(contract.get("exact_replay_starts_at", ""))
    final_id = str(contract.get("final_dataset_id", ""))
    if start_id not in datasets or final_id not in datasets:
        raise ValueError("dataset rebuild endpoints are not declared datasets")

    dependencies: dict[str, set[str]] = {}
    dependents = {dataset_id: set() for dataset_id in datasets}
    for dataset_id, entry in datasets.items():
        inputs = entry.get("inputs")
        if not isinstance(inputs, list) or any(not isinstance(value, str) for value in inputs):
            raise ValueError(f"dataset inputs must be a list of ids: {dataset_id}")
        dependencies[dataset_id] = {value for value in inputs if value in datasets}
        for input_id in dependencies[dataset_id]:
            dependents[input_id].add(dataset_id)

    downstream = _reachable_ids(start_id, dependents)
    upstream = _reachable_ids(final_id, dependencies)
    required = (downstream & upstream) - {start_id}
    if final_id not in required:
        raise ValueError("final dataset is not derived from the exact replay start")
    return required


def _reachable_ids(start_id: str, edges: Mapping[str, set[str]]) -> set[str]:
    """Return nodes reachable from ``start_id`` through a bounded contract graph."""
    reached: set[str] = set()
    pending = [start_id]
    while pending:
        dataset_id = pending.pop()
        if dataset_id in reached:
            continue
        reached.add(dataset_id)
        pending.extend(edges[dataset_id] - reached)
    return reached


def _validate_rebenchmark_step(
    step: Mapping[str, object],
    inputs: Sequence[object],
    entries: Mapping[str, Mapping[str, object]],
) -> None:
    benchmark_id = step.get("benchmark_input_id")
    parameters = step.get("parameters")
    if (
        len(inputs) != 2
        or benchmark_id not in inputs
        or benchmark_id not in entries
        or not isinstance(parameters, Mapping)
        or parameters.get("require_complete_cohort") is not True
        or not isinstance(parameters.get("expected_cohort_rows"), int)
        or parameters["expected_cohort_rows"] <= 0
        or not isinstance(parameters.get("maximum_stock_entry_lag_seconds"), int)
        or parameters["maximum_stock_entry_lag_seconds"] <= 0
    ):
        raise ValueError("dataset rebenchmark step is unsafe or incomplete")
    decision_from = _aware_datetime(parameters.get("decision_from"))
    decision_before = _aware_datetime(parameters.get("decision_before"))
    if decision_from >= decision_before:
        raise ValueError("dataset rebenchmark cohort window is empty")


def _verify_direct_inputs(
    root: Path,
    datasets: Mapping[str, Mapping[str, object]],
    entries: Mapping[str, Mapping[str, object]],
    steps: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    produced = {str(step["dataset_id"]) for step in steps}
    input_ids = {
        str(input_id)
        for step in steps
        for input_id in datasets[str(step["dataset_id"])]["inputs"]
        if str(input_id) not in produced
    }
    verified = []
    for input_id in sorted(input_ids):
        entry = entries[input_id]
        path = _resolve_local_path(root, entry["path"])
        digest = signal_dataset_sha256(path)
        if digest != entry.get("sha256"):
            raise ValueError(f"dataset rebuild input changed: {input_id}")
        result: dict[str, object] = {"id": input_id, "sha256": digest}
        if input_id in datasets:
            _verify_dataset(path, entry)
            result["rows"] = entry["rows"]
        verified.append(result)
    return verified


def _dataset_input_path(
    root: Path,
    datasets: Mapping[str, Mapping[str, object]],
    generated: Mapping[str, Path],
    input_id: object,
) -> Path:
    identity = str(input_id)
    if identity in generated:
        return generated[identity]
    if identity not in datasets:
        raise ValueError(f"merge input is not a dataset: {identity}")
    return _resolve_local_path(root, datasets[identity]["path"])


def _merge_datasets(input_paths: Sequence[Path], output: Path) -> None:
    datasets = [(path, load_signal_dataset(path)) for path in input_paths]
    examples: list[SignalDatasetExample] = [example for _, rows in datasets for example in rows]
    _validate_cross_dataset_identities(examples)
    overlap = _cross_dataset_event_overlap(datasets)
    if overlap["pairs"]:
        raise ValueError("cross-dataset near-duplicate event detected")
    output.parent.mkdir(parents=True, exist_ok=False)
    write_signal_dataset(output, examples)


def _rebenchmark_inputs(
    root: Path,
    datasets: Mapping[str, Mapping[str, object]],
    entries: Mapping[str, Mapping[str, object]],
    generated: Mapping[str, Path],
    step: Mapping[str, object],
) -> tuple[Path, Path]:
    output_entry = datasets[str(step["dataset_id"])]
    benchmark_id = str(step["benchmark_input_id"])
    dataset_ids = [value for value in output_entry["inputs"] if value != benchmark_id]
    if len(dataset_ids) != 1:
        raise ValueError("rebenchmark step must have exactly one dataset input")
    dataset_path = _dataset_input_path(root, datasets, generated, dataset_ids[0])
    benchmark_path = _resolve_local_path(root, entries[benchmark_id]["path"])
    return dataset_path, benchmark_path.parent


def _rebenchmark(
    step: Mapping[str, object],
    entry: Mapping[str, object],
    input_path: Path,
    benchmark_directory: Path,
    output_directory: Path,
) -> None:
    parameters = step["parameters"]
    rebenchmark_dataset(
        SimpleNamespace(
            dataset=input_path,
            benchmark_dir=benchmark_directory,
            output_dir=output_directory,
            artifact_version=str(parameters["artifact_version"]),
            decision_from=_aware_datetime(parameters["decision_from"]),
            decision_before=_aware_datetime(parameters["decision_before"]),
            expected_input_sha256=signal_dataset_sha256(input_path),
            expected_cohort_rows=int(parameters["expected_cohort_rows"]),
            require_complete_cohort=True,
            maximum_stock_entry_lag_seconds=int(parameters["maximum_stock_entry_lag_seconds"]),
        )
    )
    if entry["sha256"] == signal_dataset_sha256(input_path):
        raise ValueError("rebenchmark step did not change the frozen dataset")


def _verify_dataset(path: Path, entry: Mapping[str, object]) -> None:
    digest = signal_dataset_sha256(path)
    if digest != entry.get("sha256"):
        raise ValueError(f"rebuilt dataset SHA-256 changed: {entry.get('id')}")
    rows = sum(1 for _ in iter_jsonl(path, maximum_rows=_MAXIMUM_DATASET_ROWS))
    if rows != entry.get("rows"):
        raise ValueError(f"rebuilt dataset row count changed: {entry.get('id')}")


def _aware_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("rebenchmark timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("rebenchmark timestamp must include a timezone")
    return parsed


def _timeout(signum: int, frame: FrameType | None) -> None:
    del signum, frame
    raise TimeoutError("canonical dataset rebuild exceeded its hard wall-clock limit")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--contract",
        type=Path,
        default=Path("EventEdge/DATASET_LINEAGE_CONTRACT.json"),
    )
    parser.add_argument("--artifact-root", type=Path, default=Path("."))
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--maximum-run-seconds", type=int, default=1_800)
    args = parser.parse_args()
    if not 30 <= args.maximum_run_seconds <= 3_600:
        parser.error("maximum run time must be between 30 and 3600 seconds")
    previous = signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(args.maximum_run_seconds)
    try:
        report = rebuild_canonical_dataset(
            args.contract,
            args.artifact_root,
            args.output_root,
        )
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
