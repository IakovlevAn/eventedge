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


def test_liveness_is_public_and_traceable() -> None:
    response = client.get("/health/live", headers={"X-Request-Id": "test-request-123"})

    assert response.status_code == 200
    assert response.headers["X-Request-Id"] == "test-request-123"
    assert response.json()["status"] == "ok"
    assert response.json()["checked_at"].endswith("Z")


def test_invalid_request_id_is_replaced() -> None:
    response = client.get("/health/ready", headers={"X-Request-Id": "bad"})

    assert response.status_code == 200
    assert response.headers["X-Request-Id"].startswith("req_")


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
    assert accepted.json()["data"]["status"] == "queued"
    assert accepted.json()["data"]["kind"] == "news_ingestion"
    assert replayed.headers["Idempotency-Replayed"] == "true"
    assert accepted.headers["Location"] == f"/v1/jobs/{accepted.json()['data']['id']}"

    job = client.get(accepted.headers["Location"])
    assert job.status_code == 200
    assert job.json() == accepted.json()


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
