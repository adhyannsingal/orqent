"""Unit of Work transaction semantics (async, in-memory SQLite)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # StaticPool keeps a single in-memory connection so the schema persists
    # across sessions within the test.
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _count(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        result = await session.execute(text("SELECT COUNT(*) FROM t"))
        return int(result.scalar_one())


async def test_commit_persists(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.session.execute(text("INSERT INTO t (v) VALUES ('a')"))
        await uow.commit()
    assert await _count(session_factory) == 1


async def test_uncommitted_work_is_rolled_back(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.session.execute(text("INSERT INTO t (v) VALUES ('b')"))
        # No commit — __aexit__ must roll back.
    assert await _count(session_factory) == 0


async def test_exception_rolls_back(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(RuntimeError):
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            await uow.session.execute(text("INSERT INTO t (v) VALUES ('c')"))
            raise RuntimeError("boom")
    assert await _count(session_factory) == 0


async def test_session_unavailable_outside_context(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    uow = SqlAlchemyUnitOfWork(session_factory)
    with pytest.raises(RuntimeError):
        _ = uow.session
