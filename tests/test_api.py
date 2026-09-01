import asyncio
import time
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

import eventedge.main as main_module
from eventedge.evals import EVALUATION_METHODOLOGY_VERSION
from eventedge.main import app
from eventedge.storage import (
    EvaluationEpochRecord,
    MemoryNewsRepository,
    NewsDocument,
    NewsRecord,
    SignalRecord,
    TelegramSourceRecord,
    to_rfc3339,
)

client = TestClient(app)

NEWS_PAYLOAD = {
    "source_id": "interfax",
    "external_id": "news-2026-08-08-001",
    "published_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "title": "Сбербанк опубликовал результаты за семь месяцев",
    "url": "https://example.com/news/001",
    "content": "Чистая прибыль выросла быстрее рыночного консенсуса.",
    "language": "ru",
    "source_metadata": {"section": "companies"},
}


def test_liveness_is_public_traceable_and_revisioned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_REVISION", "a" * 40)
    response = client.get("/health/live", headers={"X-Request-Id": "test-request-123"})

    assert response.status_code == 200
    assert response.headers["X-Request-Id"] == "test-request-123"
    assert response.json()["status"] == "ok"
    assert response.json()["checked_at"].endswith("Z")
    assert response.json()["revision"] == "a" * 40


def test_invalid_request_id_is_replaced() -> None:
    response = client.get("/health/ready", headers={"X-Request-Id": "bad"})

    assert response.status_code == 200
    assert response.headers["X-Request-Id"].startswith("req_")


def test_readiness_reuses_recent_repository_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = AsyncMock(side_effect=AssertionError("readiness probe should use recent success"))
    monkeypatch.setattr(main_module, "RECENT_REPOSITORY_SUCCESS_TTL_SECONDS", 120)
    monkeypatch.setattr(app.state.news_repository, "ready", ready)
    app.state.repository_last_success_at = time.monotonic()

    response = client.get("/health/ready")

    assert response.status_code == 200
    ready.assert_not_awaited()


def test_worker_startup_skips_public_read_model_prewarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    application = SimpleNamespace(
        state=SimpleNamespace(
            news_repository=repository,
            repository_last_success_at=None,
            content_snapshot_inflight=None,
        )
    )
    prewarm = AsyncMock()
    monkeypatch.setenv("EVENTEDGE_COMPONENT", "worker")
    monkeypatch.setattr(main_module, "CONTENT_SNAPSHOT_TTL_SECONDS", 60)
    monkeypatch.setattr(main_module, "refresh_content_snapshot_in_background", prewarm)

    async def scenario() -> None:
        async with main_module.lifespan(application):
            pass

    asyncio.run(scenario())

    repository.start.assert_awaited_once()
    repository.stop.assert_awaited_once()
    prewarm.assert_not_awaited()


def test_api_startup_does_not_block_on_public_read_model_prewarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    application = SimpleNamespace(
        state=SimpleNamespace(
            news_repository=repository,
            repository_last_success_at=None,
            content_snapshot_inflight=None,
            signal_feed_inflight=None,
        )
    )
    prewarm = AsyncMock(side_effect=AssertionError("startup must not build a broad snapshot"))
    monkeypatch.setenv("EVENTEDGE_COMPONENT", "api")
    monkeypatch.setattr(main_module, "CONTENT_SNAPSHOT_TTL_SECONDS", 60)
    monkeypatch.setattr(main_module, "refresh_content_snapshot_in_background", prewarm)

    async def scenario() -> None:
        async with main_module.lifespan(application):
            pass

    asyncio.run(scenario())

    repository.start.assert_awaited_once()
    repository.stop.assert_awaited_once()
    prewarm.assert_not_awaited()


def test_expired_content_snapshot_is_served_while_refresh_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "CONTENT_SNAPSHOT_TTL_SECONDS", 60)

    async def scenario() -> tuple[list[object], list[object], AsyncMock, AsyncMock]:
        news = [object()]
        signals = [object()]
        list_news = AsyncMock()
        list_signals = AsyncMock()
        repository = SimpleNamespace(list_news=list_news, list_signals=list_signals)
        application = SimpleNamespace(
            state=SimpleNamespace(
                news_repository=repository,
                content_snapshot_cache={
                    "repository": repository,
                    "expires_at": time.monotonic() - 1,
                    "news": news,
                    "signals": signals,
                },
                content_snapshot_lock=asyncio.Lock(),
                content_snapshot_inflight=None,
                repository_last_success_at=None,
            )
        )

        result = await main_module.load_content_snapshot(SimpleNamespace(app=application))
        refresh = application.state.content_snapshot_inflight
        assert refresh is not None
        refresh.cancel()
        with suppress(asyncio.CancelledError):
            await refresh
        return (*result, list_news, list_signals)

    news, signals, list_news, list_signals = asyncio.run(scenario())

    assert len(news) == 1
    assert len(signals) == 1
    list_news.assert_not_awaited()
    list_signals.assert_not_awaited()


def test_signal_feed_cache_reuses_complete_repository_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "SIGNAL_FEED_TTL_SECONDS", 30)

    async def scenario() -> tuple[list[SignalRecord], AsyncMock, AsyncMock]:
        repository = MemoryNewsRepository()
        timestamp = datetime.now(UTC).replace(microsecond=0)
        await repository.ingest(
            "cached-signal-feed-key",
            NewsDocument(
                source_id="interfax",
                external_id="cached-signal-feed",
                published_at=timestamp,
                received_at=timestamp,
                title="Сбербанк опубликовал сильную отчётность",
                url="https://example.com/cached-signal-feed",
                content="Чистая прибыль выросла на 20% и превысила ожидания.",
                language="ru",
                source_metadata={"signal_candidate": True},
                payload_hash="cached-signal-feed-payload",
            ),
        )
        list_signals = AsyncMock(wraps=repository.list_signals)
        get_news_by_ids = AsyncMock(wraps=repository.get_news_by_ids)
        repository.list_signals = list_signals
        repository.get_news_by_ids = get_news_by_ids
        application = SimpleNamespace(
            state=SimpleNamespace(
                news_repository=repository,
                repository_last_success_at=None,
                signal_feed_cache=None,
                signal_feed_lock=asyncio.Lock(),
                signal_feed_inflight=None,
            )
        )
        request = SimpleNamespace(app=application)

        first = await main_module.load_signal_feed(request)
        second = await main_module.load_signal_feed(request)

        assert first is not second
        assert first[0] is second[0]
        assert first[1] is second[1]
        return first[1], list_signals, get_news_by_ids

    signals, list_signals, get_news_by_ids = asyncio.run(scenario())

    assert len(signals) == 1
    list_signals.assert_awaited_once()
    get_news_by_ids.assert_awaited_once()


def test_expired_signal_feed_is_served_while_refresh_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "SIGNAL_FEED_TTL_SECONDS", 30)
    news = [object()]
    signals = [object()]
    refresh = AsyncMock(return_value=(news, signals))
    monkeypatch.setattr(main_module, "refresh_signal_feed", refresh)

    async def scenario() -> tuple[list[object], list[object]]:
        repository = object()
        application = SimpleNamespace(
            state=SimpleNamespace(
                news_repository=repository,
                signal_feed_cache={
                    "repository": repository,
                    "expires_at": time.monotonic() - 1,
                    "news": news,
                    "signals": signals,
                },
                signal_feed_inflight=None,
            )
        )
        result = await main_module.load_signal_feed(SimpleNamespace(app=application))
        await application.state.signal_feed_inflight
        return result

    result_news, result_signals = asyncio.run(scenario())

    assert result_news is news
    assert result_signals is signals
    refresh.assert_awaited_once()


def test_empty_signal_refresh_preserves_nonempty_last_known_good(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "SIGNAL_FEED_TTL_SECONDS", 30)

    async def scenario() -> tuple[list[object], list[object], float]:
        repository = SimpleNamespace(
            list_signals=AsyncMock(return_value=[]),
            get_news_by_ids=AsyncMock(return_value=[]),
        )
        news = [object()]
        signals = [object()]
        application = SimpleNamespace(
            state=SimpleNamespace(
                news_repository=repository,
                repository_last_success_at=None,
                signal_feed_cache={
                    "repository": repository,
                    "expires_at": time.monotonic() - 1,
                    "news": news,
                    "signals": signals,
                },
                signal_feed_lock=asyncio.Lock(),
            )
        )

        result_news, result_signals = await main_module.refresh_signal_feed(application)
        return result_news, result_signals, application.state.signal_feed_cache["expires_at"]

    result_news, result_signals, expires_at = asyncio.run(scenario())

    assert len(result_news) == 1
    assert len(result_signals) == 1
    assert expires_at > time.monotonic()


def test_failed_background_signal_refresh_applies_retry_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = object()
    cache = {
        "repository": repository,
        "expires_at": time.monotonic() - 1,
        "news": [object()],
        "signals": [object()],
    }
    application = SimpleNamespace(
        state=SimpleNamespace(
            news_repository=repository,
            signal_feed_cache=cache,
            signal_feed_inflight=None,
        )
    )
    refresh = AsyncMock(side_effect=TimeoutError)
    monkeypatch.setattr(main_module, "refresh_signal_feed", refresh)

    async def scenario() -> None:
        application.state.signal_feed_inflight = asyncio.current_task()
        await main_module.refresh_signal_feed_in_background(application)

    asyncio.run(scenario())

    assert cache["expires_at"] > time.monotonic()
    assert application.state.signal_feed_inflight is None


def test_news_response_cache_reuses_only_the_same_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "CONTENT_SNAPSHOT_TTL_SECONDS", 60)
    application = SimpleNamespace(
        state=SimpleNamespace(
            news_response_cache=None,
            content_snapshot_inflight=None,
        )
    )
    first_news: list[object] = []
    first_signals: list[object] = []
    second_news: list[object] = []
    key = (None, None, 5)
    payload = {"data": [], "meta": {"limit": 5}}

    main_module.store_news_response(application, first_news, first_signals, key, payload)

    assert main_module.cached_news_response(application, first_news, first_signals, key) is payload
    assert main_module.cached_news_response(application, second_news, first_signals, key) is None


def test_event_projection_is_built_once_for_concurrent_snapshot_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "CONTENT_SNAPSHOT_TTL_SECONDS", 60)
    news: list[NewsRecord] = []
    signals: list[SignalRecord] = []
    events = [{"id": "evt_00000000000000000000000000"}]
    build = Mock(return_value=events)
    monkeypatch.setattr(main_module, "build_market_event_records", build)
    application = SimpleNamespace(
        state=SimpleNamespace(
            event_response_cache=None,
            event_response_lock=asyncio.Lock(),
        )
    )

    async def scenario() -> list[list[dict[str, object]]]:
        return await asyncio.gather(
            main_module.get_or_build_market_event_records(application, news, signals),
            main_module.get_or_build_market_event_records(application, news, signals),
        )

    results = asyncio.run(scenario())

    assert results == [events, events]
    build.assert_called_once_with(news, signals)


def test_news_response_cache_serves_stale_payload_during_projection_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "CONTENT_SNAPSHOT_TTL_SECONDS", 60)
    old_news: list[object] = []
    old_signals: list[object] = []
    new_news: list[object] = []
    new_signals: list[object] = []
    key = (None, None, 20)
    payload = {"data": [], "meta": {"limit": 20}}
    inflight = SimpleNamespace(done=lambda: False)
    application = SimpleNamespace(
        state=SimpleNamespace(
            news_response_cache={
                "news": old_news,
                "signals": old_signals,
                "responses": {key: payload},
            },
            content_snapshot_inflight=inflight,
        )
    )

    result = main_module.cached_news_response(application, new_news, new_signals, key)

    assert result is payload
    assert application.state.news_response_cache["news"] is old_news


