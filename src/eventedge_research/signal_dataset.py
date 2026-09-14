from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eventedge_research.research_artifacts import iter_jsonl_objects, write_jsonl

DatasetId = Annotated[str, Field(min_length=1, max_length=200)]
Ticker = Annotated[str, Field(pattern=r"^[A-Z0-9._-]{1,32}$")]
FOUR_HOUR_HORIZON = timedelta(hours=4)
MAX_OUTCOME_OBSERVATION_LAG = timedelta(minutes=20)


class SignalLabelSource(StrEnum):
    """Provenance of the market outcome used as a signal label."""

    MARKET_OUTCOME = "market_outcome"
    SYNTHETIC_TEST = "synthetic_test"


class MarketOutcomeObservation(BaseModel):
    """One timely price observation used to calculate a future return."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    schema_version: Literal["market-outcome-observation-1.0"] = (
        "market-outcome-observation-1.0"
    )
    target_at: datetime
    observed_at: datetime
    price: Annotated[float, Field(gt=0)]
    return_pct: Annotated[float, Field(ge=-100)]
    benchmark_id: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    benchmark_return_pct: float | None = None
    abnormal_return_pct: float | None = None
    price_field: Literal["open"] = "open"
    timely: Literal[True] = True

    @model_validator(mode="after")
    def validate_observation(self) -> MarketOutcomeObservation:
        _require_aware("target_at", self.target_at)
        _require_aware("observed_at", self.observed_at)
        if self.observed_at < self.target_at:
            raise ValueError("observed_at cannot be earlier than target_at")
        benchmark_values = (
            self.benchmark_id,
            self.benchmark_return_pct,
            self.abnormal_return_pct,
        )
        if any(value is not None for value in benchmark_values) and any(
            value is None for value in benchmark_values
        ):
            raise ValueError("benchmark fields must be all present or all absent")
        if self.benchmark_return_pct is not None and not math.isclose(
            self.abnormal_return_pct or 0,
            self.return_pct - self.benchmark_return_pct,
            abs_tol=0.02,
        ):
            raise ValueError("abnormal_return_pct is inconsistent with returns")
        return self


class SignalFeatureSnapshot(BaseModel):
    """Structured features known no later than the simulated decision time."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    schema_version: Literal["signal-features-1.0"] = "signal-features-1.0"
    as_of: datetime
    event_type: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    polarity: Annotated[float, Field(ge=-1, le=1)] | None = None
    materiality: Annotated[float, Field(ge=0, le=1)] | None = None
    novelty: Annotated[float, Field(ge=0, le=1)] | None = None
    source_quality: Annotated[float, Field(ge=0, le=1)] | None = None
    fact_count: Annotated[int, Field(ge=0, le=100)] | None = None
    semantic_extractor_version: Annotated[
        str,
        Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,63}$"),
    ] | None = None
    semantic_feature_set_id: DatasetId | None = None
    rule_direction: Literal["up", "down", "neutral"] | None = None
    rule_score: Annotated[float, Field(ge=-100, le=100)] | None = None
    rule_confidence: Annotated[float, Field(ge=0, le=1)] | None = None
    rule_model_version: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    rule_config_version: Annotated[int, Field(ge=1)] | None = None
    pre_event_return_1h_pct: float | None = None
    pre_event_return_1d_pct: float | None = None
    pre_event_return_5d_pct: float | None = None
    benchmark_pre_event_return_1h_pct: float | None = None
    volatility_20d_pct: Annotated[float, Field(ge=0)] | None = None
    volume_ratio: Annotated[float, Field(ge=0)] | None = None
    liquidity_status: Literal["sufficient", "limited", "unavailable"] | None = None

    @model_validator(mode="after")
    def validate_snapshot(self) -> SignalFeatureSnapshot:
        _require_aware("features.as_of", self.as_of)
        rule_fields = (
            self.rule_direction,
            self.rule_score,
            self.rule_confidence,
            self.rule_model_version,
            self.rule_config_version,
        )
        if any(value is not None for value in rule_fields) and any(
            value is None for value in rule_fields
        ):
            raise ValueError("rule feature fields must be all present or all absent")
        if self.semantic_feature_set_id is not None and self.semantic_extractor_version is None:
            raise ValueError("semantic feature set id requires its extractor version")
        return self


