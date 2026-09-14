from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from signal_fixtures import signal_example

from eventedge_research.signal_dataset import SignalDatasetExample, load_signal_dataset
from eventedge_research.signal_dataset_builder import (
    MarketOpenObservation,
    SignalEventCandidate,
    SignalEventCandidateV2,
    add_pre_event_market_context,
    build_signal_dataset,
    load_market_open_observations,
    load_signal_event_candidates,
    load_verified_benchmark_archive,
    replace_benchmark_context,
)
from eventedge_research.ydb_signal_candidates import write_signal_event_candidates
from scripts.build_signal_dataset import main as build_signal_dataset_main


def test_dataset_builder_uses_first_post_decision_open_and_timely_four_hour_open() -> None:
    decision_at = datetime(2026, 1, 5, 10, tzinfo=UTC)
    candidate = SignalEventCandidate.model_validate(_candidate(1, decision_at=decision_at))
    observations = [
        MarketOpenObservation.model_validate(payload)
        for payload in _market_observations(decision_at, outcome_delay_minutes=5)
    ]

    examples, report = build_signal_dataset([candidate], observations)

    assert len(examples) == 1
    example = examples[0]
    assert example.entry_at == decision_at + timedelta(minutes=1)
    assert example.entry_price == 100
    assert example.outcome_4h.target_at == example.entry_at + timedelta(hours=4)
    assert example.outcome_4h.observed_at == example.outcome_4h.target_at + timedelta(minutes=5)
    assert example.outcome_4h.price == 102
    assert example.outcome_4h.return_pct == pytest.approx(2)
    assert example.features.pre_event_return_1h_pct == pytest.approx((99 / 98 - 1) * 100)
    assert example.label_available_at == example.outcome_4h.observed_at
    assert report["built_event_tickers"] == 1
    assert report["skip_reasons"] == {}
    assert report["feature_coverage"] == {
        "pre_event_return_1h_pct": 1,
        "pre_event_return_1d_pct": 0,
        "pre_event_return_5d_pct": 0,
    }


def test_dataset_builder_does_not_use_decision_or_stale_open_for_momentum() -> None:
    decision_at = datetime(2026, 1, 5, 10, tzinfo=UTC)
    candidate = SignalEventCandidate.model_validate(_candidate(1, decision_at=decision_at))
    observations = [
        MarketOpenObservation.model_validate(payload)
        for payload in _market_observations(decision_at, outcome_delay_minutes=5)
        if payload["at"]
        not in {
            (decision_at - timedelta(minutes=61)).isoformat(),
            (decision_at - timedelta(minutes=1)).isoformat(),
        }
    ]

    examples, report = build_signal_dataset([candidate], observations)

    assert len(examples) == 1
    assert examples[0].features.pre_event_return_1h_pct is None
    assert report["feature_coverage"] == {
        "pre_event_return_1h_pct": 0,
        "pre_event_return_1d_pct": 0,
        "pre_event_return_5d_pct": 0,
    }


def test_verified_benchmark_archive_rejects_symlink_escape(tmp_path: Path) -> None:
    directory = tmp_path / "benchmark"
    directory.mkdir()
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n")
    (directory / "IMOEX2.jsonl").symlink_to(outside)
    (directory / "complete.json").write_text(
        json.dumps({"ticker": "IMOEX2", "sha256": "0" * 64})
    )

    with pytest.raises(ValueError, match="incomplete or changed"):
        load_verified_benchmark_archive(directory)


def test_dataset_builder_adds_causal_long_horizon_returns_without_lookahead() -> None:
    decision_at = datetime(2026, 1, 12, 10, tzinfo=UTC)
    current_at = decision_at - timedelta(minutes=1)
    entry_at = decision_at + timedelta(minutes=1)
    candidate = SignalEventCandidate.model_validate(_candidate(1, decision_at=decision_at))
    observations = [
        MarketOpenObservation.model_validate(payload)
        for payload in (
            _market_observation(current_at - timedelta(days=5, minutes=10), 80),
            _market_observation(current_at - timedelta(days=5) + timedelta(minutes=1), 8_000),
            _market_observation(current_at - timedelta(days=1, minutes=10), 90),
            _market_observation(current_at - timedelta(days=1) + timedelta(minutes=1), 9_000),
            _market_observation(decision_at - timedelta(minutes=61), 98),
            _market_observation(current_at, 100),
            _market_observation(decision_at, 10_000),
            _market_observation(entry_at, 101),
            _market_observation(entry_at + timedelta(hours=4), 102),
        )
    ]

    examples, report = build_signal_dataset([candidate], observations)

    features = examples[0].features
    assert features.pre_event_return_1d_pct == pytest.approx((100 / 90 - 1) * 100)
    assert features.pre_event_return_5d_pct == pytest.approx((100 / 80 - 1) * 100)
    assert report["feature_coverage"] == {
        "pre_event_return_1h_pct": 1,
        "pre_event_return_1d_pct": 1,
        "pre_event_return_5d_pct": 1,
    }


