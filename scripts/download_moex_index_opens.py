from __future__ import annotations

import argparse
import json
import time
from datetime import date
from pathlib import Path

from eventedge_research.moex_market_data import MoexCandleSource, download_moex_market_opens
from eventedge_research.research_artifacts import write_json


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download bounded MOEX ISS index opens for local benchmarks"
    )
    parser.add_argument("--index-id", action="append", dest="index_ids")
    parser.add_argument("--from-date", type=date.fromisoformat, required=True)
    parser.add_argument("--till-date", type=date.fromisoformat, required=True)
    parser.add_argument("--interval-minutes", type=int, choices=(1, 10), default=10)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--request-delay-seconds", type=float, default=0.05)
    parser.add_argument("--maximum-pages-per-index", type=int, default=500)
    parser.add_argument("--maximum-rows-per-index", type=int, default=100_000)
    parser.add_argument(
        "--max-run-seconds",
        type=_bounded_run_seconds,
        default=3_600.0,
        help="Hard wall-clock budget for this invocation (60-21600 seconds)",
    )
    args = parser.parse_args()
    run_deadline_monotonic = time.monotonic() + args.max_run_seconds

    report = download_moex_market_opens(
        tickers=args.index_ids or {"IMOEX"},
        from_date=args.from_date,
        till_date=args.till_date,
        interval_minutes=args.interval_minutes,
        output_path=args.output,
        request_delay_seconds=args.request_delay_seconds,
        maximum_pages_per_ticker=args.maximum_pages_per_index,
        maximum_rows_per_ticker=args.maximum_rows_per_index,
        source=MoexCandleSource.SNDX_INDEX,
        run_deadline_monotonic=run_deadline_monotonic,
    )
    report["output_path"] = str(args.output)
    report["runtime_safety"] = {
        "max_run_seconds": args.max_run_seconds,
        "request_delay_seconds": args.request_delay_seconds,
    }
    serialized = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.report is not None:
        write_json(args.report, report)
    print(serialized, end="")


def _bounded_run_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected a number of seconds") from error
    if not 60 <= seconds <= 21_600:
        raise argparse.ArgumentTypeError("run time must be between 60 and 21600 seconds")
    return seconds


if __name__ == "__main__":
    main()
