from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from eventedge.quality import (
    QualityExample,
    evaluate_current_router,
    load_quality_dataset,
    temporal_group_split,
)
from scripts.evaluate_quality_dataset import main as evaluate_quality_main


def quality_example(
    index: int,
    *,
    event_id: str | None = None,
    relevant: bool = True,
    scope: str = "company",
    title: str = "Сбербанк опубликовал финансовые результаты",
    target_ids: list[str] | None = None,
    label_source: str = "synthetic_test",
) -> dict[str, object]:
    published_at = datetime(2026, 8, 1, tzinfo=UTC) + timedelta(days=index)
    received_at = published_at + timedelta(minutes=2)
    return {
        "schema_version": "quality-example-1.0",
        "id": f"quality-{index}",
        "event_id": event_id or f"event-{index}",
        "published_at": published_at.isoformat(),
        "received_at": received_at.isoformat(),
        "source_id": "fixture",
        "title": title,
        "content": "Чистая прибыль выросла на 12%.",
        "categories": ["Экономика"],
        "source_metadata": {"tickers": ["SBER"]} if scope == "company" else {},
        "labels": {
            "relevant": relevant,
            "scope": scope,
            "target_ids": target_ids if target_ids is not None else ["SBER"],
            "event_type": "financial_results" if relevant else None,
            "direction": "up" if relevant else None,
        },
        "label_source": label_source,
        "labeler": "test-suite",
        "labeled_at": (received_at + timedelta(hours=1)).isoformat(),
        "notes": "Synthetic contract fixture; not financial ground truth.",
    }


def test_quality_example_requires_consistent_labels_and_timezone() -> None:
    invalid_scope = quality_example(1, relevant=False, scope="company", target_ids=[])
    with pytest.raises(ValidationError, match="irrelevant example must use scope=none"):
        QualityExample.model_validate(invalid_scope)

    naive_timestamp = quality_example(2)
    naive_timestamp["received_at"] = "2026-08-01T10:00:00"
    with pytest.raises(ValidationError, match="received_at must include a timezone"):
        QualityExample.model_validate(naive_timestamp)

    received_too_early = quality_example(3)
    received_too_early["received_at"] = "2026-08-01T10:00:00Z"
    with pytest.raises(ValidationError, match="received_at cannot be earlier"):
        QualityExample.model_validate(received_too_early)


def test_dataset_loader_rejects_synthetic_labels_by_default(tmp_path: Path) -> None:
    dataset = tmp_path / "quality.jsonl"
    dataset.write_text(
        json.dumps(quality_example(1, label_source="synthetic_test"), ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="synthetic labels are allowed only for tests"):
        load_quality_dataset(dataset)

    loaded = load_quality_dataset(dataset, allow_synthetic=True)
    assert loaded[0].labeler == "test-suite"


def test_dataset_loader_reports_line_and_rejects_duplicate_ids(tmp_path: Path) -> None:
    dataset = tmp_path / "quality.jsonl"
    row = json.dumps(quality_example(1, label_source="human"), ensure_ascii=False)
    dataset.write_text(f"{row}\n{row}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate quality example id at line 2"):
        load_quality_dataset(dataset)


def test_quality_cli_emits_report_and_purged_split(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dataset = tmp_path / "quality.jsonl"
    rows = [quality_example(index) for index in range(1, 6)]
    dataset.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "evaluate_quality_dataset",
            "--dataset",
            str(dataset),
            "--allow-synthetic",
            "--include-split",
        ],
    )

    evaluate_quality_main()

    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == "quality-report-1.0"
    assert report["observations"] == 5
    assert report["split"] == {
        "train": 3,
        "validation": 1,
        "test": 1,
        "purged": 0,
        "train_events": 3,
        "validation_events": 1,
        "test_events": 1,
        "purged_events": 0,
    }


def test_temporal_split_keeps_event_groups_together() -> None:
    examples = [
        QualityExample.model_validate(quality_example(1, event_id="shared-event")),
        QualityExample.model_validate(quality_example(2, event_id="shared-event")),
        QualityExample.model_validate(quality_example(3)),
        QualityExample.model_validate(quality_example(4)),
        QualityExample.model_validate(quality_example(5)),
    ]

    split = temporal_group_split(examples, train_fraction=0.5, validation_fraction=0.25)
    train_events, validation_events, test_events, purged_events = split.event_ids()

    assert not train_events & validation_events
    assert not train_events & test_events
    assert not validation_events & test_events
    assert not purged_events
    assert sum(map(len, (split.train, split.validation, split.test, split.purged))) == len(
        examples
    )
    assert {item.id for item in split.train if item.event_id == "shared-event"} == {
        "quality-1",
        "quality-2",
    }


def test_temporal_split_purges_event_crossing_a_boundary() -> None:
    examples = [
        QualityExample.model_validate(quality_example(1, event_id="crossing")),
        QualityExample.model_validate(quality_example(5, event_id="crossing")),
        QualityExample.model_validate(quality_example(2)),
        QualityExample.model_validate(quality_example(3)),
        QualityExample.model_validate(quality_example(4)),
        QualityExample.model_validate(quality_example(6)),
    ]

    split = temporal_group_split(examples, train_fraction=0.4, validation_fraction=0.2)

    assert {example.event_id for example in split.purged} == {"crossing"}
    active_ids = {
        example.event_id
        for partition in (split.train, split.validation, split.test)
        for example in partition
    }
    assert "crossing" not in active_ids


def test_temporal_split_requires_independent_events() -> None:
    examples = [
        QualityExample.model_validate(quality_example(1, event_id="one")),
        QualityExample.model_validate(quality_example(2, event_id="two")),
    ]

    with pytest.raises(ValueError, match="three independent event groups"):
        temporal_group_split(examples)


def test_router_report_handles_zero_denominators_and_records_predictions() -> None:
    examples = [
        QualityExample.model_validate(
            quality_example(
                1,
                relevant=False,
                scope="none",
                target_ids=[],
                title="Фестиваль открылся в городском парке",
            )
        )
    ]

    report = evaluate_current_router(examples)

    assert report["relevance"] == {
        "true_positive": 0,
        "false_positive": 0,
        "false_negative": 0,
        "true_negative": 1,
        "precision": None,
        "recall": None,
    }
    assert report["scope"] == {"observations": 0, "accuracy": None}
    assert report["predictions"][0]["predicted_scope"] == "none"
