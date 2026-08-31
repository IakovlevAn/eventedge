from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from eventedge.llm import RuleBasedNewsAnalyzer
from eventedge.main import (
    active_signals_from_content,
    build_news_response_payload,
    reprocess_signal_candidates_batch,
)
from eventedge.ml_router import (
    MlRouterArtifact,
    MlRouterMode,
    MlRouterRuntime,
    MlRouterSafetyMetrics,
    MlRouterTrainingRow,
    build_ml_router_artifact,
    runtime_from_environment,
    write_artifact,
)
from eventedge.storage import MemoryNewsRepository, NewsDocument
from scripts.train_ml_router import main as train_ml_router_main


def row(index: int, *, relevant: bool, token: str) -> MlRouterTrainingRow:
    return MlRouterTrainingRow(
        id=f"row-{index}",
        event_id=f"event-{index}",
        title=f"Сбербанк {token}",
        content=f"Компания сообщила: {token}",
        categories=("Компании",),
        relevant=relevant,
        received_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index),
        labeled_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index + 1),
    )


def separable_rows(start: int, count: int) -> list[MlRouterTrainingRow]:
    return [
        row(
            start + index,
            relevant=index % 2 == 0,
            token="дивиденды" if index % 2 == 0 else "интервью",
        )
        for index in range(count)
    ]


def artifact(*, label_source: str = "human") -> MlRouterArtifact:
    return build_ml_router_artifact(
        train=separable_rows(0, 120),
        validation=separable_rows(200, 40),
        test=separable_rows(300, 40),
        label_source=label_source,
        trained_at=datetime(2026, 8, 16, tzinfo=UTC),
        dataset_sha256="a" * 64,
    )


def test_training_is_deterministic_and_uses_train_vocabulary_only() -> None:
    train = separable_rows(0, 20)
    validation = separable_rows(100, 10)
    validation[0] = row(100, relevant=True, token="validationonlytoken")
    kwargs = {
        "train": train,
        "validation": validation,
        "test": separable_rows(200, 10),
        "label_source": "synthetic_test",
        "trained_at": datetime(2026, 8, 16, tzinfo=UTC),
        "dataset_sha256": "b" * 64,
    }

    first = build_ml_router_artifact(**kwargs)
    second = build_ml_router_artifact(**kwargs)

    assert first == second
    assert "word:validationonlytoken" not in first.vocabulary


def test_builder_rejects_event_and_time_leakage() -> None:
    train = separable_rows(0, 20)
    validation = separable_rows(100, 10)
    test = separable_rows(200, 10)
    common = {
        "label_source": "synthetic_test",
        "trained_at": datetime(2026, 8, 16, tzinfo=UTC),
        "dataset_sha256": "d" * 64,
    }

    leaked_validation = [replace(validation[0], event_id=train[0].event_id), *validation[1:]]
    with pytest.raises(ValueError, match="event leakage"):
        build_ml_router_artifact(
            train=train,
            validation=leaked_validation,
            test=test,
            **common,
        )

    time_leaked_validation = [
        replace(item, received_at=datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=index))
        for index, item in enumerate(validation)
    ]
    with pytest.raises(ValueError, match="strictly chronological"):
        build_ml_router_artifact(
            train=train,
            validation=time_leaked_validation,
            test=test,
            **common,
        )

    future_labeled_test = [
        replace(item, labeled_at=datetime(2027, 1, 1, tzinfo=UTC)) for item in test
    ]
    with pytest.raises(ValueError, match="labels unavailable"):
        build_ml_router_artifact(
            train=train,
            validation=validation,
            test=future_labeled_test,
            **common,
        )


