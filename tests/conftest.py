"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Environment, Settings
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    """Settings that depend on nothing outside this file.

    ``_env_file=None`` and the explicit ``database_url``/``jwt_secret_key``
    ignore any ambient ``APP_*`` variables and any local ``.env``. Without this
    the default suite is only accidentally offline: a developer (or a shell that
    just ran Alembic) with ``APP_DATABASE_URL`` exported would silently give
    these tests a real database, and results would differ between machines.
    Tests that need either value set one explicitly.
    """

    return Settings(
        _env_file=None,
        environment=Environment.TEST,
        log_json=False,
        database_url=None,
        jwt_secret_key=None,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
