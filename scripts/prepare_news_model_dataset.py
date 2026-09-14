"""Prepare portable model inputs from a local labeled dataset and MOEX opens."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

from eventedge_research.news_model_pipeline import prepare_news_model_dataset


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--stock-opens", type=Path, action="append", required=True)
    parser.add_argument("--benchmark-opens", type=Path, required=True)
    parser.add_argument("--benchmark-id", default="IMOEX2")
    parser.add_argument("--validation-from", type=datetime.fromisoformat, required=True)
    parser.add_argument("--evaluation-from", type=datetime.fromisoformat, required=True)
    parser.add_argument("--evaluation-until", type=datetime.fromisoformat, required=True)
    parser.add_argument("--embargo-hours", type=int, default=72)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--allow-synthetic",
        action="store_true",
        help="Allow synthetic_test labels; intended only for fixtures and CI.",
    )
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    if not 0 <= args.embargo_hours <= 24 * 30:
        raise ValueError("--embargo-hours must be between 0 and 720")
    report = prepare_news_model_dataset(
        args.dataset,
        args.stock_opens,
        args.benchmark_opens,
        args.output,
        validation_from=args.validation_from,
        evaluation_from=args.evaluation_from,
        evaluation_until=args.evaluation_until,
        benchmark_id=args.benchmark_id,
        embargo=timedelta(hours=args.embargo_hours),
        allow_synthetic=args.allow_synthetic,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
