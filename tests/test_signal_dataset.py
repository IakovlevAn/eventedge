from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from signal_fixtures import signal_example

from eventedge_research.signal_dataset import (
    SignalDatasetExample,
    load_signal_dataset,
    signal_dataset_sha256,
    split_signal_dataset,
    temporal_signal_split,
    write_signal_dataset,
)
from eventedge_research.signal_dataset_v2 import SignalDatasetExampleV2
from scripts.audit_signal_dataset import main as audit_signal_dataset_main


def test_signal_example_rejects_future_features_and_inconsistent_prices() -> None:
    decision_at = datetime(2026, 1, 5, tzinfo=UTC)
    future_features = signal_example(
        1,
        decision_at=decision_at,
        feature_as_of=decision_at + timedelta(seconds=1),
    )
    with pytest.raises(ValidationError, match="features.as_of cannot be later"):
        SignalDatasetExample.model_validate(future_features)

    inconsistent_return = signal_example(2)
    inconsistent_return["outcome_4h"]["return_pct"] = 5.0
    with pytest.raises(ValidationError, match="return_pct is inconsistent"):
        SignalDatasetExample.model_validate(inconsistent_return)


def test_signal_example_requires_complete_consistent_benchmark() -> None:
    missing_abnormal_return = signal_example(1)
    missing_abnormal_return["outcome_4h"]["abnormal_return_pct"] = None
    with pytest.raises(ValidationError, match="benchmark fields must be all present"):
        SignalDatasetExample.model_validate(missing_abnormal_return)

    inconsistent_abnormal_return = signal_example(2)
    inconsistent_abnormal_return["outcome_4h"]["abnormal_return_pct"] = 9.0
    with pytest.raises(ValidationError, match="abnormal_return_pct is inconsistent"):
        SignalDatasetExample.model_validate(inconsistent_abnormal_return)


def test_signal_example_requires_exact_timely_four_hour_outcome() -> None:
    wrong_horizon = signal_example(1)
    wrong_horizon["outcome_4h"]["target_at"] = (
        datetime.fromisoformat(wrong_horizon["entry_at"]) + timedelta(hours=5)
    ).isoformat()
    wrong_horizon["outcome_4h"]["observed_at"] = wrong_horizon["outcome_4h"][
        "target_at"
    ]
    wrong_horizon["label_available_at"] = wrong_horizon["outcome_4h"]["target_at"]
    with pytest.raises(ValidationError, match="exactly four hours"):
        SignalDatasetExample.model_validate(wrong_horizon)

    late_observation = signal_example(2)
    late_at = datetime.fromisoformat(
        late_observation["outcome_4h"]["target_at"]
    ) + timedelta(minutes=21)
    late_observation["outcome_4h"]["observed_at"] = late_at.isoformat()
    late_observation["label_available_at"] = late_at.isoformat()
    with pytest.raises(ValidationError, match="20-minute lag limit"):
        SignalDatasetExample.model_validate(late_observation)


def test_signal_example_requires_explicit_corporate_action_review() -> None:
    unreviewed = signal_example(1)
    del unreviewed["corporate_action_status"]

    with pytest.raises(ValidationError, match="Field required"):
        SignalDatasetExample.model_validate(unreviewed)


def test_signal_loader_rejects_synthetic_and_duplicate_event_ticker(tmp_path: Path) -> None:
    dataset = tmp_path / "signal.jsonl"
    rows = [signal_example(1), signal_example(2, event_id="event-1")]
    dataset.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="synthetic labels are allowed only for tests"):
        load_signal_dataset(dataset)
    with pytest.raises(ValueError, match="duplicate event/ticker"):
        load_signal_dataset(dataset, allow_synthetic=True)


def test_signal_loader_dispatches_version_two_rows(tmp_path: Path) -> None:
    payload = signal_example(1)
    payload.update(
        schema_version="signal-dataset-example-2.0",
        received_at=payload["decision_at"],
        corporate_action_status="unknown",
        event_group_id="group-1",
        source_url="https://t.me/source/1",
        retrieved_at="2026-02-01T00:00:00+00:00",
        admission_version="test-v2",
        analysis_title="Компания опубликовала результаты",
        analysis_content_sha256="a" * 64,
        receipt_provenance="publication_plus_assumed_5_minutes",
        text_provenance="no_edit_marker_current_view_not_first_snapshot",
    )
    dataset = tmp_path / "signal-v2.jsonl"
    dataset.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    rows = load_signal_dataset(dataset, allow_synthetic=True)

    assert len(rows) == 1
    assert isinstance(rows[0], SignalDatasetExampleV2)


def test_signal_dataset_fingerprint_uses_exact_bytes(tmp_path: Path) -> None:
    dataset = tmp_path / "signal.jsonl"
    payload = b'{"fixture":true}\n'
    dataset.write_bytes(payload)

    assert signal_dataset_sha256(dataset) == hashlib.sha256(payload).hexdigest()


