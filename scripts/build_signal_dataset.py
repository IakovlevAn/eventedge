"""Label local event candidates under an explicit execution timing protocol."""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

from eventedge_research.research_artifacts import write_json
from eventedge_research.signal_dataset import signal_dataset_sha256, write_signal_dataset
from eventedge_research.signal_dataset_builder import (
    MARKET_TIME_ZONE,
    MAX_ENTRY_OBSERVATION_LAG,
    build_signal_dataset,
    load_market_open_observations,
    load_signal_event_candidates,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a local EventEdge event/ticker dataset from reviewed events and opens"
    )
    parser.add_argument("--candidates", type=Path, action="append", required=True)
    parser.add_argument("--market-opens", type=Path, action="append", required=True)
    parser.add_argument("--benchmark-opens", type=Path)
    parser.add_argument("--benchmark-id", default="IMOEX")
    parser.add_argument("--from-decision-date", type=date.fromisoformat)
    parser.add_argument("--till-decision-date", type=date.fromisoformat)
    parser.add_argument(
        "--maximum-entry-lag-seconds",
        type=int,
        default=int(MAX_ENTRY_OBSERVATION_LAG.total_seconds()),
        help=(
            "Maximum delay from decision to first open, not an intentional wait; "
            "use 60 with one-minute opens for immediate-entry research. "
            "Default preserves the legacy next-session protocol (10 days)."
        ),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--allow-synthetic",
        action="store_true",
        help="Allow synthetic_test market observations; intended only for fixtures and CI",
    )
    args = parser.parse_args()
    maximum_entry_lag = timedelta(seconds=args.maximum_entry_lag_seconds)
    if not timedelta(0) < maximum_entry_lag <= MAX_ENTRY_OBSERVATION_LAG:
        parser.error("--maximum-entry-lag-seconds must be between 1 and 864000")
    input_sha256 = {
        "candidates": _fingerprints(args.candidates, "candidate"),
        "market_opens": _fingerprints(args.market_opens, "market-open"),
    }
    if args.benchmark_opens is not None:
        input_sha256["benchmark_opens"] = signal_dataset_sha256(
            args.benchmark_opens
        )

    candidates = [
        candidate for path in args.candidates for candidate in load_signal_event_candidates(path)
    ]
    candidates_before_date_filter = len(candidates)
    candidates = [
        candidate
        for candidate in candidates
        if (
            args.from_decision_date is None
            or candidate.decision_at.astimezone(MARKET_TIME_ZONE).date() >= args.from_decision_date
        )
        and (
            args.till_decision_date is None
            or candidate.decision_at.astimezone(MARKET_TIME_ZONE).date() <= args.till_decision_date
        )
    ]
    if not candidates:
        raise ValueError("no signal candidates remain after the decision-date filter")
    observations = [
        observation
        for path in args.market_opens
        for observation in load_market_open_observations(
            path,
            allow_synthetic=args.allow_synthetic,
        )
    ]
    benchmark_observations = (
        load_market_open_observations(
            args.benchmark_opens,
            allow_synthetic=args.allow_synthetic,
        )
        if args.benchmark_opens is not None
        else None
    )
    examples, report = build_signal_dataset(
        candidates,
        observations,
        benchmark_observations=benchmark_observations,
        benchmark_id=args.benchmark_id if benchmark_observations is not None else None,
        maximum_entry_lag=maximum_entry_lag,
    )
    _verify_unchanged(input_sha256, args)
    report["candidate_date_filter"] = {
        "from_date": (
            args.from_decision_date.isoformat() if args.from_decision_date is not None else None
        ),
        "till_date": (
            args.till_decision_date.isoformat() if args.till_decision_date is not None else None
        ),
        "input_candidates": candidates_before_date_filter,
        "selected_candidates": len(candidates),
    }
    write_signal_dataset(args.output, examples)
    report["input_sha256"] = input_sha256
    report["dataset_path"] = str(args.output)
    report["dataset_sha256"] = signal_dataset_sha256(args.output)
    serialized = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.report is not None:
        write_json(args.report, report)
    print(serialized, end="")


def _fingerprints(paths: list[Path], kind: str) -> dict[str, str]:
    """Hash unique inputs before reading them."""
    result = {str(path): signal_dataset_sha256(path) for path in paths}
    if len(result) != len(paths):
        raise ValueError(f"{kind} input paths must be unique")
    return result


def _verify_unchanged(
    expected: dict[str, object],
    args: argparse.Namespace,
) -> None:
    """Reject source replacement during dataset construction."""
    current: dict[str, object] = {
        "candidates": _fingerprints(args.candidates, "candidate"),
        "market_opens": _fingerprints(args.market_opens, "market-open"),
    }
    if args.benchmark_opens is not None:
        current["benchmark_opens"] = signal_dataset_sha256(args.benchmark_opens)
    if current != expected:
        raise ValueError("signal dataset input changed while being read")


if __name__ == "__main__":
    main()
