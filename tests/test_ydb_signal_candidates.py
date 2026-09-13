from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eventedge_research.signal_dataset_builder import load_signal_event_candidates
from eventedge_research.ydb_signal_candidates import (
    build_ydb_signal_candidates,
    write_signal_event_candidates,
)


def test_build_candidates_clusters_duplicates_without_future_corroboration(
    tmp_path: Path,
) -> None:
    received_at = datetime(2026, 8, 4, 9, tzinfo=UTC)
    rows = [
        _news_row(
            "news_1",
            source_id="tass",
            title="Сбербанк сообщил, что чистая прибыль выросла на 20%",
            published_at=received_at - timedelta(minutes=5),
            received_at=received_at,
        ),
        _news_row(
            "news_2",
            source_id="interfax",
            title="Сбербанк сообщил: чистая прибыль выросла на 20%",
            published_at=received_at + timedelta(minutes=20),
            received_at=received_at + timedelta(minutes=21),
        ),
    ]
    export_directory = _write_export(tmp_path, rows)

    candidates, report = build_ydb_signal_candidates(export_directory)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.ticker == "SBER"
    assert candidate.primary_news_id == "news_1"
    assert candidate.news_ids == ("news_1",)
    assert candidate.corroborating_source_ids == ()
    assert candidate.decision_at == received_at
    assert candidate.features.as_of == received_at
    assert candidate.features.rule_direction == "up"
    assert candidate.corporate_action_status == "unknown"
    assert report["eligible_news_rows"] == 2
    assert report["clustered_events"] == 1
    assert report["multi_publication_events"] == 1
    assert report["source_read_consistency"] == {
        "mode": "legacy_manifest_unspecified",
        "shared_snapshot": False,
        "concurrent_mutation_safe": False,
        "interpretation": (
            "legacy manifest has no consistency assertion; do not treat it as a "
            "coherent point-in-time snapshot"
        ),
    }


def test_build_candidates_skips_invalid_chronology_and_unknown_tickers(
    tmp_path: Path,
) -> None:
    received_at = datetime(2026, 8, 4, 9, tzinfo=UTC)
    rows = [
        _news_row(
            "news_future",
            source_id="tass",
            title="Сбербанк сообщил о прибыли",
            published_at=received_at + timedelta(seconds=1),
            received_at=received_at,
        ),
        _news_row(
            "news_unknown",
            source_id="tass",
            title="Неизвестная компания сообщила о прибыли",
            published_at=received_at,
            received_at=received_at,
            tickers=["UNKNOWN"],
        ),
    ]
    export_directory = _write_export(tmp_path, rows)

    candidates, report = build_ydb_signal_candidates(export_directory)

    assert candidates == []
    assert report["skipped_news_rows"] == {
        "no_direct_ticker": 1,
        "published_after_received": 1,
    }


def test_build_candidates_can_exclude_stale_backlog(tmp_path: Path) -> None:
    received_at = datetime(2026, 8, 4, 9, tzinfo=UTC)
    export_directory = _write_export(
        tmp_path,
        [
            _news_row(
                "fresh",
                source_id="tass",
                title="Сбербанк сообщил о росте прибыли",
                published_at=received_at - timedelta(hours=24),
                received_at=received_at,
            ),
            _news_row(
                "stale",
                source_id="tass",
                title="Сбербанк сообщил о росте прибыли",
                published_at=received_at - timedelta(hours=24, seconds=1),
                received_at=received_at,
            ),
        ],
    )

    candidates, report = build_ydb_signal_candidates(
        export_directory,
        maximum_publication_age=timedelta(hours=24),
        maximum_decision_delay=timedelta(days=1),
    )

    assert len(candidates) == 1
    assert candidates[0].primary_news_id == "fresh"
    assert report["skipped_news_rows"] == {"publication_too_old": 1}
    assert report["freshness_policy"] == {
        "maximum_publication_age_seconds": 86_400.0,
        "timestamp_pair": "published_at_to_received_at",
        "maximum_decision_delay_seconds": 86_400.0,
        "decision_timestamp": "final_feature_available_at",
    }


