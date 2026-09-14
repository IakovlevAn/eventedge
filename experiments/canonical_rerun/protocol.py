"""Frozen data preparation and fold guards for the canonical experiment rerun."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from eventedge_research.signal_dataset import (
    load_signal_dataset,
    signal_dataset_sha256,
    write_signal_dataset,
)
from eventedge_research.signal_dataset_builder import (
    MarketOpenObservation,
    replace_benchmark_context,
)
from eventedge_research.signal_dataset_v2 import split_information_updates
from experiments.finbert_stage1.run_experiment import Fold

LEGACY_DATASET_SHA256 = "ac639e944a100c83b91275bbf627d3e9af9c654e5a0ce726dc484c0e2c0b81b6"
CANONICAL_DATASET_SHA256 = "ee47437edd61f2f444f32e1d85e7562fb0a07c47750651217ffc898c55ca632c"
MARKET_CACHE_SHA256 = "3e3f0ae2f296f747d6ff8cf00a4cd2115e8b133549948f2d48272fdac8b4fb6e"
OUTCOME_CACHE_SHA256 = "4956ceb5ade19baaac61dd27178fba944a949842a8e915a844a074f8236d5dc6"
EXPECTED_FORMULA_ROWS = 157
EXPECTED_FORMULA_HIT_RATE = 0.5095541401273885
HORIZONS_MINUTES = (30, 60, 120, 240)
MAXIMUM_OUTCOME_LAG = pd.Timedelta(minutes=20)


@dataclass(frozen=True)
class CanonicalPaths:
    """Explicit inputs used to recreate the ignored canonical workspace."""

    legacy_dataset: Path
    market_cache: Path
    outcome_cache: Path
    benchmark_contract: Path
    canonical_dataset: Path


def rebuild_canonical_dataset(paths: CanonicalPaths) -> dict[str, Any]:
    """Rebenchmark the legacy rows and require the byte-exact canonical digest."""
    _require_sha(paths.legacy_dataset, LEGACY_DATASET_SHA256, "legacy dataset")
    _require_sha(paths.market_cache, MARKET_CACHE_SHA256, "market cache")
    _require_sha(paths.outcome_cache, OUTCOME_CACHE_SHA256, "outcome cache")
    if paths.canonical_dataset.exists():
        _require_sha(paths.canonical_dataset, CANONICAL_DATASET_SHA256, "canonical dataset")
        return {
            "rows": _jsonl_rows(paths.canonical_dataset),
            "sha256": CANONICAL_DATASET_SHA256,
            "replacements": _replacement_summary(),
        }

    examples = load_signal_dataset(paths.legacy_dataset)
    observations = _benchmark_observations(paths.market_cache, paths.outcome_cache)
    replacements = []
    windows = (
        ("2026", "2026-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00", 592),
        ("history", "2024-01-01T00:00:00+00:00", "2025-09-07T00:00:00+00:00", 533),
        ("gap", "2025-09-07T00:00:00+00:00", "2026-01-01T00:00:00+00:00", 113),
    )
    for name, start_text, end_text, expected_rows in windows:
        start = pd.Timestamp(start_text).to_pydatetime()
        end = pd.Timestamp(end_text).to_pydatetime()
        cohort = [row for row in examples if start <= row.decision_at < end]
        if len(cohort) != expected_rows:
            raise AssertionError(f"canonical {name} cohort changed: {len(cohort)}")
        derived, report = replace_benchmark_context(
            cohort,
            observations,
            benchmark_id="IMOEX2",
        )
        if report["outcome_coverage"] != expected_rows:
            raise AssertionError(f"canonical {name} benchmark coverage changed")
        by_id = {row.id: row for row in derived}
        examples = [by_id.get(row.id, row) for row in examples]
        replacements.append(
            {
                "cohort": name,
                "rows": expected_rows,
                "label_availability_changed_rows": sum(
                    source.label_available_at != by_id[source.id].label_available_at
                    for source in cohort
                ),
            }
        )

    if replacements != _replacement_summary():
        raise AssertionError("canonical benchmark replacement audit changed")

    paths.canonical_dataset.parent.mkdir(parents=True, exist_ok=True)
    write_signal_dataset(paths.canonical_dataset, examples)
    _require_sha(paths.canonical_dataset, CANONICAL_DATASET_SHA256, "canonical dataset")
    return {
        "rows": len(examples),
        "sha256": CANONICAL_DATASET_SHA256,
        "replacements": replacements,
    }


def build_remaining_labels(
    frame: pd.DataFrame,
    outcome_cache: Path,
) -> pd.DataFrame:
    """Build publication+5m to publication+horizon abnormal-return labels."""
    _require_sha(outcome_cache, OUTCOME_CACHE_SHA256, "outcome cache")
    candles = pd.read_csv(outcome_cache, parse_dates=["begin_at"])
    groups = {
        key: group.sort_values("begin_at").reset_index(drop=True)
        for key, group in candles.groupby("request_key", sort=False)
    }
    empty = candles.iloc[0:0]
    records: list[dict[str, Any]] = []
    for row in frame.itertuples(index=False):
        day = row.published_at.tz_convert("Europe/Moscow").date().isoformat()
        stock = groups.get(f"shares:{row.ticker}:{day}", empty)
        benchmark = groups.get(f"index:IMOEX2:{day}", empty)
        record: dict[str, Any] = {"id": row.id}
        decision_at = row.published_at + pd.Timedelta(minutes=5)
        for horizon in HORIZONS_MINUTES:
            target_at = row.published_at + pd.Timedelta(minutes=horizon)
            stock_entry = _first_open(stock, decision_at)
            stock_target = _first_open(stock, target_at)
            benchmark_entry = _first_open(benchmark, decision_at)
            benchmark_target = _first_open(benchmark, target_at)
            values = (stock_entry, stock_target, benchmark_entry, benchmark_target)
            if any(value is None for value in values):
                raw_return = benchmark_return = abnormal_return = available_at = None
                timely = False
            else:
                assert stock_entry is not None
                assert stock_target is not None
                assert benchmark_entry is not None
                assert benchmark_target is not None
                raw_return = (stock_target[1] / stock_entry[1] - 1) * 100
                benchmark_return = (benchmark_target[1] / benchmark_entry[1] - 1) * 100
                abnormal_return = raw_return - benchmark_return
                available_at = max(stock_target[0], benchmark_target[0])
                timely = True
            record.update(
                {
                    f"raw_return_{horizon}m_pct": raw_return,
                    f"benchmark_return_{horizon}m_pct": benchmark_return,
                    f"abnormal_return_{horizon}m_pct": abnormal_return,
                    f"label_available_at_{horizon}m": available_at,
                    f"timely_{horizon}m": timely,
                }
            )
        records.append(record)
    labels = pd.DataFrame(records)
    if len(labels) != len(frame) or labels["id"].duplicated().any():
        raise AssertionError("canonical remaining labels do not map one-to-one")
    return labels


def attach_remaining_direction(frame: pd.DataFrame, labels: pd.DataFrame) -> pd.DataFrame:
    """Replace legacy entry+4h direction with the canonical remaining target."""
    result = frame.merge(labels, on="id", validate="one_to_one")
    remaining = result["abnormal_return_240m_pct"]
    if (remaining.dropna() == 0).any():
        raise AssertionError("canonical 240-minute direction target contains an exact zero")
    result["legacy_abnormal_direction_label"] = result["abnormal_direction_label"]
    result["legacy_raw_direction_label"] = result["raw_direction_label"]
    result["abnormal_direction_label"] = np.where(
        remaining.notna(),
        (remaining > 0).astype(int),
        np.nan,
    )
    raw = result["raw_return_240m_pct"]
    result["raw_direction_label"] = np.where(
        raw.notna(),
        (raw > 0).astype(int),
        np.nan,
    )
    return result


def filter_direction_folds(frame: pd.DataFrame, folds: list[Fold]) -> list[Fold]:
    """Apply the canonical outcome-availability filter to direction fitting rows."""
    indexed = frame.set_index("id")
    filtered = []
    for fold in folds:
        evaluation_from = pd.Timestamp(fold.validation_cutoff)

        def eligible(
            identities: tuple[str, ...],
            cutoff: pd.Timestamp = evaluation_from,
        ) -> tuple[str, ...]:
            rows = indexed.loc[list(identities)]
            mask = rows["abnormal_direction_label"].notna() & (
                rows["label_available_at_240m"] < cutoff
            )
            return tuple(rows.index[mask])

        test = indexed.loc[list(fold.test_ids)]
        if test["abnormal_direction_label"].isna().any():
            raise AssertionError(f"canonical evaluation target is incomplete: {fold.name}")
        filtered.append(
            Fold(
                name=fold.name,
                train_ids=eligible(fold.train_ids),
                validation_ids=eligible(fold.validation_ids),
                test_ids=fold.test_ids,
                train_cutoff=fold.train_cutoff,
                validation_cutoff=fold.validation_cutoff,
            )
        )
    return filtered


def build_canonical_folds(
    canonical_dataset: Path,
    benchmark_contract: Path,
) -> list[Fold]:
    """Build and fingerprint the seven folds declared by the canonical contract."""
    _require_sha(canonical_dataset, CANONICAL_DATASET_SHA256, "canonical dataset")
    contract = json.loads(benchmark_contract.read_text(encoding="utf-8"))
    examples = load_signal_dataset(canonical_dataset)
    folds: list[Fold] = []
    all_test_ids: list[str] = []
    for source in contract["folds"]:
        validation_from = pd.Timestamp(source["validation_from"]).to_pydatetime()
        evaluation_from = pd.Timestamp(source["evaluation_from"]).to_pydatetime()
        evaluation_until = pd.Timestamp(source["evaluation_until"]).to_pydatetime()
        split = split_information_updates(
            examples,
            validation_from=validation_from,
            test_from=evaluation_from,
            test_until=evaluation_until,
            embargo=timedelta(hours=72),
        )
        test_ids = tuple(row.id for row in split.test)
        if (
            len(test_ids) != source["evaluation_rows"]
            or _identity_sequence_sha256(test_ids) != source["evaluation_ids_sha256"]
        ):
            raise AssertionError(f"canonical fold membership changed: {source['name']}")
        folds.append(
            Fold(
                name=str(source["name"]),
                train_ids=tuple(row.id for row in split.train),
                validation_ids=tuple(row.id for row in split.validation),
                test_ids=test_ids,
                train_cutoff=validation_from,
                validation_cutoff=evaluation_from,
            )
        )
        all_test_ids.extend(test_ids)
    if len(all_test_ids) != 480 or len(set(all_test_ids)) != 480:
        raise AssertionError("canonical evaluation population changed")
    return folds


def verify_formula_control(frame: pd.DataFrame, folds: list[Fold]) -> dict[str, Any]:
    """Use the frozen config-6 comparison as an independent target guard."""
    evaluation_ids = {identity for fold in folds for identity in fold.test_ids}
    rows = frame[
        frame["id"].isin(evaluation_ids) & frame["rule_direction"].isin(["up", "down"])
    ].copy()
    predictions = (rows["rule_direction"] == "up").astype(int)
    hit_rate = float((predictions == rows["abnormal_direction_label"]).mean())
    if len(rows) != EXPECTED_FORMULA_ROWS or not np.isclose(
        hit_rate,
        EXPECTED_FORMULA_HIT_RATE,
        rtol=0,
        atol=1e-15,
    ):
        raise AssertionError("canonical remaining target differs from the frozen formula control")
    return {"rows": len(rows), "hit_rate": hit_rate}


def _benchmark_observations(*cache_paths: Path) -> list[MarketOpenObservation]:
    opens: dict[pd.Timestamp, float] = {}
    for path in cache_paths:
        with gzip.open(path, "rt", newline="") as source:
            for row in csv.DictReader(source):
                if row["ticker"] != "IMOEX2":
                    continue
                at = pd.Timestamp(row["begin_at"])
                price = float(row["open"])
                previous = opens.setdefault(at, price)
                if previous != price:
                    raise AssertionError(f"conflicting IMOEX2 open at {at.isoformat()}")
    return [
        MarketOpenObservation(
            ticker="IMOEX2",
            at=at.to_pydatetime(),
            open=price,
            provider_id="moex-iss-sndx-index-1m",
            adjusted=False,
            label_source="market_outcome",
        )
        for at, price in sorted(opens.items())
    ]


def _first_open(
    candles: pd.DataFrame,
    target_at: pd.Timestamp,
) -> tuple[pd.Timestamp, float] | None:
    candidates = candles[candles["begin_at"] >= target_at]
    if candidates.empty:
        return None
    row = candidates.iloc[0]
    if row["begin_at"] - target_at > MAXIMUM_OUTCOME_LAG:
        return None
    return row["begin_at"], float(row["open"])


def _identity_sequence_sha256(identities: tuple[str, ...]) -> str:
    payload = json.dumps(
        list(identities),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _require_sha(path: Path, expected: str, label: str) -> None:
    if not path.is_file() or signal_dataset_sha256(path) != expected:
        raise ValueError(f"{label} is missing or differs from its frozen SHA-256")


def _jsonl_rows(path: Path) -> int:
    with path.open(encoding="utf-8") as source:
        return sum(bool(line.strip()) for line in source)


def _replacement_summary() -> list[dict[str, Any]]:
    return [
        {"cohort": "2026", "rows": 592, "label_availability_changed_rows": 3},
        {"cohort": "history", "rows": 533, "label_availability_changed_rows": 17},
        {"cohort": "gap", "rows": 113, "label_availability_changed_rows": 3},
    ]
