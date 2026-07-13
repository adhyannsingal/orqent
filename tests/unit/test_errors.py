"""Centralized error handling produces the standard envelope."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.errors import register_exception_handlers
from app.domain.errors import ConflictError, NotFoundError


def _build_client() -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/missing")
    def _missing() -> None:
        raise NotFoundError("agent not found")

    @app.get("/conflict")
    def _conflict() -> None:
        raise ConflictError()

    return TestClient(app, raise_server_exceptions=False)


def test_not_found_maps_to_404_envelope() -> None:
    response = _build_client().get("/missing")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["message"] == "agent not found"
    assert body["error"]["details"] == []


def test_conflict_uses_default_message() -> None:
    response = _build_client().get("/conflict")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
