from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TOKEN_PATTERN = re.compile(r"[0-9a-zа-яё]{2,}", re.IGNORECASE)
MAX_VOCABULARY_SIZE = 1_024
MAX_ARTIFACT_BYTES = 1_000_000
MAX_TOKEN_LENGTH = 64
MAX_CATEGORIES = 50
MAX_CATEGORY_LENGTH = 200
MIN_TRAINING_EXAMPLES = 100
MIN_EVALUATION_EXAMPLES = 30
MIN_CLASS_EXAMPLES = 10
MIN_REJECTION_PRECISION = 0.90
MIN_RELEVANT_RECALL = 0.98
MIN_REJECTION_COVERAGE = 0.05


class MlRouterMode(StrEnum):
    DISABLED = "disabled"
    SHADOW = "shadow"
    ENFORCE = "enforce"


class MlRouterSafetyMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observations: Annotated[int, Field(ge=0)]
    relevant: Annotated[int, Field(ge=0)]
    irrelevant: Annotated[int, Field(ge=0)]
    rejected: Annotated[int, Field(ge=0)]
    false_rejections: Annotated[int, Field(ge=0)]
    rejection_precision: Annotated[float | None, Field(ge=0, le=1)]
    relevant_recall: Annotated[float | None, Field(ge=0, le=1)]
    rejection_coverage: Annotated[float, Field(ge=0, le=1)]

    @model_validator(mode="after")
    def validate_counts(self) -> MlRouterSafetyMetrics:
        if self.relevant + self.irrelevant != self.observations:
            raise ValueError("metric class counts must equal observations")
        if self.rejected > self.observations:
            raise ValueError("rejected cannot exceed observations")
        if self.false_rejections > min(self.relevant, self.rejected):
            raise ValueError("false_rejections exceeds its class count")
        if self.rejected - self.false_rejections > self.irrelevant:
            raise ValueError("correct rejections exceed irrelevant observations")
        expected_precision = (
            round((self.rejected - self.false_rejections) / self.rejected, 6)
            if self.rejected
            else None
        )
        expected_recall = (
            round((self.relevant - self.false_rejections) / self.relevant, 6)
            if self.relevant
            else None
        )
        expected_coverage = round(self.rejected / self.observations, 6) if self.observations else 0
        if self.rejection_precision != expected_precision:
            raise ValueError("rejection_precision is inconsistent with counts")
        if self.relevant_recall != expected_recall:
            raise ValueError("relevant_recall is inconsistent with counts")
        if self.rejection_coverage != expected_coverage:
            raise ValueError("rejection_coverage is inconsistent with counts")
        return self


class MlRouterArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["ml-router-artifact-1.0"] = "ml-router-artifact-1.0"
    model_version: Literal["ml-router-nb-0.1.0"] = "ml-router-nb-0.1.0"
    label_source: Literal["human", "synthetic_test"]
    trained_at: datetime
    dataset_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    training_examples: Annotated[int, Field(ge=1)]
    training_events: Annotated[int, Field(ge=1)]
    train_received_at_through: datetime
    validation_received_at_from: datetime
    validation_received_at_through: datetime
    test_received_at_from: datetime
    test_received_at_through: datetime
    labels_available_through: datetime
    vocabulary: Annotated[dict[str, float], Field(min_length=1, max_length=MAX_VOCABULARY_SIZE)]
    intercept: float
    reject_threshold: Annotated[float, Field(gt=0, lt=0.5)] = 0.20
    accept_threshold: Annotated[float, Field(gt=0.5, lt=1)] = 0.80
    validation: MlRouterSafetyMetrics
    test: MlRouterSafetyMetrics

    @model_validator(mode="after")
    def validate_artifact(self) -> MlRouterArtifact:
        timestamps = (
            self.trained_at,
            self.train_received_at_through,
            self.validation_received_at_from,
            self.validation_received_at_through,
            self.test_received_at_from,
            self.test_received_at_through,
            self.labels_available_through,
        )
        if any(value.tzinfo is None or value.utcoffset() is None for value in timestamps):
            raise ValueError("artifact timestamps must include a timezone")
        if not (
            self.train_received_at_through < self.validation_received_at_from
            <= self.validation_received_at_through
            < self.test_received_at_from
            <= self.test_received_at_through
        ):
            raise ValueError("artifact partition windows must be strictly chronological")
        if not self.test_received_at_through <= self.labels_available_through <= self.trained_at:
            raise ValueError("artifact cannot use labels unavailable at training time")
        values = (self.intercept, *self.vocabulary.values())
        if not all(math.isfinite(value) for value in values):
            raise ValueError("model weights must be finite")
        if any(
            len(feature) > 2 * MAX_TOKEN_LENGTH + len("title_bigram:_")
            or not feature.startswith(("word:", "title_bigram:", "category:"))
            for feature in self.vocabulary
        ):
            raise ValueError("vocabulary contains an invalid feature")
        if self.reject_threshold >= self.accept_threshold:
            raise ValueError("reject_threshold must be below accept_threshold")
        return self

    def production_gate(self) -> dict[str, object]:
        checks = [
            self.label_source == "human",
            self.training_examples >= MIN_TRAINING_EXAMPLES,
            self.training_events >= MIN_TRAINING_EXAMPLES,
            _metrics_pass(self.validation),
            _metrics_pass(self.test),
        ]
        return {
            "passed": all(checks),
            "label_source": self.label_source,
            "minimum_training_examples": MIN_TRAINING_EXAMPLES,
            "minimum_evaluation_examples": MIN_EVALUATION_EXAMPLES,
            "minimum_class_examples": MIN_CLASS_EXAMPLES,
            "minimum_rejection_precision": MIN_REJECTION_PRECISION,
            "minimum_relevant_recall": MIN_RELEVANT_RECALL,
            "minimum_rejection_coverage": MIN_REJECTION_COVERAGE,
        }


class MlRouterDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_version: str
    probability_relevant: Annotated[float, Field(ge=0, le=1)]
    action: Literal["accept", "reject", "abstain"]

    def as_metadata(self, *, mode: MlRouterMode) -> dict[str, object]:
        return {
            "model_version": self.model_version,
            "mode": mode.value,
            "probability_relevant": self.probability_relevant,
            "action": self.action,
        }


@dataclass(frozen=True)
class MlRouterTrainingRow:
    id: str
    event_id: str
    title: str
    content: str
    categories: tuple[str, ...]
    relevant: bool
    received_at: datetime
    labeled_at: datetime


@dataclass(frozen=True)
class MlRouterRuntime:
    artifact: MlRouterArtifact
    mode: MlRouterMode

    def predict(
        self,
        *,
        title: str,
        content: str,
        categories: Sequence[str],
    ) -> MlRouterDecision:
        probability = _predict_probability(
            self.artifact.intercept,
            self.artifact.vocabulary,
            _router_features(title=title, content=content, categories=categories),
        )
        if probability <= self.artifact.reject_threshold:
            action = "reject"
        elif probability >= self.artifact.accept_threshold:
            action = "accept"
        else:
            action = "abstain"
        return MlRouterDecision(
            model_version=self.artifact.model_version,
            probability_relevant=round(probability, 6),
            action=action,
        )

    def status(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "model_version": self.artifact.model_version,
            "label_source": self.artifact.label_source,
            "production_gate": self.artifact.production_gate(),
        }


def runtime_from_environment(
    environment: Mapping[str, str],
) -> MlRouterRuntime | None:
    try:
        mode = MlRouterMode(environment.get("ML_ROUTER_MODE", "disabled").casefold())
    except ValueError as error:
        raise RuntimeError("ML_ROUTER_MODE must be disabled, shadow or enforce") from error
    artifact_path = environment.get("ML_ROUTER_ARTIFACT_PATH", "").strip()
    if not artifact_path:
        if mode is not MlRouterMode.DISABLED:
            raise RuntimeError("ML_ROUTER_ARTIFACT_PATH is required when ML router is enabled")
        return None
    try:
        path = Path(artifact_path)
        if path.stat().st_size > MAX_ARTIFACT_BYTES:
            raise ValueError("artifact exceeds the size limit")
        artifact = MlRouterArtifact.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except Exception as error:
        raise RuntimeError("ML router artifact is invalid") from error
    if environment.get("APP_ENV") == "prod" and artifact.label_source != "human":
        raise RuntimeError("production ML router requires human labels")
    if mode is MlRouterMode.ENFORCE and not artifact.production_gate()["passed"]:
        raise RuntimeError("ML router artifact does not pass the production gate")
    if mode is MlRouterMode.DISABLED:
        return None
    return MlRouterRuntime(artifact=artifact, mode=mode)


