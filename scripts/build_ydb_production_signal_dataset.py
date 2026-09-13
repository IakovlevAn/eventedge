"""Build a strict local signal dataset from read-only YDB export files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from eventedge_research.research_artifacts import write_json
from eventedge_research.signal_dataset import signal_dataset_sha256, write_signal_dataset
from eventedge_research.ydb_production_signal_dataset import build_ydb_production_signal_dataset
from eventedge_research.ydb_signal_export import (
    iter_ydb_export_rows,
    verify_ydb_export,
    ydb_export_read_consistency,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signal-export-dir", type=Path, required=True)
    parser.add_argument("--evaluation-export-dir", type=Path, required=True)
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--config-version", type=int, required=True)
    parser.add_argument("--cohort", choices=("live", "retrospective", "all"), default="live")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.report.exists():
        raise FileExistsError("refusing to overwrite a production signal dataset artifact")

    signal_manifest = verify_ydb_export(args.signal_export_dir)
    evaluation_manifest = verify_ydb_export(
        args.evaluation_export_dir,
        required_tables=("evaluation_epochs",),
    )
    signals = list(iter_ydb_export_rows(args.signal_export_dir / "signals.jsonl"))
    epochs = list(
        iter_ydb_export_rows(args.evaluation_export_dir / "evaluation_epochs.jsonl")
    )
    selected_signals = [
        row
        for row in signals
        if row.get("model_version") == args.model_version
        and row.get("config_version") == args.config_version
    ]
    news_ids = {str(row["news_id"]) for row in selected_signals}
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
    examples, report = build_ydb_production_signal_dataset(
        signals,
        epochs,
        news_by_id,
        feature_sets,
        model_version=args.model_version,
        config_version=args.config_version,
        cohort=args.cohort,
    )
    write_signal_dataset(args.output, examples)
    report["inputs"] = {
        "signal_export_completed_at": signal_manifest["completed_at"],
        "signal_export_manifest_schema_version": signal_manifest["schema_version"],
        "signal_export_read_consistency": ydb_export_read_consistency(signal_manifest),
        "evaluation_export_completed_at": evaluation_manifest["completed_at"],
        "evaluation_export_manifest_schema_version": evaluation_manifest["schema_version"],
        "evaluation_export_read_consistency": ydb_export_read_consistency(
            evaluation_manifest
        ),
        "cross_export_consistency": {
            "shared_snapshot": False,
            "interpretation": (
                "signal and evaluation files are separate exports; their completion "
                "timestamps do not establish one coherent point-in-time database snapshot"
            ),
        },
    }
    report["dataset_path"] = str(args.output)
    report["dataset_sha256"] = signal_dataset_sha256(args.output)
    write_json(args.report, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
