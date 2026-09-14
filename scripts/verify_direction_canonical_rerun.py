"""Verify the tracked canonical five-stage direction rerun."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from eventedge_research.direction_canonical_rerun import (
    canonical_rerun_summary,
    load_canonical_rerun_contract,
    verify_canonical_rerun_results,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--contract",
        type=Path,
        default=PROJECT_ROOT / "EventEdge/NEWS_DIRECTION_CANONICAL_RERUN_CONTRACT.json",
    )
    parser.add_argument(
        "--results",
        type=Path,
        help="Optional complete ignored results.json to compare with tracked headlines",
    )
    arguments = parser.parse_args()
    contract = load_canonical_rerun_contract(arguments.contract, project_root=PROJECT_ROOT)
    if arguments.results is not None:
        verify_canonical_rerun_results(contract, arguments.results)
    print(json.dumps(canonical_rerun_summary(contract), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
