from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from signal_fixtures import signal_example

from eventedge_research.signal_baselines import SignalBaseline, build_baseline_predictions
from eventedge_research.signal_benchmark import (
    EvaluationTarget,
    PredictionDirection,
    SignalPrediction,
    evaluate_signal_predictions,
    load_signal_predictions,
    select_asymmetric_probability_policy,
    wilson_interval_95,
    write_signal_predictions,
)
from eventedge_research.signal_dataset import SignalDatasetExample
from scripts.evaluate_signal_baselines import main as evaluate_signal_baselines_main
from scripts.evaluate_signal_predictions import main as evaluate_signal_predictions_main

DATASET_SHA256 = "a" * 64


def prediction(
    example: SignalDatasetExample,
    direction: PredictionDirection,
    *,
    confidence: float = 0.75,
    training_labels_through: datetime | None = None,
    dataset_sha256: str = DATASET_SHA256,
    model_artifact_sha256: str | None = None,
) -> SignalPrediction:
    return SignalPrediction(
        example_id=example.id,
        event_id=example.event_id,
        ticker=example.ticker,
        decision_at=example.decision_at,
        feature_cutoff_at=example.features.as_of,
        direction=direction,
        confidence=confidence,
        model_version="fixture-model-1.0",
        config_version=1,
        model_artifact_sha256=model_artifact_sha256,
        partition="test",
        dataset_sha256=dataset_sha256,
        training_labels_through=training_labels_through,
    )


def test_signal_prediction_rejects_future_and_overlapping_training_data() -> None:
    example = SignalDatasetExample.model_validate(signal_example(1))
    payload = prediction(example, PredictionDirection.UP).model_dump(mode="json")
    payload["feature_cutoff_at"] = (example.decision_at + timedelta(seconds=1)).isoformat()
    with pytest.raises(ValidationError, match="feature_cutoff_at cannot be later"):
        SignalPrediction.model_validate(payload)

    payload = prediction(example, PredictionDirection.UP).model_dump(mode="json")
    payload["training_labels_through"] = example.decision_at.isoformat()
    with pytest.raises(ValidationError, match="training labels must predate"):
        SignalPrediction.model_validate(payload)


def test_signal_benchmark_counts_hits_coverage_returns_and_calibration() -> None:
    examples = [
        SignalDatasetExample.model_validate(signal_example(1, return_pct=1.0)),
        SignalDatasetExample.model_validate(signal_example(2, return_pct=-1.0)),
        SignalDatasetExample.model_validate(signal_example(3, return_pct=-1.0)),
        SignalDatasetExample.model_validate(signal_example(4, return_pct=0.5)),
    ]
    predictions = [
        prediction(examples[0], PredictionDirection.UP, confidence=0.8),
        prediction(examples[1], PredictionDirection.DOWN, confidence=0.7),
        prediction(examples[2], PredictionDirection.UP, confidence=0.9),
        prediction(examples[3], PredictionDirection.ABSTAIN, confidence=0.5),
    ]

    report = evaluate_signal_predictions(
        examples,
        predictions,
        dataset_sha256=DATASET_SHA256,
    )

    assert report["metrics"] == {
        "eligible_event_tickers": 4,
        "independent_events": 4,
        "information_groups": 4,
        "directional_predictions": 3,
        "directional_events": 3,
        "abstentions": 1,
        "hits": 2,
        "misses": 1,
        "hit_rate_pct": 66.6667,
        "selection_coverage_pct": 75.0,
        "wilson_95": {"lower_pct": 20.766, "upper_pct": 93.8508},
        "average_signed_return_pct": 0.3333,
        "median_signed_return_pct": 1.0,
        "average_signed_abnormal_return_pct": 0.3333,
        "brier_score_confidence": 0.313333,
    }
    assert report["by_prediction"]["up"]["hit_rate_pct"] == 50.0
    assert report["by_prediction"]["down"]["hit_rate_pct"] == 100.0


