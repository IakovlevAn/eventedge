from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.finbert_stage1.run_experiment import (  # noqa: E402
    load_dataset,
    sha256_file,
)
from experiments.market_stage2.run_experiment import (  # noqa: E402
    MOSCOW,
    RequestSpec,
    _fetch_spec,
    _spec_signature,
)
from experiments.paths import stage_artifact_directory  # noqa: E402

LABEL_CACHE_VERSION = "direction-horizon-labels-1.0"
HORIZONS_MINUTES = (30, 60, 120, 240)
MAX_OBSERVATION_DELAY = pd.Timedelta(minutes=15)


def _reference_rows(dataset_path: Path) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    with dataset_path.open(encoding="utf-8") as source:
        for line in source:
            raw = json.loads(line)
            outcome = raw["outcome_4h"]
            rows.append(
                {
                    "id": str(raw["id"]),
                    "entry_price_reference": float(raw["entry_price"]),
                    "outcome_observed_at_reference": pd.Timestamp(outcome["observed_at"]),
                    "stock_return_240m_reference": float(outcome["return_pct"]),
                    "benchmark_return_240m_reference": float(outcome["benchmark_return_pct"]),
                    "abnormal_return_240m_reference": float(outcome["abnormal_return_pct"]),
                }
            )
    return pd.DataFrame(rows)


def _outcome_specs(frame: pd.DataFrame) -> list[RequestSpec]:
    raw: dict[tuple[str, str, str], dict[str, pd.Timestamp]] = {}
    for row in frame.itertuples(index=False):
        day = row.published_at.tz_convert(MOSCOW).date().isoformat()
        needed_until = row.entry_at + pd.Timedelta(minutes=255)
        for market, ticker in (("shares", row.ticker), ("index", "IMOEX2")):
            key = (market, ticker, day)
            current = raw.get(key)
            if current is None:
                raw[key] = {
                    "retain_from": row.decision_at,
                    "needed_until": needed_until,
                }
            else:
                current["retain_from"] = min(current["retain_from"], row.decision_at)
                current["needed_until"] = max(current["needed_until"], needed_until)
    return sorted(
        (
            RequestSpec(
                market=market,
                ticker=ticker,
                day=day,
                retain_from=window["retain_from"],
                needed_until=window["needed_until"],
            )
            for (market, ticker, day), window in raw.items()
        ),
        key=lambda item: item.key,
    )


def load_or_download_outcome_candles(
    frame: pd.DataFrame,
    *,
    dataset_sha256: str,
    cache_path: Path,
    metadata_path: Path,
    workers: int,
    offline: bool,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    specs = _outcome_specs(frame)
    signature = _spec_signature(specs)
    if cache_path.exists() and metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        valid = (
            metadata.get("cache_version") == LABEL_CACHE_VERSION
            and metadata.get("dataset_sha256") == dataset_sha256
            and metadata.get("request_spec_sha256") == signature
            and metadata.get("request_specs") == len(specs)
        )
        if valid:
            candles = pd.read_csv(cache_path, parse_dates=["begin_at"])
            if len(candles) != metadata.get("rows"):
                raise AssertionError("outcome cache row count mismatch")
            if sha256_file(cache_path) != metadata.get("data_sha256"):
                raise AssertionError("outcome cache hash mismatch")
            return candles, metadata
    if offline:
        raise FileNotFoundError("valid outcome cache unavailable in offline mode")

    results: dict[str, list[dict[str, Any]]] = {}
    failures: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_spec, spec): spec for spec in specs}
        for completed, future in enumerate(as_completed(futures), 1):
            spec = futures[future]
            try:
                key, rows = future.result()
                results[key] = rows
            except RuntimeError as error:
                failures.append({"key": spec.key, "error": str(error)})
                results[spec.key] = []
            if completed % 100 == 0 or completed == len(specs):
                print(f"Outcome cache: {completed}/{len(specs)} requests", flush=True)
    candles = pd.DataFrame([row for key in sorted(results) for row in results[key]]).sort_values(
        ["request_key", "begin_at"]
    )
    if candles.empty:
        raise RuntimeError("MOEX returned no outcome candles")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    candles.to_csv(
        cache_path,
        index=False,
        float_format="%.10g",
        compression={"method": "gzip", "compresslevel": 6, "mtime": 0},
    )
    metadata = {
        "cache_version": LABEL_CACHE_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "dataset_sha256": dataset_sha256,
        "request_spec_sha256": signature,
        "request_specs": len(specs),
        "successful_specs": sum(bool(results[spec.key]) for spec in specs),
        "empty_or_failed_specs": sum(not results[spec.key] for spec in specs),
        "failures": failures,
        "rows": len(candles),
        "data_sha256": sha256_file(cache_path),
        "source": "MOEX ISS",
    }
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return candles, metadata