def test_dataset_builder_adds_causal_benchmark_features_and_abnormal_return() -> None:
    decision_at = datetime(2026, 1, 5, 10, tzinfo=UTC)
    candidate = SignalEventCandidate.model_validate(_candidate(1, decision_at=decision_at))
    observations = [
        MarketOpenObservation.model_validate(payload)
        for payload in _market_observations(decision_at, outcome_delay_minutes=5)
    ]
    entry_at = decision_at + timedelta(minutes=1)
    benchmark_observations = [
        MarketOpenObservation.model_validate(payload)
        for payload in (
            _market_observation(
                decision_at - timedelta(minutes=61),
                2_700,
                ticker="IMOEX",
                provider_id="benchmark-fixture",
            ),
            _market_observation(
                decision_at - timedelta(minutes=1),
                2_710,
                ticker="IMOEX",
                provider_id="benchmark-fixture",
            ),
            _market_observation(
                decision_at,
                9_999,
                ticker="IMOEX",
                provider_id="benchmark-fixture",
            ),
            _market_observation(
                entry_at,
                2_720,
                ticker="IMOEX",
                provider_id="benchmark-fixture",
            ),
            _market_observation(
                entry_at + timedelta(hours=4),
                2_730,
                ticker="IMOEX",
                provider_id="benchmark-fixture",
            ),
        )
    ]

    examples, report = build_signal_dataset(
        [candidate],
        observations,
        benchmark_observations=benchmark_observations,
        benchmark_id="IMOEX",
    )

    example = examples[0]
    benchmark_return = (2_730 / 2_720 - 1) * 100
    assert example.features.benchmark_pre_event_return_1h_pct == pytest.approx(
        (2_710 / 2_700 - 1) * 100
    )
    assert example.outcome_4h.benchmark_id == "IMOEX"
    assert example.outcome_4h.benchmark_return_pct == pytest.approx(benchmark_return)
    assert example.outcome_4h.abnormal_return_pct == pytest.approx(
        example.outcome_4h.return_pct - benchmark_return
    )
    assert example.label_available_at == example.outcome_4h.observed_at
    assert report["benchmark"]["outcome_coverage"] == 1
    assert report["benchmark"]["pre_event_return_1h_coverage"] == 1


def test_dataset_builder_waits_for_later_benchmark_outcome() -> None:
    decision_at = datetime(2026, 1, 5, 10, tzinfo=UTC)
    candidate = SignalEventCandidate.model_validate(_candidate(1, decision_at=decision_at))
    stock = [
        MarketOpenObservation.model_validate(payload)
        for payload in _market_observations(decision_at, outcome_delay_minutes=2)
    ]
    entry_at = decision_at + timedelta(minutes=1)
    benchmark = [
        MarketOpenObservation.model_validate(payload)
        for payload in (
            _market_observation(
                entry_at,
                2_720,
                ticker="IMOEX2",
                provider_id="benchmark-fixture",
            ),
            _market_observation(
                entry_at + timedelta(hours=4, minutes=5),
                2_730,
                ticker="IMOEX2",
                provider_id="benchmark-fixture",
            ),
        )
    ]

    examples, _ = build_signal_dataset(
        [candidate],
        stock,
        benchmark_observations=benchmark,
        benchmark_id="IMOEX2",
    )

    assert examples[0].label_available_at == entry_at + timedelta(hours=4, minutes=5)


def test_market_context_enrichment_preserves_external_entry_and_outcome() -> None:
    decision_at = datetime(2026, 1, 5, 10, tzinfo=UTC)
    example = SignalDatasetExample.model_validate(
        signal_example(1, decision_at=decision_at, pre_event_return_1h_pct=None)
    )
    observations = [
        MarketOpenObservation.model_validate(payload)
        for payload in (
            _market_observation(decision_at - timedelta(minutes=61), 98),
            _market_observation(decision_at - timedelta(minutes=1), 99),
        )
    ]

    enriched, report = add_pre_event_market_context([example], observations)

    assert enriched[0].features.pre_event_return_1h_pct == pytest.approx((99 / 98 - 1) * 100)
    assert enriched[0].entry_at == example.entry_at
    assert enriched[0].entry_price == example.entry_price
    assert enriched[0].outcome_4h == example.outcome_4h
    assert enriched[0].label_available_at == example.label_available_at
    assert report["stored_entries_and_outcomes_preserved"] is True


