from fastapi.testclient import TestClient

from eventedge.main import app

client = TestClient(app)


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
