"""Evaluate reproducible directional controls on a frozen partition."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from eventedge_research.signal_baselines import SignalBaseline, build_baseline_predictions
from eventedge_research.signal_benchmark import (
    EvaluationTarget,
    evaluate_signal_predictions,
    signal_target_return,
    write_signal_predictions,
)
from eventedge_research.signal_dataset import (
    SignalDatasetExample,
    SignalDatasetSplit,
    load_signal_dataset,
    signal_dataset_sha256,
    split_signal_dataset,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
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
        "--baseline",
        action="append",
        choices=tuple(item.value for item in SignalBaseline),
        dest="baselines",
        help="Repeat to select controls; defaults to every baseline",
    )
    parser.add_argument("--predictions-dir", type=Path)
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
    training, evaluation = _partitions(split, args.partition)
    selected = [SignalBaseline(value) for value in args.baselines or SignalBaseline]
    reports = {}
    for baseline in selected:
        predictions = build_baseline_predictions(
            baseline,
            training_examples=training,
            evaluation_examples=evaluation,
            partition=args.partition,
            dataset_sha256=dataset_digest,
            target=target,
        )
        if args.predictions_dir is not None:
            write_signal_predictions(
                args.predictions_dir / f"{args.partition}-{baseline.value}.jsonl",
                predictions,
            )
        report = evaluate_signal_predictions(
            evaluation,
            predictions,
            dataset_sha256=dataset_digest,
            expected_partition=args.partition,
            target=target,
        )
        reports[baseline.value] = report

    print(
        json.dumps(
            {
                "schema_version": "signal-baseline-suite-1.0",
                "dataset_sha256": dataset_digest,
                "partition": args.partition,
                "evaluation_target": target.value,
                "reports": reports,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _partitions(
    split: SignalDatasetSplit,
    partition: Literal["validation", "test", "shadow"],
) -> tuple[tuple[SignalDatasetExample, ...], tuple[SignalDatasetExample, ...]]:
    if partition == "validation":
        return split.train, split.validation
    if partition == "test":
        return (*split.train, *split.validation), split.test
    return (*split.train, *split.validation), split.future


if __name__ == "__main__":
    main()