def test_build_candidates_rejects_nonpositive_publication_age_limit(
    tmp_path: Path,
) -> None:
    export_directory = _write_export(tmp_path, [])

    with pytest.raises(ValueError, match="maximum publication age"):
        build_ydb_signal_candidates(
            export_directory,
            maximum_publication_age=timedelta(0),
        )


def test_build_candidates_rejects_decisions_after_five_minutes(
    tmp_path: Path,
) -> None:
    received_at = datetime(2026, 8, 4, 9, tzinfo=UTC)
    export_directory = _write_export(
        tmp_path,
        [
            _news_row(
                "late_decision",
                source_id="tass",
                title="Сбербанк сообщил о росте прибыли",
                published_at=received_at - timedelta(minutes=5, seconds=1),
                received_at=received_at,
            )
        ],
    )

    candidates, report = build_ydb_signal_candidates(export_directory)

    assert candidates == []
    assert report["skipped_news_rows"] == {
        "decision_after_admission_cutoff": 1,
    }


def test_build_candidates_rejects_nonpositive_decision_delay(
    tmp_path: Path,
) -> None:
    export_directory = _write_export(tmp_path, [])

    with pytest.raises(ValueError, match="maximum decision delay"):
        build_ydb_signal_candidates(
            export_directory,
            maximum_decision_delay=timedelta(0),
        )


def test_build_candidates_requires_explicit_telegram_permission(tmp_path: Path) -> None:
    received_at = datetime(2026, 8, 4, 9, tzinfo=UTC)
    export_directory = _write_export(
        tmp_path,
        [
            _news_row(
                "telegram_news",
                source_id="telegram_example",
                title="Сбербанк сообщил о росте чистой прибыли на 20%",
                published_at=received_at,
                received_at=received_at,
            )
        ],
    )

    candidates, report = build_ydb_signal_candidates(export_directory)

    assert candidates == []
    assert report["skipped_news_rows"] == {"telegram_permission_required": 1}
    assert report["telegram_policy"] == {
        "permission_asserted": False,
        "rows_seen": 1,
        "rows_included": 0,
    }

    permitted, permitted_report = build_ydb_signal_candidates(
        export_directory,
        include_telegram_with_permission=True,
    )
    assert len(permitted) == 1
    assert permitted_report["telegram_policy"] == {
        "permission_asserted": True,
        "rows_seen": 1,
        "rows_included": 1,
    }


