from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eventedge.collectors import RssItem, is_semantic_analysis_candidate
from eventedge.events import classify_news_event


class QualityLabelSource(StrEnum):
    HUMAN = "human"
    SYNTHETIC_TEST = "synthetic_test"


class QualityLabels(BaseModel):
    """Human decisions used to evaluate routing; never inferred from model output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    relevant: bool
    scope: Literal["none", "market", "sector", "company"]
    target_ids: tuple[str, ...] = ()
    event_type: str | None = None
    direction: Literal["up", "down", "neutral", "unclear"] | None = None

    @model_validator(mode="after")
    def validate_consistency(self) -> QualityLabels:
        if self.relevant and self.scope == "none":
            raise ValueError("a relevant example must have a non-none scope")
        if not self.relevant and self.scope != "none":
            raise ValueError("an irrelevant example must use scope=none")
        if not self.relevant and self.target_ids:
            raise ValueError("an irrelevant example cannot have target_ids")
        if self.scope in {"company", "sector"} and not self.target_ids:
            raise ValueError("company and sector labels require at least one target_id")
        return self


class QualityExample(BaseModel):
    """One immutable, point-in-time quality label for an economic-news event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["quality-example-1.0"] = "quality-example-1.0"
    id: Annotated[str, Field(min_length=1, max_length=200)]
    event_id: Annotated[str, Field(min_length=1, max_length=200)]
    published_at: datetime
    received_at: datetime
    source_id: Annotated[str, Field(min_length=1, max_length=100)]
    title: Annotated[str, Field(min_length=1, max_length=2_000)]
    content: Annotated[str, Field(max_length=50_000)] = ""
    categories: tuple[str, ...] = ()
    source_metadata: dict[str, object] = Field(default_factory=dict)
    labels: QualityLabels
    label_source: QualityLabelSource
    labeler: Annotated[str, Field(min_length=1, max_length=200)]
    labeled_at: datetime
    notes: Annotated[str, Field(max_length=2_000)] = ""

    @model_validator(mode="after")
    def require_point_in_time_metadata(self) -> QualityExample:
        if self.published_at.tzinfo is None or self.published_at.utcoffset() is None:
            raise ValueError("published_at must include a timezone")
        if self.received_at.tzinfo is None or self.received_at.utcoffset() is None:
            raise ValueError("received_at must include a timezone")
        if self.labeled_at.tzinfo is None or self.labeled_at.utcoffset() is None:
            raise ValueError("labeled_at must include a timezone")
        if self.received_at < self.published_at:
            raise ValueError("received_at cannot be earlier than published_at")
        if self.labeled_at < self.received_at:
            raise ValueError("labeled_at cannot be earlier than received_at")
        return self


