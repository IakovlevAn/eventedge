from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from eventedge.events import EVENT_STOP_WORDS
from eventedge_research.research_artifacts import write_json
from eventedge_research.signal_dataset import (
    SignalDatasetExample,
    load_signal_dataset,
    signal_dataset_schema_version,
    signal_dataset_sha256,
    signal_information_group_id,
    write_signal_dataset,
)

CROSS_DATASET_EVENT_WINDOW = timedelta(hours=48)
CROSS_DATASET_TITLE_OVERLAP = 0.6


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge local signal datasets with global identity validation"
    )
    parser.add_argument("--dataset", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--allow-synthetic", action="store_true")
    args = parser.parse_args()

    if len(args.dataset) < 2:
        raise ValueError("at least two signal datasets are required")
    if args.output.exists():
        raise FileExistsError(f"refusing to overwrite merged dataset: {args.output}")
    datasets = [
        (
            path,
            load_signal_dataset(
                path,
                allow_synthetic=args.allow_synthetic,
            ),
        )
        for path in args.dataset
    ]
    examples = [example for _, rows in datasets for example in rows]
    dataset_schema_version = signal_dataset_schema_version(examples)
    _validate_cross_dataset_identities(examples)
    overlap_report = _cross_dataset_event_overlap(datasets)
    if overlap_report["pairs"]:
        first = overlap_report["samples"][0]
        raise ValueError(
            "cross-dataset near-duplicate event detected; merge candidates before "
            "market labeling or remove the duplicate: "
            f"{first['left_example_id']}/{first['right_example_id']}"
        )
    write_signal_dataset(args.output, examples)
    report = {
        "schema_version": "signal-dataset-merge-report-1.0",
        "dataset_schema_version": dataset_schema_version,
        "input_sha256": {
            str(path): signal_dataset_sha256(path) for path in args.dataset
        },
        "output_path": str(args.output),
        "output_sha256": signal_dataset_sha256(args.output),
        "observations": len(examples),
        "events": len({example.event_id for example in examples}),
        "information_groups": len(
            {signal_information_group_id(example) for example in examples}
        ),
        "cross_dataset_event_overlap": overlap_report,
        "decision_at": {
            "minimum": min(example.decision_at for example in examples).isoformat(),
            "maximum": max(example.decision_at for example in examples).isoformat(),
        },
    }
    serialized = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.report is not None:
        write_json(args.report, report)
    print(serialized, end="")


def _validate_cross_dataset_identities(
    examples: Sequence[SignalDatasetExample],
) -> None:
    seen_ids: set[str] = set()
    seen_event_tickers: set[tuple[str, str]] = set()
    for example in examples:
        if example.id in seen_ids:
            raise ValueError(f"duplicate signal dataset id: {example.id}")
        identity = (example.event_id, example.ticker)
        if identity in seen_event_tickers:
            raise ValueError(
                "duplicate signal dataset event/ticker: "
                f"{example.event_id}/{example.ticker}"
            )
        seen_ids.add(example.id)
        seen_event_tickers.add(identity)


def _cross_dataset_event_overlap(
    datasets: Sequence[tuple[Path, Sequence[SignalDatasetExample]]],
) -> dict[str, object]:
    """Detect likely copies that would overweight one event across input files."""
    by_ticker: defaultdict[
        str,
        list[tuple[int, Path, SignalDatasetExample]],
    ] = defaultdict(list)
    for dataset_index, (path, examples) in enumerate(datasets):
        for example in examples:
            by_ticker[example.ticker].append((dataset_index, path, example))

    samples: list[dict[str, str]] = []
    pair_count = 0
    for ticker_rows in by_ticker.values():
        ordered = sorted(
            ticker_rows,
            key=lambda item: (
                item[2].published_at,
                item[0],
                item[2].id,
            ),
        )
        for index, current in enumerate(ordered):
            for previous in reversed(ordered[:index]):
                age = current[2].published_at - previous[2].published_at
                if age > CROSS_DATASET_EVENT_WINDOW:
                    break
                if current[0] == previous[0] or not _titles_overlap(
                    current[2].title,
                    previous[2].title,
                    ticker=current[2].ticker,
                ):
                    continue
                pair_count += 1
                if len(samples) < 100:
                    samples.append(
                        {
                            "left_dataset": str(previous[1]),
                            "right_dataset": str(current[1]),
                            "left_example_id": previous[2].id,
                            "right_example_id": current[2].id,
                            "left_event_id": previous[2].event_id,
                            "right_event_id": current[2].event_id,
                            "ticker": current[2].ticker,
                            "left_published_at": previous[2].published_at.isoformat(),
                            "right_published_at": current[2].published_at.isoformat(),
                            "left_title": previous[2].title,
                            "right_title": current[2].title,
                        }
                    )
    return {
        "pairs": pair_count,
        "samples": samples,
        "policy": {
            "same_ticker": True,
            "maximum_publication_gap_seconds": (
                CROSS_DATASET_EVENT_WINDOW.total_seconds()
            ),
            "minimum_title_token_overlap": CROSS_DATASET_TITLE_OVERLAP,
            "action": "fail_closed_before_write",
        },
    }


def _titles_overlap(left: str, right: str, *, ticker: str) -> bool:
    ignored_tokens = {ticker.casefold()}
    left_tokens = _title_tokens(left) - ignored_tokens
    right_tokens = _title_tokens(right) - ignored_tokens
    if not left_tokens or not right_tokens:
        return False
    shared = len(left_tokens & right_tokens)
    return shared / min(len(left_tokens), len(right_tokens)) >= (
        CROSS_DATASET_TITLE_OVERLAP
    )


def _title_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.sub(r"[^a-zа-яё0-9]+", " ", value.casefold()).split()
        if len(token) > 2 and token not in EVENT_STOP_WORDS
    }

if __name__ == "__main__":
    main()
