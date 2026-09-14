"""Tests for portable materiality training state and offline inference."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eventedge_research.news_model_artifact import (
    MODEL_ID,
    artifact_payload_sha256,
    build_portable_materiality_artifact,
    load_portable_materiality_model,
    load_portable_materiality_payload,
    predict_materiality_jsonl,
)
from eventedge_research.news_model_features import (
    NewsFeatureRow,
    fit_news_model,
    retained_feature_specs,
    serialize_feature_row,
)
from eventedge_research.news_model_training import fit_latest_materiality_artifact
from eventedge_research.research_artifacts import write_json, write_jsonl
from eventedge_research.signal_dataset import signal_dataset_sha256
from scripts.fit_news_materiality_model import _arguments as fit_arguments
from scripts.predict_news_materiality import _arguments as predict_arguments


def _row(
    index: int,
    *,
    decision_at: datetime | None = None,
    event_group_id: str | None = None,
    missing_reaction: bool = False,
) -> NewsFeatureRow:
    sign = 1 if index % 2 else -1
    reaction = None if missing_reaction else sign * (0.2 + index / 100)
    return NewsFeatureRow(
        example_id=f"example-{index}",
        event_id=f"event-{index}",
        event_group_id=event_group_id or f"group-{index // 2}",
        ticker="SBER" if index % 3 else "GAZP",
        source_id="telegram_markettwits" if index % 4 else "telegram_selfinvestor",
        title=f"Новость {index}",
        content=f"Содержание новости {index}",
        decision_at=(
            decision_at
            if decision_at is not None
            else datetime(2026, 1, 1, 7, tzinfo=UTC) + timedelta(minutes=index)
        ),
        event_type="financial_results",
        temporal_status="result",
        numeric={
            "market_pre_event_return_1h_pct": sign * 0.1,
            "market_pre_event_return_1d_pct": sign * 0.3,
            "market_pre_event_return_5d_pct": sign * 0.6,
            "market_benchmark_pre_event_return_1h_pct": sign * 0.05,
            "risk_volatility_20d_pct": 1.0 + index / 50,
            "risk_beta_60d": 0.8 + index / 100,
            "risk_beta_adjusted_pre_event_1h_pct": sign * 0.08,
            "time_hour_sin": 0.2,
            "time_hour_cos": 0.8,
            "time_weekday_sin": 0.4,
            "time_weekday_cos": 0.6,
            "time_year": 2026.0,
            "time_month": 1.0,
            "reaction_stock_pct": reaction,
            "reaction_benchmark_pct": sign * 0.04,
            "reaction_abnormal_pct": reaction,
            "derived_abs_reaction_stock_pct": (
                abs(reaction) if reaction is not None else None
            ),
            "derived_abs_reaction_abnormal_pct": (
                abs(reaction) if reaction is not None else None
            ),
            "derived_abs_reaction_abnormal_to_volatility": (
                abs(reaction) / (1.0 + index / 50)
                if reaction is not None
                else None
            ),
        },
        categorical={
            "ticker": "SBER" if index % 3 else "GAZP",
            "source_id": (
                "telegram_markettwits" if index % 4 else "telegram_selfinvestor"
            ),
            "publication_session": "main_session",
        },
    )


def _fitted_artifact():
    spec = retained_feature_specs()["update_5m_symmetric_materiality"]
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
    artifact = build_portable_materiality_artifact(
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


def test_portable_inference_matches_fitted_sklearn() -> None:
    fitted, artifact = _fitted_artifact()
    model = load_portable_materiality_payload(artifact)
    row = _row(999)

    assert model.raw_probability_material(row) == pytest.approx(
        fitted.raw_probabilities_up([row])[0], abs=1e-14
    )
    assert model.probability_material(row) == pytest.approx(
        fitted.probabilities_up([row])[0], abs=1e-14
    )


def test_portable_inference_ignores_unknown_nested_features() -> None:
    _, artifact = _fitted_artifact()
    model = load_portable_materiality_payload(artifact)
    row = _row(999)
    numeric = {**row.numeric, "future_unknown_numeric": 9_999.0}
    categorical = {**row.categorical, "future_unknown_category": "value"}
    extended = NewsFeatureRow(**{**row.__dict__, "numeric": numeric, "categorical": categorical})

    assert model.predict(extended) == model.predict(row)


def test_portable_inference_matches_sklearn_for_unseen_category() -> None:
    fitted, artifact = _fitted_artifact()
    model = load_portable_materiality_payload(artifact)
    row = _row(999)
    categorical = {**row.categorical, "ticker": "UNSEEN"}
    unseen = NewsFeatureRow(
        **{**row.__dict__, "ticker": "UNSEEN", "categorical": categorical}
    )

    assert model.raw_probability_material(unseen) == pytest.approx(
        fitted.raw_probabilities_up([unseen])[0], abs=1e-14
    )


@pytest.mark.parametrize("drop_value", (False, True))
def test_portable_inference_matches_sklearn_for_missing_numeric(
    drop_value: bool,
) -> None:
    fitted, artifact = _fitted_artifact()
    model = load_portable_materiality_payload(artifact)
    row = _row(999, missing_reaction=True)
    if drop_value:
        numeric = dict(row.numeric)
        del numeric["reaction_stock_pct"]
        row = NewsFeatureRow(**{**row.__dict__, "numeric": numeric})

    assert model.raw_probability_material(row) == pytest.approx(
        fitted.raw_probabilities_up([row])[0], abs=1e-14
    )


def test_portable_inference_rejects_missing_required_category() -> None:
    _, artifact = _fitted_artifact()
    model = load_portable_materiality_payload(artifact)
    row = _row(999)
    categorical = dict(row.categorical)
    del categorical["ticker"]
    invalid = NewsFeatureRow(**{**row.__dict__, "categorical": categorical})

    with pytest.raises(ValueError, match="missing categorical features: ticker"):
        model.predict(invalid)


def test_loader_rejects_mutation_not_bound_by_artifact_digest() -> None:
    _, artifact = _fitted_artifact()
    artifact["classifier"]["intercept"] += 1.0

    with pytest.raises(ValueError, match="payload digest mismatch"):
        load_portable_materiality_payload(artifact)


@pytest.mark.parametrize("mutation", ("coefficient", "scale", "threshold"))
def test_loader_rejects_resigned_malformed_artifact(mutation: str) -> None:
    _, original = _fitted_artifact()
    artifact = copy.deepcopy(original)
    if mutation == "coefficient":
        artifact["classifier"]["coefficients"].pop()
    elif mutation == "scale":
        artifact["preprocessing"]["numeric_scale_factors"][0] = 0.0
    else:
        artifact["coverage_thresholds"]["calibrated"]["20"] = 2.0
    _resign(artifact)

    with pytest.raises(ValueError):
        load_portable_materiality_payload(artifact)


def test_file_loader_enforces_expected_artifact_digest(tmp_path: Path) -> None:
    _, artifact = _fitted_artifact()
    path = tmp_path / "model.json"
    write_json(path, artifact)

    model = load_portable_materiality_model(
        path,
        expected_payload_sha256=artifact["artifact_payload_sha256"],
    )
    assert model.model_id == MODEL_ID
    with pytest.raises(ValueError, match="expected digest"):
        load_portable_materiality_model(path, expected_payload_sha256="0" * 64)


def test_jsonl_prediction_is_outcome_free_and_artifact_bound(tmp_path: Path) -> None:
    _, artifact = _fitted_artifact()
    model = load_portable_materiality_payload(artifact)
    input_path = tmp_path / "inputs.jsonl"
    output_path = tmp_path / "predictions.jsonl"
    input_path.write_text(
        json.dumps(serialize_feature_row(_row(999)), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    report = predict_materiality_jsonl(model, input_path, output_path)
    prediction = json.loads(output_path.read_text(encoding="utf-8"))

    assert report["rows"] == 1
    assert prediction["artifact_payload_sha256"] == artifact["artifact_payload_sha256"]
    assert prediction["model_id"] == MODEL_ID
    assert prediction["event_group_id"] == "group-499"
    assert prediction["ticker"] == "GAZP"
    assert prediction["source_id"] == "telegram_markettwits"
    assert 0 <= prediction["calibrated_probability_material"] <= 1
    assert set(prediction["selected_at_coverage_pct"]) == {"raw", "calibrated"}


def test_jsonl_prediction_rejects_outcome_field(tmp_path: Path) -> None:
    _, artifact = _fitted_artifact()
    model = load_portable_materiality_payload(artifact)
    payload = serialize_feature_row(_row(999))
    payload["return_pct"] = 3.0
    input_path = tmp_path / "inputs.jsonl"
    input_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="feature row fields"):
        predict_materiality_jsonl(model, input_path, tmp_path / "predictions.jsonl")


@pytest.mark.parametrize("field", ("ticker", "source_id"))
def test_jsonl_prediction_rejects_identity_category_mismatch(
    tmp_path: Path,
    field: str,
) -> None:
    _, artifact = _fitted_artifact()
    model = load_portable_materiality_payload(artifact)
    payload = serialize_feature_row(_row(999))
    payload["categorical"][field] = "DIFFERENT"
    input_path = tmp_path / "inputs.jsonl"
    output_path = tmp_path / "predictions.jsonl"
    input_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match=f"{field} identity differs"):
        predict_materiality_jsonl(model, input_path, output_path)

    assert not output_path.exists()


def _training_fixture(
    tmp_path: Path,
    *,
    mutation: str | None = None,
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
    if mutation == "training_after_cutoff":
        training_rows[0] = replace(
            training_rows[0],
            decision_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
    elif mutation == "validation_before_cutoff":
        validation_rows[0] = replace(
            validation_rows[0],
            decision_at=datetime(2025, 12, 31, tzinfo=UTC),
        )
    elif mutation == "evaluation_after_cutoff":
        evaluation_rows[0] = replace(
            evaluation_rows[0],
            decision_at=datetime(2026, 4, 1, tzinfo=UTC),
        )
    elif mutation == "group_overlap":
        validation_rows[0] = replace(
            validation_rows[0],
            event_group_id=training_rows[0].event_group_id,
        )
    rows = [*training_rows, *validation_rows, *evaluation_rows]
    write_jsonl(feature_path, (serialize_feature_row(row) for row in rows))
    training = [
        {"example_id": row.example_id, "return_pct": 1.0 if index % 2 else -1.0}
        for index, row in enumerate(training_rows)
    ]
    validation = [
        {"example_id": row.example_id, "return_pct": 1.0 if index % 2 else -1.0}
        for index, row in enumerate(validation_rows)
    ]
    evaluation_ids = [row.example_id for row in evaluation_rows]
    if mutation == "evaluation_identity_in_training":
        training[0]["example_id"] = evaluation_ids[0]
    bundle_path = tmp_path / "fit-bundle.json"
    write_json(
        bundle_path,
        {
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
                        "materiality_4h": {
                            "training": training,
                            "validation": validation,
                            "evaluation_ids": evaluation_ids,
                        }
                    },
                }
            ],
        },
    )
    artifact_path = tmp_path / "materiality-model.json"
    return bundle_path, artifact_path, signal_dataset_sha256(bundle_path)


def test_latest_fold_training_writes_loadable_artifact(tmp_path: Path) -> None:
    bundle_path, artifact_path, bundle_sha256 = _training_fixture(tmp_path)

    artifact = fit_latest_materiality_artifact(
        bundle_path,
        artifact_path,
        expected_fit_bundle_sha256=bundle_sha256,
    )
    model = load_portable_materiality_model(
        artifact_path,
        expected_payload_sha256=artifact["artifact_payload_sha256"],
    )

    assert artifact["provenance"]["fold"] == "2026_03"
    assert artifact["provenance"]["fit_bundle_sha256"] == signal_dataset_sha256(
        bundle_path
    )
    assert model.predict(_row(999))["artifact_payload_sha256"] == artifact[
        "artifact_payload_sha256"
    ]
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        fit_latest_materiality_artifact(
            bundle_path,
            artifact_path,
            expected_fit_bundle_sha256=bundle_sha256,
        )


def test_training_rejects_untrusted_bundle_before_parsing(tmp_path: Path) -> None:
    bundle_path = tmp_path / "fit-bundle.json"
    bundle_path.write_text("not JSON", encoding="utf-8")

    with pytest.raises(ValueError, match="does not match the expected digest"):
        fit_latest_materiality_artifact(
            bundle_path,
            tmp_path / "model.json",
            expected_fit_bundle_sha256="0" * 64,
        )


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
def test_materiality_clis_require_trusted_digest(
    monkeypatch: pytest.MonkeyPatch,
    arguments: Callable[[], object],
    command_line: list[str],
) -> None:
    monkeypatch.setattr("sys.argv", ["materiality-command", *command_line])

    with pytest.raises(SystemExit):
        arguments()


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("training_after_cutoff", "training timestamp is outside"),
        ("validation_before_cutoff", "validation timestamp is outside"),
        ("evaluation_after_cutoff", "evaluation timestamp is outside"),
        ("group_overlap", "training and validation groups overlap"),
        ("evaluation_identity_in_training", "training and evaluation identities overlap"),
    ),
)
def test_training_rejects_partition_leakage(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    bundle_path, artifact_path, bundle_sha256 = _training_fixture(
        tmp_path,
        mutation=mutation,
    )

    with pytest.raises(ValueError, match=message):
        fit_latest_materiality_artifact(
            bundle_path,
            artifact_path,
            expected_fit_bundle_sha256=bundle_sha256,
        )

    assert not artifact_path.exists()
