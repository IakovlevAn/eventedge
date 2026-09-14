from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from signal_fixtures import signal_example

from eventedge_research.signal_dataset import (
    SignalDatasetExample,
    load_signal_dataset,
    signal_dataset_sha256,
    write_signal_dataset,
)
from scripts.rebenchmark_signal_dataset import _run

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_rebenchmark_script_help_runs_as_a_file() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/rebenchmark_signal_dataset.py", "--help"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "benchmark-only derivative" in result.stdout


def test_rebenchmark_run_preserves_stock_labels(tmp_path) -> None:
    decision_at = datetime(2026, 1, 5, 10, tzinfo=UTC)
    example = SignalDatasetExample.model_validate(
        signal_example(
            1,
            decision_at=decision_at,
            benchmark_return_pct=None,
            label_source="market_outcome",
        )
    )
    dataset_path = tmp_path / "input.jsonl"
    write_signal_dataset(dataset_path, [example])
    benchmark_dir = tmp_path / "benchmark"
    benchmark_dir.mkdir()
    benchmark_path = benchmark_dir / "IMOEX2.jsonl"
    benchmark_path.write_text(
        "".join(
            json.dumps(_benchmark_row(at, price), sort_keys=True) + "\n"
            for at, price in (
                (decision_at - timedelta(minutes=61), 2_700),
                (decision_at - timedelta(minutes=1), 2_710),
                (decision_at, 9_999),
                (example.entry_at, 2_720),
                (example.outcome_4h.target_at + timedelta(minutes=5), 2_730),
            )
        ),
        encoding="utf-8",
    )
    (benchmark_dir / "complete.json").write_text(
        json.dumps(
            {
                "ticker": "IMOEX2",
                "sha256": signal_dataset_sha256(benchmark_path),
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"

    report = _run(
        argparse.Namespace(
            dataset=dataset_path,
            benchmark_dir=benchmark_dir,
            output_dir=output_dir,
        )
    )

    actual = load_signal_dataset(output_dir / "dataset.jsonl")[0]
    assert actual.entry_price == example.entry_price
    assert actual.outcome_4h.price == example.outcome_4h.price
    assert actual.outcome_4h.return_pct == example.outcome_4h.return_pct
    assert actual.outcome_4h.benchmark_id == "IMOEX2"
    assert report["selection_changed"] is False
    assert actual.label_available_at == example.outcome_4h.target_at + timedelta(minutes=5)
    assert report["stock_price_return_labels_changed"] is False
    assert report["label_availability_changed_rows"] == 1
    assert report["integrity"]["label_availability_changed_rows"] == 1
    assert report["temporal_quality"]["label_availability"] == {
        "policy": "max_stock_and_benchmark_outcome_observation",
        "changed_rows": 1,
        "verified_rows": 1,
    }
    assert report["benchmark_replacement"]["outcome_coverage"] == 1
    assert report["temporal_quality"]["lookahead_audit"] == {
        "selection_was_frozen_before_benchmark_join": True,
        "features_use_only_observations_strictly_before_decision": True,
        "post_decision_benchmark_values_are_label_fields_only": True,
        "label_target_is_entry_plus_exactly_4h": True,
    }
    assert actual.features.benchmark_pre_event_return_1h_pct == pytest.approx(
        (2_710 / 2_700 - 1) * 100
    )
    complete = json.loads((output_dir / "complete.json").read_text(encoding="utf-8"))
    assert complete["sha256"] == {
        name: signal_dataset_sha256(output_dir / name)
        for name in ("dataset.jsonl", "quality-report.json")
    }


def test_rebenchmark_run_only_changes_the_bounded_cohort(tmp_path) -> None:
    historical = SignalDatasetExample.model_validate(
        signal_example(
            1,
            decision_at=datetime(2025, 9, 1, 10, tzinfo=UTC),
            benchmark_return_pct=0.25,
            label_source="market_outcome",
        )
    )
    decision_at = datetime(2026, 1, 5, 10, tzinfo=UTC)
    cohort = SignalDatasetExample.model_validate(
        signal_example(
            2,
            decision_at=decision_at,
            benchmark_return_pct=None,
            label_source="market_outcome",
        )
    )
    dataset_path = tmp_path / "input.jsonl"
    write_signal_dataset(dataset_path, [historical, cohort])
    benchmark_dir = _write_benchmark(
        tmp_path,
        [
            (decision_at - timedelta(minutes=61), 2_700),
            (decision_at - timedelta(minutes=1), 2_710),
            (cohort.entry_at, 2_720),
            (cohort.outcome_4h.target_at, 2_730),
        ],
    )
    output_dir = tmp_path / "output"

    report = _run(
        argparse.Namespace(
            dataset=dataset_path,
            benchmark_dir=benchmark_dir,
            output_dir=output_dir,
            artifact_version="fixture-v2",
            decision_from=datetime(2026, 1, 1, tzinfo=UTC),
            decision_before=datetime(2027, 1, 1, tzinfo=UTC),
            expected_input_sha256=signal_dataset_sha256(dataset_path),
            expected_cohort_rows=1,
            require_complete_cohort=True,
            maximum_stock_entry_lag_seconds=600,
        )
    )

    actual = load_signal_dataset(output_dir / "dataset.jsonl")
    assert actual[0] == historical
    assert actual[1].outcome_4h.benchmark_id == "IMOEX2"
    assert report["cohort"]["rows"] == 1
    assert report["integrity"]["rows_outside_cohort_preserved"] == 1
    assert report["integrity"]["cohort_rows_with_only_benchmark_fields_mutable"] == 1
    assert report["artifact_version"] == "fixture-v2"


def test_rebenchmark_run_requires_complete_cohort_before_writing(tmp_path) -> None:
    first_decision = datetime(2026, 1, 5, 10, tzinfo=UTC)
    second_decision = datetime(2026, 1, 6, 10, tzinfo=UTC)
    examples = [
        SignalDatasetExample.model_validate(
            signal_example(
                index,
                decision_at=decision,
                benchmark_return_pct=None,
                label_source="market_outcome",
            )
        )
        for index, decision in enumerate((first_decision, second_decision), 1)
    ]
    dataset_path = tmp_path / "input.jsonl"
    write_signal_dataset(dataset_path, examples)
    benchmark_dir = _write_benchmark(
        tmp_path,
        [
            (first_decision - timedelta(minutes=61), 2_700),
            (first_decision - timedelta(minutes=1), 2_710),
            (examples[0].entry_at, 2_720),
            (examples[0].outcome_4h.target_at, 2_730),
        ],
    )
    output_dir = tmp_path / "output"

    with pytest.raises(ValueError, match="does not cover every cohort"):
        _run(
            argparse.Namespace(
                dataset=dataset_path,
                benchmark_dir=benchmark_dir,
                output_dir=output_dir,
                decision_from=datetime(2026, 1, 1, tzinfo=UTC),
                decision_before=datetime(2027, 1, 1, tzinfo=UTC),
                require_complete_cohort=True,
            )
        )

    assert not output_dir.exists()


def test_rebenchmark_run_rejects_non_overlapping_archive(tmp_path) -> None:
    decision_at = datetime(2026, 1, 5, 10, tzinfo=UTC)
    example = SignalDatasetExample.model_validate(
        signal_example(
            1,
            decision_at=decision_at,
            benchmark_return_pct=None,
            label_source="market_outcome",
        )
    )
    dataset_path = tmp_path / "input.jsonl"
    write_signal_dataset(dataset_path, [example])
    benchmark_dir = tmp_path / "benchmark"
    benchmark_dir.mkdir()
    benchmark_path = benchmark_dir / "IMOEX2.jsonl"
    benchmark_path.write_text(
        json.dumps(
            _benchmark_row(datetime(2025, 1, 5, 10, tzinfo=UTC), 2_700),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (benchmark_dir / "complete.json").write_text(
        json.dumps(
            {
                "ticker": "IMOEX2",
                "sha256": signal_dataset_sha256(benchmark_path),
            }
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"

    with pytest.raises(ValueError, match="does not overlap"):
        _run(
            argparse.Namespace(
                dataset=dataset_path,
                benchmark_dir=benchmark_dir,
                output_dir=output_dir,
            )
        )

    assert not output_dir.exists()


def _benchmark_row(at: datetime, price: float) -> dict[str, object]:
    return {
        "schema_version": "market-open-observation-1.0",
        "ticker": "IMOEX2",
        "at": at.isoformat(),
        "open": price,
        "provider_id": "moex-iss-sndx-index-1m",
        "adjusted": False,
        "label_source": "market_outcome",
    }


def _write_benchmark(tmp_path, rows: list[tuple[datetime, float]]):
    benchmark_dir = tmp_path / "benchmark"
    benchmark_dir.mkdir()
    benchmark_path = benchmark_dir / "IMOEX2.jsonl"
    benchmark_path.write_text(
        "".join(json.dumps(_benchmark_row(at, price), sort_keys=True) + "\n" for at, price in rows),
        encoding="utf-8",
    )
    (benchmark_dir / "complete.json").write_text(
        json.dumps(
            {
                "ticker": "IMOEX2",
                "sha256": signal_dataset_sha256(benchmark_path),
            }
        ),
        encoding="utf-8",
    )
    return benchmark_dir
