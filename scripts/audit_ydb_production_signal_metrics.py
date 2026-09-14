"""Audit the current production signal metrics from immutable local YDB exports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from eventedge_research.research_artifacts import write_json
from eventedge_research.signal_dataset import signal_dataset_sha256
from eventedge_research.signal_production_metrics import (
    build_current_production_metrics,
    current_production_epoch,
)
from eventedge_research.ydb_signal_export import (
    iter_ydb_export_rows,
    verify_ydb_export,
    ydb_export_read_consistency,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signal-export-dir", type=Path, required=True)
    parser.add_argument("--evaluation-export-dir", type=Path, required=True)
    parser.add_argument("--config-version", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("refusing to overwrite a production metric audit")

    signal_manifest = verify_ydb_export(args.signal_export_dir)
    evaluation_manifest = verify_ydb_export(
        args.evaluation_export_dir,
        required_tables=("evaluation_epochs",),
    )
    signals = list(iter_ydb_export_rows(args.signal_export_dir / "signals.jsonl"))
    epochs = list(
        iter_ydb_export_rows(args.evaluation_export_dir / "evaluation_epochs.jsonl")
    )
    current_epoch = current_production_epoch(
        epochs,
        config_version=args.config_version,
    )
    model_version = str(current_epoch["model_version"])
    config_version = current_epoch["config_version"]
    if type(config_version) is not int:
        raise ValueError("current config_version is not an integer")
    current_signals = [
        row
        for row in signals
        if row.get("model_version") == model_version and row.get("config_version") == config_version
    ]
    news_ids = {str(row["news_id"]) for row in current_signals}
    news_by_id = {
        str(row["news_id"]): row
        for row in iter_ydb_export_rows(args.signal_export_dir / "news_items.jsonl")
        if row.get("news_id") in news_ids
    }
    feature_sets = [
        row
        for row in iter_ydb_export_rows(args.signal_export_dir / "feature_sets.jsonl")
        if row.get("news_id") in news_ids
    ]
    report = build_current_production_metrics(
        signals,
        epochs,
        news_by_id,
        feature_sets,
        config_version=args.config_version,
    )
    report["inputs"] = {
        "signal_export_dir": str(args.signal_export_dir),
        "signal_export_completed_at": signal_manifest["completed_at"],
        "signal_export_manifest_schema_version": signal_manifest["schema_version"],
        "signal_export_read_consistency": ydb_export_read_consistency(signal_manifest),
        "signal_export_tables": signal_manifest["tables"],
        "evaluation_export_dir": str(args.evaluation_export_dir),
        "evaluation_export_completed_at": evaluation_manifest["completed_at"],
        "evaluation_export_manifest_schema_version": evaluation_manifest["schema_version"],
        "evaluation_export_read_consistency": ydb_export_read_consistency(
            evaluation_manifest
        ),
        "evaluation_export_tables": evaluation_manifest["tables"],
        "cross_export_consistency": {
            "shared_snapshot": False,
            "interpretation": (
                "signal and evaluation files are separate exports; their completion "
                "timestamps do not establish one coherent point-in-time database snapshot"
            ),
        },
        "source_sha256": {
            path: signal_dataset_sha256(Path(path))
            for path in (
                "scripts/audit_ydb_production_signal_metrics.py",
                "src/eventedge_research/signal_production_metrics.py",
                "src/eventedge/evals.py",
            )
        },
    }
    write_json(args.output, report)
    print(
        json.dumps(
            {
                "status": "complete",
                "output": str(args.output),
                "sha256": signal_dataset_sha256(args.output),
                "model_version": report["selection"]["model_version"],
                "config_version": report["selection"]["config_version"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
