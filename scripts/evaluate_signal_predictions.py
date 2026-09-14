"""Evaluate point-in-time directional predictions on a frozen partition."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

from eventedge_research.signal_benchmark import (
    EvaluationTarget,
    evaluate_signal_predictions,
    load_signal_predictions,
    signal_target_return,
)
from eventedge_research.signal_dataset import (
    load_signal_dataset,
    signal_dataset_sha256,
    split_signal_dataset,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--validation-from", type=datetime.fromisoformat, required=True)
    parser.add_argument("--test-from", type=datetime.fromisoformat, required=True)
    parser.add_argument("--test-until", type=datetime.fromisoformat, required=True)
    parser.add_argument("--embargo-hours", type=float, default=72)
    parser.add_argument(
        "--target",
        choices=tuple(target.value for target in EvaluationTarget),
        default=EvaluationTarget.RAW_RETURN.value,
    )
    parser.add_argument(
        "--partition",
        choices=("validation", "test", "shadow"),
        default="test",
    )
    parser.add_argument(
        "--allow-synthetic",
        action="store_true",
        help="Allow synthetic_test outcomes; intended only for fixtures and CI",
    )
    args = parser.parse_args()

    target = EvaluationTarget(args.target)
    examples = [
        example
        for example in load_signal_dataset(
            args.dataset,
            allow_synthetic=args.allow_synthetic,
        )
        if signal_target_return(example, target) is not None
    ]
    dataset_digest = signal_dataset_sha256(args.dataset)
    split = split_signal_dataset(
        examples,
        validation_from=args.validation_from,
        test_from=args.test_from,
        test_until=args.test_until,
        embargo=timedelta(hours=args.embargo_hours),
    )
    partition = {
        "validation": split.validation,
        "test": split.test,
        "shadow": split.future,
    }[args.partition]
    report = evaluate_signal_predictions(
        partition,
        load_signal_predictions(args.predictions),
        dataset_sha256=dataset_digest,
        expected_partition=args.partition,
        target=target,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