def test_benchmark_replacement_preserves_stock_outcome() -> None:
    decision_at = datetime(2026, 1, 5, 10, tzinfo=UTC)
    example = SignalDatasetExample.model_validate(
        signal_example(1, decision_at=decision_at, benchmark_return_pct=None)
    )
    benchmark = [
        MarketOpenObservation.model_validate(payload)
        for payload in (
            _market_observation(
                decision_at - timedelta(minutes=61),
                2_700,
                ticker="IMOEX2",
                provider_id="benchmark-fixture",
            ),
            _market_observation(
                decision_at - timedelta(minutes=1),
                2_710,
                ticker="IMOEX2",
                provider_id="benchmark-fixture",
            ),
            _market_observation(
                example.entry_at,
                2_720,
                ticker="IMOEX2",
                provider_id="benchmark-fixture",
            ),
            _market_observation(
                example.outcome_4h.target_at + timedelta(minutes=5),
                2_730,
                ticker="IMOEX2",
                provider_id="benchmark-fixture",
            ),
        )
    ]

    enriched, report = replace_benchmark_context(
        [example],
        benchmark,
        benchmark_id="IMOEX2",
    )

    actual = enriched[0]
    benchmark_return = (2_730 / 2_720 - 1) * 100
    assert actual.entry_at == example.entry_at
    assert actual.entry_price == example.entry_price
    assert actual.outcome_4h.price == example.outcome_4h.price
    assert actual.outcome_4h.return_pct == example.outcome_4h.return_pct
    assert actual.outcome_4h.benchmark_id == "IMOEX2"
    assert actual.outcome_4h.benchmark_return_pct == pytest.approx(benchmark_return)
    assert actual.outcome_4h.abnormal_return_pct == pytest.approx(
        example.outcome_4h.return_pct - benchmark_return
    )
    assert actual.features.benchmark_pre_event_return_1h_pct == pytest.approx(
        (2_710 / 2_700 - 1) * 100
    )
    assert actual.label_available_at == example.outcome_4h.target_at + timedelta(minutes=5)
    assert report["previous_benchmark_ids"] == {"missing": 1}
    assert report["outcome_coverage"] == 1
    assert report["stock_entries_and_price_outcomes_preserved"] is True
    assert report["label_availability_changed_rows"] == 1


def test_dataset_builder_rejects_mismatched_benchmark_id() -> None:
    decision_at = datetime(2026, 1, 5, 10, tzinfo=UTC)
    candidate = SignalEventCandidate.model_validate(_candidate(1, decision_at=decision_at))
    observations = [
        MarketOpenObservation.model_validate(payload)
        for payload in _market_observations(decision_at, outcome_delay_minutes=5)
    ]

    with pytest.raises(ValueError, match="only benchmark_id"):
        build_signal_dataset(
            [candidate],
            observations,
            benchmark_observations=observations,
            benchmark_id="IMOEX",
        )


def test_dataset_builder_reports_missed_window_and_unadjusted_actions() -> None:
    decision_at = datetime(2026, 1, 5, 10, tzinfo=UTC)
    candidate = SignalEventCandidate.model_validate(_candidate(1, decision_at=decision_at))
    late_observations = [
        MarketOpenObservation.model_validate(payload)
        for payload in _market_observations(decision_at, outcome_delay_minutes=21)
    ]

    examples, report = build_signal_dataset([candidate], late_observations)

    assert examples == []
    assert report["skip_reasons"] == {"outcome_open_missed_window": 1}

    adjusted_candidate = SignalEventCandidate.model_validate(
        _candidate(2, decision_at=decision_at, corporate_action_status="adjusted")
    )
    raw_observations = [
        MarketOpenObservation.model_validate(payload)
        for payload in _market_observations(decision_at, outcome_delay_minutes=5)
    ]
    examples, report = build_signal_dataset([adjusted_candidate], raw_observations)
    assert examples == []
    assert report["skip_reasons"] == {"unadjusted_corporate_action": 1}


def test_dataset_builder_preserves_unknown_corporate_action_status() -> None:
    decision_at = datetime(2026, 1, 5, 10, tzinfo=UTC)
    candidate = SignalEventCandidate.model_validate(
        _candidate(
            1,
            decision_at=decision_at,
            corporate_action_status="unknown",
        )
    )
    observations = [
        MarketOpenObservation.model_validate(payload)
        for payload in _market_observations(decision_at, outcome_delay_minutes=5)
    ]

    examples, _ = build_signal_dataset([candidate], observations)

    assert examples[0].corporate_action_status == "unknown"