def _first_open_at_or_after(
    candles: pd.DataFrame,
    timestamp: pd.Timestamp,
) -> tuple[pd.Timestamp | None, float | None]:
    candidates = candles[candles["begin_at"] >= timestamp]
    if candidates.empty:
        return None, None
    row = candidates.iloc[0]
    return row["begin_at"], float(row["open"])


def _aligned_return(
    candles: pd.DataFrame,
    *,
    entry_at: pd.Timestamp,
    target_at: pd.Timestamp,
) -> tuple[float | None, pd.Timestamp | None, float | None, float | None]:
    observed_entry_at, entry_price = _first_open_at_or_after(candles, entry_at)
    observed_target_at, target_price = _first_open_at_or_after(candles, target_at)
    if (
        observed_entry_at is None
        or entry_price is None
        or observed_target_at is None
        or target_price is None
        or observed_entry_at - entry_at > MAX_OBSERVATION_DELAY
        or observed_target_at - target_at > MAX_OBSERVATION_DELAY
    ):
        return None, observed_target_at, entry_price, target_price
    return (
        (target_price / entry_price - 1) * 100,
        observed_target_at,
        entry_price,
        target_price,
    )


def build_horizon_labels(
    frame: pd.DataFrame,
    candles: pd.DataFrame,
    reference: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    groups = {
        key: group.sort_values("begin_at").reset_index(drop=True)
        for key, group in candles.groupby("request_key", sort=False)
    }
    output: list[dict[str, Any]] = []
    for row in frame.itertuples(index=False):
        day = row.published_at.tz_convert(MOSCOW).date().isoformat()
        empty = next(iter(groups.values())).iloc[0:0]
        stock = groups.get(f"shares:{row.ticker}:{day}", empty)
        benchmark = groups.get(f"index:IMOEX2:{day}", empty)
        values: dict[str, Any] = {
            "id": row.id,
            "entry_at": row.entry_at,
        }
        for horizon in HORIZONS_MINUTES:
            target_at = row.entry_at + pd.Timedelta(minutes=horizon)
            stock_return, stock_observed, stock_entry, stock_target = _aligned_return(
                stock,
                entry_at=row.entry_at,
                target_at=target_at,
            )
            benchmark_return, benchmark_observed, _, _ = _aligned_return(
                benchmark,
                entry_at=row.entry_at,
                target_at=target_at,
            )
            timely = (
                stock_return is not None
                and benchmark_return is not None
                and stock_observed is not None
                and benchmark_observed is not None
            )
            abnormal = stock_return - benchmark_return if timely else None
            values[f"stock_return_{horizon}m_pct"] = stock_return
            values[f"benchmark_return_{horizon}m_pct"] = benchmark_return
            values[f"abnormal_return_{horizon}m_pct"] = abnormal
            values[f"observed_at_{horizon}m"] = stock_observed
            values[f"label_available_at_{horizon}m"] = (
                max(stock_observed, benchmark_observed)
                if stock_observed is not None and benchmark_observed is not None
                else None
            )
            values[f"timely_{horizon}m"] = int(timely)
            if horizon == 240:
                values["entry_price_derived"] = stock_entry
                values["target_price_derived"] = stock_target
        output.append(values)
    labels = pd.DataFrame(output)
    labels = labels.merge(reference, left_on="id", right_on="id", validate="one_to_one")

    derived_mask = labels["timely_240m"] == 1
    complete = labels[derived_mask]
    stock_error = (
        complete["stock_return_240m_pct"] - complete["stock_return_240m_reference"]
    ).abs()
    benchmark_error = (
        complete["benchmark_return_240m_pct"] - complete["benchmark_return_240m_reference"]
    ).abs()
    abnormal_error = (
        complete["abnormal_return_240m_pct"] - complete["abnormal_return_240m_reference"]
    ).abs()
    validation = {
        "derived_240m_rows": len(complete),
        "fallback_240m_rows": int((~derived_mask).sum()),
        "fallback_240m_ids": labels.loc[~derived_mask, "id"].tolist(),
        "stock_max_abs_error_pct": float(stock_error.max()),
        "stock_median_abs_error_pct": float(stock_error.median()),
        "benchmark_max_abs_error_pct": float(benchmark_error.max()),
        "benchmark_median_abs_error_pct": float(benchmark_error.median()),
        "abnormal_max_abs_error_pct": float(abnormal_error.max()),
        "abnormal_median_abs_error_pct": float(abnormal_error.median()),
        "entry_price_max_abs_error": float(
            (complete["entry_price_derived"] - complete["entry_price_reference"]).abs().max()
        ),
        "observed_at_exact_matches": int(
            (complete["observed_at_240m"] == complete["outcome_observed_at_reference"]).sum()
        ),
    }
    if validation["derived_240m_rows"] < len(frame) - 1:
        raise AssertionError("too many existing 240-minute labels could not be reproduced")
    if validation["entry_price_max_abs_error"] > 1e-8:
        raise AssertionError("derived entry price differs from dataset")
    if validation["stock_max_abs_error_pct"] > 1e-8:
        raise AssertionError("derived stock 240-minute return differs from dataset")
    labels.loc[~derived_mask, "stock_return_240m_pct"] = labels.loc[
        ~derived_mask, "stock_return_240m_reference"
    ]
    labels.loc[~derived_mask, "benchmark_return_240m_pct"] = labels.loc[
        ~derived_mask, "benchmark_return_240m_reference"
    ]
    labels.loc[~derived_mask, "abnormal_return_240m_pct"] = labels.loc[
        ~derived_mask, "abnormal_return_240m_reference"
    ]
    labels.loc[~derived_mask, "observed_at_240m"] = labels.loc[
        ~derived_mask, "outcome_observed_at_reference"
    ]
    labels.loc[~derived_mask, "label_available_at_240m"] = labels.loc[
        ~derived_mask, "outcome_observed_at_reference"
    ]
    labels.loc[~derived_mask, "timely_240m"] = 1
    labels["label_source_240m"] = np.where(
        derived_mask,
        "moex_iss_reproduced",
        "dataset_reference_fallback",
    )
    return labels, validation


def main() -> None:
    parser = argparse.ArgumentParser(description="Build stage-3 direction labels")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=stage_artifact_directory("direction_stage3") / "cache",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=stage_artifact_directory("direction_stage3") / "horizon_labels.csv",
    )
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--offline", action="store_true")
    arguments = parser.parse_args()
    dataset_sha256 = sha256_file(arguments.dataset)
    frame = load_dataset(arguments.dataset)
    candles, cache_metadata = load_or_download_outcome_candles(
        frame,
        dataset_sha256=dataset_sha256,
        cache_path=arguments.cache_dir / "outcome_candles.csv.gz",
        metadata_path=arguments.cache_dir / "outcome_candles.metadata.json",
        workers=arguments.workers,
        offline=arguments.offline,
    )
    labels, validation = build_horizon_labels(
        frame,
        candles,
        _reference_rows(arguments.dataset),
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    labels.to_csv(arguments.output, index=False, float_format="%.10g")
    report = {
        "dataset_sha256": dataset_sha256,
        "cache": cache_metadata,
        "horizon_coverage": {
            str(horizon): {
                "n": int(labels[f"timely_{horizon}m"].sum()),
                "rate": float(labels[f"timely_{horizon}m"].mean()),
            }
            for horizon in HORIZONS_MINUTES
        },
        "reproduction_240m": validation,
        "labels_sha256": sha256_file(arguments.output),
    }
    (arguments.output.parent / "label_build_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