def test_build_candidates_uses_timely_payload_bound_stored_features(
    tmp_path: Path,
) -> None:
    received_at = datetime(2026, 8, 4, 9, tzinfo=UTC)
    feature_at = received_at + timedelta(minutes=2)
    export_directory = _write_export(
        tmp_path,
        [
            _news_row(
                "news_1",
                source_id="tass",
                title="Сбербанк сообщил, что чистая прибыль выросла на 20%",
                published_at=received_at,
                received_at=received_at,
                created_at=feature_at,
            )
        ],
        feature_rows=[_feature_row("news_1", created_at=feature_at)],
    )

    candidates, report = build_ydb_signal_candidates(
        export_directory,
        use_stored_feature_sets=True,
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.decision_at == feature_at
    assert candidate.features.as_of == feature_at
    assert candidate.features.event_type == "financial_results"
    assert candidate.features.polarity == 0.8
    assert candidate.features.semantic_extractor_version == "yandexgpt-lite-0.6.1"
    assert candidate.features.semantic_feature_set_id == "feat_news_1"
    assert report["semantic_extractors"] == {"yandexgpt-lite-0.6.1": 1}
    assert report["stored_feature_policy"] == {
        "enabled": True,
        "payload_binding": "news_id_and_exact_processing_created_at",
        "maximum_latency_seconds": 300.0,
    }


def test_build_candidates_rejects_mismatched_and_late_stored_features(
    tmp_path: Path,
) -> None:
    received_at = datetime(2026, 8, 4, 9, tzinfo=UTC)
    payload_at = received_at + timedelta(minutes=2)
    late_at = received_at + timedelta(minutes=6)
    export_directory = _write_export(
        tmp_path,
        [
            _news_row(
                "news_mismatch",
                source_id="tass",
                title="Сбербанк сообщил о росте прибыли",
                published_at=received_at,
                received_at=received_at,
                created_at=payload_at,
            ),
            _news_row(
                "news_late",
                source_id="tass",
                title="Сбербанк сообщил о росте прибыли",
                published_at=received_at,
                received_at=received_at,
                created_at=late_at,
            ),
        ],
        feature_rows=[
            _feature_row(
                "news_mismatch",
                created_at=received_at + timedelta(minutes=1),
            ),
            _feature_row("news_late", created_at=late_at),
        ],
    )

    candidates, report = build_ydb_signal_candidates(
        export_directory,
        use_stored_feature_sets=True,
    )

    assert candidates == []
    assert report["skipped_news_rows"] == {
        "stored_feature_payload_mismatch": 1,
        "stored_feature_too_late": 1,
    }


def test_write_candidates_refuses_overwrite_and_round_trips(tmp_path: Path) -> None:
    received_at = datetime(2026, 8, 4, 9, tzinfo=UTC)
    export_directory = _write_export(
        tmp_path,
        [
            _news_row(
                "news_1",
                source_id="tass",
                title="Сбербанк сообщил о росте чистой прибыли на 20%",
                published_at=received_at,
                received_at=received_at,
            )
        ],
    )
    candidates, _ = build_ydb_signal_candidates(export_directory)
    output = tmp_path / "candidates.jsonl"

    fingerprint = write_signal_event_candidates(output, candidates)

    assert fingerprint == hashlib.sha256(output.read_bytes()).hexdigest()
    assert load_signal_event_candidates(output) == candidates
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        write_signal_event_candidates(output, candidates)


def _write_export(
    tmp_path: Path,
    rows: list[dict[str, object]],
    *,
    feature_rows: list[dict[str, object]] | None = None,
) -> Path:
    directory = tmp_path / "export"
    directory.mkdir()
    news_path = directory / "news_items.jsonl"
    news_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    tables = {
        "news_items": {
            "file": "news_items.jsonl",
            "rows": len(rows),
            "truncated": False,
            "sha256": hashlib.sha256(news_path.read_bytes()).hexdigest(),
        }
    }
    if feature_rows is not None:
        feature_path = directory / "feature_sets.jsonl"
        feature_path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in feature_rows),
            encoding="utf-8",
        )
        tables["feature_sets"] = {
            "file": "feature_sets.jsonl",
            "rows": len(feature_rows),
            "truncated": False,
            "sha256": hashlib.sha256(feature_path.read_bytes()).hexdigest(),
        }
    manifest = {
        "schema_version": "eventedge-ydb-export-manifest-1.0",
        "complete": True,
        "database": "/database",
        "tables": tables,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


def _news_row(
    news_id: str,
    *,
    source_id: str,
    title: str,
    published_at: datetime,
    received_at: datetime,
    tickers: list[str] | None = None,
    created_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "news_id": news_id,
        "source_id": source_id,
        "external_id": f"external-{news_id}",
        "published_at": published_at.isoformat(),
        "received_at": received_at.isoformat(),
        "title": title,
        "url": f"https://example.test/{news_id}",
        "content": title,
        "language": "ru",
        "source_metadata": {
            "categories": ["Экономика"],
            "tickers": tickers if tickers is not None else ["SBER"],
        },
        "created_at": (created_at or received_at).isoformat(),
    }


def _feature_row(news_id: str, *, created_at: datetime) -> dict[str, object]:
    extractor_version = "yandexgpt-lite-0.6.1"
    return {
        "feature_set_id": f"feat_{news_id}",
        "news_id": news_id,
        "schema_version": "news-features-0.1",
        "extractor_version": extractor_version,
        "features": {
            "schema_version": "news-features-0.1",
            "extractor_version": extractor_version,
            "event_type": "financial_results",
            "instruments": [
                {
                    "ticker": "SBER",
                    "relevance": 1.0,
                    "matched_alias": "Сбербанк",
                }
            ],
            "facts": [],
            "evidence_quotes": [],
            "polarity": 0.8,
            "materiality": 0.9,
            "novelty": 1.0,
            "temporal_status": "current",
            "rationale": "Рост прибыли является существенным корпоративным событием.",
        },
        "created_at": created_at.isoformat(),
    }
