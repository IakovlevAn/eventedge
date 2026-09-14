"""Tests for the isolated retained news-model feature contract."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from signal_fixtures import signal_example

from eventedge_research.news_model_features import (
    MinuteOpenSeries,
    NewsFeatureRow,
    build_dataset_feature_rows,
    build_decision_feature_rows,
    deserialize_feature_row,
    retained_feature_specs,
    serialize_feature_row,
)
from eventedge_research.signal_dataset import SignalDatasetExample


def _write_minute_open(path: Path, *, price: float) -> None:
    """Write one local minute-open observation."""
    path.write_text(
        json.dumps({"at": "2026-01-05T07:00:00Z", "open": price}) + "\n",
        encoding="utf-8",
    )


def _row() -> NewsFeatureRow:
    return NewsFeatureRow(
        example_id="example-1",
        event_id="event-1",
        event_group_id="group-1",
        ticker="SBER",
        source_id="telegram_markettwits",
        title="Сбербанк опубликовал результаты",
        content="Чистая прибыль выросла.",
        decision_at=datetime(2026, 1, 5, 7, tzinfo=UTC),
        event_type="financial_results",
        temporal_status="result",
        numeric={
            "reaction_stock_pct": -0.4,
            "reaction_benchmark_pct": -0.1,
            "reaction_abnormal_pct": -0.3,
            "risk_volatility_20d_pct": 1.5,
        },
        categorical={
            "ticker": "SBER",
            "source_id": "telegram_markettwits",
            "publication_session": "main_session",
            "corrected_category": "financial_results",
            "corrected_stage": "result",
            "corrected_issuer_role": "issuer_named_or_tagged",
            "corrected_disposition": "candidate",
        },
    )


def test_feature_row_round_trip_preserves_the_outcome_free_contract() -> None:
    row = _row()

    restored = deserialize_feature_row(serialize_feature_row(row))

    assert restored == row


def test_publication_view_removes_post_publication_reaction() -> None:
    row = _row()
    publication = row.decision_at - timedelta(minutes=5)

    result = build_decision_feature_rows(
        [row],
        {row.example_id: publication},
        mode="publication",
    )[0]

    assert result.decision_at == publication
    assert result.numeric["reaction_stock_pct"] is None
    assert result.numeric["reaction_benchmark_pct"] is None
    assert result.numeric["reaction_abnormal_pct"] is None
    assert result.numeric["derived_abs_reaction_abnormal_pct"] is None


def test_update_view_adds_symmetric_reaction_and_interactions() -> None:
    row = _row()
    publication = row.decision_at - timedelta(minutes=5)

    result = build_decision_feature_rows(
        [row],
        {row.example_id: publication},
        mode="update_5m",
    )[0]

    assert result.decision_at == row.decision_at
    assert result.numeric["derived_abs_reaction_stock_pct"] == 0.4
    assert result.numeric["derived_abs_reaction_abnormal_pct"] == 0.3
    assert result.numeric["derived_abs_reaction_abnormal_to_volatility"] == pytest.approx(0.2)
    assert result.categorical["corrected_stage_x_reaction_sign"] == "result|negative"


def test_retained_specs_exclude_exploratory_text_models() -> None:
    assert set(retained_feature_specs()) == {
        "publication_control",
        "update_5m_common_direction",
        "update_5m_market_control",
        "update_5m_symmetric_materiality",
        "update_5m_corrected_reaction_interactions",
    }


def test_fresh_projection_rejects_a_decision_after_five_minutes() -> None:
    decision_at = datetime(2026, 1, 5, 10, tzinfo=UTC)
    payload = signal_example(1, decision_at=decision_at)
    payload["published_at"] = (decision_at - timedelta(minutes=10)).isoformat()
    example = SignalDatasetExample.model_validate(payload)

    with pytest.raises(ValueError, match="decision, text and features"):
        build_dataset_feature_rows(
            [example],
            {},
            MinuteOpenSeries.from_observations([]),
        )


@pytest.mark.parametrize("reverse_paths", [False, True])
def test_minute_open_series_accepts_matching_duplicates(
    tmp_path: Path,
    reverse_paths: bool,
) -> None:
    first_path = tmp_path / "first.jsonl"
    second_path = tmp_path / "second.jsonl"
    _write_minute_open(first_path, price=100.0)
    _write_minute_open(second_path, price=100.0)
    paths = [first_path, second_path]
    if reverse_paths:
        paths.reverse()

    result = MinuteOpenSeries.from_paths(paths)

    assert result.times == (datetime(2026, 1, 5, 7, tzinfo=UTC),)
    assert result.prices == (100.0,)


@pytest.mark.parametrize("reverse_paths", [False, True])
def test_minute_open_series_rejects_conflicting_duplicates(
    tmp_path: Path,
    reverse_paths: bool,
) -> None:
    first_path = tmp_path / "first.jsonl"
    second_path = tmp_path / "second.jsonl"
    _write_minute_open(first_path, price=100.0)
    _write_minute_open(second_path, price=101.0)
    paths = [first_path, second_path]
    if reverse_paths:
        paths.reverse()

    with pytest.raises(
        ValueError,
        match="conflicting minute opens for timestamp 2026-01-05T07:00:00\\+00:00",
    ):
        MinuteOpenSeries.from_paths(paths)
