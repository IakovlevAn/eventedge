"""Verify the frozen local checkpoints behind the canonical research dataset."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from eventedge_research.research_artifacts import file_sha256, iter_jsonl, read_json
from eventedge_research.signal_dataset import signal_dataset_sha256

MAXIMUM_CONTRACT_ENTRIES = 100
MAXIMUM_DATASET_ROWS = 5_000
MAXIMUM_LINKED_FILES = 20_000
MAXIMUM_LINKED_FILES_PER_MANIFEST = 5_000
MAXIMUM_LINK_DEPTH = 8
MAXIMUM_TELEGRAM_CHANNELS = 100
MAXIMUM_TELEGRAM_RECORDS_PER_CHANNEL = 250_000

TELEGRAM_BATCH_SCHEMA = "eventedge-telegram-archive-batch-manifest-1.0"


@dataclass
class _LinkedVerification:
    """Bounded state accumulated while following sealed local file links."""

    digests: dict[Path, str] = field(default_factory=dict)
    visited_manifests: set[Path] = field(default_factory=set)
    telegram_channels: int = 0
    telegram_records: int = 0
    plan_files: int = 0


def verify_lineage(contract_path: Path, artifact_root: Path) -> dict[str, object]:
    """Verify sealed local bytes and the accepted-checkpoint dataset DAG.

    Raw Telegram files are checked recursively when their manifests expose
    hashes, but this only proves local byte integrity. It cannot prove that a
    historical Telegram page is the first point-in-time snapshot.
    """
    contract = read_json(contract_path)
    _validate_contract(contract)
    root = artifact_root.resolve()
    sections = ("source_seals", "checkpoint_seals", "datasets")
    entries = [entry for section in sections for entry in contract[section]]
    if len(entries) > MAXIMUM_CONTRACT_ENTRIES:
        raise ValueError("dataset lineage contract is unexpectedly large")
    identifiers = [str(entry["id"]) for entry in entries]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("dataset lineage contract contains duplicate ids")
    known = set(identifiers)
    linked = _LinkedVerification()
    verified = []
    for entry in entries:
        path = _resolve_local_path(root, entry["path"])
        digest = signal_dataset_sha256(path)
        expected_digest = _validate_sha256(entry["sha256"])
        if digest != expected_digest:
            raise ValueError(f"dataset lineage artifact changed: {entry['id']}")
        result = {"id": entry["id"], "path": entry["path"], "sha256": digest}
        if "rows" in entry:
            rows = sum(1 for _ in iter_jsonl(path, maximum_rows=MAXIMUM_DATASET_ROWS))
            if rows != entry["rows"]:
                raise ValueError(f"dataset lineage row count changed: {entry['id']}")
            result["rows"] = rows
        missing_inputs = set(entry.get("inputs", ())) - known
        if missing_inputs:
            raise ValueError(f"dataset lineage has unknown inputs: {sorted(missing_inputs)}")
        if path.name in {"complete.json", "manifest.json"}:
            _verify_manifest_links(path, root, linked)
        verified.append(result)
    final_id = contract["final_dataset_id"]
    if final_id not in {entry["id"] for entry in contract["datasets"]}:
        raise ValueError("dataset lineage final dataset is not declared")
    return {
        "schema_version": "eventedge-dataset-lineage-verification-1.1",
        "contract_sha256": signal_dataset_sha256(contract_path),
        "verified_files": len(verified),
        "verified_linked_files": len(linked.digests),
        "verified_datasets": len(contract["datasets"]),
        "verified_telegram_channels": linked.telegram_channels,
        "verified_telegram_records": linked.telegram_records,
        "verified_plan_files": linked.plan_files,
        "final_dataset_id": final_id,
        "accepted_checkpoint_boundary": contract["exact_replay_starts_at"],
        "raw_provenance_scope": (
            "declared_local_byte_integrity_only; historical_first_snapshot_and_"
            "complete_raw_replay_not_proven"
        ),
        "production_accessed": False,
        "remote_ydb_accessed": False,
    }


def _verify_manifest_links(
    manifest_path: Path,
    root: Path,
    state: _LinkedVerification,
    *,
    depth: int = 0,
) -> None:
    """Verify bounded children declared by one local manifest."""
    resolved_manifest = manifest_path.resolve()
    if resolved_manifest in state.visited_manifests:
        return
    if depth > MAXIMUM_LINK_DEPTH:
        raise ValueError("dataset lineage manifest nesting exceeds its bound")
    state.visited_manifests.add(resolved_manifest)
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError(f"dataset lineage manifest is not an object: {manifest_path}")
    if manifest.get("schema_version") == TELEGRAM_BATCH_SCHEMA:
        _verify_telegram_archive(manifest, manifest_path, root, state)
    fingerprints = manifest.get("sha256")
    if isinstance(fingerprints, dict):
        _verify_fingerprint_mapping(
            fingerprints,
            manifest_path,
            root,
            state,
            depth=depth,
        )
    if "plan_sha256" in manifest:
        _verify_linked_plan(manifest, manifest_path, root, state)


def _verify_telegram_archive(
    manifest: dict[str, Any],
    manifest_path: Path,
    root: Path,
    state: _LinkedVerification,
) -> None:
    """Verify every channel payload and its declared JSONL record count."""
    channels = manifest.get("channels")
    if (
        not isinstance(channels, list)
        or not 1 <= len(channels) <= MAXIMUM_TELEGRAM_CHANNELS
    ):
        raise ValueError("Telegram archive channel count is missing or out of bounds")
    seen_paths: set[Path] = set()
    for channel in channels:
        if not isinstance(channel, dict):
            raise ValueError("Telegram archive channel manifest is not an object")
        path = _resolve_root_relative_child(
            root,
            manifest_path.parent,
            channel.get("data_path"),
        )
        if path in seen_paths:
            raise ValueError("Telegram archive declares a duplicate data path")
        seen_paths.add(path)
        expected_digest = _validate_sha256(channel.get("data_sha256"))
        _verify_linked_file(path, expected_digest, state)
        expected_rows = channel.get("records_written")
        if (
            not isinstance(expected_rows, int)
            or isinstance(expected_rows, bool)
            or not 0 <= expected_rows <= MAXIMUM_TELEGRAM_RECORDS_PER_CHANNEL
        ):
            raise ValueError("Telegram archive record count is out of bounds")
        rows = sum(
            1
            for _ in iter_jsonl(
                path,
                maximum_rows=MAXIMUM_TELEGRAM_RECORDS_PER_CHANNEL,
            )
        )
        if rows != expected_rows:
            raise ValueError(f"Telegram archive record count changed: {path}")
        state.telegram_channels += 1
        state.telegram_records += rows


def _verify_fingerprint_mapping(
    fingerprints: dict[object, object],
    manifest_path: Path,
    root: Path,
    state: _LinkedVerification,
    *,
    depth: int,
) -> None:
    """Verify and recursively expand a generic complete.json hash mapping."""
    if not 1 <= len(fingerprints) <= MAXIMUM_LINKED_FILES_PER_MANIFEST:
        raise ValueError("completion-manifest fingerprint count is out of bounds")
    for name, raw_digest in fingerprints.items():
        child_path = _resolve_manifest_relative_child(
            root,
            manifest_path.parent,
            name,
        )
        expected_digest = _validate_sha256(raw_digest)
        _verify_linked_file(child_path, expected_digest, state)
        if child_path.name == "complete.json":
            _verify_manifest_links(
                child_path,
                root,
                state,
                depth=depth + 1,
            )


def _verify_linked_plan(
    manifest: dict[str, Any],
    manifest_path: Path,
    root: Path,
    state: _LinkedVerification,
) -> None:
    """Verify a same-directory plan linked by a completion seal."""
    plan_path = _resolve_manifest_relative_child(root, manifest_path.parent, "plan.json")
    _verify_linked_file(
        plan_path,
        _validate_sha256(manifest.get("plan_sha256")),
        state,
    )
    plan = read_json(plan_path)
    if not isinstance(plan, dict):
        raise ValueError(f"dataset lineage plan is not an object: {plan_path}")
    tickers = plan.get("tickers")
    completed_tickers = manifest.get("completed_tickers")
    if (
        not isinstance(tickers, list)
        or not tickers
        or len(tickers) > MAXIMUM_TELEGRAM_RECORDS_PER_CHANNEL
        or len(tickers) != len(set(tickers))
        or not all(isinstance(ticker, str) and ticker for ticker in tickers)
        or not isinstance(completed_tickers, int)
        or isinstance(completed_tickers, bool)
        or completed_tickers != len(tickers)
    ):
        raise ValueError("completion seal and linked plan ticker counts differ")
    state.plan_files += 1


def _verify_linked_file(
    path: Path,
    expected_digest: str,
    state: _LinkedVerification,
) -> None:
    """Verify one unique linked file under the global file-count bound."""
    previous_digest = state.digests.get(path)
    if previous_digest is not None:
        if previous_digest != expected_digest:
            raise ValueError(f"dataset lineage has conflicting hashes for: {path}")
        return
    if len(state.digests) >= MAXIMUM_LINKED_FILES:
        raise ValueError("dataset lineage linked-file count exceeds its bound")
    if file_sha256(path) != expected_digest:
        raise ValueError(f"dataset lineage linked artifact changed: {path}")
    state.digests[path] = expected_digest


def _validate_sha256(value: object) -> str:
    """Return a lowercase SHA-256 digest or reject the value."""
    digest = str(value)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("dataset lineage contains an invalid SHA-256 digest")
    return digest


def _validate_contract(contract: dict[str, object]) -> None:
    guards = contract.get("guards", {})
    if (
        contract.get("schema_version") != "eventedge-dataset-lineage-contract-2.0"
        or contract.get("status") != "historical_research_proxy"
        or not isinstance(guards, dict)
        or guards.get("production_access_allowed") is not False
        or guards.get("remote_ydb_access_allowed") is not False
        or guards.get("network_access_allowed") is not False
        or contract.get("exact_replay_starts_at") != "repaired_592"
        or not isinstance(contract.get("rebuild"), dict)
        or contract["rebuild"].get("schema_version")
        != "eventedge-canonical-dataset-rebuild-1.0"
    ):
        raise ValueError("dataset lineage contract is unsafe or incompatible")
    for section in ("source_seals", "checkpoint_seals", "datasets"):
        if not isinstance(contract.get(section), list):
            raise ValueError(f"dataset lineage section is missing: {section}")


def _resolve_local_path(root: Path, value: object) -> Path:
    relative = PurePosixPath(str(value))
    if (
        relative.is_absolute()
        or not relative.parts
        or ".." in relative.parts
        or relative.parts[0] not in {".local-artifacts", ".local-data"}
    ):
        raise ValueError("dataset lineage paths must be local and relative")
    allowed_directory = (root / relative.parts[0]).resolve()
    path = (root / Path(relative)).resolve()
    if not path.is_relative_to(allowed_directory) or not path.is_file():
        raise ValueError(f"dataset lineage path is missing or escapes root: {value}")
    return path


def _resolve_root_relative_child(root: Path, parent: Path, value: object) -> Path:
    """Resolve a root-relative manifest child and keep it under its manifest."""
    path = _resolve_local_path(root, value)
    resolved_parent = parent.resolve()
    if not path.is_relative_to(resolved_parent):
        raise ValueError("dataset lineage child escapes its manifest directory")
    return path


def _resolve_manifest_relative_child(
    root: Path,
    parent: Path,
    value: object,
) -> Path:
    """Resolve a complete.json child relative to the completion directory."""
    if not isinstance(value, str):
        raise ValueError("dataset lineage child path must be a string")
    relative = PurePosixPath(value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("dataset lineage child paths must be safe and relative")
    resolved_parent = parent.resolve()
    path = (resolved_parent / Path(relative)).resolve()
    allowed_directories = {
        (root / ".local-artifacts").resolve(),
        (root / ".local-data").resolve(),
    }
    if (
        not path.is_relative_to(resolved_parent)
        or not any(path.is_relative_to(directory) for directory in allowed_directories)
        or not path.is_file()
    ):
        raise ValueError("dataset lineage child is missing or escapes its manifest")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--contract",
        type=Path,
        default=Path("EventEdge/DATASET_LINEAGE_CONTRACT.json"),
    )
    parser.add_argument("--artifact-root", type=Path, default=Path("."))
    args = parser.parse_args()
    report = verify_lineage(args.contract, args.artifact_root)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