def test_production_loader_rejects_synthetic_and_unsafe_enforcement(
    tmp_path: Path,
) -> None:
    synthetic_path = tmp_path / "synthetic.json"
    write_artifact(synthetic_path, artifact(label_source="synthetic_test"))
    with pytest.raises(RuntimeError, match="production ML router requires human labels"):
        runtime_from_environment(
            {
                "APP_ENV": "prod",
                "ML_ROUTER_MODE": "shadow",
                "ML_ROUTER_ARTIFACT_PATH": str(synthetic_path),
            }
        )

    unsafe = artifact().model_copy(update={"training_examples": 10, "training_events": 10})
    unsafe_path = tmp_path / "unsafe.json"
    write_artifact(unsafe_path, unsafe)
    with pytest.raises(RuntimeError, match="does not pass the production gate"):
        runtime_from_environment(
            {
                "ML_ROUTER_MODE": "enforce",
                "ML_ROUTER_ARTIFACT_PATH": str(unsafe_path),
            }
        )

    shadow = runtime_from_environment(
        {
            "ML_ROUTER_MODE": "shadow",
            "ML_ROUTER_ARTIFACT_PATH": str(unsafe_path),
        }
    )
    assert shadow is not None
    assert shadow.mode is MlRouterMode.SHADOW


def test_missing_or_invalid_enabled_configuration_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="ARTIFACT_PATH is required"):
        runtime_from_environment({"ML_ROUTER_MODE": "shadow"})
    with pytest.raises(RuntimeError, match="must be disabled, shadow or enforce"):
        runtime_from_environment({"ML_ROUTER_MODE": "maybe"})

    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="artifact is invalid"):
        runtime_from_environment(
            {
                "ML_ROUTER_MODE": "shadow",
                "ML_ROUTER_ARTIFACT_PATH": str(invalid_path),
            }
        )


def test_safety_metrics_cannot_be_forged_independently_of_counts() -> None:
    with pytest.raises(ValidationError, match="rejection_precision is inconsistent"):
        MlRouterSafetyMetrics(
            observations=40,
            relevant=20,
            irrelevant=20,
            rejected=20,
            false_rejections=10,
            rejection_precision=1,
            relevant_recall=0.5,
            rejection_coverage=0.5,
        )


def rejecting_runtime(mode: MlRouterMode) -> MlRouterRuntime:
    metrics = MlRouterSafetyMetrics(
        observations=40,
        relevant=20,
        irrelevant=20,
        rejected=20,
        false_rejections=0,
        rejection_precision=1,
        relevant_recall=1,
        rejection_coverage=0.5,
    )
    forced_reject = MlRouterArtifact(
        label_source="human",
        trained_at=datetime(2026, 8, 16, tzinfo=UTC),
        dataset_sha256="c" * 64,
        training_examples=120,
        training_events=120,
        train_received_at_through=datetime(2026, 4, 30, tzinfo=UTC),
        validation_received_at_from=datetime(2026, 5, 1, tzinfo=UTC),
        validation_received_at_through=datetime(2026, 6, 9, tzinfo=UTC),
        test_received_at_from=datetime(2026, 6, 10, tzinfo=UTC),
        test_received_at_through=datetime(2026, 7, 19, tzinfo=UTC),
        labels_available_through=datetime(2026, 8, 1, tzinfo=UTC),
        vocabulary={"word:сбербанк": -20},
        intercept=0,
        validation=metrics,
        test=metrics,
    )
    return MlRouterRuntime(artifact=forced_reject, mode=mode)


def candidate_document() -> NewsDocument:
    return NewsDocument(
        source_id="interfax",
        external_id="ml-router-candidate",
        published_at=datetime(2026, 8, 16, 10, tzinfo=UTC),
        received_at=datetime(2026, 8, 16, 10, 1, tzinfo=UTC),
        title="Сбербанк рекомендовал дивиденды",
        url="https://example.com/ml-router-candidate",
        content="Совет директоров рекомендовал выплатить дивиденды.",
        language="ru",
        source_metadata={"categories": ["Компании"]},
        payload_hash="original-ml-router-candidate",
    )