def build_ml_router_artifact(
    *,
    train: Sequence[MlRouterTrainingRow],
    validation: Sequence[MlRouterTrainingRow],
    test: Sequence[MlRouterTrainingRow],
    label_source: Literal["human", "synthetic_test"],
    trained_at: datetime,
    dataset_sha256: str,
    vocabulary_size: int = MAX_VOCABULARY_SIZE,
    reject_threshold: float = 0.20,
    accept_threshold: float = 0.80,
) -> MlRouterArtifact:
    if trained_at.tzinfo is None or trained_at.utcoffset() is None:
        raise ValueError("trained_at must include a timezone")
    if not train or not validation or not test:
        raise ValueError("train, validation and test must be non-empty")
    partitions = (train, validation, test)
    if any(
        row.received_at.tzinfo is None
        or row.received_at.utcoffset() is None
        or row.labeled_at.tzinfo is None
        or row.labeled_at.utcoffset() is None
        for rows in partitions
        for row in rows
    ):
        raise ValueError("training row timestamps must include a timezone")
    if any(row.labeled_at < row.received_at for rows in partitions for row in rows):
        raise ValueError("training label cannot predate its received_at")
    event_sets = [{row.event_id for row in rows} for rows in partitions]
    if (
        event_sets[0] & event_sets[1]
        or event_sets[0] & event_sets[2]
        or event_sets[1] & event_sets[2]
    ):
        raise ValueError("event leakage detected between ML partitions")
    all_ids = [row.id for rows in partitions for row in rows]
    if len(all_ids) != len(set(all_ids)):
        raise ValueError("duplicate row id detected between ML partitions")
    train_through = max(row.received_at for row in train)
    validation_from = min(row.received_at for row in validation)
    validation_through = max(row.received_at for row in validation)
    test_from = min(row.received_at for row in test)
    test_through = max(row.received_at for row in test)
    labels_available_through = max(row.labeled_at for rows in partitions for row in rows)
    if not train_through < validation_from or not validation_through < test_from:
        raise ValueError("ML partitions must be strictly chronological")
    if labels_available_through > trained_at:
        raise ValueError("ML partitions contain labels unavailable at training time")
    positive_count = sum(row.relevant for row in train)
    negative_count = len(train) - positive_count
    if not positive_count or not negative_count:
        raise ValueError("training data must contain both relevant and irrelevant examples")
    if not 1 <= vocabulary_size <= MAX_VOCABULARY_SIZE:
        raise ValueError(f"vocabulary_size must be between 1 and {MAX_VOCABULARY_SIZE}")

    document_frequency = Counter(
        feature
        for row in train
        for feature in _router_features(
            title=row.title,
            content=row.content,
            categories=row.categories,
        )
    )
    vocabulary = [
        feature
        for feature, _ in sorted(
            document_frequency.items(),
            key=lambda item: (-item[1], item[0]),
        )[:vocabulary_size]
    ]
    positive_presence = Counter()
    negative_presence = Counter()
    vocabulary_set = set(vocabulary)
    for row in train:
        features = _router_features(
            title=row.title,
            content=row.content,
            categories=row.categories,
        ) & vocabulary_set
        (positive_presence if row.relevant else negative_presence).update(features)

    positive_prior = (positive_count + 1) / (len(train) + 2)
    negative_prior = 1 - positive_prior
    intercept = math.log(positive_prior / negative_prior)
    weights: dict[str, float] = {}
    for feature in vocabulary:
        positive_probability = (positive_presence[feature] + 1) / (positive_count + 2)
        negative_probability = (negative_presence[feature] + 1) / (negative_count + 2)
        intercept += math.log((1 - positive_probability) / (1 - negative_probability))
        weights[feature] = round(
            math.log(positive_probability / (1 - positive_probability))
            - math.log(negative_probability / (1 - negative_probability)),
            10,
        )

    def metrics(rows: Sequence[MlRouterTrainingRow]) -> MlRouterSafetyMetrics:
        decisions = [
            (
                row,
                _predict_probability(
                    intercept,
                    weights,
                    _router_features(
                        title=row.title,
                        content=row.content,
                        categories=row.categories,
                    ),
                )
                <= reject_threshold,
            )
            for row in rows
        ]
        relevant = sum(row.relevant for row, _ in decisions)
        irrelevant = len(decisions) - relevant
        rejected = sum(rejected for _, rejected in decisions)
        false_rejections = sum(row.relevant and rejected for row, rejected in decisions)
        correct_rejections = rejected - false_rejections
        return MlRouterSafetyMetrics(
            observations=len(decisions),
            relevant=relevant,
            irrelevant=irrelevant,
            rejected=rejected,
            false_rejections=false_rejections,
            rejection_precision=(
                round(correct_rejections / rejected, 6) if rejected else None
            ),
            relevant_recall=(
                round((relevant - false_rejections) / relevant, 6) if relevant else None
            ),
            rejection_coverage=round(rejected / len(decisions), 6),
        )

    return MlRouterArtifact(
        label_source=label_source,
        trained_at=trained_at,
        dataset_sha256=dataset_sha256,
        training_examples=len(train),
        training_events=len({row.event_id for row in train}),
        train_received_at_through=train_through,
        validation_received_at_from=validation_from,
        validation_received_at_through=validation_through,
        test_received_at_from=test_from,
        test_received_at_through=test_through,
        labels_available_through=labels_available_through,
        vocabulary=weights,
        intercept=round(intercept, 10),
        reject_threshold=reject_threshold,
        accept_threshold=accept_threshold,
        validation=metrics(validation),
        test=metrics(test),
    )