def test_dataset_builder_rejects_stale_entry_open() -> None:
    decision_at = datetime(2020, 1, 1, 10, tzinfo=UTC)
    candidate = SignalEventCandidate.model_validate(_candidate(1, decision_at=decision_at))
    stale_entry_at = decision_at + timedelta(days=11)
    observations = [
        MarketOpenObservation.model_validate(_market_observation(stale_entry_at, 100)),
        MarketOpenObservation.model_validate(
            _market_observation(stale_entry_at + timedelta(hours=4), 101)
        ),
    ]

    examples, report = build_signal_dataset([candidate], observations)

    assert examples == []
    assert report["skip_reasons"] == {"entry_open_missed_window": 1}


@pytest.mark.parametrize("delay_seconds", [1, 60, 61, 1200, 86400])
def test_immediate_entry_limit_does_not_delay_the_signal(delay_seconds: int) -> None:
    decision_at = datetime(2026, 1, 5, 10, tzinfo=UTC)
    candidate = SignalEventCandidate.model_validate(_candidate(1, decision_at=decision_at))
    entry_at = decision_at + timedelta(seconds=delay_seconds)
    observations = [
        MarketOpenObservation.model_validate(_market_observation(at, price))
        for at, price in (
            (decision_at, 999),
            (entry_at, 100),
            (entry_at + timedelta(hours=4, minutes=20), 102),
        )
    ]

    examples, report = build_signal_dataset(
        [candidate], observations, maximum_entry_lag=timedelta(seconds=60)
    )

    assert report["outcome_policy"]["maximum_entry_lag_seconds"] == 60
    assert report["outcome_policy"]["maximum_observation_lag_seconds"] == 1200
    if delay_seconds > 60:
        assert examples == []
        assert report["skip_reasons"] == {"entry_open_missed_window": 1}
    else:
        assert examples[0].decision_at == candidate.decision_at
        assert examples[0].received_at == candidate.received_at
        assert examples[0].entry_at == entry_at
        assert examples[0].entry_price == 100
        assert examples[0].outcome_4h.return_pct == pytest.approx(2)


@pytest.mark.parametrize("limit", [timedelta(0), timedelta(seconds=-1), timedelta(days=11)])
def test_dataset_builder_rejects_invalid_entry_limit(limit: timedelta) -> None:
    with pytest.raises(ValueError, match="maximum entry lag"):
        build_signal_dataset([], [], maximum_entry_lag=limit)