def test_reprocess_prioritizes_material_event_without_changing_batch_limit() -> None:
    analyzer = AsyncMock()
    analyzer.extract.side_effect = RuleBasedNewsAnalyzer().extract
    repository = MemoryNewsRepository(analyzer=analyzer)
    standard = replace(
        candidate_document(),
        external_id="newer-management",
        published_at=datetime(2026, 8, 16, 11, tzinfo=UTC),
        received_at=datetime(2026, 8, 16, 11, 1, tzinfo=UTC),
        title="Сбербанк назначил нового руководителя направления",
        content="Компания сообщила о кадровом назначении.",
        payload_hash="newer-management",
    )
    material = replace(
        candidate_document(),
        external_id="older-dividend",
        published_at=datetime(2026, 8, 16, 10, tzinfo=UTC),
        received_at=datetime(2026, 8, 16, 10, 1, tzinfo=UTC),
        title="Лукойл рекомендовал дивиденды",
        content="Совет директоров рекомендовал выплатить 500 рублей на акцию.",
        payload_hash="older-dividend",
    )

    async def scenario() -> tuple[dict[str, object], str, dict[str, object]]:
        await repository.ingest("standard-original", standard, generate_signals=False)
        await repository.ingest("material-original", material, generate_signals=False)
        news = await repository.list_news(source_id="interfax", limit=10)
        material_id = next(item.id for item in news if item.external_id == "older-dividend")
        result = await reprocess_signal_candidates_batch(repository, limit=1)
        updated = await repository.list_news(source_id="interfax", limit=10)
        standard_metadata = next(
            dict(item.source_metadata)
            for item in updated
            if item.external_id == "newer-management"
        )
        return result, material_id, standard_metadata

    result, material_id, standard_metadata = asyncio.run(scenario())

    assert result["meta"]["batch_limit"] == 1
    assert result["meta"]["selected"] == 1
    assert result["meta"]["selected_priority_counts"] == {"3": 1}
    assert result["data"][0]["news_id"] == material_id
    assert analyzer.extract.await_count == 1
    assert analyzer.extract.await_args.args[0].title == "Лукойл рекомендовал дивиденды"
    assert "reprocess_version" not in standard_metadata
    assert "signal_outcome" not in standard_metadata


def test_reprocess_excludes_analysis_only_sources_and_multi_company_roundups() -> None:
    analyzer = AsyncMock(side_effect=AssertionError("excluded candidate reached analyzer"))
    repository = MemoryNewsRepository(analyzer=analyzer)
    analysis = replace(
        candidate_document(),
        source_id="telegram_mozgovikresearch",
        external_id="analysis-only-report",
        title="ФосАгро 1П26: почему EBITDA упала в 2 раза?",
        content="Частный аналитический разбор отчетности.",
        payload_hash="analysis-only-report",
    )
    roundup = replace(
        candidate_document(),
        external_id="multi-company-roundup",
        title=(
            "На этой неделе дивиденды рекомендовали сразу пять компаний — "
            "НОВАТЭК, Норникель и Лукойл"
        ),
        content="Сводка решений советов директоров за неделю.",
        payload_hash="multi-company-roundup",
    )

    async def scenario() -> dict[str, object]:
        await repository.ingest("analysis-original", analysis, generate_signals=False)
        await repository.ingest("roundup-original", roundup, generate_signals=False)
        return await reprocess_signal_candidates_batch(repository, limit=10, dry_run=True)

    result = asyncio.run(scenario())

    analyzer.assert_not_awaited()
    assert result["data"] == []
    assert result["meta"]["selected"] == 0
    assert result["meta"]["estimated_llm_calls"] == 0


def test_live_views_hide_legacy_signal_from_newly_excluded_evidence() -> None:
    repository = MemoryNewsRepository()
    analysis = replace(
        candidate_document(),
        source_id="telegram_finamalert",
        external_id="legacy-analysis-signal",
        source_metadata={"categories": ["Компании"], "event_candidate": True},
        payload_hash="legacy-analysis-signal",
    )

    async def scenario() -> tuple[list[object], list[object]]:
        await repository.ingest("legacy-analysis", analysis, generate_signals=True)
        return (
            await repository.list_news(source_id=None, limit=10),
            await repository.list_signals(
                ticker=None,
                directions=None,
                status=None,
                min_confidence=None,
                limit=10,
            ),
        )

    news, signals = asyncio.run(scenario())

    assert len(signals) == 1
    assert active_signals_from_content(news, signals) == {}
    payload = build_news_response_payload(news, signals, source_id=None, scope=None, limit=10)
    assert len(payload["data"]) == 1
    assert payload["data"][0]["related_signals"] == []


