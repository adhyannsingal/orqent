"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.rate_limit import reset_limiter
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
        # Off unless a test is about limiting. A suite that fires twenty
        # requests to check twenty payloads is not testing abuse, and leaving
        # the limiter on would make those tests fail for a reason that has
        # nothing to do with what they assert. The limiter suite turns it on
        # explicitly, which also keeps "is this test rate-limited?" visible at
        # the point of use rather than inherited from a default.
        rate_limit_enabled=False,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _clean_limiter() -> Iterator[None]:
    """Empty the process-wide limiter around every test.

    The limiter is deliberately process-global, so without this one test's
    requests would count against the next one's allowance and failures would
    depend on execution order. Cleared before *and* after, so a test that
    enables limiting leaves nothing behind either.
    """

    reset_limiter()
    yield
    reset_limiter()