def artifact_summary(artifact: MlRouterArtifact) -> dict[str, object]:
    return {
        "schema_version": artifact.schema_version,
        "model_version": artifact.model_version,
        "label_source": artifact.label_source,
        "trained_at": artifact.trained_at.isoformat(),
        "dataset_sha256": artifact.dataset_sha256,
        "training_examples": artifact.training_examples,
        "training_events": artifact.training_events,
        "partition_windows": {
            "train_through": artifact.train_received_at_through.isoformat(),
            "validation_from": artifact.validation_received_at_from.isoformat(),
            "validation_through": artifact.validation_received_at_through.isoformat(),
            "test_from": artifact.test_received_at_from.isoformat(),
            "test_through": artifact.test_received_at_through.isoformat(),
            "labels_available_through": artifact.labels_available_through.isoformat(),
        },
        "vocabulary_size": len(artifact.vocabulary),
        "validation": artifact.validation.model_dump(mode="json"),
        "test": artifact.test.model_dump(mode="json"),
        "production_gate": artifact.production_gate(),
    }


def write_artifact(path: Path, artifact: MlRouterArtifact) -> None:
    path.write_text(
        json.dumps(artifact.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _metrics_pass(metrics: MlRouterSafetyMetrics) -> bool:
    return bool(
        metrics.observations >= MIN_EVALUATION_EXAMPLES
        and metrics.relevant >= MIN_CLASS_EXAMPLES
        and metrics.irrelevant >= MIN_CLASS_EXAMPLES
        and metrics.rejection_precision is not None
        and metrics.rejection_precision >= MIN_REJECTION_PRECISION
        and metrics.relevant_recall is not None
        and metrics.relevant_recall >= MIN_RELEVANT_RECALL
        and metrics.rejection_coverage >= MIN_REJECTION_COVERAGE
    )


def _router_features(
    *,
    title: str,
    content: str,
    categories: Sequence[str],
) -> set[str]:
    title_tokens = [token[:MAX_TOKEN_LENGTH] for token in TOKEN_PATTERN.findall(title.casefold())]
    content_tokens = [
        token[:MAX_TOKEN_LENGTH]
        for token in TOKEN_PATTERN.findall(content[:2_500].casefold())
    ]
    features = {f"word:{token}" for token in (*title_tokens, *content_tokens)}
    features.update(
        f"title_bigram:{left}_{right}"
        for left, right in zip(title_tokens, title_tokens[1:], strict=False)
    )
    features.update(
        f"category:{token}"
        for category in categories[:MAX_CATEGORIES]
        for raw_token in TOKEN_PATTERN.findall(category[:MAX_CATEGORY_LENGTH].casefold())
        for token in (raw_token[:MAX_TOKEN_LENGTH],)
    )
    return features


def _predict_probability(
    intercept: float,
    weights: Mapping[str, float],
    features: set[str],
) -> float:
    logit = intercept + sum(weights.get(feature, 0.0) for feature in features)
    if logit >= 0:
        return 1 / (1 + math.exp(-min(logit, 700)))
    exp_value = math.exp(max(logit, -700))
    return exp_value / (1 + exp_value)
