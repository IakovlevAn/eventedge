"""Verify the tracked five-stage direction experiment archive."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from eventedge_research.direction_experiment_archive import (
    archive_summary,
    load_direction_experiment_archive,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--contract",
        type=Path,
        default=PROJECT_ROOT / "EventEdge/NEWS_DIRECTION_EXPERIMENTS_CONTRACT.json",
    )
    arguments = parser.parse_args()
    contract = load_direction_experiment_archive(
        arguments.contract,
        project_root=PROJECT_ROOT,
    )
    print(json.dumps(archive_summary(contract), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
