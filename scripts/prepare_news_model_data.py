"""Prepare frozen local inputs for the retained EventEdge news models."""

from __future__ import annotations

import argparse
import json
import signal
from pathlib import Path
from types import FrameType

from eventedge_research.news_model_data import prepare_benchmark


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument(
        "--artifact-root",
        type=Path,
        help="Root containing the contract's relative .local-data and .local-artifacts paths.",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--maximum-run-seconds", type=int, default=600)
    args = parser.parse_args()
    if not 30 <= args.maximum_run_seconds <= 1_800:
        parser.error("maximum run time must be between 30 and 1800 seconds")
    previous = signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(args.maximum_run_seconds)
    try:
        report = prepare_benchmark(
            args.contract,
            args.output,
            artifact_root=args.artifact_root,
        )
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


def _timeout(signum: int, frame: FrameType | None) -> None:
    raise TimeoutError("news model preparation exceeded its hard wall-clock limit")


if __name__ == "__main__":
    main()