def test_signal_benchmark_can_score_direction_relative_to_market() -> None:
    example = SignalDatasetExample.model_validate(
        signal_example(1, return_pct=1.0, benchmark_return_pct=2.0)
    )
    predictions = [prediction(example, PredictionDirection.DOWN)]

    raw_report = evaluate_signal_predictions(
        [example],
        predictions,
        dataset_sha256=DATASET_SHA256,
    )
    abnormal_report = evaluate_signal_predictions(
        [example],
        predictions,
        dataset_sha256=DATASET_SHA256,
        target=EvaluationTarget.ABNORMAL_RETURN,
    )

    assert raw_report["metrics"]["hit_rate_pct"] == 0.0
    assert abnormal_report["evaluation_target"] == "abnormal_return"
    assert abnormal_report["metrics"]["hit_rate_pct"] == 100.0


def test_signal_benchmark_requires_selected_target_for_every_example() -> None:
    example = SignalDatasetExample.model_validate(
        signal_example(1, benchmark_return_pct=None)
    )

    with pytest.raises(ValueError, match="target 'abnormal_return' is unavailable"):
        evaluate_signal_predictions(
            [example],
            [prediction(example, PredictionDirection.UP)],
            dataset_sha256=DATASET_SHA256,
            target=EvaluationTarget.ABNORMAL_RETURN,
        )


def test_signal_benchmark_requires_explicit_prediction_for_every_example() -> None:
    examples = [
        SignalDatasetExample.model_validate(signal_example(1)),
        SignalDatasetExample.model_validate(signal_example(2)),
    ]

    with pytest.raises(ValueError, match="coverage must be explicit"):
        evaluate_signal_predictions(
            examples,
            [prediction(examples[0], PredictionDirection.UP)],
            dataset_sha256=DATASET_SHA256,
        )


def test_signal_benchmark_rejects_duplicate_evaluation_units() -> None:
    example = SignalDatasetExample.model_validate(signal_example(1))

    with pytest.raises(ValueError, match="duplicate ids"):
        evaluate_signal_predictions(
            [example, example],
            [prediction(example, PredictionDirection.UP)],
            dataset_sha256=DATASET_SHA256,
        )

    same_pair_payload = signal_example(2, event_id=example.event_id)
    same_pair_payload["id"] = "different-id"
    same_pair = SignalDatasetExample.model_validate(same_pair_payload)
    with pytest.raises(ValueError, match="duplicate event/ticker"):
        evaluate_signal_predictions(
            [example, same_pair],
            [
                prediction(example, PredictionDirection.UP),
                prediction(same_pair, PredictionDirection.DOWN),
            ],
            dataset_sha256=DATASET_SHA256,
        )


def test_signal_benchmark_rejects_wrong_dataset_fingerprint() -> None:
    example = SignalDatasetExample.model_validate(signal_example(1))
    trained_prediction = prediction(
        example,
        PredictionDirection.UP,
        training_labels_through=example.decision_at - timedelta(days=1),
        dataset_sha256="b" * 64,
    )

    with pytest.raises(ValueError, match="dataset fingerprint"):
        evaluate_signal_predictions(
            [example],
            [trained_prediction],
            dataset_sha256=DATASET_SHA256,
        )


def test_signal_benchmark_does_not_mix_model_artifacts() -> None:
    examples = [
        SignalDatasetExample.model_validate(signal_example(1)),
        SignalDatasetExample.model_validate(signal_example(2)),
    ]
    predictions = [
        prediction(
            examples[0],
            PredictionDirection.UP,
            model_artifact_sha256="b" * 64,
        ),
        prediction(
            examples[1],
            PredictionDirection.DOWN,
            model_artifact_sha256="c" * 64,
        ),
    ]

    with pytest.raises(ValueError, match="cannot mix model, config, artifact"):
        evaluate_signal_predictions(
            examples,
            predictions,
            dataset_sha256=DATASET_SHA256,
        )


def test_wilson_interval_matches_pre_registered_sample_calculation() -> None:
    lower, upper = wilson_interval_95(116, 200)

    assert lower == pytest.approx(0.510721, abs=1e-6)
    assert upper == pytest.approx(0.646264, abs=1e-6)
    assert wilson_interval_95(0, 0) == (None, None)


