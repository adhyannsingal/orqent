"""Health endpoint and correlation-id behaviour."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Environment, Settings
from app.core.constants import CORRELATION_ID_HEADER
from app.main import create_app


def test_live_returns_ok(client: TestClient) -> None:
    response = client.get("/health/live")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["environment"] == "test"


def test_live_ignores_the_database(client: TestClient) -> None:
    # Liveness drives container restarts, so it must not fail when a dependency
    # is down — restarting the process would not fix the database.
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json()["components"] == []


# --- Readiness --------------------------------------------------------------


def test_ready_reports_down_without_a_database(client: TestClient) -> None:
    # The shared fixture pins database_url to None regardless of the ambient
    # environment. Reporting ready here is exactly the stub behaviour this
    # replaced.
    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "down"


def test_ready_names_the_failing_component(client: TestClient) -> None:
    body = client.get("/health/ready").json()

    assert body["components"] == [{"name": "mysql", "status": "down", "detail": "unreachable"}]


def test_ready_does_not_leak_connection_details(client: TestClient) -> None:
    # The endpoint is reachable without credentials, and driver errors quote
    # host names, ports, and user names.
    detail = client.get("/health/ready").json()["components"][0]["detail"]

    assert detail == "unreachable"
    for leak in ("mysql+asyncmy", "password", "127.0.0.1", "root"):
        assert leak not in detail


def test_ready_reports_down_when_the_database_is_unreachable() -> None:
    # A syntactically valid URL pointing at nothing: the failure happens on
    # connect, not on configuration, which is the realistic outage.
    settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        log_json=False,
        database_url="mysql+asyncmy://app:app@127.0.0.1:59999/app",
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "down"


@pytest.fixture
def reachable_app() -> Iterator[FastAPI]:
    """An app whose database probe succeeds, without needing a real MySQL.

    SQLite is unusable for this project's *schema* (see the integration suite),
    but ``SELECT 1`` is exactly the portable statement the probe runs, so it is
    a faithful stand-in for a reachable database here.
    """

    settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        log_json=False,
        database_url="sqlite+aiosqlite://",
    )
    yield create_app(settings)


def test_ready_returns_ok_when_the_database_answers(reachable_app: FastAPI) -> None:
    with TestClient(reachable_app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["components"] == [{"name": "mysql", "status": "ok", "detail": None}]


def test_ready_keeps_the_health_response_shape(reachable_app: FastAPI) -> None:
    # Readiness reports status; it does not raise, so it never becomes the
    # error envelope even when the answer is 503.
    with TestClient(reachable_app) as client:
        body = client.get("/health/ready").json()

    assert set(body) == {"status", "version", "environment", "components"}
    assert "error" not in body


# --- Correlation id ---------------------------------------------------------


def test_correlation_id_is_generated(client: TestClient) -> None:
    response = client.get("/health/live")
    assert response.headers.get(CORRELATION_ID_HEADER)


def test_correlation_id_is_echoed(client: TestClient) -> None:
    response = client.get("/health/live", headers={CORRELATION_ID_HEADER: "trace-123"})
    assert response.headers[CORRELATION_ID_HEADER] == "trace-123"
