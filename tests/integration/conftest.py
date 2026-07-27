"""Fixtures for tests that need a real MySQL.

Repositories cannot be verified without SQL, and SQLite is not a stand-in for
this schema: ``users.email_active`` is a generated column defined with MySQL's
``IF()``, and the models use MySQL-dialect ``BIGINT UNSIGNED`` and
``DATETIME(fsp=6)``. Creating the tables on SQLite fails outright, so a fake
database here would test something the application never runs against.

These tests therefore need the compose stack up and migrated::

    docker compose up -d mysql
    alembic upgrade head
    pytest -m integration

Every test runs inside a transaction that is rolled back afterwards, so the
database is left exactly as it was found and tests cannot leak state into one
another.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

DATABASE_URL = os.getenv("APP_DATABASE_URL", "mysql+asyncmy://app:app@127.0.0.1:3306/app")


@pytest.fixture
async def connection() -> AsyncIterator[AsyncConnection]:
    """Yield a connection inside a transaction that is always rolled back.

    Everything a test does — including work a session commits — happens inside
    this transaction, so rolling it back leaves the database exactly as found.
    """

    engine = create_async_engine(DATABASE_URL, poolclass=None)
    try:
        conn = await engine.connect()
    except SQLAlchemyError as exc:  # pragma: no cover - environment dependent
        await engine.dispose()
        pytest.skip(f"MySQL is not reachable at {DATABASE_URL}: {exc}")

    transaction = await conn.begin()
    try:
        yield conn
    finally:
        await transaction.rollback()
        await conn.close()
        await engine.dispose()


def _session_factory(conn: AsyncConnection) -> async_sessionmaker[AsyncSession]:
    """Build sessions that nest inside the test's transaction.

    ``join_transaction_mode="create_savepoint"`` is what makes the isolation
    hold for tests that provoke an ``IntegrityError``, and for any code that
    calls ``commit``. Without it, the session operates directly on the outer
    transaction: a failed statement or a commit ends it, and teardown then has
    nothing left to roll back — isolation that works only by accident. With a
    savepoint the session unwinds to, or commits, the savepoint instead, leaving
    the outer transaction alive to be rolled back.
    """

    return async_sessionmaker(
        bind=conn,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


@pytest.fixture
async def session(connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """Yield a single session for tests that drive repositories directly."""

    db_session = _session_factory(connection)()
    try:
        yield db_session
    finally:
        await db_session.close()


@pytest.fixture
def session_factory(connection: AsyncConnection) -> async_sessionmaker[AsyncSession]:
    """Yield a session *factory*, for code that opens its own units of work."""

    return _session_factory(connection)
