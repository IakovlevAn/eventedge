from __future__ import annotations

import argparse
import json
from pathlib import Path

from eventedge.quality import (
    evaluate_current_router,
    load_quality_dataset,
    temporal_group_split,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate the current EventEdge router on human quality labels"
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument(
        "--allow-synthetic",
        action="store_true",
        help="Allow synthetic_test labels; intended only for fixtures and CI",
    )
    parser.add_argument(
        "--include-split",
        action="store_true",
        help="Include chronological group-split sizes in the report",
    )
    args = parser.parse_args()

    examples = load_quality_dataset(
        args.dataset,
        allow_synthetic=args.allow_synthetic,
    )
    report = evaluate_current_router(examples)
    if args.include_split:
        split = temporal_group_split(examples)
        report["split"] = {
            "train": len(split.train),
            "validation": len(split.validation),
            "test": len(split.test),
            "purged": len(split.purged),
            "train_events": len(split.event_ids()[0]),
            "validation_events": len(split.event_ids()[1]),
            "test_events": len(split.event_ids()[2]),
            "purged_events": len(split.event_ids()[3]),
        }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