def test_shadow_records_decision_but_still_calls_downstream_analyzer() -> None:
    analyzer = AsyncMock()
    from eventedge.llm import RuleBasedNewsAnalyzer

    analyzer.extract.side_effect = RuleBasedNewsAnalyzer().extract
    repository = MemoryNewsRepository(analyzer=analyzer)

    async def scenario() -> tuple[dict[str, object], dict[str, object]]:
        await repository.ingest("original", candidate_document(), generate_signals=False)
        result = await reprocess_signal_candidates_batch(
            repository,
            limit=1,
            ml_router=rejecting_runtime(MlRouterMode.SHADOW),
        )
        news = (await repository.list_news(source_id="interfax", limit=1))[0]
        return result, news.source_metadata

    result, metadata = asyncio.run(scenario())

    analyzer.extract.assert_awaited_once()
    assert result["data"][0]["ml_router_action"] == "reject"
    assert metadata["ml_router"]["mode"] == "shadow"
    assert metadata["analysis_candidate"] is True


def test_enforced_rejection_skips_analyzer_and_is_not_reprocessed_again() -> None:
    analyzer = AsyncMock()
    analyzer.extract.side_effect = AssertionError("rejected item reached the analyzer")
    repository = MemoryNewsRepository(analyzer=analyzer)

    async def scenario() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        await repository.ingest("original", candidate_document(), generate_signals=False)
        first = await reprocess_signal_candidates_batch(
            repository,
            limit=1,
            ml_router=rejecting_runtime(MlRouterMode.ENFORCE),
        )
        second = await reprocess_signal_candidates_batch(
            repository,
            limit=1,
            ml_router=rejecting_runtime(MlRouterMode.ENFORCE),
        )
        news = (await repository.list_news(source_id="interfax", limit=1))[0]
        return first, second, news.source_metadata

    first, second, metadata = asyncio.run(scenario())

    analyzer.extract.assert_not_awaited()
    assert first["meta"]["completed"] == 1
    assert second["meta"]["selected"] == 0
    assert metadata["classification_status"] == "ml_rejected"
    assert metadata["analysis_candidate"] is False
    assert metadata["reprocess_version"] == "signal-engine-0.6.1"


def test_artifact_json_rejects_unknown_fields() -> None:
    payload = artifact().model_dump(mode="json")
    payload["future_outcome"] = "not allowed"
    with pytest.raises(ValidationError):
        MlRouterArtifact.model_validate_json(json.dumps(payload))


def test_training_cli_writes_non_deployable_synthetic_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dataset = tmp_path / "quality.jsonl"
    output = tmp_path / "router.json"
    rows = []
    for index in range(30):
        relevant = index % 2 == 0
        published_at = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)
        received_at = published_at + timedelta(minutes=1)
        rows.append(
            {
                "id": f"quality-{index}",
                "event_id": f"event-{index}",
                "published_at": published_at.isoformat(),
                "received_at": received_at.isoformat(),
                "source_id": "fixture",
                "title": (
                    "Сбербанк рекомендовал дивиденды подтверждено"
                    if relevant
                    else "Сбербанк рекомендовал дивиденды интервью"
                ),
                "content": "Компания раскрыла важные новости.",
                "categories": ["Компании"],
                "labels": {
                    "relevant": relevant,
                    "scope": "company" if relevant else "none",
                    "target_ids": ["SBER"] if relevant else [],
                },
                "label_source": "synthetic_test",
                "labeler": "test-suite",
                "labeled_at": (received_at + timedelta(minutes=1)).isoformat(),
            }
        )
    dataset.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in rows),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "train_ml_router",
            "--dataset",
            str(dataset),
            "--output",
            str(output),
            "--allow-synthetic",
            "--trained-at",
            "2026-08-16T00:00:00+00:00",
        ],
    )

    train_ml_router_main()

    report = json.loads(capsys.readouterr().out)
    saved = MlRouterArtifact.model_validate_json(output.read_text(encoding="utf-8"))
    assert report["production_gate"]["passed"] is False
    assert saved.label_source == "synthetic_test"
    assert saved.training_examples == 18
