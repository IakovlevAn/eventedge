from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

from eventedge_research.research_artifacts import write_json
from eventedge_research.signal_dataset import (
    load_signal_dataset,
    signal_dataset_report,
    signal_dataset_sha256,
    split_signal_dataset,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit a local point-in-time EventEdge signal dataset"
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--validation-from", type=datetime.fromisoformat, required=True)
    parser.add_argument("--test-from", type=datetime.fromisoformat, required=True)
    parser.add_argument("--test-until", type=datetime.fromisoformat, required=True)
    parser.add_argument("--embargo-hours", type=float, default=72)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--allow-synthetic",
        action="store_true",
        help="Allow synthetic_test outcomes; intended only for fixtures and CI",
    )
    args = parser.parse_args()

    examples = load_signal_dataset(args.dataset, allow_synthetic=args.allow_synthetic)
    embargo = timedelta(hours=args.embargo_hours)
    split = split_signal_dataset(
        examples,
        validation_from=args.validation_from,
        test_from=args.test_from,
        test_until=args.test_until,
        embargo=embargo,
    )
    report = signal_dataset_report(
        examples,
        split,
        dataset_sha256=signal_dataset_sha256(args.dataset),
        validation_from=args.validation_from,
        test_from=args.test_from,
        test_until=args.test_until,
        embargo=embargo,
    )
    serialized = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        write_json(args.output, report)
    print(serialized, end="")


if __name__ == "__main__":
    main()
