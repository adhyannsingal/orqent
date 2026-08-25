"""Concurrent refresh against a real MySQL.

Rotation makes a refresh token single-use, and the only place that guarantee can
actually be tested is against a real database with real transactions: the whole
mechanism is ``SELECT ... FOR UPDATE`` taking a row lock, which no in-memory
double reproduces.

Unlike the rest of the integration suite, these tests commit for real — the
concurrency being tested *is* the interaction between separate committed
transactions, so they cannot run inside one rolled-back transaction. They clean
up after themselves explicitly instead.

Cleanup and assertions are both **scoped to the rows this module creates**. An
earlier version emptied ``organizations`` outright and counted every live
refresh token in the database, which made the suite depend on starting from an
empty schema: against a development database holding unrelated data it either
failed on a foreign key or measured somebody else's sessions. Neither says
anything about whether rotation is single-use.
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
from app.infrastructure.db.models.password_reset_token import PasswordResetToken
from app.infrastructure.db.models.refresh_token import RefreshToken
from app.infrastructure.db.models.user import User
from app.infrastructure.db.models.user_role import UserRole
from app.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.security.password_hasher import Argon2PasswordHasher
from app.infrastructure.security.token_service import JwtTokenService
from app.services.auth_service import AuthService
from tests.integration.conftest import DATABASE_URL
from tests.unit.fakes import FakePasswordResetNotifier

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
    """An AuthService whose transactions really commit, with scoped cleanup."""

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
        FakePasswordResetNotifier(),
        password_reset_ttl_seconds=1_800,
        password_reset_url_base="https://app.example.com/reset-password",
    )

    # A previous aborted run would collide with the unique email, so this
    # module clears its own rows first — and only its own.
    await _purge(factory)
    try:
        yield service, factory
    finally:
        await _purge(factory)
        await engine.dispose()


async def _purge(factory: async_sessionmaker[AsyncSession]) -> None:
    """Remove only this module's rows, children before parents.

    Scoped by the email this module registers. Emptying the tables would be
    simpler and wrong: a shared development database routinely holds unrelated
    data that such a fixture either destroys or trips over.
    """

    async with factory() as session:
        user_ids = list((await session.scalars(select(User.id).where(User.email == EMAIL))).all())
        organization_ids = list(
            (await session.scalars(select(User.organization_id).where(User.email == EMAIL))).all()
        )
        if user_ids:
            await session.execute(
                delete(PasswordResetToken).where(PasswordResetToken.user_id.in_(user_ids))
            )
            await session.execute(delete(RefreshToken).where(RefreshToken.user_id.in_(user_ids)))
            await session.execute(delete(UserRole).where(UserRole.user_id.in_(user_ids)))
            await session.execute(delete(User).where(User.id.in_(user_ids)))
        if organization_ids:
            await session.execute(delete(Organization).where(Organization.id.in_(organization_ids)))
        # `roles` is deliberately untouched: migration 0003 owns those rows.
        await session.commit()


async def _live_token_count(factory: async_sessionmaker[AsyncSession], user_id: int) -> int:
    """Live tokens belonging to *this* user.

    Scoped deliberately. An unscoped count only holds on an empty database, and
    would pass or fail on unrelated sessions — which is not the property under
    test.
    """

    async with factory() as session:
        return (
            await session.scalar(
                select(func.count())
                .select_from(RefreshToken)
                .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
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
    user = await service.register(email=EMAIL, password=PASSWORD, organization_name="Race Co")
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
    assert await _live_token_count(factory, user.id) == 0


async def test_sequential_refreshes_are_unaffected(
    committed_service: tuple[AuthService, async_sessionmaker[AsyncSession]],
) -> None:
    # The lock must not make ordinary, one-at-a-time rotation fail.
    service, factory = committed_service
    user = await service.register(email=EMAIL, password=PASSWORD, organization_name="Race Co")
    tokens = await service.login(email=EMAIL, password=PASSWORD)

    for _ in range(3):
        tokens = await service.refresh(tokens.refresh_token)

    assert await _live_token_count(factory, user.id) == 1


async def test_concurrent_refreshes_of_different_sessions_both_succeed(
    committed_service: tuple[AuthService, async_sessionmaker[AsyncSession]],
) -> None:
    # The lock is per row, so unrelated sessions never contend.
    service, factory = committed_service
    user = await service.register(email=EMAIL, password=PASSWORD, organization_name="Race Co")
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
    assert await _live_token_count(factory, user.id) == 2