def test_signal_dataset_writer_is_canonical_across_input_order(tmp_path: Path) -> None:
    examples = [
        SignalDatasetExample.model_validate(signal_example(2)),
        SignalDatasetExample.model_validate(signal_example(1)),
    ]
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"

    write_signal_dataset(first, examples)
    write_signal_dataset(second, list(reversed(examples)))

    assert first.read_bytes() == second.read_bytes()
    assert load_signal_dataset(first, allow_synthetic=True) == list(reversed(examples))


def test_temporal_signal_split_purges_boundaries_and_freezes_test() -> None:
    validation_from = datetime(2026, 1, 10, tzinfo=UTC)
    test_from = datetime(2026, 1, 20, tzinfo=UTC)
    test_until = datetime(2026, 2, 1, tzinfo=UTC)
    examples = [
        SignalDatasetExample.model_validate(
            signal_example(1, decision_at=datetime(2026, 1, 1, tzinfo=UTC))
        ),
        SignalDatasetExample.model_validate(
            signal_example(2, decision_at=datetime(2026, 1, 9, tzinfo=UTC))
        ),
        SignalDatasetExample.model_validate(
            signal_example(3, decision_at=datetime(2026, 1, 10, tzinfo=UTC))
        ),
        SignalDatasetExample.model_validate(
            signal_example(4, decision_at=datetime(2026, 1, 15, tzinfo=UTC))
        ),
        SignalDatasetExample.model_validate(
            signal_example(5, decision_at=datetime(2026, 1, 20, tzinfo=UTC))
        ),
        SignalDatasetExample.model_validate(
            signal_example(6, decision_at=datetime(2026, 1, 25, tzinfo=UTC))
        ),
        SignalDatasetExample.model_validate(
            signal_example(7, decision_at=datetime(2026, 2, 2, tzinfo=UTC))
        ),
    ]

    split = temporal_signal_split(
        examples,
        validation_from=validation_from,
        test_from=test_from,
        test_until=test_until,
        embargo=timedelta(days=2),
    )

    assert [example.id for example in split.train] == ["signal-example-1-SBER"]
    assert [example.id for example in split.validation] == [
        "signal-example-3-SBER",
        "signal-example-4-SBER",
    ]
    assert [example.id for example in split.test] == [
        "signal-example-5-SBER",
        "signal-example-6-SBER",
    ]
    assert [example.id for example in split.purged] == ["signal-example-2-SBER"]
    assert [example.id for example in split.future] == ["signal-example-7-SBER"]


def test_temporal_signal_split_purges_crossing_events_and_unavailable_labels() -> None:
    validation_from = datetime(2026, 1, 10, tzinfo=UTC)
    test_from = datetime(2026, 1, 20, tzinfo=UTC)
    test_until = datetime(2026, 2, 1, tzinfo=UTC)
    examples = [
        SignalDatasetExample.model_validate(
            signal_example(1, decision_at=datetime(2026, 1, 1, tzinfo=UTC))
        ),
        SignalDatasetExample.model_validate(
            signal_example(
                2,
                decision_at=datetime(2026, 1, 7, tzinfo=UTC),
                event_id="crossing",
                ticker="SBER",
            )
        ),
        SignalDatasetExample.model_validate(
            signal_example(
                3,
                decision_at=datetime(2026, 1, 10, tzinfo=UTC),
                event_id="crossing",
                ticker="LKOH",
            )
        ),
        SignalDatasetExample.model_validate(
            signal_example(
                4,
                decision_at=datetime(2026, 1, 12, tzinfo=UTC),
                label_available_at=datetime(2026, 1, 21, tzinfo=UTC),
            )
        ),
        SignalDatasetExample.model_validate(
            signal_example(5, decision_at=datetime(2026, 1, 14, tzinfo=UTC))
        ),
        SignalDatasetExample.model_validate(
            signal_example(6, decision_at=datetime(2026, 1, 22, tzinfo=UTC))
        ),
    ]

    split = temporal_signal_split(
        examples,
        validation_from=validation_from,
        test_from=test_from,
        test_until=test_until,
        embargo=timedelta(0),
    )

    assert {example.event_id for example in split.purged} == {"crossing", "event-4"}
    partition_events = split.event_ids()
    assert all(
        not event_ids & other
        for index, event_ids in enumerate(partition_events)
        for other in partition_events[index + 1 :]
    )


def test_version_aware_split_keeps_v2_information_group_in_one_partition() -> None:
    validation_from = datetime(2026, 1, 10, tzinfo=UTC)
    test_from = datetime(2026, 1, 20, tzinfo=UTC)
    test_until = datetime(2026, 2, 1, tzinfo=UTC)
    examples = [
        _version_two_example(1, datetime(2026, 1, 1, tzinfo=UTC), "shared"),
        _version_two_example(2, datetime(2026, 1, 10, tzinfo=UTC), "shared"),
        _version_two_example(3, datetime(2026, 1, 2, tzinfo=UTC), "train"),
        _version_two_example(4, datetime(2026, 1, 11, tzinfo=UTC), "validation"),
        _version_two_example(5, datetime(2026, 1, 20, tzinfo=UTC), "test"),
    ]

    split = split_signal_dataset(
        examples,
        validation_from=validation_from,
        test_from=test_from,
        test_until=test_until,
        embargo=timedelta(0),
    )

    assert {example.event_id for example in split.purged} == {"event-1", "event-2"}
    group_partitions = [
        {example.event_group_id for example in partition}
        for partition in (split.train, split.validation, split.test, split.purged)
    ]
    assert all(
        not groups & other
        for index, groups in enumerate(group_partitions)
        for other in group_partitions[index + 1 :]
    )