class SignalDatasetExample(BaseModel):
    """Immutable point-in-time training row for one canonical event and ticker."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    schema_version: Literal["signal-dataset-example-1.0"] = "signal-dataset-example-1.0"
    id: DatasetId
    event_id: DatasetId
    ticker: Ticker
    news_ids: Annotated[tuple[DatasetId, ...], Field(min_length=1, max_length=100)]
    primary_news_id: DatasetId
    source_id: Annotated[str, Field(min_length=1, max_length=100)]
    corroborating_source_ids: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=100)], ...],
        Field(max_length=100),
    ] = ()
    title: Annotated[str, Field(min_length=1, max_length=2_000)]
    content: Annotated[str, Field(max_length=50_000)] = ""
    categories: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=200)], ...],
        Field(max_length=100),
    ] = ()
    published_at: datetime
    received_at: datetime
    decision_at: datetime
    entry_at: datetime
    entry_price: Annotated[float, Field(gt=0)]
    features: SignalFeatureSnapshot
    outcome_4h: MarketOutcomeObservation
    label_available_at: datetime
    label_source: SignalLabelSource
    corporate_action_status: Literal["unknown", "none", "adjusted"]
    overlapping_event_ids: Annotated[tuple[DatasetId, ...], Field(max_length=100)] = ()
    notes: Annotated[str, Field(max_length=2_000)] = ""

    @model_validator(mode="after")
    def require_point_in_time_provenance(self) -> SignalDatasetExample:
        timestamps = {
            "published_at": self.published_at,
            "received_at": self.received_at,
            "decision_at": self.decision_at,
            "entry_at": self.entry_at,
            "label_available_at": self.label_available_at,
        }
        for name, value in timestamps.items():
            _require_aware(name, value)
        if not self.published_at <= self.received_at <= self.decision_at <= self.entry_at:
            raise ValueError(
                "published_at, received_at, decision_at and entry_at must be chronological"
            )
        if self.features.as_of > self.decision_at:
            raise ValueError("features.as_of cannot be later than decision_at")
        if self.outcome_4h.target_at < self.entry_at:
            raise ValueError("outcome target cannot be earlier than entry_at")
        if self.outcome_4h.target_at != self.entry_at + FOUR_HOUR_HORIZON:
            raise ValueError("outcome_4h target must be exactly four hours after entry_at")
        if (
            self.outcome_4h.observed_at - self.outcome_4h.target_at
            > MAX_OUTCOME_OBSERVATION_LAG
        ):
            raise ValueError("outcome_4h observation exceeds the 20-minute lag limit")
        if self.label_available_at < self.outcome_4h.observed_at:
            raise ValueError("label cannot be available before its price observation")
        if self.primary_news_id not in self.news_ids:
            raise ValueError("primary_news_id must be included in news_ids")
        if len(set(self.news_ids)) != len(self.news_ids):
            raise ValueError("news_ids must be unique")
        if len(set(self.corroborating_source_ids)) != len(self.corroborating_source_ids):
            raise ValueError("corroborating_source_ids must be unique")
        if self.source_id in self.corroborating_source_ids:
            raise ValueError("source_id cannot be repeated in corroborating_source_ids")
        if len(set(self.overlapping_event_ids)) != len(self.overlapping_event_ids):
            raise ValueError("overlapping_event_ids must be unique")
        if self.event_id in self.overlapping_event_ids:
            raise ValueError("event_id cannot overlap itself")
        expected_return = (self.outcome_4h.price / self.entry_price - 1) * 100
        if not math.isclose(self.outcome_4h.return_pct, expected_return, abs_tol=0.02):
            raise ValueError("outcome return_pct is inconsistent with entry and observed prices")
        return self


class SignalDatasetSplit(BaseModel):
    """Chronological event-group partitions with a frozen test window."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    train: tuple[SignalDatasetExample, ...]
    validation: tuple[SignalDatasetExample, ...]
    test: tuple[SignalDatasetExample, ...]
    purged: tuple[SignalDatasetExample, ...] = ()
    future: tuple[SignalDatasetExample, ...] = ()

    def event_ids(self) -> tuple[set[str], set[str], set[str], set[str], set[str]]:
        return (
            {example.event_id for example in self.train},
            {example.event_id for example in self.validation},
            {example.event_id for example in self.test},
            {example.event_id for example in self.purged},
            {example.event_id for example in self.future},
        )


