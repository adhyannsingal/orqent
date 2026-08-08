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
from app.infrastructure.repositories.organization_repository import OrganizationRepository
from app.infrastructure.repositories.refresh_token_repository import RefreshTokenRepository
from app.infrastructure.repositories.role_repository import RoleRepository
from app.infrastructure.repositories.user_repository import UserRepository
from app.infrastructure.repositories.workflow_repository import WorkflowRepository
from app.infrastructure.repositories.workflow_version_repository import (
    WorkflowVersionRepository,
)

REPOSITORIES = {
    "organizations": OrganizationRepository,
    "users": UserRepository,
    "roles": RoleRepository,
    "refresh_tokens": RefreshTokenRepository,
    "workflows": WorkflowRepository,
    "workflow_versions": WorkflowVersionRepository,
}


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


# --- Repository wiring ------------------------------------------------------


@pytest.mark.parametrize(("name", "expected_type"), REPOSITORIES.items())
async def test_exposes_each_repository(
    session_factory: async_sessionmaker[AsyncSession],
    name: str,
    expected_type: type,
) -> None:
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert isinstance(getattr(uow, name), expected_type)


async def test_all_repositories_share_the_unit_of_work_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The whole point of the pattern: writes through different repositories must
    # land in one transaction, which only holds if they share one session.
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        sessions = {getattr(uow, name)._session for name in REPOSITORIES}

        assert sessions == {uow.session}


@pytest.mark.parametrize("name", list(REPOSITORIES))
async def test_repositories_are_cached_per_unit_of_work(
    session_factory: async_sessionmaker[AsyncSession], name: str
) -> None:
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert getattr(uow, name) is getattr(uow, name)


@pytest.mark.parametrize("name", list(REPOSITORIES))
async def test_repositories_unavailable_outside_context(
    session_factory: async_sessionmaker[AsyncSession], name: str
) -> None:
    # Handing back a repository bound to no session would fail later and
    # further away; failing on access keeps the error next to the mistake.
    uow = SqlAlchemyUnitOfWork(session_factory)
    with pytest.raises(RuntimeError):
        _ = getattr(uow, name)


async def test_repositories_are_rebuilt_on_reentry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A cached repository would otherwise still hold the closed session from the
    # previous block, silently writing into a dead transaction.
    uow = SqlAlchemyUnitOfWork(session_factory)

    async with uow:
        first = uow.users
        first_session = uow.session
    async with uow:
        second = uow.users
        assert second._session is uow.session

    assert first is not second
    assert second._session is not first_session