def test_background_snapshot_refresh_precomputes_default_news_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "CONTENT_SNAPSHOT_TTL_SECONDS", 60)
    news: list[NewsRecord] = []
    signals: list[SignalRecord] = []
    payload = {"data": [], "meta": {"limit": 500}}
    refresh = AsyncMock(return_value=(news, signals))
    monkeypatch.setattr(main_module, "refresh_content_snapshot", refresh)
    monkeypatch.setattr(main_module, "build_news_response_payload", lambda *args, **kwargs: payload)
    application = SimpleNamespace(
        state=SimpleNamespace(
            content_snapshot_inflight=None,
            news_response_cache=None,
        )
    )

    async def scenario() -> None:
        application.state.content_snapshot_inflight = asyncio.current_task()
        await main_module.refresh_content_snapshot_in_background(application)

    asyncio.run(scenario())

    assert application.state.news_response_cache["responses"][(None, None, 500)] is payload
    assert application.state.content_snapshot_inflight is None


def test_news_response_cache_derives_limits_from_canonical_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "CONTENT_SNAPSHOT_TTL_SECONDS", 60)
    news: list[object] = []
    signals: list[object] = []
    canonical = {
        "data": [{"id": index} for index in range(500)],
        "meta": {"limit": 500, "total": 994, "has_more": True, "next_cursor": None},
    }
    responses = {(None, None, 500): canonical}
    application = SimpleNamespace(
        state=SimpleNamespace(
            news_response_cache={"news": news, "signals": signals, "responses": responses},
            content_snapshot_inflight=None,
        )
    )

    result = main_module.cached_news_response(application, news, signals, (None, None, 100))

    assert result is not None
    assert len(result["data"]) == 100
    assert result["meta"]["limit"] == 100
    assert result["meta"]["total"] == 994
    assert result["meta"]["has_more"] is True
    assert responses[(None, None, 100)] is result


