"""Health endpoint and correlation-id behaviour."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.constants import CORRELATION_ID_HEADER


def test_live_returns_ok(client: TestClient) -> None:
    response = client.get("/health/live")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["environment"] == "test"


def test_ready_returns_ok(client: TestClient) -> None:
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_correlation_id_is_generated(client: TestClient) -> None:
    response = client.get("/health/live")
    assert response.headers.get(CORRELATION_ID_HEADER)


def test_correlation_id_is_echoed(client: TestClient) -> None:
    response = client.get("/health/live", headers={CORRELATION_ID_HEADER: "trace-123"})
    assert response.headers[CORRELATION_ID_HEADER] == "trace-123"