def test_version_aware_split_rejects_mixed_schema_versions() -> None:
    version_one = SignalDatasetExample.model_validate(signal_example(1))
    version_two = _version_two_example(
        2,
        datetime(2026, 1, 2, tzinfo=UTC),
        "group-2",
    )

    with pytest.raises(ValueError, match="mixed signal dataset schema versions"):
        split_signal_dataset(
            [version_one, version_two],
            validation_from=datetime(2026, 1, 10, tzinfo=UTC),
            test_from=datetime(2026, 1, 20, tzinfo=UTC),
            test_until=datetime(2026, 2, 1, tzinfo=UTC),
        )


def test_audit_signal_dataset_cli_reports_frozen_partitions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dataset = tmp_path / "signal.jsonl"
    output = tmp_path / "audit.json"
    dates = (
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 10, tzinfo=UTC),
        datetime(2026, 1, 20, tzinfo=UTC),
        datetime(2026, 2, 2, tzinfo=UTC),
    )
    dataset.write_text(
        "\n".join(
            json.dumps(signal_example(index, decision_at=value), ensure_ascii=False)
            for index, value in enumerate(dates, 1)
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "audit_signal_dataset",
            "--dataset",
            str(dataset),
            "--validation-from",
            "2026-01-10T00:00:00+00:00",
            "--test-from",
            "2026-01-20T00:00:00+00:00",
            "--test-until",
            "2026-02-01T00:00:00+00:00",
            "--embargo-hours",
            "0",
            "--allow-synthetic",
            "--output",
            str(output),
        ],
    )

    audit_signal_dataset_main()

    report = json.loads(capsys.readouterr().out)
    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert report["schema_version"] == "signal-dataset-report-2.0"
    assert report["dataset_schema_version"] == "signal-dataset-example-1.0"
    assert report["information_groups"] == 4
    assert report["split_policy"]["grouping_key"] == "event_id"
    assert report["partitions"] == {
        "train": {"observations": 1, "events": 1, "information_groups": 1, "tickers": 1},
        "validation": {
            "observations": 1,
            "events": 1,
            "information_groups": 1,
            "tickers": 1,
        },
        "test": {"observations": 1, "events": 1, "information_groups": 1, "tickers": 1},
        "purged": {"observations": 0, "events": 0, "information_groups": 0, "tickers": 0},
        "future": {
            "observations": 1,
            "events": 1,
            "information_groups": 1,
            "tickers": 1,
        },
    }


def test_audit_signal_dataset_cli_uses_v2_information_groups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dataset = tmp_path / "signal-v2.jsonl"
    examples = [
        _version_two_example(1, datetime(2026, 1, 1, tzinfo=UTC), "train"),
        _version_two_example(2, datetime(2026, 1, 11, tzinfo=UTC), "validation"),
        _version_two_example(3, datetime(2026, 1, 20, tzinfo=UTC), "test"),
        _version_two_example(4, datetime(2026, 2, 2, tzinfo=UTC), "future"),
    ]
    write_signal_dataset(dataset, examples)
    monkeypatch.setattr(
        "sys.argv",
        [
            "audit_signal_dataset",
            "--dataset",
            str(dataset),
            "--validation-from",
            "2026-01-10T00:00:00+00:00",
            "--test-from",
            "2026-01-20T00:00:00+00:00",
            "--test-until",
            "2026-02-01T00:00:00+00:00",
            "--embargo-hours",
            "0",
            "--allow-synthetic",
        ],
    )

    audit_signal_dataset_main()

    report = json.loads(capsys.readouterr().out)
    assert report["dataset_schema_version"] == "signal-dataset-example-2.0"
    assert report["information_groups"] == 4
    assert report["split_policy"]["grouping_key"] == "event_group_id"


def _version_two_example(
    index: int,
    decision_at: datetime,
    event_group_id: str,
) -> SignalDatasetExampleV2:
    payload = signal_example(index, decision_at=decision_at)
    payload.update(
        schema_version="signal-dataset-example-2.0",
        received_at=payload["decision_at"],
        corporate_action_status="unknown",
        event_group_id=event_group_id,
        source_url=f"https://t.me/source/{index}",
        retrieved_at="2026-02-02T00:00:00+00:00",
        admission_version="test-v2",
        analysis_title=f"Компания опубликовала результаты {index}",
        analysis_content_sha256=f"{index:064x}",
        receipt_provenance="publication_plus_assumed_5_minutes",
        text_provenance="no_edit_marker_current_view_not_first_snapshot",
    )
    return SignalDatasetExampleV2.model_validate(payload)