def test_concurrent_news_projection_is_single_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module, "CONTENT_SNAPSHOT_TTL_SECONDS", 60)
    news: list[NewsRecord] = []
    signals: list[SignalRecord] = []
    payload = {"data": [], "meta": {"limit": 100}}
    build_calls = 0

    def build(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal build_calls
        build_calls += 1
        time.sleep(0.02)
        return payload

    monkeypatch.setattr(main_module, "build_news_response_payload", build)
    application = SimpleNamespace(
        state=SimpleNamespace(
            news_response_cache=None,
            news_response_inflight={},
            content_snapshot_inflight=None,
            content_snapshot_cache={"news": news, "signals": signals},
        )
    )

    async def scenario() -> list[dict[str, object]]:
        return await asyncio.gather(
            *(
                main_module.get_or_build_news_response(
                    application,
                    news,
                    signals,
                    source_id=None,
                    scope=None,
                    limit=100,
                )
                for _ in range(10)
            )
        )

    results = asyncio.run(scenario())

    assert results == [payload] * 10
    assert build_calls == 1
    assert application.state.news_response_inflight == {}


def test_source_registry_and_protected_telegram_addition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = client.get("/v1/sources")

    assert registry.status_code == 200
    assert registry.json()["meta"]["telegram_limit"] == 18
    assert registry.json()["meta"]["telegram_active"] >= 6
    assert registry.json()["meta"]["registry_status"] in {"live", "cached"}
    assert registry.json()["meta"]["observation_status"] in {
        "live",
        "cached",
        "shared_snapshot",
    }
    assert registry.json()["meta"]["freshness_basis"] == "latest_stored_publication"
    assert any(source["source_id"] == "telegram_bcs_express" for source in registry.json()["data"])
    assert all("freshness_status" in source for source in registry.json()["data"])

    unavailable = client.post(
        "/v1/sources/telegram",
        json={"channel": "eventedge_test_one"},
    )
    assert unavailable.status_code == 503

    monkeypatch.setenv("EVENTEDGE_ADMIN_KEY", "test-admin-key")
    unauthorized = client.post(
        "/v1/sources/telegram",
        json={"channel": "eventedge_test_one"},
        headers={"X-EventEdge-Admin-Key": "wrong-key"},
    )
    assert unauthorized.status_code == 401

    created = client.post(
        "/v1/sources/telegram",
        json={
            "channel": "eventedge_test_one",
            "display_name": "EventEdge test",
            "description": "Тестовый источник событий российского рынка.",
        },
        headers={"X-EventEdge-Admin-Key": "test-admin-key"},
    )
    assert created.status_code == 201
    assert created.json()["data"]["source_id"] == "telegram_eventedge_test_one"
    assert created.json()["data"]["managed"] is True
    assert created.json()["data"]["role"] == "Тестовый источник событий российского рынка."


def test_source_registry_timeout_serves_static_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = MemoryNewsRepository()
    original_repository = app.state.news_repository
    original_cache = app.state.source_registry_cache

    async def slow_registry() -> list[TelegramSourceRecord]:
        await asyncio.sleep(60)
        return []

    monkeypatch.setattr(repository, "list_telegram_sources", slow_registry)
    monkeypatch.setattr(main_module, "SOURCE_REGISTRY_TIMEOUT_SECONDS", 0.01)
    app.state.news_repository = repository
    app.state.source_registry_cache = None
    try:
        response = client.get("/v1/sources")
    finally:
        app.state.news_repository = original_repository
        app.state.source_registry_cache = original_cache

    assert response.status_code == 200
    assert response.json()["meta"]["registry_status"] == "unavailable"
    assert any(source["source_id"] == "interfax" for source in response.json()["data"])


def test_source_registry_reports_actual_freshness_and_delivery_lag() -> None:
    repository = MemoryNewsRepository()
    observed_at = datetime.now(UTC).replace(microsecond=0)
    fresh_received_at = observed_at - timedelta(seconds=30)
    delayed_received_at = observed_at - timedelta(minutes=10)
    for idempotency_key, document in (
        (
            "source-fresh-interfax",
            NewsDocument(
                source_id="interfax",
                external_id="source-fresh-interfax",
                published_at=fresh_received_at - timedelta(seconds=15),
                received_at=fresh_received_at,
                title="Сбербанк сообщил о решении совета директоров",
                url="https://example.com/source-fresh-interfax",
                content="Совет директоров рассмотрел вопрос о дивидендах.",
                language="ru",
                source_metadata={},
                payload_hash="source-fresh-interfax",
            ),
        ),
        (
            "source-delayed-tass",
            NewsDocument(
                source_id="tass",
                external_id="source-delayed-tass",
                published_at=delayed_received_at - timedelta(seconds=20),
                received_at=delayed_received_at,
                title="Компания раскрыла операционные результаты",
                url="https://example.com/source-delayed-tass",
                content="Опубликованы операционные результаты за период.",
                language="ru",
                source_metadata={},
                payload_hash="source-delayed-tass",
            ),
        ),
        (
            "source-fresh-moex-non-equity",
            NewsDocument(
                source_id="moex_news",
                external_id="source-fresh-moex-non-equity",
                published_at=fresh_received_at - timedelta(seconds=10),
                received_at=fresh_received_at,
                title="Московская биржа сообщила об инфраструктурном обновлении",
                url="https://example.com/source-fresh-moex-non-equity",
                content="Обновление инфраструктуры не относится к отдельной акции.",
                language="ru",
                source_metadata={},
                payload_hash="source-fresh-moex-non-equity",
            ),
        ),
    ):
        asyncio.run(repository.ingest(idempotency_key, document, generate_signals=False))

    original_repository = app.state.news_repository
    original_content_cache = app.state.content_snapshot_cache
    original_observation_cache = app.state.source_observation_cache
    original_registry_cache = app.state.source_registry_cache
    app.state.news_repository = repository
    app.state.content_snapshot_cache = None
    app.state.source_observation_cache = None
    app.state.source_registry_cache = None
    try:
        response = client.get("/v1/sources")
    finally:
        app.state.news_repository = original_repository
        app.state.content_snapshot_cache = original_content_cache
        app.state.source_observation_cache = original_observation_cache
        app.state.source_registry_cache = original_registry_cache

    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"]["observation_status"] == "live"
    assert payload["meta"]["observed_news"] == 3
    sources = {source["source_id"]: source for source in payload["data"]}
    assert sources["interfax"]["count"] == 1
    assert sources["interfax"]["last_received_at"] is not None
    assert sources["interfax"]["latest_delivery_lag_seconds"] == 15
    assert sources["interfax"]["collection_lane"] == "fast"
    assert sources["interfax"]["poll_interval_seconds"] == 60
    assert sources["interfax"]["freshness_threshold_seconds"] == 180
    assert sources["interfax"]["freshness_status"] == "fresh"
    assert sources["tass"]["freshness_status"] == "delayed"
    assert sources["moex_news"]["count"] == 1
    assert sources["moex_news"]["freshness_status"] == "fresh"
    assert sources["cbr_press"]["freshness_status"] == "no_data"
    assert sources["moex_iss"]["freshness_status"] == "not_applicable"


def test_source_registry_does_not_turn_observation_timeout_into_false_no_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = MemoryNewsRepository()

    async def slow_news(*, source_id: str | None, limit: int) -> list[NewsRecord]:
        del source_id, limit
        await asyncio.sleep(60)
        return []

    monkeypatch.setattr(repository, "list_news", slow_news)
    monkeypatch.setattr(main_module, "SOURCE_OBSERVATION_TIMEOUT_SECONDS", 0.01)
    original_repository = app.state.news_repository
    original_content_cache = app.state.content_snapshot_cache
    original_observation_cache = app.state.source_observation_cache
    original_registry_cache = app.state.source_registry_cache
    app.state.news_repository = repository
    app.state.content_snapshot_cache = None
    app.state.source_observation_cache = None
    app.state.source_registry_cache = None
    try:
        response = client.get("/v1/sources")
    finally:
        app.state.news_repository = original_repository
        app.state.content_snapshot_cache = original_content_cache
        app.state.source_observation_cache = original_observation_cache
        app.state.source_registry_cache = original_registry_cache

    assert response.status_code == 200
    assert response.json()["meta"]["observation_status"] == "unavailable"
    sources = {source["source_id"]: source for source in response.json()["data"]}
    assert sources["interfax"]["freshness_status"] == "unknown"
    assert sources["moex_iss"]["freshness_status"] == "not_applicable"


def test_admin_reprocesses_one_explicit_stored_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVENTEDGE_ADMIN_KEY", "test-admin-key")
    original_repository = app.state.news_repository
    app.state.news_repository = MemoryNewsRepository()
    try:
        with TestClient(app) as isolated_client:
            repository = app.state.news_repository
            document = NewsDocument(
                source_id="interfax",
                external_id="reprocess-sber-1",
                published_at=datetime(2026, 8, 9, 10, tzinfo=UTC),
                received_at=datetime(2026, 8, 9, 10, 1, tzinfo=UTC),
                title="Сбербанк рекомендовал дивиденды за полугодие",
                url="https://example.com/reprocess-sber-1",
                content="Совет директоров рекомендовал выплатить 20 рублей на акцию.",
                language="ru",
                source_metadata={"signal_candidate": False, "tickers": []},
                payload_hash="reprocess-original-payload",
            )
            asyncio.run(
                repository.ingest(
                    "reprocess-original-key",
                    document,
                    generate_signals=False,
                )
            )
            stored = asyncio.run(repository.list_news(source_id="interfax", limit=10))[0]

            response = isolated_client.post(
                "/v1/admin/signals/reprocess",
                json={"limit": 1, "news_ids": [stored.id], "dry_run": False},
                headers={"X-EventEdge-Admin-Key": "test-admin-key"},
            )
            updated = asyncio.run(repository.list_news(source_id="interfax", limit=10))[0]
    finally:
        app.state.news_repository = original_repository
        app.state.evaluation_material_cache = None

    assert response.status_code == 200
    assert response.json()["meta"]["completed"] == 1
    assert response.json()["meta"]["batch_limit"] == 1
    assert response.json()["meta"]["model_version"] == "signal-engine-0.6.1"
    assert response.json()["data"][0]["news_id"] == stored.id
    assert response.json()["data"][0]["result_ref"].startswith("sig_")
    assert updated.source_metadata["reprocess_version"] == "signal-engine-0.6.1"


def test_admin_reprocess_defaults_to_a_free_non_mutating_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVENTEDGE_ADMIN_KEY", "test-admin-key")
    original_repository = app.state.news_repository
    app.state.news_repository = MemoryNewsRepository()
    try:
        with TestClient(app) as isolated_client:
            repository = app.state.news_repository
            document = NewsDocument(
                source_id="interfax",
                external_id="preview-sber-1",
                published_at=datetime(2026, 8, 9, 10, tzinfo=UTC),
                received_at=datetime(2026, 8, 9, 10, 1, tzinfo=UTC),
                title="Сбербанк рекомендовал дивиденды за полугодие",
                url="https://example.com/preview-sber-1",
                content="Совет директоров рекомендовал выплатить 20 рублей на акцию.",
                language="ru",
                source_metadata={"signal_candidate": False, "tickers": []},
                payload_hash="preview-sber-original",
            )
            asyncio.run(repository.ingest("preview-sber-key", document, generate_signals=False))
            stored = asyncio.run(repository.list_news(source_id="interfax", limit=10))[0]

            response = isolated_client.post(
                "/v1/admin/signals/reprocess",
                json={"limit": 1, "news_ids": [stored.id], "tickers": ["sber", "SBER"]},
                headers={"X-EventEdge-Admin-Key": "test-admin-key"},
            )
            unchanged = asyncio.run(repository.list_news(source_id="interfax", limit=10))[0]
            signals = asyncio.run(
                repository.list_signals(
                    ticker=None,
                    directions=None,
                    status=None,
                    min_confidence=None,
                    limit=10,
                )
            )
    finally:
        app.state.news_repository = original_repository
        app.state.evaluation_material_cache = None

    assert response.status_code == 200
    assert response.json()["meta"] | {
        "mode": "dry_run",
        "mutations_performed": 0,
        "completed": 0,
        "requested_tickers": ["SBER"],
        "estimated_llm_calls": 1,
        "maximum_llm_calls": 2,
    } == response.json()["meta"]
    assert response.json()["data"][0] | {
        "news_id": stored.id,
        "detected_tickers": ["SBER"],
        "would_generate_signals": True,
    } == response.json()["data"][0]
    assert "reprocess_version" not in unchanged.source_metadata
    assert signals == []


def test_admin_reprocess_rejects_unknown_company_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVENTEDGE_ADMIN_KEY", "test-admin-key")

    response = client.post(
        "/v1/admin/signals/reprocess",
        json={"tickers": ["NOTREAL"]},
        headers={"X-EventEdge-Admin-Key": "test-admin-key"},
    )

    assert response.status_code == 400


def test_admin_reprocess_accepts_a_refreshed_copy_of_the_same_news(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source refresh may update canonical metadata after an earlier model pass."""
    monkeypatch.setenv("EVENTEDGE_ADMIN_KEY", "test-admin-key")
    original_repository = app.state.news_repository
    app.state.news_repository = MemoryNewsRepository()
    try:
        with TestClient(app) as isolated_client:
            repository = app.state.news_repository
            published_at = datetime(2026, 8, 9, 10, tzinfo=UTC)
            received_at = datetime(2026, 8, 9, 10, 1, tzinfo=UTC)
            original = NewsDocument(
                source_id="interfax",
                external_id="reprocess-sber-refreshed",
                published_at=published_at,
                received_at=received_at,
                title="В России обсудили нефтяной рынок",
                url="https://example.com/reprocess-sber-refreshed",
                content="Участники совещания обменялись мнениями.",
                language="ru",
                source_metadata={"signal_candidate": False, "tickers": []},
                payload_hash="reprocess-refreshed-original",
            )
            asyncio.run(repository.ingest("refresh-original", original, generate_signals=False))
            stored = asyncio.run(repository.list_news(source_id="interfax", limit=10))[0]

            first = isolated_client.post(
                "/v1/admin/signals/reprocess",
                json={"limit": 1, "news_ids": [stored.id], "dry_run": False},
                headers={"X-EventEdge-Admin-Key": "test-admin-key"},
            )
            refreshed = replace(
                original,
                content=f"{original.content} Решение принято единогласно.",
                source_metadata={"signal_candidate": False, "tickers": []},
                payload_hash="reprocess-refreshed-second-copy",
            )
            asyncio.run(repository.ingest("refresh-second-copy", refreshed, generate_signals=False))
            second = isolated_client.post(
                "/v1/admin/signals/reprocess",
                json={"limit": 1, "news_ids": [stored.id], "dry_run": False},
                headers={"X-EventEdge-Admin-Key": "test-admin-key"},
            )
            asyncio.run(repository.ingest("refresh-third-copy", original, generate_signals=False))
            replayed = isolated_client.post(
                "/v1/admin/signals/reprocess",
                json={"limit": 1, "news_ids": [stored.id], "dry_run": False},
                headers={"X-EventEdge-Admin-Key": "test-admin-key"},
            )
    finally:
        app.state.news_repository = original_repository
        app.state.evaluation_material_cache = None

    assert first.status_code == 200
    assert first.json()["meta"]["completed"] == 1
    assert second.status_code == 200
    assert second.json()["meta"]["completed"] == 1
    assert second.json()["meta"]["failed"] == 0
    assert second.json()["meta"]["mutations_performed"] == 1
    assert replayed.status_code == 200
    assert replayed.json()["meta"]["completed"] == 1
    assert replayed.json()["meta"]["mutations_performed"] == 0
    assert replayed.json()["data"][0]["replayed"] is True


def test_backfill_reclassifies_stored_sector_news_without_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVENTEDGE_ADMIN_KEY", "test-admin-key")
    original_repository = app.state.news_repository
    app.state.news_repository = MemoryNewsRepository()
    try:
        with TestClient(app) as isolated_client:
            repository = app.state.news_repository
            document = NewsDocument(
                source_id="telegram_bbbreaking",
                external_id="bbbreaking/235334",
                published_at=datetime(2026, 8, 10, 7, 10, tzinfo=UTC),
                received_at=datetime(2026, 8, 10, 7, 12, tzinfo=UTC),
                title="Правительство готовит поддержку железнодорожных перевозок",
                url="https://t.me/bbbreaking/235334",
                content=(
                    "Правительство выделит 10 млрд руб. на поддержку ж/д перевозок "
                    "экспортной сельхозпродукции."
                ),
                language="ru",
                source_metadata={"signal_candidate": False, "tickers": []},
                payload_hash="bbbreaking-235334-original",
            )
            asyncio.run(
                repository.ingest(
                    "bbbreaking-235334-original-key",
                    document,
                    generate_signals=False,
                )
            )
            stored = asyncio.run(
                repository.list_news(source_id="telegram_bbbreaking", limit=10)
            )[0]

            response = isolated_client.post(
                "/v1/admin/signals/reprocess",
                json={"limit": 1, "news_ids": [stored.id], "dry_run": False},
                headers={"X-EventEdge-Admin-Key": "test-admin-key"},
            )
            signals = asyncio.run(
                repository.list_signals(
                    ticker=None,
                    directions=None,
                    status=None,
                    min_confidence=None,
                    limit=10,
                )
            )
            updated = asyncio.run(
                repository.list_news(source_id="telegram_bbbreaking", limit=10)
            )[0]
    finally:
        app.state.news_repository = original_repository
        app.state.evaluation_material_cache = None

    assert response.status_code == 200
    assert response.json()["meta"]["candidate_policy"] == "material-event-priority-0.5.0"
    assert signals == []
    assert response.json()["meta"]["rejection_reasons"] == {"unvalidated_context_signal": 1}
    assert updated.source_metadata["classification_status"] == "semantic_candidate"
    assert updated.source_metadata["analysis_candidate"] is True


def test_timer_event_dispatches_private_collector() -> None:
    original = app.state.collectors["cbr_press"]

    async def fake_collector(repository: object) -> dict[str, int]:
        return {"fetched": 2, "accepted": 1, "replayed": 1}

    app.state.collectors["cbr_press"] = fake_collector
    try:
        response = client.post(
            "/",
            json={
                "messages": [
                    {
                        "event_metadata": {
                            "event_type": ("yandex.cloud.events.serverless.triggers.TimerMessage")
                        },
                        "details": {"payload": "cbr_press"},
                    }
                ]
            },
        )
    finally:
        app.state.collectors["cbr_press"] = original

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "collectors": {"cbr_press": {"fetched": 2, "accepted": 1, "replayed": 1}},
    }


def test_fast_news_timer_dispatches_minute_collector() -> None:
    original = app.state.collectors["fast_news"]

    async def fake_collector(repository: object) -> dict[str, int]:
        return {"fetched": 4, "accepted": 1, "replayed": 3, "failed": 0}

    app.state.collectors["fast_news"] = fake_collector
    try:
        response = client.post(
            "/",
            json={
                "messages": [
                    {
                        "event_metadata": {
                            "event_type": ("yandex.cloud.events.serverless.triggers.TimerMessage")
                        },
                        "details": {"payload": "fast_news"},
                    }
                ]
            },
        )
    finally:
        app.state.collectors["fast_news"] = original

    assert response.status_code == 200
    assert response.json()["collectors"]["fast_news"]["accepted"] == 1


def test_maintenance_timer_refreshes_models_and_persisted_evals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_reprocess(*args: object, **kwargs: object) -> dict[str, object]:
        return {
            "data": [{"news_id": "news_1"}],
            "meta": {"completed": 1, "remaining_candidates": 4},
        }

    async def fake_load(*args: object, **kwargs: object) -> tuple[object, ...]:
        return ([{"signal_id": "sig_1"}], [], {}, {}, [])

    monkeypatch.setattr(main_module, "reprocess_signal_candidates_batch", fake_reprocess)
    monkeypatch.setattr(main_module, "_load_evaluation_material", fake_load)

    response = client.post(
        "/",
        json={
            "messages": [
                {
                    "event_metadata": {
                        "event_type": "yandex.cloud.events.serverless.triggers.TimerMessage"
                    },
                    "details": {"payload": "maintenance"},
                }
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["collectors"]["maintenance"] == {
        "reprocessed": 1,
        "failed": 0,
        "failure_types": [],
        "remaining": 4,
        "rejection_reasons": {},
        "outcomes": 1,
        "epochs": 0,
    }


def test_maintenance_timer_defers_work_before_trigger_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eval_refreshed = False

    async def slow_reprocess(*args: object, **kwargs: object) -> dict[str, object]:
        await asyncio.sleep(60)
        raise AssertionError("maintenance should have been cancelled")

    async def fake_load(*args: object, **kwargs: object) -> tuple[object, ...]:
        nonlocal eval_refreshed
        eval_refreshed = True
        return ([{"signal_id": "sig_1"}], [], {}, {}, [])

    monkeypatch.setattr(main_module, "SIGNAL_REPROCESS_DEADLINE_SECONDS", 0.01)
    monkeypatch.setattr(main_module, "reprocess_signal_candidates_batch", slow_reprocess)
    monkeypatch.setattr(main_module, "_load_evaluation_material", fake_load)

    response = client.post(
        "/",
        json={
            "messages": [
                {
                    "event_metadata": {
                        "event_type": "yandex.cloud.events.serverless.triggers.TimerMessage"
                    },
                    "details": {"payload": "maintenance"},
                }
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["collectors"]["maintenance"] == {
        "outcomes": 1,
        "epochs": 0,
        "status": "partial",
        "deferred": ["signal_reprocessing"],
        "deadline_seconds": 60.0,
    }
    assert eval_refreshed is True


def test_maintenance_timer_reprocesses_when_eval_refresh_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_reprocess(*args: object, **kwargs: object) -> dict[str, object]:
        return {
            "data": [{"news_id": "news_1"}],
            "meta": {"completed": 1, "remaining_candidates": 4},
        }

    async def slow_load(*args: object, **kwargs: object) -> tuple[object, ...]:
        await asyncio.sleep(60)
        raise AssertionError("eval refresh should have been cancelled")

    monkeypatch.setattr(main_module, "EVALUATION_REFRESH_DEADLINE_SECONDS", 0.01)
    monkeypatch.setattr(main_module, "reprocess_signal_candidates_batch", fake_reprocess)
    monkeypatch.setattr(main_module, "_load_evaluation_material", slow_load)

    response = client.post(
        "/",
        json={
            "messages": [
                {
                    "event_metadata": {
                        "event_type": "yandex.cloud.events.serverless.triggers.TimerMessage"
                    },
                    "details": {"payload": "maintenance"},
                }
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["collectors"]["maintenance"] == {
        "reprocessed": 1,
        "failed": 0,
        "failure_types": [],
        "remaining": 4,
        "rejection_reasons": {},
        "status": "partial",
        "deferred": ["eval_refresh"],
        "deadline_seconds": 60.0,
    }


def test_signal_list_has_contract_shape_and_etag() -> None:
    response = client.get("/v1/signals", params={"ticker": "SBER", "limit": 10})

    assert response.status_code == 200
    assert response.json() == {
        "data": [],
        "meta": {
            "limit": 10,
            "has_more": False,
            "next_cursor": None,
            "model_scope": "news_event",
            "final_assessment_endpoint": "/v1/assessments",
            "final_assessment_model_version": "hybrid-market-0.2.1",
            "model_version": "signal-engine-0.6.1",
        },
    }
    assert response.headers["ETag"].startswith('"')

    cached = client.get(
        "/v1/signals",
        params={"ticker": "SBER", "limit": 10},
        headers={"If-None-Match": response.headers["ETag"]},
    )
    assert cached.status_code == 304
    assert cached.content == b""


def test_current_signal_uses_newest_event_before_direction_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = MemoryNewsRepository()
    now = datetime.now(UTC).replace(microsecond=0)

    def news(news_id: str, published_at: datetime, title: str) -> NewsRecord:
        return NewsRecord(
            id=news_id,
            source_id="interfax",
            external_id=news_id,
            published_at=published_at,
            received_at=published_at,
            title=title,
            url=f"https://example.com/{news_id}",
            content=title,
            language="ru",
            source_metadata={"analysis_candidate": True, "signal_candidate": True},
            created_at=published_at,
        )

    def signal(
        signal_id: str,
        item: NewsRecord,
        direction: str,
        score: float,
    ) -> SignalRecord:
        return SignalRecord(
            id=signal_id,
            news_id=item.id,
            ticker="SBER",
            as_of=item.published_at,
            data_cutoff_at=item.received_at,
            status="active",
            direction=direction,
            action="consider_buy" if direction == "up" else "no_action",
            horizon_value=3,
            horizon_unit="calendar_days",
            score=score,
            strength=abs(score) / 100,
            confidence=0.75,
            summary=f"Текущий {direction} news-сигнал.",
            factor_contributions=(),
            evidence_refs=(item.id,),
            expires_at=now + timedelta(days=3),
            invalidation_conditions=(),
            model_version="signal-engine-0.6.1",
            config_version=3,
            created_at=item.received_at,
        )

    older_news = news(
        "news_current_older",
        now - timedelta(hours=2),
        "Сбербанк увеличил чистую прибыль",
    )
    newer_news = news(
        "news_current_newer",
        now - timedelta(hours=1),
        "Сбербанк увеличил чистую прибыль без изменения прогноза",
    )
    older_up = signal("sig_current_older_up", older_news, "up", 42.0)
    newer_neutral = signal("sig_current_newer_neutral", newer_news, "neutral", 5.0)
    repository._news = {item.id: item for item in (older_news, newer_news)}
    repository._signals = {item.id: item for item in (older_up, newer_neutral)}
    monkeypatch.setattr(app.state, "news_repository", repository)

    class FakeMarketDataClient:
        async def snapshot(self, ticker: str) -> dict[str, object]:
            return {
                "ticker": ticker,
                "name": "Сбербанк",
                "last_price": "100.0",
                "currency": "RUB",
                "observed_at": to_rfc3339(now),
                "daily_change_pct": 0.2,
                "volume_shares": 1_000_000,
                "value_rub": 100_000_000.0,
                "lot_size": 1,
                "liquidity_status": "sufficient",
                "daily_volatility_pct": 1.0,
                "annualized_volatility_pct": 15.87,
                "candles": [],
                "source": {"name": "MOEX ISS", "url": "https://iss.moex.com/iss/"},
            }

    original_market_data_client = app.state.market_data_client
    app.state.market_data_client = FakeMarketDataClient()
    try:
        current = client.get("/v1/signals", params={"ticker": "SBER", "limit": 10})
        stale_direction = client.get(
            "/v1/signals",
            params={"ticker": "SBER", "direction": "up", "limit": 10},
        )
        neutral_direction = client.get(
            "/v1/signals",
            params={"ticker": "SBER", "direction": "neutral", "limit": 10},
        )
        history = client.get(
            "/v1/signals/history", params={"ticker": "SBER", "limit": 10}
        )
        assessment = client.get("/v1/assessments", params={"tickers": "SBER"})
    finally:
        app.state.market_data_client = original_market_data_client

    assert current.status_code == 200
    assert [item["id"] for item in current.json()["data"]] == [newer_neutral.id]
    assert stale_direction.status_code == 200
    assert stale_direction.json()["data"] == []
    assert neutral_direction.status_code == 200
    assert [item["id"] for item in neutral_direction.json()["data"]] == [
        newer_neutral.id
    ]
    assert history.status_code == 200
    assert [item["signal"]["id"] for item in history.json()["data"]] == [
        newer_neutral.id,
        older_up.id,
    ]
    assert assessment.status_code == 200
    assert assessment.json()["data"][0]["news_signal"]["id"] == newer_neutral.id
    assert assessment.json()["data"][0]["direction"] == "neutral"


def test_signal_history_compares_consecutive_stored_news_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = MemoryNewsRepository()
    now = datetime.now(UTC).replace(microsecond=0)

    def news(news_id: str, published_at: datetime, title: str) -> NewsRecord:
        return NewsRecord(
            id=news_id,
            source_id="interfax",
            external_id=news_id,
            published_at=published_at,
            received_at=published_at,
            title=title,
            url=f"https://example.com/{news_id}",
            content=title,
            language="ru",
            source_metadata={"signal_candidate": True},
            created_at=published_at,
        )

    first_news = news("news_history_first", now - timedelta(days=2), "Сбербанк сохранил прогноз")
    second_news = news("news_history_second", now - timedelta(days=1), "Прибыль Сбербанка выросла")

    def signal(
        signal_id: str,
        item: NewsRecord,
        direction: str,
        score: float,
        contribution: float,
    ) -> SignalRecord:
        return SignalRecord(
            id=signal_id,
            news_id=item.id,
            ticker="SBER",
            as_of=item.published_at,
            data_cutoff_at=item.received_at,
            status="active",
            direction=direction,
            action="consider_buy" if direction == "up" else "no_action",
            horizon_value=3,
            horizon_unit="calendar_days",
            score=score,
            strength=abs(score) / 100,
            confidence=0.72,
            summary=f"Сохранённый {direction} news-сигнал.",
            factor_contributions=(
                {
                    "code": "event_impact",
                    "label": "Влияние события",
                    "contribution": contribution,
                },
            ),
            evidence_refs=(item.id,),
            expires_at=item.published_at + timedelta(days=3),
            invalidation_conditions=(
                "Появилась новая существенная информация по компании.",
                "Истёк горизонт сигнала.",
            ),
            model_version="signal-engine-0.6.1",
            config_version=3,
            created_at=item.received_at,
        )

    first_signal = signal("sig_history_first", first_news, "neutral", 5.0, 0.05)
    second_signal = signal("sig_history_second", second_news, "up", 31.0, 0.31)
    repository._news = {item.id: item for item in (first_news, second_news)}
    repository._signals = {item.id: item for item in (first_signal, second_signal)}
    monkeypatch.setattr(app.state, "news_repository", repository)

    response = client.get("/v1/signals/history", params={"ticker": "SBER", "limit": 12})

    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"] == {
        "ticker": "SBER",
        "limit": 12,
        "has_more": False,
        "order": "as_of_desc",
        "basis": "chronological_news_signals",
        "model_version": "signal-engine-0.6.1",
    }
    assert [item["signal"]["id"] for item in payload["data"]] == [
        second_signal.id,
        first_signal.id,
    ]
    assert payload["data"][0]["change_from_previous"] == {
        "previous_signal_id": first_signal.id,
        "from_direction": "neutral",
        "to_direction": "up",
        "direction_changed": True,
        "score_delta": 26.0,
        "confidence_delta": 0.0,
        "primary_factor_change": {
            "code": "event_impact",
            "label": "Влияние события",
            "previous_contribution": 0.05,
            "current_contribution": 0.31,
            "delta": 0.26,
        },
        "factor_changes": [
            {
                "code": "event_impact",
                "label": "Влияние события",
                "previous_contribution": 0.05,
                "current_contribution": 0.31,
                "delta": 0.26,
            }
        ],
    }
    assert payload["data"][1]["change_from_previous"] is None
    assert payload["data"][0]["signal"]["invalidation_conditions"] == list(
        second_signal.invalidation_conditions
    )
    assert payload["data"][0]["signal"]["expires_at"] == to_rfc3339(
        second_signal.expires_at
    )
    assert payload["data"][0]["signal"]["evidence"][0]["id"] == second_news.id

    cached = client.get(
        "/v1/signals/history",
        params={"ticker": "SBER", "limit": 12},
        headers={"If-None-Match": response.headers["ETag"]},
    )
    assert cached.status_code == 304


def test_signal_history_requires_ticker_and_precedes_signal_id_route() -> None:
    response = client.get("/v1/signals/history")

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PARAMETER"


def test_signal_list_reads_repository_instead_of_stale_empty_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = MemoryNewsRepository()
    timestamp = datetime.now(UTC).replace(microsecond=0)
    document = NewsDocument(
        source_id="interfax",
        external_id="direct-signal-read",
        published_at=timestamp,
        received_at=timestamp,
        title="Сбербанк опубликовал сильную отчётность",
        url="https://example.com/direct-signal-read",
        content="Чистая прибыль выросла на 20% и оказалась выше ожиданий.",
        language="ru",
        source_metadata={"signal_candidate": True},
        payload_hash="direct-signal-read-payload",
    )
    result = asyncio.run(repository.ingest("direct-signal-read-key", document))
    monkeypatch.setattr(app.state, "news_repository", repository)
    monkeypatch.setattr(
        app.state,
        "content_snapshot_cache",
        {
            "repository": repository,
            "expires_at": time.monotonic() + 60,
            "news": [],
            "signals": [],
        },
    )
    snapshot_read = AsyncMock(side_effect=AssertionError("signal list must bypass snapshot"))
    monkeypatch.setattr(main_module, "load_content_snapshot", snapshot_read)

    response = client.get("/v1/signals", params={"ticker": "SBER"})

    assert response.status_code == 200
    payload = response.json()["data"]
    assert [item["id"] for item in payload] == [result.job.result_ref]
    assert payload[0]["evidence"] == [
        {
            "id": payload[0]["evidence_refs"][0],
            "source_id": "interfax",
            "title": document.title,
            "url": document.url,
            "published_at": timestamp.isoformat().replace("+00:00", "Z"),
            "received_at": timestamp.isoformat().replace("+00:00", "Z"),
        }
    ]
    assert payload[0]["provenance"] == {
        "method": "deterministic_news_event_scoring",
        "decision_at": payload[0]["created_at"],
        "data_cutoff_at": timestamp.isoformat().replace("+00:00", "Z"),
        "model_version": "signal-engine-0.6.1",
        "config_version": 6,
        "evidence_status": "complete",
        "evidence_expected": 1,
        "evidence_resolved": 1,
    }
    unresolved = main_module.signal_api_payload(
        repository._signals[result.job.result_ref],
        {},
    )
    assert unresolved["evidence"] == []
    assert unresolved["provenance"]["evidence_status"] == "missing"
    assert unresolved["provenance"]["evidence_expected"] == 1
    snapshot_read.assert_not_awaited()


def test_signal_list_hides_context_history_but_keeps_strong_company_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = MemoryNewsRepository()
    timestamp = datetime.now(UTC).replace(microsecond=0)
    news = NewsRecord(
        id="news_down_publication_gate",
        source_id="interfax",
        external_id="down-publication-gate",
        published_at=timestamp,
        received_at=timestamp,
        title="Санкции затронули компанию и рынок",
        url="https://example.com/down-publication-gate",
        content="Опубликованы новые существенные ограничения.",
        language="ru",
        source_metadata={"analysis_candidate": True, "signal_candidate": True},
        created_at=timestamp,
    )

    def signal(ticker: str) -> SignalRecord:
        return SignalRecord(
            id=f"sig_down_{ticker.lower()}",
            news_id=news.id,
            ticker=ticker,
            as_of=timestamp,
            data_cutoff_at=timestamp,
            status="active",
            direction="down",
            action="review_position" if ticker == "SBER" else "risk_off",
            horizon_value=3,
            horizon_unit="calendar_days",
            score=-45.0,
            strength=0.8,
            confidence=0.85,
            summary="Проверка защитного down-gate.",
            factor_contributions=(),
            evidence_refs=(news.id,),
            expires_at=timestamp + timedelta(days=3),
            invalidation_conditions=(),
            model_version="signal-engine-0.6.1",
            config_version=2,
            created_at=timestamp,
        )

    repository._news[news.id] = news
    repository._signals = {
        item.id: item
        for item in (
            signal("RUEQ"),
            signal("SBER"),
            replace(
                signal("RUEQ"),
                id="sig_expired_rueq",
                status="expired",
                expires_at=timestamp - timedelta(seconds=1),
            ),
        )
    }
    monkeypatch.setattr(app.state, "news_repository", repository)

    response = client.get("/v1/signals", params={"direction": "down", "limit": 100})

    assert response.status_code == 200
    assert [item["ticker"] for item in response.json()["data"]] == ["SBER"]

    expired_response = client.get("/v1/signals", params={"status": "expired", "limit": 100})

    assert expired_response.status_code == 200
    assert expired_response.json()["data"] == []


def test_signal_list_rejects_incomplete_repository_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = MemoryNewsRepository()
    timestamp = datetime.now(UTC).replace(microsecond=0)
    document = NewsDocument(
        source_id="interfax",
        external_id="incomplete-signal-read",
        published_at=timestamp,
        received_at=timestamp,
        title="Сбербанк опубликовал сильную отчётность",
        url="https://example.com/incomplete-signal-read",
        content="Чистая прибыль выросла на 20% и оказалась выше ожиданий.",
        language="ru",
        source_metadata={"signal_candidate": True},
        payload_hash="incomplete-signal-read-payload",
    )
    asyncio.run(repository.ingest("incomplete-signal-read-key", document))
    monkeypatch.setattr(repository, "get_news_by_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(app.state, "news_repository", repository)

    response = client.get("/v1/signals", params={"ticker": "SBER"})

    assert response.status_code == 503
    assert response.json()["code"] == "DEPENDENCY_UNAVAILABLE"
    assert response.headers["content-type"].startswith("application/problem+json")


def test_invalid_limit_uses_problem_json() -> None:
    response = client.get("/v1/signals", params={"limit": 101})

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "INVALID_PARAMETER"
    assert response.json()["request_id"] == response.headers["X-Request-Id"]


def test_unknown_signal_is_explicit() -> None:
    response = client.get("/v1/signals/sig_01JZK6K5GDX90Q2X8C0R4D7M9P")

    assert response.status_code == 404
    assert response.json()["code"] == "SIGNAL_NOT_FOUND"


def test_unknown_api_route_keeps_problem_json_contract() -> None:
    response = client.get("/v1/not-a-real-route")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"


def test_news_ingestion_is_idempotent_and_job_is_readable() -> None:
    headers = {"Idempotency-Key": "interfax-news-001"}

    accepted = client.post("/v1/internal/news", json=NEWS_PAYLOAD, headers=headers)
    replayed = client.post("/v1/internal/news", json=NEWS_PAYLOAD, headers=headers)

    assert accepted.status_code == 202
    assert replayed.status_code == 202
    assert accepted.json() == replayed.json()
    assert accepted.json()["data"]["status"] == "succeeded"
    assert accepted.json()["data"]["kind"] == "news_ingestion"
    assert accepted.json()["data"]["progress"] == 1
    assert accepted.json()["data"]["result_ref"].startswith("sig_")
    assert accepted.json()["data"]["completed_at"].endswith("Z")
    assert replayed.headers["Idempotency-Replayed"] == "true"
    assert accepted.headers["Location"] == f"/v1/jobs/{accepted.json()['data']['id']}"

    job = client.get(accepted.headers["Location"])
    assert job.status_code == 200
    assert job.json() == accepted.json()

    signal_id = accepted.json()["data"]["result_ref"]
    signal = client.get(f"/v1/signals/{signal_id}")
    assert signal.status_code == 200
    assert signal.json()["data"]["id"] == signal_id
    assert signal.json()["data"]["ticker"] == "SBER"
    assert signal.json()["data"]["direction"] == "up"
    assert signal.json()["data"]["action"] == "consider_buy"
    assert signal.json()["data"]["model_version"] == "signal-engine-0.6.1"
    assert len(signal.json()["data"]["factor_contributions"]) == 5

    listed = client.get("/v1/signals", params={"ticker": "SBER", "direction": "up"})
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["data"]] == [signal_id]

    news = client.get("/v1/news", params={"source_id": "interfax"})
    assert news.status_code == 200
    assert news.json()["meta"]["poll_interval_seconds"] == 60
    assert news.json()["meta"]["client_refresh_interval_seconds"] == 30
    assert news.json()["meta"]["delivery_target_seconds"] == 120
    assert news.json()["meta"]["excluded_irrelevant"] == 0
    assert news.json()["meta"]["processing_coverage"] == {
        "stored": 1,
        "relevant": 1,
        "analysis_candidates": 1,
        "signaled": 1,
        "candidate_coverage_pct": 100.0,
        "signal_yield_pct": 100.0,
        "rejection_reasons": {},
        "signal_model_version": "signal-engine-0.6.1",
    }
    company_coverage = news.json()["meta"]["company_coverage"]
    assert company_coverage["basis"] == "current_content_snapshot"
    assert company_coverage["window_news"] == 1
    assert company_coverage["supported"] == 20
    assert company_coverage["with_relevant_news"] == 1
    assert company_coverage["with_analysis_candidates"] == 1
    assert company_coverage["with_signal"] == 1
    sber_coverage = next(
        item for item in company_coverage["items"] if item["ticker"] == "SBER"
    )
    assert sber_coverage == {
        "ticker": "SBER",
        "relevant_news": 1,
        "analysis_candidates": 1,
        "signaled_news": 1,
        "last_published_at": NEWS_PAYLOAD["published_at"],
        "status": "signal_available",
    }
    assert sum(item["status"] == "no_relevant_news" for item in company_coverage["items"]) == 19
    assert news.json()["meta"]["collection_lanes"] == [
        {
            "id": "fast",
            "interval_seconds": 60,
            "source_ids": [
                "interfax",
                "tass",
                "rbc",
            ],
        },
        {
            "id": "discovery",
            "interval_seconds": 300,
            "source_ids": [
                "google_news",
                "market_background",
                "moex_news",
                "telegram_ak47pfl",
                "telegram_markettwits",
                "telegram_centralbank_russia",
                "telegram_moscowexchangeofficial",
                "telegram_bcs_express",
                "telegram_russianmacro",
            ],
        },
        {
            "id": "slow",
            "interval_seconds": 900,
            "source_ids": ["cbr_press"],
        },
    ]
    stored = next(
        item for item in news.json()["data"] if item["external_id"] == NEWS_PAYLOAD["external_id"]
    )
    assert stored["title"] == NEWS_PAYLOAD["title"]
    assert stored["content"] == NEWS_PAYLOAD["content"]
    assert stored["event"]["scope"] == "company"
    assert stored["event"]["tickers"] == ["SBER"]
    assert stored["related_signals"][0]["id"] == signal_id

    events = client.get("/v1/events", params={"scope": "company", "limit": 100})
    assert events.status_code == 200
    stored_event = next(
        event
        for event in events.json()["data"]
        if event["news_ids"] == [stored["id"]] and event["scope"] == "company"
    )
    assert stored_event["id"].startswith("evt_")
    assert stored_event["event_type"] == "financial_results"
    assert stored_event["evidence"][0]["news_id"] == stored["id"]
    assert len(stored_event["evidence"][0]["content_hash"]) == 64

    event_detail = client.get(f"/v1/events/{stored_event['id']}")
    assert event_detail.status_code == 200
    assert event_detail.json()["data"] == stored_event

    missing_event = client.get("/v1/events/evt_00000000000000000000000000")
    assert missing_event.status_code == 404
    assert missing_event.json()["code"] == "RESOURCE_NOT_FOUND"

    cached = client.get(
        f"/v1/signals/{signal_id}",
        headers={"If-None-Match": signal.headers["ETag"]},
    )
    assert cached.status_code == 304


def test_news_coverage_aggregates_bounded_signal_rejection_reasons() -> None:
    timestamp = datetime(2026, 8, 18, 12, tzinfo=UTC)
    news = NewsRecord(
        id="news_rejected_company",
        source_id="interfax",
        external_id="rejected-company",
        published_at=timestamp,
        received_at=timestamp,
        title="Сбербанк провел встречу сообщества",
        url="https://example.com/rejected-company",
        content="Участники обсудили общественные инициативы.",
        language="ru",
        source_metadata={
            "event_candidate": True,
            "analysis_candidate": True,
            "signal_candidate": True,
            "tickers": ["SBER"],
            "processing_status": "processed",
            "signal_outcome": {
                "status": "rejected_after_analysis",
                "reason": "event_other",
                "signal_count": 0,
                "model_version": "signal-engine-0.6.1",
            },
        },
        created_at=timestamp,
    )

    payload = main_module.build_news_response_payload(
        [news],
        [],
        source_id=None,
        scope="company",
        limit=100,
    )

    coverage = payload["meta"]["processing_coverage"]
    assert coverage["rejection_reasons"] == {"event_other": 1}
    company_coverage = payload["meta"]["company_coverage"]
    sber = next(item for item in company_coverage["items"] if item["ticker"] == "SBER")
    assert sber["status"] == "awaiting_signal"
    assert sber["relevant_news"] == 1
    assert sber["analysis_candidates"] == 1
    assert sber["signaled_news"] == 0


def test_public_news_and_event_surfaces_hide_unrouted_storage_noise() -> None:
    timestamp = datetime(2026, 8, 18, 12, tzinfo=UTC)
    common = {
        "source_id": "interfax",
        "published_at": timestamp,
        "received_at": timestamp,
        "url": "https://example.com/news",
        "content": "Проверочный материал.",
        "language": "ru",
        "created_at": timestamp,
    }
    relevant = NewsRecord(
        id="news_relevant_market_event",
        external_id="relevant-market-event",
        title="Банк России изменил ключевую ставку",
        source_metadata={"event_candidate": True, "analysis_candidate": True},
        **common,
    )
    irrelevant = NewsRecord(
        id="news_unrouted_storage_noise",
        external_id="unrouted-storage-noise",
        title="Спортивная команда провела товарищеский матч",
        source_metadata={
            "event_candidate": False,
            "analysis_candidate": False,
            "signal_candidate": False,
        },
        **common,
    )

    news_payload = main_module.build_news_response_payload(
        [relevant, irrelevant],
        [],
        source_id=None,
        scope=None,
        limit=100,
    )
    events = main_module.build_market_event_records([relevant, irrelevant], [])

    assert [item["id"] for item in news_payload["data"]] == [relevant.id]
    assert news_payload["meta"]["total"] == 1
    assert news_payload["meta"]["excluded_irrelevant"] == 1
    assert news_payload["meta"]["scope_counts"] == {
        "market": 1,
        "sector": 0,
        "company": 0,
    }
    assert news_payload["meta"]["sources"][0]["count"] == 1
    assert news_payload["meta"]["processing_coverage"]["stored"] == 2
    assert news_payload["meta"]["processing_coverage"]["relevant"] == 1
    assert len(events) == 1
    assert events[0]["news_ids"] == [relevant.id]


def test_signal_list_rejects_unknown_direction() -> None:
    response = client.get("/v1/signals", params={"direction": "up,sideways"})

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PARAMETER"


def test_news_ingestion_rejects_idempotency_key_reuse_with_new_payload() -> None:
    headers = {"Idempotency-Key": "interfax-news-conflict"}
    first = client.post("/v1/internal/news", json=NEWS_PAYLOAD, headers=headers)
    changed = {**NEWS_PAYLOAD, "content": "Содержимое было изменено."}

    conflict = client.post("/v1/internal/news", json=changed, headers=headers)

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.headers["content-type"].startswith("application/problem+json")
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_news_ingestion_requires_timezone_and_well_formed_key() -> None:
    naive_time = {**NEWS_PAYLOAD, "published_at": "2026-08-08T10:18:00"}

    invalid_time = client.post(
        "/v1/internal/news",
        json=naive_time,
        headers={"Idempotency-Key": "valid-key-001"},
    )
    invalid_key = client.post(
        "/v1/internal/news",
        json=NEWS_PAYLOAD,
        headers={"Idempotency-Key": "bad key"},
    )

    assert invalid_time.status_code == 400
    assert invalid_time.json()["code"] == "INVALID_PARAMETER"
    assert invalid_time.json()["errors"][0]["location"].startswith("body")
    assert invalid_key.status_code == 400
    assert invalid_key.json()["code"] == "INVALID_PARAMETER"


def test_unknown_job_is_explicit() -> None:
    response = client.get("/v1/jobs/job_01JZK7AHMXYCG6A5T9D1B8R3QP")

    assert response.status_code == 404
    assert response.json()["code"] == "JOB_NOT_FOUND"


def test_instrument_snapshot_exposes_market_data_and_etag() -> None:
    original = app.state.market_data_client

    class FakeMarketDataClient:
        async def snapshot(self, ticker: str) -> dict[str, object]:
            return {
                "ticker": ticker,
                "name": "Сбербанк",
                "last_price": "283.65",
                "currency": "RUB",
                "observed_at": "2026-08-08T16:00:08Z",
                "daily_change_pct": 0.41,
                "volume_shares": 3_204_170,
                "value_rub": 908_086_982.0,
                "lot_size": 1,
                "liquidity_status": "sufficient",
                "daily_volatility_pct": 1.4,
                "annualized_volatility_pct": 22.22,
                "candles": [],
                "source": {"name": "MOEX ISS", "url": "https://iss.moex.com/iss/"},
            }

    app.state.market_data_client = FakeMarketDataClient()
    try:
        response = client.get("/v1/instruments/SBER/snapshot")
        cached = client.get(
            "/v1/instruments/SBER/snapshot",
            headers={"If-None-Match": response.headers["ETag"]},
        )
    finally:
        app.state.market_data_client = original

    assert response.status_code == 200
    assert response.json()["data"]["market"]["last_price"] == "283.65"
    assert response.json()["data"]["market"]["source"]["name"] == "MOEX ISS"
    assert cached.status_code == 304


def test_batch_instrument_snapshots_return_partial_results() -> None:
    original = app.state.market_data_client

    class FakeMarketDataClient:
        async def snapshot(self, ticker: str) -> dict[str, object]:
            if ticker == "MISS":
                from eventedge.market import InstrumentNotFoundError

                raise InstrumentNotFoundError(ticker)
            return {
                "ticker": ticker,
                "name": ticker,
                "last_price": "100.0",
                "currency": "RUB",
                "observed_at": "2026-08-08T16:00:08Z",
                "daily_change_pct": 0.2,
                "volume_shares": 10,
                "value_rub": 1000.0,
                "lot_size": 1,
                "liquidity_status": "limited",
                "daily_volatility_pct": 1.0,
                "annualized_volatility_pct": 15.87,
                "candles": [],
                "source": {"name": "MOEX ISS", "url": "https://iss.moex.com/iss/"},
            }

    app.state.market_data_client = FakeMarketDataClient()
    try:
        response = client.get(
            "/v1/instruments/snapshots",
            params={"tickers": "SBER,LKOH,MISS,SBER"},
        )
    finally:
        app.state.market_data_client = original

    assert response.status_code == 200
    assert [item["ticker"] for item in response.json()["data"]] == ["SBER", "LKOH"]
    assert response.json()["errors"] == [{"ticker": "MISS", "code": "INSTRUMENT_NOT_FOUND"}]
    assert response.json()["meta"] == {
        "requested": 3,
        "returned": 2,
        "refresh_after_seconds": 30,
    }


def test_batch_instrument_snapshots_reuse_content_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = app.state.market_data_client

    class FakeMarketDataClient:
        async def snapshot(self, ticker: str) -> dict[str, object]:
            return {
                "ticker": ticker,
                "name": ticker,
                "last_price": "100.0",
                "currency": "RUB",
                "observed_at": "2026-08-08T16:00:08Z",
                "daily_change_pct": 0.2,
                "volume_shares": 10,
                "value_rub": 1000.0,
                "lot_size": 1,
                "liquidity_status": "limited",
                "daily_volatility_pct": 1.0,
                "annualized_volatility_pct": 15.87,
                "candles": [],
                "source": {"name": "MOEX ISS", "url": "https://iss.moex.com/iss/"},
            }

    load_content = AsyncMock(return_value=([], []))
    monkeypatch.setattr(main_module, "load_content_snapshot", load_content)
    repository_read = AsyncMock(side_effect=AssertionError("must reuse content snapshot"))
    monkeypatch.setattr(main_module, "active_signals_by_ticker", repository_read)
    app.state.market_data_client = FakeMarketDataClient()
    try:
        response = client.get(
            "/v1/instruments/snapshots",
            params={"tickers": "SBER"},
        )
    finally:
        app.state.market_data_client = original

    assert response.status_code == 200
    assert response.json()["meta"]["returned"] == 1
    load_content.assert_awaited_once()
    repository_read.assert_not_awaited()


def test_intraday_candles_endpoint_returns_ten_minute_series() -> None:
    original = app.state.market_data_client

    class FakeMarketDataClient:
        async def candles(
            self,
            ticker: str,
            *,
            interval: int,
            lookback_days: int,
        ) -> dict[str, object]:
            assert interval == 10
            assert lookback_days == 14
            return {
                "ticker": ticker,
                "interval_minutes": interval,
                "observed_at": "2026-08-07T07:10:00Z",
                "candles": [
                    {
                        "begin": "2026-08-07T07:10:00Z",
                        "open": 283.1,
                        "close": 283.8,
                        "high": 284.0,
                        "low": 282.9,
                        "value_rub": 52_000_000.0,
                        "volume_shares": 184_220,
                    }
                ],
                "source": {"name": "MOEX ISS", "url": "https://iss.moex.com/iss/"},
            }

    app.state.market_data_client = FakeMarketDataClient()
    try:
        response = client.get(
            "/v1/instruments/SBER/candles",
            params={"interval": 10, "lookback_days": 14},
        )
    finally:
        app.state.market_data_client = original

    assert response.status_code == 200
    assert response.json()["data"]["interval_minutes"] == 10
    assert response.json()["meta"]["refresh_after_seconds"] == 60


def test_intraday_candles_endpoint_rejects_unsupported_interval() -> None:
    response = client.get("/v1/instruments/SBER/candles", params={"interval": 5})

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PARAMETER"


def test_batch_instrument_snapshots_validate_tickers() -> None:
    response = client.get(
        "/v1/instruments/snapshots",
        params={"tickers": "SBER,INVALID-TICKER"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PARAMETER"


def test_assessments_cover_companies_without_fresh_news_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = app.state.market_data_client
    snapshot_store = AsyncMock(side_effect=lambda _key, payload, **_kwargs: dict(payload))
    monkeypatch.setattr(
        app.state.news_repository,
        "get_or_create_assessment_snapshot",
        snapshot_store,
    )

    class FakeMarketDataClient:
        async def snapshot(self, ticker: str) -> dict[str, object]:
            return {
                "ticker": ticker,
                "name": ticker,
                "last_price": "105.0",
                "currency": "RUB",
                "observed_at": "2026-08-08T16:00:08Z",
                "daily_change_pct": 1.4,
                "volume_shares": 2_000_000,
                "value_rub": 300_000_000.0,
                "lot_size": 1,
                "liquidity_status": "sufficient",
                "daily_volatility_pct": 1.6,
                "annualized_volatility_pct": 25.4,
                "candles": [
                    {
                        "begin": f"2026-08-0{day}T07:00:00Z",
                        "open": 99 + day,
                        "close": 100 + day,
                        "high": 101 + day,
                        "low": 98 + day,
                        "value_rub": 300_000_000.0,
                        "volume_shares": 1_000_000 + day * 10_000,
                    }
                    for day in range(1, 7)
                ],
                "source": {"name": "MOEX ISS", "url": "https://iss.moex.com/iss/"},
            }

    app.state.market_data_client = FakeMarketDataClient()
    try:
        response = client.get("/v1/assessments", params={"tickers": "SBER,LKOH"})
    finally:
        app.state.market_data_client = original

    assert response.status_code == 200
    assert [item["ticker"] for item in response.json()["data"]] == ["SBER", "LKOH"]
    assert all(item["assessment_type"] in {"hybrid", "quant"} for item in response.json()["data"])
    assert all(item["config_version"] == 3 for item in response.json()["data"])
    assert all(item["market_context"]["is_signal"] is False for item in response.json()["data"])
    assert all(
        item["market_context"]["bias_direction"] in {"up", "neutral", "down"}
        for item in response.json()["data"]
    )
    assert all(
        item["market_scenario"]["low_pct"] == -item["market_scenario"]["high_pct"]
        for item in response.json()["data"]
    )
    assert all(
        item["market_scenario"]["method"] == "realized_volatility_sqrt_time_v1"
        for item in response.json()["data"]
    )
    assert {
        "price_reaction",
        "volume",
        "volatility",
        "liquidity",
        "reporting",
    } <= {
        factor["code"]
        for item in response.json()["data"]
        for factor in item["factor_contributions"]
    }
    assert response.json()["meta"]["returned"] == 2
    assert response.json()["meta"]["market_biases"] == 2
    assert response.json()["meta"]["snapshot_as_of"] == "2026-08-08T16:00:08Z"
    assert response.json()["meta"]["snapshot_id"].startswith("market_")
    assert response.headers["etag"] == f'"{response.json()["meta"]["snapshot_id"]}"'
    assert response.headers["cache-control"] == "public, max-age=15, stale-while-revalidate=15"
    snapshot_store.assert_awaited_once()
    assert ":" in snapshot_store.await_args.args[0]


def test_evals_endpoint_exposes_analysis_and_downloads() -> None:
    original = app.state.market_data_client

    class FakeMarketDataClient:
        def __init__(self) -> None:
            self.calls = 0

        async def candles(
            self,
            ticker: str,
            *,
            interval: int,
            lookback_days: int,
        ) -> dict[str, object]:
            self.calls += 1
            assert interval == 10
            assert lookback_days == 14
            return {
                "ticker": ticker,
                "interval_minutes": 10,
                "observed_at": "2026-08-11T10:20:00Z",
                "candles": [
                    {"begin": "2026-08-08T10:20:00Z", "open": 100, "close": 100},
                    {"begin": "2026-08-08T11:20:00Z", "open": 100, "close": 101},
                    {"begin": "2026-08-09T10:20:00Z", "open": 101, "close": 103},
                    {"begin": "2026-08-11T10:20:00Z", "open": 103, "close": 105},
                ],
                "source": {"name": "MOEX ISS", "url": "https://iss.moex.com/iss/"},
            }

    fake_market = FakeMarketDataClient()
    app.state.market_data_client = fake_market
    try:
        asyncio.run(
            main_module._load_evaluation_material(
                app.state.news_repository,
                fake_market,
            )
        )
        assert fake_market.calls > 0
        fake_market.calls = 0
        response = client.get("/v1/evals")
        csv_export = client.get(
            "/v1/evals/export",
            params={"format": "csv", "dataset": "outcomes"},
        )
        timeseries_export = client.get(
            "/v1/evals/export",
            params={"format": "json", "dataset": "timeseries"},
        )
    finally:
        app.state.market_data_client = original

    assert response.status_code == 200
    assert set(response.json()["data"]) == {
        "summary",
        "breakdowns",
        "relationships",
        "quality_series",
        "outcomes",
    }
    assert len(response.json()["data"]["breakdowns"]["by_horizon"]) == 4
    assert response.json()["meta"]["primary_horizon"] == "4h"
    assert (
        response.json()["meta"]["evaluation_scope"]
        == "company_directional_signals_only"
    )
    assert response.json()["meta"]["model_epochs"]
    assert all(
        outcome["direction"] in {"up", "down"} for outcome in response.json()["data"]["outcomes"]
    )
    assert "point-in-time" in response.json()["meta"]["warning"]
    assert csv_export.status_code == 200
    assert csv_export.headers["content-type"].startswith("text/csv")
    assert "eventedge-outcomes" in csv_export.headers["content-disposition"]
    assert "signal_id,ticker,signal_as_of" in csv_export.text
    assert timeseries_export.status_code == 200
    assert timeseries_export.json()["meta"]["dataset"] == "timeseries"
    assert timeseries_export.json()["meta"]["truncated"] is False
    assert fake_market.calls == 0


def test_evals_export_paginates_filters_config_and_compresses_full_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = MemoryNewsRepository()
    evaluated_at = datetime(2026, 8, 31, 12, tzinfo=UTC)

    def outcome(signal_id: str, config_version: int) -> dict[str, object]:
        return {
            "signal_id": signal_id,
            "ticker": "SBER",
            "direction": "up",
            "model_version": "signal-engine-0.6.1",
            "config_version": config_version,
        }

    for config_version, signal_ids in ((1, ("sig_cfg1_a", "sig_cfg1_b")), (2, ("sig_cfg2",))):
        asyncio.run(
            repository.upsert_evaluation_epoch(
                EvaluationEpochRecord(
                    epoch_id=f"eval_cfg_{config_version}",
                    model_version="signal-engine-0.6.1",
                    config_version=config_version,
                    evaluated_at=evaluated_at,
                    outcomes=tuple(outcome(signal_id, config_version) for signal_id in signal_ids),
                    observations=(),
                )
            )
        )
        asyncio.run(
            repository.upsert_evaluation_observations(
                [
                    {
                        "signal_id": signal_id,
                        "ticker": "SBER",
                        "signal_as_of": "2026-08-31T10:00:00Z",
                        "observation_at": f"2026-08-31T1{index}:00:00Z",
                        "model_version": "signal-engine-0.6.1",
                        "config_version": config_version,
                    }
                    for index, signal_id in enumerate(signal_ids)
                ],
                evaluated_at=evaluated_at,
            )
        )

    monkeypatch.setattr(app.state, "news_repository", repository)
    first = client.get(
        "/v1/evals/export",
        params={
            "format": "json",
            "dataset": "timeseries",
            "model_version": "signal-engine-0.6.1",
            "config_version": 1,
            "limit": 1,
        },
    )
    second = client.get(
        "/v1/evals/export",
        params={
            "format": "json",
            "dataset": "timeseries",
            "model_version": "signal-engine-0.6.1",
            "config_version": 1,
            "limit": 1,
            "cursor": 1,
        },
    )
    full = client.get(
        "/v1/evals/export",
        params={
            "format": "json",
            "dataset": "timeseries",
            "model_version": "all",
            "download": "true",
        },
    )

    assert first.status_code == 200
    assert first.json()["meta"] == {
        "dataset": "timeseries",
        "rows": 1,
        "total_rows": 2,
        "truncated": False,
        "generated_at": first.json()["meta"]["generated_at"],
        "model_version": "signal-engine-0.6.1",
        "config_version": 1,
        "has_more": True,
        "next_cursor": 1,
        "limit": 1,
    }
    assert second.json()["meta"]["has_more"] is False
    assert second.json()["meta"]["next_cursor"] is None
    assert full.status_code == 200
    assert full.headers["content-encoding"] == "gzip"
    assert full.json()["meta"]["rows"] == 3
    assert full.json()["meta"]["total_rows"] == 3
    assert {row["config_version"] for row in full.json()["data"]} == {1, 2}


def test_complete_eval_outcome_is_reused_without_moex_request() -> None:
    async def scenario() -> None:
        repository = MemoryNewsRepository()
        timestamp = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1)
        document = NewsDocument(
            source_id="interfax",
            external_id="incremental-eval-sber",
            published_at=timestamp,
            received_at=timestamp,
            title="Сбербанк опубликовал сильную отчётность",
            url="https://example.com/incremental-eval-sber",
            content="Чистая прибыль выросла на 20% и оказалась выше ожиданий.",
            language="ru",
            source_metadata={"signal_candidate": True},
            payload_hash="incremental-eval-sber-payload",
        )
        await repository.ingest("incremental-eval-sber-key", document)
        signal = (
            await repository.list_signals(
                ticker=None,
                directions=None,
                status=None,
                min_confidence=None,
                limit=10,
            )
        )[0]
        cached_outcome = {
            "signal_id": signal.id,
            "ticker": signal.ticker,
            "direction": signal.direction,
            "evaluation_methodology": EVALUATION_METHODOLOGY_VERSION,
            "eligibility": {
                "eligible": True,
                "reason": None,
                "decision_at": signal.created_at.isoformat(),
                "processing_lag_seconds": 60,
            },
            "status": "evaluated",
            "returns": {"1h": 0.2, "4h": 0.5, "1d": 0.8, "3d": 1.1},
            "horizon_observations": {"3d": {"timely": True}},
            "verdict": True,
        }
        cached_observation = {
            "signal_id": signal.id,
            "signal_as_of": signal.as_of.isoformat(),
            "observation_at": "2026-08-01T11:00:00Z",
            "return_pct": 0.2,
        }
        await repository.upsert_evaluation_epoch(
            EvaluationEpochRecord(
                epoch_id="eval_cached_current",
                model_version=signal.model_version,
                config_version=signal.config_version,
                evaluated_at=datetime(2026, 8, 5, tzinfo=UTC),
                outcomes=(cached_outcome,),
                observations=(cached_observation,),
            )
        )

        class NoMoexExpected:
            async def candles(self, *args: object, **kwargs: object) -> dict[str, object]:
                raise AssertionError("settled signal must reuse its persisted outcome")

        outcomes, _, candles, _, epochs = await main_module._load_evaluation_material(
            repository,
            NoMoexExpected(),  # type: ignore[arg-type]
        )

        assert outcomes == [cached_outcome]
        assert candles == {}
        current = next(epoch for epoch in epochs if epoch.model_version == signal.model_version)
        assert current.observations == ()
        archived = await repository.list_evaluation_observations()
        assert [
            {
                "signal_id": row["signal_id"],
                "observation_at": row["observation_at"],
            }
            for row in archived
        ] == [
            {
                "signal_id": cached_observation["signal_id"],
                "observation_at": cached_observation["observation_at"],
            }
        ]

    asyncio.run(scenario())


def test_eval_refresh_never_drops_previous_outcomes_for_the_same_config() -> None:
    async def scenario() -> None:
        repository = MemoryNewsRepository()
        timestamp = datetime(2026, 8, 30, 10, tzinfo=UTC)
        old_news = NewsRecord(
            id="news_eval_old",
            source_id="interfax",
            external_id="eval-old",
            published_at=timestamp,
            received_at=timestamp,
            title="Сбербанк опубликовал отчётность",
            url="https://example.com/eval-old",
            content="Сбербанк опубликовал отчётность.",
            language="ru",
            source_metadata={"classification_status": "hidden_after_publication"},
            created_at=timestamp,
        )
        new_news = replace(
            old_news,
            id="news_eval_new",
            external_id="eval-new",
            title="Газпром обновил прогноз",
            url="https://example.com/eval-new",
            published_at=timestamp + timedelta(hours=1),
            received_at=timestamp + timedelta(hours=1),
            created_at=timestamp + timedelta(hours=1),
        )

        def signal(signal_id: str, news: NewsRecord, ticker: str) -> SignalRecord:
            return SignalRecord(
                id=signal_id,
                news_id=news.id,
                ticker=ticker,
                as_of=news.received_at,
                data_cutoff_at=news.received_at,
                status="expired",
                direction="up",
                action="consider_buy",
                horizon_value=3,
                horizon_unit="calendar_days",
                score=40.0,
                strength=0.4,
                confidence=0.8,
                summary="Исторический сигнал.",
                factor_contributions=(),
                evidence_refs=(news.id,),
                expires_at=news.received_at + timedelta(days=3),
                invalidation_conditions=(),
                model_version="signal-engine-0.6.1",
                config_version=3,
                created_at=news.received_at,
            )

        old_signal = signal("sig_eval_old", old_news, "SBER")
        new_signal = signal("sig_eval_new", new_news, "GAZP")
        repository._news = {old_news.id: old_news, new_news.id: new_news}
        repository._signals = {old_signal.id: old_signal, new_signal.id: new_signal}
        old_outcome = {
            "signal_id": old_signal.id,
            "ticker": old_signal.ticker,
            "direction": old_signal.direction,
            "evaluation_methodology": EVALUATION_METHODOLOGY_VERSION,
            "eligibility": {"eligible": True, "reason": None},
            "status": "evaluated",
            "returns": {"1h": 0.1, "4h": 0.2, "1d": 0.3, "3d": 0.4},
            "horizon_observations": {"3d": {"timely": True}},
        }
        await repository.upsert_evaluation_epoch(
            EvaluationEpochRecord(
                epoch_id="eval_previous_config_3",
                model_version="signal-engine-0.6.1",
                config_version=3,
                evaluated_at=timestamp,
                outcomes=(old_outcome,),
                observations=(),
            )
        )

        class EmptyMarket:
            async def candles(self, *args: object, **kwargs: object) -> dict[str, object]:
                return {"candles": []}

        await main_module._load_evaluation_material(
            repository,
            EmptyMarket(),  # type: ignore[arg-type]
        )

        current = main_module.latest_model_evaluation_epoch(
            main_module.latest_evaluation_epochs(
                await repository.list_evaluation_epochs()
            ),
            "signal-engine-0.6.1",
            3,
        )
        assert current is not None
        assert {outcome["signal_id"] for outcome in current.outcomes} == {
            old_signal.id,
            new_signal.id,
        }

    asyncio.run(scenario())


def test_complete_outcome_rebuilds_raw_rows_when_legacy_epoch_was_truncated() -> None:
    async def scenario() -> None:
        repository = MemoryNewsRepository()
        timestamp = datetime.now(UTC).replace(microsecond=0) - timedelta(days=4)
        news = NewsRecord(
            id="news_truncated_raw",
            source_id="interfax",
            external_id="truncated-raw",
            published_at=timestamp,
            received_at=timestamp,
            title="Сбербанк опубликовал отчётность",
            url="https://example.com/truncated-raw",
            content="Чистая прибыль выросла.",
            language="ru",
            source_metadata={},
            created_at=timestamp,
        )
        signal = SignalRecord(
            id="sig_truncated_raw",
            news_id=news.id,
            ticker="SBER",
            as_of=timestamp,
            data_cutoff_at=timestamp,
            status="expired",
            direction="up",
            action="consider_buy",
            horizon_value=3,
            horizon_unit="calendar_days",
            score=40.0,
            strength=0.4,
            confidence=0.8,
            summary="Исторический сигнал.",
            factor_contributions=(),
            evidence_refs=(news.id,),
            expires_at=timestamp + timedelta(days=3),
            invalidation_conditions=(),
            model_version="signal-engine-0.6.1",
            config_version=3,
            created_at=timestamp,
        )
        repository._news[news.id] = news
        repository._signals[signal.id] = signal
        await repository.upsert_evaluation_epoch(
            EvaluationEpochRecord(
                epoch_id="eval_truncated_raw",
                model_version=signal.model_version,
                config_version=signal.config_version,
                evaluated_at=timestamp + timedelta(days=3),
                outcomes=(
                    {
                        "signal_id": signal.id,
                        "ticker": signal.ticker,
                        "direction": signal.direction,
                        "evaluation_methodology": EVALUATION_METHODOLOGY_VERSION,
                        "eligibility": {"eligible": True, "reason": None},
                        "status": "evaluated",
                        "returns": {"1h": 0.1, "4h": 0.2, "1d": 0.3, "3d": 0.4},
                        "horizon_observations": {"3d": {"timely": True}},
                    },
                ),
                observations=(
                    {
                        "signal_id": signal.id,
                        "signal_as_of": to_rfc3339(signal.as_of),
                        "observation_at": to_rfc3339(timestamp + timedelta(minutes=10)),
                    },
                ),
                observations_truncated=True,
            )
        )

        class HistoricalMarket:
            calls = 0

            async def candles(
                self,
                ticker: str,
                *,
                interval: int,
                lookback_days: int,
            ) -> dict[str, object]:
                self.calls += 1
                assert ticker == "SBER"
                assert interval == 10
                assert lookback_days >= 14
                return {
                    "candles": [
                        {
                            "begin": to_rfc3339(timestamp + offset),
                            "open": price,
                            "close": price,
                        }
                        for offset, price in (
                            (timedelta(minutes=10), 100.0),
                            (timedelta(hours=1), 101.0),
                            (timedelta(days=1), 102.0),
                            (timedelta(days=3), 103.0),
                        )
                    ]
                }

        market = HistoricalMarket()
        await main_module._load_evaluation_material(
            repository,
            market,  # type: ignore[arg-type]
        )

        assert market.calls == 1
        archived = await repository.list_evaluation_observations()
        assert len(archived) == 4
        assert {row["signal_id"] for row in archived} == {signal.id}
        rebuilt_epoch = main_module.latest_model_evaluation_epoch(
            main_module.latest_evaluation_epochs(
                await repository.list_evaluation_epochs()
            ),
            signal.model_version,
            signal.config_version,
        )
        assert rebuilt_epoch is not None
        assert rebuilt_epoch.observations_truncated is False

    asyncio.run(scenario())


def test_legacy_or_incomplete_eval_outcome_is_not_treated_as_settled() -> None:
    legacy = {
        "status": "evaluated",
        "returns": {"3d": 1.0},
    }
    missing_timing_audit = {
        **legacy,
        "evaluation_methodology": EVALUATION_METHODOLOGY_VERSION,
        "eligibility": {"eligible": True},
    }

    assert main_module._is_complete_eval_outcome(legacy) is False
    assert main_module._is_complete_eval_outcome(missing_timing_audit) is False


def test_current_eval_method_is_selected_without_deleting_legacy_epoch() -> None:
    current = EvaluationEpochRecord(
        epoch_id="eval_current_method",
        model_version="signal-engine-0.6.1",
        config_version=1,
        evaluated_at=datetime(2026, 8, 15, tzinfo=UTC),
        outcomes=(
            {
                "signal_id": "sig_current",
                "evaluation_methodology": EVALUATION_METHODOLOGY_VERSION,
            },
        ),
        observations=(),
    )
    newer_legacy = replace(
        current,
        epoch_id="eval_legacy",
        evaluated_at=datetime(2026, 8, 16, tzinfo=UTC),
        outcomes=({"signal_id": "sig_legacy"},),
    )

    selected = main_module.latest_evaluation_epochs([newer_legacy, current])

    assert [epoch.epoch_id for epoch in selected] == ["eval_current_method"]
    assert main_module.evaluation_epoch_methodology(newer_legacy) == "legacy-or-mixed"


def test_evals_selects_highest_config_before_newest_evaluation_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lower_config = EvaluationEpochRecord(
        epoch_id="eval_config_1",
        model_version="signal-engine-0.6.1",
        config_version=1,
        evaluated_at=datetime(2026, 8, 18, 15, 2, tzinfo=UTC),
        outcomes=(
            {
                "signal_id": "sig_config_1",
                "ticker": "SBER",
                "direction": "up",
                "evaluation_methodology": EVALUATION_METHODOLOGY_VERSION,
                "status": "excluded",
                "eligibility": {"eligible": False, "reason": "test_fixture"},
            },
        ),
        observations=(),
    )
    current_config = replace(
        lower_config,
        epoch_id="eval_config_2",
        config_version=2,
        evaluated_at=datetime(2026, 8, 18, 14, 57, tzinfo=UTC),
        outcomes=(
            {
                "signal_id": "sig_config_2",
                "ticker": "SBER",
                "direction": "down",
                "evaluation_methodology": EVALUATION_METHODOLOGY_VERSION,
                "status": "excluded",
                "eligibility": {"eligible": False, "reason": "test_fixture"},
            },
        ),
    )
    repository = MemoryNewsRepository()
    asyncio.run(repository.upsert_evaluation_epoch(lower_config))
    asyncio.run(repository.upsert_evaluation_epoch(current_config))
    list_epochs = AsyncMock(wraps=repository.list_evaluation_epochs)
    count_observations = AsyncMock(wraps=repository.count_evaluation_observations)
    repository.list_evaluation_epochs = list_epochs  # type: ignore[method-assign]
    repository.count_evaluation_observations = count_observations  # type: ignore[method-assign]
    monkeypatch.setattr(app.state, "news_repository", repository)
    app.state.evaluation_epoch_index_cache = None

    response = client.get("/v1/evals")
    lower_response = client.get(
        "/v1/evals",
        params={"model_version": "signal-engine-0.6.1", "config_version": 1},
    )

    assert response.status_code == 200
    assert response.json()["meta"]["selected_config_version"] == 2
    assert response.json()["meta"]["generated_at"] == "2026-08-18T14:57:00Z"
    assert [row["signal_id"] for row in response.json()["data"]["outcomes"]] == [
        "sig_config_2"
    ]
    assert lower_response.status_code == 200
    assert lower_response.json()["meta"]["selected_config_version"] == 1
    assert [
        row["signal_id"] for row in lower_response.json()["data"]["outcomes"]
    ] == ["sig_config_1"]
    assert list_epochs.await_count == 1
    assert count_observations.await_count == 1

    app.state.evaluation_epoch_index_cache["loaded_at"] = 0.0
    list_epochs.side_effect = RuntimeError("temporary YDB read failure")
    count_observations.side_effect = RuntimeError("temporary YDB read failure")
    stale = client.get(
        "/v1/evals",
        params={"model_version": "signal-engine-0.6.1", "config_version": 2},
    )
    assert stale.status_code == 200
    assert stale.json()["meta"]["selected_config_version"] == 2


def test_retrospective_signal_is_excluded_without_moex_request() -> None:
    async def scenario() -> None:
        class ExactNewsRepository(MemoryNewsRepository):
            requested_news_ids: frozenset[str] | None = None

            async def list_news(self, *, source_id: str | None, limit: int) -> list[NewsRecord]:
                raise AssertionError("eval refresh must not scan an unrelated recent-news window")

            async def get_news_by_ids(self, news_ids: frozenset[str]) -> list[NewsRecord]:
                self.requested_news_ids = news_ids
                return await super().get_news_by_ids(news_ids)

        repository = ExactNewsRepository()
        historical = datetime.now(UTC).replace(microsecond=0) - timedelta(days=2)
        await repository.ingest(
            "retrospective-eval-key",
            NewsDocument(
                source_id="interfax",
                external_id="retrospective-eval-sber",
                published_at=historical,
                received_at=historical + timedelta(minutes=1),
                title="Сбербанк опубликовал сильную отчётность",
                url="https://example.com/retrospective-eval-sber",
                content="Чистая прибыль выросла на 20% и оказалась выше ожиданий.",
                language="ru",
                source_metadata={"signal_candidate": True},
                payload_hash="retrospective-eval-sber-payload",
            ),
        )

        class NoMoexExpected:
            async def candles(self, *args: object, **kwargs: object) -> dict[str, object]:
                raise AssertionError("retrospective signals must not request market outcomes")

        outcomes, _, candles, _, _ = await main_module._load_evaluation_material(
            repository,
            NoMoexExpected(),  # type: ignore[arg-type]
        )

        assert candles == {}
        assert outcomes[0]["status"] == "excluded"
        assert outcomes[0]["eligibility"]["reason"] == "retrospective_signal"
        assert outcomes[0]["evaluation_methodology"] == EVALUATION_METHODOLOGY_VERSION
        assert repository.requested_news_ids == {outcomes[0]["news"]["id"]}

    asyncio.run(scenario())


def test_context_signal_is_excluded_from_evaluation() -> None:
    async def scenario() -> None:
        repository = MemoryNewsRepository()
        timestamp = datetime(2026, 8, 10, 7, tzinfo=UTC)
        news = NewsRecord(
            id="news_market",
            source_id="telegram_bbbreaking",
            external_id="market-1",
            published_at=timestamp,
            received_at=timestamp,
            title="Санкционные риски усилились для российского рынка",
            url="https://example.com/market-1",
            content="Новые ограничения повышают риски для российских акций.",
            language="ru",
            source_metadata={"scope": "market"},
            created_at=timestamp,
        )
        signal = SignalRecord(
            id="sig_market",
            news_id=news.id,
            ticker="RUEQ",
            as_of=timestamp,
            data_cutoff_at=timestamp,
            status="active",
            direction="down",
            action="risk_off",
            horizon_value=3,
            horizon_unit="calendar_days",
            score=-45.0,
            strength=0.45,
            confidence=0.8,
            summary="Негативный рыночный контекст",
            factor_contributions=(),
            evidence_refs=(news.id,),
            expires_at=timestamp.replace(day=13),
            invalidation_conditions=(),
            model_version="signal-engine-0.6.1",
            config_version=1,
            created_at=timestamp,
        )
        repository._news[news.id] = news
        repository._signals[signal.id] = signal

        class NoMarketDataExpected:
            calls: list[str] = []

            async def candles(
                self,
                ticker: str,
                *,
                interval: int,
                lookback_days: int,
            ) -> dict[str, object]:
                self.calls.append(ticker)
                raise AssertionError("hidden context signals must not request market outcomes")

        market = NoMarketDataExpected()
        outcomes, signals, _, _, epochs = await main_module._load_evaluation_material(
            repository,
            market,  # type: ignore[arg-type]
        )

        assert market.calls == []
        assert signals == []
        assert outcomes == []
        assert epochs == []

    asyncio.run(scenario())


def test_instrument_snapshot_does_not_revive_hidden_exchange_noise() -> None:
    noisy_news = {
        **NEWS_PAYLOAD,
        "source_id": "moex_news",
        "external_id": "moex-hidden-ydex-001",
        "title": "О начале торгов ценными бумагами YDEX",
        "content": "Механическое уведомление биржевого контура по YDEX.",
    }
    accepted = client.post(
        "/v1/internal/news",
        json=noisy_news,
        headers={"Idempotency-Key": "moex-hidden-ydex-001"},
    )
    assert accepted.status_code == 202

    original = app.state.market_data_client

    class FakeMarketDataClient:
        async def snapshot(self, ticker: str) -> dict[str, object]:
            return {
                "ticker": ticker,
                "name": "Яндекс",
                "last_price": "3995.5",
                "currency": "RUB",
                "observed_at": "2026-08-08T16:00:08Z",
                "daily_change_pct": 0.41,
                "volume_shares": 10,
                "value_rub": 1_000.0,
                "lot_size": 1,
                "liquidity_status": "limited",
                "daily_volatility_pct": 2.0,
                "annualized_volatility_pct": 31.75,
                "candles": [],
                "source": {"name": "MOEX ISS", "url": "https://iss.moex.com/iss/"},
            }

    app.state.market_data_client = FakeMarketDataClient()
    try:
        response = client.get("/v1/instruments/YDEX/snapshot")
    finally:
        app.state.market_data_client = original

    assert response.status_code == 200
    assert response.json()["data"]["active_signal"] is None
    assert response.json()["data"]["scenario"] is None
