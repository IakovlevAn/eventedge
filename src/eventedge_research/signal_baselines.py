"""Reproducible controls for learned directional signal models."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Literal

from eventedge_research.signal_benchmark import (
    EvaluationTarget,
    PredictionDirection,
    SignalPrediction,
    signal_target_return,
)
from eventedge_research.signal_dataset import SignalDatasetExample


class SignalBaseline(StrEnum):
    """Reproducible controls that every learned signal model must face."""

    COIN_FLIP = "coin_flip"
    ALWAYS_UP = "always_up"
    ALWAYS_DOWN = "always_down"
    MAJORITY = "majority"
    STORED_RULES = "stored_rules"
    MARKET_MOMENTUM = "market_momentum"


def build_baseline_predictions(
    baseline: SignalBaseline,
    *,
    training_examples: Sequence[SignalDatasetExample],
    evaluation_examples: Sequence[SignalDatasetExample],
    partition: Literal["validation", "test", "shadow"],
    dataset_sha256: str,
    target: EvaluationTarget = EvaluationTarget.RAW_RETURN,
) -> list[SignalPrediction]:
    """Build predictions without reading outcomes from the evaluation partition."""
    if not evaluation_examples:
        raise ValueError("cannot build baseline predictions for an empty partition")
    if baseline is SignalBaseline.MAJORITY:
        direction, confidence, labels_through = _majority_parameters(
            training_examples,
            evaluation_examples,
            target=target,
        )
    else:
        direction = confidence = labels_through = None

    return [
        SignalPrediction(
            example_id=example.id,
            event_id=example.event_id,
            ticker=example.ticker,
            decision_at=example.decision_at,
            feature_cutoff_at=example.features.as_of,
            direction=(
                direction
                if baseline is SignalBaseline.MAJORITY
                else _baseline_direction(baseline, example)
            ),
            confidence=(
                confidence
                if baseline is SignalBaseline.MAJORITY
                else _baseline_confidence(baseline, example)
            ),
            model_version=signal_baseline_model_version(baseline, target=target),
            config_version=1,
            partition=partition,
            dataset_sha256=dataset_sha256,
            training_labels_through=labels_through,
        )
        for example in evaluation_examples
    ]


def _majority_parameters(
    training_examples: Sequence[SignalDatasetExample],
    evaluation_examples: Sequence[SignalDatasetExample],
    *,
    target: EvaluationTarget,
) -> tuple[PredictionDirection, float, datetime]:
    if not training_examples:
        raise ValueError("majority baseline requires non-empty training data")
    counts = Counter(
        _outcome_direction(example, target=target) for example in training_examples
    )
    directional_count = counts[PredictionDirection.UP] + counts[PredictionDirection.DOWN]
    if not directional_count:
        raise ValueError("majority baseline requires at least one non-flat training outcome")
    direction = (
        PredictionDirection.UP
        if counts[PredictionDirection.UP] >= counts[PredictionDirection.DOWN]
        else PredictionDirection.DOWN
    )
    labels_through = max(example.label_available_at for example in training_examples)
    first_decision = min(example.decision_at for example in evaluation_examples)
    if labels_through >= first_decision:
        raise ValueError("majority baseline training labels overlap the evaluation partition")
    return direction, counts[direction] / directional_count, labels_through


def _baseline_direction(
    baseline: SignalBaseline,
    example: SignalDatasetExample,
) -> PredictionDirection:
    if baseline is SignalBaseline.COIN_FLIP:
        digest = hashlib.sha256(f"signal-coin-v1\0{example.id}".encode()).digest()
        return PredictionDirection.UP if digest[0] % 2 == 0 else PredictionDirection.DOWN
    if baseline is SignalBaseline.ALWAYS_UP:
        return PredictionDirection.UP
    if baseline is SignalBaseline.ALWAYS_DOWN:
        return PredictionDirection.DOWN
    if baseline is SignalBaseline.STORED_RULES:
        if example.features.rule_direction == "up":
            return PredictionDirection.UP
        if example.features.rule_direction == "down":
            return PredictionDirection.DOWN
        return PredictionDirection.ABSTAIN
    if baseline is SignalBaseline.MARKET_MOMENTUM:
        momentum = example.features.pre_event_return_1h_pct
        if momentum is None or momentum == 0:
            return PredictionDirection.ABSTAIN
        return PredictionDirection.UP if momentum > 0 else PredictionDirection.DOWN
    raise ValueError(f"baseline {baseline.value} requires trained parameters")


def _baseline_confidence(
    baseline: SignalBaseline,
    example: SignalDatasetExample,
) -> float:
    if baseline is SignalBaseline.STORED_RULES:
        return example.features.rule_confidence or 0
    if baseline is SignalBaseline.MARKET_MOMENTUM:
        return 0 if example.features.pre_event_return_1h_pct in {None, 0} else 0.5
    if baseline in {
        SignalBaseline.COIN_FLIP,
        SignalBaseline.ALWAYS_UP,
        SignalBaseline.ALWAYS_DOWN,
    }:
        return 0.5
    raise ValueError(f"baseline {baseline.value} requires trained parameters")


def signal_baseline_model_version(
    baseline: SignalBaseline,
    *,
    target: EvaluationTarget = EvaluationTarget.RAW_RETURN,
) -> str:
    """Return the immutable identity of a reproducible control."""
    if baseline is SignalBaseline.MAJORITY and target is EvaluationTarget.ABNORMAL_RETURN:
        return "majority-direction-abnormal-return-1.0"
    return {
        SignalBaseline.COIN_FLIP: "coin-flip-sha256-1.0",
        SignalBaseline.ALWAYS_UP: "always-up-1.0",
        SignalBaseline.ALWAYS_DOWN: "always-down-1.0",
        SignalBaseline.MAJORITY: "majority-direction-1.0",
        SignalBaseline.STORED_RULES: "stored-rules-1.0",
        SignalBaseline.MARKET_MOMENTUM: "market-momentum-1h-1.0",
    }[baseline]


def _outcome_direction(
    example: SignalDatasetExample,
    *,
    target: EvaluationTarget,
) -> PredictionDirection | None:
    value = signal_target_return(example, target)
    if value is None:
        return None
    if value > 0:
        return PredictionDirection.UP
    if value < 0:
        return PredictionDirection.DOWN
    return None