def test_dataset_builder_loaders_reject_synthetic_and_duplicates(tmp_path: Path) -> None:
    decision_at = datetime(2026, 1, 5, 10, tzinfo=UTC)
    candidates_path = tmp_path / "candidates.jsonl"
    candidate = _candidate(1, decision_at=decision_at)
    candidates_path.write_text(
        "\n".join(json.dumps(candidate) for _ in range(2)) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate event/ticker candidate"):
        load_signal_event_candidates(candidates_path)

    legacy_candidate = _candidate(3, decision_at=decision_at)
    del legacy_candidate["schema_version"]
    candidates_path.write_text(json.dumps(legacy_candidate) + "\n", encoding="utf-8")
    assert load_signal_event_candidates(candidates_path)[0].schema_version == (
        "signal-event-candidate-1.0"
    )

    observations_path = tmp_path / "opens.jsonl"
    observation = _market_observations(decision_at, outcome_delay_minutes=5)[0]
    observations_path.write_text(json.dumps(observation) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="synthetic labels are allowed only for tests"):
        load_market_open_observations(observations_path)

    observations_path.write_text(
        "\n".join(json.dumps(observation) for _ in range(2)) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate market observation"):
        load_market_open_observations(observations_path, allow_synthetic=True)


def test_candidate_loader_writer_and_builder_reject_mixed_versions(
    tmp_path: Path,
) -> None:
    first_decision = datetime(2026, 1, 5, 10, tzinfo=UTC)
    second_decision = first_decision + timedelta(days=1)
    version_one = SignalEventCandidate.model_validate(
        _candidate(1, decision_at=first_decision)
    )
    version_two_payload = _candidate(2, decision_at=second_decision)
    version_two_payload.update(
        schema_version="signal-event-candidate-2.0",
        received_at=version_two_payload["decision_at"],
        corporate_action_status="unknown",
        event_group_id=version_two_payload["event_id"],
        source_url="https://t.me/source/2",
        retrieved_at=(second_decision + timedelta(days=1)).isoformat(),
        admission_version="test-admission-1.0",
        analysis_title=version_two_payload["title"],
        analysis_content_sha256=hashlib.sha256(
            str(version_two_payload["content"]).encode("utf-8")
        ).hexdigest(),
        receipt_provenance="publication_plus_assumed_5_minutes",
        text_provenance="no_edit_marker_current_view_not_first_snapshot",
    )
    version_two = SignalEventCandidateV2.model_validate(version_two_payload)
    mixed_path = tmp_path / "mixed.jsonl"
    mixed_path.write_text(
        "".join(
            json.dumps(candidate.model_dump(mode="json")) + "\n"
            for candidate in (version_one, version_two)
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="mixed signal event candidate"):
        load_signal_event_candidates(mixed_path)
    with pytest.raises(ValueError, match="mixed signal event candidate"):
        write_signal_event_candidates(
            tmp_path / "mixed-output.jsonl",
            [version_one, version_two],
        )
    with pytest.raises(ValueError, match="mixed signal event candidate"):
        build_signal_dataset([version_one, version_two], [])


def test_build_signal_dataset_cli_writes_reproducible_dataset_and_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    decision_at = datetime(2026, 1, 5, 10, tzinfo=UTC)
    candidates_path = tmp_path / "candidates.jsonl"
    observations_path = tmp_path / "opens.jsonl"
    dataset_path = tmp_path / "dataset.jsonl"
    report_path = tmp_path / "report.json"
    candidates_path.write_text(
        json.dumps(_candidate(1, decision_at=decision_at), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    observations_path.write_text(
        "\n".join(
            json.dumps(payload)
            for payload in _market_observations(decision_at, outcome_delay_minutes=5)
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "build_signal_dataset",
            "--candidates",
            str(candidates_path),
            "--market-opens",
            str(observations_path),
            "--output",
            str(dataset_path),
            "--report",
            str(report_path),
            "--maximum-entry-lag-seconds",
            "60",
            "--allow-synthetic",
        ],
    )

    build_signal_dataset_main()

    report = json.loads(capsys.readouterr().out)
    assert json.loads(report_path.read_text(encoding="utf-8")) == report
    assert report["built_event_tickers"] == 1
    assert report["outcome_policy"]["maximum_entry_lag_seconds"] == 60
    assert len(report["dataset_sha256"]) == 64
    assert set(report["input_sha256"]) == {"candidates", "market_opens"}
    examples = load_signal_dataset(dataset_path, allow_synthetic=True)
    assert len(examples) == 1
    assert examples[0].id.startswith("sigex_")


def _candidate(
    index: int,
    *,
    decision_at: datetime,
    corporate_action_status: str = "none",
) -> dict[str, object]:
    example = signal_example(index, decision_at=decision_at)
    return {
        "schema_version": "signal-event-candidate-1.0",
        "event_id": example["event_id"],
        "ticker": example["ticker"],
        "news_ids": example["news_ids"],
        "primary_news_id": example["primary_news_id"],
        "source_id": example["source_id"],
        "corroborating_source_ids": example["corroborating_source_ids"],
        "title": example["title"],
        "content": example["content"],
        "categories": example["categories"],
        "published_at": example["published_at"],
        "received_at": example["received_at"],
        "decision_at": example["decision_at"],
        "features": example["features"],
        "corporate_action_status": corporate_action_status,
        "overlapping_event_ids": example["overlapping_event_ids"],
        "notes": "Reviewed synthetic builder fixture.",
    }


def _market_observations(
    decision_at: datetime,
    *,
    outcome_delay_minutes: int,
) -> list[dict[str, object]]:
    entry_at = decision_at + timedelta(minutes=1)
    return [
        _market_observation(decision_at - timedelta(minutes=61), 98),
        _market_observation(decision_at - timedelta(minutes=1), 99),
        _market_observation(decision_at, 1_000),
        _market_observation(entry_at, 100),
        _market_observation(
            entry_at + timedelta(hours=4, minutes=outcome_delay_minutes),
            102,
        ),
    ]


def _market_observation(
    at: datetime,
    price: float,
    *,
    ticker: str = "SBER",
    provider_id: str = "synthetic-fixture",
) -> dict[str, object]:
    return {
        "schema_version": "market-open-observation-1.0",
        "ticker": ticker,
        "at": at.isoformat(),
        "open": price,
        "provider_id": provider_id,
        "adjusted": False,
        "label_source": "synthetic_test",
    }
