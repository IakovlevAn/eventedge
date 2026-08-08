import pytest
from fastapi.testclient import TestClient

from eventedge.main import app

client = TestClient(app)

NEWS_PAYLOAD = {
    "source_id": "interfax",
    "external_id": "news-2026-08-08-001",
    "published_at": "2026-08-08T10:18:00Z",
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
                            "event_type": (
                                "yandex.cloud.events.serverless.triggers.TimerMessage"
                            )
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
        "collectors": {
            "cbr_press": {"fetched": 2, "accepted": 1, "replayed": 1}
        },
    }


def test_signal_list_has_contract_shape_and_etag() -> None:
    response = client.get("/v1/signals", params={"ticker": "SBER", "limit": 10})

    assert response.status_code == 200
    assert response.json() == {
        "data": [],
        "meta": {"limit": 10, "has_more": False, "next_cursor": None},
    }
    assert response.headers["ETag"].startswith('"')

    cached = client.get(
        "/v1/signals",
        params={"ticker": "SBER", "limit": 10},
        headers={"If-None-Match": response.headers["ETag"]},
    )
    assert cached.status_code == 304
    assert cached.content == b""


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
    assert signal.json()["data"]["model_version"] == "news-baseline-0.1.1"
    assert len(signal.json()["data"]["factor_contributions"]) == 5

    listed = client.get("/v1/signals", params={"ticker": "SBER", "direction": "up"})
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["data"]] == [signal_id]

    news = client.get("/v1/news", params={"source_id": "interfax"})
    assert news.status_code == 200
    stored = next(
        item
        for item in news.json()["data"]
        if item["external_id"] == NEWS_PAYLOAD["external_id"]
    )
    assert stored["title"] == NEWS_PAYLOAD["title"]
    assert stored["content"] == NEWS_PAYLOAD["content"]
    assert stored["related_signals"][0]["id"] == signal_id

    cached = client.get(
        f"/v1/signals/{signal_id}",
        headers={"If-None-Match": signal.headers["ETag"]},
    )
    assert cached.status_code == 304


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