class QualityDatasetSplit(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    train: tuple[QualityExample, ...]
    validation: tuple[QualityExample, ...]
    test: tuple[QualityExample, ...]
    purged: tuple[QualityExample, ...] = ()

    def event_ids(self) -> tuple[set[str], set[str], set[str], set[str]]:
        return (
            {example.event_id for example in self.train},
            {example.event_id for example in self.validation},
            {example.event_id for example in self.test},
            {example.event_id for example in self.purged},
        )


def load_quality_dataset(
    path: Path,
    *,
    allow_synthetic: bool = False,
) -> list[QualityExample]:
    examples: list[QualityExample] = []
    seen_ids: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            example = QualityExample.model_validate_json(line)
        except Exception as error:
            raise ValueError(f"invalid quality example at line {line_number}: {error}") from error
        if example.id in seen_ids:
            raise ValueError(f"duplicate quality example id at line {line_number}: {example.id}")
        if example.label_source is not QualityLabelSource.HUMAN and not allow_synthetic:
            raise ValueError(
                f"non-human label at line {line_number}; "
                "synthetic labels are allowed only for tests"
            )
        seen_ids.add(example.id)
        examples.append(example)
    if not examples:
        raise ValueError("quality dataset is empty")
    return examples


def temporal_group_split(
    examples: list[QualityExample],
    *,
    train_fraction: float = 0.6,
    validation_fraction: float = 0.2,
) -> QualityDatasetSplit:
    if not 0 < train_fraction < 1:
        raise ValueError("train_fraction must be between 0 and 1")
    if not 0 < validation_fraction < 1:
        raise ValueError("validation_fraction must be between 0 and 1")
    if train_fraction + validation_fraction >= 1:
        raise ValueError("train and validation fractions must leave a test partition")

    grouped: dict[str, list[QualityExample]] = defaultdict(list)
    for example in examples:
        grouped[example.event_id].append(example)
    if len(grouped) < 3:
        raise ValueError("at least three independent event groups are required")

    ordered_groups = sorted(grouped.values(), key=_group_start)
    group_count = len(ordered_groups)
    train_end = max(1, min(group_count - 2, round(group_count * train_fraction)))
    validation_end = max(
        train_end + 1,
        min(group_count - 1, round(group_count * (train_fraction + validation_fraction))),
    )

    def flatten(groups: list[list[QualityExample]]) -> tuple[QualityExample, ...]:
        return tuple(
            sorted(
                (example for group in groups for example in group),
                key=lambda example: (example.received_at, example.id),
            )
        )

    validation_cutoff = _group_start(ordered_groups[train_end])
    test_cutoff = _group_start(ordered_groups[validation_end])
    train_groups: list[list[QualityExample]] = []
    validation_groups: list[list[QualityExample]] = []
    test_groups: list[list[QualityExample]] = []
    purged_groups: list[list[QualityExample]] = []
    for group in ordered_groups:
        start = _group_start(group)
        end = max(example.received_at for example in group)
        if end < validation_cutoff:
            train_groups.append(group)
        elif start >= validation_cutoff and end < test_cutoff:
            validation_groups.append(group)
        elif start >= test_cutoff:
            test_groups.append(group)
        else:
            purged_groups.append(group)

    result = QualityDatasetSplit(
        train=flatten(train_groups),
        validation=flatten(validation_groups),
        test=flatten(test_groups),
        purged=flatten(purged_groups),
    )
    train_events, validation_events, test_events, purged_events = result.event_ids()
    leaked_events = (
        train_events & validation_events
        or train_events & test_events
        or validation_events & test_events
    )
    if leaked_events:
        raise AssertionError("event leakage detected between dataset partitions")
    if purged_events & (train_events | validation_events | test_events):
        raise AssertionError("purged event leaked into a dataset partition")
    if not result.train or not result.validation or not result.test:
        raise ValueError("temporal split produced an empty train, validation or test partition")
    return result


def _group_start(group: list[QualityExample]) -> datetime:
    return min(example.received_at for example in group)


def evaluate_current_router(examples: list[QualityExample]) -> dict[str, object]:
    true_positive = false_positive = false_negative = true_negative = 0
    scope_correct = 0
    relevant_count = 0
    rows = []

    for example in examples:
        item = RssItem(
            external_id=example.id,
            published_at=example.published_at.astimezone(UTC),
            title=example.title,
            url=f"quality://{example.id}",
            content=example.content,
            categories=example.categories,
        )
        predicted_relevant = is_semantic_analysis_candidate(item)
        projection = classify_news_event(
            title=example.title,
            content=example.content,
            source_metadata=example.source_metadata,
        )
        predicted_scope = str(projection["scope"]) if predicted_relevant else "none"

        if predicted_relevant and example.labels.relevant:
            true_positive += 1
        elif predicted_relevant:
            false_positive += 1
        elif example.labels.relevant:
            false_negative += 1
        else:
            true_negative += 1

        if example.labels.relevant:
            relevant_count += 1
            scope_correct += predicted_scope == example.labels.scope
        rows.append(
            {
                "id": example.id,
                "event_id": example.event_id,
                "expected_relevant": example.labels.relevant,
                "predicted_relevant": predicted_relevant,
                "expected_scope": example.labels.scope,
                "predicted_scope": predicted_scope,
            }
        )

    predicted_positive = true_positive + false_positive
    actual_positive = true_positive + false_negative
    return {
        "schema_version": "quality-report-1.0",
        "observations": len(examples),
        "independent_events": len({example.event_id for example in examples}),
        "relevance": {
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "true_negative": true_negative,
            "precision": (
                round(true_positive / predicted_positive, 4) if predicted_positive else None
            ),
            "recall": round(true_positive / actual_positive, 4) if actual_positive else None,
        },
        "scope": {
            "observations": relevant_count,
            "accuracy": round(scope_correct / relevant_count, 4) if relevant_count else None,
        },
        "predictions": rows,
    }
