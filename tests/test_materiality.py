"""Tests for production-safe news materiality inference."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

import pytest

from eventedge.materiality import (
    PACKAGED_ARTIFACT_NAME,
    PACKAGED_ARTIFACT_PAYLOAD_SHA256,
    MaterialityFeatureRow,
    MaterialityMode,
    artifact_payload_sha256,
    load_materiality_payload,
    load_packaged_materiality_model,
    runtime_from_environment,
)
from eventedge_research.news_model_artifact import load_portable_materiality_model


def _row() -> MaterialityFeatureRow:
    return MaterialityFeatureRow(
        news_id="news_example",
        ticker="SBER",
        source_id="telegram_markettwits",
        decision_at=datetime(2026, 9, 10, 9, 5, tzinfo=UTC),
        numeric={
            "market_pre_event_return_1h_pct": 0.1,
            "market_pre_event_return_1d_pct": -0.2,
            "market_pre_event_return_5d_pct": 0.8,
            "market_benchmark_pre_event_return_1h_pct": 0.03,
            "risk_volatility_20d_pct": 1.2,
            "risk_beta_60d": 0.9,
            "risk_beta_adjusted_pre_event_1h_pct": 0.073,
            "time_hour_sin": 0.7,
            "time_hour_cos": -0.7,
            "time_weekday_sin": 0.4,
            "time_weekday_cos": -0.9,
            "time_year": 2026.0,
            "time_month": 9.0,
            "reaction_stock_pct": 0.6,
            "reaction_benchmark_pct": 0.1,
            "reaction_abnormal_pct": 0.5,
            "derived_abs_reaction_stock_pct": 0.6,
            "derived_abs_reaction_abnormal_pct": 0.5,
            "derived_abs_reaction_abnormal_to_volatility": 0.5 / 1.2,
        },
        categorical={
            "ticker": "SBER",
            "source_id": "telegram_markettwits",
            "publication_session": "main_session",
        },
    )


def _artifact_payload() -> dict[str, object]:
    artifact = resources.files("eventedge").joinpath("models", PACKAGED_ARTIFACT_NAME)
    value = json.loads(artifact.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_packaged_runtime_prediction_matches_research_reference() -> None:
    runtime_model = load_packaged_materiality_model()
    artifact = resources.files("eventedge").joinpath("models", PACKAGED_ARTIFACT_NAME)
    reference = load_portable_materiality_model(
        Path(str(artifact)),
        expected_payload_sha256=PACKAGED_ARTIFACT_PAYLOAD_SHA256,
    )

    score = runtime_model.predict(_row())

    assert score.raw_probability == pytest.approx(
        reference.raw_probability_material(_row()), abs=1e-14
    )
    assert score.calibrated_probability == pytest.approx(
        reference.probability_material(_row()), abs=1e-14
    )
    assert score.model_version == runtime_model.model_version
    assert set(score.selected_at_coverage_pct) == {"raw", "calibrated"}


def test_packaged_runtime_tracks_known_and_unseen_categories() -> None:
    model = load_packaged_materiality_model()

    assert model.category_was_seen("ticker", "SBER")
    assert model.category_was_seen("source_id", "telegram_markettwits")
    assert not model.category_was_seen("source_id", "interfax")
    with pytest.raises(ValueError, match="unsupported materiality category"):
        model.category_was_seen("event_type", "financial_results")


def test_disabled_runtime_does_not_read_configured_artifact() -> None:
    runtime = runtime_from_environment(
        {
            "NEWS_MATERIALITY_MODE": "disabled",
            "NEWS_MATERIALITY_ARTIFACT_PATH": "/missing/model.json",
        }
    )

    assert runtime.mode is MaterialityMode.DISABLED
    assert not runtime.enabled
    assert runtime.model is None


def test_enabled_runtime_loads_only_the_pinned_artifact() -> None:
    runtime = runtime_from_environment(
        {
            "NEWS_MATERIALITY_MODE": "shadow",
            "NEWS_MATERIALITY_BATCH_LIMIT": "3",
        }
    )

    assert runtime.mode is MaterialityMode.SHADOW
    assert runtime.enabled
    assert runtime.batch_limit == 3
    assert runtime.model is not None
    assert runtime.model.artifact_payload_sha256 == PACKAGED_ARTIFACT_PAYLOAD_SHA256


@pytest.mark.parametrize(
    ("environment", "message"),
    (
        ({"NEWS_MATERIALITY_MODE": "enforce"}, "must be disabled, shadow or rank"),
        (
            {
                "NEWS_MATERIALITY_MODE": "shadow",
                "NEWS_MATERIALITY_BATCH_LIMIT": "0",
            },
            "must be between 1 and 40",
        ),
    ),
)
def test_runtime_rejects_invalid_environment(
    environment: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        runtime_from_environment(environment)


def test_loader_rejects_resigned_incompatible_artifact() -> None:
    payload = copy.deepcopy(_artifact_payload())
    payload["feature_spec"]["include_text"] = True
    payload["artifact_payload_sha256"] = artifact_payload_sha256(payload)

    with pytest.raises(ValueError, match="feature spec is incompatible"):
        load_materiality_payload(payload, str(payload["artifact_payload_sha256"]))
