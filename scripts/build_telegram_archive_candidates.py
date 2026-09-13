from __future__ import annotations

import argparse
import json
from pathlib import Path

from eventedge_research.research_artifacts import file_sha256, write_json
from eventedge_research.telegram_archive import (
    MAX_ARCHIVE_ROWS,
    build_telegram_archive_candidates,
    iter_telegram_archive_records,
    load_verified_telegram_archive_manifests,
)
from eventedge_research.ydb_signal_candidates import write_signal_event_candidates


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build research signal candidates from consented Telegram archive JSONL"
        )
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--archive", type=Path, action="append")
    source.add_argument("--manifest", type=Path, action="append")
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path("."),
        help="Root used to resolve and contain paths declared by --manifest",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--assumed-receipt-lag-minutes", type=int, default=5)
    parser.add_argument("--max-rows", type=int, default=MAX_ARCHIVE_ROWS)
    parser.add_argument(
        "--confirm-content-permission",
        action="store_true",
        help="Reassert that every input channel is authorized for this ML use",
    )
    args = parser.parse_args()
    if not args.confirm_content_permission:
        parser.error("--confirm-content-permission is required")

    if args.manifest:
        archive_paths, source_verification = load_verified_telegram_archive_manifests(
            args.manifest,
            artifact_root=args.artifact_root,
        )
    else:
        archive_paths = args.archive
        source_verification = {
            "mode": "direct_unsealed_files",
            "verified": False,
            "warning": "rows are validated, but no downloader manifest was supplied",
            "sha256": {str(path): file_sha256(path) for path in archive_paths},
        }
    records = iter_telegram_archive_records(archive_paths, max_rows=args.max_rows)
    candidates, report = build_telegram_archive_candidates(
        records,
        assumed_receipt_lag_minutes=args.assumed_receipt_lag_minutes,
        max_rows=args.max_rows,
    )
    _verify_sources_unchanged(
        args,
        archive_paths=archive_paths,
        source_verification=source_verification,
    )
    report["archive_paths"] = [str(path) for path in archive_paths]
    report["source_verification"] = source_verification
    report["candidate_sha256"] = write_signal_event_candidates(
        args.output,
        candidates,
    )
    report["candidate_path"] = str(args.output)
    serialized = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.report is not None:
        write_json(args.report, report)
    print(serialized, end="")


def _verify_sources_unchanged(
    args: argparse.Namespace,
    *,
    archive_paths: list[Path],
    source_verification: dict[str, object],
) -> None:
    """Reject raw archive replacement between verification and admission."""
    if args.manifest:
        current_paths, current = load_verified_telegram_archive_manifests(
            args.manifest,
            artifact_root=args.artifact_root,
        )
        if current_paths != archive_paths or current != source_verification:
            raise ValueError("Telegram manifest inputs changed while being read")
        return
    current_sha256 = {str(path): file_sha256(path) for path in archive_paths}
    if current_sha256 != source_verification["sha256"]:
        raise ValueError("Telegram archive input changed while being read")


if __name__ == "__main__":
    main()