def load_signal_dataset(
    path: Path,
    *,
    allow_synthetic: bool = False,
) -> list[SignalDatasetExample]:
    """Load a JSONL signal dataset and reject ambiguous or unsafe rows."""
    examples: list[SignalDatasetExample] = []
    seen_ids: set[str] = set()
    seen_event_tickers: set[tuple[str, str]] = set()
    for line_number, payload in enumerate(iter_jsonl_objects(path), 1):
        try:
            if payload.get("schema_version") == "signal-dataset-example-2.0":
                from eventedge_research.signal_dataset_v2 import SignalDatasetExampleV2

                example = SignalDatasetExampleV2.model_validate(payload)
            else:
                example = SignalDatasetExample.model_validate(payload)
        except Exception as error:
            raise ValueError(
                f"invalid signal dataset example at row {line_number}: {error}"
            ) from error
        _validate_unique_example(
            example,
            line_number=line_number,
            seen_ids=seen_ids,
            seen_event_tickers=seen_event_tickers,
        )
        if example.label_source is SignalLabelSource.SYNTHETIC_TEST and not allow_synthetic:
            raise ValueError(
                f"synthetic market outcome at row {line_number}; "
                "synthetic labels are allowed only for tests"
            )
        examples.append(example)
    if not examples:
        raise ValueError("signal dataset is empty")
    return examples


