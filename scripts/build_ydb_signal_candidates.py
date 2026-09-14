from __future__ import annotations

import argparse
import json
from datetime import timedelta
from pathlib import Path

from eventedge_research.research_artifacts import write_json
from eventedge_research.ydb_signal_candidates import (
    build_ydb_signal_candidates,
    write_signal_event_candidates,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build outcome-free signal candidates from a verified local YDB export"
    )
    parser.add_argument("--export-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--max-rows", type=int, default=100_000)
    parser.add_argument(
        "--use-stored-feature-sets",
        action="store_true",
        help=(
            "Use payload-bound feature_sets and move decision_at to their actual "
            "availability time"
        ),
    )
    parser.add_argument(
        "--maximum-feature-latency-seconds",
        type=int,
        default=300,
        help="Reject stored semantic features produced too late for a news decision",
    )
    parser.add_argument(
        "--maximum-decision-delay-seconds",
        type=int,
        default=300,
        help=(
            "Reject a final rule/stored-feature decision produced more than this "
            "many seconds after publication"
        ),
    )
    parser.add_argument(
        "--maximum-publication-age-hours",
        type=float,
        help=(
            "Exclude backlog received more than this many hours after publication; "
            "disabled by default"
        ),
    )
    parser.add_argument(
        "--include-telegram-with-permission",
        action="store_true",
        help=(
            "Assert explicit informed permission for every included Telegram source; "
            "Telegram rows are excluded by default"
        ),
    )
    args = parser.parse_args()

    candidates, report = build_ydb_signal_candidates(
        args.export_dir,
        max_rows=args.max_rows,
        include_telegram_with_permission=args.include_telegram_with_permission,
        use_stored_feature_sets=args.use_stored_feature_sets,
        maximum_feature_latency=timedelta(
            seconds=args.maximum_feature_latency_seconds
        ),
        maximum_decision_delay=timedelta(
            seconds=args.maximum_decision_delay_seconds
        ),
        maximum_publication_age=(
            timedelta(hours=args.maximum_publication_age_hours)
            if args.maximum_publication_age_hours is not None
            else None
        ),
    )
    report["candidate_sha256"] = write_signal_event_candidates(args.output, candidates)
    report["candidate_path"] = str(args.output)
    serialized = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.report is not None:
        write_json(args.report, report)
    print(serialized, end="")


if __name__ == "__main__":
    main()
