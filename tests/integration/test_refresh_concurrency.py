"""Concurrent refresh against a real MySQL.

Rotation makes a refresh token single-use, and the only place that guarantee can
actually be tested is against a real database with real transactions: the whole
mechanism is ``SELECT ... FOR UPDATE`` taking a row lock, which no in-memory
double reproduces.

Unlike the rest of the integration suite, these tests commit for real — the
concurrency being tested *is* the interaction between separate committed
transactions, so they cannot run inside one rolled-back transaction. They clean
up after themselves explicitly instead.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.domain.errors import AuthenticationError
from app.domain.value_objects.token_pair import TokenPair
from app.infrastructure.db.models.organization import Organization
from app.infrastructure.db.models.refresh_token import RefreshToken
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.user_role import UserRole
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.security.password_hasher import Argon2PasswordHasher
from app.infrastructure.security.token_service import JwtTokenService
from app.services.auth_service import AuthService
from tests.integration.conftest import DATABASE_URL

pytestmark = pytest.mark.integration

SECRET = "concurrency-test-secret-long-enough-32"
PASSWORD = "correct horse battery staple"
EMAIL = "race@example.com"

# Generous: the point is to fail loudly rather than hang the suite if the row
# lock ever turns into a deadlock instead of a wait.
_TIMEOUT_SECONDS = 30


@pytest.fixture
async def committed_service() -> AsyncIterator[
    tuple[AuthService, async_sessionmaker[AsyncSession]]
]:
    """An AuthService whose transactions really commit, with explicit cleanup."""

    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as probe:
            await probe.execute(select(1))
    except SQLAlchemyError as exc:  # pragma: no cover - environment dependent
        await engine.dispose()
        pytest.skip(f"MySQL is not reachable at {DATABASE_URL}: {exc}")

    factory = async_sessionmaker(engine, expire_on_commit=False)

    service = AuthService(
        lambda: SqlAlchemyUnitOfWork(factory),
        Argon2PasswordHasher(),
        JwtTokenService(
            secret_key=SECRET,
            algorithm="HS256",
            access_ttl_seconds=900,
            refresh_ttl_seconds=2_592_000,
        ),
    )

    try:
        yield service, factory
    finally:
        # Order matters: children before parents, since the FKs are enforced.
        async with factory() as cleanup:
            await cleanup.execute(delete(RefreshToken))
            await cleanup.execute(delete(UserRole))
            await cleanup.execute(delete(User))
            await cleanup.execute(delete(Organization))
            # `roles` is deliberately untouched: migration 0003 owns those rows.
            await cleanup.commit()
        await engine.dispose()


async def _live_token_count(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        return (
            await session.scalar(
                select(func.count())
                .select_from(RefreshToken)
                .where(RefreshToken.revoked_at.is_(None))
            )
        ) or 0


async def test_two_simultaneous_refreshes_rotate_only_once(
    committed_service: tuple[AuthService, async_sessionmaker[AsyncSession]],
) -> None:
    """The core guarantee: one token, one rotation, no matter the timing.

    Both requests read the same row, but the second's locking read blocks until
    the first commits and then sees ``revoked_at`` set. Without the lock — or
    with a plain SELECT, which under REPEATABLE READ would serve a stale
    snapshot — both would rotate and the token would not be single-use.
    """

    service, _ = committed_service
    await service.register(email=EMAIL, password=PASSWORD, organization_name="Race Co")
    original = await service.login(email=EMAIL, password=PASSWORD)

    results = await asyncio.wait_for(
        asyncio.gather(
            service.refresh(original.refresh_token),
            service.refresh(original.refresh_token),
            return_exceptions=True,
        ),
        timeout=_TIMEOUT_SECONDS,
    )

    rotated = [r for r in results if isinstance(r, TokenPair)]
    rejected = [r for r in results if isinstance(r, AuthenticationError)]

    assert len(rotated) == 1, f"expected exactly one rotation, got {results}"
    assert len(rejected) == 1, f"expected exactly one rejection, got {results}"


async def test_losing_the_race_is_treated_as_reuse_and_kills_the_session(
    committed_service: tuple[AuthService, async_sessionmaker[AsyncSession]],
) -> None:
    """The loser cannot be told apart from a thief, so the family is revoked.

    This is deliberately conservative: a client that fires two refreshes at once
    ends up logged out. Failing closed is the right trade — the alternative is a
    window in which a stolen token is indistinguishable from a retry and
    survives.
    """

    service, factory = committed_service
    await service.register(email=EMAIL, password=PASSWORD, organization_name="Race Co")
    original = await service.login(email=EMAIL, password=PASSWORD)

    await asyncio.wait_for(
        asyncio.gather(
            service.refresh(original.refresh_token),
            service.refresh(original.refresh_token),
            return_exceptions=True,
        ),
        timeout=_TIMEOUT_SECONDS,
    )

    # The winner's successor is revoked too: reuse detection revokes the family,
    # not just the token presented.
    assert await _live_token_count(factory) == 0


async def test_sequential_refreshes_are_unaffected(
    committed_service: tuple[AuthService, async_sessionmaker[AsyncSession]],
) -> None:
    # The lock must not make ordinary, one-at-a-time rotation fail.
    service, factory = committed_service
    await service.register(email=EMAIL, password=PASSWORD, organization_name="Race Co")
    tokens = await service.login(email=EMAIL, password=PASSWORD)

    for _ in range(3):
        tokens = await service.refresh(tokens.refresh_token)

    assert await _live_token_count(factory) == 1


async def test_concurrent_refreshes_of_different_sessions_both_succeed(
    committed_service: tuple[AuthService, async_sessionmaker[AsyncSession]],
) -> None:
    # The lock is per row, so unrelated sessions never contend.
    service, factory = committed_service
    await service.register(email=EMAIL, password=PASSWORD, organization_name="Race Co")
    first = await service.login(email=EMAIL, password=PASSWORD)
    second = await service.login(email=EMAIL, password=PASSWORD)

    results = await asyncio.wait_for(
        asyncio.gather(
            service.refresh(first.refresh_token),
            service.refresh(second.refresh_token),
            return_exceptions=True,
        ),
        timeout=_TIMEOUT_SECONDS,
    )

    assert all(isinstance(result, TokenPair) for result in results), results
    assert await _live_token_count(factory) == 2
