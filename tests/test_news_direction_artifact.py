"""Tests for portable direction training state and offline inference."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eventedge_research.news_model_artifact import (
    CANONICAL_DIRECTION_FEATURE_SPEC_NAME,
    CANONICAL_DIRECTION_MODEL_ID,
    COMMON_DIRECTION_FEATURE_SPEC_NAME,
    COMMON_DIRECTION_MODEL_ID,
    artifact_payload_sha256,
    build_portable_direction_artifact,
    load_portable_direction_model,
    load_portable_direction_payload,
    predict_direction_jsonl,
)
from eventedge_research.news_model_features import (
    NewsFeatureRow,
    fit_news_model,
    retained_feature_specs,
    serialize_feature_row,
)
from eventedge_research.news_model_training import fit_latest_direction_artifact
from eventedge_research.research_artifacts import write_json, write_jsonl
from eventedge_research.signal_dataset import signal_dataset_sha256
from scripts.fit_news_direction_model import _arguments as fit_arguments
from scripts.predict_news_direction import _arguments as predict_arguments


def _row(index: int, *, decision_at: datetime | None = None) -> NewsFeatureRow:
    sign = 1 if index % 2 else -1
    ticker = "SBER" if index % 3 else "GAZP"
    source_id = "telegram_markettwits" if index % 4 else "telegram_selfinvestor"
    category = "financial_results" if index % 5 else "dividend"
    stage = "result" if index % 7 else "recommendation"
    spec = retained_feature_specs()[COMMON_DIRECTION_FEATURE_SPEC_NAME]
    numeric = {
        name: sign * (0.05 + position / 100 + index / 10_000)
        for position, name in enumerate(spec.numeric_names)
    }
    categorical = {
        "ticker": ticker,
        "source_id": source_id,
        "publication_session": "main_session",
        "event_type": category,
        "temporal_status": stage,
        "corrected_category": category,
        "corrected_stage": stage,
        "corrected_issuer_role": "direct",
        "corrected_disposition": "confirmed",
        "corrected_category_x_reaction_sign": (
            f"{category}|{'positive' if sign > 0 else 'negative'}"
        ),
        "corrected_stage_x_reaction_sign": (
            f"{stage}|{'positive' if sign > 0 else 'negative'}"
        ),
    }
    return NewsFeatureRow(
        example_id=f"example-{index}",
        event_id=f"event-{index}",
        event_group_id=f"group-{index}",
        ticker=ticker,
        source_id=source_id,
        title=f"Новость {index}",
        content=f"Содержание новости {index}",
        decision_at=decision_at
        or datetime(2026, 1, 1, 7, tzinfo=UTC) + timedelta(minutes=index),
        event_type=category,
        temporal_status=stage,
        numeric=numeric,
        categorical=categorical,
    )


def _fitted_artifact(
    spec_name: str = COMMON_DIRECTION_FEATURE_SPEC_NAME,
):
    spec = retained_feature_specs()[spec_name]
    training = [_row(index) for index in range(30)]
    validation = [_row(index + 100) for index in range(12)]
    training_values = [1.0 if index % 2 else -1.0 for index in range(30)]
    validation_values = [1.0 if index % 2 else -1.0 for index in range(12)]
    fitted = fit_news_model(
        training,
        training_values,
        validation,
        validation_values,
        spec=spec,
    )
    artifact = build_portable_direction_artifact(
        fitted,
        raw_validation_probabilities=fitted.raw_probabilities_up(validation),
        fold={
            "name": "2026_03",
            "validation_from": "2026-01-01T00:00:00+03:00",
            "evaluation_from": "2026-03-01T00:00:00+03:00",
            "evaluation_until": "2026-04-01T00:00:00+03:00",
        },
        dataset_sha256="a" * 64,
        fit_bundle_sha256="b" * 64,
        feature_input_filename="update-5m-inputs.jsonl",
        feature_input_sha256="c" * 64,
        training_rows=len(training),
        validation_rows=len(validation),
    )
    return fitted, artifact


def _resign(artifact: dict[str, object]) -> None:
    artifact["artifact_payload_sha256"] = artifact_payload_sha256(artifact)


@pytest.mark.parametrize(
    "spec_name",
    (COMMON_DIRECTION_FEATURE_SPEC_NAME, CANONICAL_DIRECTION_FEATURE_SPEC_NAME),
)
def test_portable_direction_matches_fitted_sklearn_probability_and_direction(
    spec_name: str,
) -> None:
    fitted, artifact = _fitted_artifact(spec_name)
    model = load_portable_direction_payload(artifact)

    for row in (_row(999), _row(1_000)):
        expected_raw = fitted.raw_probabilities_up([row])[0]
        expected_calibrated = fitted.probabilities_up([row])[0]
        prediction = model.predict(row)
        assert model.raw_probability_up(row) == pytest.approx(expected_raw, abs=1e-14)
        assert model.probability_up(row) == pytest.approx(
            expected_calibrated, abs=1e-14
        )
        assert prediction["raw_direction"] == (
            "up" if expected_raw >= 0.5 else "down"
        )
        assert prediction["calibrated_direction"] == (
            "up" if expected_calibrated >= 0.5 else "down"
        )


def test_direction_loader_rejects_hash_schema_and_provenance_mutations() -> None:
    _, original = _fitted_artifact()

    unbound = copy.deepcopy(original)
    unbound["classifier"]["intercept"] += 1
    with pytest.raises(ValueError, match="payload digest mismatch"):
        load_portable_direction_payload(unbound)

    incompatible = copy.deepcopy(original)
    incompatible["feature_spec"]["name"] = "update_5m_market_control"
    _resign(incompatible)
    with pytest.raises(ValueError, match="feature spec is incompatible"):
        load_portable_direction_payload(incompatible)

    future_leakage = copy.deepcopy(original)
    future_leakage["provenance"]["evaluation_from_inclusive"] = (
        "2026-04-02T00:00:00+03:00"
    )
    _resign(future_leakage)
    with pytest.raises(ValueError, match="temporal cutoffs are invalid"):
        load_portable_direction_payload(future_leakage)


def test_direction_file_loader_requires_expected_digest(tmp_path: Path) -> None:
    _, artifact = _fitted_artifact()
    path = tmp_path / "direction-model.json"
    write_json(path, artifact)

    model = load_portable_direction_model(
        path,
        expected_payload_sha256=artifact["artifact_payload_sha256"],
    )
    assert model.model_id == COMMON_DIRECTION_MODEL_ID
    with pytest.raises(ValueError, match="expected digest"):
        load_portable_direction_model(path, expected_payload_sha256="0" * 64)


def test_direction_jsonl_is_outcome_free_bound_and_no_clobber(tmp_path: Path) -> None:
    _, artifact = _fitted_artifact()
    model = load_portable_direction_payload(artifact)
    input_path = tmp_path / "inputs.jsonl"
    output_path = tmp_path / "predictions.jsonl"
    write_jsonl(input_path, [serialize_feature_row(_row(999))])

    report = predict_direction_jsonl(model, input_path, output_path)
    original_output = output_path.read_bytes()
    prediction = json.loads(original_output)

    assert report["rows"] == 1
    assert report["input_sha256"] == signal_dataset_sha256(input_path)
    assert prediction["artifact_payload_sha256"] == artifact[
        "artifact_payload_sha256"
    ]
    assert prediction["model_id"] == COMMON_DIRECTION_MODEL_ID
    assert set(prediction["selected_at_coverage_pct"]) == {"raw", "calibrated"}
    assert 0 <= prediction["calibrated_probability_up"] <= 1

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        predict_direction_jsonl(model, input_path, output_path)
    assert output_path.read_bytes() == original_output


def test_direction_jsonl_rejects_outcomes_and_duplicate_ids(tmp_path: Path) -> None:
    _, artifact = _fitted_artifact()
    model = load_portable_direction_payload(artifact)
    payload = serialize_feature_row(_row(999))
    payload["return_pct"] = 2.0
    outcome_input = tmp_path / "outcome-input.jsonl"
    write_jsonl(outcome_input, [payload])
    with pytest.raises(ValueError, match="feature row fields"):
        predict_direction_jsonl(
            model,
            outcome_input,
            tmp_path / "outcome-predictions.jsonl",
        )

    duplicate_input = tmp_path / "duplicate-input.jsonl"
    row = serialize_feature_row(_row(998))
    write_jsonl(duplicate_input, [row, row])
    duplicate_output = tmp_path / "duplicate-predictions.jsonl"
    with pytest.raises(ValueError, match="duplicate ids"):
        predict_direction_jsonl(model, duplicate_input, duplicate_output)
    assert not duplicate_output.exists()


def _training_fixture(
    tmp_path: Path,
    *,
    recorded_spec: str | None = COMMON_DIRECTION_FEATURE_SPEC_NAME,
) -> tuple[Path, Path, str]:
    feature_path = tmp_path / "update-5m-inputs.jsonl"
    training_rows = [
        _row(
            index,
            decision_at=datetime(2025, 12, 1, tzinfo=UTC) + timedelta(minutes=index),
        )
        for index in range(30)
    ]
    validation_rows = [
        _row(
            index + 100,
            decision_at=datetime(2026, 1, 2, tzinfo=UTC) + timedelta(minutes=index),
        )
        for index in range(12)
    ]
    evaluation_rows = [
        _row(
            index + 200,
            decision_at=datetime(2026, 3, 2, tzinfo=UTC) + timedelta(minutes=index),
        )
        for index in range(6)
    ]
    write_jsonl(
        feature_path,
        (
            serialize_feature_row(row)
            for row in (*training_rows, *validation_rows, *evaluation_rows)
        ),
    )
    training = [
        {"example_id": row.example_id, "return_pct": 1.0 if index % 2 else -1.0}
        for index, row in enumerate(training_rows)
    ]
    validation = [
        {"example_id": row.example_id, "return_pct": 1.0 if index % 2 else -1.0}
        for index, row in enumerate(validation_rows)
    ]
    evaluation_ids = [row.example_id for row in evaluation_rows]
    bundle_path = tmp_path / "fit-bundle.json"
    bundle = {
        "schema_version": "news-model-fit-bundle-1.0",
        "dataset_sha256": "a" * 64,
        "evaluation_outcomes_included": False,
        "production_accessed": False,
        "ydb_accessed": False,
        "update_input_path": feature_path.name,
        "update_input_sha256": signal_dataset_sha256(feature_path),
        "folds": [
            {
                "name": "2026_03",
                "validation_from": "2026-01-01T00:00:00+03:00",
                "evaluation_from": "2026-03-01T00:00:00+03:00",
                "evaluation_until": "2026-04-01T00:00:00+03:00",
                "evaluation_ids": evaluation_ids,
                "labels": {
                    "remaining_abnormal_4h": {
                        "training": training,
                        "validation": validation,
                        "evaluation_ids": evaluation_ids,
                    }
                },
            }
        ],
    }
    if recorded_spec is not None:
        bundle["portable_model_specs"] = {"direction": recorded_spec}
    write_json(bundle_path, bundle)
    artifact_path = tmp_path / "direction-model.json"
    return bundle_path, artifact_path, signal_dataset_sha256(bundle_path)


def test_latest_direction_fold_writes_loadable_artifact(tmp_path: Path) -> None:
    bundle_path, artifact_path, bundle_sha256 = _training_fixture(tmp_path)

    artifact = fit_latest_direction_artifact(
        bundle_path,
        artifact_path,
        expected_fit_bundle_sha256=bundle_sha256,
    )
    model = load_portable_direction_model(
        artifact_path,
        expected_payload_sha256=artifact["artifact_payload_sha256"],
    )

    assert artifact["provenance"]["fold"] == "2026_03"
    assert artifact["feature_spec"]["name"] == COMMON_DIRECTION_FEATURE_SPEC_NAME
    assert model.model_id == COMMON_DIRECTION_MODEL_ID
    assert model.predict(_row(999))["artifact_payload_sha256"] == artifact[
        "artifact_payload_sha256"
    ]
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        fit_latest_direction_artifact(
            bundle_path,
            artifact_path,
            expected_fit_bundle_sha256=bundle_sha256,
        )


def test_direction_training_rejects_untrusted_bundle_before_parsing(
    tmp_path: Path,
) -> None:
    bundle_path = tmp_path / "fit-bundle.json"
    bundle_path.write_text("not JSON", encoding="utf-8")

    with pytest.raises(ValueError, match="does not match the expected digest"):
        fit_latest_direction_artifact(
            bundle_path,
            tmp_path / "direction-model.json",
            expected_fit_bundle_sha256="0" * 64,
        )


def test_direction_training_defaults_canonical_bundle_to_locked_candidate(
    tmp_path: Path,
) -> None:
    bundle_path, artifact_path, bundle_sha256 = _training_fixture(
        tmp_path,
        recorded_spec=None,
    )

    artifact = fit_latest_direction_artifact(
        bundle_path,
        artifact_path,
        expected_fit_bundle_sha256=bundle_sha256,
    )
    model = load_portable_direction_model(
        artifact_path,
        expected_payload_sha256=artifact["artifact_payload_sha256"],
    )

    assert artifact["feature_spec"]["name"] == CANONICAL_DIRECTION_FEATURE_SPEC_NAME
    assert model.model_id == CANONICAL_DIRECTION_MODEL_ID


def test_direction_training_rejects_unregistered_recorded_spec(tmp_path: Path) -> None:
    bundle_path, artifact_path, bundle_sha256 = _training_fixture(
        tmp_path,
        recorded_spec="update_5m_market_control",
    )

    with pytest.raises(ValueError, match="retained direction feature spec"):
        fit_latest_direction_artifact(
            bundle_path,
            artifact_path,
            expected_fit_bundle_sha256=bundle_sha256,
        )

    assert not artifact_path.exists()


@pytest.mark.parametrize(
    ("arguments", "command_line"),
    (
        (fit_arguments, ["--fit-bundle", "bundle.json", "--output", "model.json"]),
        (
            predict_arguments,
            [
                "--artifact",
                "model.json",
                "--input",
                "features.jsonl",
                "--output",
                "predictions.jsonl",
            ],
        ),
    ),
)
def test_direction_clis_require_trusted_digest(
    monkeypatch: pytest.MonkeyPatch,
    arguments: Callable[[], object],
    command_line: list[str],
) -> None:
    monkeypatch.setattr("sys.argv", ["direction-command", *command_line])

    with pytest.raises(SystemExit):
        arguments()
