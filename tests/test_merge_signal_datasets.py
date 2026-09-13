from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from signal_fixtures import signal_example

from eventedge_research.signal_dataset import load_signal_dataset
from scripts.merge_signal_datasets import main as merge_signal_datasets_main


def test_merge_signal_datasets_orders_and_fingerprints_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    output = tmp_path / "merged.jsonl"
    report_path = tmp_path / "merge-report.json"
    first.write_text(json.dumps(signal_example(2)) + "\n", encoding="utf-8")
    second.write_text(
        json.dumps(signal_example(1, ticker="LKOH")) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "merge_signal_datasets",
            "--dataset",
            str(first),
            "--dataset",
            str(second),
            "--output",
            str(output),
            "--report",
            str(report_path),
            "--allow-synthetic",
        ],
    )

    merge_signal_datasets_main()

    report = json.loads(capsys.readouterr().out)
    examples = load_signal_dataset(output, allow_synthetic=True)
    assert [example.decision_at for example in examples] == sorted(
        example.decision_at for example in examples
    )
    assert report["observations"] == 2
    assert report["dataset_schema_version"] == "signal-dataset-example-1.0"
    assert report["information_groups"] == 2
    assert report["cross_dataset_event_overlap"]["pairs"] == 0
    assert len(report["output_sha256"]) == 64
    assert json.loads(report_path.read_text(encoding="utf-8")) == report
    assert output.stat().st_mode & 0o777 == 0o600
    assert report_path.stat().st_mode & 0o777 == 0o600


def test_merge_signal_datasets_rejects_cross_file_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    payload = json.dumps(signal_example(1)) + "\n"
    first.write_text(payload, encoding="utf-8")
    second.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "merge_signal_datasets",
            "--dataset",
            str(first),
            "--dataset",
            str(second),
            "--output",
            str(tmp_path / "merged.jsonl"),
            "--allow-synthetic",
        ],
    )

    with pytest.raises(ValueError, match="duplicate signal dataset id"):
        merge_signal_datasets_main()


def test_merge_signal_datasets_rejects_cross_file_near_duplicate_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    left = signal_example(1)
    right = signal_example(2, decision_at=_decision_at(left) + timedelta(hours=1))
    right["title"] = "Компания опубликовала квартальные результаты 1"
    first.write_text(json.dumps(left) + "\n", encoding="utf-8")
    second.write_text(json.dumps(right) + "\n", encoding="utf-8")
    output = tmp_path / "merged.jsonl"
    monkeypatch.setattr(
        "sys.argv",
        [
            "merge_signal_datasets",
            "--dataset",
            str(first),
            "--dataset",
            str(second),
            "--output",
            str(output),
            "--allow-synthetic",
        ],
    )

    with pytest.raises(ValueError, match="cross-dataset near-duplicate event"):
        merge_signal_datasets_main()

    assert not output.exists()


def test_merge_signal_datasets_does_not_treat_ticker_only_titles_as_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    left = signal_example(1)
    right = signal_example(2, decision_at=_decision_at(left) + timedelta(hours=1))
    left["title"] = "#SBER"
    right["title"] = "🇷🇺 #SBER #dividend"
    first.write_text(json.dumps(left) + "\n", encoding="utf-8")
    second.write_text(json.dumps(right) + "\n", encoding="utf-8")
    output = tmp_path / "merged.jsonl"
    monkeypatch.setattr(
        "sys.argv",
        [
            "merge_signal_datasets",
            "--dataset",
            str(first),
            "--dataset",
            str(second),
            "--output",
            str(output),
            "--allow-synthetic",
        ],
    )

    merge_signal_datasets_main()

    assert output.exists()


def test_merge_signal_datasets_rejects_mixed_schema_versions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    output = tmp_path / "merged.jsonl"
    first.write_text(json.dumps(signal_example(1)) + "\n", encoding="utf-8")
    version_two = signal_example(2, ticker="LKOH")
    version_two.update(
        schema_version="signal-dataset-example-2.0",
        received_at=version_two["decision_at"],
        corporate_action_status="unknown",
        event_group_id="group-2",
        source_url="https://t.me/source/2",
        retrieved_at="2026-02-01T00:00:00+00:00",
        admission_version="test-v2",
        analysis_title="Компания опубликовала результаты",
        analysis_content_sha256="a" * 64,
        receipt_provenance="publication_plus_assumed_5_minutes",
        text_provenance="no_edit_marker_current_view_not_first_snapshot",
    )
    second.write_text(json.dumps(version_two) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "merge_signal_datasets",
            "--dataset",
            str(first),
            "--dataset",
            str(second),
            "--output",
            str(output),
            "--allow-synthetic",
        ],
    )

    with pytest.raises(ValueError, match="mixed signal dataset schema versions"):
        merge_signal_datasets_main()

    assert not output.exists()


def _decision_at(payload: dict[str, object]) -> datetime:
    value = payload["decision_at"]
    assert isinstance(value, str)
    return datetime.fromisoformat(value)