def signal_dataset_sha256(path: Path) -> str:
    """Return a bounded-memory fingerprint of the exact dataset bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as dataset:
        while chunk := dataset.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def write_signal_dataset(
    path: Path,
    examples: list[SignalDatasetExample],
) -> None:
    """Atomically write canonical JSONL ordered by decision and identity."""
    if not examples:
        raise ValueError("cannot write an empty signal dataset")
    seen_ids: set[str] = set()
    seen_event_tickers: set[tuple[str, str]] = set()
    ordered = sorted(
        examples,
        key=lambda example: (example.decision_at, example.event_id, example.ticker, example.id),
    )
    for line_number, example in enumerate(ordered, 1):
        _validate_unique_example(
            example,
            line_number=line_number,
            seen_ids=seen_ids,
            seen_event_tickers=seen_event_tickers,
        )
    write_jsonl(
        path,
        (example.model_dump(mode="json") for example in ordered),
    )


def temporal_signal_split(
    examples: list[SignalDatasetExample],
    *,
    validation_from: datetime,
    test_from: datetime,
    test_until: datetime,
    embargo: timedelta = timedelta(days=3),
) -> SignalDatasetSplit:
    """Split event groups without label overlap and freeze the test decision window."""
    _validate_split_boundaries(validation_from, test_from, test_until, embargo)
    grouped: dict[str, list[SignalDatasetExample]] = defaultdict(list)
    for example in examples:
        grouped[example.event_id].append(example)

    partitions: dict[str, list[list[SignalDatasetExample]]] = defaultdict(list)
    for group in sorted(grouped.values(), key=_group_start):
        partitions[
            _partition_name(
                group,
                validation_from=validation_from,
                test_from=test_from,
                test_until=test_until,
                embargo=embargo,
            )
        ].append(group)

    result = SignalDatasetSplit(
        train=_flatten(partitions["train"]),
        validation=_flatten(partitions["validation"]),
        test=_flatten(partitions["test"]),
        purged=_flatten(partitions["purged"]),
        future=_flatten(partitions["future"]),
    )
    _validate_partition_events(result)
    if not result.train or not result.validation or not result.test:
        raise ValueError("temporal split produced an empty train, validation or test partition")
    return result


def split_signal_dataset(
    examples: list[SignalDatasetExample],
    *,
    validation_from: datetime,
    test_from: datetime,
    test_until: datetime,
    embargo: timedelta = timedelta(days=3),
) -> SignalDatasetSplit:
    """Split a homogeneous dataset with its version-specific grouping contract.

    Version 1 rows use ``event_id`` as the independent information unit. Version
    2 rows can carry distinct update IDs for the same underlying event, so their
    ``event_group_id`` is kept wholly within one partition. Mixing versions is
    rejected because the two identifiers do not define a common grouping policy.
    """
    version = signal_dataset_schema_version(examples)
    if version == "signal-dataset-example-1.0":
        return temporal_signal_split(
            examples,
            validation_from=validation_from,
            test_from=test_from,
            test_until=test_until,
            embargo=embargo,
        )
    if version == "signal-dataset-example-2.0":
        from eventedge_research.signal_dataset_v2 import (
            SignalDatasetExampleV2,
            split_information_updates,
        )

        if not all(
            isinstance(example, SignalDatasetExampleV2)
            for example in examples
        ):
            raise ValueError("version 2 rows must use SignalDatasetExampleV2")
        version_two_examples = [
            example for example in examples if isinstance(example, SignalDatasetExampleV2)
        ]
        return split_information_updates(
            version_two_examples,
            validation_from=validation_from,
            test_from=test_from,
            test_until=test_until,
            embargo=embargo,
        )
    raise ValueError(f"unsupported signal dataset schema version: {version}")


def signal_dataset_schema_version(examples: Sequence[SignalDatasetExample]) -> str:
    """Return the single supported schema version shared by all examples."""
    versions = {example.schema_version for example in examples}
    if not versions:
        raise ValueError("signal dataset is empty")
    if len(versions) != 1:
        raise ValueError("cannot use mixed signal dataset schema versions")
    version = next(iter(versions))
    if version not in {"signal-dataset-example-1.0", "signal-dataset-example-2.0"}:
        raise ValueError(f"unsupported signal dataset schema version: {version}")
    return version


def signal_information_group_id(example: SignalDatasetExample) -> str:
    """Return the independent information-unit identity for a dataset row."""
    if example.schema_version == "signal-dataset-example-1.0":
        return example.event_id
    group_id = getattr(example, "event_group_id", None)
    if not isinstance(group_id, str) or not group_id:
        raise ValueError("version 2 row has no event_group_id")
    return group_id


def signal_dataset_report(
    examples: list[SignalDatasetExample],
    split: SignalDatasetSplit,
    *,
    dataset_sha256: str,
    validation_from: datetime,
    test_from: datetime,
    test_until: datetime,
    embargo: timedelta,
) -> dict[str, object]:
    """Build a compact, reproducible audit report for a signal dataset."""
    directions = Counter(_outcome_direction(example) for example in examples)
    partitions = {
        name: _partition_summary(partition)
        for name, partition in (
            ("train", split.train),
            ("validation", split.validation),
            ("test", split.test),
            ("purged", split.purged),
            ("future", split.future),
        )
    }
    dataset_schema_version = signal_dataset_schema_version(examples)
    return {
        "schema_version": "signal-dataset-report-2.0",
        "dataset_schema_version": dataset_schema_version,
        "dataset_sha256": dataset_sha256,
        "observations": len(examples),
        "independent_events": len({example.event_id for example in examples}),
        "information_groups": len(
            {signal_information_group_id(example) for example in examples}
        ),
        "tickers": sorted({example.ticker for example in examples}),
        "sources": sorted(
            {
                source_id
                for example in examples
                for source_id in (example.source_id, *example.corroborating_source_ids)
            }
        ),
        "label_sources": sorted({example.label_source.value for example in examples}),
        "outcome_direction": dict(sorted(directions.items())),
        "decision_window": {
            "from": min(example.decision_at for example in examples).isoformat(),
            "through": max(example.decision_at for example in examples).isoformat(),
        },
        "split_policy": {
            "grouping_key": (
                "event_group_id"
                if dataset_schema_version == "signal-dataset-example-2.0"
                else "event_id"
            ),
            "validation_from": validation_from.isoformat(),
            "test_from": test_from.isoformat(),
            "test_until_exclusive": test_until.isoformat(),
            "embargo_seconds": round(embargo.total_seconds()),
        },
        "partitions": partitions,
    }


def _validate_unique_example(
    example: SignalDatasetExample,
    *,
    line_number: int,
    seen_ids: set[str],
    seen_event_tickers: set[tuple[str, str]],
) -> None:
    if example.id in seen_ids:
        raise ValueError(f"duplicate signal dataset id at line {line_number}: {example.id}")
    event_ticker = (example.event_id, example.ticker)
    if event_ticker in seen_event_tickers:
        raise ValueError(
            f"duplicate event/ticker at line {line_number}: {example.event_id}/{example.ticker}"
        )
    seen_ids.add(example.id)
    seen_event_tickers.add(event_ticker)


def _validate_split_boundaries(
    validation_from: datetime,
    test_from: datetime,
    test_until: datetime,
    embargo: timedelta,
) -> None:
    for name, value in (
        ("validation_from", validation_from),
        ("test_from", test_from),
        ("test_until", test_until),
    ):
        _require_aware(name, value)
    if not validation_from < test_from < test_until:
        raise ValueError("split boundaries must be strictly chronological")
    if embargo < timedelta(0):
        raise ValueError("embargo cannot be negative")


def _partition_name(
    group: list[SignalDatasetExample],
    *,
    validation_from: datetime,
    test_from: datetime,
    test_until: datetime,
    embargo: timedelta,
) -> str:
    start = _group_start(group)
    end = max(example.decision_at for example in group)
    labels_available = max(example.label_available_at for example in group)
    if end < validation_from - embargo and labels_available < validation_from:
        return "train"
    if (
        start >= validation_from
        and end < test_from - embargo
        and labels_available < test_from
    ):
        return "validation"
    if start >= test_from and end < test_until:
        return "test"
    if start >= test_until:
        return "future"
    return "purged"


def _flatten(groups: list[list[SignalDatasetExample]]) -> tuple[SignalDatasetExample, ...]:
    return tuple(
        sorted(
            (example for group in groups for example in group),
            key=lambda example: (example.decision_at, example.id),
        )
    )


def _validate_partition_events(split: SignalDatasetSplit) -> None:
    event_sets = split.event_ids()
    for index, event_ids in enumerate(event_sets):
        for other in event_sets[index + 1 :]:
            if event_ids & other:
                raise AssertionError("event leakage detected between signal dataset partitions")


def _group_start(group: list[SignalDatasetExample]) -> datetime:
    return min(example.decision_at for example in group)


def _outcome_direction(example: SignalDatasetExample) -> str:
    if example.outcome_4h.return_pct > 0:
        return "up"
    if example.outcome_4h.return_pct < 0:
        return "down"
    return "flat"


def _partition_summary(examples: tuple[SignalDatasetExample, ...]) -> dict[str, int]:
    return {
        "observations": len(examples),
        "events": len({example.event_id for example in examples}),
        "information_groups": len(
            {signal_information_group_id(example) for example in examples}
        ),
        "tickers": len({example.ticker for example in examples}),
    }


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
