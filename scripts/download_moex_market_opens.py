from __future__ import annotations

import argparse
import json
import time
from datetime import date
from pathlib import Path

from eventedge_research.moex_market_data import download_moex_market_opens
from eventedge_research.research_artifacts import write_json
from eventedge_research.signal_dataset_builder import (
    MARKET_TIME_ZONE,
    SignalEventCandidate,
    load_signal_event_candidates,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download bounded MOEX ISS opens for local signal labels"
    )
    parser.add_argument("--candidates", type=Path, action="append", required=True)
    parser.add_argument("--from-date", type=date.fromisoformat, required=True)
    parser.add_argument("--till-date", type=date.fromisoformat, required=True)
    parser.add_argument("--interval-minutes", type=int, choices=(1, 10), default=10)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--request-delay-seconds", type=float, default=0.05)
    parser.add_argument("--maximum-pages-per-ticker", type=int, default=500)
    parser.add_argument("--maximum-rows-per-ticker", type=int, default=100_000)
    parser.add_argument(
        "--max-run-seconds",
        type=_bounded_run_seconds,
        default=3_600.0,
        help="Hard wall-clock budget for this invocation (60-21600 seconds)",
    )
    parser.add_argument(
        "--security-id",
        action="append",
        default=[],
        metavar="CANONICAL=MOEX_ID",
        help="Map a canonical dataset ticker to its historical MOEX security id",
    )
    args = parser.parse_args()
    run_deadline_monotonic = time.monotonic() + args.max_run_seconds

    candidates = [
        candidate for path in args.candidates for candidate in load_signal_event_candidates(path)
    ]
    tickers = _candidate_tickers(
        candidates,
        from_date=args.from_date,
        till_date=args.till_date,
    )
    security_ids = _security_ids(args.security_id)
    report = download_moex_market_opens(
        tickers=tickers,
        from_date=args.from_date,
        till_date=args.till_date,
        interval_minutes=args.interval_minutes,
        output_path=args.output,
        request_delay_seconds=args.request_delay_seconds,
        maximum_pages_per_ticker=args.maximum_pages_per_ticker,
        maximum_rows_per_ticker=args.maximum_rows_per_ticker,
        run_deadline_monotonic=run_deadline_monotonic,
        security_ids=security_ids,
    )
    report["candidate_paths"] = [str(path) for path in args.candidates]
    report["candidate_rows_in_date_window"] = sum(
        args.from_date
        <= candidate.decision_at.astimezone(MARKET_TIME_ZONE).date()
        <= args.till_date
        for candidate in candidates
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


def _candidate_tickers(
    candidates: list[SignalEventCandidate],
    *,
    from_date: date,
    till_date: date,
) -> set[str]:
    tickers = {
        candidate.ticker
        for candidate in candidates
        if from_date <= candidate.decision_at.astimezone(MARKET_TIME_ZONE).date() <= till_date
    }
    if not tickers:
        raise ValueError("no candidate tickers occur in the requested date window")
    return tickers


def _security_ids(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        canonical, separator, security_id = value.partition("=")
        if not separator or not canonical or not security_id:
            raise ValueError(f"invalid --security-id mapping: {value!r}")
        canonical = canonical.upper()
        if canonical in result:
            raise ValueError(f"duplicate --security-id mapping for {canonical}")
        result[canonical] = security_id.upper()
    return result


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
