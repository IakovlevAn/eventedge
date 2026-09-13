"""Fit and score the retained materiality and direction models locally."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from eventedge_research.news_model_benchmark import fit_benchmark, score_benchmark


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    fit_parser = subparsers.add_parser("fit")
    fit_parser.add_argument("--fit-bundle", type=Path, required=True)
    fit_parser.add_argument("--expected-fit-bundle-sha256", required=True)
    fit_parser.add_argument("--output", type=Path, required=True)
    score_parser = subparsers.add_parser("score")
    score_parser.add_argument("--output", type=Path, required=True)
    score_parser.add_argument("--outcomes", type=Path, required=True)
    score_parser.add_argument("--outcomes-sha256", required=True)
    score_parser.add_argument("--scoring-seal", type=Path, required=True)
    score_parser.add_argument("--contract", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    if args.command == "fit":
        report = fit_benchmark(
            args.fit_bundle,
            args.output,
            expected_fit_bundle_sha256=args.expected_fit_bundle_sha256,
        )
    else:
        report = score_benchmark(
            args.output,
            args.outcomes,
            args.scoring_seal,
            args.contract,
            expected_outcomes_sha256=args.outcomes_sha256,
        )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