def test_asymmetric_probability_policy_learns_prior_shift_and_abstention_band() -> None:
    returns = (-1.0, -1.0, -1.0, 1.0, 1.0, 1.0)
    examples = [
        SignalDatasetExample.model_validate(signal_example(index, return_pct=value))
        for index, value in enumerate(returns, 1)
    ]

    policy = select_asymmetric_probability_policy(
        examples,
        [0.1, 0.2, 0.3, 0.7, 0.8, 0.9],
        target=EvaluationTarget.RAW_RETURN,
        minimum_predictions=4,
        minimum_predictions_per_direction=2,
        minimum_coverage_pct=50,
    )

    assert policy["down_raw_probability_threshold"] == 0.3
    assert policy["up_raw_probability_threshold"] == 0.7
    assert policy["probability_logit_offset"] == pytest.approx(0)
    assert policy["confidence_threshold"] == pytest.approx(0.7)
    assert policy["validation_directional_predictions"] == 6
    assert policy["validation_down_predictions"] == 3
    assert policy["validation_up_predictions"] == 3
    assert policy["validation_hit_rate_pct"] == 100.0


def test_asymmetric_probability_policy_can_preserve_a_semantic_abstention() -> None:
    returns = (-1.0, -1.0, 1.0, 1.0, -1.0, 1.0)
    examples = [
        SignalDatasetExample.model_validate(signal_example(index, return_pct=value))
        for index, value in enumerate(returns, 1)
    ]

    policy = select_asymmetric_probability_policy(
        examples,
        [0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
        target=EvaluationTarget.RAW_RETURN,
        minimum_predictions=2,
        minimum_predictions_per_direction=1,
        minimum_coverage_pct=30,
        required_abstention_probability=0.5,
    )

    assert policy["down_raw_probability_threshold"] < 0.5
    assert policy["up_raw_probability_threshold"] > 0.5


def test_asymmetric_probability_policy_rejects_invalid_abstention_probability() -> None:
    examples = [
        SignalDatasetExample.model_validate(signal_example(1, return_pct=-1)),
        SignalDatasetExample.model_validate(signal_example(2, return_pct=1)),
    ]

    with pytest.raises(ValueError, match="required abstention probability"):
        select_asymmetric_probability_policy(
            examples,
            [0.1, 0.9],
            target=EvaluationTarget.RAW_RETURN,
            minimum_predictions=2,
            minimum_predictions_per_direction=1,
            minimum_coverage_pct=100,
            required_abstention_probability=float("nan"),
        )


def test_baselines_are_deterministic_and_use_only_prior_labels() -> None:
    training = [
        SignalDatasetExample.model_validate(
            signal_example(
                1,
                decision_at=datetime(2026, 1, 1, tzinfo=UTC),
                return_pct=1.0,
            )
        ),
        SignalDatasetExample.model_validate(
            signal_example(
                2,
                decision_at=datetime(2026, 1, 2, tzinfo=UTC),
                return_pct=1.0,
            )
        ),
        SignalDatasetExample.model_validate(
            signal_example(
                3,
                decision_at=datetime(2026, 1, 3, tzinfo=UTC),
                return_pct=-1.0,
            )
        ),
    ]
    evaluation = [
        SignalDatasetExample.model_validate(
            signal_example(
                4,
                decision_at=datetime(2026, 1, 10, tzinfo=UTC),
                rule_direction="down",
                pre_event_return_1h_pct=0.3,
            )
        )
    ]

    majority = build_baseline_predictions(
        SignalBaseline.MAJORITY,
        training_examples=training,
        evaluation_examples=evaluation,
        partition="test",
        dataset_sha256=DATASET_SHA256,
    )[0]
    stored_rules = build_baseline_predictions(
        SignalBaseline.STORED_RULES,
        training_examples=training,
        evaluation_examples=evaluation,
        partition="test",
        dataset_sha256=DATASET_SHA256,
    )[0]
    market = build_baseline_predictions(
        SignalBaseline.MARKET_MOMENTUM,
        training_examples=training,
        evaluation_examples=evaluation,
        partition="test",
        dataset_sha256=DATASET_SHA256,
    )[0]
    always_up = build_baseline_predictions(
        SignalBaseline.ALWAYS_UP,
        training_examples=training,
        evaluation_examples=evaluation,
        partition="test",
        dataset_sha256=DATASET_SHA256,
    )[0]
    always_down = build_baseline_predictions(
        SignalBaseline.ALWAYS_DOWN,
        training_examples=training,
        evaluation_examples=evaluation,
        partition="test",
        dataset_sha256=DATASET_SHA256,
    )[0]
    coin_first = build_baseline_predictions(
        SignalBaseline.COIN_FLIP,
        training_examples=training,
        evaluation_examples=evaluation,
        partition="test",
        dataset_sha256=DATASET_SHA256,
    )
    coin_second = build_baseline_predictions(
        SignalBaseline.COIN_FLIP,
        training_examples=training,
        evaluation_examples=evaluation,
        partition="test",
        dataset_sha256=DATASET_SHA256,
    )

    assert majority.direction is PredictionDirection.UP
    assert majority.confidence == pytest.approx(2 / 3)
    assert majority.training_labels_through == training[-1].label_available_at
    assert stored_rules.direction is PredictionDirection.DOWN
    assert stored_rules.model_version == "stored-rules-1.0"
    assert market.direction is PredictionDirection.UP
    assert always_up.direction is PredictionDirection.UP
    assert always_up.confidence == 0.5
    assert always_up.training_labels_through is None
    assert always_down.direction is PredictionDirection.DOWN
    assert always_down.confidence == 0.5
    assert always_down.training_labels_through is None
    assert coin_first == coin_second


def test_signal_prediction_jsonl_round_trip(tmp_path: Path) -> None:
    example = SignalDatasetExample.model_validate(signal_example(1))
    predictions = [prediction(example, PredictionDirection.ABSTAIN, confidence=0)]
    path = tmp_path / "predictions.jsonl"

    write_signal_predictions(path, predictions)

    assert load_signal_predictions(path) == predictions


def test_baseline_and_prediction_clis_share_the_frozen_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dataset = tmp_path / "signal.jsonl"
    predictions_dir = tmp_path / "predictions"
    rows = [
        signal_example(1, decision_at=datetime(2026, 1, 1, tzinfo=UTC), return_pct=1),
        signal_example(2, decision_at=datetime(2026, 1, 2, tzinfo=UTC), return_pct=1),
        signal_example(3, decision_at=datetime(2026, 1, 10, tzinfo=UTC), return_pct=-1),
        signal_example(4, decision_at=datetime(2026, 1, 20, tzinfo=UTC), return_pct=1),
        signal_example(5, decision_at=datetime(2026, 1, 21, tzinfo=UTC), return_pct=-1),
        signal_example(6, decision_at=datetime(2026, 2, 2, tzinfo=UTC), return_pct=1),
    ]
    dataset.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
        encoding="utf-8",
    )
    common_arguments = [
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
    ]
    monkeypatch.setattr(
        "sys.argv",
        [
            "evaluate_signal_baselines",
            *common_arguments,
            "--baseline",
            "majority",
            "--predictions-dir",
            str(predictions_dir),
        ],
    )

    evaluate_signal_baselines_main()

    suite = json.loads(capsys.readouterr().out)
    majority = suite["reports"]["majority"]
    assert majority["metrics"]["hit_rate_pct"] == 50.0
    assert majority["metrics"]["selection_coverage_pct"] == 100.0

    prediction_path = predictions_dir / "test-majority.jsonl"
    monkeypatch.setattr(
        "sys.argv",
        [
            "evaluate_signal_predictions",
            *common_arguments,
            "--predictions",
            str(prediction_path),
        ],
    )

    evaluate_signal_predictions_main()

    report = json.loads(capsys.readouterr().out)
    assert report["metrics"] == majority["metrics"]
    assert report["dataset_sha256"] == suite["dataset_sha256"]

    monkeypatch.setattr(
        "sys.argv",
        [
            "evaluate_signal_baselines",
            *common_arguments,
            "--baseline",
            "majority",
            "--partition",
            "shadow",
        ],
    )
    evaluate_signal_baselines_main()
    shadow_suite = json.loads(capsys.readouterr().out)
    shadow_report = shadow_suite["reports"]["majority"]
    assert shadow_report["partition"] == "shadow"
