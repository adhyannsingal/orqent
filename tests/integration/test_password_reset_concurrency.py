"""Concurrent redemption of one password-reset link, against a real MySQL.

A reset token is single-use, and the only place that guarantee can actually be
tested is against a real database with real transactions: the mechanism is
``SELECT ... FOR UPDATE`` taking a row lock, which no in-memory double
reproduces. Checking ``consumed_at`` and then updating it later would pass every
sequential test in the suite and still let two simultaneous requests both
succeed — under MySQL's default REPEATABLE READ the second would read its own
snapshot and never see the first's write.

Like ``test_refresh_concurrency``, these commit for real, because the
concurrency under test *is* the interaction between separate committed
transactions. Cleanup is **scoped to the rows this module created** rather than
emptying the tables: a shared development database routinely holds unrelated
data, and a fixture that deletes everything either fails against it or destroys
it.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.domain.errors import AuthenticationError
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

SECRET = "reset-concurrency-secret-long-enough-32"
PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "a whole new passphrase 9!"
EMAIL = "reset-race@example.com"

# Generous: the point is to fail loudly rather than hang the suite if the row
# lock ever turns into a deadlock instead of a wait.
_TIMEOUT_SECONDS = 30


@pytest.fixture
async def committed() -> AsyncIterator[tuple[AuthService, FakePasswordResetNotifier, int]]:
    """A really-committing AuthService, a registered user, and that user's id."""

    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.connect() as probe:
            await probe.execute(select(1))
    except SQLAlchemyError as exc:  # pragma: no cover - environment dependent
        await engine.dispose()
        pytest.skip(f"MySQL is not reachable at {DATABASE_URL}: {exc}")

    factory = async_sessionmaker(engine, expire_on_commit=False)
    notifier = FakePasswordResetNotifier()
    service = AuthService(
        lambda: SqlAlchemyUnitOfWork(factory),
        Argon2PasswordHasher(),
        JwtTokenService(
            secret_key=SECRET,
            algorithm="HS256",
            access_ttl_seconds=900,
            refresh_ttl_seconds=2_592_000,
        ),
        notifier,
        password_reset_ttl_seconds=1_800,
        password_reset_url_base="https://app.example.com/reset-password",
    )

    # Any leftover from a previous aborted run would collide with the unique
    # email, so this module clears its own user first — and only its own.
    await _purge(factory)
    user = await service.register(email=EMAIL, password=PASSWORD, organization_name="Reset Race Co")
    user_id, organization_id = user.id, user.organization_id

    try:
        yield service, notifier, user_id
    finally:
        await _purge(factory, organization_id=organization_id)
        await engine.dispose()


async def _purge(
    factory: async_sessionmaker[AsyncSession], *, organization_id: int | None = None
) -> None:
    """Remove only this module's rows, children before parents."""

    async with factory() as session:
        user_ids = list((await session.scalars(select(User.id).where(User.email == EMAIL))).all())
        if user_ids:
            await session.execute(
                delete(PasswordResetToken).where(PasswordResetToken.user_id.in_(user_ids))
            )
            await session.execute(delete(RefreshToken).where(RefreshToken.user_id.in_(user_ids)))
            await session.execute(delete(UserRole).where(UserRole.user_id.in_(user_ids)))
            await session.execute(delete(User).where(User.id.in_(user_ids)))
        if organization_id is not None:
            await session.execute(delete(Organization).where(Organization.id == organization_id))
        # `roles` is deliberately untouched: migration 0003 owns those rows.
        await session.commit()


def _issued_token(notifier: FakePasswordResetNotifier) -> str:
    return notifier.sent[-1][1].rsplit("token=", 1)[1]


async def test_two_simultaneous_resets_change_the_password_once(
    committed: tuple[AuthService, FakePasswordResetNotifier, int],
) -> None:
    """The core guarantee: one link, one reset, no matter the timing.

    Both requests find the same row, but the second's locking read blocks until
    the first commits and then sees ``consumed_at`` set. The two submit
    *different* new passwords, so "at most one succeeded" is not merely a count
    — the surviving password names which one won.
    """

    service, notifier, _ = committed
    await service.forgot_password(email=EMAIL)
    token = _issued_token(notifier)

    async def attempt(new_password: str) -> str | None:
        try:
            await service.reset_password(token=token, new_password=new_password)
        except AuthenticationError:
            return None
        return new_password

    winners = await asyncio.wait_for(
        asyncio.gather(attempt(NEW_PASSWORD), attempt("a rival passphrase 3?")),
        timeout=_TIMEOUT_SECONDS,
    )

    succeeded = [outcome for outcome in winners if outcome is not None]
    assert len(succeeded) == 1

    # The password that won is the one now in force, and the loser's is not.
    assert await service.login(email=EMAIL, password=succeeded[0])
    for loser in {NEW_PASSWORD, "a rival passphrase 3?"} - set(succeeded):
        with pytest.raises(AuthenticationError):
            await service.login(email=EMAIL, password=loser)


async def test_a_concurrent_replay_leaves_exactly_one_consumed_grant(
    committed: tuple[AuthService, FakePasswordResetNotifier, int],
) -> None:
    # A second grant appearing, or the row being consumed twice with different
    # timestamps, would both mean the lock did not serialise the two.
    service, notifier, user_id = committed
    await service.forgot_password(email=EMAIL)
    token = _issued_token(notifier)

    async def attempt() -> None:
        with contextlib.suppress(AuthenticationError):
            await service.reset_password(token=token, new_password=NEW_PASSWORD)

    await asyncio.wait_for(asyncio.gather(attempt(), attempt()), timeout=_TIMEOUT_SECONDS)

    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            rows = (
                await session.scalars(
                    select(PasswordResetToken).where(PasswordResetToken.user_id == user_id)
                )
            ).all()
    finally:
        await engine.dispose()

    assert len(rows) == 1
    assert rows[0].consumed_at is not None
